"""Live trading API router -- deploy, monitor, and control live strategies.

Manages the full lifecycle of live/paper trading deployments: starting
strategies, stopping them, querying status, and emergency halt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.audit import log_audit
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.models.strategy import Strategy
from msai.models.user import User
from msai.schemas.live import (
    LiveDeploymentInfo,
    LiveKillAllResponse,
    LivePositionsResponse,
    LiveStartRequest,
    LiveStatusResponse,
    LiveStopRequest,
    LiveTradesResponse,
)
from msai.services.nautilus.trading_node import TradingNodeManager
from msai.services.risk_engine import RiskEngine

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/live", tags=["live"])

# Module-level risk engine and trading node manager (singleton per process)
_risk_engine = RiskEngine()
_node_manager = TradingNodeManager(_risk_engine)


async def _resolve_user_id(db: AsyncSession, claims: dict[str, Any]) -> UUID | None:
    """Resolve the authenticated user's database ID from JWT claims."""
    sub = claims.get("sub")
    if not sub:
        return None
    result = await db.execute(select(User.id).where(User.entra_id == sub))
    row = result.scalar_one_or_none()
    return row


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def live_start(
    request: LiveStartRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Deploy a strategy to paper or live trading.

    The risk engine validates the deployment before it is allowed to start.
    A ``LiveDeployment`` record is created in the database and the
    ``TradingNodeManager`` is instructed to launch the process.
    """
    # Verify the strategy exists
    result = await db.execute(select(Strategy).where(Strategy.id == request.strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {request.strategy_id} not found",
        )

    # Create the deployment record
    deployment = LiveDeployment(
        strategy_id=request.strategy_id,
        strategy_code_hash="live",  # TODO: compute from strategy file
        config=request.config,
        instruments=request.instruments,
        status="starting",
        paper_trading=request.paper_trading,
        started_at=datetime.now(UTC),
        started_by=await _resolve_user_id(db, claims),
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)

    # Attempt to start the trading node
    started = await _node_manager.start(
        deployment_id=str(deployment.id),
        strategy_path=strategy.file_path,
        config=request.config,
        instruments=request.instruments,
    )

    if not started:
        deployment.status = "rejected"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment rejected by risk engine",
        )

    deployment.status = "running"
    await db.commit()

    await log_audit(
        db,
        user_id=await _resolve_user_id(db, claims),
        action="live_start",
        resource_type="live_deployment",
        resource_id=deployment.id,
        details={"instruments": request.instruments, "paper": request.paper_trading},
    )

    log.info("live_deployment_started", deployment_id=str(deployment.id))

    return {
        "id": str(deployment.id),
        "status": deployment.status,
        "paper_trading": deployment.paper_trading,
    }


@router.post("/stop")
async def live_stop(
    request: LiveStopRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, str]:
    """Stop a running deployment.

    Updates the deployment status in the database and instructs the
    ``TradingNodeManager`` to terminate the process.
    """
    result = await db.execute(
        select(LiveDeployment).where(LiveDeployment.id == request.deployment_id)
    )
    deployment: LiveDeployment | None = result.scalar_one_or_none()

    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment {request.deployment_id} not found",
        )

    stopped = await _node_manager.stop(str(deployment.id))
    deployment.status = "stopped"
    deployment.stopped_at = datetime.now(UTC)
    await db.commit()

    await log_audit(
        db,
        user_id=deployment.started_by,
        action="live_stop",
        resource_type="live_deployment",
        resource_id=deployment.id,
    )

    log.info("live_deployment_stopped", deployment_id=str(deployment.id), was_running=stopped)

    return {"id": str(deployment.id), "status": "stopped"}


@router.post("/kill-all")
async def live_kill_all(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveKillAllResponse:
    """Emergency stop ALL running strategies.

    Triggers the risk engine halt and terminates every managed trading node.
    """
    count = await _node_manager.stop_all()

    await log_audit(
        db,
        user_id=await _resolve_user_id(db, claims),
        action="live_kill_all",
        resource_type="live_deployment",
        details={"stopped_count": count},
    )

    log.critical("kill_all_executed", stopped=count)

    return LiveKillAllResponse(stopped=count, risk_halted=_risk_engine.is_halted)


@router.get("/status")
async def live_status(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveStatusResponse:
    """All deployments with their current status.

    Queries the database for recent deployments and combines that with
    the in-memory node manager status.
    """
    result = await db.execute(
        select(LiveDeployment).order_by(LiveDeployment.created_at.desc()).limit(50)
    )
    deployments = result.scalars().all()

    items = [
        LiveDeploymentInfo(
            id=d.id,
            strategy_id=d.strategy_id,
            status=d.status,
            paper_trading=d.paper_trading,
            instruments=d.instruments,
            started_at=d.started_at,
            stopped_at=d.stopped_at,
        )
        for d in deployments
    ]

    return LiveStatusResponse(
        deployments=items,
        risk_halted=_risk_engine.is_halted,
        active_count=_node_manager.active_count,
    )


@router.get("/positions")
async def live_positions(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> LivePositionsResponse:
    """Current open positions.

    TODO: Wire to TradingNodeManager position tracking in Phase 2.
    """
    return LivePositionsResponse(positions=[])


@router.get("/trades")
async def live_trades(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveTradesResponse:
    """Recent live trade executions.

    TODO: Query the trades table filtered by ``is_live=True``.
    """
    return LiveTradesResponse(trades=[], total=0)
