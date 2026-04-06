"""Tests for msai.services.market_data_query module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest

from msai.services.market_data_query import MarketDataQuery
from msai.services.parquet_store import ParquetStore

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_bars(symbol: str = "AAPL", n: int = 5) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for testing."""
    timestamps = pd.date_range("2024-03-15 09:30", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "symbol": [symbol] * n,
            "timestamp": timestamps,
            "open": [150.0 + i for i in range(n)],
            "high": [151.0 + i for i in range(n)],
            "low": [149.0 + i for i in range(n)],
            "close": [150.5 + i for i in range(n)],
            "volume": [1000.0 + i * 100 for i in range(n)],
        }
    )


def _write_test_data(tmp_path: str, asset_class: str, symbol: str, n: int = 5) -> None:
    """Write sample bar data to the Parquet store for testing."""
    store = ParquetStore(tmp_path)
    store.write_bars(asset_class, symbol, _sample_bars(symbol, n))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetBars:
    """Tests for MarketDataQuery.get_bars."""

    def test_get_bars_returns_list_of_dicts(self, tmp_path: Path) -> None:
        """Write Parquet data, query via DuckDB, verify list of dicts."""
        # Arrange
        _write_test_data(str(tmp_path), "stocks", "AAPL", n=5)
        query = MarketDataQuery(str(tmp_path))

        # Act
        bars = query.get_bars("AAPL", "2024-03-15", "2024-03-16")

        # Assert
        assert isinstance(bars, list)
        assert len(bars) == 5
        bar = bars[0]
        assert "timestamp" in bar
        assert "open" in bar
        assert "high" in bar
        assert "low" in bar
        assert "close" in bar
        assert "volume" in bar

    def test_get_bars_empty_for_nonexistent_symbol(self, tmp_path: Path) -> None:
        """Querying a symbol with no data should return empty list."""
        # Arrange
        query = MarketDataQuery(str(tmp_path))

        # Act
        bars = query.get_bars("ZZZZ", "2024-01-01", "2024-12-31")

        # Assert
        assert bars == []

    def test_get_bars_respects_date_range(self, tmp_path: Path) -> None:
        """Bars outside the requested date range should not be returned.

        The end date filter uses ``< end_date + 1 day`` to include all
        intraday bars for the end date.  We test with data spanning two
        calendar days so that the date boundary excludes the second day.
        """
        # Arrange -- write bars on 2024-03-15 AND 2024-03-16
        store = ParquetStore(str(tmp_path))
        ts_day1 = pd.date_range("2024-03-15 09:30", periods=5, freq="1min")
        ts_day2 = pd.date_range("2024-03-16 09:30", periods=5, freq="1min")
        df = pd.DataFrame(
            {
                "symbol": ["AAPL"] * 10,
                "timestamp": list(ts_day1) + list(ts_day2),
                "open": [150.0 + i for i in range(10)],
                "high": [151.0 + i for i in range(10)],
                "low": [149.0 + i for i in range(10)],
                "close": [150.5 + i for i in range(10)],
                "volume": [1000.0 + i * 100 for i in range(10)],
            }
        )
        store.write_bars("stocks", "AAPL", df)
        query = MarketDataQuery(str(tmp_path))

        # Act -- query only the first day
        bars = query.get_bars("AAPL", "2024-03-15", "2024-03-15")

        # Assert -- only bars from 2024-03-15 are returned
        assert len(bars) == 5


class TestGetSymbols:
    """Tests for MarketDataQuery.get_symbols."""

    def test_get_symbols_returns_grouped(self, tmp_path: Path) -> None:
        """Write data for stocks and futures, verify grouping."""
        # Arrange
        _write_test_data(str(tmp_path), "stocks", "AAPL")
        _write_test_data(str(tmp_path), "stocks", "GOOG")
        _write_test_data(str(tmp_path), "futures", "ES")
        query = MarketDataQuery(str(tmp_path))

        # Act
        symbols = query.get_symbols()

        # Assert
        assert "stocks" in symbols
        assert sorted(symbols["stocks"]) == ["AAPL", "GOOG"]
        assert "futures" in symbols
        assert symbols["futures"] == ["ES"]

    def test_get_symbols_empty_store(self, tmp_path: Path) -> None:
        """Empty data root should return empty dict."""
        # Arrange
        query = MarketDataQuery(str(tmp_path))

        # Act
        symbols = query.get_symbols()

        # Assert
        assert symbols == {}


class TestGetStorageStats:
    """Tests for MarketDataQuery.get_storage_stats."""

    def test_get_storage_stats_returns_correct_structure(self, tmp_path: Path) -> None:
        """Verify storage stats returns expected keys and values."""
        # Arrange
        _write_test_data(str(tmp_path), "stocks", "AAPL")
        query = MarketDataQuery(str(tmp_path))

        # Act
        stats = query.get_storage_stats()

        # Assert
        assert "asset_classes" in stats
        assert "total_files" in stats
        assert "total_bytes" in stats
        assert stats["total_files"] >= 1
        assert stats["total_bytes"] > 0
