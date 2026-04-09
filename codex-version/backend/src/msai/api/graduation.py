from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.models import Strategy
from msai.schemas.graduation import (
    GraduationCandidateCreateRequest,
    GraduationCandidateResponse,
    GraduationCandidateStageRequest,
)
from msai.services.graduation_service import (
    GraduationCandidateNotFoundError,
    GraduationService,
    GraduationStageError,
)
from msai.services.research_artifacts import (
    ResearchArtifactNotFoundError,
    ResearchArtifactService,
)
from msai.services.strategy_registry import StrategyRegistry
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/graduation", tags=["graduation"])
graduation_service = GraduationService()
artifact_service = ResearchArtifactService()


@router.get("/candidates", response_model=list[GraduationCandidateResponse])
async def list_graduation_candidates(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 100,
) -> list[GraduationCandidateResponse]:
    bounded_limit = max(1, min(limit, 250))
    return [GraduationCandidateResponse(**row) for row in graduation_service.list_candidates(limit=bounded_limit)]


@router.post("/candidates", response_model=GraduationCandidateResponse)
async def create_graduation_candidate(
    payload: GraduationCandidateCreateRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GraduationCandidateResponse:
    try:
        promotion = artifact_service.load_promotion(payload.promotion_id)
    except ResearchArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    strategy = await db.get(Strategy, str(promotion["strategy_id"]))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    registry = StrategyRegistry(settings.strategies_root)
    strategy_path = registry.resolve_path(strategy)
    user_id = await resolve_user_id_from_claims(db, claims)
    candidate = graduation_service.create_candidate(
        promotion=promotion,
        strategy_path=str(strategy_path),
        created_by=user_id,
        notes=payload.notes,
    )
    await db.commit()
    return GraduationCandidateResponse(**candidate)


@router.get("/candidates/{candidate_id}", response_model=GraduationCandidateResponse)
async def get_graduation_candidate(
    candidate_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> GraduationCandidateResponse:
    try:
        candidate = graduation_service.load_candidate(candidate_id)
    except GraduationCandidateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return GraduationCandidateResponse(**candidate)


@router.post("/candidates/{candidate_id}/stage", response_model=GraduationCandidateResponse)
async def update_graduation_candidate_stage(
    candidate_id: str,
    payload: GraduationCandidateStageRequest,
    _: Mapping[str, object] = Depends(get_current_user),
) -> GraduationCandidateResponse:
    try:
        candidate = graduation_service.update_stage(
            candidate_id,
            stage=payload.stage,
            notes=payload.notes,
        )
    except GraduationCandidateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except GraduationStageError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return GraduationCandidateResponse(**candidate)
