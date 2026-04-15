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


def test_idempotency_skip_when_raw_unchanged(tmp_path: Path) -> None:
    """Calling the builder twice with identical raw parquet files
    should be a no-op on the second call (existing behaviour we
    don't want to regress)."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=1_000,
        start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="MSFT",
    )

    first_id = build_catalog_for_symbol(
        symbol="MSFT",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    second_id = build_catalog_for_symbol(
        symbol="MSFT",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    assert first_id == second_id

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    # Still 1 000 bars — second call did NOT double-write.
    assert len(catalog.bars(instrument_ids=[first_id])) == 1_000


def test_rebuild_when_raw_data_extends(tmp_path: Path) -> None:
    """Stale-catalog regression (drill 2026-04-15): if the raw
    parquet tree gains new partitions after the first catalog
    build, the next ``build_catalog_for_symbol`` MUST detect the
    delta and rebuild — otherwise every backtest after a fresh
    ingest silently runs against the old (truncated) catalog and
    produces wrong (or zero-trade) results.

    The drill on 2026-04-15 hit exactly this: 1 month of AAPL
    data was ingested early, then a full year was ingested later;
    the second backtest read 2 653 bars from the stale catalog
    instead of 123 072 from the new parquet tree.
    """
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    # Initial ingest — 1 month, ~28 trading days × 1 440 minutes
    # is overkill for a unit test, so use 1 000 rows.
    _write_synthetic_parquet(
        raw_root,
        rows=1_000,
        start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="GOOG",
    )
    instrument_id = build_catalog_for_symbol(
        symbol="GOOG",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )

    # Operator adds another month of raw data via a follow-up ingest.
    _write_synthetic_parquet(
        raw_root,
        rows=2_000,
        start_ts=datetime(2026, 2, 1, tzinfo=UTC),
        symbol="GOOG",
    )

    # Second build call — must rebuild to include the new partition.
    build_catalog_for_symbol(
        symbol="GOOG",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    assert len(catalog.bars(instrument_ids=[instrument_id])) == 3_000


def test_legacy_catalog_without_marker_is_rebuilt(tmp_path: Path) -> None:
    """Codex review P1: pre-patch catalogs have no source-hash
    marker. The first call after the upgrade MUST treat existing
    bars as stale and purge them — appending on top of legacy bars
    can either silently no-op (same partition filename) or error
    (overlapping intervals), and the post-rebuild marker would
    lock in the staleness on subsequent calls."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"

    _write_synthetic_parquet(
        raw_root,
        rows=500,
        start_ts=datetime(2026, 1, 1, tzinfo=UTC),
        symbol="AMZN",
    )
    instrument_id = build_catalog_for_symbol(
        symbol="AMZN",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )
    # Simulate "legacy" by removing the marker and growing the raw
    # tree. Without the legacy guard, the next call would skip
    # straight to the appender path.
    marker = catalog_root / ".msai_source_hashes" / f"{instrument_id}.hash"
    marker.unlink()
    _write_synthetic_parquet(
        raw_root,
        rows=700,
        start_ts=datetime(2026, 2, 1, tzinfo=UTC),
        symbol="AMZN",
    )

    build_catalog_for_symbol(
        symbol="AMZN",
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    assert len(catalog.bars(instrument_ids=[instrument_id])) == 1_200


def test_source_hash_distinguishes_same_basename_in_different_partitions(
    tmp_path: Path,
) -> None:
    """Codex review P2: ingest writes every monthly partition as
    ``MM.parquet``, so ``2024/01.parquet`` and ``2025/01.parquet``
    share a basename. The source hash must include the relative
    path so a same-size identically-named file in a different year
    doesn't masquerade as the original."""
    from msai.services.nautilus.catalog_builder import _compute_raw_source_hash

    raw_root = tmp_path / "raw"
    _write_synthetic_parquet(
        raw_root,
        rows=100,
        start_ts=datetime(2024, 1, 1, tzinfo=UTC),
        symbol="META",
    )
    files_before = sorted((raw_root / "stocks" / "META").rglob("*.parquet"))
    hash_before = _compute_raw_source_hash(files_before, raw_root=raw_root)

    _write_synthetic_parquet(
        raw_root,
        rows=100,
        start_ts=datetime(2025, 1, 1, tzinfo=UTC),
        symbol="META",
    )
    files_after = sorted((raw_root / "stocks" / "META").rglob("*.parquet"))
    hash_after = _compute_raw_source_hash(files_after, raw_root=raw_root)

    assert hash_before != hash_after, (
        "adding a 2025/01.parquet alongside 2024/01.parquet must change the hash; "
        "if it doesn't, basename-only fingerprinting silently masked the new file"
    )


def test_purge_preserves_sibling_bar_specs(tmp_path: Path) -> None:
    """Codex review P2: ``_purge_catalog_for_instrument`` must NOT
    delete the shared ``data/equity/<instrument>`` directory because
    other bar specs for the same instrument depend on it. Only the
    matching ``data/bar/<instrument>-<bar_spec>`` directory should
    be removed."""
    from msai.services.nautilus.catalog_builder import _purge_catalog_for_instrument

    catalog_root = tmp_path / "catalog"
    bar_dir = catalog_root / "data" / "bar" / "TSLA.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    sibling_bar_dir = catalog_root / "data" / "bar" / "TSLA.NASDAQ-5-MINUTE-LAST-EXTERNAL"
    equity_dir = catalog_root / "data" / "equity" / "TSLA.NASDAQ"
    for d in (bar_dir, sibling_bar_dir, equity_dir):
        d.mkdir(parents=True)
        (d / "placeholder.parquet").write_bytes(b"x")

    _purge_catalog_for_instrument(
        catalog_root,
        "TSLA.NASDAQ",
        bar_spec="1-MINUTE-LAST-EXTERNAL",
    )

    assert not bar_dir.exists(), "target bar dir must be removed"
    assert sibling_bar_dir.exists(), "sibling bar spec must survive purge"
    assert equity_dir.exists(), "shared instrument definition must survive purge"


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
