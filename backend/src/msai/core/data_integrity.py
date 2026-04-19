"""Data integrity utilities for Parquet writes in MSAI v2.

Provides atomic file writes with checksums, deduplication of bar data,
and gap detection for time-series DataFrames.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def atomic_write_parquet(table: pa.Table, target_path: Path) -> str:
    """Write a Parquet file atomically with ZSTD compression.

    The write uses a temporary file in the same directory as *target_path*
    so that ``os.rename`` is atomic (same filesystem). On success the temp
    file is renamed to *target_path*; on any failure the temp file is removed.

    Args:
        table: The PyArrow Table to write.
        target_path: Destination path for the final ``.parquet`` file.

    Returns:
        The SHA-256 hex digest of the written file.

    Raises:
        OSError: If the directory cannot be created or the rename fails.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd: int = -1
    tmp_path: str = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=".parquet.tmp",
            dir=str(target_path.parent),
        )
        # Close the file descriptor immediately -- pq.write_table opens by path.
        os.close(fd)
        fd = -1

        pq.write_table(table, tmp_path, compression="zstd")

        sha256_hex = _sha256_file(tmp_path)

        os.rename(tmp_path, target_path)
        tmp_path = ""  # Rename succeeded; nothing to clean up.

        return sha256_hex
    except BaseException:
        # Clean up the temp file on *any* failure (including KeyboardInterrupt).
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def dedup_bars(
    df: pd.DataFrame,
    key_columns: tuple[str, ...] = ("symbol", "timestamp"),
) -> pd.DataFrame:
    """Remove duplicate rows by *key_columns*, keeping the last occurrence.

    Args:
        df: DataFrame containing bar data.
        key_columns: Column names that form the dedup key.

    Returns:
        A new DataFrame with duplicates removed. The index is reset.
    """
    key_list: list[str] = list(key_columns)
    deduped: pd.DataFrame = df.drop_duplicates(subset=key_list, keep="last")
    deduped = deduped.reset_index(drop=True)
    return deduped


def detect_gaps(
    df: pd.DataFrame,
    expected_freq_minutes: int = 1,
    trading_start: str = "09:30",
    trading_end: str = "16:00",
) -> list[dict[str, Any]]:
    """Detect missing timestamps in a time-series DataFrame.

    The function assumes a ``"timestamp"`` column with timezone-aware or
    naive ``datetime64`` values. Gaps are identified by comparing consecutive
    timestamps against *expected_freq_minutes*. Only gaps that fall within the
    trading window (``trading_start`` -- ``trading_end``) are reported.

    Args:
        df: DataFrame with a ``"timestamp"`` column sorted in ascending order.
        expected_freq_minutes: Expected number of minutes between consecutive rows.
        trading_start: Start of the trading window as ``"HH:MM"``.
        trading_end: End of the trading window as ``"HH:MM"``.

    Returns:
        A list of gap dictionaries, each containing:
        - ``start``: The last observed timestamp before the gap.
        - ``end``: The first observed timestamp after the gap.
        - ``count_missing``: Number of expected bars missing in the gap.
    """
    if df.empty or "timestamp" not in df.columns:
        return []

    ts: pd.Series[Any] = pd.to_datetime(df["timestamp"]).sort_values().reset_index(drop=True)

    freq = pd.Timedelta(minutes=expected_freq_minutes)
    t_start = pd.Timestamp(trading_start).time()
    t_end = pd.Timestamp(trading_end).time()

    gaps: list[dict[str, Any]] = []

    for i in range(1, len(ts)):
        prev: pd.Timestamp = ts.iloc[i - 1]
        curr: pd.Timestamp = ts.iloc[i]
        delta: pd.Timedelta = curr - prev

        if delta <= freq:
            continue

        # Only report gaps whose endpoints fall within the trading window.
        prev_time = prev.time()
        curr_time = curr.time()
        if prev_time < t_start or curr_time > t_end:
            continue

        count_missing = int(delta / freq) - 1
        if count_missing > 0:
            gaps.append(
                {
                    "start": prev,
                    "end": curr,
                    "count_missing": count_missing,
                }
            )

    return gaps


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: str, buf_size: int = 1 << 16) -> str:
    """Compute the SHA-256 hex digest of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
