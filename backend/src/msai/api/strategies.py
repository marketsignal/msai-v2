"""Strategies API router -- discover, list, update, and validate strategies.

Syncs the on-disk ``strategies/`` directory with the ``strategies`` table
on every list request so the frontend always sees real database IDs it
can pass to the backtest endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.strategy import Strategy
from msai.schemas.common import MessageResponse
from msai.schemas.strategy import StrategyListResponse, StrategyResponse, StrategyUpdate
from msai.services.strategy_registry import (
    sync_strategies_to_db,
    validate_strategy_file,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

_STRATEGIES_DIR = settings.strategies_root


@router.get("/", response_model=StrategyListResponse)
async def list_strategies(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StrategyListResponse:
    """Scan the strategies directory, sync with DB, and return real strategy IDs.

    Discovered files are upserted into the ``strategies`` table so that
    every response row has a stable database ID that ``/{id}`` and the
    backtest endpoints can look up.
    """
    paired = await sync_strategies_to_db(db, _STRATEGIES_DIR)
    await db.commit()
    for row, _ in paired:
        await db.refresh(row)

    items: list[StrategyResponse] = [
        StrategyResponse(
            id=row.id,
            name=row.name,
            description=row.description,
            file_path=row.file_path,
            strategy_class=row.strategy_class,
            config_class=row.config_class,
            config_schema=row.config_schema,
            default_config=row.default_config,
            config_schema_status=row.config_schema_status,
            code_hash=row.code_hash or "",
            created_at=row.created_at,
        )
        for row, _ in paired
    ]

    return StrategyListResponse(items=items, total=len(items))


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StrategyResponse:
    """Retrieve a single strategy by its database ID.

    Calls :func:`sync_strategies_to_db` before reading the row so the
    detail endpoint works on a cold DB (no prior list call needed) and
    picks up ``config_schema`` updates for files edited since the last
    list call. Memoization inside the sync ensures the cost is a
    single filesystem scan + zero schema recompute when nothing changed.
    """
    # Refresh DB state from disk so the detail response reflects the
    # current file contents (Maintainer council blocking objection #2:
    # the detail endpoint must not depend on list-endpoint side effects).
    await sync_strategies_to_db(db, _STRATEGIES_DIR)
    await db.commit()

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
        config_class=strategy.config_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
        config_schema_status=strategy.config_schema_status,
        code_hash=strategy.code_hash or "",
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
        config_class=strategy.config_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
        config_schema_status=strategy.config_schema_status,
        code_hash=strategy.code_hash or "",
        created_at=strategy.created_at,
    )


@router.post("/{strategy_id}/validate", response_model=MessageResponse)
async def validate_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> MessageResponse:
    """Validate that a strategy's source file exposes a Nautilus Strategy class.

    Uses the database row to locate the source file, then defers to
    :func:`validate_strategy_file` so the API never tries to instantiate
    a Nautilus ``Strategy`` subclass (which would require a configured
    backtest / live engine that does not exist in the API process).
    """
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )

    file_path = Path(strategy.file_path) if strategy.file_path else None
    if file_path is None or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Strategy file not found on disk: {strategy.file_path}",
        )

    ok, message = validate_strategy_file(file_path)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Strategy validation failed: {message}",
        )

    return MessageResponse(message=f"Strategy '{message}' validated successfully")


@router.delete("/{strategy_id}", response_model=MessageResponse)
async def delete_strategy(
    strategy_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> MessageResponse:
    """Soft-delete / unregister a strategy.

    TODO: Implement a real soft-delete flag on the Strategy model so we
    can preserve historical backtest references.
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
