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
import json
from typing import Any

import fakeredis.aioredis
import pytest

from msai.core.halt_keys import fleet_halt_key, halt_cause_key
from msai.services.nautilus.disconnect_handler import IBDisconnectHandler


def _build_redis() -> Any:
    """Real in-memory Redis (``fakeredis`` with the ``lua`` extra) so the
    handler's atomic ``HALT_WRITE_LUA`` script runs for real and tests can
    assert on the canonical keys it writes (PR 1b T5 swapped the write path
    from ad-hoc ``:reason`` / ``:source`` SETs to the shared Lua script)."""
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


async def _read_str(redis: Any, key: str) -> str | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


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

    assert await redis.get(fleet_halt_key()) is None


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
    assert await redis.get(fleet_halt_key()) is None


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

    # Halt latch was set.
    assert await _read_str(redis, fleet_halt_key()) == "true"
    # The CANONICAL fleet cause records ib_disconnect (PR 1b T5 — no longer the
    # ad-hoc :reason / :source keys, which must NOT exist).
    cause = json.loads((await redis.get(halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "ib_disconnect"
    assert cause["deployment_id"] == "test"
    assert await redis.get("msai:risk:halt:reason") is None
    assert await redis.get("msai:risk:halt:source") is None


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

    assert await _read_str(redis, fleet_halt_key()) == "true"
    ttl = await redis.ttl(fleet_halt_key())
    assert 86000 < ttl <= 86400


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

    # Halt fired — latch is set.
    assert await _read_str(redis, fleet_halt_key()) == "true"

    # Now reconnect and try to keep running — but the loop has returned, so
    # nothing happens. The handler does NOT auto-clear the halt on reconnect.
    state["connected"] = True
    await asyncio.sleep(0.05)

    # The latch is STILL set — recovery never clears it (resume is operator-only).
    assert await _read_str(redis, fleet_halt_key()) == "true"


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
    assert await _read_str(redis, fleet_halt_key()) == "true"


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
    """Codex batch 10 P2 regression: previously a Redis write error swallowed
    silently and the halt was lost. The handler retries up to 5 times. PR 1b T5:
    the write is now ONE atomic ``HALT_WRITE_LUA`` ``eval`` call per attempt — so
    a first-attempt failure plus a second-attempt success is exactly 2 calls, and
    the canonical cause lands.
    """
    redis = _build_redis()
    real_eval = redis.eval
    call_count = {"n": 0}

    async def flaky_eval(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("redis blip")
        return await real_eval(*args, **kwargs)

    callback_fired = False

    async def on_halt() -> None:
        nonlocal callback_fired
        callback_fired = True

    import msai.services.nautilus.disconnect_handler as dh_mod

    real_backoff = dh_mod.HALT_SET_BACKOFF_S
    dh_mod.HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        redis.eval = flaky_eval  # type: ignore[assignment]
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
        dh_mod.HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]
        redis.eval = real_eval  # type: ignore[assignment]

    # Attempt 1 raised; attempt 2 succeeded — exactly 2 eval calls.
    assert call_count["n"] == 2
    assert callback_fired is True
    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await redis.get(halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "ib_disconnect"


@pytest.mark.asyncio
async def test_redis_set_all_retries_exhaust_still_fires_callback() -> None:
    """Codex batch 10 P3 iter 2: exercises the EXHAUSTED path — every halt write
    raises, the handler retries HALT_SET_MAX_ATTEMPTS times, gives up, and the
    on_halt callback STILL fires so a flatten hook runs regardless of Redis
    health. PR 1b T5: the write is one atomic ``eval`` per attempt, so the call
    count equals max attempts and nothing is written.
    """
    from msai.services.nautilus.disconnect_handler import HALT_SET_MAX_ATTEMPTS

    redis = _build_redis()
    real_eval = redis.eval
    call_count = {"n": 0}

    async def always_fails(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise RuntimeError("redis dead")

    callback_fired = False

    async def on_halt() -> None:
        nonlocal callback_fired
        callback_fired = True

    # Tighten the backoff so the test runs in ms instead of seconds.
    import msai.services.nautilus.disconnect_handler as dh_mod

    real_backoff = dh_mod.HALT_SET_BACKOFF_S
    dh_mod.HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        redis.eval = always_fails  # type: ignore[assignment]
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
        dh_mod.HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]
        redis.eval = real_eval  # type: ignore[assignment]

    # One eval per attempt, all failing → exactly max attempts.
    assert call_count["n"] == HALT_SET_MAX_ATTEMPTS
    # Callback STILL fired despite all retries failing.
    assert callback_fired is True
    # Nothing was written.
    assert await redis.get(fleet_halt_key()) is None


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
