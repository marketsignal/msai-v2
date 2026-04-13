"""FastAPI application entrypoint for MSAI v2.

Creates and configures the FastAPI application with:
- Structured logging via structlog
- CORS middleware for the frontend (localhost:3000)
- Request-scoped logging middleware (request_id injection)
- Health check and readiness probe endpoints
- API routers for auth, strategies, backtests, live trading, and account
- WebSocket endpoint for real-time live trading updates
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from msai.api.account import router as account_router
from msai.api.asset_universe import router as universe_router
from msai.api.auth import router as auth_router
from msai.api.backtests import router as backtests_router
from msai.api.graduation import router as graduation_router
from msai.api.live import router as live_router
from msai.api.portfolio import router as portfolio_router
from msai.api.market_data import router as market_data_router
from msai.api.research import router as research_router
from msai.api.strategies import router as strategies_router
from msai.api.websocket import live_stream
from msai.core.auth import _API_KEY_CLAIMS, init_validator
from msai.core.config import settings
from msai.core.logging import logging_middleware, setup_logging

setup_logging(settings.environment)

# Initialize Entra ID JWT validator at startup (required for auth endpoints)
if settings.azure_tenant_id and settings.azure_client_id:
    init_validator(settings.azure_tenant_id, settings.azure_client_id)


_api_key_user_ready: bool = False


async def _ensure_api_key_user() -> bool:
    """Idempotently create the API-key user. Returns True on success/no-op."""
    global _api_key_user_ready  # noqa: PLW0603
    if _api_key_user_ready or not settings.msai_api_key:
        return True
    try:
        from msai.core.database import async_session_factory
        from msai.models.user import User

        async with async_session_factory() as session:
            api_user_id = _API_KEY_CLAIMS["sub"]
            result = await session.execute(select(User).where(User.entra_id == api_user_id))
            if result.scalar_one_or_none() is None:
                session.add(
                    User(
                        entra_id=api_user_id,
                        email=_API_KEY_CLAIMS["preferred_username"],
                        display_name=_API_KEY_CLAIMS.get("name", "API Key User"),
                        role="admin",
                    )
                )
                await session.commit()
            _api_key_user_ready = True
            return True
    except Exception:
        return False  # DB may not be ready yet (migrations pending)


import asyncio

_projection_tasks: list[asyncio.Task[None]] = []
_projection_stop = asyncio.Event()
_projection_redis_clients: list[Any] = []  # closed on shutdown

# Module-level singleton so /api/v1/live/start can register new deployments
# after boot. Lazy-initialized in _start_projection_tasks.
_stream_registry: StreamRegistry | None = None  # type: ignore[name-defined]


def get_stream_registry() -> StreamRegistry:  # type: ignore[name-defined]
    """Return the per-worker StreamRegistry singleton.

    Called by the live router when a new deployment is started so the
    projection consumer discovers the new Nautilus message bus stream
    without requiring a FastAPI restart.
    """
    from msai.services.nautilus.projection.registry import StreamRegistry as _SR

    global _stream_registry  # noqa: PLW0603
    if _stream_registry is None:
        _stream_registry = _SR()
    return _stream_registry


async def _start_projection_tasks() -> None:
    """Start StateApplier + ProjectionConsumer as background tasks.

    - StateApplier subscribes to ``msai:live:state:*`` pub/sub and
      feeds every event into the per-worker ProjectionState.
    - ProjectionConsumer reads Nautilus message bus streams via
      consumer groups and publishes translated events to the dual
      pub/sub channels (state + events).

    Both run until ``_projection_stop`` is set.
    """
    from redis.asyncio import Redis as AsyncRedis

    from msai.api.live_deps import get_projection_state
    from msai.services.nautilus.projection.consumer import ProjectionConsumer
    from msai.services.nautilus.projection.fanout import DualPublisher
    from msai.services.nautilus.projection.registry import StreamRegistry
    from msai.services.nautilus.projection.state_applier import StateApplier

    state = get_projection_state()
    _projection_stop.clear()

    # StateApplier needs text-mode Redis (pub/sub payloads are JSON strings)
    redis_text = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
    _projection_redis_clients.append(redis_text)
    applier = StateApplier(redis=redis_text, projection_state=state)
    _projection_tasks.append(asyncio.create_task(applier.run(_projection_stop)))

    # ProjectionConsumer needs binary-mode Redis (Nautilus streams carry msgpack bytes)
    redis_binary = AsyncRedis.from_url(settings.redis_url, decode_responses=False)
    _projection_redis_clients.append(redis_binary)
    registry = get_stream_registry()

    # Populate registry with active deployments from DB so the consumer
    # knows which Nautilus message bus streams to read on startup.
    try:
        from msai.core.database import async_session_factory
        from msai.models.live_deployment import LiveDeployment

        async with async_session_factory() as session:
            active_deps = (
                await session.execute(
                    select(LiveDeployment).where(
                        LiveDeployment.status.in_(("running", "ready", "starting", "building"))
                    )
                )
            ).scalars().all()
            for dep in active_deps:
                if dep.message_bus_stream:
                    registry.register(
                        deployment_id=dep.id,
                        deployment_slug=dep.deployment_slug,
                        stream_name=dep.message_bus_stream,
                    )
    except Exception:  # noqa: BLE001
        # DB may not be ready; consumer will start with empty registry
        # and pick up streams as deployments are started via /api/v1/live/start
        pass

    publisher = DualPublisher(redis=redis_text)  # publishes JSON strings
    consumer = ProjectionConsumer(
        redis=redis_binary,
        registry=registry,
        publisher=publisher,
    )
    _projection_tasks.append(asyncio.create_task(consumer.run(_projection_stop)))


async def _stop_projection_tasks() -> None:
    """Signal projection tasks to stop, await them, close Redis clients."""
    _projection_stop.set()
    for task in _projection_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _projection_tasks.clear()

    # Close Redis clients to avoid connection leaks on shutdown
    for client in _projection_redis_clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _projection_redis_clients.clear()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    await _ensure_api_key_user()  # best-effort, retried on /ready
    await _start_projection_tasks()
    yield
    await _stop_projection_tasks()


app: FastAPI = FastAPI(
    title="MSAI v2",
    description="Personal Hedge Fund Platform",
    lifespan=lifespan,
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(logging_middleware)

# ---------------------------------------------------------------------------
# API Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(strategies_router)
app.include_router(backtests_router)
app.include_router(market_data_router)
app.include_router(live_router)
app.include_router(account_router)
app.include_router(universe_router)
app.include_router(research_router)
app.include_router(graduation_router)
app.include_router(portfolio_router)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/api/v1/live/stream/{deployment_id}")
async def ws_live_stream(websocket: WebSocket, deployment_id: UUID) -> None:
    """WebSocket endpoint for real-time live trading updates
    for one deployment. The handler subscribes to the
    per-deployment Redis pub/sub channel and forwards every
    event to the connected client. See ``api/websocket.py``
    for the full protocol."""
    await live_stream(websocket, deployment_id)


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint (Phase 4 task 4.6)
# ---------------------------------------------------------------------------
@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint. Exposes every counter and
    gauge registered in :func:`get_registry`. The endpoint is
    intentionally unauthenticated — operators expose it on a
    private network or behind a reverse proxy, matching the
    standard Prometheus deployment model."""
    from msai.services.observability import get_registry

    body = get_registry().render()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Health & readiness endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe -- confirms the process is running."""
    return {"status": "healthy", "environment": settings.environment}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe -- confirms PostgreSQL is reachable.

    Also retries the API-key user bootstrap if it deferred at startup.
    """
    from sqlalchemy import text

    from msai.core.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            content={"status": "not_ready", "error": str(exc)},
            status_code=503,
        )

    await _ensure_api_key_user()
    return JSONResponse(content={"status": "ready"}, status_code=200)
