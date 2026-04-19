"""Unit tests for health and readiness endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from msai.main import app


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def mock_db_ready() -> None:
    """Patch async_session_factory so /ready works without a real DB."""
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=MagicMock())

    @asynccontextmanager
    async def _session_cm():
        yield mock_session

    with (
        patch("msai.core.database.async_session_factory", return_value=_session_cm()),
        patch("msai.main._ensure_api_key_user", new=AsyncMock(return_value=True)),
    ):
        yield


class TestHealthEndpoint:
    """Tests for ``GET /health``."""

    async def test_health_returns_ok(self, client: httpx.AsyncClient) -> None:
        """GET /health must return 200 with status=healthy."""
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"

    async def test_health_includes_environment(self, client: httpx.AsyncClient) -> None:
        """GET /health must include an 'environment' field."""
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert "environment" in body
        assert isinstance(body["environment"], str)
        assert len(body["environment"]) > 0


class TestReadyEndpoint:
    """Tests for ``GET /ready``."""

    async def test_ready_returns_ok(self, client: httpx.AsyncClient, mock_db_ready: None) -> None:
        """GET /ready must return 200 when DB is reachable."""
        response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
