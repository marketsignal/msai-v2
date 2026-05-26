"""Redis-backed ingest mutex per ``(symbol, year-month)``.

Concurrent smoke invocations (CLI + UI + scheduler) can each call
``ingest_symbols`` for overlapping windows. The mutex serializes Databento
fetches per ``(symbol, YYYY-MM)`` window so only one fetcher runs at a time
for a given symbol-month; others poll for the slot until
``wait_timeout_seconds`` elapses.

Pattern mirrors :mod:`msai.services.backtests.auto_heal_lock`:

* Atomic acquire via ``SET key value NX EX ttl`` — the canonical Redis
  single-write dedupe primitive.
* TTL guarantees auto-release if a holder crashes before calling release.
* Release is holder-checked (GET then DEL) so a stale/foreign token never
  steals the lock from the rightful owner — spurious releases arriving
  after TTL expiry are functionally identical to natural TTL expiry.

Key shape: ``smoke:ingest:{symbol}:{YYYY-MM}`` where ``YYYY-MM`` is derived
from ``window_start``. Different symbols in the same window get independent
locks; the same symbol across different months also gets independent locks.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

    from redis.asyncio import Redis

log = get_logger(__name__)

INGEST_LOCK_KEY_PREFIX = "smoke:ingest:"
"""Namespace prefix for ingest-mutex keys. Public so tests can assert on it."""

_POLL_INTERVAL_SECONDS = 0.5
"""Sleep between acquire retries while waiting for an existing holder to release."""


class IngestLockTimeoutError(RuntimeError):
    """Raised when ``acquire_ingest_lock`` cannot get the lock within ``wait_timeout_seconds``.

    Surfaces to the smoke runner so it can fail-fast rather than block
    indefinitely on a stuck holder (TTL handles eventual recovery, but the
    caller decides how long it's willing to wait).
    """


def _build_lock_key(*, symbol: str, window_start: date) -> str:
    """Deterministic key per ``(symbol, YYYY-MM)``.

    Uses the ``window_start`` month — same month across different
    ``window_start`` days collide into one lock by design (Databento fetches
    full monthly Parquet files, so concurrent overlapping requests for the
    same month would duplicate the same download).
    """
    return f"{INGEST_LOCK_KEY_PREFIX}{symbol}:{window_start.strftime('%Y-%m')}"


async def acquire_ingest_lock(
    redis: Redis,
    *,
    symbol: str,
    window_start: date,
    ttl_seconds: int,
    wait_timeout_seconds: int,
) -> str:
    """Acquire the ingest mutex for ``(symbol, YYYY-MM(window_start))``.

    Returns a holder token that must be passed back to
    :func:`release_ingest_lock`. Polls every :data:`_POLL_INTERVAL_SECONDS`
    until either:

    * the lock becomes free and we win it (returns the token), or
    * ``wait_timeout_seconds`` elapses (raises :class:`IngestLockTimeout`).

    Args:
        redis: An ``redis.asyncio.Redis``-compatible client. ``arq.ArqRedis``
            and ``fakeredis.aioredis.FakeRedis`` both satisfy this interface.
        symbol: The asset symbol (e.g. ``"AAPL"``).
        window_start: First date of the requested ingest window. Only the
            month is consulted for keying.
        ttl_seconds: Lock TTL. Holder must complete + release within this
            window or the lock auto-releases. Sized at the smoke caller
            site to comfortably exceed the worst-case Databento download.
        wait_timeout_seconds: Maximum time to wait for an existing holder
            to release. ``0`` means try once and fail-fast on contention.

    Raises:
        IngestLockTimeout: when the lock could not be acquired in time.
    """
    key = _build_lock_key(symbol=symbol, window_start=window_start)
    token = secrets.token_hex(16)

    # First attempt is immediate (no sleep before the first SET-NX).
    deadline = asyncio.get_event_loop().time() + max(wait_timeout_seconds, 0)
    while True:
        was_set = await redis.set(key, token, nx=True, ex=ttl_seconds)
        if was_set:
            return token

        if asyncio.get_event_loop().time() >= deadline:
            raise IngestLockTimeoutError(
                f"Could not acquire ingest lock for {symbol} "
                f"{window_start.strftime('%Y-%m')} within {wait_timeout_seconds}s"
            )

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def release_ingest_lock(
    redis: Redis,
    *,
    symbol: str,
    window_start: date,
    token: str,
) -> None:
    """Release the lock only if ``token`` matches the current holder.

    Uses a GET-then-DEL pattern (not a Lua script) because the race window
    is bounded by TTL; a spurious release arriving after TTL expiry is
    functionally identical to natural TTL expiry. If a different holder
    now owns the key we log a warning and no-op — stealing a lock you
    don't own is never the right move.
    """
    key = _build_lock_key(symbol=symbol, window_start=window_start)
    current = await redis.get(key)
    if current is None:
        return
    current_str = current.decode() if isinstance(current, bytes) else str(current)
    if current_str != token:
        log.warning(
            "smoke_ingest_lock_release_wrong_holder",
            key=key,
            current=current_str,
            requested=token,
        )
        return
    await redis.delete(key)
