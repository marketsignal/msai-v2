"""Graduation pipeline API router -- manage strategy promotion through stages.

Provides CRUD for graduation candidates and enforced stage transitions with
an immutable audit trail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 -- FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user, resolve_user_id
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.schemas.graduation import (
    GraduationCandidateCreate,
    GraduationCandidateListResponse,
    GraduationCandidateResponse,
    GraduationStageUpdate,
    GraduationTransitionListResponse,
    GraduationTransitionResponse,
)
from msai.services.graduation import GraduationService, GraduationStageError

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/graduation", tags=["graduation"])

# Module-level singleton -- stateless service, safe to share.
_service = GraduationService()


# ---------------------------------------------------------------------------
# GET /candidates -- list graduation candidates
# ---------------------------------------------------------------------------


@router.get("/candidates", response_model=GraduationCandidateListResponse)
async def list_candidates(
    stage: str | None = Query(default=None, description="Filter by stage"),
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> GraduationCandidateListResponse:
    """List graduation candidates, optionally filtered by stage."""
    candidates = await _service.list_candidates(db, stage=stage, limit=limit)

    # Total count (with same filter)
    count_stmt = select(func.count()).select_from(GraduationCandidate)
    if stage is not None:
        count_stmt = count_stmt.where(GraduationCandidate.stage == stage)
    total: int = (await db.execute(count_stmt)).scalar_one()

    return GraduationCandidateListResponse(
        items=[GraduationCandidateResponse.model_validate(c) for c in candidates],
        total=total,
    )


# ---------------------------------------------------------------------------
# POST /candidates -- create a new graduation candidate
# ---------------------------------------------------------------------------


@router.post(
    "/candidates",
    status_code=status.HTTP_201_CREATED,
    response_model=GraduationCandidateResponse,
)
async def create_candidate(
    body: GraduationCandidateCreate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> GraduationCandidateResponse:
    """Create a new graduation candidate in the 'discovery' stage."""
    user_id = await resolve_user_id(db, claims)
    candidate = await _service.create_candidate(
        db,
        strategy_id=body.strategy_id,
        config=body.config,
        metrics=body.metrics,
        research_job_id=body.research_job_id,
        notes=body.notes,
        user_id=user_id,
    )
    await db.commit()
    await db.refresh(candidate)

    return GraduationCandidateResponse.model_validate(candidate)


# ---------------------------------------------------------------------------
# GET /candidates/{candidate_id} -- candidate detail
# ---------------------------------------------------------------------------


@router.get("/candidates/{candidate_id}", response_model=GraduationCandidateResponse)
async def get_candidate(
    candidate_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> GraduationCandidateResponse:
    """Return a single graduation candidate by ID."""
    try:
        candidate = await _service.get_candidate(db, candidate_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Candidate {candidate_id} not found",
        )
    return GraduationCandidateResponse.model_validate(candidate)


# ---------------------------------------------------------------------------
# POST /candidates/{candidate_id}/stage -- advance stage
# ---------------------------------------------------------------------------


@router.post(
    "/candidates/{candidate_id}/stage",
    response_model=GraduationCandidateResponse,
)
async def update_candidate_stage(
    candidate_id: UUID,
    body: GraduationStageUpdate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> GraduationCandidateResponse:
    """Advance a candidate to a new stage.

    Returns 422 if the transition is invalid, with the list of allowed transitions.
    """
    user_id = await resolve_user_id(db, claims)
    try:
        candidate = await _service.update_stage(
            db,
            candidate_id,
            new_stage=body.stage,
            reason=body.reason,
            user_id=user_id,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Candidate {candidate_id} not found",
        )
    except GraduationStageError as exc:
        # Include the current stage's allowed transitions in the error response
        current_candidate = await db.get(GraduationCandidate, candidate_id)
        current_stage = current_candidate.stage if current_candidate else "unknown"
        allowed = _service.get_allowed_transitions(current_stage)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "current_stage": current_stage,
                "allowed_transitions": allowed,
            },
        )

    await db.commit()
    await db.refresh(candidate)
    return GraduationCandidateResponse.model_validate(candidate)


# ---------------------------------------------------------------------------
# GET /candidates/{candidate_id}/transitions -- audit trail
# ---------------------------------------------------------------------------


@router.get(
    "/candidates/{candidate_id}/transitions",
    response_model=GraduationTransitionListResponse,
)
async def get_candidate_transitions(
    candidate_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> GraduationTransitionListResponse:
    """Return the full audit trail of stage transitions for a candidate."""
    # Verify candidate exists
    existing = await db.get(GraduationCandidate, candidate_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Candidate {candidate_id} not found",
        )

    transitions = await _service.get_transitions(db, candidate_id)
    return GraduationTransitionListResponse(
        items=[GraduationTransitionResponse.model_validate(t) for t in transitions],
        total=len(transitions),
    )
