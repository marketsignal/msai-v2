"""Tests for msai.core.data_integrity module."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pytest

from msai.core.data_integrity import atomic_write_parquet, dedup_bars, detect_gaps

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_table() -> pa.Table:
    """Return a minimal PyArrow table for testing."""
    return pa.table({"symbol": ["AAPL", "GOOG"], "price": [150.0, 2800.0]})


# ---------------------------------------------------------------------------
# atomic_write_parquet
# ---------------------------------------------------------------------------


class TestAtomicWriteParquet:
    """Tests for atomic_write_parquet."""

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        # Arrange
        table = _sample_table()
        target = tmp_path / "data" / "bars.parquet"

        # Act
        atomic_write_parquet(table, target)

        # Assert
        assert target.exists()
        assert target.stat().st_size > 0

    def test_atomic_write_returns_checksum(self, tmp_path: Path) -> None:
        # Arrange
        table = _sample_table()
        target = tmp_path / "out.parquet"

        # Act
        checksum = atomic_write_parquet(table, target)

        # Assert
        assert isinstance(checksum, str)
        assert len(checksum) == 64  # SHA-256 hex digest length
        # Must be a valid hex string
        int(checksum, 16)

    def test_atomic_write_cleans_up_on_error(self, tmp_path: Path) -> None:
        # Arrange
        table = _sample_table()
        target = tmp_path / "fail.parquet"

        # Act
        with (
            patch("msai.core.data_integrity.pq.write_table", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            atomic_write_parquet(table, target)

        # Assert -- no temp file (.parquet.tmp) should remain
        remaining = list(tmp_path.glob("*.parquet.tmp"))
        assert remaining == []
        assert not target.exists()


# ---------------------------------------------------------------------------
# dedup_bars
# ---------------------------------------------------------------------------


class TestDedupBars:
    """Tests for dedup_bars."""

    def test_dedup_bars_removes_duplicates(self) -> None:
        # Arrange -- two rows share the same (symbol, timestamp) key
        df = pd.DataFrame(
            {
                "symbol": ["AAPL", "AAPL", "GOOG"],
                "timestamp": [
                    "2024-01-02 09:30",
                    "2024-01-02 09:30",
                    "2024-01-02 09:30",
                ],
                "price": [149.0, 150.0, 2800.0],
            }
        )

        # Act
        result = dedup_bars(df)

        # Assert
        assert len(result) == 2
        # "last" occurrence kept -- price should be 150 for AAPL
        aapl_row = result[result["symbol"] == "AAPL"]
        assert aapl_row.iloc[0]["price"] == 150.0

    def test_dedup_bars_preserves_order_and_resets_index(self) -> None:
        # Arrange
        df = pd.DataFrame(
            {
                "symbol": ["A", "B", "A"],
                "timestamp": ["t1", "t1", "t1"],
                "value": [1, 2, 3],
            }
        )

        # Act
        result = dedup_bars(df)

        # Assert
        assert list(result.index) == list(range(len(result)))

    def test_dedup_bars_custom_key_columns(self) -> None:
        # Arrange
        df = pd.DataFrame(
            {
                "id": [1, 1, 2],
                "value": [10, 20, 30],
            }
        )

        # Act
        result = dedup_bars(df, key_columns=("id",))

        # Assert
        assert len(result) == 2
        assert result.iloc[0]["value"] == 20  # Last occurrence for id=1


# ---------------------------------------------------------------------------
# detect_gaps
# ---------------------------------------------------------------------------


class TestDetectGaps:
    """Tests for detect_gaps."""

    def test_detect_gaps_finds_missing(self) -> None:
        # Arrange -- 09:30, 09:31, 09:35 with a 3-bar gap (09:32, 09:33, 09:34)
        timestamps = [
            "2024-01-02 09:30",
            "2024-01-02 09:31",
            "2024-01-02 09:35",
        ]
        df = pd.DataFrame({"timestamp": pd.to_datetime(timestamps)})

        # Act
        gaps = detect_gaps(df, expected_freq_minutes=1)

        # Assert
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["start"] == pd.Timestamp("2024-01-02 09:31")
        assert gap["end"] == pd.Timestamp("2024-01-02 09:35")
        assert gap["count_missing"] == 3

    def test_detect_gaps_returns_empty_for_contiguous(self) -> None:
        # Arrange
        timestamps = pd.date_range("2024-01-02 09:30", periods=5, freq="1min")
        df = pd.DataFrame({"timestamp": timestamps})

        # Act
        gaps = detect_gaps(df, expected_freq_minutes=1)

        # Assert
        assert gaps == []

    def test_detect_gaps_returns_empty_for_empty_df(self) -> None:
        # Arrange
        df = pd.DataFrame({"timestamp": pd.Series([], dtype="datetime64[ns]")})

        # Act
        gaps = detect_gaps(df, expected_freq_minutes=1)

        # Assert
        assert gaps == []

    def test_detect_gaps_ignores_outside_trading_hours(self) -> None:
        # Arrange -- gap spans outside trading window
        timestamps = [
            "2024-01-02 08:00",
            "2024-01-02 09:30",
        ]
        df = pd.DataFrame({"timestamp": pd.to_datetime(timestamps)})

        # Act
        gaps = detect_gaps(df, expected_freq_minutes=1, trading_start="09:30", trading_end="16:00")

        # Assert -- 08:00 is before trading_start, so this gap is not reported
        assert gaps == []
