"""Pydantic schemas for strategy API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class StrategyResponse(BaseModel):
    """Response schema for a single strategy resource."""

    id: UUID
    name: str
    description: str | None
    file_path: str
    strategy_class: str
    config_schema: dict[str, Any] | None
    default_config: dict[str, Any] | None
    code_hash: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyUpdate(BaseModel):
    """Request schema for partial strategy updates."""

    default_config: dict[str, Any] | None = None
    description: str | None = None


class StrategyListResponse(BaseModel):
    """Paginated list response for strategies."""

    items: list[StrategyResponse]
    total: int
