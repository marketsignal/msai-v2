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

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import Depends, FastAPI, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.live_command_bus import LiveCommandBus
    from msai.services.nautilus.projection.registry import StreamRegistry

from msai.api.account import router as account_router
from msai.api.alerts import router as alerts_router
from msai.api.auth import router as auth_router
from msai.api.backtests import (
    StrategyConfigValidationError,
)
from msai.api.backtests import (
    router as backtests_router,
)
from msai.api.broker_accounts import router as broker_accounts_router
from msai.api.graduation import router as graduation_router
from msai.api.instruments import router as instruments_router
from msai.api.live import router as live_router
from msai.api.live_deps import get_command_bus
from msai.api.market_data import router as market_data_router
from msai.api.portfolio import router as portfolio_router
from msai.api.portfolios import (
    revisions_router as live_portfolio_revisions_router,
)
from msai.api.portfolios import (
    router as live_portfolios_router,
)
from msai.api.research import router as research_router
from msai.api.strategies import router as strategies_router
from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.api.system import router as system_router
from msai.api.websocket import live_stream
from msai.core.auth import _API_KEY_CLAIMS, init_validator
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger, logging_middleware, setup_logging

setup_logging(settings.environment)
log = get_logger(__name__)

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
    except Exception as exc:  # noqa: BLE001
        # iter-3 SF P2: log the actual exception type so a DB schema
        # mismatch or programming error (FK violation on partially-
        # migrated schema, wrong model field, etc.) leaves a forensic
        # trail instead of every API-key request 401-ing with no clue.
        log.warning(
            "api_key_user_bootstrap_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


_projection_tasks: list[asyncio.Task[None]] = []
_projection_stop = asyncio.Event()
_projection_redis_clients: list[Any] = []  # closed on shutdown

# Module-level singleton so /api/v1/live/start can register new deployments
# after boot. Lazy-initialized in _start_projection_tasks.
_stream_registry: StreamRegistry | None = None


def get_stream_registry() -> StreamRegistry:
    """Return the per-worker StreamRegistry singleton.

    Called by the live router when a new deployment is started so the
    projection consumer discovers the new Nautilus message bus stream
    without requiring a FastAPI restart.
    """
    from msai.services.nautilus.projection.registry import StreamRegistry

    global _stream_registry  # noqa: PLW0603
    if _stream_registry is None:
        _stream_registry = StreamRegistry()
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
                (
                    await session.execute(
                        select(LiveDeployment).where(
                            LiveDeployment.status.in_(("running", "ready", "starting", "building"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            for dep in active_deps:
                if dep.message_bus_stream:
                    registry.register(
                        deployment_id=dep.id,
                        deployment_slug=dep.deployment_slug,
                        stream_name=dep.message_bus_stream,
                    )
    except Exception as exc:  # noqa: BLE001
        # iter-3 SF P2: a real DB error here means projection consumer
        # starts empty and silently misses every in-flight deployment's
        # events until manual restart. Log so the failure is forensically
        # visible. (DB-not-ready during boot is also legitimate; the log
        # line is "the consumer started without bootstrap" either way.)
        log.warning(
            "projection_registry_bootstrap_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )

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
        with suppress(asyncio.CancelledError):
            await task
    _projection_tasks.clear()

    # Close Redis clients to avoid connection leaks on shutdown
    for client in _projection_redis_clients:
        with suppress(Exception):
            await client.aclose()
    _projection_redis_clients.clear()


async def _has_active_live_deployments() -> bool:
    """True if any live deployment is in an active lifecycle state.

    Uses the broader ``ACTIVE_DEPLOYMENT_STATUSES`` set (running/ready/starting/
    building/**stopping**), which intentionally differs from the projection-
    registry bootstrap query above (that one omits ``stopping``). A ``stopping``
    deployment is mid-teardown but still holds IB positions (nautilus gotcha
    #13), so for the boot KV-reachability probe it MUST count as active — the
    probe should fail-closed while any real-money node is still winding down,
    not just while it is fully running. Shares the same set as the archive guard.
    """
    from msai.core.database import async_session_factory
    from msai.models.live_deployment import LiveDeployment
    from msai.services.live.broker_account_service import ACTIVE_DEPLOYMENT_STATUSES

    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(LiveDeployment)
                .where(LiveDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES))
            )
        ).scalar_one()
    return count > 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle."""
    import os

    from msai.api.account import start_ib_probe_task, stop_ib_probe_task
    from msai.core.soft_delete import register_soft_delete_listeners
    from msai.services.ib_account_snapshot import get_snapshot
    from msai.services.live.gateway_router import GatewayRouter

    # Soft-delete listener — default-filters ``deleted_at IS NULL`` from
    # select(Strategy); opt-out via ``execution_options(include_deleted=True)``.
    register_soft_delete_listeners()

    # PR 1 T5 + council 2026-05-29 obj #13: fail-closed on misconfigured
    # GATEWAY_CONFIG (duplicate ib_login_key). GatewayRouter.__init__
    # raises ValueError, which propagates and causes the ASGI process to
    # exit non-zero — operators see the duplicate key in their logs
    # within 10s of boot rather than at first live deployment.
    app.state.gateway_router = GatewayRouter(os.environ.get("GATEWAY_CONFIG"))

    # Broker-account credentials store (multi-account fleet). Azure Key Vault in
    # prod (via the VM managed identity), file-backed in dev. The prod factory
    # fails LOUD if AZURE_KEYVAULT_URI is absent — docker-compose.prod.yml +
    # msai-render-env.service supply it (else the backend crashloops by design).
    from msai.services.live.broker_credentials_store import get_broker_credentials_store

    app.state.broker_credentials_store = get_broker_credentials_store(
        environment=settings.environment,
        data_root=settings.data_root,
        kv_uri=settings.azure_keyvault_uri,
        # Dedicated MI client id — NEVER the JWT AZURE_CLIENT_ID (Codex iter-3 P1).
        mi_client_id=settings.azure_kv_mi_client_id,
    )
    # Boot KV reachability probe (council blocking #6): fail-closed if KV is
    # unreachable AND any live deployment is in an active lifecycle state. The
    # active set mirrors the projection bootstrap query above (NOT just
    # "running" — a starting/ready deployment is in-flight and counts).
    if settings.environment == "production" and not app.state.broker_credentials_store.ping():
        if await _has_active_live_deployments():
            raise RuntimeError(
                "Broker credentials store unreachable at boot with active deployments"
            )
        # error (not warning) so this is alertable: the store is unreachable,
        # we just don't hard-fail because no live deployment is in flight.
        log.error("broker_credentials_store_unreachable_at_boot_no_active_deploys")

    await _ensure_api_key_user()  # best-effort, retried on /ready
    await _start_projection_tasks()
    await start_ib_probe_task()
    # IBAccountSnapshot singleton — one long-lived IB connection,
    # 30 s background refresh. ``start()`` is synchronous so startup
    # does NOT block when IB Gateway is unreachable.
    get_snapshot().start()
    yield
    await get_snapshot().stop()
    await stop_ib_probe_task()
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
# Exception handlers
# ---------------------------------------------------------------------------

# Field names whose rejected value must never be echoed back in a 422 body.
# Pydantic v2 includes the raw ``input`` in every validation error — even for
# ``SecretStr`` fields (SecretStr masks repr/serialization, NOT the error
# input). Two leak shapes exist and BOTH must be masked:
#
#   1. Field-level scalar input — e.g. a too-long ``tws_password`` produces an
#      error with ``loc=("body", "tws_password")`` and ``input`` = the rejected
#      cleartext scalar.
#   2. Model-level dict input — a cross-field ``model_validator(mode="after")``
#      (e.g. the broker-account prefix-vs-mode guard) reports
#      ``loc=("body",)`` with ``input`` = the ENTIRE request body dict, which
#      includes the cleartext ``tws_password`` / ``tws_userid``.
#
# This handler masks both: it redacts any sensitive key found inside a dict
# ``input`` AND masks a scalar ``input`` when ``loc`` names a sensitive field.
# Everything else is passed through unchanged, preserving FastAPI's default
# ``{"detail": [...]}`` shape.
_SENSITIVE_VALIDATION_FIELDS: frozenset[str] = frozenset({"tws_password", "tws_userid"})

# Credential-looking key tokens. A key is treated as sensitive (its echoed value
# redacted) if its NORMALIZED name (lowercased, non-alphanumerics stripped) CONTAINS
# any of these — so EVERY separator/casing alias a client might send as an extra key
# (``twsPassword``, ``password``, ``twsUserid``, ``tws_user_id``, ``twsUsername``,
# ``pass_word``, ``tws-userid``) is masked, not just the exact field names. Normalizing
# closes the whole alias-spelling class at once (Codex iter-10/11/12 P2). The ``user``
# token (not the narrower ``userid``) covers every user-identifier spelling a client
# might send for the TWS login — ``userid`` / ``user_id`` / ``username`` / ``tws_user``
# all normalize to a name CONTAINING ``user`` (Codex final2 P2: a ``twsUsername`` alias
# was slipping past ``userid``). Tokens chosen to match credential fields without
# catching benign body keys: ``ib_account_id`` → ``ibaccountid``, ``ib_login_key`` →
# ``ibloginkey``, ``trading_mode`` → ``tradingmode``, ``gateway_slot`` → ``gatewayslot``,
# ``created_by`` → ``createdby``, ``label`` — none contain a token.
_SENSITIVE_KEY_TOKENS: tuple[str, ...] = ("password", "passwd", "pwd", "secret", "user")

# Sentinel for a redacted value in a 422 ``input`` echo.
_REDACTED = "***"


def _is_sensitive_key(name: object) -> bool:
    """True if ``name`` looks like a credential key (exact field OR any alias/variant).

    Normalizes the name (lowercase + drop non-alphanumerics) before the token check
    so underscore/hyphen/camelCase variants of a credential token can't slip a value
    past the 422 redactor.
    """
    if not isinstance(name, str):
        return False
    if name in _SENSITIVE_VALIDATION_FIELDS:
        return True
    normalized = "".join(ch for ch in name.lower() if ch.isalnum())
    return any(tok in normalized for tok in _SENSITIVE_KEY_TOKENS)


def _redact_validation_input(loc: tuple[Any, ...], value: Any) -> Any:
    """Return a safe ``input`` echo for a single validation error item.

    Handles both leak shapes (see ``_SENSITIVE_VALIDATION_FIELDS`` docstring):

    * If ``value`` is a dict (model-level error), replace every sensitive key's
      value with ``"***"`` and RECURSE into the remaining values so a sensitive
      key nested at any depth (inside a sub-dict or a list of dicts) is masked
      too. Returns a copy; the original error structure is left intact.
    * If ``value`` is a list, recurse element-wise (carrying ``loc`` so a
      sensitive scalar inside the list is still masked by the ``loc`` branch).
    * Otherwise, if ``loc`` names a sensitive field, mask the scalar value.
    * Otherwise, return ``value`` unchanged.

    The recursion is defensive: today's request schemas are flat, but a future
    nested/list credential schema must not leak ``tws_password`` / ``tws_userid``
    buried below the top level (iter-5 P3-a).
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive_key(k) else _redact_validation_input(loc, v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_validation_input(loc, item) for item in value]
    if any(_is_sensitive_key(part) for part in loc):
        return _REDACTED
    return value


@app.exception_handler(RequestValidationError)
async def _masked_validation_handler(
    request: Request,  # noqa: ARG001 — FastAPI handler signature
    exc: RequestValidationError,
) -> JSONResponse:
    """Mask the echoed ``input`` for sensitive credential material in 422s.

    Mirrors FastAPI's default ``RequestValidationError`` rendering
    (``{"detail": jsonable_encoder(errors)}``, status 422) but redacts any
    sensitive credential value from each error's ``input`` — covering both the
    field-level scalar shape and the model-level body-dict shape. Non-sensitive
    errors are passed through unchanged.
    """
    masked: list[dict[str, Any]] = []
    for err in exc.errors():
        item = dict(err)
        if "input" in item:
            item["input"] = _redact_validation_input(tuple(item.get("loc", ())), item["input"])
        masked.append(item)
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": masked}))


@app.exception_handler(StrategyConfigValidationError)
async def _strategy_config_validation_handler(
    request: Request,  # noqa: ARG001 — FastAPI handler signature
    exc: StrategyConfigValidationError,
) -> JSONResponse:
    """Render :class:`StrategyConfigValidationError` as the api-design.md
    envelope (top-level ``{"error": {code, message, details}}``).

    Raising ``HTTPException(detail={"error": ...})`` would produce
    ``{"detail": {"error": ...}}`` because FastAPI wraps ``detail`` as
    a top-level key. This handler sidesteps that by returning a
    ``JSONResponse`` directly. Frontend ``extract422Envelope`` accepts
    both shapes so older ``{"detail": ...}`` responses still parse.
    """
    return JSONResponse(status_code=422, content=exc.envelope())


# ---------------------------------------------------------------------------
# API Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(strategies_router)
app.include_router(backtests_router)
app.include_router(broker_accounts_router)
app.include_router(market_data_router)
app.include_router(live_router)
app.include_router(account_router)
app.include_router(symbol_onboarding_router)
app.include_router(research_router)
app.include_router(graduation_router)
app.include_router(portfolio_router)
app.include_router(live_portfolios_router)
app.include_router(live_portfolio_revisions_router)
app.include_router(alerts_router)
app.include_router(instruments_router)
app.include_router(system_router)


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
async def metrics(
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> Response:
    """Prometheus scrape endpoint. Exposes every counter and
    gauge registered in :func:`get_registry`. The endpoint is
    intentionally unauthenticated — operators expose it on a
    private network or behind a reverse proxy, matching the
    standard Prometheus deployment model.

    PR 1b T7: before rendering, hydrate the Redis-backed data-feed
    health gauges (``msai_data_feed_age_seconds`` /
    ``msai_data_feed_stale`` / ``msai_databento_dataset_alive`` /
    ``msai_ib_exec_pacing_errors``) from the SAME manifest-first
    reader the ``/api/v1/live/data-health`` route uses, so a bare
    scrape exposes the current feed health. The DB session + command
    bus come through the same overridable dependencies the live
    routes use. Hydration degrades gracefully — if Redis or the DB
    is down it renders whatever is already registered and NEVER 500s
    the scrape."""
    from msai.services.observability import get_registry

    with suppress(Exception):
        from msai.services.observability.data_health import hydrate_data_health_metrics

        await hydrate_data_health_metrics(db, bus._redis)  # noqa: SLF001

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
