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


class StrategyTemplateSummary(BaseModel):
    id: str
    label: str
    description: str
    default_config: dict = Field(default_factory=dict)


class StrategyTemplateScaffoldRequest(BaseModel):
    module_name: str
    template_id: str
    description: str | None = None
    force: bool = False


class StrategyTemplateScaffoldResponse(BaseModel):
    strategy_id: str | None = None
    template_id: str
    name: str
    description: str | None = None
    file_path: str
    strategy_class: str
    config_schema: dict | None = None
    default_config: dict | None = None
