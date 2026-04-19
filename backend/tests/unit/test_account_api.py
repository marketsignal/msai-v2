"""Unit tests for the account API endpoints."""

from __future__ import annotations

import httpx
import pytest

from msai.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/account/summary
# ---------------------------------------------------------------------------


class TestAccountSummary:
    """Tests for GET /api/v1/account/summary."""

    async def test_account_summary_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/account/summary returns 200 with account data."""
        response = await client.get("/api/v1/account/summary")

        assert response.status_code == 200
        body = response.json()
        assert "net_liquidation" in body
        assert "buying_power" in body
        assert "margin_used" in body
        assert isinstance(body["net_liquidation"], float)

    async def test_account_summary_has_all_fields(self, client: httpx.AsyncClient) -> None:
        """Account summary contains all expected financial fields."""
        response = await client.get("/api/v1/account/summary")

        body = response.json()
        expected_fields = [
            "net_liquidation",
            "buying_power",
            "margin_used",
            "available_funds",
            "unrealized_pnl",
            "realized_pnl",
        ]
        for field in expected_fields:
            assert field in body, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/account/portfolio
# ---------------------------------------------------------------------------


class TestAccountPortfolio:
    """Tests for GET /api/v1/account/portfolio."""

    async def test_account_portfolio_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/account/portfolio returns 200 with list."""
        response = await client.get("/api/v1/account/portfolio")

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/account/health
# ---------------------------------------------------------------------------


class TestAccountHealth:
    """Tests for GET /api/v1/account/health."""

    async def test_account_health_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/account/health returns 200 with status."""
        response = await client.get("/api/v1/account/health")

        assert response.status_code == 200
        body = response.json()
        assert "status" in body
        assert "gateway_connected" in body

    async def test_account_health_reports_unhealthy_by_default(
        self, client: httpx.AsyncClient
    ) -> None:
        """When no IB Gateway is running, health reports unhealthy."""
        response = await client.get("/api/v1/account/health")

        body = response.json()
        # IBProbe starts with is_healthy=False
        assert body["gateway_connected"] is False
