"""Pydantic schemas for asset universe API endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AssetUniverseCreate(BaseModel):
    """Request schema for adding an asset to the universe."""

    symbol: str = Field(max_length=32)
    exchange: str = Field(max_length=32)
    asset_class: str = Field(max_length=32)  # stocks, futures, options, crypto, forex
    resolution: str = Field(default="1m", max_length=16)


class AssetUniverseResponse(BaseModel):
    """Response schema for a single asset universe entry."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    symbol: str
    exchange: str
    asset_class: str
    resolution: str
    enabled: bool
    last_ingested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AssetUniverseListResponse(BaseModel):
    """Paginated list response for asset universe entries."""

    items: list[AssetUniverseResponse]
    total: int
