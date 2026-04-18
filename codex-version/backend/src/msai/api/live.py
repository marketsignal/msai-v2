from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models import LiveOrderEvent, LivePortfolioRevision, LivePortfolioRevisionStrategy, Strategy, Trade
from msai.schemas.live import LiveStartRequest, LiveStopRequest, PortfolioStartRequest
from msai.services.live import (
    derive_deployment_identity,
    derive_portfolio_deployment_identity,
)
from msai.services.live_runtime import (
    LiveRuntimeUnavailableError,
    live_runtime_client,
)
from msai.services.live_state_view import (
    build_orders_payload,
    build_positions_payload,
    build_risk_payload,
    build_status_payload,
    build_trades_payload,
)
from msai.services.live_updates import load_live_snapshots, publish_live_snapshot
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import prepare_live_strategy_config
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths
from msai.services.nautilus.trading_node import (
    LiveLiquidationFailedError,
    LiveStartBlockedError,
    LiveStartFailedError,
)
from msai.services.risk_engine import RiskEngine, RiskMetrics
from msai.services.strategy_registry import StrategyRegistry, file_sha256
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/live", tags=["live"])
risk_engine = RiskEngine()
logger = get_logger("api.live")


@router.post("/start")
async def live_start(
    payload: LiveStartRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    strategy = await db.get(Strategy, payload.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")

    try:
        canonical_instruments = await instrument_service.canonicalize_live_instruments(
            db,
            payload.instruments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    strategy_config = prepare_live_strategy_config(
        payload.config,
        canonical_instruments,
    )
    registry = StrategyRegistry(settings.strategies_root)
    strategy_path = registry.resolve_path(strategy)
    if not strategy_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy file not found: {strategy.file_path}",
        )
    user_id = await resolve_user_id_from_claims(db, claims)
    runtime_instruments = list(
        dict.fromkeys(
            [
                str(strategy_config["instrument_id"]),
                *canonical_instruments,
            ]
        )
    )
    await db.commit()
    identity_signature = derive_deployment_identity(
        user_id=user_id,
        strategy_id=strategy.id,
        strategy_code_hash=file_sha256(strategy_path),
        config=strategy_config,
        account_id=settings.ib_account_id,
        paper_trading=payload.paper_trading,
        instruments=runtime_instruments,
    ).signature()
    try:
        deployment_id = await live_runtime_client.start(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            strategy_file=str(strategy_path),
            config=strategy_config,
            instruments=runtime_instruments,
            strategy_code_hash=file_sha256(strategy_path),
            strategy_git_sha=None,
            paper_trading=payload.paper_trading,
            started_by=user_id,
            account_id=settings.ib_account_id,
            identity_signature=identity_signature,
        )
    except LiveStartBlockedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except LiveStartFailedError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except LiveRuntimeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"deployment_id": deployment_id}


@router.post("/start-portfolio")
async def live_start_portfolio(
    payload: PortfolioStartRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    revision = (
        await db.execute(
            select(LivePortfolioRevision)
            .options(
                selectinload(LivePortfolioRevision.strategies).selectinload(LivePortfolioRevisionStrategy.strategy)
            )
            .where(LivePortfolioRevision.id == payload.portfolio_revision_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live portfolio revision not found")
    if not revision.is_frozen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only frozen live portfolio revisions can be deployed",
        )
    if not revision.strategies:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Live portfolio revision has no strategy members",
        )

    registry = StrategyRegistry(settings.strategies_root)
    member_inputs: list[dict[str, object]] = []
    runtime_instruments: list[str] = []
    total_quantity = 0.0

    for member in sorted(revision.strategies, key=lambda item: item.order_index):
        strategy = member.strategy
        if strategy is None:
            strategy = await db.get(Strategy, member.strategy_id)
        if strategy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
        try:
            canonical_instruments = await instrument_service.canonicalize_live_instruments(
                db,
                list(member.instruments),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        strategy_config = prepare_live_strategy_config(dict(member.config), canonical_instruments)
        strategy_path = registry.resolve_path(strategy)
        if not strategy_path.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Strategy file not found: {strategy.file_path}",
            )
        import_paths = resolve_importable_strategy_paths(
            str(strategy_path),
            strategy_class_name=strategy.strategy_class,
        )
        member_inputs.append(
            {
                "revision_strategy_id": member.id,
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "strategy_class": strategy.strategy_class,
                "strategy_code_hash": file_sha256(strategy_path),
                "strategy_path": import_paths.strategy_path,
                "config_path": import_paths.config_path,
                "config": strategy_config,
                "instrument_ids": canonical_instruments,
                "order_index": member.order_index,
            }
        )
        runtime_instruments.extend(canonical_instruments)
        try:
            total_quantity += abs(float(strategy_config.get("trade_size", 1.0)))
        except (TypeError, ValueError):
            total_quantity += 1.0

    decision = await risk_engine.validate_start(
        strategy="portfolio",
        instrument=payload.account_id,
        quantity=max(total_quantity, 1.0),
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=0.0,
            notional_exposure=0.0,
            margin_used=0.0,
        ),
        paper_trading=payload.paper_trading,
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=decision.reason)

    user_id = await resolve_user_id_from_claims(db, claims)
    await db.commit()
    identity_signature = derive_portfolio_deployment_identity(
        user_id=user_id,
        portfolio_revision_id=revision.id,
        account_id=payload.account_id,
        paper_trading=payload.paper_trading,
    ).signature()

    try:
        deployment_id = await live_runtime_client.start(
            portfolio_revision_id=revision.id,
            strategy_members=member_inputs,
            instruments=list(dict.fromkeys(runtime_instruments)),
            paper_trading=payload.paper_trading,
            started_by=user_id,
            account_id=payload.account_id,
            identity_signature=identity_signature,
        )
    except LiveStartBlockedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except LiveStartFailedError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except LiveRuntimeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {"deployment_id": deployment_id}


@router.post("/stop")
async def live_stop(payload: LiveStopRequest, _: Mapping[str, object] = Depends(get_current_user)) -> dict[str, str]:
    try:
        result = await live_runtime_client.stop(
            payload.deployment_id,
            reason=f"Operator requested graceful stop for deployment {payload.deployment_id}",
        )
    except LiveLiquidationFailedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except LiveRuntimeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if not result.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    if not result.stopped:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.reason)
    return {"status": "stopped"}


@router.post("/kill-all")
async def live_kill_all(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, int]:
    decision = await risk_engine.kill_all()
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=decision.reason)
    try:
        count = await live_runtime_client.kill_all()
    except LiveLiquidationFailedError as exc:
        state = await risk_engine.current_state()
        await _publish_live_snapshot_safely(
            "risk",
            {
                "halted": state.halted,
                "reason": state.reason,
                "updated_at": state.updated_at,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except LiveRuntimeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    state = await risk_engine.current_state()
    await _publish_live_snapshot_safely(
        "risk",
        {
            "halted": state.halted,
            "reason": state.reason,
            "updated_at": state.updated_at,
            "stopped": count,
        },
    )
    return {"stopped": count}


@router.post("/reset-halt")
async def live_reset_halt(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, str]:
    decision = await risk_engine.reset_halt()
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=decision.reason)
    state = await risk_engine.current_state()
    await _publish_live_snapshot_safely(
        "risk",
        {
            "halted": state.halted,
            "reason": state.reason,
            "updated_at": state.updated_at,
        },
    )
    return {"status": "ok"}


@router.get("/risk-status")
async def live_risk_status(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, object]:
    state = await risk_engine.current_state()
    snapshots = await load_live_snapshots("risk")
    payload = build_risk_payload(
        {
            "halted": state.halted,
            "reason": state.reason,
            "updated_at": state.updated_at,
        },
        snapshots,
        active_deployments=await _active_deployment_ids(),
    )
    await _publish_live_snapshot_safely("risk", payload)
    return payload


@router.get("/status")
async def live_status(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    try:
        rows = await live_runtime_client.status()
    except LiveRuntimeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    strategy_ids = {str(row["strategy_id"]) for row in rows if row.get("strategy_id")}
    strategy_name_by_id: dict[str, str] = {}
    if strategy_ids:
        result = await db.execute(select(Strategy.id, Strategy.name).where(Strategy.id.in_(strategy_ids)))
        strategy_name_by_id = {strategy_id: name for strategy_id, name in result.all()}

    return build_status_payload(rows, strategy_name_by_id, await load_live_snapshots("status"))


@router.get("/positions")
async def live_positions(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> list[dict[str, object]]:
    return build_positions_payload(
        await load_live_snapshots("positions"),
        active_deployments=await _active_deployment_ids(),
        paper_trading=paper_trading,
    )


@router.get("/orders")
async def live_orders(
    _: Mapping[str, object] = Depends(get_current_user),
    paper_trading: bool = True,
) -> list[dict]:
    return build_orders_payload(
        await load_live_snapshots("orders"),
        active_deployments=await _active_deployment_ids(),
        paper_trading=paper_trading,
    )


@router.get("/order-events")
async def live_order_events(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    deployment_id: str | None = None,
    paper_trading: bool | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    bounded_limit = max(1, min(limit, 500))
    query = select(LiveOrderEvent).order_by(desc(LiveOrderEvent.ts_event)).limit(bounded_limit)
    if deployment_id:
        query = query.where(LiveOrderEvent.deployment_id == deployment_id)
    if paper_trading is not None:
        query = query.where(LiveOrderEvent.paper_trading.is_(paper_trading))

    rows = (await db.execute(query)).scalars().all()
    return [
        {
            "id": row.id,
            "deployment_id": row.deployment_id,
            "strategy_id": row.strategy_id,
            "paper_trading": row.paper_trading,
            "event_id": row.event_id,
            "event_type": row.event_type,
            "instrument": row.instrument,
            "client_order_id": row.client_order_id,
            "venue_order_id": row.venue_order_id,
            "account_id": row.broker_account_id,
            "reason": row.reason,
            "payload": row.payload,
            "executed_at": row.ts_event.isoformat(),
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/trades")
async def live_trades(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    snapshot_rows = build_trades_payload(
        await load_live_snapshots("trades"),
        active_deployments=await _active_deployment_ids(),
    )
    if snapshot_rows:
        return snapshot_rows

    rows = (
        await db.execute(
            select(Trade).where(Trade.is_live.is_(True)).order_by(desc(Trade.executed_at)).limit(100)
        )
    ).scalars()
    payload = [
        {
            "id": row.id,
            "executed_at": row.executed_at.isoformat(),
            "instrument": row.instrument,
            "side": row.side,
            "quantity": float(row.quantity),
            "price": float(row.price),
            "commission": float(row.commission) if row.commission is not None else None,
            "pnl": float(row.pnl) if row.pnl is not None else 0.0,
            "broker_trade_id": row.broker_trade_id,
            "client_order_id": row.client_order_id,
            "venue_order_id": row.venue_order_id,
            "position_id": row.position_id,
            "account_id": row.broker_account_id,
        }
        for row in rows
    ]
    return payload


async def _active_deployment_ids() -> set[str]:
    try:
        rows = await live_runtime_client.status()
    except LiveRuntimeUnavailableError:
        return set()
    return {
        str(row["id"])
        for row in rows
        if str(row.get("status")) in {"running", "starting", "liquidating"}
    }


async def _publish_live_snapshot_safely(name: str, data: object) -> None:
    try:
        await publish_live_snapshot(name, data)
    except Exception as exc:
        logger.warning("live_snapshot_publish_failed", snapshot=name, error=str(exc))
