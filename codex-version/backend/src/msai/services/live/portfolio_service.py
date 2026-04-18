from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models import LivePortfolio, LivePortfolioRevision, LivePortfolioRevisionStrategy, Strategy
from msai.services.graduation_service import GraduationService
from msai.services.live.revision_service import PortfolioDomainError, RevisionImmutableError

_LIVE_READY_GRADUATION_STAGES = {"live_candidate", "live_running", "paused"}


class StrategyNotGraduatedError(PortfolioDomainError):
    pass


class PortfolioService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        graduation_service: GraduationService | None = None,
    ) -> None:
        self._session = session
        self._graduation_service = graduation_service or GraduationService()

    async def list_portfolios(self, *, limit: int = 100) -> list[LivePortfolio]:
        result = await self._session.execute(
            select(LivePortfolio).order_by(LivePortfolio.updated_at.desc()).limit(max(1, min(limit, 250)))
        )
        return list(result.scalars().all())

    async def get_portfolio(self, portfolio_id: str) -> LivePortfolio | None:
        return await self._session.get(LivePortfolio, portfolio_id)

    async def create_portfolio(
        self,
        *,
        name: str,
        description: str | None,
        created_by: str | None,
    ) -> LivePortfolio:
        portfolio = LivePortfolio(name=name, description=description, created_by=created_by)
        self._session.add(portfolio)
        await self._session.flush()
        return portfolio

    async def add_strategy(
        self,
        portfolio_id: str,
        strategy_id: str,
        config: dict[str, Any],
        instruments: list[str],
        weight: Decimal,
    ) -> LivePortfolioRevisionStrategy:
        strategy = await self._session.get(Strategy, strategy_id)
        if strategy is None:
            raise ValueError(f"Strategy {strategy_id} not found")
        if not self._is_graduated(strategy_id):
            raise StrategyNotGraduatedError(
                f"Strategy {strategy_id} is not in a live-ready graduation stage"
            )

        draft = await self._get_or_create_draft_revision(portfolio_id)
        locked = (
            await self._session.execute(
                select(LivePortfolioRevision).where(LivePortfolioRevision.id == draft.id).with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None or locked.is_frozen:
            raise RevisionImmutableError(
                f"Draft revision {draft.id} was frozen or replaced by a concurrent snapshot"
            )

        existing = await self._session.execute(
            select(LivePortfolioRevisionStrategy.id).where(
                LivePortfolioRevisionStrategy.revision_id == draft.id,
                LivePortfolioRevisionStrategy.strategy_id == strategy_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Strategy {strategy_id} is already a member of this draft")

        member = LivePortfolioRevisionStrategy(
            revision_id=draft.id,
            strategy_id=strategy_id,
            config=dict(config),
            instruments=list(instruments),
            weight=weight,
            order_index=await self._next_order_index(draft.id),
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def list_draft_members(self, portfolio_id: str) -> list[LivePortfolioRevisionStrategy]:
        draft = await self.get_current_draft(portfolio_id)
        if draft is None:
            return []
        result = await self._session.execute(
            select(LivePortfolioRevisionStrategy)
            .where(LivePortfolioRevisionStrategy.revision_id == draft.id)
            .order_by(LivePortfolioRevisionStrategy.order_index)
        )
        return list(result.scalars().all())

    async def get_current_draft(self, portfolio_id: str) -> LivePortfolioRevision | None:
        result = await self._session.execute(
            select(LivePortfolioRevision).where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
        )
        return result.scalar_one_or_none()

    def _is_graduated(self, strategy_id: str) -> bool:
        for candidate in self._graduation_service.list_candidates(limit=1000):
            if str(candidate.get("strategy_id")) != strategy_id:
                continue
            if str(candidate.get("stage")) in _LIVE_READY_GRADUATION_STAGES:
                return True
        return False

    async def _get_or_create_draft_revision(self, portfolio_id: str) -> LivePortfolioRevision:
        existing = await self.get_current_draft(portfolio_id)
        if existing is not None:
            return existing

        max_number = (
            await self._session.execute(
                select(func.coalesce(func.max(LivePortfolioRevision.revision_number), 0)).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id
                )
            )
        ).scalar_one()
        draft = LivePortfolioRevision(
            portfolio_id=portfolio_id,
            revision_number=int(max_number) + 1,
            composition_hash="0" * 64,
            is_frozen=False,
        )
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def _next_order_index(self, revision_id: str) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(LivePortfolioRevisionStrategy.order_index), -1)).where(
                LivePortfolioRevisionStrategy.revision_id == revision_id
            )
        )
        return int(result.scalar_one()) + 1
