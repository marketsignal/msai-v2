from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.queue import enqueue_ingestion, get_redis_pool
from msai.schemas.backtest import MarketDataDailyIngestRequest, MarketDataIngestRequest
from msai.schemas.market_data import (
    BarsResponse,
    DailyUniverseEntry,
    DailyUniverseResponse,
    StorageStatsResponse,
    SymbolsResponse,
)
from msai.services.daily_ingest import DailyIngestRequest
from msai.services.daily_universe import DailyUniverseService
from msai.services.data_ingestion import DataIngestionService
from msai.services.market_data_query import MarketDataQuery
from msai.services.parquet_store import ParquetStore

router = APIRouter(prefix="/market-data", tags=["market-data"])
daily_universe_service = DailyUniverseService()


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
        recent_runs=status.get("recent_runs", []),
    )


@router.post("/ingest")
async def trigger_ingest(
    payload: MarketDataIngestRequest,
    _: Mapping[str, object] = Depends(get_current_user),
) -> dict[str, str]:
    await _enqueue_ingestion_request(
        payload.asset_class,
        payload.symbols,
        payload.start,
        payload.end,
        provider=payload.provider,
        dataset=payload.dataset,
        schema=payload.data_schema,
    )
    return {"status": "queued"}


@router.post("/ingest-daily")
async def trigger_daily_ingest(
    payload: MarketDataDailyIngestRequest,
    _: Mapping[str, object] = Depends(get_current_user),
) -> dict[str, str]:
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    await _enqueue_ingestion_request(
        payload.asset_class,
        payload.symbols,
        start,
        end,
        provider=payload.provider,
        dataset=payload.dataset,
        schema=payload.data_schema,
    )
    return {"status": "queued", "start": start, "end": end}


@router.get("/daily-universe", response_model=DailyUniverseResponse)
async def get_daily_universe(
    _: Mapping[str, object] = Depends(get_current_user),
) -> DailyUniverseResponse:
    requests = daily_universe_service.list_requests()
    return DailyUniverseResponse(requests=[DailyUniverseEntry(**asdict(request)) for request in requests])


@router.put("/daily-universe", response_model=DailyUniverseResponse)
async def update_daily_universe(
    payload: DailyUniverseResponse,
    _: Mapping[str, object] = Depends(get_current_user),
) -> DailyUniverseResponse:
    requests = daily_universe_service.save_requests(
        [
            DailyIngestRequest(
                asset_class=request.asset_class,
                symbols=request.symbols,
                provider=request.provider,
                dataset=request.dataset,
                schema=request.data_schema,
            )
            for request in payload.requests
        ]
    )
    return DailyUniverseResponse(requests=[DailyUniverseEntry(**asdict(request)) for request in requests])


@router.post("/ingest-daily-configured")
async def trigger_configured_daily_ingest(
    _: Mapping[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    requests = daily_universe_service.list_requests()
    for request in requests:
        await _enqueue_ingestion_request(
            request.asset_class,
            request.symbols,
            start,
            end,
            provider=request.provider,
            dataset=request.dataset,
            schema=request.schema,
        )
    return {
        "status": "queued",
        "start": start,
        "end": end,
        "request_count": len(requests),
    }


async def _enqueue_ingestion_request(
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    provider: str,
    dataset: str | None,
    schema: str,
) -> None:
    pool = await get_redis_pool()
    await enqueue_ingestion(
        pool,
        asset_class,
        symbols,
        start,
        end,
        provider=provider,
        dataset=dataset,
        schema=schema,
    )
