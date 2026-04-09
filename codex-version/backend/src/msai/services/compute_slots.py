from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from arq import ArqRedis

from msai.core.config import settings

_LOCK_KEY = "msai:compute-slots:lock"
_ACTIVE_SET_KEY = "msai:compute-slots:active"
_LEASE_KEY_PREFIX = "msai:compute-slots:lease:"


class ComputeSlotUnavailableError(TimeoutError):
    """Raised when no compute slots can be acquired before the timeout."""


async def acquire_compute_slots(
    pool: ArqRedis,
    *,
    job_kind: str,
    job_id: str,
    requested_slots: int,
) -> dict[str, object]:
    slot_count = max(1, min(int(requested_slots), int(settings.compute_slot_limit)))
    deadline = datetime.now(UTC).timestamp() + max(1, int(settings.compute_slot_wait_seconds))

    while True:
        async with pool.lock(_LOCK_KEY, timeout=30, blocking_timeout=5):
            used_slots = await _active_slot_usage(pool)
            if used_slots + slot_count <= int(settings.compute_slot_limit):
                lease_id = str(uuid4())
                payload = {
                    "lease_id": lease_id,
                    "job_kind": job_kind,
                    "job_id": job_id,
                    "slot_count": slot_count,
                    "updated_at": _now_iso(),
                }
                await pool.set(
                    _lease_key(lease_id),
                    json.dumps(payload, sort_keys=True),
                    ex=int(settings.compute_slot_lease_seconds),
                )
                await pool.sadd(_ACTIVE_SET_KEY, lease_id)
                return payload

        if datetime.now(UTC).timestamp() >= deadline:
            raise ComputeSlotUnavailableError(
                f"Timed out waiting for {slot_count} compute slots for {job_kind}:{job_id}"
            )
        await _sleep_poll()


async def renew_compute_slots(pool: ArqRedis, lease_id: str) -> bool:
    if not lease_id:
        return False
    key = _lease_key(lease_id)
    if not await pool.exists(key):
        return False
    await pool.expire(key, int(settings.compute_slot_lease_seconds))
    payload = await pool.get(key)
    if payload:
        decoded = json.loads(payload)
        decoded["updated_at"] = _now_iso()
        await pool.set(key, json.dumps(decoded, sort_keys=True), ex=int(settings.compute_slot_lease_seconds))
    return True


async def release_compute_slots(pool: ArqRedis, lease_id: str) -> None:
    if not lease_id:
        return
    async with pool.lock(_LOCK_KEY, timeout=30, blocking_timeout=5):
        await pool.delete(_lease_key(lease_id))
        await pool.srem(_ACTIVE_SET_KEY, lease_id)


async def describe_compute_slots(pool: ArqRedis) -> dict[str, object]:
    leases = await _active_leases(pool)
    used_slots = sum(int(lease.get("slot_count") or 1) for lease in leases)
    limit = int(settings.compute_slot_limit)
    return {
        "limit": limit,
        "used": used_slots,
        "available": max(0, limit - used_slots),
        "active_leases": len(leases),
        "leases": leases,
    }


async def _active_slot_usage(pool: ArqRedis) -> int:
    used = 0
    leases = await _active_leases(pool)
    for lease in leases:
        used += max(1, int(lease.get("slot_count", 1)))
    return used


async def _active_leases(pool: ArqRedis) -> list[dict[str, object]]:
    leases: list[dict[str, object]] = []
    active_ids = await pool.smembers(_ACTIVE_SET_KEY)
    stale_ids: list[str] = []
    for raw_id in active_ids:
        lease_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        payload = await pool.get(_lease_key(lease_id))
        if payload is None:
            stale_ids.append(lease_id)
            continue
        decoded = json.loads(payload)
        try:
            decoded["slot_count"] = max(1, int(decoded.get("slot_count", 1)))
            leases.append(decoded)
        except (TypeError, ValueError):
            stale_ids.append(lease_id)
    if stale_ids:
        await pool.srem(_ACTIVE_SET_KEY, *stale_ids)
    leases.sort(
        key=lambda lease: (
            str(lease.get("job_kind") or ""),
            str(lease.get("job_id") or ""),
            str(lease.get("lease_id") or ""),
        )
    )
    return leases


async def _sleep_poll() -> None:
    import asyncio

    await asyncio.sleep(max(0.1, float(settings.compute_slot_poll_seconds)))


def _lease_key(lease_id: str) -> str:
    return f"{_LEASE_KEY_PREFIX}{lease_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
