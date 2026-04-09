"""Convert raw OHLCV Parquet → NautilusTrader ``ParquetDataCatalog``.

The Market Data API and ingestion pipeline store bars as raw OHLCV Parquet
files under ``data/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet``. This
module lazily builds a Nautilus catalog at ``data/nautilus/`` from those
files so ``BacktestNode`` can read them as real ``Bar`` objects.

Idempotent: re-running against an already-populated catalog is a no-op
unless ``force=True`` is passed.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from msai.core.logging import get_logger
from msai.services.nautilus.instrument_service import ResolvedInstrumentDefinition

log = get_logger(__name__)


def build_catalog_for_instrument(
    definition: ResolvedInstrumentDefinition,
    raw_parquet_root: Path,
    catalog_root: Path,
    asset_class: str | None = None,
    force: bool = False,
) -> str:
    """Convert raw OHLCV files for an instrument into Nautilus catalog format.

    Args:
        definition: Canonical Nautilus instrument definition resolved via provider.
        raw_parquet_root: Root of raw OHLCV Parquet files (``data/parquet``).
        catalog_root: Root of the Nautilus catalog (``data/nautilus``).
        asset_class: Optional asset class override for raw Parquet lookup.
        force: If True, rewrite the catalog even if it already exists.

    Returns:
        The canonical Nautilus instrument ID string (e.g. ``"AAPL.XNAS"``).
    """
    instrument = definition.to_instrument()
    instrument_id = str(instrument.id)
    raw_symbol = definition.raw_symbol
    parquet_asset_class = asset_class or definition.asset_class

    catalog_root.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_root))

    # Find raw Parquet files for this symbol
    symbol_dir = raw_parquet_root / parquet_asset_class / raw_symbol
    parquet_files = sorted(symbol_dir.rglob("*.parquet"))
    if not parquet_files:
        for candidate_asset_class in _asset_class_aliases(parquet_asset_class):
            candidate_dir = raw_parquet_root / candidate_asset_class / raw_symbol
            parquet_files = sorted(candidate_dir.rglob("*.parquet"))
            if parquet_files:
                parquet_asset_class = candidate_asset_class
                symbol_dir = candidate_dir
                break
    if not parquet_files:
        raise FileNotFoundError(
            f"No raw Parquet files found for {raw_symbol} under {symbol_dir}"
        )

    # Skip only when the existing catalog coverage actually spans the raw
    # parquet coverage. New backfilled months can arrive after an instrument
    # already exists in the catalog, so a simple "instrument exists" check is
    # not enough for research correctness.
    raw_months_covered = _catalog_covers_raw_months(
        instrument_id=instrument_id,
        parquet_files=parquet_files,
        catalog_root=catalog_root,
    )
    instrument_matches = _catalog_instrument_matches_definition(
        definition=definition,
        catalog_root=catalog_root,
    )
    if not force and raw_months_covered and instrument_matches:
        log.info("catalog_already_populated", instrument_id=instrument_id)
        return instrument_id
    if not force and raw_months_covered and not instrument_matches:
        _rewrite_catalog_instrument(
            definition=definition,
            catalog_root=catalog_root,
        )
        log.info("catalog_instrument_refreshed", instrument_id=instrument_id)
        return instrument_id

    _purge_existing_catalog_data(instrument_id=instrument_id, catalog_root=catalog_root)
    bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)

    catalog.write_data([instrument])
    total_bars = 0

    for parquet_file in parquet_files:
        frame = pd.read_parquet(
            parquet_file,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        if frame.empty:
            continue

        df_indexed = (
            frame.assign(timestamp=pd.to_datetime(frame["timestamp"], utc=True))
            .set_index("timestamp")[["open", "high", "low", "close", "volume"]]
            .sort_index()
        )
        bars = wrangler.process(df_indexed)
        if not bars:
            continue
        catalog.write_data(bars)
        total_bars += len(bars)

    log.info(
        "catalog_built",
        instrument_id=instrument_id,
        bar_count=total_bars,
        catalog_root=str(catalog_root),
    )
    return instrument_id


def ensure_catalog_data(
    definitions: list[ResolvedInstrumentDefinition],
    raw_parquet_root: Path,
    catalog_root: Path,
    asset_class: str | None = None,
) -> list[str]:
    """Ensure Nautilus catalog contains data for all requested instruments.

    Returns the list of canonical instrument IDs (e.g. ``["AAPL.XNAS", ...]``)
    that the backtest can pass to ``BacktestDataConfig.instrument_ids``.
    """
    instrument_ids: list[str] = []
    for definition in definitions:
        instrument_id = build_catalog_for_instrument(
            definition=definition,
            raw_parquet_root=raw_parquet_root,
            catalog_root=catalog_root,
            asset_class=asset_class,
        )
        instrument_ids.append(instrument_id)
    return instrument_ids


def _asset_class_aliases(asset_class: str) -> list[str]:
    if asset_class == "stocks":
        return ["equities"]
    if asset_class == "equities":
        return ["stocks"]
    return []


def _catalog_covers_raw_months(
    *,
    instrument_id: str,
    parquet_files: list[Path],
    catalog_root: Path,
) -> bool:
    raw_months = {
        (int(file.parent.name), int(file.stem))
        for file in parquet_files
        if file.parent.name.isdigit() and file.stem.isdigit()
    }
    if not raw_months:
        return False

    bar_dir = catalog_root / "data" / "bar" / f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
    if not bar_dir.exists():
        return False

    catalog_months: set[tuple[int, int]] = set()
    for parquet_file in bar_dir.glob("*.parquet"):
        stem = parquet_file.stem
        try:
            start_raw, end_raw = stem.split("_", maxsplit=1)
            start = pd.Timestamp(pd.to_datetime(start_raw, utc=True, format="%Y-%m-%dT%H-%M-%S-%fZ")).to_pydatetime()
            end = pd.Timestamp(pd.to_datetime(end_raw, utc=True, format="%Y-%m-%dT%H-%M-%S-%fZ")).to_pydatetime()
        except ValueError:
            return False
        catalog_months.update(_month_range(start=start, end=end))

    return raw_months.issubset(catalog_months)


def _catalog_instrument_matches_definition(
    *,
    definition: ResolvedInstrumentDefinition,
    catalog_root: Path,
) -> bool:
    instrument_dir = _catalog_instrument_dir(
        instrument_id=definition.instrument_id,
        catalog_root=catalog_root,
    )
    if instrument_dir is None:
        return False

    parquet_files = sorted(instrument_dir.glob("*.parquet"))
    if not parquet_files:
        return False

    frame = pd.read_parquet(parquet_files[-1])
    if frame.empty:
        return False

    row = frame.iloc[0].to_dict()
    payload = definition.instrument_data
    keys = ("id", "raw_symbol", "activation_ns", "expiration_ns", "price_increment", "multiplier")
    for key in keys:
        if key not in payload:
            continue
        if str(row.get(key)) != str(payload.get(key)):
            return False
    return True


def _catalog_instrument_dir(*, instrument_id: str, catalog_root: Path) -> Path | None:
    data_root = catalog_root / "data"
    if not data_root.exists():
        return None
    matches = [path for path in data_root.rglob(instrument_id) if path.is_dir()]
    if not matches:
        return None
    matches.sort()
    return matches[0]


def _rewrite_catalog_instrument(
    *,
    definition: ResolvedInstrumentDefinition,
    catalog_root: Path,
) -> None:
    instrument_dir = _catalog_instrument_dir(
        instrument_id=definition.instrument_id,
        catalog_root=catalog_root,
    )
    if instrument_dir is not None and instrument_dir.exists():
        shutil.rmtree(instrument_dir)

    catalog = ParquetDataCatalog(str(catalog_root))
    catalog.write_data([definition.to_instrument()])


def _month_range(*, start: datetime, end: datetime) -> set[tuple[int, int]]:
    current_year = start.year
    current_month = start.month
    final = (end.year, end.month)
    months: set[tuple[int, int]] = set()

    while True:
        months.add((current_year, current_month))
        if (current_year, current_month) == final:
            return months
        if current_month == 12:
            current_year += 1
            current_month = 1
        else:
            current_month += 1


def _purge_existing_catalog_data(*, instrument_id: str, catalog_root: Path) -> None:
    data_root = catalog_root / "data"
    if not data_root.exists():
        return

    targets = [
        data_root / "bar" / f"{instrument_id}-1-MINUTE-LAST-EXTERNAL",
        *data_root.rglob(instrument_id),
    ]
    seen: set[Path] = set()
    for target in targets:
        resolved = target.resolve()
        if resolved in seen or not target.exists():
            continue
        seen.add(resolved)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
