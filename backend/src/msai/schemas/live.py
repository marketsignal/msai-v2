"""Pydantic schemas for live trading API endpoints."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from typing import Any
from uuid import UUID  # noqa: TC003 — Pydantic resolves annotations at runtime

from pydantic import BaseModel, Field


class LiveStartRequest(BaseModel):
    """Request schema for starting a live or paper trading deployment."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    paper_trading: bool = True


class PortfolioStartRequest(BaseModel):
    """Request schema for starting a portfolio-based live deployment.

    Instead of deploying a single strategy (like :class:`LiveStartRequest`),
    this deploys an entire frozen portfolio revision — a set of strategies
    with weights, configs, and instruments — to a specific IB account.
    """

    portfolio_revision_id: UUID
    account_id: str
    paper_trading: bool = True
    ib_login_key: str = Field(min_length=1, max_length=64)


class LiveStopRequest(BaseModel):
    """Request schema for stopping a running deployment."""

    deployment_id: UUID


class LiveDeploymentInfo(BaseModel):
    """Summary of a single live deployment."""

    id: UUID
    strategy_id: UUID | None = None
    status: str
    paper_trading: bool
    instruments: list[str] = []
    started_at: datetime | None = None
    stopped_at: datetime | None = None

    model_config = {"from_attributes": True}


class LiveStatusResponse(BaseModel):
    """Response schema for the live status endpoint."""

    deployments: list[LiveDeploymentInfo]
    risk_halted: bool
    active_count: int


class LiveDeploymentStatusResponse(BaseModel):
    """Response schema for ``GET /api/v1/live/status/{deployment_id}``.

    Combines the stable ``LiveDeployment`` row (logical record — survives
    restarts, keyed by ``identity_signature``) with the most recent
    ``LiveNodeProcess`` row (per-restart run record — pid, heartbeat,
    terminal outcome). Returning both lets the UI show "this deployment
    is running as pid 12345 on host box-3 with last heartbeat 1.2 s ago"
    without hitting the supervisor directly.

    Process fields (``pid``, ``host``, ``process_status``, etc.) are
    nullable because a deployment that has never run (or whose newest
    process row has been garbage-collected) has no live row.
    """

    # Logical deployment fields
    id: UUID
    strategy_id: UUID | None = None
    deployment_slug: str
    status: str
    paper_trading: bool
    instruments: list[str] = []
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None

    # Latest per-run process fields — nullable when no live_node_processes
    # row exists for this deployment.
    process_id: UUID | None = None
    pid: int | None = None
    host: str | None = None
    process_status: str | None = None
    last_heartbeat_at: datetime | None = None
    exit_code: int | None = None
    error_message: str | None = None
    failure_kind: str | None = None

    model_config = {"from_attributes": True}


class LiveKillAllResponse(BaseModel):
    """Response schema for the kill-all emergency endpoint.

    ``stopped`` is the count of stop commands SUCCESSFULLY
    published to the supervisor command bus. ``failed_publish``
    is the count of active deployments where the publish
    raised — these are NOT acknowledged by the supervisor and
    require manual intervention. ``risk_halted`` is always True
    after a kill-all because the persistent halt flag is set
    unconditionally as Layer 1, BEFORE any publishes. Codex
    batch 9 P1: an emergency-stop endpoint must NOT silently
    swallow failures — operators need to see them.
    """

    stopped: int
    failed_publish: int = 0
    risk_halted: bool


class LiveResumeResponse(BaseModel):
    """Response schema for the resume endpoint that clears the
    persistent halt flag set by ``/kill-all``."""

    resumed: bool


class StrategyMemberInfo(BaseModel):
    """Per-strategy member detail within a portfolio deployment."""

    strategy_id: UUID
    strategy_id_full: str
    instruments: list[str]
    weight: str

    model_config = {"from_attributes": True}


class PortfolioDeploymentInfo(BaseModel):
    """Summary of a portfolio-based deployment with per-member detail."""

    id: UUID
    portfolio_revision_id: UUID | None = None
    account_id: str
    status: str
    paper_trading: bool
    deployment_slug: str
    members: list[StrategyMemberInfo] = []

    model_config = {"from_attributes": True}


class LivePositionsResponse(BaseModel):
    """Response schema for current open positions."""

    positions: list[dict[str, Any]]


class LiveTradesResponse(BaseModel):
    """Response schema for recent live trade executions."""

    trades: list[dict[str, Any]]
    total: int
