"""Unit tests for the account API endpoints.

Iter-3 SF P1 contract change: ``/summary`` and ``/portfolio`` now raise 503
when the snapshot has NEVER successfully refreshed. The old behaviour of
returning a zero-summary on cold start lied to the dashboard ($0.00
indistinguishable from a truly-empty account). These tests exercise both
sides of that contract: the cold-start 503 path AND the cached 200 path
(via dependency override).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from msai.api.account import _get_snapshot_dep
from msai.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class _StubSnapshot:
    """Minimal stand-in for IBAccountSnapshot used by dependency-override.

    Carries a non-None ``last_refresh_success_at`` so the 503 cold-start
    guard short-circuits and the cached 200 path is exercised.
    """

    def __init__(
        self,
        summary: dict[str, float] | None = None,
        portfolio: list[dict[str, object]] | None = None,
    ) -> None:
        self.last_refresh_success_at: datetime | None = datetime.now(UTC)
        self._summary = summary or {
            "net_liquidation": 100000.0,
            "buying_power": 200000.0,
            "margin_used": 0.0,
            "available_funds": 100000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
        }
        self._portfolio = portfolio or []

    def get_summary(self) -> dict[str, float]:
        return dict(self._summary)

    def get_portfolio(self) -> list[dict[str, object]]:
        return list(self._portfolio)


@pytest.fixture
def seeded_snapshot_app() -> None:
    """Override the snapshot dep with a pre-seeded stub for one test."""
    app.dependency_overrides[_get_snapshot_dep] = lambda: _StubSnapshot()
    yield
    app.dependency_overrides.pop(_get_snapshot_dep, None)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/account/summary
# ---------------------------------------------------------------------------


class TestAccountSummary:
    """Tests for GET /api/v1/account/summary."""

    async def test_account_summary_returns_503_when_never_connected(
        self, client: httpx.AsyncClient
    ) -> None:
        """Cold start with IB down → 503 (iter-3 SF P1).

        The previous behaviour of returning a zero-summary on cold start
        rendered as "$0.00" on the dashboard, indistinguishable from a
        truly-empty account. The 503 surfaces the gateway outage honestly.
        """
        response = await client.get("/api/v1/account/summary")

        assert response.status_code == 503
        body = response.json()
        assert "IB Gateway unreachable" in body["detail"]

    async def test_account_summary_returns_200_after_successful_refresh(
        self, client: httpx.AsyncClient, seeded_snapshot_app: None
    ) -> None:
        """Once a refresh has succeeded, ``/summary`` serves cached values."""
        response = await client.get("/api/v1/account/summary")

        assert response.status_code == 200
        body = response.json()
        assert "net_liquidation" in body
        assert "buying_power" in body
        assert "margin_used" in body
        assert isinstance(body["net_liquidation"], float)

    async def test_account_summary_has_all_fields(
        self, client: httpx.AsyncClient, seeded_snapshot_app: None
    ) -> None:
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

    async def test_account_portfolio_returns_503_when_never_connected(
        self, client: httpx.AsyncClient
    ) -> None:
        """Cold start → 503 (iter-3 SF P1).

        Empty list on cold start is indistinguishable from "no positions"
        — 503 surfaces the outage honestly.
        """
        response = await client.get("/api/v1/account/portfolio")

        assert response.status_code == 503

    async def test_account_portfolio_returns_200_after_successful_refresh(
        self, client: httpx.AsyncClient, seeded_snapshot_app: None
    ) -> None:
        """Once a refresh has succeeded, ``/portfolio`` serves cached list."""
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
