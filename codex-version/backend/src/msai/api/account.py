from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.services.ib_account import ib_account_service
from msai.services.ib_probe import ib_probe

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/summary")
async def account_summary(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> dict[str, float]:
    summary = await ib_account_service.summary(paper_trading=paper_trading)
    return {
        "net_liquidation": summary.net_liquidation,
        "equity_with_loan_value": summary.equity_with_loan_value,
        "buying_power": summary.buying_power,
        "margin_used": summary.margin_used,
        "initial_margin_requirement": summary.initial_margin_requirement,
        "maintenance_margin_requirement": summary.maintenance_margin_requirement,
        "available_funds": summary.available_funds,
        "excess_liquidity": summary.excess_liquidity,
        "sma": summary.sma,
        "gross_position_value": summary.gross_position_value,
        "cushion": summary.cushion,
        "unrealized_pnl": summary.unrealized_pnl,
    }


@router.get("/portfolio")
async def account_portfolio(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> list[dict[str, float | str]]:
    return await ib_account_service.portfolio(paper_trading=paper_trading)


@router.get("/snapshot")
async def account_snapshot(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> dict[str, Any]:
    snapshot = await ib_account_service.reconciliation_snapshot(paper_trading=paper_trading)
    return {
        "connected": snapshot.connected,
        "mock_mode": snapshot.mock_mode,
        "generated_at": snapshot.generated_at,
        "positions": snapshot.positions,
        "open_orders": snapshot.open_orders,
    }


@router.get("/health")
async def account_health(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> dict[str, int | str | bool]:
    health = await ib_account_service.health(paper_trading=paper_trading)
    probe = ib_probe.health_status()
    return {**probe, **health, "paper_trading": paper_trading}
