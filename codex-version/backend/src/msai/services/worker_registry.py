from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from uuid import uuid4

from arq import ArqRedis

from msai.core.config import settings

_ACTIVE_SET_KEY = "msai:workers:active"
_WORKER_KEY_PREFIX = "msai:worker:"


async def register_worker(
    pool: ArqRedis,
    *,
    worker_role: str,
    queue_name: str,
    max_jobs: int,
) -> dict[str, object]:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    payload = {
        "worker_id": worker_id,
        "worker_role": worker_role,
        "queue_name": queue_name,
        "max_jobs": max(1, int(max_jobs)),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await pool.set(_worker_key(worker_id), json.dumps(payload, sort_keys=True), ex=_worker_ttl())
    await pool.sadd(_ACTIVE_SET_KEY, worker_id)
    return payload


async def heartbeat_worker(pool: ArqRedis, worker_id: str) -> bool:
    if not worker_id:
        return False
    key = _worker_key(worker_id)
    payload = await pool.get(key)
    if payload is None:
        return False
    decoded = json.loads(payload)
    decoded["updated_at"] = _now_iso()
    await pool.set(key, json.dumps(decoded, sort_keys=True), ex=_worker_ttl())
    return True


async def deregister_worker(pool: ArqRedis, worker_id: str) -> None:
    if not worker_id:
        return
    await pool.delete(_worker_key(worker_id))
    await pool.srem(_ACTIVE_SET_KEY, worker_id)


async def list_workers(pool: ArqRedis) -> list[dict[str, object]]:
    workers: list[dict[str, object]] = []
    stale_ids: list[str] = []
    raw_ids = await pool.smembers(_ACTIVE_SET_KEY)
    for raw_id in raw_ids:
        worker_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        payload = await pool.get(_worker_key(worker_id))
        if payload is None:
            stale_ids.append(worker_id)
            continue
        decoded = json.loads(payload)
        decoded["max_jobs"] = max(1, int(decoded.get("max_jobs", 1)))
        workers.append(decoded)
    if stale_ids:
        await pool.srem(_ACTIVE_SET_KEY, *stale_ids)
    workers.sort(
        key=lambda worker: (
            str(worker.get("queue_name") or ""),
            str(worker.get("worker_role") or ""),
            str(worker.get("worker_id") or ""),
        )
    )
    return workers


async def summarize_workers(
    pool: ArqRedis,
    *,
    queue_names: list[str] | None = None,
) -> dict[str, object]:
    workers = await list_workers(pool)
    queue_filters = set(queue_names or [])
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for worker in workers:
        queue_name = str(worker.get("queue_name") or "")
        if queue_filters and queue_name not in queue_filters:
            continue
        worker_role = str(worker.get("worker_role") or "")
        key = (queue_name, worker_role)
        entry = grouped.setdefault(
            key,
            {
                "queue_name": queue_name,
                "worker_role": worker_role,
                "active_workers": 0,
                "total_capacity": 0,
                "max_jobs_per_worker": max(1, int(worker.get("max_jobs") or 1)),
                "queued_jobs": 0,
            },
        )
        entry["active_workers"] = int(entry["active_workers"]) + 1
        entry["total_capacity"] = int(entry["total_capacity"]) + max(1, int(worker.get("max_jobs") or 1))
        entry["max_jobs_per_worker"] = max(
            int(entry["max_jobs_per_worker"]),
            max(1, int(worker.get("max_jobs") or 1)),
        )

    queues = sorted(grouped.values(), key=lambda queue: (str(queue["queue_name"]), str(queue["worker_role"])))
    for queue in queues:
        queue["queued_jobs"] = int(await pool.zcard(str(queue["queue_name"])))

    total_capacity = sum(int(queue["total_capacity"]) for queue in queues)
    return {
        "total_active_workers": sum(int(queue["active_workers"]) for queue in queues),
        "total_capacity": total_capacity,
        "workers": workers if not queue_filters else [w for w in workers if str(w.get("queue_name") or "") in queue_filters],
        "queues": queues,
    }


def _worker_key(worker_id: str) -> str:
    return f"{_WORKER_KEY_PREFIX}{worker_id}"


def _worker_ttl() -> int:
    return max(
        int(settings.worker_registry_ttl_seconds),
        int(settings.worker_registry_heartbeat_seconds) * 2,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
