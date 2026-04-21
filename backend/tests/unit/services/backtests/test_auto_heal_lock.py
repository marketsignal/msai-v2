"""Tests for the Redis-backed auto-heal dedupe lock (Task B4).

Covers:
  * ``build_lock_key`` — deterministic, symbol-order-insensitive key hash.
  * ``AutoHealLock.try_acquire`` — single-owner semantics (atomic NX SET).
  * ``AutoHealLock.release`` — holder-only release; non-holder is a no-op.
  * ``AutoHealLock.get_holder`` — inspect current holder string or None.
  * TTL-based auto-release — crashed holder cannot permanently deadlock.
  * Redis connection errors propagate — caller handles graceful degradation.

Uses :mod:`fakeredis` (async interface) so tests run without a real Redis.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fakeredis import aioredis as fakeredis_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

from msai.services.backtests.auto_heal_lock import (
    AutoHealLock,
    build_lock_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Provide an isolated fake async Redis client per test."""
    client = fakeredis_asyncio.FakeRedis()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def test_build_lock_key_normalizes_symbol_order() -> None:
    """Sorting symbols makes (AAPL, MSFT) and (MSFT, AAPL) hash identically."""
    key_a = build_lock_key(
        asset_class="stocks",
        symbols=["AAPL", "MSFT"],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    key_b = build_lock_key(
        asset_class="stocks",
        symbols=["MSFT", "AAPL"],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert key_a == key_b
    assert key_a.startswith("auto_heal:")


async def test_try_acquire_first_holder_wins(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    first = await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")
    second = await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h2")
    assert first is True
    assert second is False


async def test_try_acquire_releases_allow_reacquire(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    assert (await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")) is True
    await lock.release("auto_heal:test", holder_id="h1")
    assert (await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h2")) is True


async def test_release_only_by_holder(redis_client: Redis) -> None:
    """A non-holder's release() is a no-op — don't steal a lock you don't own."""
    lock = AutoHealLock(redis_client)
    assert (await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")) is True
    await lock.release("auto_heal:test", holder_id="h2")  # wrong holder
    assert (await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h3")) is False


async def test_get_holder_returns_value_when_locked(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")
    assert await lock.get_holder("auto_heal:test") == "h1"
    assert await lock.get_holder("auto_heal:missing") is None


# ---------------------------------------------------------------------------
# compare_and_swap — used by the orchestrator for placeholder → job_id handoff
# ---------------------------------------------------------------------------


async def test_compare_and_swap_succeeds_when_from_holder_matches(
    redis_client: Redis,
) -> None:
    """CAS swaps the value and resets TTL iff the current value matches from_holder."""
    lock = AutoHealLock(redis_client)
    key = "auto_heal:cas-positive"
    assert (await lock.try_acquire(key, ttl_s=60, holder_id="placeholder")) is True

    ok = await lock.compare_and_swap(
        key,
        from_holder="placeholder",
        to_holder="real-job-id",
        ttl_s=60,
    )
    assert ok is True
    assert await lock.get_holder(key) == "real-job-id"


async def test_compare_and_swap_fails_when_from_holder_mismatches(
    redis_client: Redis,
) -> None:
    """CAS is a no-op when the current value differs from from_holder — other's state preserved."""
    lock = AutoHealLock(redis_client)
    key = "auto_heal:cas-negative"
    assert (await lock.try_acquire(key, ttl_s=60, holder_id="someone-else")) is True

    ok = await lock.compare_and_swap(
        key,
        from_holder="placeholder",  # doesn't match
        to_holder="real-job-id",
        ttl_s=60,
    )
    assert ok is False
    # Other holder's value is preserved.
    assert await lock.get_holder(key) == "someone-else"


# ---------------------------------------------------------------------------
# US-004 coverage: TTL-based auto-release
# ---------------------------------------------------------------------------


async def test_lock_ttl_expiry_allows_new_acquisition(redis_client: Redis) -> None:
    """After TTL expires on a crashed holder, a new acquirer must succeed.

    PRD US-004 (bullet 4) calls for a watchdog to clear stale ingest locks
    on a dead holder; the shipped implementation deliberately relies on
    the Redis ``SET NX EX`` TTL (not a separate watchdog) for cleanup —
    see the silent-failure-hunter review note. This test pins the
    user-facing property: a dead holder cannot permanently deadlock ingest.

    We use a short TTL (1s) + :func:`asyncio.sleep` to let Redis expire
    the key naturally; fakeredis honors real wall-clock TTL, so no
    time-machine/freezegun is required.
    """
    lock = AutoHealLock(redis_client)
    key = "auto_heal:ttl-test"

    # Holder h1 "crashes" — acquires but never releases.
    assert (await lock.try_acquire(key, ttl_s=1, holder_id="h1")) is True

    # Sanity: TTL is counting down (strictly positive, at most ttl_s).
    ttl_remaining = await redis_client.ttl(key)
    assert 0 < ttl_remaining <= 1

    # While the lock is live, a second acquirer is rejected.
    assert (await lock.try_acquire(key, ttl_s=1, holder_id="h2")) is False

    # Sleep past TTL — Redis auto-expires the key.
    await asyncio.sleep(1.2)

    # New holder can now acquire; no watchdog needed.
    assert (await lock.try_acquire(key, ttl_s=60, holder_id="h3")) is True
    assert await lock.get_holder(key) == "h3"


# ---------------------------------------------------------------------------
# US-004 coverage: Redis-unavailable degraded path
# ---------------------------------------------------------------------------


async def test_try_acquire_redis_connection_error_raises_cleanly() -> None:
    """``try_acquire`` must NOT silence Redis connection errors.

    Contract: the lock is a thin primitive. The caller (``run_auto_heal``
    in the orchestrator) is responsible for graceful degradation when
    Redis is unreachable. Swallowing the error here would make the lock
    appear acquirable when it is actually in an unknown state — classic
    silent-failure vector.
    """
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(
        side_effect=RedisConnectionError("redis unreachable"),
    )

    lock = AutoHealLock(fake_redis)

    with pytest.raises(RedisConnectionError, match="redis unreachable"):
        await lock.try_acquire("auto_heal:degraded", ttl_s=60, holder_id="h1")


async def test_get_holder_redis_connection_error_raises_cleanly() -> None:
    """``get_holder`` must NOT silence Redis connection errors.

    Same contract as ``try_acquire``: surface the error so the caller can
    decide whether to retry, degrade, or fail the outer request.
    """
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(
        side_effect=RedisConnectionError("redis unreachable"),
    )

    lock = AutoHealLock(fake_redis)

    with pytest.raises(RedisConnectionError, match="redis unreachable"):
        await lock.get_holder("auto_heal:degraded")
