from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api import (
    account_router,
    auth_router,
    backtests_router,
    live_router,
    market_data_router,
    strategies_router,
    websocket_router,
)
from msai.core.audit import audit_middleware
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger, request_context_logging_middleware, setup_logging
from msai.core.queue import close_redis_pool, get_redis_pool
from msai.services.ib_probe import ib_probe

setup_logging(settings.environment)
logger = get_logger("main")

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.parquet_root.mkdir(parents=True, exist_ok=True)
    settings.reports_root.mkdir(parents=True, exist_ok=True)
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
api_router.include_router(strategies_router)
api_router.include_router(backtests_router)
api_router.include_router(market_data_router)
api_router.include_router(account_router)
api_router.include_router(live_router)
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
    return {"status": "ready"}
