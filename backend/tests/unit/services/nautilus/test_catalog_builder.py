"""Unit tests for ``catalog_builder.verify_catalog_coverage`` (Task B5).

The helper wraps ``ParquetDataCatalog.get_missing_intervals_for_request``
to report per-instrument ``(start_ns, end_ns)`` gaps against a requested
date range. It's used by the auto-heal orchestrator to verify that
ingest actually produced usable data before retrying a backtest.

These tests cover:

1. Empty-catalog → one gap spanning the full requested range. This is the
   contract the auto-heal orchestrator relies on to decide "go ingest".
2. Full-coverage regression for the iter-2 P2-b fix — the previous
   end-of-day truncation (``23:59:59``) introduced a spurious 1-second
   gap at the tail because Nautilus writes bars in ns granularity. The
   fix is ``(end + 1 day) * 1e9 - 1``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from msai.services.nautilus.catalog_builder import (
    build_catalog_for_symbol,
    verify_catalog_coverage,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_synthetic_parquet(
    path: Path,
    *,
    rows: int,
    start_ts: datetime,
    symbol: str,
    asset_class: str = "stocks",
) -> None:
    """Write a synthetic Parquet file with ``rows`` minute bars starting at
    ``start_ts``. Mirrors the production layout
    ``{path}/{asset_class}/{symbol}/YYYY/MM.parquet`` so the standard
    ``build_catalog_for_symbol`` ingest path can consume it.
    """
    rng = np.random.default_rng(42)
    timestamps = pd.date_range(start=start_ts, periods=rows, freq="1min", tz="UTC")
    closes = 100.0 + rng.standard_normal(rows).cumsum() * 0.1
    opens = closes + rng.standard_normal(rows) * 0.05
    highs = np.maximum(opens, closes) + np.abs(rng.standard_normal(rows)) * 0.1
    lows = np.minimum(opens, closes) - np.abs(rng.standard_normal(rows)) * 0.1
    volumes = rng.integers(100, 10_000, rows).astype(np.int64)

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )
    table = pa.Table.from_pandas(df, preserve_index=False)

    out = path / asset_class / symbol / str(start_ts.year)
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"{start_ts.month:02d}.parquet")


def test_verify_catalog_coverage_empty_catalog_returns_full_gap(tmp_path: Path) -> None:
    """An instrument with no catalog data → one gap == full requested range."""
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir(parents=True)

    start = date(2024, 1, 1)
    end = date(2024, 12, 31)

    gaps = verify_catalog_coverage(
        catalog_root=catalog_root,
        instrument_ids=["AAPL.NASDAQ"],
        bar_spec="1-MINUTE-LAST-EXTERNAL",
        start=start,
        end=end,
    )

    assert len(gaps) == 1
    instrument_id, intervals = gaps[0]
    assert instrument_id == "AAPL.NASDAQ"
    assert len(intervals) == 1
    gap_start_ns, gap_end_ns = intervals[0]

    expected_start_ns = int(
        datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1e9
    )
    # end_ns is end-of-day exclusive minus 1 ns (iter-2 P2-b fix).
    expected_end_ns = (
        int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1e9)
        + 86_400 * 1_000_000_000
        - 1
    )
    assert gap_start_ns == expected_start_ns
    assert gap_end_ns == expected_end_ns


def test_verify_catalog_coverage_end_date_ns_precision_no_off_by_one(tmp_path: Path) -> None:
    """Iter-2 P2-b regression test: a catalog that fully covers the
    requested ``[start, end]`` range (inclusive) must return zero gaps.

    Pre-fix, ``end_ns`` was computed as ``datetime(end, 23, 59, 59) * 1e9``
    which left a 1-second tail gap because Nautilus bars are
    ns-granular and the last bar of the day stamps at ``23:59:00``
    with a close-time after ``23:59:59.000000000``. The fix is
    ``(end + 1 day) * 1e9 - 1``.
    """
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    # Write one bar per minute from 2026-01-01 00:00 through 2026-02-01
    # 00:00 inclusive (31 * 24 * 60 + 1 = 44 641 bars). The wrangler
    # stamps each bar's ``ts_event`` at the START of its minute (see
    # ``BarDataWrangler.process``), so the catalog's recorded coverage
    # window is ``[2026-01-01 00:00:00, 2026-02-01 00:00:00]`` — i.e.,
    # every ns of January 2026 is covered, ending exactly at the
    # ``end + 1 day`` boundary used by the P2-b formula.
    _write_synthetic_parquet(
        raw_root,
        rows=31 * 24 * 60 + 1,
        start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="AAPL",
    )

    instrument_id = build_catalog_for_symbol(
        symbol="AAPL",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )

    # Request the EXACT coverage window. Pre-fix this would return a
    # 1-second gap at the tail.
    gaps = verify_catalog_coverage(
        catalog_root=catalog_root,
        instrument_ids=[instrument_id],
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )

    assert len(gaps) == 1
    returned_id, intervals = gaps[0]
    assert returned_id == instrument_id
    assert intervals == [], (
        f"expected zero gaps for full-coverage catalog, got {intervals!r} — "
        "likely an off-by-one at the end-of-day ns boundary"
    )
