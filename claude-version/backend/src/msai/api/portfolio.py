"""Portfolio management API router -- create, list, and run portfolio backtests.

Manages portfolios of graduated strategies with weighted capital allocation
and combined backtest runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 -- FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user, resolve_user_id
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.schemas.portfolio import (
    PortfolioCreate,
    PortfolioListResponse,
    PortfolioResponse,
    PortfolioRunCreate,
    PortfolioRunListResponse,
    PortfolioRunResponse,
)
from msai.services.portfolio_service import PortfolioService

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/portfolios", tags=["portfolios"])

# Module-level singleton -- stateless service, safe to share.
_service = PortfolioService()


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios -- list portfolios
# ---------------------------------------------------------------------------


@router.get("", response_model=PortfolioListResponse)
async def list_portfolios(
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioListResponse:
    """List portfolios ordered by creation time (newest first)."""
    portfolios = await _service.list(db, limit=limit)
    total = await _service.count(db)

    return PortfolioListResponse(
        items=[PortfolioResponse.model_validate(p) for p in portfolios],
        total=total,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/portfolios -- create a portfolio
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioResponse,
)
async def create_portfolio(
    body: PortfolioCreate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioResponse:
    """Create a new portfolio with weighted strategy allocations."""
    user_id = await resolve_user_id(db, claims)
    portfolio = await _service.create(db, body, user_id=user_id)
    await db.commit()
    await db.refresh(portfolio)

    return PortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs -- list all portfolio runs
# IMPORTANT: Static /runs routes MUST be registered before /{portfolio_id}
# to avoid FastAPI matching "runs" as a UUID path parameter.
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=PortfolioRunListResponse)
async def list_portfolio_runs(
    portfolio_id: UUID | None = Query(default=None, description="Filter by portfolio"),
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunListResponse:
    """List portfolio runs, optionally filtered by portfolio ID."""
    runs = await _service.list_runs(db, portfolio_id=portfolio_id, limit=limit)
    total = await _service.count_runs(db, portfolio_id=portfolio_id)

    return PortfolioRunListResponse(
        items=[PortfolioRunResponse.model_validate(r) for r in runs],
        total=total,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs/{run_id} -- run detail
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=PortfolioRunResponse)
async def get_portfolio_run(
    run_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Return a single portfolio run by ID."""
    try:
        run = await _service.get_run(db, run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio run {run_id} not found",
        )
    return PortfolioRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs/{run_id}/report -- download HTML report
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/report")
async def get_portfolio_run_report(
    run_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse:
    """Download the generated HTML report for a completed portfolio run."""
    try:
        run = await _service.get_run(db, run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio run {run_id} not found",
        )

    if run.report_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No report available for portfolio run {run_id}",
        )

    # Path traversal protection: ensure resolved path is within expected directory
    report_file = Path(run.report_path).resolve()
    expected_dir = (Path(settings.data_root) / "reports").resolve()
    if not str(report_file).startswith(str(expected_dir)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid report path",
        )

    if not report_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file not found on disk for portfolio run {run_id}",
        )

    return FileResponse(
        path=str(report_file),
        media_type="text/html",
        filename=f"portfolio_run_{run_id}_report.html",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/{portfolio_id} -- portfolio detail
# ---------------------------------------------------------------------------


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioResponse:
    """Return a single portfolio by ID."""
    try:
        portfolio = await _service.get(db, portfolio_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio {portfolio_id} not found",
        )
    return PortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# POST /api/v1/portfolios/{portfolio_id}/runs -- start a portfolio run
# ---------------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioRunResponse,
)
async def create_portfolio_run(
    portfolio_id: UUID,
    body: PortfolioRunCreate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Create a portfolio backtest run and enqueue it for execution."""
    user_id = await resolve_user_id(db, claims)

    try:
        run = await _service.create_run(db, portfolio_id, body, user_id=user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio {portfolio_id} not found",
        )

    # Enqueue to arq BEFORE commit -- if enqueue fails, rollback the row
    try:
        pool = await get_redis_pool()
        await enqueue_portfolio_run(pool, str(run.id), str(portfolio_id))
    except Exception as exc:
        await db.rollback()
        log.error("portfolio_run_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue portfolio run job -- Redis may be unavailable",
        ) from exc

    await db.commit()
    await db.refresh(run)

    log.info(
        "portfolio_run_enqueued",
        run_id=str(run.id),
        portfolio_id=str(portfolio_id),
    )
    return PortfolioRunResponse.model_validate(run)
