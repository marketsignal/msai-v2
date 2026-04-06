"""NautilusTrader Parquet catalog abstraction.

Wraps NautilusTrader's ParquetDataCatalog when available, falling back to
direct Parquet reading via the existing :class:`ParquetStore` otherwise.
This allows the backtesting engine to work even when NautilusTrader's native
C extensions are not installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from msai.core.logging import get_logger
from msai.services.parquet_store import ParquetStore

log = get_logger(__name__)

# Try to import NautilusTrader; gracefully degrade if unavailable.
try:
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    _HAS_NAUTILUS = True
except ImportError:
    _HAS_NAUTILUS = False


class NautilusCatalog:
    """Wrapper around NautilusTrader's ParquetDataCatalog.

    Falls back to direct Parquet reading via :class:`ParquetStore` if
    NautilusTrader is not available or the catalog path does not exist.

    Args:
        data_root: Root directory for Parquet data storage.
    """

    def __init__(self, data_root: str) -> None:
        self.data_root = Path(data_root)
        self._store = ParquetStore(data_root)
        self._catalog: Any | None = None

        if _HAS_NAUTILUS:
            try:
                self._catalog = ParquetDataCatalog(str(self.data_root))
                log.info("nautilus_catalog_initialized", path=str(self.data_root))
            except Exception:
                log.warning(
                    "nautilus_catalog_fallback",
                    reason="Failed to initialize NautilusTrader catalog, using ParquetStore",
                )
        else:
            log.info("nautilus_not_available", fallback="ParquetStore")

    @property
    def has_nautilus(self) -> bool:
        """Return True if NautilusTrader catalog is available."""
        return self._catalog is not None

    def get_available_instruments(self) -> list[str]:
        """List instruments available in the Parquet catalog.

        Scans the data root for asset class directories, then lists symbols
        within each. Returns a flat list of ``asset_class/symbol`` strings.

        Returns:
            Sorted list of available instrument identifiers.
        """
        instruments: list[str] = []

        if not self.data_root.exists():
            return instruments

        for ac_dir in sorted(self.data_root.iterdir()):
            if not ac_dir.is_dir():
                continue
            asset_class = ac_dir.name
            symbols = self._store.list_symbols(asset_class)
            for symbol in symbols:
                instruments.append(f"{asset_class}/{symbol}")

        return instruments

    def load_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        asset_class: str = "stocks",
    ) -> pd.DataFrame:
        """Load bar data for a symbol.

        Uses ParquetStore to read from the partitioned directory structure.
        NautilusTrader catalog integration is reserved for Phase 2.

        Args:
            symbol: Ticker symbol, e.g. ``"AAPL"``.
            start: ISO-8601 start date filter (inclusive). ``None`` = no lower bound.
            end: ISO-8601 end date filter (inclusive). ``None`` = no upper bound.
            asset_class: Asset class directory name (default ``"stocks"``).

        Returns:
            DataFrame of OHLCV bar data sorted by timestamp.
            Empty DataFrame if no data is found.
        """
        df = self._store.read_bars(asset_class, symbol, start=start, end=end)

        if df.empty:
            log.warning("no_bars_found", symbol=symbol, asset_class=asset_class)

        return df
