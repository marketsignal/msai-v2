from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.schemas.alert import AlertListResponse
from msai.services.alerting import alerting_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/", response_model=AlertListResponse)
async def list_alerts(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 50,
) -> AlertListResponse:
    bounded_limit = max(1, min(limit, 200))
    return AlertListResponse(alerts=alerting_service.list_alerts(limit=bounded_limit))
