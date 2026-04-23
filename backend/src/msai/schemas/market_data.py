"""Pydantic schemas for the Market Data API endpoints.

Defines request bodies and response models for bar queries, symbol listings,
ingestion triggers, and storage status reporting.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    """Request body for ``POST /api/v1/market-data/ingest``."""

    asset_class: Literal["stocks", "equities", "indexes", "futures", "options", "crypto"] = Field(
        ..., description="Asset class"
    )
    symbols: list[str] = Field(..., min_length=1, description="Ticker symbols to ingest")
    start: date = Field(..., description="Start date YYYY-MM-DD")
    end: date = Field(..., description="End date YYYY-MM-DD")
    provider: str = Field("auto", description="Data provider: auto, databento, or polygon")
    dataset: str | None = Field(None, description="Override default Databento dataset")
    data_schema: str | None = Field(None, description="Override default Databento schema")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class BarResponse(BaseModel):
    """Single OHLCV bar record."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarsResponse(BaseModel):
    """Response for ``GET /api/v1/market-data/bars/{symbol}``."""

    symbol: str
    interval: str
    bars: list[BarResponse]
    count: int


class SymbolsResponse(BaseModel):
    """Response for ``GET /api/v1/market-data/symbols``."""

    symbols: dict[str, list[str]]


class StorageStatsResponse(BaseModel):
    """Storage statistics breakdown."""

    asset_classes: dict[str, int]
    total_files: int
    total_bytes: int


class StatusResponse(BaseModel):
    """Response for ``GET /api/v1/market-data/status``."""

    status: str
    storage: StorageStatsResponse


class IngestResponse(BaseModel):
    """Response for ``POST /api/v1/market-data/ingest``."""

    message: str
    asset_class: str
    symbols: list[str]
    start: date
    end: date
