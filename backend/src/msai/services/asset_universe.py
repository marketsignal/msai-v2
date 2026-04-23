"""Asset universe management service.

Provides CRUD operations for the tracked symbols table.  The ``enabled``
flag implements soft-delete so historical data references are never
orphaned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from msai.core.logging import get_logger
from msai.models.asset_universe import AssetUniverse

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.schemas.asset_universe import AssetUniverseCreate

log = get_logger(__name__)


class AssetUniverseService:
    """Manage the set of instruments tracked for data ingestion."""

    async def add(
        self,
        session: AsyncSession,
        data: AssetUniverseCreate,
        user_id: UUID | None = None,
    ) -> AssetUniverse:
        """Add an asset to the universe.

        Args:
            session: Active database session.
            data: Validated creation payload.
            user_id: Optional ID of the user who added the asset.

        Returns:
            The newly created :class:`AssetUniverse` row.
        """
        asset = AssetUniverse(
            symbol=data.symbol,
            exchange=data.exchange,
            asset_class=data.asset_class,
            resolution=data.resolution,
            created_by=user_id,
        )
        session.add(asset)
        await session.flush()
        log.info(
            "asset_universe_added",
            symbol=data.symbol,
            exchange=data.exchange,
            asset_class=data.asset_class,
        )
        return asset

    async def remove(self, session: AsyncSession, asset_id: UUID) -> None:
        """Soft-delete an asset by setting ``enabled=False``.

        Historical data linked to this asset is retained; only the
        ingestion pipeline stops fetching new bars.

        Args:
            session: Active database session.
            asset_id: Primary key of the asset to disable.

        Raises:
            ValueError: If no asset with the given ID exists.
        """
        asset = await session.get(AssetUniverse, asset_id)
        if asset is None:
            raise ValueError(f"Asset {asset_id} not found")
        asset.enabled = False
        log.info("asset_universe_removed", asset_id=str(asset_id), symbol=asset.symbol)

    async def list(
        self,
        session: AsyncSession,
        *,
        asset_class: str | None = None,
        enabled: bool | None = True,
    ) -> list[AssetUniverse]:
        """List assets, optionally filtered by asset_class and enabled status.

        Args:
            session: Active database session.
            asset_class: Filter to a specific asset class (e.g. ``"stocks"``).
            enabled: Filter by enabled flag.  Defaults to ``True`` so callers
                get only active assets unless they explicitly ask for all.

        Returns:
            List of matching :class:`AssetUniverse` rows ordered by
            ``(asset_class, symbol)``.
        """
        stmt = select(AssetUniverse)
        if asset_class is not None:
            stmt = stmt.where(AssetUniverse.asset_class == asset_class)
        if enabled is not None:
            stmt = stmt.where(AssetUniverse.enabled == enabled)
        stmt = stmt.order_by(AssetUniverse.asset_class, AssetUniverse.symbol)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_ingest_targets(self, session: AsyncSession) -> list[AssetUniverse]:
        """Get all enabled assets for daily ingestion.

        Convenience wrapper around :meth:`list` with ``enabled=True``.
        """
        return await self.list(session, enabled=True)

    async def mark_ingested(
        self,
        session: AsyncSession,
        asset_id: UUID,
        timestamp: datetime,
    ) -> None:
        """Update ``last_ingested_at`` after a successful data download.

        Args:
            session: Active database session.
            asset_id: Primary key of the ingested asset.
            timestamp: Datetime of the ingestion completion.
        """
        asset = await session.get(AssetUniverse, asset_id)
        if asset is not None:
            asset.last_ingested_at = timestamp
            log.info(
                "asset_universe_ingested",
                asset_id=str(asset_id),
                symbol=asset.symbol,
                timestamp=str(timestamp),
            )
