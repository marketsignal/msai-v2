from __future__ import annotations

import json
from datetime import date, timedelta

from msai.core.config import settings
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.market_data_query import MarketDataQuery
from msai.services.parquet_store import ParquetStore


class DataIngestionService:
    def __init__(self, parquet_store: ParquetStore) -> None:
        self.parquet_store = parquet_store
        self.polygon = PolygonClient()
        self.databento = DatabentoClient()
        self.status_file = settings.data_root / "ingestion_status.json"

    async def ingest_historical(
        self,
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict:
        ingested: dict[str, int] = {}
        for symbol in symbols:
            if asset_class == "futures":
                frame = await self.databento.fetch_bars(symbol, start, end)
            else:
                frame = await self.polygon.fetch_bars(symbol, start, end)
            written_paths = self.parquet_store.write_bars(asset_class, symbol, frame)
            ingested[symbol] = len(written_paths)

        self._write_status(
            {
                "last_run_at": date.today().isoformat(),
                "asset_class": asset_class,
                "symbols": symbols,
                "start": start,
                "end": end,
                "ingested": ingested,
            }
        )
        return ingested

    async def ingest_daily(self, asset_class: str, symbols: list[str]) -> dict:
        yesterday = date.today() - timedelta(days=1)
        start = yesterday.isoformat()
        end = date.today().isoformat()
        return await self.ingest_historical(asset_class, symbols, start, end)

    def data_status(self) -> dict:
        payload: dict
        if self.status_file.exists():
            payload = json.loads(self.status_file.read_text())
        else:
            payload = {"last_run_at": None}
        payload["storage_stats"] = MarketDataQuery(settings.data_root).get_storage_stats()
        return payload

    def _write_status(self, payload: dict) -> None:
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(json.dumps(payload, indent=2, sort_keys=True))


async def run_ingest(
    ctx: dict,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
) -> None:
    _ = ctx
    service = DataIngestionService(ParquetStore(settings.data_root))
    await service.ingest_historical(asset_class, symbols, start, end)
