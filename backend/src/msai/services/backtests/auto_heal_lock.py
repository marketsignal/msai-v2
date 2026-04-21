"""Redis-backed dedupe lock for concurrent auto-heal ingest requests.

Pattern mirrors :mod:`msai.services.live.idempotency` — single atomic
``SET key value NX EX ttl`` acquire; TTL-based auto-release on crashed
holder. The release path is holder-checked so a non-owner never steals
the lock from the rightful owner.

Key normalization: ``auto_heal:sha256(asset_class|sorted(symbols)|start|end)``.
Sorting makes ``[AAPL, MSFT]`` and ``[MSFT, AAPL]`` collide into a single
lock — correct for ingest dedupe because the backing download is
symmetric in symbol order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

    from redis.asyncio import Redis

log = get_logger(__name__)


# Compare-and-swap: if the current value at KEYS[1] equals ARGV[1],
# replace it with ARGV[2] and reset TTL to ARGV[3] seconds. Returns 1
# on swap, 0 otherwise. Wrapped by :meth:`AutoHealLock.compare_and_swap`.
CAS_LOCK_VALUE_LUA = """\
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
else
    return 0
end
"""


def build_lock_key(
    *,
    asset_class: str,
    symbols: list[str],
    start: date,
    end: date,
) -> str:
    """Deterministic lock key for a normalized ingest scope.

    Symbols are sorted before hashing so that callers which submit the
    same underlying set in different orders collide into a single lock.
    """
    canonical = "|".join(
        [
            asset_class,
            ",".join(sorted(symbols)),
            start.isoformat(),
            end.isoformat(),
        ]
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"auto_heal:{digest}"


@dataclass(frozen=True, slots=True)
class AutoHealLock:
    """Thin wrapper over :class:`redis.asyncio.Redis` with safe release semantics."""

    redis: Redis

    async def try_acquire(self, key: str, *, ttl_s: int, holder_id: str) -> bool:
        """Atomically acquire the lock; return ``True`` iff we own it.

        Uses ``SET key value NX EX ttl`` — the canonical Redis single-write
        dedupe primitive. TTL guarantees the lock auto-releases if the
        holder crashes before calling :meth:`release`.
        """
        was_set = await self.redis.set(key, holder_id, nx=True, ex=ttl_s)
        return bool(was_set)

    async def release(self, key: str, *, holder_id: str) -> None:
        """Release only if we still hold it.

        Uses a GET-then-DEL pattern (not a Lua script) because the race
        window is bounded by TTL; a spurious release arriving after TTL
        expiry is functionally identical to natural TTL expiry. If a
        different holder now owns the key we log a warning and no-op —
        stealing a lock you don't own is never the right move.
        """
        current = await self.redis.get(key)
        if current is None:
            return
        current_str = current.decode() if isinstance(current, bytes) else str(current)
        if current_str != holder_id:
            log.warning(
                "auto_heal_lock_release_wrong_holder",
                key=key,
                current=current_str,
                requested=holder_id,
            )
            return
        await self.redis.delete(key)

    async def get_holder(self, key: str) -> str | None:
        """Return the current holder string, or ``None`` if unlocked."""
        current = await self.redis.get(key)
        if current is None:
            return None
        return current.decode() if isinstance(current, bytes) else str(current)

    async def compare_and_swap(
        self,
        key: str,
        *,
        from_holder: str,
        to_holder: str,
        ttl_s: int,
    ) -> bool:
        """Atomically swap the lock value only if it currently matches ``from_holder``.

        Returns True iff the swap succeeded. On False, another caller owns the lock
        and our new value was NOT written (so their state is preserved).

        Used by the auto-heal orchestrator for the placeholder → real-job-id handoff.
        """
        result: Any = await self.redis.eval(  # type: ignore[misc]
            CAS_LOCK_VALUE_LUA,
            1,
            key,
            from_holder,
            to_holder,
            str(ttl_s),
        )
        return int(result) == 1
