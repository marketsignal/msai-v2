from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.services.ib_account import ib_account_service
from msai.services.ib_probe import ib_probe

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/summary")
async def account_summary(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, float]:
    summary = await ib_account_service.summary()
    return {
        "net_liquidation": summary.net_liquidation,
        "buying_power": summary.buying_power,
        "margin_used": summary.margin_used,
        "available_funds": summary.available_funds,
        "unrealized_pnl": summary.unrealized_pnl,
    }


@router.get("/portfolio")
async def account_portfolio(_: Mapping[str, object] = Depends(get_current_user)) -> list[dict[str, float | str]]:
    return await ib_account_service.portfolio()


@router.get("/health")
async def account_health(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, int | str]:
    return ib_probe.health_status()
