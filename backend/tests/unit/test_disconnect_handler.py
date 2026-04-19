"""Unit tests for the IB disconnect handler (Phase 4 task 4.2).

We do NOT spin up a real IB Gateway or Nautilus runtime here.
The handler is built to be unit-testable: it takes a
``Callable[[], bool]`` for the connection state and a Redis
client. The tests inject both, drive the loop with a
controlled clock, and assert the kill switch fires (or
doesn't) at the expected moments.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from msai.services.nautilus.disconnect_handler import IBDisconnectHandler


def _build_redis() -> Any:
    """Stub async Redis with the two methods the handler
    actually calls. Records every set so tests can assert."""
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    return redis


@pytest.mark.asyncio
async def test_no_halt_when_always_connected() -> None:
    """Steady-state happy path: connection check always
    returns True, the loop runs a few iterations, no halt
    fires."""
    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: True,
        deployment_slug="test",
        grace_seconds=10.0,
        poll_interval_s=0.01,
    )
    stop = asyncio.Event()

    async def stop_after() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(handler.run(stop), stop_after())

    redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_no_halt_on_quick_reconnect() -> None:
    """Disconnect for less than grace, then reconnect — no
    halt should fire."""
    state = {"connected": True}

    async def flip_disconnect_then_reconnect() -> None:
        await asyncio.sleep(0.02)
        state["connected"] = False
        await asyncio.sleep(0.05)
        state["connected"] = True

    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: state["connected"],
        deployment_slug="test",
        grace_seconds=1.0,  # well above the 50ms disconnect window
        poll_interval_s=0.01,
    )
    stop = asyncio.Event()

    async def stop_after() -> None:
        await asyncio.sleep(0.15)
        stop.set()

    await asyncio.gather(handler.run(stop), flip_disconnect_then_reconnect(), stop_after())

    # No halt — the reconnect happened inside the grace window
    redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_halt_fires_when_grace_expires() -> None:
    """Disconnect that lasts longer than grace_seconds — halt
    must fire and the handler returns immediately (one-shot)."""
    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,  # always disconnected
        deployment_slug="test",
        grace_seconds=0.05,
        poll_interval_s=0.01,
    )
    stop = asyncio.Event()

    # The handler returns by itself once the grace expires —
    # we don't need to set the stop event.
    await asyncio.wait_for(handler.run(stop), timeout=1.0)

    # Halt flag was set
    set_calls = redis.set.call_args_list
    keys = [call.args[0] for call in set_calls]
    assert "msai:risk:halt" in keys
    assert "msai:risk:halt:reason" in keys
    # Reason marks ib_disconnect
    reason_call = next(call for call in set_calls if call.args[0] == "msai:risk:halt:reason")
    assert reason_call.args[1] == "ib_disconnect"


@pytest.mark.asyncio
async def test_halt_includes_24h_ttl() -> None:
    """The halt flag carries the same 24h TTL the API's
    /kill-all uses, so disconnect halts and manual halts
    have identical recovery behavior."""
    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug="test",
        grace_seconds=0.01,
        poll_interval_s=0.005,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(handler.run(stop), timeout=1.0)

    halt_call = next(call for call in redis.set.call_args_list if call.args[0] == "msai:risk:halt")
    assert halt_call.kwargs.get("ex") == 86400


@pytest.mark.asyncio
async def test_no_auto_resume_after_halt() -> None:
    """Once the loop fires the halt, it returns. Even if the
    connection comes back, the handler does NOT automatically
    clear the halt — operators must POST /resume."""
    state = {"connected": False}
    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: state["connected"],
        deployment_slug="test",
        grace_seconds=0.02,
        poll_interval_s=0.005,
    )
    stop = asyncio.Event()

    await asyncio.wait_for(handler.run(stop), timeout=1.0)

    # Halt fired
    initial_set_count = redis.set.call_count

    # Now reconnect and try to keep running — but the loop
    # has returned, so nothing happens. To prove it, simulate
    # the reconnect and verify no DELETE on the halt key.
    state["connected"] = True
    await asyncio.sleep(0.05)

    # No additional Redis writes happened after the loop
    # returned (it's one-shot)
    assert redis.set.call_count == initial_set_count
    # And specifically, no delete of the halt key was issued
    redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_connection_check_exception_treated_as_disconnect() -> None:
    """A probe error must be treated as 'still disconnected'
    so the loop fails closed (cautious)."""

    def boom() -> bool:
        raise RuntimeError("ib gateway probe failed")

    redis = _build_redis()
    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=boom,
        deployment_slug="test",
        grace_seconds=0.02,
        poll_interval_s=0.005,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(handler.run(stop), timeout=1.0)

    # The loop treated each failed probe as disconnected and
    # eventually fired the halt
    assert any(call.args[0] == "msai:risk:halt" for call in redis.set.call_args_list)


@pytest.mark.asyncio
async def test_on_halt_callback_invoked() -> None:
    """The optional ``on_halt`` callback is awaited after the
    Redis flag is set so callers can hook a flatten / cleanup
    action."""
    redis = _build_redis()
    callback_fired = False

    async def on_halt() -> None:
        nonlocal callback_fired
        callback_fired = True

    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug="test",
        grace_seconds=0.01,
        poll_interval_s=0.005,
        on_halt=on_halt,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(handler.run(stop), timeout=1.0)

    assert callback_fired is True


@pytest.mark.asyncio
async def test_redis_set_failure_retries_then_succeeds() -> None:
    """Codex batch 10 P2 regression: previously a Redis SET
    error swallowed silently and the halt was lost. The new
    behavior retries up to 5 times. Verify the retry path
    when the FIRST attempt fails but the SECOND succeeds.
    """
    call_count = {"n": 0}

    async def flaky_set(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        # First attempt's first set call fails; everything
        # after succeeds. The handler retries the WHOLE
        # 3-key sequence on failure, so attempt 2 makes
        # 3 fresh set calls.
        if call_count["n"] == 1:
            raise RuntimeError("redis blip")
        return True

    redis = AsyncMock()
    redis.set = flaky_set

    callback_fired = False

    async def on_halt() -> None:
        nonlocal callback_fired
        callback_fired = True

    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug="test",
        grace_seconds=0.01,
        poll_interval_s=0.005,
        on_halt=on_halt,
    )
    stop = asyncio.Event()
    await asyncio.wait_for(handler.run(stop), timeout=5.0)

    # Attempt 1 = 1 failed call; attempt 2 = 3 successful
    # calls (one for each halt key)
    assert call_count["n"] == 4
    assert callback_fired is True


@pytest.mark.asyncio
async def test_redis_set_all_retries_exhaust_still_fires_callback() -> None:
    """Codex batch 10 P3 iter 2: the previous test only
    exercised the eventual-success path. This one exercises
    the EXHAUSTED path: every Redis SET raises, the handler
    retries _HALT_SET_MAX_ATTEMPTS times, gives up, and the
    on_halt callback STILL fires so a flatten hook runs
    regardless of Redis health.
    """
    from msai.services.nautilus.disconnect_handler import _HALT_SET_MAX_ATTEMPTS

    call_count = {"n": 0}

    async def always_fails(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        raise RuntimeError("redis dead")

    redis = AsyncMock()
    redis.set = always_fails

    callback_fired = False

    async def on_halt() -> None:
        nonlocal callback_fired
        callback_fired = True

    # Tighten the backoff so the test runs in ms instead of
    # seconds
    import msai.services.nautilus.disconnect_handler as dh_mod

    real_backoff = dh_mod._HALT_SET_BACKOFF_S
    dh_mod._HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        handler = IBDisconnectHandler(
            redis=redis,
            is_connected=lambda: False,
            deployment_slug="test",
            grace_seconds=0.01,
            poll_interval_s=0.005,
            on_halt=on_halt,
        )
        stop = asyncio.Event()
        await asyncio.wait_for(handler.run(stop), timeout=5.0)
    finally:
        dh_mod._HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]

    # Each attempt makes ONE set call (the first one always
    # raises, so the remaining 2 of the 3-key sequence
    # never execute). Total = max attempts.
    assert call_count["n"] == _HALT_SET_MAX_ATTEMPTS
    # Callback STILL fired despite all retries failing
    assert callback_fired is True


@pytest.mark.asyncio
async def test_on_halt_callback_failure_does_not_propagate() -> None:
    """If the callback raises, the handler logs and exits
    cleanly — the halt flag is already set, the callback
    failure is just metadata."""
    redis = _build_redis()

    async def boom() -> None:
        raise RuntimeError("flatten failed")

    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug="test",
        grace_seconds=0.01,
        poll_interval_s=0.005,
        on_halt=boom,
    )
    stop = asyncio.Event()
    # Must NOT raise even though the callback raises
    await asyncio.wait_for(handler.run(stop), timeout=1.0)
