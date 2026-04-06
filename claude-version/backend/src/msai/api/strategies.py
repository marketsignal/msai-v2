"""Strategies API router -- CRUD and validation for trading strategies.

Discovers strategy files on disk and manages their database records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.strategy import Strategy
from msai.schemas.common import MessageResponse
from msai.schemas.strategy import StrategyListResponse, StrategyResponse, StrategyUpdate
from msai.services.strategy_registry import (
    StrategyInfo,
    discover_strategies,
    load_strategy_class,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

_STRATEGIES_DIR = settings.strategies_root


@router.get("/", response_model=StrategyListResponse)
async def list_strategies(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> StrategyListResponse:
    """Scan the strategies directory and return all discovered strategies.

    This endpoint performs a filesystem scan each time it is called so that
    newly added strategy files are picked up immediately.
    """
    strategies_dir = _STRATEGIES_DIR
    discovered: list[StrategyInfo] = discover_strategies(strategies_dir)

    items: list[StrategyResponse] = [
        StrategyResponse(
            id=UUID(int=0),
            name=info.name,
            description=info.description,
            file_path=str(info.module_path),
            strategy_class=info.class_name,
            config_schema=None,
            default_config=None,
            code_hash=info.code_hash,
            created_at=datetime.fromtimestamp(info.module_path.stat().st_mtime, tz=timezone.utc),
        )
        for info in discovered
    ]

    return StrategyListResponse(items=items, total=len(items))


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StrategyResponse:
    """Retrieve a single strategy by its database ID."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )

    return StrategyResponse(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        file_path=strategy.file_path,
        strategy_class=strategy.strategy_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
        code_hash="",  # TODO: compute from file_path
        created_at=strategy.created_at,
    )


@router.patch("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: UUID,
    body: StrategyUpdate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StrategyResponse:
    """Update a strategy's default_config and/or description."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(strategy, field, value)

    await db.commit()
    await db.refresh(strategy)

    return StrategyResponse(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        file_path=strategy.file_path,
        strategy_class=strategy.strategy_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
        code_hash="",  # TODO: compute from file_path
        created_at=strategy.created_at,
    )


@router.post("/{strategy_id}/validate", response_model=MessageResponse)
async def validate_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> MessageResponse:
    """Validate that a strategy class can be loaded and instantiated.

    Scans the strategies directory for a matching strategy (by name derived
    from the strategy_id path), loads the class dynamically, and verifies
    it can be instantiated.

    For now this endpoint uses filesystem discovery rather than a DB lookup
    to keep it simple for M2.

    TODO: Look up strategy from DB by ID and use file_path + strategy_class.
    """
    strategies_dir = _STRATEGIES_DIR
    discovered: list[StrategyInfo] = discover_strategies(strategies_dir)

    if not discovered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No strategies found on disk",
        )

    # For M2 we validate the first discovered strategy as a proof of concept.
    # In M3+ this will look up the strategy by DB ID.
    info = discovered[0]

    try:
        cls = load_strategy_class(info.module_path, info.class_name)
        # Verify we can instantiate (strategies typically accept **kwargs)
        _instance = cls()
    except (ImportError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Strategy validation failed: {exc}",
        ) from exc

    return MessageResponse(message=f"Strategy '{info.class_name}' validated successfully")


@router.delete("/{strategy_id}", response_model=MessageResponse)
async def delete_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> MessageResponse:
    """Soft-delete / unregister a strategy.

    TODO: Implement actual soft-delete flag on the Strategy model.
    """
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )

    await db.delete(strategy)
    await db.commit()

    return MessageResponse(message=f"Strategy {strategy_id} deleted")
