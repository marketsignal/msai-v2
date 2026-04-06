"""Tests for msai.services.data_sources.databento_client module."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pandas as pd
import pytest

from msai.services.data_sources.databento_client import DatabentoClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_databento_df() -> pd.DataFrame:
    """Build a DataFrame that mimics Databento OHLCV-1m output."""
    timestamps = pd.date_range("2024-03-15 09:30", periods=3, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "ts_event": timestamps,
            "open": [4500.0, 4501.0, 4502.0],
            "high": [4505.0, 4506.0, 4507.0],
            "low": [4495.0, 4496.0, 4497.0],
            "close": [4502.0, 4503.0, 4504.0],
            "volume": [100, 150, 200],
        }
    )


def _make_mock_databento_module(to_df_return: pd.DataFrame) -> ModuleType:
    """Create a fake ``databento`` module with mocked Historical client."""
    mock_data = MagicMock()
    mock_data.to_df.return_value = to_df_return

    mock_historical_instance = MagicMock()
    mock_historical_instance.timeseries.get_range.return_value = mock_data

    mock_module = ModuleType("databento")
    mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

    return mock_module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDatabentoFetchFuturesBars:
    """Tests for DatabentoClient.fetch_futures_bars."""

    async def test_fetch_futures_bars_returns_dataframe(self) -> None:
        """Mock Databento SDK response and verify DataFrame columns."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(_mock_databento_df())

        # Inject the mock module into sys.modules so the lazy import finds it.
        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_futures_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        expected_cols = ["symbol", "timestamp", "open", "high", "low", "close", "volume"]
        assert list(df.columns) == expected_cols
        assert df["symbol"].iloc[0] == "ES.FUT"
        assert df["open"].iloc[0] == 4500.0

    async def test_fetch_futures_bars_empty_result(self) -> None:
        """When Databento returns empty data, return empty DataFrame."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(pd.DataFrame())

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_futures_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    async def test_fetch_futures_bars_handles_sdk_error(self) -> None:
        """When Databento SDK raises an error, return empty DataFrame gracefully."""
        # Arrange
        client = DatabentoClient(api_key="test_key")

        mock_module = ModuleType("databento")
        mock_historical_instance = MagicMock()
        mock_historical_instance.timeseries.get_range.side_effect = RuntimeError("API error")
        mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_futures_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert df.empty
