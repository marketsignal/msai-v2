"""Asset universe API router — manage tracked symbols for data ingestion.

Provides endpoints to list, add, disable, and trigger ingestion for
the symbols in the asset universe table.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.schemas.asset_universe import (
    AssetUniverseCreate,
    AssetUniverseListResponse,
    AssetUniverseResponse,
)
from msai.schemas.common import MessageResponse
from msai.services.asset_universe import AssetUniverseService

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/universe", tags=["universe"])

_service = AssetUniverseService()


@router.get("/", response_model=AssetUniverseListResponse)
async def list_assets(
    asset_class: str | None = None,
    enabled: bool | None = True,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AssetUniverseListResponse:
    """List tracked assets, optionally filtered by asset_class and enabled."""
    assets = await _service.list(db, asset_class=asset_class, enabled=enabled)
    items = [AssetUniverseResponse.model_validate(a) for a in assets]
    return AssetUniverseListResponse(items=items, total=len(items))


@router.post(
    "/",
    response_model=AssetUniverseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_asset(
    body: AssetUniverseCreate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AssetUniverseResponse:
    """Add a new asset to the universe.

    Returns 201 Created with the new asset record.
    """
    user_id = claims.get("sub")
    asset = await _service.add(db, body, user_id=user_id if isinstance(user_id, UUID) else None)
    await db.commit()
    await db.refresh(asset)
    return AssetUniverseResponse.model_validate(asset)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_asset(
    asset_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Soft-delete an asset (sets enabled=False). Returns 204 No Content."""
    try:
        await _service.remove(db, asset_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset {asset_id} not found",
        )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/ingest", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingest(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> MessageResponse:
    """Trigger ingestion for all enabled assets.

    Enqueues an arq job for each distinct asset_class group with the
    corresponding symbols.  Returns 202 Accepted immediately.
    """
    from collections import defaultdict

    from msai.core.queue import enqueue_ingest, get_redis_pool

    targets = await _service.get_ingest_targets(db)
    if not targets:
        log.warning("ingest_trigger_empty_universe")
        return MessageResponse(message="No enabled assets in universe — nothing to ingest")

    # Group symbols by asset_class for efficient batching
    groups: dict[str, list[str]] = defaultdict(list)
    for asset in targets:
        groups[asset.asset_class].append(asset.symbol)

    pool = await get_redis_pool()
    enqueued = 0
    for asset_class, symbols in groups.items():
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        await enqueue_ingest(
            pool=pool,
            asset_class=asset_class,
            symbols=symbols,
            start=yesterday,
            end=yesterday,
        )
        enqueued += 1
        log.info(
            "ingest_job_enqueued",
            asset_class=asset_class,
            symbol_count=len(symbols),
        )

    return MessageResponse(
        message=f"Enqueued {enqueued} ingestion job(s) for {len(targets)} asset(s)"
    )
