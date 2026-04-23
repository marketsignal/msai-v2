"""DuckDB-based query service for Parquet market data.

Provides fast analytical queries over the Parquet store using DuckDB's
in-memory engine with direct Parquet file scanning.  This avoids loading
full DataFrames into memory for common query patterns.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from msai.core.logging import get_logger

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,20}$")

log = get_logger(__name__)


class MarketDataQuery:
    """Query Parquet bar data using DuckDB for fast analytical access."""

    def __init__(self, data_root: str) -> None:
        self.data_root = Path(data_root) / "parquet"

    def get_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "1m",
    ) -> list[dict[str, Any]]:
        """Query Parquet files via DuckDB and return OHLCV as list of dicts.

        Scans all asset class directories for the given symbol and returns
        matching bars in the requested date range.

        Args:
            symbol: Ticker symbol (e.g. ``"AAPL"``).
            start: ISO-8601 start date (inclusive).
            end: ISO-8601 end date (inclusive of all intraday bars).
            interval: Bar interval -- currently only ``"1m"`` is stored.

        Returns:
            List of dicts with keys: timestamp, open, high, low, close, volume.
        """
        if not _SYMBOL_PATTERN.match(symbol):
            return []

        parquet_glob = self._find_parquet_glob(symbol)
        if parquet_glob is None:
            return []

        con = duckdb.connect(":memory:")
        try:
            query = """
                SELECT timestamp, open, high, low, close, volume
                FROM read_parquet($1)
                WHERE timestamp >= $2::TIMESTAMP
                  AND timestamp < ($3::DATE + INTERVAL '1 day')
                ORDER BY timestamp
            """
            result = con.execute(query, [parquet_glob, start, end])
            columns = [desc[0] for desc in result.description]
            rows: list[dict[str, Any]] = [
                dict(zip(columns, row, strict=True)) for row in result.fetchall()
            ]
            return rows
        except duckdb.IOException:
            log.warning("duckdb_io_error", symbol=symbol, glob=parquet_glob)
            return []
        finally:
            con.close()

    def get_symbols(self) -> dict[str, list[str]]:
        """Return symbols grouped by asset class.

        Scans the data root directory tree for asset_class/symbol/ directories
        that contain at least one ``.parquet`` file.

        Returns:
            Mapping of asset class name to sorted list of symbol names.
        """
        grouped: dict[str, list[str]] = {}
        if not self.data_root.exists():
            return grouped

        for ac_dir in sorted(self.data_root.iterdir()):
            if not ac_dir.is_dir():
                continue
            symbols: list[str] = []
            for sym_dir in sorted(ac_dir.iterdir()):
                if sym_dir.is_dir() and list(sym_dir.rglob("*.parquet")):
                    symbols.append(sym_dir.name)
            if symbols:
                grouped[ac_dir.name] = symbols

        return grouped

    def get_storage_stats(self) -> dict[str, Any]:
        """Return storage statistics.

        Returns:
            Dictionary with ``asset_classes`` (name -> bytes), ``total_files``,
            and ``total_bytes``.
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

    def _find_parquet_glob(self, symbol: str) -> str | None:
        """Build a DuckDB-compatible glob pattern for a symbol's Parquet files.

        Searches all asset class directories for the symbol and returns the
        first matching glob path.  Returns ``None`` if no data exists.
        """
        if not self.data_root.exists():
            return None

        for ac_dir in self.data_root.iterdir():
            if not ac_dir.is_dir():
                continue
            sym_dir = ac_dir / symbol
            if sym_dir.exists() and list(sym_dir.rglob("*.parquet")):
                return str(sym_dir / "**" / "*.parquet")

        return None
