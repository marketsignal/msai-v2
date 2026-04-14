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
    """Build a DataFrame that mimics Databento OHLCV-1m output after to_df().reset_index().

    The Databento SDK returns a DataFrame with ``ts_event`` as the timestamp
    column plus OHLCV fields.
    """
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


class TestDatabentoFetchBars:
    """Tests for DatabentoClient.fetch_bars."""

    async def test_fetch_bars_returns_dataframe(self) -> None:
        """Mock Databento SDK response and verify DataFrame columns."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(_mock_databento_df())

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        expected_cols = ["timestamp", "open", "high", "low", "close", "volume"]
        assert list(df.columns) == expected_cols
        assert df["open"].iloc[0] == 4500.0

    async def test_fetch_bars_with_dataset_and_schema(self) -> None:
        """Verify custom dataset and schema are passed to the SDK."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(_mock_databento_df())

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_bars(
                "AAPL",
                "2024-03-15",
                "2024-03-16",
                dataset="EQUS.MINI",
                schema="ohlcv-1m",
            )
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

        # Verify the SDK was called with the correct parameters
        historical_instance = mock_module.Historical.return_value  # type: ignore[attr-defined]
        call_kwargs = historical_instance.timeseries.get_range.call_args
        assert call_kwargs.kwargs["dataset"] == "EQUS.MINI"
        assert call_kwargs.kwargs["schema"] == "ohlcv-1m"

    async def test_fetch_bars_empty_result(self) -> None:
        """When Databento returns empty data, return empty DataFrame."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(pd.DataFrame())

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    async def test_fetch_bars_raises_on_sdk_error(self) -> None:
        """When Databento SDK raises an error, propagate as RuntimeError."""
        # Arrange
        client = DatabentoClient(api_key="test_key")

        mock_module = ModuleType("databento")
        mock_historical_instance = MagicMock()
        mock_historical_instance.timeseries.get_range.side_effect = RuntimeError("API error")
        mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act / Assert
            with pytest.raises(RuntimeError, match="Databento historical request failed"):
                await client.fetch_bars("ES.FUT", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

    async def test_fetch_bars_raises_without_api_key(self) -> None:
        """When no API key is configured, raise RuntimeError."""
        # Arrange
        client = DatabentoClient(api_key="")

        # Act / Assert
        with pytest.raises(RuntimeError, match="DATABENTO_API_KEY is not configured"):
            await client.fetch_bars("ES.FUT", "2024-03-15", "2024-03-16")

    async def test_fetch_bars_renames_size_to_volume(self) -> None:
        """When the response has 'size' instead of 'volume', rename it."""
        # Arrange
        timestamps = pd.date_range("2024-03-15 09:30", periods=2, freq="1min", tz="UTC")
        df_with_size = pd.DataFrame(
            {
                "ts_event": timestamps,
                "open": [100.0, 101.0],
                "high": [105.0, 106.0],
                "low": [95.0, 96.0],
                "close": [102.0, 103.0],
                "size": [500, 600],
            }
        )
        client = DatabentoClient(api_key="test_key")
        mock_module = _make_mock_databento_module(df_with_size)

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act
            df = await client.fetch_bars("AAPL", "2024-03-15", "2024-03-16")
        finally:
            if original is not None:
                sys.modules["databento"] = original
            else:
                sys.modules.pop("databento", None)

        # Assert
        assert "volume" in df.columns
        assert "size" not in df.columns
        assert df["volume"].iloc[0] == 500
