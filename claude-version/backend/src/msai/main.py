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

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from msai.api.account import router as account_router
from msai.api.auth import router as auth_router
from msai.api.backtests import router as backtests_router
from msai.api.live import router as live_router
from msai.api.market_data import router as market_data_router
from msai.api.strategies import router as strategies_router
from msai.api.websocket import live_stream
from msai.core.auth import _API_KEY_CLAIMS, init_validator
from msai.core.config import settings
from msai.core.logging import logging_middleware, setup_logging

setup_logging(settings.environment)

# Initialize Entra ID JWT validator at startup (required for auth endpoints)
if settings.azure_tenant_id and settings.azure_client_id:
    init_validator(settings.azure_tenant_id, settings.azure_client_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle — ensure API key user exists in DB."""
    if settings.msai_api_key:
        try:
            from msai.core.database import async_session_factory
            from msai.models.user import User

            async with async_session_factory() as session:
                api_user_id = _API_KEY_CLAIMS["sub"]
                result = await session.execute(
                    select(User).where(User.entra_id == api_user_id)
                )
                if result.scalar_one_or_none() is None:
                    session.add(User(
                        entra_id=api_user_id,
                        email=_API_KEY_CLAIMS["preferred_username"],
                        display_name=_API_KEY_CLAIMS.get("name", "API Key User"),
                        role="admin",
                    ))
                    await session.commit()
        except Exception:
            pass  # DB may not be ready yet (migrations pending)
    yield


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


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/api/v1/live/stream")
async def ws_live_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time live trading updates."""
    await live_stream(websocket)


# ---------------------------------------------------------------------------
# Health & readiness endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe -- confirms the process is running."""
    return {"status": "healthy", "environment": settings.environment}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe -- confirms external dependencies are reachable.

    TODO: Replace the placeholder with real checks once the database session
    and Redis pool are wired up:
      - PostgreSQL: ``SELECT 1`` via SQLAlchemy async session
      - Redis: ``ping`` via arq connection pool
    """
    # TODO: Check PostgreSQL connectivity (SELECT 1 via async session)
    # TODO: Check Redis connectivity (ping via arq pool)
    return JSONResponse(content={"status": "ready"}, status_code=200)
