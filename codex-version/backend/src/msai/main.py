from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api import (
    account_router,
    alerts_router,
    auth_router,
    backtests_router,
    graduation_router,
    live_router,
    market_data_router,
    portfolio_router,
    research_router,
    strategies_router,
    strategy_templates_router,
    websocket_router,
)
from msai.core.audit import audit_middleware
from msai.core.auth import _API_KEY_CLAIMS
from msai.core.config import settings
from msai.core.database import async_session_factory, get_db
from msai.core.logging import get_logger, request_context_logging_middleware, setup_logging
from msai.core.queue import close_redis_pool, get_redis_pool
from msai.models import User
from msai.services.ib_probe import ib_probe

setup_logging(settings.environment)
logger = get_logger("main")

_api_key_user_ready = False


async def _ensure_api_key_user() -> bool:
    """Idempotently create the API-key user.

    Returns True if the user exists (or was just created), False if the
    DB is unreachable. Safe to call repeatedly — caches success in a
    module-level flag so hot-path calls skip the DB query entirely.
    """
    global _api_key_user_ready
    if _api_key_user_ready or not settings.msai_api_key:
        return True
    try:
        async with async_session_factory() as session:
            api_user_id = _API_KEY_CLAIMS["sub"]
            result = await session.execute(
                select(User).where(User.entra_id == api_user_id)
            )
            if result.scalar_one_or_none() is None:
                session.add(User(
                    id=api_user_id,
                    entra_id=api_user_id,
                    email=_API_KEY_CLAIMS["preferred_username"],
                    display_name=_API_KEY_CLAIMS.get("name", "API Key User"),
                    role="admin",
                ))
                await session.commit()
            _api_key_user_ready = True
            return True
    except Exception as exc:
        logger.warning("api_key_user_bootstrap_deferred", error=str(exc))
        return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.parquet_root.mkdir(parents=True, exist_ok=True)
    settings.reports_root.mkdir(parents=True, exist_ok=True)
    settings.research_root.mkdir(parents=True, exist_ok=True)

    # Best-effort — retried on /ready if DB is not yet reachable
    await _ensure_api_key_user()

    ib_probe.start()
    logger.info("app_started", environment=settings.environment)
    try:
        yield
    finally:
        ib_probe.stop()
        await close_redis_pool()


app = FastAPI(title="MSAI API", version="0.1.0", lifespan=lifespan)
app.middleware("http")(request_context_logging_middleware)
app.middleware("http")(audit_middleware)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(strategy_templates_router)
api_router.include_router(strategies_router)
api_router.include_router(backtests_router)
api_router.include_router(graduation_router)
api_router.include_router(market_data_router)
api_router.include_router(alerts_router)
api_router.include_router(account_router)
api_router.include_router(live_router)
api_router.include_router(portfolio_router)
api_router.include_router(research_router)
api_router.include_router(websocket_router)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "environment": settings.environment}


@app.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    await db.execute(text("SELECT 1"))
    redis = await get_redis_pool()
    await redis.ping()
    # Retry the API key user bootstrap if lifespan couldn't reach the DB
    await _ensure_api_key_user()
    return {"status": "ready"}
