from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, Query

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.queue import enqueue_ingestion, get_redis_pool
from msai.schemas.backtest import MarketDataIngestRequest
from msai.schemas.market_data import BarsResponse, StorageStatsResponse, SymbolsResponse
from msai.services.data_ingestion import DataIngestionService
from msai.services.market_data_query import MarketDataQuery
from msai.services.parquet_store import ParquetStore

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/bars/{symbol}", response_model=BarsResponse)
async def get_bars(
    symbol: str,
    start: str = Query(...),
    end: str = Query(...),
    interval: str = Query("1m"),
    _: Mapping[str, object] = Depends(get_current_user),
) -> BarsResponse:
    payload = MarketDataQuery(settings.data_root).get_bars(symbol, start, end, interval)
    return BarsResponse(**payload)


@router.get("/symbols", response_model=SymbolsResponse)
async def get_symbols(_: Mapping[str, object] = Depends(get_current_user)) -> SymbolsResponse:
    payload = MarketDataQuery(settings.data_root).get_symbols()
    return SymbolsResponse(symbols=payload)


@router.get("/status", response_model=StorageStatsResponse)
async def get_status(_: Mapping[str, object] = Depends(get_current_user)) -> StorageStatsResponse:
    service = DataIngestionService(ParquetStore(settings.data_root))
    status = service.data_status()
    return StorageStatsResponse(
        status="ok",
        last_run_at=status.get("last_run_at"),
        storage_stats=status.get("storage_stats", {}),
        gaps_detected=status.get("gaps_detected", []),
    )


@router.post("/ingest")
async def trigger_ingest(
    payload: MarketDataIngestRequest,
    _: Mapping[str, object] = Depends(get_current_user),
) -> dict[str, str]:
    pool = await get_redis_pool()
    await enqueue_ingestion(pool, payload.asset_class, payload.symbols, payload.start, payload.end)
    return {"status": "queued"}
