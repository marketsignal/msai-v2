"""FastAPI dependency providers for the live trading layer.

The live endpoints (``/api/v1/live/start``, ``/stop``,
``/status/...``) depend on an async Redis client for two distinct
purposes that must NOT share the arq pool the backtest path uses:

1. :class:`IdempotencyStore` — HTTP-layer reservation store with
   binary JSON payloads. Uses ``decode_responses=False``.
2. :class:`LiveCommandBus` — Redis Streams producer for the
   supervisor command bus. Uses ``decode_responses=True`` so
   stream field values come back as strings.

Both share the same Redis instance but instantiate the client
differently. We memoize each client on the FastAPI app state so
the first request builds it lazily and subsequent requests reuse
the same connection.

Phase 3 (task 3.6) adds a third client purpose:

3. WebSocket pub/sub fan-out — the live-stream WebSocket handler
   subscribes to ``msai:live:events:{deployment_id}``. Reuses the
   binary client so JSON payloads stay as bytes (we forward them
   verbatim to the WebSocket; double-decoding would round-trip
   through Python str needlessly).

Plus a process-wide :class:`ProjectionState` + :class:`PositionReader`
the WebSocket snapshot handler reads from on connect.

Tests use ``app.dependency_overrides[...]`` to inject their own
testcontainer-backed Redis clients.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from msai.core.config import settings
from msai.services.live.idempotency import IdempotencyStore
from msai.services.live_command_bus import LiveCommandBus
from msai.services.nautilus.projection.position_reader import PositionReader
from msai.services.nautilus.projection.projection_state import ProjectionState

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis


_binary_redis: AsyncRedis | None = None
_text_redis: AsyncRedis | None = None
_projection_state: ProjectionState | None = None
_position_reader: PositionReader | None = None


async def get_live_redis_binary() -> AsyncRedis:
    """Shared ``redis.asyncio.Redis`` client with
    ``decode_responses=False`` — used by
    :class:`IdempotencyStore`, which stores binary JSON
    payloads."""
    global _binary_redis  # noqa: PLW0603 — lazy singleton
    if _binary_redis is None:
        import redis.asyncio as aioredis

        _binary_redis = aioredis.from_url(settings.redis_url, decode_responses=False)
    return _binary_redis


async def get_live_redis_text() -> AsyncRedis:
    """Shared ``redis.asyncio.Redis`` client with
    ``decode_responses=True`` — used by
    :class:`LiveCommandBus`, which reads stream field values
    as strings via XREAD-style commands."""
    global _text_redis  # noqa: PLW0603 — lazy singleton
    if _text_redis is None:
        import redis.asyncio as aioredis

        _text_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _text_redis


async def get_idempotency_store() -> IdempotencyStore:
    """FastAPI dependency: returns an :class:`IdempotencyStore` bound
    to the shared binary Redis client. Tests override this via
    ``app.dependency_overrides``."""
    redis = await get_live_redis_binary()
    return IdempotencyStore(redis=redis)


async def get_command_bus() -> LiveCommandBus:
    """FastAPI dependency: returns a :class:`LiveCommandBus` bound
    to the shared text-decoded Redis client. Tests override this
    via ``app.dependency_overrides``."""
    redis = await get_live_redis_text()
    return LiveCommandBus(redis=redis)


def get_projection_state() -> ProjectionState:
    """Per-worker singleton :class:`ProjectionState`. Built once
    on first access and shared across the StateApplier task,
    PositionReader, and any future readers (like the WebSocket
    snapshot handler). One instance per uvicorn worker."""
    global _projection_state  # noqa: PLW0603 — lazy singleton
    if _projection_state is None:
        _projection_state = ProjectionState()
    return _projection_state


def get_position_reader() -> PositionReader:
    """Per-worker singleton :class:`PositionReader`. Wraps the
    shared :class:`ProjectionState` (fast path) plus a cold-path
    Cache reader bound to the project's Redis URL via the
    shared ``build_redis_database_config`` helper."""
    global _position_reader  # noqa: PLW0603 — lazy singleton
    if _position_reader is None:
        _position_reader = PositionReader(projection_state=get_projection_state())
    return _position_reader
