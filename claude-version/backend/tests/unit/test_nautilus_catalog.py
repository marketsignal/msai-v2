"""Unit tests for msai.services.nautilus.catalog module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest

from msai.services.nautilus.catalog import NautilusCatalog
from msai.services.parquet_store import ParquetStore

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_bars(symbol: str = "AAPL", n: int = 20) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for testing."""
    timestamps = pd.date_range("2024-06-01 09:30", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "symbol": [symbol] * n,
            "timestamp": timestamps,
            "open": [150.0 + i * 0.1 for i in range(n)],
            "high": [151.0 + i * 0.1 for i in range(n)],
            "low": [149.0 + i * 0.1 for i in range(n)],
            "close": [150.5 + i * 0.1 for i in range(n)],
            "volume": [1000 + i * 100 for i in range(n)],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetAvailableInstruments:
    """Tests for NautilusCatalog.get_available_instruments."""

    def test_get_available_instruments_finds_written_data(self, tmp_path: Path) -> None:
        """Write Parquet data for two symbols and verify instruments are listed."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        store.write_bars("stocks", "AAPL", _sample_bars("AAPL"))
        store.write_bars("stocks", "GOOG", _sample_bars("GOOG"))
        store.write_bars("futures", "ES", _sample_bars("ES"))
        catalog = NautilusCatalog(str(tmp_path))

        # Act
        instruments = catalog.get_available_instruments()

        # Assert
        assert "futures/ES" in instruments
        assert "stocks/AAPL" in instruments
        assert "stocks/GOOG" in instruments
        assert len(instruments) == 3

    def test_get_available_instruments_empty_root(self, tmp_path: Path) -> None:
        """An empty data root returns an empty list."""
        # Arrange
        catalog = NautilusCatalog(str(tmp_path))

        # Act
        instruments = catalog.get_available_instruments()

        # Assert
        assert instruments == []

    def test_get_available_instruments_nonexistent_root(self, tmp_path: Path) -> None:
        """A nonexistent data root returns an empty list without raising."""
        # Arrange
        catalog = NautilusCatalog(str(tmp_path / "nonexistent"))

        # Act
        instruments = catalog.get_available_instruments()

        # Assert
        assert instruments == []


class TestLoadBars:
    """Tests for NautilusCatalog.load_bars."""

    def test_load_bars_returns_dataframe(self, tmp_path: Path) -> None:
        """Write Parquet bars then load them back via the catalog."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df = _sample_bars("AAPL", n=20)
        store.write_bars("stocks", "AAPL", df)
        catalog = NautilusCatalog(str(tmp_path))

        # Act
        result = catalog.load_bars("AAPL", asset_class="stocks")

        # Assert
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 20
        assert "close" in result.columns
        assert "timestamp" in result.columns

    def test_load_bars_with_date_range(self, tmp_path: Path) -> None:
        """Load bars filtered by start and end date."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df = _sample_bars("AAPL", n=20)
        store.write_bars("stocks", "AAPL", df)
        catalog = NautilusCatalog(str(tmp_path))

        # Act
        result = catalog.load_bars(
            "AAPL",
            start="2024-06-01 09:30",
            end="2024-06-01 09:39",
            asset_class="stocks",
        )

        # Assert
        assert len(result) == 10  # First 10 minutes

    def test_load_bars_nonexistent_symbol(self, tmp_path: Path) -> None:
        """Loading bars for a symbol with no data returns empty DataFrame."""
        # Arrange
        catalog = NautilusCatalog(str(tmp_path))

        # Act
        result = catalog.load_bars("ZZZZ", asset_class="stocks")

        # Assert
        assert result.empty
