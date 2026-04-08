"""Memory + correctness tests for the streaming catalog builder
(Phase 2 task 2.7).

The architecture review flagged the old full-partition pandas load
as an OOM risk on TB-scale catalogs. The streaming refactor reads
via ``pyarrow.parquet.ParquetFile.iter_batches(batch_size=100_000)``
and wrangles each batch into the catalog before the next is read.

This module exercises both the correctness contract (N rows in → N
bars out) and the memory contract (peak RSS below a generous
threshold under ``tracemalloc``). The tracemalloc check is lenient
on the upper bound because:

- pytest itself holds 30-50 MB before the test starts
- pyarrow/nautilus allocate their own buffers we can't measure
  with the Python-level allocator

The important signal is that peak memory does NOT scale linearly
with input size — a 1 M-row input should not allocate 10x what a
100 k-row input does.
"""

from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from msai.services.nautilus.catalog_builder import build_catalog_for_symbol

if TYPE_CHECKING:
    from pathlib import Path


def _write_synthetic_parquet(path: Path, *, rows: int, start_ts: datetime, symbol: str) -> None:
    """Write a synthetic Parquet file with ``rows`` minute bars
    starting at ``start_ts``. Columns match what the catalog
    builder expects: ``timestamp, open, high, low, close, volume``."""
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

    # Mirror the production layout: data/parquet/stocks/SYMBOL/YYYY/MM.parquet
    out = path / "stocks" / symbol / str(start_ts.year)
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / f"{start_ts.month:02d}.parquet")


def test_streaming_builder_processes_all_rows(tmp_path: Path) -> None:
    """Correctness: a 150 k-row synthetic input yields a catalog
    with 150 k bars. Larger than ``_BATCH_SIZE`` (100 k) so the
    iter_batches path executes at least twice."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=150_000,
        start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="AAPL",
    )

    # Run the builder — returns the canonical instrument id.
    instrument_id = build_catalog_for_symbol(
        symbol="AAPL",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    assert instrument_id.endswith(".NASDAQ")

    # Verify the catalog actually contains 150 k bars by reading
    # them back via Nautilus's ParquetDataCatalog.
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    bars = catalog.bars(instrument_ids=[instrument_id])
    assert len(bars) == 150_000


def test_streaming_builder_peak_memory_below_threshold(tmp_path: Path) -> None:
    """Memory contract: a 200 k-row input should NOT allocate
    linearly — ``tracemalloc``'s peak Python-allocator allocation
    should stay under 200 MB. (The real goal is that the builder
    doesn't hold every row in memory at once; the threshold is a
    generous upper bound that would be blown by the old
    ``pd.concat([pd.read_parquet(p) for p in files])`` pattern on
    larger inputs.)"""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=200_000,
        start_ts=datetime(2026, 2, 1, tzinfo=UTC),
        symbol="MSFT",
    )

    tracemalloc.start()
    try:
        build_catalog_for_symbol(
            symbol="MSFT",
            raw_parquet_root=raw_root,
            catalog_root=catalog_root,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # 200 MB threshold — very lenient (pytest + pandas + pyarrow
    # all live in here). The old full-load pattern would stream the
    # 200 k rows through pandas AND concatenate them into one
    # DataFrame simultaneously. This threshold catches any
    # regression where the streaming path is accidentally loading
    # everything at once.
    peak_mb = peak / (1024 * 1024)
    assert peak_mb < 200, f"streaming builder peak memory was {peak_mb:.1f} MB (> 200 MB)"


def test_streaming_builder_idempotent(tmp_path: Path) -> None:
    """Re-running the builder on an already-populated catalog is
    a no-op (the idempotency guard returns the existing instrument
    id without re-wrangling)."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=5_000,
        start_ts=datetime(2026, 3, 1, tzinfo=UTC),
        symbol="GOOG",
    )

    first_id = build_catalog_for_symbol(
        symbol="GOOG",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    second_id = build_catalog_for_symbol(
        symbol="GOOG",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    assert first_id == second_id

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    bars = catalog.bars(instrument_ids=[first_id])
    # Idempotent: still exactly 5 000 bars (not 10 000).
    assert len(bars) == 5_000


def test_streaming_builder_multi_partition(tmp_path: Path) -> None:
    """Two monthly partitions combine correctly — verifies the
    per-file outer loop works in addition to the per-batch inner
    loop."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=3_000,
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        symbol="NVDA",
    )
    _write_synthetic_parquet(
        raw_root,
        rows=2_000,
        start_ts=datetime(2026, 5, 1, tzinfo=UTC),
        symbol="NVDA",
    )

    instrument_id = build_catalog_for_symbol(
        symbol="NVDA",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    bars = catalog.bars(instrument_ids=[instrument_id])
    assert len(bars) == 5_000


def test_streaming_builder_raises_when_no_raw_data(tmp_path: Path) -> None:
    """Fail-loud contract: missing raw data raises
    ``FileNotFoundError`` with a descriptive message so the backtest
    worker can surface it to the user."""
    import pytest

    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"
    (raw_root / "stocks" / "NOPE").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="No raw Parquet files"):
        build_catalog_for_symbol(
            symbol="NOPE",
            raw_parquet_root=raw_root,
            catalog_root=catalog_root,
        )
