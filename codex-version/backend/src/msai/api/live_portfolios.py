from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.models import LivePortfolio, LivePortfolioRevision, LivePortfolioRevisionStrategy
from msai.schemas.live_portfolio import (
    LivePortfolioAddStrategyRequest,
    LivePortfolioCreateRequest,
    LivePortfolioResponse,
    LivePortfolioRevisionResponse,
    LivePortfolioRevisionStrategyResponse,
)
from msai.services.live import (
    EmptyCompositionError,
    NoDraftToSnapshotError,
    PortfolioService,
    RevisionService,
    StrategyNotGraduatedError,
)
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/live/portfolios", tags=["live-portfolios"])


@router.get("", response_model=list[LivePortfolioResponse])
async def list_live_portfolios(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
) -> list[LivePortfolioResponse]:
    service = PortfolioService(db)
    revision_service = RevisionService(db)
    portfolios = await service.list_portfolios(limit=limit)
    response: list[LivePortfolioResponse] = []
    for portfolio in portfolios:
        response.append(
            await _serialize_portfolio(
                portfolio,
                revision_service=revision_service,
                portfolio_service=service,
            )
        )
    return response


@router.post("", response_model=LivePortfolioResponse)
async def create_live_portfolio(
    payload: LivePortfolioCreateRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LivePortfolioResponse:
    service = PortfolioService(db)
    revision_service = RevisionService(db)
    created_by = await resolve_user_id_from_claims(db, claims)
    portfolio = await service.create_portfolio(
        name=payload.name,
        description=payload.description,
        created_by=created_by,
    )
    await db.commit()
    await db.refresh(portfolio)
    return await _serialize_portfolio(portfolio, revision_service=revision_service, portfolio_service=service)


@router.get("/{portfolio_id}", response_model=LivePortfolioResponse)
async def get_live_portfolio(
    portfolio_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LivePortfolioResponse:
    service = PortfolioService(db)
    revision_service = RevisionService(db)
    portfolio = await service.get_portfolio(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live portfolio not found")
    return await _serialize_portfolio(portfolio, revision_service=revision_service, portfolio_service=service)


@router.post("/{portfolio_id}/strategies", response_model=LivePortfolioRevisionStrategyResponse)
async def add_live_portfolio_strategy(
    portfolio_id: str,
    payload: LivePortfolioAddStrategyRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LivePortfolioRevisionStrategyResponse:
    service = PortfolioService(db)
    portfolio = await service.get_portfolio(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live portfolio not found")
    try:
        member = await service.add_strategy(
            portfolio_id=portfolio_id,
            strategy_id=payload.strategy_id,
            config=payload.config,
            instruments=payload.instruments,
            weight=payload.weight,
        )
        await db.commit()
        await db.refresh(member)
    except StrategyNotGraduatedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _serialize_member(member)


@router.post("/{portfolio_id}/snapshot", response_model=LivePortfolioRevisionResponse)
async def snapshot_live_portfolio(
    portfolio_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LivePortfolioRevisionResponse:
    revision_service = RevisionService(db)
    try:
        revision = await revision_service.snapshot(portfolio_id)
        await db.commit()
        await db.refresh(revision)
    except NoDraftToSnapshotError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except EmptyCompositionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _serialize_revision(revision)


@router.get("/{portfolio_id}/members", response_model=list[LivePortfolioRevisionStrategyResponse])
async def list_live_portfolio_members(
    portfolio_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LivePortfolioRevisionStrategyResponse]:
    service = PortfolioService(db)
    portfolio = await service.get_portfolio(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live portfolio not found")
    return [_serialize_member(member) for member in await service.list_draft_members(portfolio_id)]


async def _serialize_portfolio(
    portfolio: LivePortfolio,
    *,
    revision_service: RevisionService,
    portfolio_service: PortfolioService,
) -> LivePortfolioResponse:
    active_revision = await revision_service.get_active_revision(portfolio.id)
    draft_revision = await portfolio_service.get_current_draft(portfolio.id)
    return LivePortfolioResponse(
        id=portfolio.id,
        name=portfolio.name,
        description=portfolio.description,
        created_by=portfolio.created_by,
        created_at=portfolio.created_at.isoformat(),
        updated_at=portfolio.updated_at.isoformat(),
        active_revision=_serialize_revision(active_revision) if active_revision is not None else None,
        draft_revision=_serialize_revision(draft_revision) if draft_revision is not None else None,
    )


def _serialize_revision(revision: LivePortfolioRevision) -> LivePortfolioRevisionResponse:
    return LivePortfolioRevisionResponse(
        id=revision.id,
        portfolio_id=revision.portfolio_id,
        revision_number=revision.revision_number,
        composition_hash=revision.composition_hash,
        is_frozen=revision.is_frozen,
        created_at=revision.created_at.isoformat(),
        strategies=[_serialize_member(member) for member in list(revision.strategies or [])],
    )


def _serialize_member(member: LivePortfolioRevisionStrategy) -> LivePortfolioRevisionStrategyResponse:
    strategy = getattr(member, "strategy", None)
    return LivePortfolioRevisionStrategyResponse(
        id=member.id,
        revision_id=member.revision_id,
        strategy_id=member.strategy_id,
        strategy_name=getattr(strategy, "name", None),
        instruments=list(member.instruments),
        config=dict(member.config),
        weight=float(member.weight),
        order_index=member.order_index,
        created_at=member.created_at.isoformat(),
    )
