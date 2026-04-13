"""Pydantic schemas for graduation pipeline API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GraduationCandidateCreate(BaseModel):
    """Request schema for creating a graduation candidate."""

    strategy_id: UUID
    research_job_id: UUID | None = None
    config: dict[str, Any]
    metrics: dict[str, Any]
    notes: str | None = None


class GraduationStageUpdate(BaseModel):
    """Request schema for advancing a candidate to the next stage."""

    stage: str
    reason: str | None = None


class GraduationCandidateResponse(BaseModel):
    """Response schema for a single graduation candidate."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    strategy_id: UUID
    research_job_id: UUID | None
    stage: str
    config: dict[str, Any]
    metrics: dict[str, Any]
    deployment_id: UUID | None
    notes: str | None
    promoted_by: UUID | None
    promoted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GraduationCandidateListResponse(BaseModel):
    """Paginated list response for graduation candidates."""

    items: list[GraduationCandidateResponse]
    total: int


class GraduationTransitionResponse(BaseModel):
    """Response schema for a single graduation stage transition."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    candidate_id: UUID
    from_stage: str
    to_stage: str
    reason: str | None
    transitioned_by: UUID | None
    created_at: datetime


class GraduationTransitionListResponse(BaseModel):
    """Paginated list response for graduation transitions."""

    items: list[GraduationTransitionResponse]
    total: int
