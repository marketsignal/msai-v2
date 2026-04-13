"""Strategy templates API router -- list and scaffold strategies from templates.

Provides two endpoints:
- ``GET  /api/v1/strategy-templates`` -- list available templates
- ``POST /api/v1/strategy-templates/scaffold`` -- generate a strategy file
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from msai.core.auth import get_current_user
from msai.core.logging import get_logger
from msai.services.strategy_templates import StrategyTemplateError, StrategyTemplateService

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/strategy-templates", tags=["strategy-templates"])


class StrategyTemplateScaffoldRequest(BaseModel):
    """Request body for scaffolding a strategy from a template."""

    template_id: str
    module_name: str  # e.g. "user.my_strategy"
    description: str | None = None


@router.get("/")
async def list_templates(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return metadata for every available strategy template."""
    svc = StrategyTemplateService()
    return svc.list_templates()


@router.post("/scaffold", status_code=status.HTTP_201_CREATED)
async def scaffold_template(
    body: StrategyTemplateScaffoldRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Generate a new strategy file from a template.

    Returns 201 with the scaffolded file metadata on success, or
    422 if the template ID or module name is invalid.
    """
    svc = StrategyTemplateService()
    try:
        result = svc.scaffold(
            template_id=body.template_id,
            module_name=body.module_name,
            description=body.description,
        )
    except StrategyTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    log.info(
        "strategy_template_scaffolded",
        template_id=body.template_id,
        module_name=body.module_name,
        user=claims.get("preferred_username", "unknown"),
    )
    return result
