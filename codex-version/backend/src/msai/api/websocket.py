from __future__ import annotations

import asyncio
import json

import jwt
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from msai.core.auth import get_token_validator
from msai.core.queue import get_redis_pool

router = APIRouter(tags=["live-stream"])


@router.websocket("/live/stream")
async def live_stream(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
        get_token_validator().validate_token(token)
    except (TimeoutError, jwt.InvalidTokenError, jwt.PyJWTError):
        await websocket.close(code=4001, reason="Authentication failed or timed out")
        return

    redis = await get_redis_pool()
    pubsub = redis.pubsub()
    await pubsub.subscribe("live_updates")

    try:
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
        await pubsub.unsubscribe("live_updates")
        await pubsub.close()
