"""Unit tests for the strategies API endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

from msai.main import app
from msai.services.strategy_registry import StrategyInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies" / "example"


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/strategies/
# ---------------------------------------------------------------------------


class TestListStrategies:
    """Tests for GET /api/v1/strategies/."""

    async def test_list_strategies_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/strategies/ returns 200 with a list of strategies."""
        response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert isinstance(body["items"], list)
        assert isinstance(body["total"], int)

    async def test_list_strategies_discovers_example(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/strategies/ discovers the example EMA cross strategy."""
        # Patch _STRATEGIES_DIR to point at the example strategies
        with patch("msai.api.strategies._STRATEGIES_DIR", STRATEGIES_DIR):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

        class_names = [item["strategy_class"] for item in body["items"]]
        assert "EMACrossStrategy" in class_names

    async def test_list_strategies_empty_dir(
        self, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        """GET /api/v1/strategies/ returns empty list for empty directory."""
        empty_dir = tmp_path / "empty_strategies"
        empty_dir.mkdir()

        with patch("msai.api.strategies._STRATEGIES_DIR", empty_dir):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/strategies/{id}/validate
# ---------------------------------------------------------------------------


class TestValidateStrategy:
    """Tests for POST /api/v1/strategies/{id}/validate."""

    async def test_validate_strategy_returns_200(self, client: httpx.AsyncClient) -> None:
        """POST /api/v1/strategies/{id}/validate returns 200 for a valid strategy."""
        strategy_id = UUID(int=0)

        with patch("msai.api.strategies._STRATEGIES_DIR", STRATEGIES_DIR):
            response = await client.post(f"/api/v1/strategies/{strategy_id}/validate")

        assert response.status_code == 200
        body = response.json()
        assert "message" in body
        assert "validated successfully" in body["message"]

    async def test_validate_strategy_no_strategies_returns_404(
        self, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        """POST /validate returns 404 when no strategies exist on disk."""
        strategy_id = UUID(int=0)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with patch("msai.api.strategies._STRATEGIES_DIR", empty_dir):
            response = await client.post(f"/api/v1/strategies/{strategy_id}/validate")

        assert response.status_code == 404
