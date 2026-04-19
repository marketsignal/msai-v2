"""Tests for the Market Data API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from msai.main import app


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class TestSymbolsEndpoint:
    """Tests for ``GET /api/v1/market-data/symbols``."""

    async def test_symbols_endpoint_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/market-data/symbols must return 200 with symbols dict."""
        # Act
        response = await client.get("/api/v1/market-data/symbols")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert "symbols" in body
        assert isinstance(body["symbols"], dict)


class TestStatusEndpoint:
    """Tests for ``GET /api/v1/market-data/status``."""

    async def test_status_endpoint_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/market-data/status must return 200 with storage stats."""
        # Act
        response = await client.get("/api/v1/market-data/status")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "storage" in body
        storage = body["storage"]
        assert "asset_classes" in storage
        assert "total_files" in storage
        assert "total_bytes" in storage


class TestBarsEndpoint:
    """Tests for ``GET /api/v1/market-data/bars/{symbol}``."""

    async def test_bars_endpoint_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/market-data/bars/AAPL with query params returns 200."""
        # Act
        response = await client.get(
            "/api/v1/market-data/bars/AAPL",
            params={"start": "2024-01-01", "end": "2024-01-31"},
        )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["symbol"] == "AAPL"
        assert body["interval"] == "1m"
        assert isinstance(body["bars"], list)
        assert body["count"] == len(body["bars"])

    async def test_bars_endpoint_requires_start_and_end(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/market-data/bars/AAPL without start/end returns 422."""
        # Act
        response = await client.get("/api/v1/market-data/bars/AAPL")

        # Assert
        assert response.status_code == 422


class TestIngestEndpoint:
    """Tests for ``POST /api/v1/market-data/ingest``."""

    async def test_ingest_endpoint_returns_503_when_redis_unavailable(
        self, client: httpx.AsyncClient
    ) -> None:
        """POST /api/v1/market-data/ingest returns 503 when Redis is down."""
        # Force get_redis_pool to raise ConnectionError
        with patch(
            "msai.core.queue.get_redis_pool",
            new=AsyncMock(side_effect=ConnectionError("Redis not available")),
        ):
            response = await client.post(
                "/api/v1/market-data/ingest",
                json={
                    "asset_class": "stocks",
                    "symbols": ["AAPL"],
                    "start": "2024-01-01",
                    "end": "2024-01-31",
                },
            )

        # Assert
        assert response.status_code == 503
        body = response.json()
        assert "Redis" in body["detail"]

    async def test_ingest_endpoint_returns_202_on_success(self, client: httpx.AsyncClient) -> None:
        """POST /api/v1/market-data/ingest returns 202 when enqueue succeeds."""
        mock_pool = AsyncMock()

        # The endpoint imports get_redis_pool and enqueue_ingest inside the
        # try block from msai.core.queue. We patch them at that module level.
        with (
            patch("msai.core.queue.get_redis_pool", new=AsyncMock(return_value=mock_pool)),
            patch("msai.core.queue.enqueue_ingest", new=AsyncMock()),
        ):
            response = await client.post(
                "/api/v1/market-data/ingest",
                json={
                    "asset_class": "stocks",
                    "symbols": ["AAPL"],
                    "start": "2024-01-01",
                    "end": "2024-01-31",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["message"] == "Ingestion job enqueued"
        assert body["asset_class"] == "stocks"
