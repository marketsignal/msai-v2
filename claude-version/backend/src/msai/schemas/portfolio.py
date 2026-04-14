"""Pydantic schemas for portfolio management API endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from msai.models.portfolio_enums import PortfolioObjective, PortfolioRunStatus

# Legacy alias — pre-port rows stored the pre-rename spelling.  Translate
# on read so the API layer keeps working without a data migration.
_OBJECTIVE_LEGACY_ALIASES: dict[str, str] = {"max_sharpe": "maximize_sharpe"}


def _normalize_objective(raw: object) -> object:
    if isinstance(raw, str):
        return _OBJECTIVE_LEGACY_ALIASES.get(raw, raw)
    return raw


class PortfolioAllocationInput(BaseModel):
    """Input schema for a single allocation within a portfolio.

    ``weight`` is optional — when omitted, the portfolio service derives a
    heuristic weight from the candidate's metrics according to the portfolio
    ``objective`` (e.g. Sharpe-weighted for ``maximize_sharpe``).  A zero
    weight is rejected; use ``None`` (omit the field) to request heuristic
    derivation, or remove the allocation entirely to exclude the candidate.
    """

    candidate_id: UUID
    # ``gt=0.0`` disambiguates "no explicit weight" (None) from "exclude this
    # candidate" (which callers should encode by omitting the allocation).
    weight: float | None = Field(default=None, gt=0.0, le=1.0)


class PortfolioCreate(BaseModel):
    """Request schema for creating a new portfolio."""

    name: str = Field(max_length=128)
    description: str | None = None
    objective: PortfolioObjective
    base_capital: float = Field(gt=0.0)
    requested_leverage: float = Field(default=1.0, ge=0.1, le=10.0)
    # Downside-target is a risk-scaling cap; values <= 0 would silently pin
    # leverage to the safety floor and misrepresent intent.
    downside_target: float | None = Field(default=None, gt=0.0)
    benchmark_symbol: str | None = Field(default=None, max_length=32)
    # Non-empty: orchestration deterministically fails on empty portfolios
    # (nothing to weight, nothing to backtest), so reject at the boundary
    # rather than accepting a payload that every subsequent ``/runs``
    # call would fail on.
    allocations: list[PortfolioAllocationInput] = Field(min_length=1)

    @field_validator("objective", mode="before")
    @classmethod
    def _translate_legacy_objective(cls, value: object) -> object:
        return _normalize_objective(value)

    @model_validator(mode="after")
    def _manual_objective_requires_explicit_weights(self) -> PortfolioCreate:
        # ``objective="manual"`` promises the operator will set each
        # weight.  If any allocation omits the weight, the service layer
        # silently falls through to equal-weight heuristic — not what
        # the caller asked for.  Fail at the boundary so the caller
        # learns about the mismatch immediately.
        if self.objective is PortfolioObjective.MANUAL:
            missing = [
                str(alloc.candidate_id) for alloc in self.allocations if alloc.weight is None
            ]
            if missing:
                raise ValueError(
                    "objective=manual requires an explicit weight on every "
                    f"allocation; missing weight for candidates: {missing}"
                )
        return self


class PortfolioResponse(BaseModel):
    """Response schema for a single portfolio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    objective: PortfolioObjective
    base_capital: float
    requested_leverage: float
    downside_target: float | None
    benchmark_symbol: str | None
    account_id: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("objective", mode="before")
    @classmethod
    def _translate_legacy_objective(cls, value: object) -> object:
        # Translate pre-rename "max_sharpe" rows transparently — strict
        # enum validation would otherwise 500 on GET for legacy data.
        return _normalize_objective(value)


class PortfolioListResponse(BaseModel):
    """Paginated list response for portfolios."""

    items: list[PortfolioResponse]
    total: int


class PortfolioRunCreate(BaseModel):
    """Request schema for launching a portfolio backtest run."""

    start_date: date
    end_date: date
    # Bounded to guard against runaway thread-pool sizes; the worker clamps
    # further via ``compute_slots`` global limits.
    max_parallelism: int | None = Field(default=None, ge=1, le=32)


class PortfolioRunResponse(BaseModel):
    """Response schema for a single portfolio backtest run."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    portfolio_id: UUID
    status: PortfolioRunStatus
    metrics: dict[str, Any] | None
    series: list[dict[str, Any]] | None
    allocations: list[dict[str, Any]] | None
    report_path: str | None
    start_date: date
    end_date: date
    max_parallelism: int | None
    error_message: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class PortfolioRunListResponse(BaseModel):
    """Paginated list response for portfolio runs."""

    items: list[PortfolioRunResponse]
    total: int
