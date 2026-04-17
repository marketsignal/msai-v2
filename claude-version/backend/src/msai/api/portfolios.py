"""Live-portfolio CRUD API router.

Delegates to :class:`PortfolioService` and :class:`RevisionService`
(``msai.services.live``) for all business logic. The router only
translates HTTP verbs into service calls and maps domain errors to
the appropriate HTTP status codes.

Separate from ``api/portfolio.py`` (singular) which handles the
backtest-portfolio domain (``Portfolio`` / ``PortfolioRun``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 -- FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user, resolve_user_id
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models import LivePortfolio
from msai.schemas.live_portfolio import (
    LivePortfolioAddStrategyRequest,
    LivePortfolioCreateRequest,
    LivePortfolioMemberResponse,
    LivePortfolioResponse,
    LivePortfolioRevisionResponse,
)
from msai.services.live.portfolio_service import (
    PortfolioService,
    StrategyNotGraduatedError,
)
from msai.services.live.revision_service import (
    EmptyCompositionError,
    NoDraftToSnapshotError,
    RevisionService,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/live-portfolios", tags=["live-portfolios"])


# ---------------------------------------------------------------------------
# POST /api/v1/live-portfolios -- create a live portfolio
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=LivePortfolioResponse,
)
async def create_live_portfolio(
    body: LivePortfolioCreateRequest,
    response: Response,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LivePortfolioResponse:
    """Create a new live portfolio (empty -- no strategies yet)."""
    user_id = await resolve_user_id(db, claims)
    svc = PortfolioService(db)
    portfolio = await svc.create_portfolio(
        name=body.name,
        description=body.description,
        created_by=user_id,
    )
    await db.commit()
    await db.refresh(portfolio)

    response.headers["Location"] = f"/api/v1/live-portfolios/{portfolio.id}"
    log.info("live_portfolio_created", portfolio_id=str(portfolio.id), name=body.name)
    return LivePortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# GET /api/v1/live-portfolios -- list all live portfolios
# ---------------------------------------------------------------------------


@router.get("", response_model=list[LivePortfolioResponse])
async def list_live_portfolios(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[LivePortfolioResponse]:
    """List all live portfolios ordered by creation time (newest first)."""
    result = await db.execute(
        select(LivePortfolio).order_by(LivePortfolio.created_at.desc())
    )
    rows = result.scalars().all()
    return [LivePortfolioResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/v1/live-portfolios/{portfolio_id} -- portfolio detail + active revision
# ---------------------------------------------------------------------------


@router.get("/{portfolio_id}", response_model=LivePortfolioResponse)
async def get_live_portfolio(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LivePortfolioResponse:
    """Get a single live portfolio by ID."""
    portfolio = await db.get(LivePortfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live portfolio {portfolio_id} not found",
        )
    return LivePortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# POST /api/v1/live-portfolios/{portfolio_id}/strategies -- add strategy to draft
# ---------------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/strategies",
    status_code=status.HTTP_201_CREATED,
    response_model=LivePortfolioMemberResponse,
)
async def add_strategy_to_portfolio(
    portfolio_id: UUID,
    body: LivePortfolioAddStrategyRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LivePortfolioMemberResponse:
    """Add a graduated strategy to the portfolio's draft revision.

    Creates a draft revision lazily if one does not exist yet.
    """
    # Verify portfolio exists
    portfolio = await db.get(LivePortfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live portfolio {portfolio_id} not found",
        )

    svc = PortfolioService(db)
    try:
        member = await svc.add_strategy(
            portfolio_id=portfolio_id,
            strategy_id=body.strategy_id,
            config=body.config,
            instruments=body.instruments,
            weight=body.weight,
        )
    except StrategyNotGraduatedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    await db.commit()
    await db.refresh(member)

    log.info(
        "live_portfolio_strategy_added",
        portfolio_id=str(portfolio_id),
        strategy_id=str(body.strategy_id),
    )
    return LivePortfolioMemberResponse.model_validate(member)


# ---------------------------------------------------------------------------
# POST /api/v1/live-portfolios/{portfolio_id}/snapshot -- freeze draft
# ---------------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/snapshot",
    status_code=status.HTTP_201_CREATED,
    response_model=LivePortfolioRevisionResponse,
)
async def snapshot_portfolio(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LivePortfolioRevisionResponse:
    """Freeze the portfolio's current draft into an immutable revision.

    Returns the frozen revision (or an existing one if the composition
    hash matches a previously frozen revision).
    """
    # Verify portfolio exists
    portfolio = await db.get(LivePortfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live portfolio {portfolio_id} not found",
        )

    svc = RevisionService(db)
    try:
        revision = await svc.snapshot(portfolio_id)
    except NoDraftToSnapshotError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except EmptyCompositionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await db.commit()
    await db.refresh(revision)

    log.info(
        "live_portfolio_snapshot_created",
        portfolio_id=str(portfolio_id),
        revision_id=str(revision.id),
        revision_number=revision.revision_number,
    )
    return LivePortfolioRevisionResponse.model_validate(revision)


# ---------------------------------------------------------------------------
# GET /api/v1/live-portfolios/{portfolio_id}/members -- draft members
# ---------------------------------------------------------------------------


@router.get(
    "/{portfolio_id}/members",
    response_model=list[LivePortfolioMemberResponse],
)
async def list_draft_members(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[LivePortfolioMemberResponse]:
    """List strategies in the portfolio's current draft revision.

    Returns an empty list if no draft exists yet.
    """
    # Verify portfolio exists
    portfolio = await db.get(LivePortfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live portfolio {portfolio_id} not found",
        )

    svc = PortfolioService(db)
    members = await svc.list_draft_members(portfolio_id)
    return [LivePortfolioMemberResponse.model_validate(m) for m in members]
