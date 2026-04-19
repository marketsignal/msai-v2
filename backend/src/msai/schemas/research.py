"""Pydantic schemas for research sweep and walk-forward API endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResearchSweepRequest(BaseModel):
    """Request schema for launching a parameter sweep job."""

    strategy_id: UUID
    instruments: list[str]
    start_date: date
    end_date: date
    asset_class: str = "stocks"
    base_config: dict[str, Any] = Field(default_factory=dict)
    parameter_grid: dict[str, list[Any]]
    objective: str = "sharpe"
    max_parallelism: int | None = None
    search_strategy: str = "auto"
    min_trades: int | None = None
    require_positive_return: bool = False
    holdout_fraction: float | None = None
    holdout_days: int | None = None
    purge_days: int = 5


class ResearchWalkForwardRequest(ResearchSweepRequest):
    """Request schema for launching a walk-forward optimization job."""

    train_days: int
    test_days: int
    step_days: int | None = None
    mode: str = "rolling"


class ResearchJobResponse(BaseModel):
    """Response schema for a research job summary."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    strategy_id: UUID
    job_type: str
    status: str
    progress: int
    progress_message: str | None
    best_config: dict[str, Any] | None
    best_metrics: dict[str, Any] | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ResearchJobListResponse(BaseModel):
    """Paginated list response for research jobs."""

    items: list[ResearchJobResponse]
    total: int


class ResearchTrialResponse(BaseModel):
    """Response schema for a single research trial within a job."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trial_number: int
    config: dict[str, Any]
    metrics: dict[str, Any] | None
    status: str
    objective_value: float | None
    backtest_id: UUID | None
    created_at: datetime


class ResearchJobDetailResponse(ResearchJobResponse):
    """Extended response schema for a research job including trials."""

    config: dict[str, Any]
    results: dict[str, Any] | None
    trials: list[ResearchTrialResponse] = Field(default_factory=list)


class ResearchPromotionRequest(BaseModel):
    """Request schema for promoting a research result to graduation."""

    research_job_id: UUID
    trial_index: int | None = None
    notes: str | None = None


class ResearchPromotionResponse(BaseModel):
    """Response schema after promoting a research result."""

    candidate_id: UUID
    stage: str
    message: str
