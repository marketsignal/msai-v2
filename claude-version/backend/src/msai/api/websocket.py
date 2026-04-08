"""WebSocket endpoint for real-time live trading updates
(Phase 3 task 3.6 — full rewrite of the heartbeat-only stub).

Connection flow:

1. Client connects to ``/api/v1/live/stream/{deployment_id}``.
2. Server accepts the connection.
3. Client sends a JWT token (or API key) as the first text
   message within 5 seconds. Server validates it.
4. Server looks up the :class:`LiveDeployment` row by
   ``deployment_id`` to get the ``trader_id``,
   ``strategy_id_full``, and ``account_id`` it needs to ask
   :class:`PositionReader` for the initial snapshot.
5. Server sends the snapshot:
   ``{"type": "snapshot", "positions": [...], "account": ...}``.
6. Server subscribes to the per-deployment Redis pub/sub
   channel ``msai:live:events:{deployment_id}`` and forwards
   every received JSON payload verbatim. The
   :class:`ProjectionConsumer` (3.4) is the producer; the
   wire format is :class:`InternalEvent` JSON.
7. Server emits an application-level heartbeat
   ``{"type": "heartbeat", "ts": ...}`` every 30 s if the
   pub/sub channel is idle so clients can detect dead sockets.
8. On disconnect (client close, server shutdown, or any
   exception), the server unsubscribes from the channel and
   cancels the heartbeat task.

Multi-worker correctness: every uvicorn worker subscribes to
the same Redis pub/sub channel, so when the projection consumer
publishes an event, EVERY worker forwards it to its own
connected clients. No in-memory state is shared across
workers — Redis is the single source of truth (Codex v2 P1).

The handler is intentionally tolerant of malformed pub/sub
messages: a single bad message must NOT crash the WebSocket
loop, or the client would lose its real-time view until it
reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import WebSocket, WebSocketDisconnect

from msai.api.live_deps import get_live_redis_binary, get_position_reader
from msai.core.auth import validate_token_or_api_key
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.services.nautilus.projection.fanout import events_channel_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.nautilus.projection.position_reader import PositionReader


log = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 5.0
"""How long the server waits for the first auth message
before closing the socket. Matches the legacy contract."""

_HEARTBEAT_INTERVAL_SECONDS = 30.0
"""Idle heartbeat cadence. Long enough that an active
deployment with regular fills won't see a heartbeat at all,
short enough that a stale TCP socket gets noticed within a
minute."""

_PUBSUB_GET_TIMEOUT_SECONDS = 1.0
"""Pub/sub poll timeout. Short enough that the heartbeat task
can fire on schedule even when the channel is idle."""


async def _authenticate(websocket: WebSocket) -> dict[str, Any] | None:
    """Wait for the first text message, validate it as a JWT
    or API key, and return the claims dict on success. On
    failure (timeout, bad token, client disconnect), close
    the socket with an appropriate code and return ``None``."""
    try:
        token = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=_AUTH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.warning("ws_auth_timeout")
        await websocket.close(code=4001, reason="Authentication timed out")
        return None
    except WebSocketDisconnect:
        log.info("ws_disconnected_before_auth")
        return None

    try:
        claims = validate_token_or_api_key(token)
    except Exception:  # noqa: BLE001 — auth library raises various types
        await websocket.close(code=4001, reason="Invalid token")
        return None

    log.info("ws_authenticated", user=claims.get("sub"))
    return claims


async def _send_initial_snapshot(
    websocket: WebSocket,
    *,
    deployment: LiveDeployment,
    position_reader: PositionReader,
) -> None:
    """Pull the current positions + account from
    :class:`PositionReader` and send a single ``snapshot``
    message to the client. The reader's fast path serves this
    in-memory if the worker has already seen events for this
    deployment; otherwise the cold path rebuilds from Redis."""
    deployment_id = deployment.id
    positions = await position_reader.get_open_positions(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        strategy_id_full=deployment.strategy_id_full,
    )
    account = await position_reader.get_account(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        account_id=deployment.account_id,
    )
    await websocket.send_json(
        {
            "type": "snapshot",
            "deployment_id": str(deployment_id),
            "positions": [p.model_dump(mode="json") for p in positions],
            "account": account.model_dump(mode="json") if account is not None else None,
        }
    )


async def _heartbeat_loop(websocket: WebSocket) -> None:
    """Send an application-level heartbeat at fixed intervals
    so clients can detect dead sockets. Cancellation is the
    normal exit path — the outer handler cancels this task on
    disconnect."""
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send_json(
                {
                    "type": "heartbeat",
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
        except (WebSocketDisconnect, RuntimeError):
            # Socket already closed — exit cleanly so the
            # outer handler doesn't see a noisy traceback.
            return


async def _forward_pubsub_to_websocket(
    websocket: WebSocket,
    pubsub: Any,
    channel: str,
) -> None:
    """Pull messages off the pub/sub subscription and write
    them verbatim to the WebSocket. Returns on
    ``WebSocketDisconnect`` or any unrecoverable pubsub error.

    A single malformed message must NOT kill the loop — log
    and continue, or the client would silently lose its live
    feed until it reconnects."""
    while True:
        try:
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=_PUBSUB_GET_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            log.exception("ws_pubsub_get_message_failed", channel=channel)
            return

        if msg is None:
            # Idle — let the heartbeat loop do its thing
            continue

        data = msg.get("data")
        if data is None:
            continue
        if isinstance(data, bytes):
            data = data.decode("utf-8")

        try:
            # Validate it's well-formed JSON before forwarding —
            # the consumer publishes ``InternalEvent.model_dump_json``
            # so anything that fails json.loads is a poison
            # message and would corrupt the client's view.
            json.loads(data)
        except (json.JSONDecodeError, TypeError):
            log.warning("ws_pubsub_payload_not_json", channel=channel, raw=str(data)[:200])
            continue

        try:
            await websocket.send_text(data)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            log.exception("ws_send_failed", channel=channel)
            return


_API_KEY_SUB = "api-key-user"
"""The ``sub`` claim emitted by the API-key auth path
(``core/auth.py:_API_KEY_CLAIMS``). Codex batch 9 P1: the
WebSocket previously hardcoded the wrong value (``msai-api-key``)
which made the API-key bypass dead code."""


def _is_authorized(deployment: LiveDeployment, claims: dict[str, Any]) -> bool:
    """Verify the authenticated principal is allowed to
    subscribe to ``deployment``.

    Authorization rules (Codex batch 9 P1):

    - The API-key dev account (``sub == "api-key-user"``) has
      access to every deployment — single-tenant local mode.
    - JWT users only see deployments they own (the
      ``LiveDeployment.started_by`` matches the user resolved
      from the JWT's ``sub``).
    - Anything else is denied.

    Returns ``True`` to allow, ``False`` to reject. The caller
    closes the socket with code 4403 on rejection.

    The model column is named ``started_by`` (Phase 1 Task
    1.1b stable identity contract) — Codex batch 9 P1
    iter 2 caught the wrong attribute name in iter 1.
    """
    sub = claims.get("sub")
    if sub == _API_KEY_SUB:
        return True
    deployment_owner = getattr(deployment, "started_by", None)
    if deployment_owner is None:
        # No owner recorded — deny by default rather than fail
        # open. The column is nullable in the model so a
        # legacy row without an owner is treated as
        # unowned and therefore inaccessible.
        return False
    claim_user_id = claims.get("_resolved_user_id")
    return claim_user_id is not None and claim_user_id == deployment_owner


async def live_stream(
    websocket: WebSocket,
    deployment_id: UUID,
) -> None:
    """WebSocket handler for one deployment's live event feed.

    The handler owns its own database session, Redis pub/sub
    subscription, and heartbeat task. All three are released
    in the ``finally`` block — even on a forced disconnect —
    so a misbehaving client can't leak resources.
    """
    await websocket.accept()

    claims = await _authenticate(websocket)
    if claims is None:
        return

    deployment = await _load_deployment(deployment_id)
    if deployment is None:
        await websocket.close(code=4404, reason="Deployment not found")
        return

    # Authorization (Codex batch 9 P1): the API-key dev user
    # has access to everything; JWT users see only deployments
    # they own. Resolve the JWT user against the DB once and
    # stash the resolved id on the claims dict for the
    # synchronous helper to compare.
    if claims.get("sub") != _API_KEY_SUB:
        resolved_user_id = await _resolve_jwt_user_id(claims)
        claims["_resolved_user_id"] = resolved_user_id
    if not _is_authorized(deployment, claims):
        log.warning(
            "ws_authorization_denied",
            deployment_id=str(deployment_id),
            sub=claims.get("sub"),
        )
        await websocket.close(code=4403, reason="Forbidden")
        return

    position_reader = get_position_reader()

    try:
        await _send_initial_snapshot(
            websocket,
            deployment=deployment,
            position_reader=position_reader,
        )
    except Exception:  # noqa: BLE001
        log.exception("ws_snapshot_failed", deployment_id=str(deployment_id))
        await websocket.close(code=1011, reason="Snapshot failed")
        return

    redis = await get_live_redis_binary()
    pubsub = redis.pubsub()
    channel = events_channel_for(deployment_id)

    heartbeat_task: asyncio.Task[None] = asyncio.create_task(_heartbeat_loop(websocket))
    forward_task: asyncio.Task[None] | None = None

    try:
        await pubsub.subscribe(channel)
        log.info("ws_subscribed", channel=channel)
        forward_task = asyncio.create_task(_forward_pubsub_to_websocket(websocket, pubsub, channel))
        # Couple the heartbeat and forward tasks: when EITHER
        # completes (heartbeat exits on send-failure /
        # cancellation; forward exits on disconnect or pubsub
        # error), cancel the other so we don't leak the
        # subscription on an idle channel where the forward
        # loop would block forever (Codex batch 9 P1).
        done, pending = await asyncio.wait(
            {heartbeat_task, forward_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Re-raise any exception from the completed task so the
        # outer except blocks log it appropriately.
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    except WebSocketDisconnect:
        log.info("ws_client_disconnected", deployment_id=str(deployment_id))
    except Exception:  # noqa: BLE001
        log.exception("ws_handler_failed", deployment_id=str(deployment_id))
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat_task
        if forward_task is not None:
            forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forward_task
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await pubsub.close()


async def _resolve_jwt_user_id(claims: dict[str, Any]) -> UUID | None:
    """Look up the ``users.id`` matching the JWT's ``sub``
    claim. Returns ``None`` if the user hasn't been
    provisioned in the local DB yet — the caller treats that
    as "not authorized" because we cannot establish ownership.
    """
    sub = claims.get("sub")
    if not sub:
        return None
    from sqlalchemy import select

    from msai.models.user import User

    session_gen = get_db()
    session: AsyncSession = await anext(session_gen)
    try:
        result = await session.execute(select(User.id).where(User.entra_id == sub))
        return result.scalar_one_or_none()
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await anext(session_gen)


async def _load_deployment(deployment_id: UUID) -> LiveDeployment | None:
    """Open a short-lived session, load the deployment row,
    and close the session before returning. We do NOT keep the
    session open for the lifetime of the WebSocket — that
    would pin a connection from the pool for the entire
    duration of the client connection (which can be hours).
    """
    session_gen = get_db()
    session: AsyncSession = await anext(session_gen)
    try:
        return await session.get(LiveDeployment, deployment_id)
    finally:
        with contextlib.suppress(StopAsyncIteration):
            await anext(session_gen)
