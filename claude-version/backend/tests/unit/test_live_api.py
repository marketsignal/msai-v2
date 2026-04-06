"""Unit tests for the live trading API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock AsyncSession that returns empty results by default."""
    session = AsyncMock(spec=AsyncSession)

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one.return_value = 0
    mock_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_result
    return session


@pytest.fixture
def client_with_mock_db(mock_db: AsyncMock) -> httpx.AsyncClient:
    """Async test client with the DB dependency overridden to use a mock."""

    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    app.dependency_overrides[get_db] = _override_get_db

    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://testserver")  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/status
# ---------------------------------------------------------------------------


class TestLiveStatus:
    """Tests for GET /api/v1/live/status."""

    async def test_live_status_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/status returns 200 with deployment list."""
        response = await client_with_mock_db.get("/api/v1/live/status")

        assert response.status_code == 200
        body = response.json()
        assert "deployments" in body
        assert "risk_halted" in body
        assert "active_count" in body
        assert isinstance(body["deployments"], list)


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/live/kill-all
# ---------------------------------------------------------------------------


class TestLiveKillAll:
    """Tests for POST /api/v1/live/kill-all."""

    async def test_kill_all_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """POST /api/v1/live/kill-all returns 200 with stopped count."""
        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        assert response.status_code == 200
        body = response.json()
        assert "stopped" in body
        assert "risk_halted" in body
        assert isinstance(body["stopped"], int)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/positions
# ---------------------------------------------------------------------------


class TestLivePositions:
    """Tests for GET /api/v1/live/positions."""

    async def test_live_positions_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/positions returns 200 with positions list."""
        response = await client_with_mock_db.get("/api/v1/live/positions")

        assert response.status_code == 200
        body = response.json()
        assert "positions" in body
        assert isinstance(body["positions"], list)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/trades
# ---------------------------------------------------------------------------


class TestLiveTrades:
    """Tests for GET /api/v1/live/trades."""

    async def test_live_trades_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/trades returns 200 with trades list."""
        response = await client_with_mock_db.get("/api/v1/live/trades")

        assert response.status_code == 200
        body = response.json()
        assert "trades" in body
        assert "total" in body
        assert isinstance(body["trades"], list)
        assert body["total"] == 0
