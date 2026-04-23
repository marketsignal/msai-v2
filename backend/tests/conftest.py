"""Shared test fixtures for the MSAI v2 test suite."""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import httpx
import pytest
import structlog

from msai.core.auth import get_current_user
from msai.main import app

# Reconfigure structlog with cache_logger_on_first_use=False so
# structlog.testing.capture_logs() can always intercept, regardless of
# which logger has already been materialized by earlier imports. The
# production setup_logging() in msai.core.logging uses cache=True for
# perf; that freezes the processor chain on first log call and makes
# capture_logs() see an empty buffer under CI's test-discovery order
# (integration/ runs before unit/ alphabetically, so a backtest_job
# log call in an integration test locks the chain before unit tests
# get a chance to intercept). Runs once here in the top-level conftest
# so it takes effect before ANY test (unit, integration, or e2e) runs.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(10),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
    context_class=dict,
)

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


@pytest.fixture(autouse=True)
def _override_auth() -> Generator[None, None, None]:
    """Override get_current_user for all tests so auth-protected endpoints pass.

    Individual test modules can add further overrides (e.g. mock DB) on top
    of this one.  The autouse cleanup restores the overrides dict after each test.
    """
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Integration test fixtures (testcontainers or CI service containers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    """Provide a real PostgreSQL URL — from env var (CI) or testcontainers (local)."""
    existing = os.getenv("DATABASE_URL")
    if existing:
        yield existing
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> Generator[str, None, None]:
    """Provide a real Redis URL — from env var (CI) or testcontainers (local)."""
    existing = os.getenv("REDIS_URL")
    if existing:
        yield existing
        return

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield redis.get_connection_url()
