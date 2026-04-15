"""Pydantic schemas for the alerts API.

Ported from codex-version/backend/src/msai/schemas/alert.py.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AlertRecord(BaseModel):
    type: str
    level: str
    title: str
    message: str
    created_at: str


class AlertListResponse(BaseModel):
    alerts: list[AlertRecord] = Field(default_factory=list)
