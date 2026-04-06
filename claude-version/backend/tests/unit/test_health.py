"""Unit tests for health and readiness endpoints."""

from __future__ import annotations

import httpx
import pytest

from msai.main import app


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


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

    async def test_ready_returns_ok(self, client: httpx.AsyncClient) -> None:
        """GET /ready must return 200 with status=ready (placeholder)."""
        response = await client.get("/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
