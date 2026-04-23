"""Market Data API router.

Provides endpoints for querying OHLCV bar data, listing available symbols,
checking storage/ingestion status, and triggering manual data ingestion.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.logging import get_logger
from msai.schemas.market_data import (
    BarResponse,
    BarsResponse,
    IngestRequest,
    IngestResponse,
    StatusResponse,
    StorageStatsResponse,
    SymbolsResponse,
)
from msai.services.market_data_query import MarketDataQuery

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/market-data", tags=["market-data"])

# ---------------------------------------------------------------------------
# Dependency -- instantiate query service from config
# ---------------------------------------------------------------------------


def _get_query_service() -> MarketDataQuery:
    return MarketDataQuery(str(settings.data_root))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/bars/{symbol}", response_model=BarsResponse)
async def get_bars(
    symbol: str,
    start: str = Query(..., description="Start date YYYY-MM-DD"),
    end: str = Query(..., description="End date YYYY-MM-DD"),
    interval: str = Query("1m", description="Bar interval"),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> BarsResponse:
    """Query OHLCV bars for a symbol within a date range."""
    query = _get_query_service()
    bars_raw = query.get_bars(symbol, start, end, interval=interval)

    bars = [
        BarResponse(
            timestamp=b["timestamp"],
            open=b["open"],
            high=b["high"],
            low=b["low"],
            close=b["close"],
            volume=b["volume"],
        )
        for b in bars_raw
    ]

    return BarsResponse(
        symbol=symbol,
        interval=interval,
        bars=bars,
        count=len(bars),
    )


@router.get("/symbols", response_model=SymbolsResponse)
async def list_symbols(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> SymbolsResponse:
    """List available symbols grouped by asset class."""
    query = _get_query_service()
    symbols = query.get_symbols()
    return SymbolsResponse(symbols=symbols)


@router.get("/status", response_model=StatusResponse)
async def get_status(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> StatusResponse:
    """Return ingestion health and storage statistics."""
    query = _get_query_service()
    stats = query.get_storage_stats()
    return StatusResponse(
        status="ok",
        storage=StorageStatsResponse(**stats),
    )


@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingest(
    body: IngestRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> IngestResponse:
    """Trigger manual data ingestion by enqueuing an arq job.

    The actual ingestion runs asynchronously via the arq worker. This
    endpoint returns 202 Accepted immediately.  Accepts optional
    ``provider``, ``dataset``, and ``data_schema`` fields to control
    routing (defaults to Databento for equities/futures).
    """
    try:
        from msai.core.queue import enqueue_ingest, get_redis_pool

        pool = await get_redis_pool()
        await enqueue_ingest(
            pool=pool,
            asset_class=body.asset_class,
            symbols=body.symbols,
            start=body.start.isoformat(),
            end=body.end.isoformat(),
            provider=body.provider,
            dataset=body.dataset,
            schema=body.data_schema,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("ingest_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue ingestion job — Redis may be unavailable",
        ) from exc

    return IngestResponse(
        message="Ingestion job enqueued",
        asset_class=body.asset_class,
        symbols=body.symbols,
        start=body.start,
        end=body.end,
    )
