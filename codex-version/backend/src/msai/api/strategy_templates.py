from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.models import Strategy
from msai.schemas.strategy import (
    StrategyTemplateScaffoldRequest,
    StrategyTemplateScaffoldResponse,
    StrategyTemplateSummary,
)
from msai.services.strategy_registry import StrategyRegistry
from msai.services.strategy_templates import StrategyTemplateError, StrategyTemplateService

router = APIRouter(prefix="/strategy-templates", tags=["strategy-templates"])
template_service = StrategyTemplateService()


@router.get("", response_model=list[StrategyTemplateSummary])
async def list_strategy_templates(
    _: Mapping[str, object] = Depends(get_current_user),
) -> list[StrategyTemplateSummary]:
    return [StrategyTemplateSummary(**row) for row in template_service.list_templates()]


@router.post("/scaffold", response_model=StrategyTemplateScaffoldResponse)
async def scaffold_strategy_template(
    payload: StrategyTemplateScaffoldRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyTemplateScaffoldResponse:
    try:
        scaffolded = template_service.scaffold(
            template_id=payload.template_id,
            module_name=payload.module_name,
            description=payload.description,
            force=payload.force,
        )
        registry = StrategyRegistry(settings.strategies_root)
        synced = await registry.sync(db)
        match = next((row for row in synced if row.name == scaffolded["name"]), None)
        if isinstance(match, Strategy):
            scaffolded["strategy_id"] = match.id
    except StrategyTemplateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StrategyTemplateScaffoldResponse(**scaffolded)
