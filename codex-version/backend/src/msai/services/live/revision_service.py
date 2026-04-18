from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models import LivePortfolioRevision, LivePortfolioRevisionStrategy
from msai.services.live.portfolio_composition import compute_composition_hash


class PortfolioDomainError(Exception):
    pass


class RevisionImmutableError(PortfolioDomainError):
    pass


class NoDraftToSnapshotError(PortfolioDomainError):
    pass


class EmptyCompositionError(PortfolioDomainError):
    pass


class RevisionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, portfolio_id: str) -> LivePortfolioRevision:
        draft = await self._lock_draft_revision(portfolio_id)
        if draft is None:
            raise NoDraftToSnapshotError(f"Portfolio {portfolio_id} has no draft revision to snapshot")

        members = (
            (
                await self._session.execute(
                    select(LivePortfolioRevisionStrategy)
                    .where(LivePortfolioRevisionStrategy.revision_id == draft.id)
                    .order_by(LivePortfolioRevisionStrategy.order_index)
                )
            )
            .scalars()
            .all()
        )
        if not members:
            raise EmptyCompositionError(f"Draft revision {draft.id} has no member strategies")

        composition_hash = compute_composition_hash(
            [
                {
                    "strategy_id": member.strategy_id,
                    "order_index": member.order_index,
                    "config": member.config,
                    "instruments": list(member.instruments),
                    "weight": member.weight,
                }
                for member in members
            ]
        )
        existing = (
            await self._session.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id,
                    LivePortfolioRevision.is_frozen.is_(True),
                    LivePortfolioRevision.composition_hash == composition_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await self._session.delete(draft)
            await self._session.flush()
            return existing

        draft.composition_hash = composition_hash
        draft.is_frozen = True
        await self._session.flush()
        return draft

    async def get_active_revision(self, portfolio_id: str) -> LivePortfolioRevision | None:
        result = await self._session.execute(
            select(LivePortfolioRevision)
            .where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(True),
            )
            .order_by(LivePortfolioRevision.revision_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def enforce_immutability(self, revision_id: str) -> None:
        revision = await self._session.get(LivePortfolioRevision, revision_id)
        if revision is None:
            raise ValueError(f"Revision {revision_id} not found")
        if revision.is_frozen:
            raise RevisionImmutableError(f"Revision {revision_id} is frozen and cannot be mutated")

    async def _lock_draft_revision(self, portfolio_id: str) -> LivePortfolioRevision | None:
        result = await self._session.execute(
            select(LivePortfolioRevision)
            .where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()
