from __future__ import annotations

from pydantic import BaseModel, Field


class StrategySummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    file_path: str
    strategy_class: str


class StrategyDetail(StrategySummary):
    config_schema: dict | None = None
    default_config: dict | None = None


class StrategyPatchRequest(BaseModel):
    default_config: dict = Field(default_factory=dict)


class StrategyValidateRequest(BaseModel):
    config: dict = Field(default_factory=dict)


class StrategyValidateResponse(BaseModel):
    valid: bool
    message: str
