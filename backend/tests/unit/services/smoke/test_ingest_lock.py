"""Unit tests for the Redis-backed ingest mutex per (symbol, year-month).

Concurrent CLI + UI + scheduler invocations can each call ``ingest_symbols``
for overlapping windows. The mutex serializes Databento fetches per
``(symbol, YYYY-MM)`` so only one fetcher runs at a time per window; others
either wait or fail-fast based on ``wait_timeout_seconds``.

These tests use ``fakeredis.aioredis.FakeRedis`` (declared in ``backend/pyproject.toml``
dev extras as ``fakeredis[lua]>=2.20``).
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fakeredis.aioredis import FakeRedis

from msai.services.smoke.ingest_lock import (
    INGEST_LOCK_KEY_PREFIX,
    IngestLockTimeoutError,
    acquire_ingest_lock,
    release_ingest_lock,
)

# Pre-bind ``asyncio.gather`` so the PostToolUse ruff formatter does NOT
# strip the ``import asyncio`` above between subagent edits (see
# ``feedback_colocate_imports_with_usage_in_edits.md``). The concurrency
# test below dispatches the two coroutines via ``asyncio.gather`` —
# binding to a module-level alias keeps ``asyncio`` "used" even after a
# formatter pass that runs before the gather() call lands.
_gather = asyncio.gather


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


# ---------------------------------------------------------------------------
# Test-analyzer P0 fix #8 — real-concurrency assertion under asyncio.gather.
#
# The existing tests acquire SEQUENTIALLY (first call returns, then a second
# call is issued); that exercises only the "second caller already sees the
# key" branch. The mutex's CORRECTNESS claim is "two truly simultaneous
# acquires resolve to exactly-one winner" — without an asyncio.gather case,
# a future refactor that broke ``SET NX EX`` atomicity (e.g., by replacing
# the single SET with a GET-then-SET pair) could pass the existing tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_ingest_lock_concurrent_gather_exactly_one_wins() -> None:
    """Two simultaneous ``acquire`` calls on the same (symbol, window): exactly one wins.

    ``wait_timeout_seconds=0`` makes this a fail-fast race — no polling
    window means the loser raises immediately, so we don't have to time
    out the test. The Redis primitive ``SET key value NX EX ttl`` is what
    guarantees the race winner is unique, even when both ``acquire`` calls
    are scheduled into the event loop in the same tick.
    """
    # Arrange
    redis = FakeRedis()
    window = date(2024, 12, 20)

    async def _try_acquire() -> str | None:
        try:
            return await acquire_ingest_lock(
                redis,
                symbol="AAPL",
                window_start=window,
                ttl_seconds=60,
                wait_timeout_seconds=0,
            )
        except IngestLockTimeoutError:
            return None

    # Act — gather both tasks so they're scheduled together on the loop.
    a, b = await _gather(_try_acquire(), _try_acquire())

    # Assert — exactly one returns a token; the other returned None
    # (translated from IngestLockTimeoutError by the local helper).
    results = [r for r in (a, b) if r is not None]
    assert len(results) == 1, f"Expected exactly one winner, got {len(results)}: a={a!r} b={b!r}"
    # The winner's token is the one stored under the key.
    raw = await redis.get(f"{INGEST_LOCK_KEY_PREFIX}AAPL:2024-12")
    assert raw is not None
    stored = raw.decode() if isinstance(raw, bytes) else str(raw)
    assert stored == results[0]
