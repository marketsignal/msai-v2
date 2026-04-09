"""Account API router -- IB account summary, portfolio, and health.

Provides endpoints to query Interactive Brokers account data and
gateway connectivity status.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.core.logging import get_logger
from msai.services.ib_account import IBAccountService
from msai.services.ib_probe import IBProbe

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/account", tags=["account"])

# Module-level singletons (per process).
# Use settings so the service connects to the IB Gateway container
# (ib-gateway:4002 in Docker, 127.0.0.1:4002 locally).
from msai.core.config import settings

_ib_service = IBAccountService(host=settings.ib_host, port=settings.ib_port)
_ib_probe = IBProbe()


@router.get("/summary")
async def account_summary(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, float]:
    """IB account summary with key financial metrics."""
    return await _ib_service.get_summary()


@router.get("/portfolio")
async def account_portfolio(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    """IB portfolio positions."""
    return await _ib_service.get_portfolio()


@router.get("/health")
async def account_health(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, str | bool]:
    """IB Gateway connection health status."""
    return {
        "status": "healthy" if _ib_probe.is_healthy else "unhealthy",
        "gateway_connected": _ib_probe.is_healthy,
        "consecutive_failures": str(_ib_probe.consecutive_failures),
    }
