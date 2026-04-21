"""Pydantic schemas for backtest API endpoints."""

from __future__ import annotations

from datetime import (  # noqa: TC003 — Pydantic v2 needs concrete types at model build time
    date,
    datetime,
)
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 — same reason

from pydantic import BaseModel


class BacktestRunRequest(BaseModel):
    """Request schema for launching a new backtest."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    start_date: date
    end_date: date


class BacktestStatusResponse(BaseModel):
    """Response schema for backtest status polling."""

    id: UUID
    status: str
    progress: int
    started_at: datetime | None
    completed_at: datetime | None
    error: ErrorEnvelope | None = None
    # --- Auto-heal lifecycle (added by backtest-auto-ingest PR, Task B9) ---
    # When an auto-heal cycle is in flight, ``phase == "awaiting_data"`` and
    # ``progress_message`` carries the user-facing "Downloading ..." text.
    # Both are ``None`` outside heal windows. The endpoint keeps
    # ``response_model_exclude_none=True`` so older clients that don't know
    # these keys see no-op-absent behaviour (backward compat with PR #39).
    # Extension point: currently the only non-None value is "awaiting_data".
    # Future phase values (e.g., "finalizing", "generating_report") can be
    # added without a schema break — clients already handle None.
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None

    model_config = {"from_attributes": True}


class BacktestResultsResponse(BaseModel):
    """Response schema for backtest results.

    Includes the aggregate metrics dict (may be ``None`` until the job
    completes) and the full list of individual trades so the UI can
    render the trade log in one round-trip.
    """

    id: UUID
    metrics: dict[str, Any] | None = None
    trade_count: int
    trades: list[dict[str, Any]] = []

    model_config = {"from_attributes": True}


class BacktestListItem(BaseModel):
    """Summary schema for a backtest in list responses."""

    id: UUID
    strategy_id: UUID
    status: str
    start_date: date
    end_date: date
    created_at: datetime
    error_code: str | None = None
    error_public_message: str | None = None
    # See :class:`BacktestStatusResponse.phase` for semantics.
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None

    model_config = {"from_attributes": True}


class BacktestListResponse(BaseModel):
    """Paginated list response for backtests."""

    items: list[BacktestListItem]
    total: int


# ---------------------------------------------------------------------------
# Error envelope (failure-surfacing PR)
# ---------------------------------------------------------------------------


class Remediation(BaseModel):
    """Machine-readable remediation metadata.

    MVP-only ``kind == 'ingest_data'`` carries full fields. Other kinds
    stay minimal in this PR; the follow-up auto-ingest PR flips
    ``auto_available`` to ``True`` for the kinds it can handle.

    Keep ``kind`` as ``Literal[...]`` so OpenAPI emits a proper
    ``enum`` and client-side type-narrowing works without loading a
    separate enum module.
    """

    kind: Literal["ingest_data", "contact_support", "retry", "none"]
    symbols: list[str] | None = None
    asset_class: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    auto_available: bool = False


class ErrorEnvelope(BaseModel):
    """Structured failure payload surfaced on `BacktestStatusResponse.error`.

    Deliberately symmetric with the api-design.md error envelope used by
    the 422 path on ``POST /backtests/run`` (see PR #38): same
    ``{code, message, ...}`` top-level shape so UI / CLI can share
    rendering helpers.
    """

    code: str
    message: str
    suggested_action: str | None = None
    remediation: Remediation | None = None
