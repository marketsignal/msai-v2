"""Tests for msai.services.parquet_store module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import pytest

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
            "volume": [1000 + i * 100 for i in range(n)],
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteAndReadBars:
    """Tests for write_bars and read_bars round-trip."""

    def test_write_and_read_bars(self, tmp_path: Path) -> None:
        """Write a DataFrame then read it back and verify equality."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df = _sample_bars("AAPL", n=5)

        # Act
        checksum = store.write_bars("stocks", "AAPL", df)
        result = store.read_bars("stocks", "AAPL")

        # Assert
        assert checksum != ""
        assert len(checksum) == 64  # SHA-256 hex
        assert len(result) == 5
        assert list(result.columns) == list(df.columns)
        assert result["symbol"].iloc[0] == "AAPL"

    def test_write_bars_deduplicates(self, tmp_path: Path) -> None:
        """Writing overlapping data should deduplicate on (symbol, timestamp)."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df1 = _sample_bars("AAPL", n=5)
        df2 = _sample_bars("AAPL", n=5)  # same timestamps

        # Act
        store.write_bars("stocks", "AAPL", df1)
        store.write_bars("stocks", "AAPL", df2)
        result = store.read_bars("stocks", "AAPL")

        # Assert -- dedup should keep only 5 unique rows
        assert len(result) == 5

    def test_write_bars_empty_df_returns_empty_string(self, tmp_path: Path) -> None:
        """Writing an empty DataFrame should return empty checksum."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df = pd.DataFrame()

        # Act
        checksum = store.write_bars("stocks", "AAPL", df)

        # Assert
        assert checksum == ""

    def test_read_bars_nonexistent_symbol(self, tmp_path: Path) -> None:
        """Reading a symbol that does not exist returns empty DataFrame."""
        # Arrange
        store = ParquetStore(str(tmp_path))

        # Act
        result = store.read_bars("stocks", "ZZZZ")

        # Assert
        assert result.empty

    def test_read_bars_with_date_range_filter(self, tmp_path: Path) -> None:
        """Read bars filtered by start/end should return subset."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        df = _sample_bars("AAPL", n=10)
        store.write_bars("stocks", "AAPL", df)

        # Act -- filter to first 5 minutes
        start = "2024-03-15 09:30"
        end = "2024-03-15 09:34"
        result = store.read_bars("stocks", "AAPL", start=start, end=end)

        # Assert
        assert len(result) == 5


class TestListSymbols:
    """Tests for list_symbols."""

    def test_list_symbols_returns_written_symbols(self, tmp_path: Path) -> None:
        """Write bars for 2 symbols, verify both are listed."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        store.write_bars("stocks", "AAPL", _sample_bars("AAPL"))
        store.write_bars("stocks", "GOOG", _sample_bars("GOOG"))

        # Act
        symbols = store.list_symbols("stocks")

        # Assert
        assert sorted(symbols) == ["AAPL", "GOOG"]

    def test_list_symbols_empty_asset_class(self, tmp_path: Path) -> None:
        """Listing symbols for a nonexistent asset class returns empty list."""
        # Arrange
        store = ParquetStore(str(tmp_path))

        # Act
        symbols = store.list_symbols("crypto")

        # Assert
        assert symbols == []


class TestGetStorageStats:
    """Tests for get_storage_stats."""

    def test_get_storage_stats_with_data(self, tmp_path: Path) -> None:
        """Verify stats returns dict with asset_class keys after writing data."""
        # Arrange
        store = ParquetStore(str(tmp_path))
        store.write_bars("stocks", "AAPL", _sample_bars("AAPL"))
        store.write_bars("futures", "ES", _sample_bars("ES"))

        # Act
        stats = store.get_storage_stats()

        # Assert
        assert "asset_classes" in stats
        assert "stocks" in stats["asset_classes"]
        assert "futures" in stats["asset_classes"]
        assert stats["total_files"] == 2
        assert stats["total_bytes"] > 0

    def test_get_storage_stats_empty(self, tmp_path: Path) -> None:
        """Verify stats returns zeroes when no data exists."""
        # Arrange
        store = ParquetStore(str(tmp_path))

        # Act
        stats = store.get_storage_stats()

        # Assert
        assert stats["total_files"] == 0
        assert stats["total_bytes"] == 0
        assert stats["asset_classes"] == {}
