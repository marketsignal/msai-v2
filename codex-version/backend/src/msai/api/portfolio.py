from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.schemas.portfolio import (
    PortfolioDefinitionCreateRequest,
    PortfolioDefinitionResponse,
    PortfolioRunRequest,
    PortfolioRunResponse,
)
from msai.services.portfolio_service import (
    PortfolioAllocationInput,
    PortfolioDefinitionError,
    PortfolioDefinitionNotFoundError,
    PortfolioRunNotFoundError,
    PortfolioService,
)
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/portfolios", tags=["portfolios"])
portfolio_service = PortfolioService()


@router.get("", response_model=list[PortfolioDefinitionResponse])
async def list_portfolios(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 100,
) -> list[PortfolioDefinitionResponse]:
    bounded_limit = max(1, min(limit, 250))
    return [PortfolioDefinitionResponse(**row) for row in portfolio_service.list_definitions(limit=bounded_limit)]


@router.post("", response_model=PortfolioDefinitionResponse)
async def create_portfolio(
    payload: PortfolioDefinitionCreateRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PortfolioDefinitionResponse:
    try:
        user_id = await resolve_user_id_from_claims(db, claims)
        definition = portfolio_service.create_definition(
            name=payload.name,
            description=payload.description,
            allocations=[
                PortfolioAllocationInput(candidate_id=row.candidate_id, weight=row.weight)
                for row in payload.allocations
            ],
            created_by=user_id,
            objective=payload.objective,
            base_capital=payload.base_capital,
            requested_leverage=payload.requested_leverage,
            downside_target=payload.downside_target,
            benchmark_symbol=payload.benchmark_symbol,
        )
        await db.commit()
    except PortfolioDefinitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return PortfolioDefinitionResponse(**definition)


@router.post("/{portfolio_id}/runs", response_model=PortfolioRunResponse)
async def create_portfolio_run(
    portfolio_id: str,
    payload: PortfolioRunRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PortfolioRunResponse:
    try:
        user_id = await resolve_user_id_from_claims(db, claims)
        run = portfolio_service.create_run(
            portfolio_id=portfolio_id,
            start_date=payload.start_date.isoformat(),
            end_date=payload.end_date.isoformat(),
            created_by=user_id,
            max_parallelism=payload.max_parallelism,
        )
    except PortfolioDefinitionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    pool = await get_redis_pool()
    queue_job_id = await enqueue_portfolio_run(pool, run["id"])
    run = portfolio_service.mark_run_enqueued(
        run["id"],
        queue_name=settings.portfolio_queue_name,
        queue_job_id=queue_job_id or run["id"],
    )
    await db.commit()
    return PortfolioRunResponse(**run)


@router.get("/runs/{run_id}", response_model=PortfolioRunResponse)
async def get_portfolio_run(
    run_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> PortfolioRunResponse:
    try:
        run = portfolio_service.load_run(run_id)
    except PortfolioRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PortfolioRunResponse(**run)


@router.get("/runs", response_model=list[PortfolioRunResponse])
async def list_portfolio_runs(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 100,
) -> list[PortfolioRunResponse]:
    bounded_limit = max(1, min(limit, 250))
    return [PortfolioRunResponse(**row) for row in portfolio_service.list_runs(limit=bounded_limit)]


@router.get("/{portfolio_id}", response_model=PortfolioDefinitionResponse)
async def get_portfolio(
    portfolio_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> PortfolioDefinitionResponse:
    try:
        definition = portfolio_service.load_definition(portfolio_id)
    except PortfolioDefinitionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PortfolioDefinitionResponse(**definition)


@router.get("/runs/{run_id}/report")
async def get_portfolio_run_report(
    run_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> FileResponse:
    try:
        run = portfolio_service.load_run(run_id)
    except PortfolioRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    report_path = run.get("report_path")
    if not report_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio report not available")
    return FileResponse(report_path, media_type="text/html")
