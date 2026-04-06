"""Pydantic schemas for live trading API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class LiveStartRequest(BaseModel):
    """Request schema for starting a live or paper trading deployment."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    paper_trading: bool = True


class LiveStopRequest(BaseModel):
    """Request schema for stopping a running deployment."""

    deployment_id: UUID


class LiveDeploymentInfo(BaseModel):
    """Summary of a single live deployment."""

    id: UUID
    strategy_id: UUID
    status: str
    paper_trading: bool
    instruments: list[str]
    started_at: datetime | None = None
    stopped_at: datetime | None = None

    model_config = {"from_attributes": True}


class LiveStatusResponse(BaseModel):
    """Response schema for the live status endpoint."""

    deployments: list[LiveDeploymentInfo]
    risk_halted: bool
    active_count: int


class LiveKillAllResponse(BaseModel):
    """Response schema for the kill-all emergency endpoint."""

    stopped: int
    risk_halted: bool


class LivePositionsResponse(BaseModel):
    """Response schema for current open positions."""

    positions: list[dict[str, Any]]


class LiveTradesResponse(BaseModel):
    """Response schema for recent live trade executions."""

    trades: list[dict[str, Any]]
    total: int
