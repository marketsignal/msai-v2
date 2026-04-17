"""Pydantic schemas for live-portfolio CRUD API endpoints.

Separate from ``schemas/portfolio.py`` which serves the backtest-portfolio
(``Portfolio`` / ``PortfolioRun``) domain. These schemas map to the
``live_portfolios``, ``live_portfolio_revisions``, and
``live_portfolio_revision_strategies`` tables introduced in PR#2.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LivePortfolioCreateRequest(BaseModel):
    """POST body for creating a new live portfolio."""

    name: str = Field(max_length=128)
    description: str | None = None


class LivePortfolioAddStrategyRequest(BaseModel):
    """POST body for adding a strategy to a portfolio's draft revision."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str] = Field(min_length=1)
    weight: Decimal = Field(gt=0, le=1)


class LivePortfolioResponse(BaseModel):
    """Response schema for a single live portfolio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class LivePortfolioRevisionResponse(BaseModel):
    """Response schema for a portfolio revision (draft or frozen)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    revision_number: int
    composition_hash: str
    is_frozen: bool
    created_at: datetime


class LivePortfolioMemberResponse(BaseModel):
    """Response schema for a strategy member within a revision."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    weight: Decimal
    order_index: int
