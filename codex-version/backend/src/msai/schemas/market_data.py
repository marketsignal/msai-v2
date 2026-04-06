from __future__ import annotations

from pydantic import BaseModel, Field


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
