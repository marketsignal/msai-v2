"""Pydantic request/response schemas for Symbol Onboarding.

Contract pins (council-ratified 2026-04-24):
- ``asset_class`` restricted to registry taxonomy: equity | futures | fx | option.
- ``end >= start``, ``start <= today`` enforced via model_validator.
- 100-symbol hard cap at the API layer.
- ``cost_ceiling_usd`` is the operator's hard spend stop, not an estimate.
- ``request_live_qualification`` is the request-side flag (distinct from
  the readiness-side ``live_qualified`` boolean).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_SYMBOLS_PER_BATCH = 100


class SymbolStepStatus(StrEnum):
    PENDING = "pending"
    BOOTSTRAP = "bootstrap"
    INGEST = "ingest"
    COVERAGE = "coverage"
    IB_QUALIFY = "ib_qualify"
    COMPLETED = "completed"
    IB_SKIPPED = "ib_skipped"
    COVERAGE_FAILED = "coverage_failed"


class SymbolStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


AssetClass = Literal["equity", "futures", "fx", "option"]


class OnboardSymbolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass
    start: date
    end: date

    @field_validator("symbol")
    @classmethod
    def _symbol_regex(cls, v: str) -> str:
        if not _SYMBOL_PATTERN.match(v):
            raise ValueError(f"symbol {v!r} does not match {_SYMBOL_PATTERN.pattern!r}")
        return v

    @model_validator(mode="after")
    def _dates_coherent(self) -> OnboardSymbolSpec:
        if self.end < self.start:
            raise ValueError(f"end must be >= start (got start={self.start}, end={self.end})")
        if self.start > date.today():
            raise ValueError(f"start must be <= today (got {self.start})")
        return self


class OnboardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watchlist_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9\-]+$")
    symbols: list[OnboardSymbolSpec] = Field(min_length=1, max_length=_MAX_SYMBOLS_PER_BATCH)
    request_live_qualification: bool = False
    cost_ceiling_usd: Decimal | None = Field(default=None, max_digits=12, decimal_places=2, ge=0)


class SymbolStateRow(BaseModel):
    """Per-symbol progress state as it appears in ``status`` responses."""

    symbol: str
    asset_class: AssetClass
    start: date
    end: date
    status: SymbolStatus
    step: SymbolStepStatus
    error: dict[str, Any] | None = None
    next_action: str | None = None


class OnboardProgress(BaseModel):
    total: int
    succeeded: int
    failed: int
    in_progress: int
    not_started: int


class OnboardResponse(BaseModel):
    """202 response body for ``POST /onboard``."""

    run_id: UUID
    watchlist_name: str
    status: RunStatus


class StatusResponse(BaseModel):
    run_id: UUID
    watchlist_name: str
    status: RunStatus
    progress: OnboardProgress
    per_symbol: list[SymbolStateRow]
    estimated_cost_usd: Decimal | None
    actual_cost_usd: Decimal | None


class DryRunResponse(BaseModel):
    watchlist_name: str
    dry_run: Literal[True] = True
    estimated_cost_usd: Decimal
    estimate_basis: str
    estimate_confidence: Literal["high", "medium", "low"]
    symbol_count: int
    breakdown: list[dict[str, Any]]


class ReadinessResponse(BaseModel):
    """Window-scoped per-instrument readiness (pin #3 amendment)."""

    instrument_uid: UUID
    registered: bool
    provider: str
    backtest_data_available: bool | None
    coverage_status: Literal["full", "gapped", "none"] | None
    covered_range: str | None
    missing_ranges: list[dict[str, Any]] = []
    live_qualified: bool
    coverage_summary: str | None = None
