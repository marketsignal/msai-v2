from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

GraduationStage = Literal[
    "paper_candidate",
    "paper_running",
    "paper_review",
    "live_candidate",
    "live_running",
    "paused",
    "archived",
]


class GraduationCandidateCreateRequest(BaseModel):
    promotion_id: str
    notes: str | None = None


class GraduationCandidateStageRequest(BaseModel):
    stage: GraduationStage
    notes: str | None = None


class GraduationCandidateResponse(BaseModel):
    id: str
    promotion_id: str
    report_id: str
    created_at: str
    updated_at: str
    created_by: str | None = None
    stage: GraduationStage
    notes: str | None = None
    strategy_id: str
    strategy_name: str
    strategy_path: str
    instruments: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    selection: dict[str, Any] = Field(default_factory=dict)
    paper_trading: bool = True
    live_url: str
    portfolio_url: str
