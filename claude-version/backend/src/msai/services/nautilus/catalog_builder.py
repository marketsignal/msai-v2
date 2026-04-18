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
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers import BarDataWrangler

from msai.core.logging import get_logger
from msai.services.nautilus.instruments import resolve_instrument

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
    raw_symbol_override: str | None = None,
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
        raw_symbol_override: Optional raw-symbol for the ingest-tree path
            lookup.  When ``None`` (default), the raw symbol is derived from
            the resolved Nautilus ``Instrument.raw_symbol`` — which works
            for equities (``AAPL.NASDAQ`` → ``AAPL``) but *not* for the
            registry-derived canonical IDs the backtest API now emits.  For
            futures canonicals like ``ESM6.CME`` the ingest pipeline writes
            Parquet under ``{asset_class}/ES/``, not ``{asset_class}/ESM6/``,
            so callers that pass a canonical ID MUST also pass the original
            root ticker (``"ES"``) via this kwarg for the path lookup to
            succeed.  See Codex Phase 5 F9.

            **Worker wiring is a follow-up PR:** today the backtest worker
            (:mod:`msai.workers.backtest_job`) does not yet have access to
            the user's original input symbols (they aren't persisted on
            :class:`~msai.models.backtest.Backtest`), so it still passes
            the canonical ID alone.  That wiring + the
            ``Backtest.input_symbols`` column will land with the live-path
            wiring follow-up.  This signature is the minimal public-API
            widening so the follow-up is a narrow change instead of a
            rewrite.

    Returns:
        The canonical Nautilus instrument ID string
        (e.g. ``"AAPL.SIM"``) that callers should pass to
        ``BacktestDataConfig.instrument_ids``.

    Raises:
        FileNotFoundError: No raw Parquet files exist for the requested
            symbol under ``{raw_parquet_root}/{asset_class}/{raw_symbol}``.
    """
    instrument = resolve_instrument(symbol)
    instrument_id_str = str(instrument.id)
    # F9: prefer the caller-supplied raw symbol for the ingest-tree
    # path lookup — needed whenever ``symbol`` is a registry-derived
    # canonical ID (e.g. ``ESM6.CME``) whose local-part does NOT match
    # the root ticker the ingestion pipeline writes under
    # (``ES/``). Equities happen to round-trip through
    # ``Instrument.raw_symbol`` because their canonical local-part IS
    # the ticker (``AAPL.NASDAQ`` → ``AAPL``).
    raw_symbol = raw_symbol_override or instrument.raw_symbol.value

    catalog_root.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_root))

    # Locate raw Parquet files.  We recurse because the ingestion pipeline
    # partitions by YYYY/MM and we want every partition in one sweep.
    symbol_dir = raw_parquet_root / asset_class / raw_symbol
    raw_files = sorted(symbol_dir.rglob("*.parquet"))
    if not raw_files:
        raise FileNotFoundError(
            f"No raw Parquet files found for {raw_symbol!r} under {symbol_dir}. "
            "Run the data ingestion pipeline for this symbol before backtesting."
        )

    # Idempotency guard: skip the rebuild only when the catalog
    # already contains bars AND the raw parquet tree hasn't changed
    # since the last build. The marker file lives under the catalog
    # root so it follows the catalog if it's relocated. Drill
    # 2026-04-15 hit the silent-stale variant: a partial ingest
    # produced 2 653 bars; a follow-up full-year ingest grew the raw
    # tree to 123 072 bars; the second backtest read the stale
    # 2 653 bars because the old guard short-circuited on "any bars
    # present" and never noticed the delta.
    source_hash = _compute_raw_source_hash(raw_files, raw_root=raw_parquet_root)
    marker_path = _source_marker_path(catalog_root, instrument_id_str)
    if not force:
        if marker_path.exists():
            stored_hash = marker_path.read_text().strip()
            if stored_hash == source_hash:
                existing_bars = catalog.bars(instrument_ids=[instrument_id_str])
                if existing_bars:
                    log.info(
                        "nautilus_catalog_already_populated",
                        instrument_id=instrument_id_str,
                        bar_count=len(existing_bars),
                        source_hash=source_hash,
                    )
                    return instrument_id_str
            else:
                log.info(
                    "nautilus_catalog_stale_rebuilding",
                    instrument_id=instrument_id_str,
                    stored_hash=stored_hash,
                    current_hash=source_hash,
                    raw_file_count=len(raw_files),
                )
                # Rebuild semantics: drop the cached bar directory for
                # THIS instrument + bar spec so the new bars don't
                # interleave with the stale ones. Other instruments
                # AND other bar specs for the same instrument are
                # untouched, and the shared instrument definition
                # under data/equity/ is left in place (Codex review
                # P2: deleting it strands sibling bar specs).
                _purge_catalog_for_instrument(catalog_root, instrument_id_str, bar_spec=_BAR_SPEC)
        else:
            # Markerless legacy catalog (Codex review P1). Pre-patch
            # builds didn't write a marker, so existing bars are
            # functionally untraceable to a source state. If the
            # catalog has bars for this instrument, treat them as
            # stale and purge — appending on top of a partially-
            # filled partition either silently no-ops or errors on
            # overlapping intervals, leaving a wrong catalog with a
            # fresh marker that locks in the staleness on the next
            # call.
            existing_bars = catalog.bars(instrument_ids=[instrument_id_str])
            if existing_bars:
                log.info(
                    "nautilus_catalog_legacy_unmarked_rebuilding",
                    instrument_id=instrument_id_str,
                    legacy_bar_count=len(existing_bars),
                    note=(
                        "no source-hash marker present; treating existing "
                        "bars as stale and purging before rebuild"
                    ),
                )
                _purge_catalog_for_instrument(catalog_root, instrument_id_str, bar_spec=_BAR_SPEC)

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

    # Persist the source hash AFTER the bars are written so a crash
    # mid-write leaves the marker absent and the next call rebuilds.
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(source_hash)

    log.info(
        "nautilus_catalog_built",
        instrument_id=instrument_id_str,
        bar_count=total_bars,
        partitions=len(raw_files),
        batch_size=_BATCH_SIZE,
        catalog_root=str(catalog_root),
        source_hash=source_hash,
    )
    return instrument_id_str


def _compute_raw_source_hash(raw_files: list[Path], *, raw_root: Path) -> str:
    """Hash the (relative path, size, mtime_ns, header bytes) of every
    raw parquet file. The relative path keeps year/month partitions
    distinct (``2024/01.parquet`` vs ``2025/01.parquet`` would
    otherwise collide on basename — Codex review P2). The first
    parquet bytes are sampled so an in-place rewrite that lands on
    the same byte size and same second-resolution mtime still
    invalidates the hash on filesystems whose mtime granularity is
    coarse."""
    parts: list[str] = []
    for path in sorted(raw_files):
        stat = path.stat()
        try:
            relpath = path.relative_to(raw_root).as_posix()
        except ValueError:
            relpath = path.as_posix()
        # Sample BOTH the parquet header (file magic + writer
        # fingerprint) AND the footer (Codex review P2 — schema
        # metadata + row-group offsets live in the footer; an
        # in-place rewrite that preserves size + mtime can leave the
        # prefix identical while later row groups or the footer
        # change). 4 KiB at each end keeps the cost bounded for
        # large parquets (one seek + two short reads) while catching
        # both same-prefix-different-footer and the legacy
        # different-prefix cases.
        header = b""
        footer = b""
        sample_bytes = 4096
        try:
            with path.open("rb") as handle:
                header = handle.read(sample_bytes)
                if stat.st_size > sample_bytes:
                    handle.seek(max(0, stat.st_size - sample_bytes))
                    footer = handle.read(sample_bytes)
        except OSError:
            pass
        edge_hash = sha256(header + footer).hexdigest()[:16]
        parts.append(f"{relpath}:{stat.st_size}:{stat.st_mtime_ns}:{edge_hash}")
    return sha256("|".join(parts).encode()).hexdigest()


def _source_marker_path(catalog_root: Path, instrument_id_str: str) -> Path:
    """Where the source-hash marker for an instrument lives.

    Co-located with the catalog (not the raw tree) so the same raw
    tree can back multiple catalogs without their markers stomping
    on each other.
    """
    return catalog_root / ".msai_source_hashes" / f"{instrument_id_str}.hash"


def _purge_catalog_for_instrument(
    catalog_root: Path,
    instrument_id_str: str,
    *,
    bar_spec: str,
) -> None:
    """Remove the bar files for one ``(instrument, bar_spec)`` from
    the catalog so a stale-detected rebuild can write a fresh copy.

    Only the matching ``data/bar/<instrument>-<bar_spec>`` directory
    is touched. Codex review P2: the shared
    ``data/equity/<instrument>`` definition is left in place because
    a single catalog can hold multiple bar specs for the same
    instrument (1-MINUTE-LAST, 5-MINUTE-LAST, ...) and they all
    point at the same instrument entry. Removing it would orphan
    the sibling specs and a crash before the rebuild's
    ``catalog.write_data([instrument])`` call would strand them
    permanently. ``write_data([instrument])`` later in the rebuild
    is idempotent — re-writing the same instrument is safe.
    """
    bar_dir = catalog_root / "data" / "bar" / f"{instrument_id_str}-{bar_spec}"
    if bar_dir.exists():
        for child in bar_dir.iterdir():
            child.unlink()
        bar_dir.rmdir()


def ensure_catalog_data(
    symbols: list[str],
    raw_parquet_root: Path,
    catalog_root: Path,
    *,
    asset_class: str = "stocks",
    raw_symbols: list[str] | None = None,
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
        raw_symbols: Optional parallel list of root tickers for the
            ingest-tree path lookup, one per entry in ``symbols``.  When
            provided, each entry is passed through as
            ``raw_symbol_override`` to :func:`build_catalog_for_symbol`.
            Required whenever ``symbols`` contains registry-derived
            canonical IDs whose local-part does not match the root ticker
            the ingestion pipeline writes under (e.g. ``ESM6.CME`` ingests
            under ``futures/ES/``, not ``futures/ESM6/``).  See Codex
            Phase 5 F9.

    Returns:
        The list of canonical Nautilus instrument IDs in the same order as
        ``symbols``.  Callers typically pass this straight to
        ``BacktestDataConfig.instrument_ids``.

    Raises:
        FileNotFoundError: Propagated from :func:`build_catalog_for_symbol`
            if any symbol is missing raw data.
        ValueError: When ``raw_symbols`` is provided but its length does
            not match ``symbols`` — the two lists are zipped positionally
            so a mismatch is a caller bug.
    """
    if raw_symbols is not None and len(raw_symbols) != len(symbols):
        raise ValueError(
            "raw_symbols length mismatch: "
            f"got {len(raw_symbols)} raw_symbols for {len(symbols)} symbols"
        )
    instrument_ids: list[str] = []
    for index, symbol in enumerate(symbols):
        raw_override = raw_symbols[index] if raw_symbols is not None else None
        instrument_ids.append(
            build_catalog_for_symbol(
                symbol=symbol,
                raw_parquet_root=raw_parquet_root,
                catalog_root=catalog_root,
                asset_class=asset_class,
                raw_symbol_override=raw_override,
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

        # Use the known MSAI layout: {data_root}/{asset_class}/{symbol}/...
        # rather than rglob which scans the entire (potentially huge) data tree.
        matching_files: list[Path] = []
        if data_root.is_dir():
            for asset_class_dir in data_root.iterdir():
                if not asset_class_dir.is_dir():
                    continue
                symbol_dir = asset_class_dir / symbol
                if symbol_dir.is_dir():
                    matching_files.extend(sorted(symbol_dir.rglob("*.parquet")))

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
        sha256("|".join(sorted(hash_parts)).encode()).hexdigest()[:16] if hash_parts else "empty"
    )

    return {
        "instruments": instruments,
        "file_count": len(files_info),
        "files": files_info[:50],  # Cap to avoid huge JSON in the DB
        "catalog_hash": catalog_hash,
    }
