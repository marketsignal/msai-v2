"""Redis-based compute slot semaphore for concurrent job limiting.

Limits the number of simultaneous research/backtest jobs by using Redis keys
with TTL as leases.  Each running job acquires N slots (default 1).  A
background heartbeat in the worker renews the TTL.  When a job finishes
(explicit release) or crashes (TTL expires), slots are automatically reclaimed.

Key layout::

    msai:compute:active          — Redis SET of active lease_id strings
    msai:compute:lease:{id}      — JSON payload with slot_count, expiring key

The semaphore is **cooperative**: callers must ``acquire`` before doing work,
``renew`` periodically, and ``release`` in a ``finally`` block.  Slots held by
crashed workers are reclaimed when the lease key expires and
``_active_leases`` prunes the stale id from the active set.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from msai.core.config import settings
from msai.core.logging import get_logger

if TYPE_CHECKING:
    from arq.connections import ArqRedis

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis key constants
# ---------------------------------------------------------------------------
_ACTIVE_SET_KEY = "msai:compute:active"
_LEASE_KEY_PREFIX = "msai:compute:lease:"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ComputeSlotUnavailableError(TimeoutError):
    """Raised when compute slots cannot be acquired within the timeout."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def acquire_compute_slots(
    redis: ArqRedis,
    *,
    job_kind: str,
    job_id: str,
    slot_count: int = 1,
    timeout_seconds: int | None = None,
) -> str:
    """Acquire *slot_count* compute slots.  Blocks up to *timeout_seconds*.

    Returns the ``lease_id`` on success.  Raises
    :class:`ComputeSlotUnavailableError` if the timeout elapses before
    enough slots become available.
    """
    limit = settings.compute_slot_limit
    slot_count = max(1, min(slot_count, limit))
    timeout = timeout_seconds if timeout_seconds is not None else settings.compute_slot_wait_seconds
    deadline = datetime.now(UTC).timestamp() + max(1, timeout)

    while True:
        used = await _active_slot_usage(redis)
        if used + slot_count <= limit:
            lease_id = str(uuid4())
            payload: dict[str, Any] = {
                "lease_id": lease_id,
                "job_kind": job_kind,
                "job_id": job_id,
                "slot_count": slot_count,
                "updated_at": _now_iso(),
            }
            await redis.set(
                _lease_key(lease_id),
                json.dumps(payload, sort_keys=True),
                ex=settings.compute_slot_lease_seconds,
            )
            await redis.sadd(_ACTIVE_SET_KEY, lease_id)
            logger.info(
                "compute_slots_acquired",
                lease_id=lease_id,
                job_kind=job_kind,
                job_id=job_id,
                slot_count=slot_count,
                used_after=used + slot_count,
                limit=limit,
            )
            return lease_id

        if datetime.now(UTC).timestamp() >= deadline:
            raise ComputeSlotUnavailableError(
                f"Timed out waiting for {slot_count} compute slot(s) "
                f"for {job_kind}:{job_id} (limit={limit}, used={used})"
            )

        await asyncio.sleep(settings.compute_slot_poll_seconds)


async def renew_compute_slots(redis: ArqRedis, lease_id: str) -> None:
    """Extend the lease TTL.  Call periodically from a heartbeat loop.

    If the lease key has already expired (e.g. worker was too slow to
    heartbeat), this is a no-op — the slots are already reclaimed.
    """
    key = _lease_key(lease_id)
    raw = await redis.get(key)
    if raw is None:
        logger.warning("compute_slots_renew_miss", lease_id=lease_id)
        return

    payload: dict[str, Any] = json.loads(raw)
    payload["updated_at"] = _now_iso()
    await redis.set(
        key,
        json.dumps(payload, sort_keys=True),
        ex=settings.compute_slot_lease_seconds,
    )
    logger.debug("compute_slots_renewed", lease_id=lease_id)


async def release_compute_slots(redis: ArqRedis, lease_id: str) -> None:
    """Release slots immediately.  Call in a ``finally`` block."""
    await redis.delete(_lease_key(lease_id))
    await redis.srem(_ACTIVE_SET_KEY, lease_id)
    logger.info("compute_slots_released", lease_id=lease_id)


async def describe_compute_slots(redis: ArqRedis) -> dict[str, Any]:
    """Return a snapshot of current slot usage.

    Returns a dict with ``total``, ``used``, ``available``, and ``leases``
    (list of active lease payloads).
    """
    leases = await _active_leases(redis)
    used = sum(int(lease.get("slot_count", 1)) for lease in leases)
    total = settings.compute_slot_limit
    return {
        "total": total,
        "used": used,
        "available": max(0, total - used),
        "leases": leases,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _active_slot_usage(redis: ArqRedis) -> int:
    """Sum ``slot_count`` across all live leases, pruning stale entries."""
    leases = await _active_leases(redis)
    return sum(int(lease.get("slot_count", 1)) for lease in leases)


async def _active_leases(redis: ArqRedis) -> list[dict[str, Any]]:
    """Return payloads of all live leases, removing stale ids from the set."""
    member_ids = await redis.smembers(_ACTIVE_SET_KEY)
    leases: list[dict[str, Any]] = []
    stale: list[str] = []

    for raw_id in member_ids:
        lease_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        raw_payload = await redis.get(_lease_key(lease_id))
        if raw_payload is None:
            # Lease key expired (job crashed or TTL elapsed) — mark for cleanup.
            stale.append(lease_id)
            continue
        try:
            payload: dict[str, Any] = json.loads(raw_payload)
            payload["slot_count"] = max(1, int(payload.get("slot_count", 1)))
            leases.append(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            stale.append(lease_id)

    if stale:
        await redis.srem(_ACTIVE_SET_KEY, *stale)
        logger.info("compute_slots_pruned_stale", stale_ids=stale)

    leases.sort(
        key=lambda lse: (
            str(lse.get("job_kind", "")),
            str(lse.get("job_id", "")),
            str(lse.get("lease_id", "")),
        )
    )
    return leases


def _lease_key(lease_id: str) -> str:
    return f"{_LEASE_KEY_PREFIX}{lease_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
