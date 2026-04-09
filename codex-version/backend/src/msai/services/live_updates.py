from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis import Redis

from msai.core.config import settings
from msai.core.queue import get_redis_pool

LIVE_UPDATES_CHANNEL = "live_updates"
_SNAPSHOT_PREFIX = "live_snapshot:"


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _snapshot_key(name: str) -> str:
    return f"{_SNAPSHOT_PREFIX}{name}"


def _sync_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


async def publish_live_update(
    event_type: str,
    data: Any,
    *,
    snapshot: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    message = {
        "type": event_type,
        "data": data,
        "scope": scope,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    pool = await get_redis_pool()
    payload = json.dumps(message, default=_json_default)
    if snapshot is not None:
        await pool.set(_snapshot_key(_scoped_snapshot_name(snapshot, scope)), payload)
    await pool.publish(LIVE_UPDATES_CHANNEL, payload)
    return message


def publish_live_update_sync(
    event_type: str,
    data: Any,
    *,
    snapshot: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    message = {
        "type": event_type,
        "data": data,
        "scope": scope,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    client = _sync_redis()
    try:
        payload = json.dumps(message, default=_json_default)
        if snapshot is not None:
            client.set(_snapshot_key(_scoped_snapshot_name(snapshot, scope)), payload)
        client.publish(LIVE_UPDATES_CHANNEL, payload)
    finally:
        client.close()
    return message


async def publish_live_snapshot(
    name: str,
    data: Any,
    *,
    scope: str | None = None,
) -> dict[str, Any]:
    return await publish_live_update(
        f"{name}.snapshot",
        data,
        snapshot=name,
        scope=scope,
    )


def publish_live_snapshot_sync(
    name: str,
    data: Any,
    *,
    scope: str | None = None,
) -> dict[str, Any]:
    return publish_live_update_sync(
        f"{name}.snapshot",
        data,
        snapshot=name,
        scope=scope,
    )


async def load_live_snapshot(name: str, *, scope: str | None = None) -> dict[str, Any] | None:
    pool = await get_redis_pool()
    raw = await pool.get(_snapshot_key(_scoped_snapshot_name(name, scope)))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def load_live_snapshots(name: str) -> list[dict[str, Any]]:
    pool = await get_redis_pool()
    pattern = _snapshot_key(f"{name}:*")
    snapshots: list[dict[str, Any]] = []
    async for key in pool.scan_iter(match=pattern):
        raw = await pool.get(key)
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        snapshots.append(json.loads(raw))
    snapshots.sort(key=lambda item: str(item.get("generated_at", "")), reverse=True)
    return snapshots


async def clear_live_scope(scope: str, names: tuple[str, ...] | None = None) -> None:
    pool = await get_redis_pool()
    scoped_names = names or ("status", "positions", "orders", "trades", "risk")
    keys = [_snapshot_key(_scoped_snapshot_name(name, scope)) for name in scoped_names]
    if keys:
        await pool.delete(*keys)


def _scoped_snapshot_name(name: str, scope: str | None) -> str:
    if not scope:
        return name
    return f"{name}:{scope}"
