"""Determinism integration test (Phase 2 task 2.11).

Runs the existing EMA cross strategy twice on the SAME synthetic
Parquet catalog via :class:`BacktestRunner`, normalizes both
``orders_df`` outputs into ``OrderIntent`` lists, and asserts the
sequences are byte-identical.

Why this is the right shape for the determinism contract:

- A strategy that uses wall-clock time, an unseeded RNG, or dict
  iteration order will emit different orders across runs even
  with identical bar input. The determinism test catches that
  by comparing two runs.
- We do NOT need a full IB-paper-replay leg to catch
  non-determinism — replaying the SAME catalog twice in the SAME
  process is sufficient and dramatically faster.
- The test is slow (~10–30 seconds) because it spawns a real
  ``BacktestNode`` subprocess. Marked ``slow`` so the regular
  test run can skip it; the Phase 2 E2E (task 2.13) runs it as
  part of the gated harness.

Skip conditions:

- The synthetic catalog setup uses Nautilus's
  ``BarDataWrangler`` + ``ParquetDataCatalog`` which import a
  large chunk of the Cython core. If the import path is broken
  on this platform we skip rather than fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.nautilus.parity.comparator import compare, is_identical
from msai.services.nautilus.parity.normalizer import normalize_orders_df

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.slow


def _write_synthetic_bars(raw_root: Path, *, symbol: str = "AAPL", rows: int = 200) -> None:
    """Write a small synthetic Parquet partition matching the
    catalog builder's expected layout. Used by the determinism
    test to spin up a Nautilus catalog without depending on
    real ingested data."""
    rng = np.random.default_rng(seed=42)
    timestamps = pd.date_range(
        start=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),  # NYSE Mon open
        periods=rows,
        freq="1min",
        tz="UTC",
    )
    closes = 100.0 + rng.standard_normal(rows).cumsum() * 0.1
    opens = closes + rng.standard_normal(rows) * 0.05
    # high/low must respect the OHLC invariant (high >= max(open, close),
    # low <= min(open, close)). Deriving them from the open+close extremes
    # plus a non-negative jitter guarantees Bar.__init__ accepts the row.
    highs = np.maximum(opens, closes) + np.abs(rng.standard_normal(rows)) * 0.1
    lows = np.minimum(opens, closes) - np.abs(rng.standard_normal(rows)) * 0.1
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.integers(100, 1000, rows).astype(np.int64),
        }
    )
    out_dir = raw_root / "stocks" / symbol / "2026"
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(df, preserve_index=False),
        out_dir / "01.parquet",
    )


def _build_catalog_or_skip(raw_root: Path, catalog_root: Path, symbol: str) -> str:
    """Build the catalog and return the canonical instrument id.
    Skips the test if Nautilus's catalog stack can't be loaded
    on this platform."""
    try:
        from msai.services.nautilus.catalog_builder import (
            build_catalog_for_symbol,
        )
    except ImportError as exc:  # pragma: no cover - environment-specific
        pytest.skip(f"Nautilus catalog stack unavailable: {exc}")
    return build_catalog_for_symbol(
        symbol=symbol,
        raw_parquet_root=raw_root,
        catalog_root=catalog_root,
    )


def _run_backtest(*, instrument_id: str, catalog_root: Path) -> pd.DataFrame:
    """Run the EMA-cross example strategy via :class:`BacktestRunner`
    and return its ``orders_df``. The runner spawns a fresh
    Nautilus subprocess each time so the two calls in the
    determinism test are guaranteed independent."""
    try:
        from msai.services.nautilus.backtest_runner import BacktestRunner
    except ImportError as exc:  # pragma: no cover - environment-specific
        pytest.skip(f"Nautilus BacktestRunner unavailable: {exc}")

    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[3]
    strategy_file = repo_root / "strategies" / "example" / "ema_cross.py"
    if not strategy_file.exists():
        pytest.skip(f"example strategy missing: {strategy_file}")

    runner = BacktestRunner()
    bar_type = f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    result = runner.run(
        strategy_file=str(strategy_file),
        strategy_config={
            "instrument_id": instrument_id,
            "bar_type": bar_type,
            "fast_ema_period": 5,
            "slow_ema_period": 20,
            "trade_size": "1",
        },
        instrument_ids=[instrument_id],
        start_date="2026-01-05",
        end_date="2026-01-06",
        catalog_path=catalog_root,
        timeout_seconds=120,
    )
    return result.orders_df


def test_ema_cross_backtest_is_deterministic(tmp_path: Path) -> None:
    """The same strategy on the same catalog must emit byte-
    identical order intents on both runs."""
    raw_root = tmp_path / "raw"
    catalog_root = tmp_path / "catalog"
    _write_synthetic_bars(raw_root, symbol="AAPL", rows=200)
    instrument_id = _build_catalog_or_skip(raw_root, catalog_root, "AAPL")

    orders_a = _run_backtest(instrument_id=instrument_id, catalog_root=catalog_root)
    orders_b = _run_backtest(instrument_id=instrument_id, catalog_root=catalog_root)

    intents_a = normalize_orders_df(orders_a)
    intents_b = normalize_orders_df(orders_b)

    divergences = compare(intents_a, intents_b)
    assert divergences == [], (
        f"backtest is non-deterministic: {len(divergences)} divergences\n"
        f"first divergence: {divergences[0] if divergences else 'none'}"
    )
    assert is_identical(intents_a, intents_b)
