"""Integration tests for the catalog-migration script
(Phase 2 task 2.8).

The script lives at ``scripts/migrate_catalog_to_canonical.py`` (user-invokable
scripts go under the top-level ``scripts/``, not ``backend/scripts/``). We
import its ``run`` function directly rather than shelling out so the test
can use pytest's ``tmp_path`` fixture and stay fast.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# The script lives OUTSIDE the backend package so pytest's default
# sys.path doesn't see it. Insert its parent before importing.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from migrate_catalog_to_canonical import (  # noqa: E402
    discover_symbols,
    run,
)


def _write_synthetic_monthly_parquet(
    data_root: Path,
    *,
    symbol: str,
    asset_class: str,
    year: int,
    month: int,
    rows: int,
) -> None:
    """Write one synthetic monthly Parquet partition in the
    production layout ``{data_root}/parquet/{asset_class}/{symbol}/{year}/{month:02d}.parquet``."""
    start = datetime(year, month, 1, tzinfo=UTC)
    rng = np.random.default_rng(hash((symbol, year, month)) & 0xFFFFFFFF)
    timestamps = pd.date_range(start=start, periods=rows, freq="1min", tz="UTC")
    closes = 100.0 + rng.standard_normal(rows).cumsum() * 0.1
    opens = closes + rng.standard_normal(rows) * 0.05

    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": np.maximum(opens, closes) + 0.1,
            "low": np.minimum(opens, closes) - 0.1,
            "close": closes,
            "volume": rng.integers(100, 10_000, rows).astype(np.int64),
        }
    )
    out_dir = data_root / "parquet" / asset_class / symbol / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(df, preserve_index=False),
        out_dir / f"{month:02d}.parquet",
    )


# ---------------------------------------------------------------------------
# discover_symbols
# ---------------------------------------------------------------------------


class TestDiscoverSymbols:
    def test_discovers_symbol_directories(self, tmp_path: Path) -> None:
        """Returns a sorted list of every subdirectory under
        ``{raw_parquet_root}/{asset_class}/``."""
        for sym in ["MSFT", "AAPL", "GOOG"]:
            (tmp_path / "stocks" / sym).mkdir(parents=True)
        # Hidden dirs + files must be ignored.
        (tmp_path / "stocks" / ".DS_Store").mkdir()
        (tmp_path / "stocks" / "a_file.parquet").touch()

        result = discover_symbols(tmp_path, "stocks")
        assert result == ["AAPL", "GOOG", "MSFT"]  # sorted

    def test_empty_when_asset_class_missing(self, tmp_path: Path) -> None:
        assert discover_symbols(tmp_path, "stocks") == []


# ---------------------------------------------------------------------------
# run() — full migration flow
# ---------------------------------------------------------------------------


def test_migrate_single_symbol_full_cycle(tmp_path: Path) -> None:
    """A single symbol with one monthly partition migrates
    successfully → the Nautilus catalog contains the expected
    canonical id."""
    _write_synthetic_monthly_parquet(
        tmp_path,
        symbol="AAPL",
        asset_class="stocks",
        year=2026,
        month=1,
        rows=100,
    )

    migrated, skipped = run(data_root=tmp_path, asset_class="stocks")
    assert migrated == 1
    assert skipped == 0

    # The catalog should exist under data_root/nautilus.
    catalog_root = tmp_path / "nautilus"
    assert catalog_root.exists()

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    bars = catalog.bars(instrument_ids=["AAPL.NASDAQ"])
    assert len(bars) == 100


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Re-running the script after a successful migration is a
    no-op (the catalog builder's idempotency guard detects the
    existing bars and skips rebuilding). Output counts the row
    as migrated both times because the script doesn't
    distinguish first-run from replay — the guard lives in
    ``build_catalog_for_symbol``."""
    _write_synthetic_monthly_parquet(
        tmp_path,
        symbol="AAPL",
        asset_class="stocks",
        year=2026,
        month=1,
        rows=50,
    )

    first = run(data_root=tmp_path, asset_class="stocks")
    second = run(data_root=tmp_path, asset_class="stocks")

    assert first == (1, 0)
    assert second == (1, 0)

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(tmp_path / "nautilus"))
    bars = catalog.bars(instrument_ids=["AAPL.NASDAQ"])
    # Still exactly 50 bars — not 100 — because the second run's
    # idempotency guard bailed before re-wrangling.
    assert len(bars) == 50


def test_migrate_skips_symbols_with_no_raw_files(tmp_path: Path) -> None:
    """A symbol directory with no Parquet files inside → the
    builder raises ``FileNotFoundError`` → the script logs and
    counts it as skipped, moving on to the next symbol."""
    # AAPL has data, MSFT is empty.
    _write_synthetic_monthly_parquet(
        tmp_path,
        symbol="AAPL",
        asset_class="stocks",
        year=2026,
        month=1,
        rows=30,
    )
    (tmp_path / "parquet" / "stocks" / "MSFT").mkdir(parents=True)

    migrated, skipped = run(data_root=tmp_path, asset_class="stocks")
    assert migrated == 1
    assert skipped == 1

    # AAPL made it into the catalog; MSFT didn't.
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(tmp_path / "nautilus"))
    assert len(catalog.bars(instrument_ids=["AAPL.NASDAQ"])) == 30
    assert catalog.bars(instrument_ids=["MSFT.NASDAQ"]) == []


def test_migrate_empty_parquet_root_is_noop(tmp_path: Path) -> None:
    """If the parquet tree doesn't exist at all, the script
    returns (0, 0) without raising."""
    migrated, skipped = run(data_root=tmp_path, asset_class="stocks")
    assert migrated == 0
    assert skipped == 0


def test_migrate_multiple_symbols(tmp_path: Path) -> None:
    """Batch migration: three symbols, each with a small partition."""
    for sym in ["AAPL", "MSFT", "GOOG"]:
        _write_synthetic_monthly_parquet(
            tmp_path,
            symbol=sym,
            asset_class="stocks",
            year=2026,
            month=2,
            rows=20,
        )

    migrated, skipped = run(data_root=tmp_path, asset_class="stocks")
    assert migrated == 3
    assert skipped == 0

    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(tmp_path / "nautilus"))
    for sym in ["AAPL", "MSFT", "GOOG"]:
        bars = catalog.bars(instrument_ids=[f"{sym}.NASDAQ"])
        assert len(bars) == 20
