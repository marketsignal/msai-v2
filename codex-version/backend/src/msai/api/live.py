from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.models import Strategy, Trade
from msai.schemas.live import LiveStartRequest, LiveStopRequest
from msai.services.ib_account import ib_account_service
from msai.services.nautilus.trading_node import trading_node_manager
from msai.services.risk_engine import RiskEngine
from msai.services.strategy_registry import StrategyRegistry, file_sha256

router = APIRouter(prefix="/live", tags=["live"])
risk_engine = RiskEngine()


@router.post("/start")
async def live_start(
    payload: LiveStartRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    strategy = await db.get(Strategy, payload.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")

    decision = risk_engine.validate_start(
        strategy=strategy.name,
        instrument=payload.instruments[0],
        quantity=float(payload.config.get("trade_size", 1.0)),
        current_pnl=0.0,
        portfolio_value=1_000_000.0,
        notional_exposure=10_000.0,
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=decision.reason)

    registry = StrategyRegistry(settings.strategies_root)
    strategy_path = registry.resolve_path(strategy)
    if not strategy_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy file not found: {strategy.file_path}",
        )
    deployment_id = await trading_node_manager.start(
        strategy_id=strategy.id,
        strategy_file=str(strategy_path),
        config=payload.config,
        instruments=payload.instruments,
        strategy_code_hash=file_sha256(strategy_path),
        strategy_git_sha=None,
        paper_trading=payload.paper_trading,
        started_by=str(claims.get("oid") or claims.get("sub") or "") or None,
    )
    return {"deployment_id": deployment_id}


@router.post("/stop")
async def live_stop(payload: LiveStopRequest, _: Mapping[str, object] = Depends(get_current_user)) -> dict[str, str]:
    stopped = await trading_node_manager.stop(payload.deployment_id)
    if not stopped:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return {"status": "stopped"}


@router.post("/kill-all")
async def live_kill_all(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, int]:
    decision = risk_engine.kill_all()
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=decision.reason)
    count = await trading_node_manager.kill_all()
    return {"stopped": count}


@router.get("/status")
async def live_status(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await trading_node_manager.status()
    strategy_ids = {str(row["strategy_id"]) for row in rows if row.get("strategy_id")}
    strategy_name_by_id: dict[str, str] = {}
    if strategy_ids:
        result = await db.execute(select(Strategy.id, Strategy.name).where(Strategy.id.in_(strategy_ids)))
        strategy_name_by_id = {strategy_id: name for strategy_id, name in result.all()}

    return [
        {
            "id": row["id"],
            "strategy": strategy_name_by_id.get(str(row["strategy_id"]), row["strategy_id"]),
            "status": row["status"],
            "started_at": row["started_at"],
            "daily_pnl": 0.0,
        }
        for row in rows
    ]


@router.get("/positions")
async def live_positions(_: Mapping[str, object] = Depends(get_current_user)) -> list[dict[str, float | str]]:
    return await ib_account_service.portfolio()


@router.get("/trades")
async def live_trades(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (
        await db.execute(
            select(Trade).where(Trade.is_live.is_(True)).order_by(desc(Trade.executed_at)).limit(100)
        )
    ).scalars()
    return [
        {
            "id": row.id,
            "executed_at": row.executed_at.isoformat(),
            "instrument": row.instrument,
            "side": row.side,
            "quantity": float(row.quantity),
            "price": float(row.price),
            "pnl": float(row.pnl) if row.pnl is not None else 0.0,
        }
        for row in rows
    ]
