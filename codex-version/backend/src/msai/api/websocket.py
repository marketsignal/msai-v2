from __future__ import annotations

import asyncio
import json

import jwt
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from msai.core.auth import validate_token_or_api_key
from msai.core.queue import get_redis_pool
from msai.services.live_updates import LIVE_UPDATES_CHANNEL, load_live_snapshot, load_live_snapshots

router = APIRouter(tags=["live-stream"])


@router.websocket("/live/stream")
async def live_stream(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
        validate_token_or_api_key(token)
    except (TimeoutError, jwt.InvalidTokenError, jwt.PyJWTError, Exception):
        await websocket.close(code=4001, reason="Authentication failed or timed out")
        return

    redis = await get_redis_pool()
    pubsub = redis.pubsub()
    await pubsub.subscribe(LIVE_UPDATES_CHANNEL)

    try:
        for snapshot_name in ("risk", "status", "positions", "orders", "trades"):
            snapshot = await load_live_snapshot(snapshot_name)
            if snapshot is not None:
                await websocket.send_json(snapshot)
            for scoped_snapshot in await load_live_snapshots(snapshot_name):
                await websocket.send_json(scoped_snapshot)
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message.get("data"):
                payload = message["data"]
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                try:
                    await websocket.send_json(json.loads(payload))
                except json.JSONDecodeError:
                    await websocket.send_json({"message": payload})
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return
    finally:
        await pubsub.unsubscribe(LIVE_UPDATES_CHANNEL)
        await pubsub.aclose()
