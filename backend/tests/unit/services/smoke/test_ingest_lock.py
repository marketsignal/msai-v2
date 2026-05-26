"""Unit tests for the Redis-backed ingest mutex per (symbol, year-month).

Concurrent CLI + UI + scheduler invocations can each call ``ingest_symbols``
for overlapping windows. The mutex serializes Databento fetches per
``(symbol, YYYY-MM)`` so only one fetcher runs at a time per window; others
either wait or fail-fast based on ``wait_timeout_seconds``.

These tests use ``fakeredis.aioredis.FakeRedis`` (declared in ``backend/pyproject.toml``
dev extras as ``fakeredis[lua]>=2.20``).
"""

from __future__ import annotations

from datetime import date

import pytest
from fakeredis.aioredis import FakeRedis

from msai.services.smoke.ingest_lock import (
    INGEST_LOCK_KEY_PREFIX,
    IngestLockTimeoutError,
    acquire_ingest_lock,
    release_ingest_lock,
)


@pytest.mark.asyncio
async def test_acquire_ingest_lock_returns_token_when_free() -> None:
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 1)

    # Act
    token = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )

    # Assert
    assert token is not None
    assert isinstance(token, str)
    assert len(token) > 0
    # The key follows the documented (symbol, YYYY-MM) namespacing.
    raw = await redis.get(f"{INGEST_LOCK_KEY_PREFIX}AAPL:2024-12")
    assert raw is not None
    stored = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert stored == token


@pytest.mark.asyncio
async def test_acquire_ingest_lock_blocks_concurrent_same_window() -> None:
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 5)

    # Act — first caller takes the lock
    first = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    assert first is not None

    # Second caller for the same (symbol, YYYY-MM) must fail-fast within the
    # wait_timeout_seconds window because the first holder hasn't released yet.
    with pytest.raises(IngestLockTimeoutError):
        await acquire_ingest_lock(
            redis,
            symbol="AAPL",
            window_start=window,
            ttl_seconds=60,
            wait_timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_acquire_ingest_lock_allows_different_symbol_same_window() -> None:
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 5)

    # Act — different symbols share the window but should each hold their own lock.
    aapl_token = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    spy_token = await acquire_ingest_lock(
        redis,
        symbol="SPY",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )

    # Assert — both succeed with distinct tokens.
    assert aapl_token is not None
    assert spy_token is not None
    assert aapl_token != spy_token


@pytest.mark.asyncio
async def test_release_ingest_lock_frees_for_next_caller() -> None:
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 10)
    first = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    assert first is not None

    # Act — release, then a new caller should acquire fresh.
    await release_ingest_lock(redis, symbol="AAPL", window_start=window, token=first)
    second = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )

    # Assert
    assert second is not None
    assert second != first


@pytest.mark.asyncio
async def test_release_ingest_lock_no_op_when_token_does_not_match() -> None:
    """Releasing with a stale/foreign token must NOT steal the current holder's lock.

    Mirrors the holder-checked release semantics in
    ``services/backtests/auto_heal_lock.py``: a non-owner release is a no-op,
    not a forced delete. The current holder's lock survives unchanged so a
    concurrent third caller still has to wait.
    """
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 15)
    holder_token = await acquire_ingest_lock(
        redis,
        symbol="AAPL",
        window_start=window,
        ttl_seconds=60,
        wait_timeout_seconds=1,
    )
    assert holder_token is not None

    # Act — attempt to release with a foreign token.
    await release_ingest_lock(redis, symbol="AAPL", window_start=window, token="not-the-real-token")

    # Assert — the original lock is still held by the original token.
    raw = await redis.get(f"{INGEST_LOCK_KEY_PREFIX}AAPL:2024-12")
    assert raw is not None
    stored = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert stored == holder_token

    # A third caller still cannot acquire.
    with pytest.raises(IngestLockTimeoutError):
        await acquire_ingest_lock(
            redis,
            symbol="AAPL",
            window_start=window,
            ttl_seconds=60,
            wait_timeout_seconds=1,
        )
