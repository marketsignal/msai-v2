"""Pydantic schemas for portfolio management API endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PortfolioAllocationInput(BaseModel):
    """Input schema for a single allocation within a portfolio."""

    candidate_id: UUID
    weight: float = Field(ge=0.0, le=1.0)


class PortfolioCreate(BaseModel):
    """Request schema for creating a new portfolio."""

    name: str = Field(max_length=128)
    description: str | None = None
    objective: str  # maximize_sharpe, equal_weight, manual
    base_capital: float
    requested_leverage: float = 1.0
    benchmark_symbol: str | None = None
    allocations: list[PortfolioAllocationInput]


class PortfolioResponse(BaseModel):
    """Response schema for a single portfolio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    objective: str
    base_capital: float
    requested_leverage: float
    benchmark_symbol: str | None
    account_id: str | None
    created_at: datetime
    updated_at: datetime


class PortfolioListResponse(BaseModel):
    """Paginated list response for portfolios."""

    items: list[PortfolioResponse]
    total: int


class PortfolioRunCreate(BaseModel):
    """Request schema for launching a portfolio backtest run."""

    start_date: date
    end_date: date
    max_parallelism: int | None = None


class PortfolioRunResponse(BaseModel):
    """Response schema for a single portfolio backtest run."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    portfolio_id: UUID
    status: str
    metrics: dict[str, Any] | None
    report_path: str | None
    start_date: date
    end_date: date
    created_at: datetime
    completed_at: datetime | None


class PortfolioRunListResponse(BaseModel):
    """Paginated list response for portfolio runs."""

    items: list[PortfolioRunResponse]
    total: int
