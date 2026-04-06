"""Unit tests for the backtests API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

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

    # Mock execute to return a result with scalars().all() -> empty list
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    # For func.count() queries -- scalar_one returns 0
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
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield client  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/backtests/history
# ---------------------------------------------------------------------------


class TestListBacktests:
    """Tests for GET /api/v1/backtests/history."""

    async def test_list_backtests_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/backtests/history returns 200 with paginated results."""
        response = await client_with_mock_db.get("/api/v1/backtests/history")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert isinstance(body["items"], list)
        assert body["total"] == 0

    async def test_list_backtests_accepts_pagination_params(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/backtests/history accepts page and page_size params."""
        response = await client_with_mock_db.get("/api/v1/backtests/history?page=2&page_size=10")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_list_backtests_rejects_invalid_page(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/backtests/history rejects page < 1."""
        response = await client_with_mock_db.get("/api/v1/backtests/history?page=0")

        assert response.status_code == 422
