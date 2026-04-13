"""Lazy converter from raw OHLCV Parquet files to a NautilusTrader catalog.

The MSAI market-data ingestion pipeline writes minute-level OHLCV data as
plain Parquet files under::

    {data_root}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet

NautilusTrader's ``BacktestNode`` cannot read those files directly -- it
expects a :class:`~nautilus_trader.persistence.catalog.ParquetDataCatalog`
containing fully-formed ``Bar`` + ``Instrument`` objects laid out in a very
specific directory structure.  This module bridges the two formats on demand.

Design goals
------------
* **Lazy** -- the catalog is built the first time a given symbol is requested,
  not up-front.  This keeps dev boxes with only a handful of symbols happy
  while still scaling to hundreds of tickers in production.
* **Idempotent** -- re-running a backtest that already has catalog data for
  its instrument is a no-op unless ``force=True`` is passed.  This matters
  because the frontend aggressively re-runs backtests during iteration.
* **Fail-loud** -- if there is no raw Parquet data for a symbol we raise a
  clear :class:`FileNotFoundError` so the backtest worker can surface the
  problem to the user instead of silently producing an empty result.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow.parquet as pq
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from msai.core.logging import get_logger
from msai.services.nautilus.instruments import resolve_instrument

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# Canonical minute-bar type written to the catalog.  All strategies in MSAI
# v2 operate on minute bars, so we hard-wire this here.  If we ever need to
# support multiple aggregation intervals we'll make it a parameter.
_BAR_SPEC = "1-MINUTE-LAST-EXTERNAL"

# Columns the BarDataWrangler expects on the input DataFrame.
_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

# Batch size for streaming pyarrow reads (Phase 2 task 2.7). 100 000
# rows is a compromise between wrangler-call overhead (fewer batches
# is faster) and peak RSS (smaller batches keep memory flat). For a
# 1 M-row input this caps the in-memory working set at ~6 MB per
# column × 5 OHLCV cols ≈ 30 MB per batch plus the catalog's own
# pyarrow buffers — well under the 200 MB target the plan calls for.
_BATCH_SIZE = 100_000


def build_catalog_for_symbol(
    symbol: str,
    raw_parquet_root: Path,
    catalog_root: Path,
    *,
    asset_class: str = "stocks",
    force: bool = False,
) -> str:
    """Convert raw OHLCV files for a single symbol into Nautilus catalog format.

    Reads every Parquet file under
    ``{raw_parquet_root}/{asset_class}/{symbol}/**/*.parquet``, concatenates
    them into a single DataFrame, normalises the index to a UTC
    ``DatetimeIndex``, runs :class:`BarDataWrangler` to produce ``Bar``
    objects, and writes both the instrument definition and the bars to the
    ``ParquetDataCatalog`` rooted at ``catalog_root``.

    Args:
        symbol: Ticker symbol (``"AAPL"``) or a Nautilus ID (``"AAPL.SIM"``).
            The venue suffix -- if present -- is stripped and re-bound to
            ``SIM`` by :func:`resolve_instrument`.
        raw_parquet_root: Root of the raw OHLCV Parquet tree (typically
            ``settings.parquet_root``).
        catalog_root: Root of the Nautilus catalog (typically
            ``settings.nautilus_catalog_root``).  Created on demand.
        asset_class: Asset-class sub-directory name under
            ``raw_parquet_root``.  Defaults to ``"stocks"``.
        force: When ``True``, rebuild the catalog entries for this symbol
            even if bars already exist.  Used to refresh stale data.

    Returns:
        The canonical Nautilus instrument ID string
        (e.g. ``"AAPL.SIM"``) that callers should pass to
        ``BacktestDataConfig.instrument_ids``.

    Raises:
        FileNotFoundError: No raw Parquet files exist for the requested
            symbol under ``{raw_parquet_root}/{asset_class}/{symbol}``.
    """
    instrument = resolve_instrument(symbol)
    instrument_id_str = str(instrument.id)
    raw_symbol = instrument.raw_symbol.value

    catalog_root.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_root))

    # Idempotency guard: bail out early if the catalog already contains bars
    # for this instrument.  The frontend re-triggers backtests on every
    # config tweak and re-converting Parquet each time is wasteful.
    if not force:
        existing_bars = catalog.bars(instrument_ids=[instrument_id_str])
        if existing_bars:
            log.info(
                "nautilus_catalog_already_populated",
                instrument_id=instrument_id_str,
                bar_count=len(existing_bars),
            )
            return instrument_id_str

    # Locate raw Parquet files.  We recurse because the ingestion pipeline
    # partitions by YYYY/MM and we want every partition in one sweep.
    symbol_dir = raw_parquet_root / asset_class / raw_symbol
    raw_files = sorted(symbol_dir.rglob("*.parquet"))
    if not raw_files:
        raise FileNotFoundError(
            f"No raw Parquet files found for {raw_symbol!r} under {symbol_dir}. "
            "Run the data ingestion pipeline for this symbol before backtesting."
        )

    # Phase 2 task 2.7: streaming read via ``pyarrow.parquet.iter_batches``
    # instead of slurping every partition into memory as a pandas
    # DataFrame. The old ``pd.concat([pd.read_parquet(p) for p in
    # raw_files])`` pattern OOMed on TB-scale catalogs (Codex
    # architecture review finding #6). Each batch is converted to
    # pandas individually, wrangled, and appended to the catalog
    # before the next batch is read — peak RSS is bounded by
    # ``_BATCH_SIZE × row_width × column_count`` plus pyarrow's own
    # buffers.
    bar_type = BarType.from_str(f"{instrument_id_str}-{_BAR_SPEC}")
    wrangler = BarDataWrangler(bar_type=bar_type, instrument=instrument)

    # Order matters: the instrument must be written BEFORE any bars
    # so the catalog indexes resolve correctly when BacktestNode
    # starts up.
    catalog.write_data([instrument])

    total_bars = 0
    columns_to_read = ["timestamp", *_OHLCV_COLUMNS]
    for raw_file in raw_files:
        parquet_file = pq.ParquetFile(raw_file)
        for record_batch in parquet_file.iter_batches(
            batch_size=_BATCH_SIZE,
            columns=columns_to_read,
        ):
            batch_df = record_batch.to_pandas()
            if batch_df.empty:
                continue
            indexed_df = (
                batch_df.assign(timestamp=pd.to_datetime(batch_df["timestamp"], utc=True))
                .set_index("timestamp")[list(_OHLCV_COLUMNS)]
                .sort_index()
            )
            bars = wrangler.process(indexed_df)
            catalog.write_data(bars)
            total_bars += len(bars)

    log.info(
        "nautilus_catalog_built",
        instrument_id=instrument_id_str,
        bar_count=total_bars,
        partitions=len(raw_files),
        batch_size=_BATCH_SIZE,
        catalog_root=str(catalog_root),
    )
    return instrument_id_str


def ensure_catalog_data(
    symbols: list[str],
    raw_parquet_root: Path,
    catalog_root: Path,
    *,
    asset_class: str = "stocks",
) -> list[str]:
    """Ensure the Nautilus catalog contains data for every requested symbol.

    Thin batch wrapper around :func:`build_catalog_for_symbol`.  The backtest
    worker calls this once per run with the list of symbols requested by the
    user; each symbol is lazily converted if needed.

    Args:
        symbols: List of ticker symbols (or Nautilus IDs) to ensure.
        raw_parquet_root: Root of raw OHLCV Parquet files.
        catalog_root: Root of the Nautilus catalog.
        asset_class: Asset-class sub-directory name. Defaults to ``"stocks"``.

    Returns:
        The list of canonical Nautilus instrument IDs in the same order as
        ``symbols``.  Callers typically pass this straight to
        ``BacktestDataConfig.instrument_ids``.

    Raises:
        FileNotFoundError: Propagated from :func:`build_catalog_for_symbol`
            if any symbol is missing raw data.
    """
    instrument_ids: list[str] = []
    for symbol in symbols:
        instrument_ids.append(
            build_catalog_for_symbol(
                symbol=symbol,
                raw_parquet_root=raw_parquet_root,
                catalog_root=catalog_root,
                asset_class=asset_class,
            )
        )
    return instrument_ids


def describe_catalog(
    instruments: list[str],
    data_path: str | Path,
    date_range: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Describe the Parquet catalog data available for given instruments.

    Scans the directory tree under *data_path* for Parquet files whose names
    match the requested instruments and returns a lightweight summary suitable
    for storing on a :class:`~msai.models.backtest.Backtest` row as
    ``data_snapshot``.

    The ``catalog_hash`` is a deterministic fingerprint derived from file
    names, sizes, and modification times.  Two backtests that ran against
    the exact same data files will share the same hash, making it trivial
    to identify which results are comparable.

    Args:
        instruments: Nautilus instrument IDs (e.g. ``["AAPL.SIM"]``).
        data_path: Root directory to scan for Parquet files.
        date_range: Optional ``(start, end)`` ISO-date strings.  Currently
            unused but reserved for future filtering.

    Returns:
        A dict with keys ``instruments``, ``file_count``, ``files``
        (capped at 50 entries), and ``catalog_hash``.
    """
    _ = date_range  # reserved for future use

    files_info: list[dict[str, Any]] = []
    hash_parts: list[str] = []

    data_root = Path(data_path)
    for instrument_id in instruments:
        # Extract the bare symbol from a Nautilus ID like "AAPL.SIM"
        symbol = instrument_id.split(".")[0] if "." in instrument_id else instrument_id

        # Look for a directory named exactly after the symbol (the MSAI
        # convention: {data_root}/{asset_class}/{symbol}/{YYYY}/{MM}.parquet)
        # and collect all .parquet files below it.  Fall back to a broad
        # filename match if no such directory exists.
        matching_files: list[Path] = []
        for candidate_dir in data_root.rglob(symbol):
            if candidate_dir.is_dir():
                matching_files.extend(sorted(candidate_dir.rglob("*.parquet")))
        if not matching_files:
            matching_files = sorted(data_root.rglob(f"*{symbol}*.parquet"))

        for f in matching_files:
            stat = f.stat()
            file_info: dict[str, Any] = {
                "path": str(f.relative_to(data_root)),
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            }
            files_info.append(file_info)
            hash_parts.append(f"{f.name}:{stat.st_size}:{stat.st_mtime}")

    catalog_hash = (
        sha256("|".join(sorted(hash_parts)).encode()).hexdigest()[:16]
        if hash_parts
        else "empty"
    )

    return {
        "instruments": instruments,
        "file_count": len(files_info),
        "files": files_info[:50],  # Cap to avoid huge JSON in the DB
        "catalog_hash": catalog_hash,
    }
