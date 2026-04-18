"""Backtests API router -- launch, monitor, and retrieve backtest results.

Manages the full lifecycle of backtest runs: creation, status polling,
results retrieval, and history browsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_backtest, get_redis_pool
from msai.models.backtest import Backtest
from msai.models.strategy import Strategy
from msai.models.trade import Trade
from msai.schemas.backtest import (
    BacktestListItem,
    BacktestListResponse,
    BacktestResultsResponse,
    BacktestRunRequest,
    BacktestStatusResponse,
)
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.service import (
    DatabentoDefinitionMissing,
    SecurityMaster,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])


@router.post("/run", status_code=status.HTTP_201_CREATED, response_model=BacktestStatusResponse)
async def run_backtest(
    body: BacktestRunRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestStatusResponse:
    """Create a new backtest record and enqueue it for execution.

    The backtest is created with status ``pending`` and enqueued to the
    arq worker pool via Redis. The caller should poll ``GET /{job_id}/status``
    to track progress.

    """
    # Verify the strategy exists
    result = await db.execute(select(Strategy).where(Strategy.id == body.strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {body.strategy_id} not found",
        )

    # Compute strategy code hash from the source file so the backtest is
    # reproducibly pinned to the exact code version used at enqueue time.
    strategy_hash = "unknown"
    if strategy.file_path:
        strategy_file = Path(strategy.file_path)
        if strategy_file.exists():
            import hashlib

            strategy_hash = hashlib.sha256(strategy_file.read_bytes()).hexdigest()

    # The worker now pulls instrument / date fields directly from the
    # Backtest row, so ``config`` is forwarded to the Nautilus
    # StrategyConfig verbatim.  We still make a defensive copy so the
    # caller's dict is not mutated downstream.
    worker_config = dict(body.config)

    # v3.0 (Task 11) — resolve every instrument the caller supplied
    # through the DB-backed registry BEFORE storing the backtest row.
    # ``SecurityMaster.resolve_for_backtest`` is fail-loud on a warm-
    # path miss (``DatabentoDefinitionMissing`` — operator must run
    # ``msai instruments refresh`` first) with a single exception: the
    # ``<root>.Z.<N>`` continuous-futures synthesis path calls Databento
    # on cold-miss. The qualifier is ``None`` because backtest
    # resolution never needs an IB round-trip. The Databento client
    # follows the existing pattern used at
    # ``workers/nightly_ingest.py`` and ``services/data_ingestion.py``
    # (None when the API key is unset — the resolver will raise a
    # ``ValueError`` with a clear message on the ``.Z.N`` cold-miss
    # path, which is what we want).
    databento_client = (
        DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None
    )
    security_master = SecurityMaster(
        qualifier=None,
        db=db,
        databento_client=databento_client,
    )
    try:
        canonical_instruments = await security_master.resolve_for_backtest(
            body.instruments,
            start=body.start_date.isoformat(),
            end=body.end_date.isoformat(),
        )
    except DatabentoDefinitionMissing as exc:
        log.warning(
            "backtest_instrument_unresolved",
            symbols=body.instruments,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        log.warning(
            "backtest_instrument_value_error",
            symbols=body.instruments,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Create the backtest record
    backtest = Backtest(
        strategy_id=body.strategy_id,
        strategy_code_hash=strategy_hash,
        config=worker_config,
        instruments=canonical_instruments,
        start_date=body.start_date,
        end_date=body.end_date,
        status="pending",
        progress=0,
    )
    db.add(backtest)
    # Flush so ``backtest.id`` is assigned before we enqueue it to arq.
    await db.flush()

    # Enqueue to arq BEFORE commit — if enqueue fails, rollback the row
    try:
        pool = await get_redis_pool()
        backtest.queue_name = "arq:queue"
        job_id = await enqueue_backtest(pool, str(backtest.id), strategy.file_path, worker_config)
        backtest.queue_job_id = job_id
    except Exception as exc:
        await db.rollback()
        log.error("backtest_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue backtest job — Redis may be unavailable",
        ) from exc

    await db.commit()
    await db.refresh(backtest)

    log.info("backtest_enqueued", backtest_id=str(backtest.id), strategy_id=str(body.strategy_id))

    return BacktestStatusResponse(
        id=backtest.id,
        status=backtest.status,
        progress=backtest.progress,
        started_at=backtest.started_at,
        completed_at=backtest.completed_at,
    )


@router.get("/history", response_model=BacktestListResponse)
async def list_backtests(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestListResponse:
    """List past backtests with pagination."""
    # Count total
    count_result = await db.execute(select(func.count()).select_from(Backtest))
    total: int = count_result.scalar_one()

    # Fetch page
    offset = (page - 1) * page_size
    result = await db.execute(
        select(Backtest).order_by(Backtest.created_at.desc()).offset(offset).limit(page_size)
    )
    backtests = result.scalars().all()

    items = [
        BacktestListItem(
            id=bt.id,
            strategy_id=bt.strategy_id,
            status=bt.status,
            start_date=bt.start_date,
            end_date=bt.end_date,
            created_at=bt.created_at,
        )
        for bt in backtests
    ]

    return BacktestListResponse(items=items, total=total)


@router.get("/{job_id}/status", response_model=BacktestStatusResponse)
async def get_backtest_status(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestStatusResponse:
    """Return the current status of a backtest run."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    return BacktestStatusResponse(
        id=backtest.id,
        status=backtest.status,
        progress=backtest.progress,
        started_at=backtest.started_at,
        completed_at=backtest.completed_at,
    )


@router.get("/{job_id}/results", response_model=BacktestResultsResponse)
async def get_backtest_results(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestResultsResponse:
    """Return metrics, trade count, and individual trade rows for a backtest."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    # Fetch every trade generated by this backtest so the UI can render
    # the trade log without a second round-trip.
    trade_rows_result = await db.execute(
        select(Trade).where(Trade.backtest_id == job_id).order_by(Trade.executed_at.asc())
    )
    trade_rows = trade_rows_result.scalars().all()

    trade_count = len(trade_rows)

    trades_payload: list[dict[str, Any]] = [
        {
            "id": str(trade.id),
            "instrument": trade.instrument,
            "side": trade.side,
            "quantity": float(trade.quantity),
            "price": float(trade.price),
            "pnl": float(trade.pnl) if trade.pnl is not None else 0.0,
            "commission": float(trade.commission) if trade.commission is not None else 0.0,
            "executed_at": trade.executed_at.isoformat(),
        }
        for trade in trade_rows
    ]

    return BacktestResultsResponse(
        id=backtest.id,
        metrics=backtest.metrics,
        trade_count=trade_count,
        trades=trades_payload,
    )


@router.get("/{job_id}/report")
async def get_backtest_report(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse:
    """Return the QuantStats HTML report file for a completed backtest."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    if backtest.report_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No report available for backtest {job_id}",
        )

    # Path traversal protection: ensure resolved path is within expected directory
    report_file = Path(backtest.report_path).resolve()
    expected_dir = (Path(settings.data_root) / "reports").resolve()
    if not str(report_file).startswith(str(expected_dir)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid report path",
        )

    if not report_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file not found on disk for backtest {job_id}",
        )

    return FileResponse(
        path=str(report_file),
        media_type="text/html",
        filename=f"backtest_{job_id}_report.html",
    )
