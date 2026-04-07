"""Convert raw OHLCV Parquet → NautilusTrader ``ParquetDataCatalog``.

The Market Data API and ingestion pipeline store bars as raw OHLCV Parquet
files under ``data/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet``. This
module lazily builds a Nautilus catalog at ``data/nautilus/`` from those
files so ``BacktestNode`` can read them as real ``Bar`` objects.

Idempotent: re-running against an already-populated catalog is a no-op
unless ``force=True`` is passed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from msai.core.logging import get_logger
from msai.services.nautilus.instruments import resolve_instrument

log = get_logger(__name__)


def build_catalog_for_symbol(
    symbol: str,
    raw_parquet_root: Path,
    catalog_root: Path,
    asset_class: str = "stocks",
    force: bool = False,
) -> str:
    """Convert raw OHLCV files for ``symbol`` into Nautilus catalog format.

    Args:
        symbol: Ticker symbol (e.g. ``"AAPL"``) or Nautilus ID (``"AAPL.XNAS"``).
        raw_parquet_root: Root of raw OHLCV Parquet files (``data/parquet``).
        catalog_root: Root of the Nautilus catalog (``data/nautilus``).
        asset_class: Asset class subdirectory name (default ``stocks``).
        force: If True, rewrite the catalog even if it already exists.

    Returns:
        The canonical Nautilus instrument ID string (e.g. ``"AAPL.XNAS"``).
    """
    instrument = resolve_instrument(symbol)
    instrument_id = str(instrument.id)
    raw_symbol = instrument.raw_symbol.value

    catalog_root.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_root))

    # Skip if we already have bars for this instrument (unless forced)
    if not force:
        existing = catalog.bars(instrument_ids=[instrument_id])
        if existing:
            log.info("catalog_already_populated", instrument_id=instrument_id, bars=len(existing))
            return instrument_id

    # Find raw Parquet files for this symbol
    symbol_dir = raw_parquet_root / asset_class / raw_symbol
    parquet_files = sorted(symbol_dir.rglob("*.parquet"))
    if not parquet_files:
        raise FileNotFoundError(
            f"No raw Parquet files found for {raw_symbol} under {symbol_dir}"
        )

    # Load and concatenate all raw files
    frames = [pd.read_parquet(f) for f in parquet_files]
    df = pd.concat(frames, ignore_index=True)

    # Normalize timestamp column to UTC DatetimeIndex with required columns only
    df_indexed = (
        df.assign(timestamp=pd.to_datetime(df["timestamp"], utc=True))
        .set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        .sort_index()
    )

    bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)
    bars = wrangler.process(df_indexed)

    catalog.write_data([instrument])
    catalog.write_data(bars)

    log.info(
        "catalog_built",
        instrument_id=instrument_id,
        bar_count=len(bars),
        catalog_root=str(catalog_root),
    )
    return instrument_id


def ensure_catalog_data(
    symbols: list[str],
    raw_parquet_root: Path,
    catalog_root: Path,
    asset_class: str = "stocks",
) -> list[str]:
    """Ensure Nautilus catalog contains data for all requested symbols.

    Returns the list of canonical instrument IDs (e.g. ``["AAPL.XNAS", ...]``)
    that the backtest can pass to ``BacktestDataConfig.instrument_ids``.
    """
    instrument_ids: list[str] = []
    for symbol in symbols:
        instrument_id = build_catalog_for_symbol(
            symbol=symbol,
            raw_parquet_root=raw_parquet_root,
            catalog_root=catalog_root,
            asset_class=asset_class,
        )
        instrument_ids.append(instrument_id)
    return instrument_ids
