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
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nautilus_trader.persistence import ParquetDataCatalog

from msai.services.nautilus.catalog_builder import (
    build_catalog_for_symbol,
    catalog_has_bars_in_window,
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


def test_native_catalog_exact_values_times_and_single_date(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    path = raw_root / "stocks/AAPL/2025/01.parquet"
    path.parent.mkdir(parents=True)
    times = pd.to_datetime([
        "2025-01-02T23:59:00.000000123Z", "2025-01-02T00:00:00.000000123Z",
    ])
    frame = pd.DataFrame({
        "timestamp": times, "open": [Decimal("100.01"), Decimal("99.98")],
        "high": [Decimal("100.05"), Decimal("100.00")],
        "low": [Decimal("99.99"), Decimal("99.95")],
        "close": [Decimal("100.02"), Decimal("99.99")], "volume": [22, 11],
    })
    pq.write_table(pa.Table.from_pandas(frame), path)
    catalog_root = tmp_path / "nautilus-v2"
    iid = build_catalog_for_symbol("AAPL.NASDAQ", raw_root, catalog_root)
    catalog = ParquetDataCatalog(str(catalog_root))
    bars = catalog.query_bars(identifiers=[f"{iid}-1-MINUTE-LAST-EXTERNAL"])
    assert len(bars) == 2
    assert [bar.ts_event for bar in bars] == sorted(int(t.value) for t in times)
    assert [bar.ts_init for bar in bars] == [bar.ts_event for bar in bars]
    assert [str(bar.open) for bar in bars] == ["99.98", "100.01"]
    assert [str(bar.high) for bar in bars] == ["100.00", "100.05"]
    assert [str(bar.low) for bar in bars] == ["99.95", "99.99"]
    assert [str(bar.close) for bar in bars] == ["99.99", "100.02"]
    assert [str(bar.volume) for bar in bars] == ["11", "22"]
    equity = catalog.instruments()[0]
    assert str(equity.quote_currency) == "USD"
    assert str(equity.price_increment) == "0.01"
    assert equity.price_precision == 2
    assert equity.size_precision == 0
    assert catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=iid,
        start=date(2025, 1, 2), end=date(2025, 1, 2),
    )
    assert not catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=iid,
        start=date(2025, 1, 3), end=date(2025, 1, 3),
    )


def test_native_arrow_schema_matches_rc6_nonnullable_contract():
    from msai.services.nautilus.catalog_builder import _native_bar_batch

    batch = pa.RecordBatch.from_pydict({
        "timestamp": [datetime(2025, 1, 2, tzinfo=UTC)],
        "open": [100.01], "high": [100.03], "low": [99.99], "close": [100.02], "volume": [3],
    })
    native = _native_bar_batch(batch, price_precision=2)
    assert native.schema.names == ["open", "high", "low", "close", "volume", "ts_event", "ts_init"]
    assert all(not field.nullable for field in native.schema)
    assert all(native.schema.field(name).type == pa.decimal128(38, 16) for name in [
        "open", "high", "low", "close", "volume",
    ])
    assert native.schema.field("ts_event").type == pa.timestamp("ns", tz="UTC")
    assert native.column("close")[0].as_py() == Decimal("100.02")


@pytest.mark.parametrize("asset,symbol", [("futures", "ES.CME"), ("options", "AAPL.OPRA")])
def test_unsupported_asset_refused_before_catalog_mutation(tmp_path, asset, symbol):
    catalog_root = tmp_path / "catalog"
    with pytest.raises(ValueError, match="minute equities"):
        build_catalog_for_symbol(symbol, tmp_path / "raw", catalog_root, asset_class=asset)
    assert not catalog_root.exists()


def test_unsupported_interval_and_reversed_window_refused(tmp_path):
    with pytest.raises(ValueError, match="1-MINUTE"):
        catalog_has_bars_in_window(
            catalog_root=tmp_path / "catalog", instrument_id="AAPL.NASDAQ",
            start=date(2025, 1, 1), end=date(2025, 1, 1), bar_spec="5-MINUTE-LAST-EXTERNAL",
        )
    with pytest.raises(ValueError, match="start"):
        verify_catalog_coverage(
            catalog_root=tmp_path / "catalog", instrument_ids=["AAPL.NASDAQ"],
            start=date(2025, 1, 2), end=date(2025, 1, 1),
        )


@pytest.mark.parametrize("broken", [
    "null", "nan", "negative_volume", "fractional_volume", "inverted_ohlc",
    "corrupt", "missing_column",
])
def test_corrupt_inputs_refuse_without_success_marker(tmp_path, broken):
    root = tmp_path / "raw"
    path = root / "stocks/AAPL/2025/01.parquet"
    path.parent.mkdir(parents=True)
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-02T09:30:00Z"]),
        "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1],
    })
    if broken == "corrupt":
        path.write_bytes(b"not parquet")
    else:
        if broken == "null":
            frame.loc[0, "open"] = None
        elif broken == "nan":
            frame.loc[0, "volume"] = float("nan")
        elif broken == "negative_volume":
            frame.loc[0, "volume"] = -1
        elif broken == "fractional_volume":
            frame["volume"] = [1.5]
        elif broken == "inverted_ohlc":
            frame["high"] = [99.0]
        elif broken == "missing_column":
            frame = frame.drop(columns="high")
        pq.write_table(pa.Table.from_pandas(frame), path)
    catalog = tmp_path / "nautilus-v2"
    with pytest.raises(ValueError, match="OHLCV|Parquet"):
        build_catalog_for_symbol("AAPL", root, catalog)
    assert not (catalog / ".msai_source_hashes/AAPL.NASDAQ.hash").exists()


def test_default_catalog_namespace_and_unsupported_native_instrument():
    from msai.core.config import Settings
    from msai.services.nautilus.instruments import resolve_instrument

    config = Settings(_env_file=None, data_root="/tmp/research")
    assert config.nautilus_catalog_root.name == "nautilus-v2"
    with pytest.raises(ValueError, match="minute equities"):
        resolve_instrument("ESM6.CME")
    assert str(resolve_instrument("BRK.B.NYSE").id) == "BRK.B.NYSE"


def test_reserved_v1_catalog_root_refused_before_mutation(tmp_path):
    baseline = tmp_path / "nautilus"
    baseline.mkdir()
    saved = baseline / "preserved.parquet"
    saved.write_bytes(b"V1 baseline")
    with pytest.raises(ValueError, match="preserved V1"):
        build_catalog_for_symbol("AAPL", tmp_path / "raw", baseline, force=True)
    assert saved.read_bytes() == b"V1 baseline"
    assert list(baseline.iterdir()) == [saved]


def test_changed_source_purge_failure_invalidates_previous_marker(tmp_path, monkeypatch):
    from msai.services.nautilus import catalog_builder

    raw, catalog = tmp_path / "raw", tmp_path / "nautilus-v2"
    _write_synthetic_parquet(raw, rows=3, start_ts=datetime(2025, 1, 2, tzinfo=UTC), symbol="AAPL")
    iid = build_catalog_for_symbol("AAPL", raw, catalog)
    marker = catalog / ".msai_source_hashes" / f"{iid}.hash"
    assert marker.exists()
    _write_synthetic_parquet(raw, rows=4, start_ts=datetime(2025, 1, 2, tzinfo=UTC), symbol="AAPL")

    def failed_purge(*args, **kwargs):
        raise OSError("interrupted native purge")

    monkeypatch.setattr(catalog_builder, "_purge_catalog_for_instrument", failed_purge)
    with pytest.raises(OSError, match="interrupted"):
        build_catalog_for_symbol("AAPL", raw, catalog)
    assert not marker.exists()


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


def test_catalog_has_bars_in_window_true_for_overlapping_data(tmp_path: Path) -> None:
    """A catalog built from Dec-2024 parquet reports bars present for the
    Dec-2024 window — the binary signal Fix #1 relies on (robust to the
    weekend/holiday edge-gaps verify_catalog_coverage would report)."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"
    _write_synthetic_parquet(
        raw_root, rows=2 * 24 * 60, start_ts=datetime(2024, 12, 2, tzinfo=UTC), symbol="AAPL"
    )
    iid = build_catalog_for_symbol(
        symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root
    )
    assert (
        catalog_has_bars_in_window(
            catalog_root=catalog_root, instrument_id=iid,
            start=date(2024, 12, 1), end=date(2024, 12, 31),
        )
        is True
    )


def test_catalog_has_bars_in_window_false_when_no_overlap(tmp_path: Path) -> None:
    """A different year's window has no overlapping bars → False (not a
    spurious True from weekend edge handling)."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"
    _write_synthetic_parquet(
        raw_root, rows=2 * 24 * 60, start_ts=datetime(2024, 12, 2, tzinfo=UTC), symbol="AAPL"
    )
    iid = build_catalog_for_symbol(
        symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root
    )
    assert (
        catalog_has_bars_in_window(
            catalog_root=catalog_root, instrument_id=iid,
            start=date(2023, 1, 1), end=date(2023, 1, 31),
        )
        is False
    )


def test_catalog_has_bars_in_window_false_when_catalog_missing(tmp_path: Path) -> None:
    """No catalog dir at all → False (the cold/empty prod case), not a crash."""
    assert (
        catalog_has_bars_in_window(
            catalog_root=tmp_path / "catalog", instrument_id="AAPL.NASDAQ",
            start=date(2024, 12, 1), end=date(2024, 12, 31),
        )
        is False
    )


def test_force_rebuild_replaces_stale_bars(tmp_path: Path) -> None:
    """force=True must PURGE then rebuild: after building from window A then
    force-rebuilding from parquet covering only window B, the catalog holds B
    and NOT A. Guards the confirmed stale-catalog root cause."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"
    # Build from window A (Jan 2024).
    _write_synthetic_parquet(
        raw_root, rows=5 * 24 * 60, start_ts=datetime(2024, 1, 2, tzinfo=UTC), symbol="AAPL"
    )
    iid = build_catalog_for_symbol(
        symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root
    )
    assert catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=iid,
        start=date(2024, 1, 1), end=date(2024, 1, 31),
    )
    # Replace the raw parquet with only window B (Mar 2024) and force-rebuild.
    import shutil

    shutil.rmtree(raw_root / "stocks" / "AAPL")
    _write_synthetic_parquet(
        raw_root, rows=5 * 24 * 60, start_ts=datetime(2024, 3, 4, tzinfo=UTC), symbol="AAPL"
    )
    build_catalog_for_symbol(
        symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root, force=True
    )
    # B present, A purged.
    assert catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=iid,
        start=date(2024, 3, 1), end=date(2024, 3, 31),
    )
    assert not catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=iid,
        start=date(2024, 1, 1), end=date(2024, 1, 31),
    )
