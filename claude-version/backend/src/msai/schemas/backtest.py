"""Pydantic schemas for backtest API endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

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

    model_config = {"from_attributes": True}


class BacktestListResponse(BaseModel):
    """Paginated list response for backtests."""

    items: list[BacktestListItem]
    total: int
