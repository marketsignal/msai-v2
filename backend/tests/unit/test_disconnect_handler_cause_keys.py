"""Canonical halt-cause-key tests for the IB disconnect handler (PR 1b Task 5).

The pre-existing bug: ``IBDisconnectHandler._fire_halt()`` wrote ad-hoc keys
``msai:risk:halt:reason`` + ``msai:risk:halt:source`` instead of the canonical
:func:`~msai.core.halt_keys.halt_cause_key('fleet')`. This left a divergent
cause representation that the API's ``/resume`` never cleared, leaving stale
cause residue after an operator resumed.

This module pins the corrected behavior: ``_fire_halt`` now writes the canonical
cause via the SAME shared :data:`~msai.core.halt_keys.HALT_WRITE_LUA` script the
data-stale monitor uses (atomic latch + ``:set_by`` / ``:set_at`` companions +
cause ONLY-IF-ABSENT + capped history LPUSH/LTRIM). It mirrors
``test_data_stale_monitor.py`` by using ``fakeredis.aioredis.FakeRedis`` (Lua via
the ``lua`` extra) so the script runs for real.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest

from msai.core.halt_keys import (
    HaltCause,
    fleet_halt_key,
    halt_cause_key,
)
from msai.services.nautilus.disconnect_handler import IBDisconnectHandler


async def _read_str(redis: Any, key: str) -> str | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


def _make_handler(redis: Any, *, deployment_slug: str = "dep-slug-1") -> IBDisconnectHandler:
    return IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug=deployment_slug,
        grace_seconds=0.01,
        poll_interval_s=0.005,
    )


# ===========================================================================
# Canonical cause-key write (replaces the ad-hoc :reason / :source SETs)
# ===========================================================================


@pytest.mark.asyncio
async def test_fire_halt_writes_canonical_cause_via_lua(redis: Any) -> None:
    """After ``_fire_halt`` the latch is set with the 24h TTL, the canonical
    fleet cause key holds the ``ib_disconnect`` cause JSON, the history list has
    one entry, and NO ad-hoc ``:reason`` / ``:source`` keys exist."""
    handler = _make_handler(redis, deployment_slug="dep-xyz")

    await handler._fire_halt()  # noqa: SLF001

    # Latch set with the 24h TTL.
    assert await _read_str(redis, fleet_halt_key()) == "true"
    ttl = await redis.ttl(fleet_halt_key())
    assert 86000 < ttl <= 86400

    # set_by / set_at companions present and attributed to the handler.
    set_by = await _read_str(redis, fleet_halt_key() + ":set_by")
    assert set_by is not None
    assert "ib_disconnect" in set_by
    assert "dep-xyz" in set_by
    assert await _read_str(redis, fleet_halt_key() + ":set_at")

    # Canonical cause key holds the ib_disconnect cause JSON.
    raw_cause = await redis.get(halt_cause_key("fleet"))
    assert raw_cause is not None
    cause = json.loads(raw_cause.decode())
    assert cause["reason"] == HaltCause.IB_DISCONNECT.value == "ib_disconnect"
    assert cause["deployment_id"] == "dep-xyz"
    assert "detected_at" in cause

    # History has exactly one entry, the ib_disconnect record.
    history = await redis.lrange(halt_cause_key("fleet") + ":history", 0, -1)
    assert len(history) == 1
    assert json.loads(history[0].decode())["reason"] == "ib_disconnect"

    # The ad-hoc keys are GONE.
    assert await redis.get("msai:risk:halt:reason") is None
    assert await redis.get("msai:risk:halt:source") is None


@pytest.mark.asyncio
async def test_fire_halt_preserves_existing_cause_and_appends_history(redis: Any) -> None:
    """A pre-existing data-stale cause is PRESERVED (SET NX) while the
    ib_disconnect record is still appended to the capped history list."""
    # Seed an existing data-stale cause first.
    await redis.set(
        halt_cause_key("fleet"),
        json.dumps({"reason": HaltCause.DATA_STALE.value, "dataset": "EQUS.MINI"}),
    )

    handler = _make_handler(redis)
    await handler._fire_halt()  # noqa: SLF001

    # The cause key still holds the ORIGINAL data-stale cause (unchanged).
    cause = json.loads((await redis.get(halt_cause_key("fleet"))).decode())
    assert cause["reason"] == HaltCause.DATA_STALE.value
    assert cause["dataset"] == "EQUS.MINI"

    # History grew by one — the ib_disconnect record.
    history = await redis.lrange(halt_cause_key("fleet") + ":history", 0, -1)
    parsed = [json.loads(h.decode()) for h in history]
    assert any(c["reason"] == "ib_disconnect" for c in parsed)


@pytest.mark.asyncio
async def test_fire_halt_retries_lua_on_first_failure(redis: Any) -> None:
    """A transient error on the FIRST Lua call is retried with backoff; the
    second attempt succeeds and the canonical cause lands."""
    handler = _make_handler(redis)

    calls = {"n": 0}
    real_eval = redis.eval

    async def flaky_eval(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis blip")
        return await real_eval(*args, **kwargs)

    import msai.services.nautilus.disconnect_handler as dh_mod

    real_backoff = dh_mod.HALT_SET_BACKOFF_S
    dh_mod.HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        redis.eval = flaky_eval  # type: ignore[assignment]
        await handler._fire_halt()  # noqa: SLF001
    finally:
        dh_mod.HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]
        redis.eval = real_eval  # type: ignore[assignment]

    assert calls["n"] == 2
    assert await _read_str(redis, fleet_halt_key()) == "true"
    cause = json.loads((await redis.get(halt_cause_key("fleet"))).decode())
    assert cause["reason"] == "ib_disconnect"


@pytest.mark.asyncio
async def test_fire_halt_calls_on_halt_callback(redis: Any) -> None:
    """Surgical write-path swap preserves the ``on_halt`` callback contract."""
    fired = False

    async def on_halt() -> None:
        nonlocal fired
        fired = True

    handler = IBDisconnectHandler(
        redis=redis,
        is_connected=lambda: False,
        deployment_slug="dep-slug-1",
        grace_seconds=0.01,
        poll_interval_s=0.005,
        on_halt=on_halt,
    )

    await handler._fire_halt()  # noqa: SLF001

    assert fired is True
    assert await _read_str(redis, fleet_halt_key()) == "true"


@pytest.mark.asyncio
async def test_fire_halt_callback_fires_even_when_lua_exhausts(redis: Any) -> None:
    """If every Lua call fails, the handler gives up on the write but the
    ``on_halt`` flatten hook STILL runs (fail-closed local shutdown path)."""
    from msai.services.nautilus.disconnect_handler import HALT_SET_MAX_ATTEMPTS

    calls = {"n": 0}

    async def always_fails(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("redis dead")

    fired = False

    async def on_halt() -> None:
        nonlocal fired
        fired = True

    import msai.services.nautilus.disconnect_handler as dh_mod

    real_backoff = dh_mod.HALT_SET_BACKOFF_S
    real_eval = redis.eval
    dh_mod.HALT_SET_BACKOFF_S = 0.001  # type: ignore[assignment]
    try:
        redis.eval = always_fails  # type: ignore[assignment]
        handler = IBDisconnectHandler(
            redis=redis,
            is_connected=lambda: False,
            deployment_slug="dep-slug-1",
            grace_seconds=0.01,
            poll_interval_s=0.005,
            on_halt=on_halt,
        )
        await handler._fire_halt()  # noqa: SLF001
    finally:
        dh_mod.HALT_SET_BACKOFF_S = real_backoff  # type: ignore[assignment]
        redis.eval = real_eval  # type: ignore[assignment]

    assert calls["n"] == HALT_SET_MAX_ATTEMPTS
    assert fired is True
    # Nothing was written (every attempt failed).
    assert await redis.get(fleet_halt_key()) is None
