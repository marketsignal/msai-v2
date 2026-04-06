"""Data ingestion orchestrator for MSAI v2.

Coordinates fetching market data from external sources (Polygon, Databento)
and writing it to the local Parquet store.  Supports both bulk historical
downloads and incremental daily updates.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from msai.core.logging import get_logger
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.parquet_store import ParquetStore

log = get_logger(__name__)

# Map asset classes to their preferred data source.
_SOURCE_MAP: dict[str, str] = {
    "stocks": "polygon",
    "crypto": "polygon",
    "futures": "databento",
}


class DataIngestionService:
    """Orchestrates data fetching from external APIs and writing to Parquet."""

    def __init__(
        self,
        store: ParquetStore,
        polygon: PolygonClient | None = None,
        databento: DatabentoClient | None = None,
    ) -> None:
        self.store = store
        self.polygon = polygon
        self.databento = databento

    async def ingest_historical(
        self,
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict[str, int]:
        """Bulk download historical data for the given symbols.

        Routes each symbol to the appropriate data source based on asset class,
        fetches the bars, and writes them to the Parquet store.

        Args:
            asset_class: Asset class name (``"stocks"``, ``"futures"``, etc.).
            symbols: List of ticker symbols to ingest.
            start: ISO-8601 start date (``"YYYY-MM-DD"``).
            end: ISO-8601 end date (``"YYYY-MM-DD"``).

        Returns:
            Mapping of symbol to number of rows written.
        """
        results: dict[str, int] = {}

        for symbol in symbols:
            try:
                df = await self._fetch_bars(asset_class, symbol, start, end)
                if df.empty:
                    log.warning("no_data_returned", symbol=symbol, start=start, end=end)
                    results[symbol] = 0
                    continue

                self.store.write_bars(asset_class, symbol, df)
                results[symbol] = len(df)
                log.info(
                    "ingested_historical",
                    symbol=symbol,
                    rows=len(df),
                    start=start,
                    end=end,
                )
            except Exception as exc:  # noqa: BLE001 — log and continue per-symbol
                log.error(
                    "ingest_error",
                    symbol=symbol,
                    error=str(exc),
                )
                results[symbol] = 0

        return results

    async def ingest_daily(
        self,
        asset_class: str,
        symbols: list[str],
    ) -> dict[str, int]:
        """Incremental daily update -- fetches yesterday's data.

        Convenience method that delegates to :meth:`ingest_historical` with
        ``start`` and ``end`` set to yesterday's date.

        Args:
            asset_class: Asset class name.
            symbols: List of ticker symbols to update.

        Returns:
            Mapping of symbol to number of rows written.
        """
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        return await self.ingest_historical(asset_class, symbols, yesterday, yesterday)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_bars(
        self,
        asset_class: str,
        symbol: str,
        start: str,
        end: str,
    ) -> Any:
        """Route to the correct data source and fetch bars."""
        source = _SOURCE_MAP.get(asset_class, "polygon")

        if source == "databento" and self.databento is not None:
            return await self.databento.fetch_futures_bars(symbol, start, end)

        if self.polygon is not None:
            return await self.polygon.fetch_bars(symbol, start, end)

        log.error(
            "no_data_source_configured",
            asset_class=asset_class,
            symbol=symbol,
        )
        import pandas as pd

        return pd.DataFrame()
