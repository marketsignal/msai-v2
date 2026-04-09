from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_backtest, get_redis_pool
from msai.models import Backtest, Strategy, Trade
from msai.schemas.backtest import (
    BacktestAnalyticsResponse,
    BacktestResultsResponse,
    BacktestRunRequest,
    BacktestRunResponse,
    BacktestStatusResponse,
)
from msai.services.backtest_analytics import (
    BacktestAnalyticsNotFoundError,
    BacktestAnalyticsService,
)
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.strategy_registry import StrategyRegistry, file_sha256
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/backtests", tags=["backtests"])
logger = get_logger("api.backtests")
analytics_service = BacktestAnalyticsService()


@router.post("/run", response_model=BacktestRunResponse)
async def run_backtest(
    payload: BacktestRunRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BacktestRunResponse:
    strategy = await db.get(Strategy, payload.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")

    registry = StrategyRegistry(settings.strategies_root)
    strategy_path = registry.resolve_path(strategy)
    if not strategy_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy file not found: {strategy.file_path}",
        )

    try:
        canonical_instruments = await instrument_service.canonicalize_backtest_instruments(
            db,
            payload.instruments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user_id = await resolve_user_id_from_claims(db, claims)
    backtest = Backtest(
        strategy_id=strategy.id,
        strategy_code_hash=file_sha256(strategy_path),
        config=payload.config,
        instruments=canonical_instruments,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status="pending",
        progress=0,
        created_by=user_id,
    )
    db.add(backtest)
    await db.flush()

    try:
        pool = await get_redis_pool()
        queue_job_id = await enqueue_backtest(pool, backtest.id, str(strategy_path), payload.config)
        backtest.queue_name = settings.backtest_queue_name
        backtest.queue_job_id = queue_job_id or backtest.id
    except Exception as exc:
        await db.rollback()
        logger.warning("backtest_enqueue_failed", error=str(exc), strategy_id=strategy.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backtest queue unavailable",
        ) from exc

    await db.commit()
    await db.refresh(backtest)
    return BacktestRunResponse(job_id=backtest.id, status=backtest.status)


@router.get("/{job_id}/status", response_model=BacktestStatusResponse)
async def backtest_status(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BacktestStatusResponse:
    backtest = await db.get(Backtest, job_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest not found")
    return BacktestStatusResponse(
        id=backtest.id,
        status=backtest.status,
        progress=backtest.progress,
        error_message=backtest.error_message,
        queue_name=backtest.queue_name,
        queue_job_id=backtest.queue_job_id,
        worker_id=backtest.worker_id,
        attempt=int(backtest.attempt or 0),
        heartbeat_at=backtest.heartbeat_at.isoformat() if backtest.heartbeat_at else None,
    )


@router.get("/{job_id}/results", response_model=BacktestResultsResponse)
async def backtest_results(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BacktestResultsResponse:
    backtest = await db.get(Backtest, job_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest not found")

    trades = (
        await db.execute(select(Trade).where(Trade.backtest_id == backtest.id).order_by(Trade.executed_at))
    ).scalars()
    rows = [
        {
            "id": t.id,
            "instrument": t.instrument,
            "side": t.side,
            "quantity": float(t.quantity),
            "price": float(t.price),
            "commission": float(t.commission) if t.commission is not None else None,
            "pnl": float(t.pnl) if t.pnl is not None else None,
            "executed_at": t.executed_at.isoformat(),
        }
        for t in trades
    ]

    return BacktestResultsResponse(
        id=backtest.id,
        status=backtest.status,
        metrics=backtest.metrics,
        trades=rows,
    )


@router.get("/{job_id}/analytics", response_model=BacktestAnalyticsResponse)
async def backtest_analytics(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> BacktestAnalyticsResponse:
    try:
        payload = analytics_service.load(job_id)
    except BacktestAnalyticsNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return BacktestAnalyticsResponse(**payload)


@router.get("/{job_id}/report")
async def backtest_report(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    backtest = await db.get(Backtest, job_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest not found")
    if not backtest.report_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not available")

    report_path = Path(backtest.report_path)
    if not report_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report file not found")
    return FileResponse(report_path, media_type="text/html", filename=report_path.name)


@router.get("/history")
async def backtest_history(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (
        await db.execute(select(Backtest).order_by(desc(Backtest.created_at)).limit(100))
    ).scalars()
    return [
        {
            "id": b.id,
            "strategy_id": b.strategy_id,
            "status": b.status,
            "created_at": b.created_at.isoformat(),
            "started_at": b.started_at.isoformat() if b.started_at else None,
            "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            "metrics": b.metrics,
        }
        for b in rows
    ]
