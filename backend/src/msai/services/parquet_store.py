"""Atomic Parquet store for OHLCV bar data.

Manages reading and writing of market data bars to a Parquet-based file store,
partitioned by asset_class/symbol/YYYY/MM.parquet.  All writes use the atomic
write + dedup primitives from :mod:`msai.core.data_integrity`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa

from msai.core.data_integrity import atomic_write_parquet, dedup_bars
from msai.core.logging import get_logger
from msai.services.symbol_onboarding.partition_index import CacheRefreshMisuseError

if TYPE_CHECKING:
    from collections.abc import Callable

    PartitionIndexRefresh = Callable[[str, str, int, int, Path], None]

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,20}$")

log = get_logger(__name__)


class ParquetStore:
    """File-based Parquet store partitioned by asset_class / symbol / year / month."""

    def __init__(
        self,
        data_root: str,
        *,
        partition_index_refresh: PartitionIndexRefresh | None = None,
    ) -> None:
        """Construct the store.

        ``partition_index_refresh`` is a SYNC callable invoked once per
        ``(year, month)`` group after each atomic write succeeds. The callback
        owns its own DB session + event-loop binding (see
        :func:`msai.services.symbol_onboarding.partition_index.make_refresh_callback`
        for the canonical builder). The writer itself is event-loop-agnostic
        and never opens a session, so we do not cross SQLAlchemy's
        "one async engine per event loop" rule
        (https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops).

        :class:`CacheRefreshMisuseError` (defined in
        :mod:`msai.services.symbol_onboarding.partition_index`) is the only
        callback exception that ``write_bars`` re-raises — it signals a
        caller-contract violation (running ``write_bars`` from an async
        context without ``asyncio.to_thread``). Every other exception is
        logged + swallowed; the parquet file is the source of truth and a
        subsequent ``compute_coverage`` run will refresh the cache.

        Pass ``None`` for environments without DB access (CLI seed scripts,
        ad-hoc tooling). The one-time backfill (Task 5) catches up the cache.
        """
        self.data_root = Path(data_root)
        self._refresh_callback: PartitionIndexRefresh | None = partition_index_refresh

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_bars(self, asset_class: str, symbol: str, df: pd.DataFrame) -> str:
        """Write bars to Parquet partitioned by asset_class/symbol/YYYY/MM.parquet.

        The incoming DataFrame is deduplicated on (symbol, timestamp) before
        writing.  If the target Parquet file already exists, the new data is
        merged with the existing rows and deduplicated again so that
        overlapping ingestion windows are handled idempotently.

        After each successful atomic write, ``partition_index_refresh`` (if
        provided) is invoked once with the partition coordinates so the
        ``parquet_partition_index`` cache stays in sync. ``CacheRefreshMisuseError``
        from the callback is re-raised (caller-contract violation: see
        :func:`make_refresh_callback`); every other exception is logged and
        swallowed because the parquet file on disk is the source of truth.

        Args:
            asset_class: E.g. ``"stocks"``, ``"futures"``, ``"crypto"``.
            symbol: Ticker symbol, e.g. ``"AAPL"``.
            df: DataFrame with at least ``timestamp`` plus OHLCV columns.

        Returns:
            SHA-256 hex checksum of the last written Parquet file.
        """
        if not _SYMBOL_PATTERN.match(symbol):
            log.warning("write_bars invalid symbol rejected", symbol=symbol)
            return ""

        if df.empty:
            log.warning("write_bars called with empty DataFrame", symbol=symbol)
            return ""

        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # Dedup by timestamp only — each file is already per-symbol.
        dedup_key = ("timestamp",)
        df = dedup_bars(df, key_columns=dedup_key)

        last_checksum = ""
        for (year, month), group in df.groupby([df["timestamp"].dt.year, df["timestamp"].dt.month]):
            target = self._bar_path(asset_class, symbol, int(year), int(month))

            # Merge with existing data when the file already exists.
            if target.exists():
                existing = pd.read_parquet(target)
                merged = pd.concat([existing, group], ignore_index=True)
                group = dedup_bars(merged, key_columns=dedup_key)

            table = pa.Table.from_pandas(group, preserve_index=False)
            last_checksum = atomic_write_parquet(table, target)
            log.info(
                "wrote_bars",
                symbol=symbol,
                year=year,
                month=month,
                rows=len(group),
            )

            if self._refresh_callback is not None:
                try:
                    self._refresh_callback(asset_class, symbol, int(year), int(month), target)
                except CacheRefreshMisuseError:
                    # Caller-contract violation (write_bars invoked from
                    # async without to_thread wrap). NOT a runtime
                    # data-layer problem — propagate so the engineer sees
                    # the misuse instead of a silent stale cache.
                    # P2 Codex iteration 4 fix.
                    raise
                except Exception:
                    # Genuine cache-update failure (DB down, transient
                    # network error). Best-effort: the parquet file is
                    # the source of truth and the next compute_coverage
                    # call will refresh from the footer. P3-2 plan-review
                    # fix: include traceback for diagnosability.
                    log.warning(
                        "partition_index_refresh_failed",
                        symbol=symbol,
                        asset_class=asset_class,
                        year=int(year),
                        month=int(month),
                        exc_info=True,
                    )

        return last_checksum

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_bars(
        self,
        asset_class: str,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Read bars from Parquet files, optionally filtered by date range.

        Args:
            asset_class: Asset class directory name.
            symbol: Ticker symbol.
            start: ISO-8601 start date filter (inclusive).  ``None`` means no
                lower bound.
            end: ISO-8601 end date filter (inclusive).  ``None`` means no upper
                bound.

        Returns:
            A concatenated DataFrame of all matching rows, sorted by timestamp.
            Returns an empty DataFrame if no data is found.
        """
        if not _SYMBOL_PATTERN.match(symbol):
            return pd.DataFrame()

        symbol_dir = self.data_root / asset_class / symbol
        if not symbol_dir.exists():
            return pd.DataFrame()

        parquet_files = sorted(symbol_dir.rglob("*.parquet"))
        if not parquet_files:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = [pd.read_parquet(f) for f in parquet_files]
        df = pd.concat(frames, ignore_index=True)

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            if start is not None:
                df = df[df["timestamp"] >= pd.Timestamp(start, tz="UTC")]
            if end is not None:
                df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]
            df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_symbols(self, asset_class: str) -> list[str]:
        """List available symbols for an asset class by scanning directories.

        Args:
            asset_class: Asset class directory name.

        Returns:
            Sorted list of symbol names that have at least one Parquet file.
        """
        ac_dir = self.data_root / asset_class
        if not ac_dir.exists():
            return []

        symbols: list[str] = []
        for child in sorted(ac_dir.iterdir()):
            if child.is_dir() and list(child.rglob("*.parquet")):
                symbols.append(child.name)
        return symbols

    def get_storage_stats(self) -> dict[str, Any]:
        """Return storage stats: size per asset class, total files, total size.

        Returns:
            Dictionary with keys ``asset_classes`` (mapping name to byte size),
            ``total_files``, and ``total_bytes``.
        """
        asset_classes: dict[str, int] = {}
        total_files = 0
        total_bytes = 0

        if not self.data_root.exists():
            return {
                "asset_classes": asset_classes,
                "total_files": total_files,
                "total_bytes": total_bytes,
            }

        for ac_dir in sorted(self.data_root.iterdir()):
            if not ac_dir.is_dir():
                continue
            ac_bytes = 0
            for pf in ac_dir.rglob("*.parquet"):
                size = pf.stat().st_size
                ac_bytes += size
                total_bytes += size
                total_files += 1
            asset_classes[ac_dir.name] = ac_bytes

        return {
            "asset_classes": asset_classes,
            "total_files": total_files,
            "total_bytes": total_bytes,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bar_path(self, asset_class: str, symbol: str, year: int, month: int) -> Path:
        """Compute the canonical Parquet file path for a given partition."""
        return self.data_root / asset_class / symbol / str(year) / f"{month:02d}.parquet"
