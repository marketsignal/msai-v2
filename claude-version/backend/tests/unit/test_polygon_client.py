"""Tests for msai.services.data_sources.polygon_client module."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pandas as pd
import pytest

from msai.services.data_sources.polygon_client import PolygonClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_REQUEST = httpx.Request("GET", "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/minute")


def _polygon_response(results: list[dict] | None = None, next_url: str | None = None) -> dict:
    """Build a mock Polygon API response."""
    return {
        "results": results or [],
        "status": "OK",
        "next_url": next_url,
    }


def _make_httpx_response(json_data: dict) -> httpx.Response:
    """Create an httpx.Response with a request attached (required for raise_for_status)."""
    response = httpx.Response(status_code=200, json=json_data, request=_DUMMY_REQUEST)
    return response


def _sample_polygon_bars() -> list[dict]:
    """Return sample Polygon bar results."""
    return [
        {
            "t": 1710496200000,  # 2024-03-15 09:30 UTC
            "o": 150.0,
            "h": 151.0,
            "l": 149.0,
            "c": 150.5,
            "v": 1000,
        },
        {
            "t": 1710496260000,  # 2024-03-15 09:31 UTC
            "o": 150.5,
            "h": 152.0,
            "l": 150.0,
            "c": 151.5,
            "v": 1200,
        },
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPolygonFetchBars:
    """Tests for PolygonClient.fetch_bars."""

    async def test_fetch_bars_returns_dataframe(self) -> None:
        """Mock httpx response and verify DataFrame columns."""
        # Arrange
        client = PolygonClient(api_key="test_key", rate_limit_delay=0)
        mock_response = _make_httpx_response(
            _polygon_response(results=_sample_polygon_bars())
        )

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            # Act
            df = await client.fetch_bars("AAPL", "2024-03-15", "2024-03-15")

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        expected_cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
        assert list(df.columns) == expected_cols
        assert df["symbol"].iloc[0] == "AAPL"
        assert df["open"].iloc[0] == 150.0

    async def test_fetch_bars_empty_results(self) -> None:
        """When Polygon returns no results, return empty DataFrame."""
        # Arrange
        client = PolygonClient(api_key="test_key", rate_limit_delay=0)
        mock_response = _make_httpx_response(
            _polygon_response(results=[])
        )

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            # Act
            df = await client.fetch_bars("AAPL", "2024-03-15", "2024-03-15")

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    async def test_fetch_bars_handles_pagination(self) -> None:
        """When Polygon returns next_url, client follows pagination."""
        # Arrange
        client = PolygonClient(api_key="test_key", rate_limit_delay=0)

        page1_bars = [_sample_polygon_bars()[0]]
        page2_bars = [_sample_polygon_bars()[1]]

        response_page1 = _make_httpx_response(
            _polygon_response(
                results=page1_bars,
                next_url="https://api.polygon.io/v2/aggs/next",
            )
        )
        response_page2 = _make_httpx_response(
            _polygon_response(results=page2_bars)
        )

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=[response_page1, response_page2],
        ):
            # Act
            df = await client.fetch_bars("AAPL", "2024-03-15", "2024-03-15")

        # Assert
        assert len(df) == 2
