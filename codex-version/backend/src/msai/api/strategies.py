from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.models import Strategy
from msai.schemas.strategy import (
    StrategyDetail,
    StrategyPatchRequest,
    StrategySummary,
    StrategyValidateRequest,
    StrategyValidateResponse,
)
from msai.services.strategy_registry import StrategyRegistry

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("/", response_model=list[StrategySummary])
async def list_strategies(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[StrategySummary]:
    registry = StrategyRegistry(settings.strategies_root)
    strategies = await registry.sync(db)
    return [
        StrategySummary(
            id=s.id,
            name=s.name,
            description=s.description,
            file_path=s.file_path,
            strategy_class=s.strategy_class,
        )
        for s in strategies
    ]


@router.post("/sync", response_model=list[StrategySummary])
async def sync_strategies(
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[StrategySummary]:
    registry = StrategyRegistry(settings.strategies_root)
    strategies = await registry.sync(db)
    return [
        StrategySummary(
            id=s.id,
            name=s.name,
            description=s.description,
            file_path=s.file_path,
            strategy_class=s.strategy_class,
        )
        for s in strategies
    ]


@router.get("/{strategy_id}", response_model=StrategyDetail)
async def get_strategy(
    strategy_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyDetail:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    return StrategyDetail(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        file_path=strategy.file_path,
        strategy_class=strategy.strategy_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
    )


@router.patch("/{strategy_id}", response_model=StrategyDetail)
async def patch_strategy(
    strategy_id: str,
    payload: StrategyPatchRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyDetail:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    strategy.default_config = payload.default_config
    await db.commit()
    await db.refresh(strategy)
    return StrategyDetail(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        file_path=strategy.file_path,
        strategy_class=strategy.strategy_class,
        config_schema=strategy.config_schema,
        default_config=strategy.default_config,
    )


@router.post("/{strategy_id}/validate", response_model=StrategyValidateResponse)
async def validate_strategy(
    strategy_id: str,
    payload: StrategyValidateRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyValidateResponse:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    registry = StrategyRegistry(settings.strategies_root)
    valid, message = await registry.validate(strategy, payload.config)
    return StrategyValidateResponse(valid=valid, message=message)


@router.delete("/{strategy_id}")
async def unregister_strategy(
    strategy_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    await db.delete(strategy)
    await db.commit()
    return {"status": "ok"}
