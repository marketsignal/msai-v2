from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    queue_name: str | None = None
    queue_job_id: str | None = None
    worker_id: str | None = None
    attempt: int = 0
    heartbeat_at: str | None = None


class BacktestResultsResponse(BaseModel):
    id: str
    status: str
    metrics: dict | None = None
    trades: list[dict] = Field(default_factory=list)


class BacktestAnalyticsResponse(BaseModel):
    id: str
    metrics: dict = Field(default_factory=dict)
    series: list[dict] = Field(default_factory=list)
    report_url: str | None = None


class MarketDataIngestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    asset_class: Literal["equities", "futures", "options", "fx", "crypto"]
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    provider: Literal["auto", "databento", "polygon"] = "auto"
    dataset: str | None = None
    data_schema: Literal["ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d"] = Field(
        default="ohlcv-1m",
        alias="schema",
    )


class MarketDataDailyIngestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    asset_class: Literal["equities", "futures", "options", "fx", "crypto"]
    symbols: list[str] = Field(min_length=1)
    provider: Literal["auto", "databento", "polygon"] = "auto"
    dataset: str | None = None
    data_schema: Literal["ohlcv-1s", "ohlcv-1m", "ohlcv-1h", "ohlcv-1d"] = Field(
        default="ohlcv-1m",
        alias="schema",
    )
