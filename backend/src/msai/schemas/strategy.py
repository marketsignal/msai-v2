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
    # Discovered ``*Config`` class name. Server-side validation uses
    # this exact name rather than re-deriving via suffix swap.
    config_class: str | None
    config_schema: dict[str, Any] | None
    default_config: dict[str, Any] | None
    # One of ``"ready" | "unsupported" | "extraction_failed" |
    # "no_config_class"`` — see
    # ``msai.services.nautilus.schema_hooks.ConfigSchemaStatus``. The
    # frontend auto-form activates only when this is ``"ready"``;
    # otherwise it falls back to a raw JSON textarea with a message.
    config_schema_status: str
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
