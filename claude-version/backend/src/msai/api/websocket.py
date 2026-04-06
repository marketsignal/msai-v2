"""WebSocket endpoint for real-time live trading updates.

Clients connect to ``/api/v1/live/stream`` and must send a JWT token
as the first text message within 5 seconds.  Once authenticated, the
server sends periodic heartbeats and will eventually stream real-time
trading events via Redis pub/sub.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import WebSocket, WebSocketDisconnect

from msai.core.auth import get_validator
from msai.core.logging import get_logger

log = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 5.0


async def live_stream(websocket: WebSocket) -> None:
    """WebSocket handler for live trading event streaming.

    Protocol:
        1. Client connects.
        2. Server accepts the connection.
        3. Client sends a JWT token as a text message within 5 seconds.
        4. Server validates the token (simplified in Phase 1).
        5. Server streams heartbeats and events until the client disconnects.

    Args:
        websocket: The FastAPI WebSocket connection.
    """
    await websocket.accept()

    # ---------- Authentication ----------
    try:
        token = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=_AUTH_TIMEOUT_SECONDS,
        )
        try:
            validator = get_validator()
            claims = validator.validate_token(token.strip())
            log.info("ws_authenticated", user=claims.get("sub"))
        except Exception:
            await websocket.close(code=4001, reason="Invalid token")
            return
    except asyncio.TimeoutError:
        log.warning("ws_auth_timeout")
        await websocket.close(code=4001, reason="Authentication timed out")
        return
    except WebSocketDisconnect:
        log.info("ws_disconnected_before_auth")
        return

    # ---------- Event streaming ----------
    try:
        while True:
            # TODO: Subscribe to Redis pub/sub for real trading events
            await asyncio.sleep(5)
            await websocket.send_json(
                {
                    "type": "heartbeat",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
    except WebSocketDisconnect:
        log.info("ws_client_disconnected")
