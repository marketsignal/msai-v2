from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class LivePortfolioCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class LivePortfolioAddStrategyRequest(BaseModel):
    strategy_id: str
    config: dict = Field(default_factory=dict)
    instruments: list[str] = Field(min_length=1)
    weight: Decimal = Field(gt=0, le=1)


class LivePortfolioRevisionStrategyResponse(BaseModel):
    id: str
    revision_id: str
    strategy_id: str
    strategy_name: str | None = None
    instruments: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    weight: float
    order_index: int
    created_at: str


class LivePortfolioRevisionResponse(BaseModel):
    id: str
    portfolio_id: str
    revision_number: int
    composition_hash: str
    is_frozen: bool
    created_at: str
    strategies: list[LivePortfolioRevisionStrategyResponse] = Field(default_factory=list)


class LivePortfolioResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str
    active_revision: LivePortfolioRevisionResponse | None = None
    draft_revision: LivePortfolioRevisionResponse | None = None
