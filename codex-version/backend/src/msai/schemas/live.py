from __future__ import annotations

from pydantic import BaseModel, Field


class LiveStartRequest(BaseModel):
    strategy_id: str
    config: dict = Field(default_factory=dict)
    instruments: list[str] = Field(min_length=1)
    paper_trading: bool = True


class PortfolioStartRequest(BaseModel):
    portfolio_revision_id: str
    account_id: str = Field(min_length=1)
    paper_trading: bool = True


class LiveStopRequest(BaseModel):
    deployment_id: str
