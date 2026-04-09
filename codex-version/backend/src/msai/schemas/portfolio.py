from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

PortfolioObjective = Literal["equal_weight", "maximize_profit", "maximize_sharpe", "maximize_sortino", "manual"]


class PortfolioAllocationRequest(BaseModel):
    candidate_id: str
    weight: float | None = Field(default=None, ge=0.0)


class PortfolioDefinitionCreateRequest(BaseModel):
    name: str
    description: str | None = None
    allocations: list[PortfolioAllocationRequest] = Field(min_length=1)
    objective: PortfolioObjective = "equal_weight"
    base_capital: float = Field(gt=0)
    requested_leverage: float = Field(default=1.0, gt=0)
    downside_target: float | None = Field(default=None, gt=0)
    benchmark_symbol: str | None = None


class PortfolioDefinitionResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str
    objective: PortfolioObjective
    base_capital: float
    requested_leverage: float
    downside_target: float | None = None
    benchmark_symbol: str | None = None
    allocations: list[dict[str, Any]] = Field(default_factory=list)


class PortfolioRunRequest(BaseModel):
    start_date: date
    end_date: date
    max_parallelism: int | None = Field(default=None, ge=1, le=16)


class PortfolioRunResponse(BaseModel):
    id: str
    portfolio_id: str
    portfolio_name: str
    created_by: str | None = None
    created_at: str
    updated_at: str
    status: str
    start_date: str
    end_date: str
    max_parallelism: int | None = None
    error_message: str | None = None
    metrics: dict[str, Any] | None = None
    series: list[dict[str, Any]] = Field(default_factory=list)
    allocations: list[dict[str, Any]] = Field(default_factory=list)
    report_path: str | None = None
    queue_name: str | None = None
    queue_job_id: str | None = None
    worker_id: str | None = None
    attempt: int = 0
    heartbeat_at: str | None = None
