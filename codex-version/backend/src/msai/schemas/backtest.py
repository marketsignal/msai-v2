from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class BacktestRunRequest(BaseModel):
    strategy_id: str
    instruments: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    config: dict = Field(default_factory=dict)


class BacktestRunResponse(BaseModel):
    job_id: str
    status: str


class BacktestStatusResponse(BaseModel):
    id: str
    status: str
    progress: int
    error_message: str | None = None


class BacktestResultsResponse(BaseModel):
    id: str
    status: str
    metrics: dict | None = None
    trades: list[dict] = Field(default_factory=list)


class MarketDataIngestRequest(BaseModel):
    asset_class: str
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
