from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BarsResponse(BaseModel):
    symbol: str
    bars: list[dict] = Field(default_factory=list)


class SymbolsResponse(BaseModel):
    symbols: dict[str, list[str]]


class StorageStatsResponse(BaseModel):
    status: str
    last_run_at: str | None = None
    storage_stats: dict = Field(default_factory=dict)
    gaps_detected: list[str] = Field(default_factory=list)
    recent_runs: list[dict] = Field(default_factory=list)


class DailyUniverseEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    asset_class: Literal["equities", "futures"]
    symbols: list[str] = Field(min_length=1)
    provider: str
    dataset: str
    data_schema: str = Field(default="ohlcv-1m", alias="schema")


class DailyUniverseResponse(BaseModel):
    requests: list[DailyUniverseEntry] = Field(default_factory=list)
