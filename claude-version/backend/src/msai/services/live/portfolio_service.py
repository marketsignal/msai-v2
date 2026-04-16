"""Portfolio service — CRUD on LivePortfolio + draft-revision mutation.

Invariants enforced:
- Only graduated strategies (promoted ``GraduationCandidate`` exists)
  can be added.
- A strategy appears at most once per revision (DB UNIQUE + service
  pre-check for better error message).
- At most one draft (``is_frozen=false``) revision per portfolio
  (DB partial unique index ``uq_one_draft_per_portfolio``).
- ``order_index`` auto-increments in insertion order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from msai.models import (
    GraduationCandidate,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
)
from msai.services.live.revision_service import (
    PortfolioDomainError,
    RevisionImmutableError,
)

if TYPE_CHECKING:
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


class StrategyNotGraduatedError(PortfolioDomainError):
    """Raised when adding a strategy that has no promoted
    :class:`GraduationCandidate`."""


class PortfolioService:
    """CRUD on LivePortfolio + draft-revision management."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_portfolio(
        self,
        *,
        name: str,
        description: str | None,
        created_by: UUID | None,
    ) -> LivePortfolio:
        """Create an empty portfolio — no draft revision yet (lazily
        created by :meth:`add_strategy`)."""
        portfolio = LivePortfolio(name=name, description=description, created_by=created_by)
        self._session.add(portfolio)
        await self._session.flush()
        return portfolio

    async def add_strategy(
        self,
        portfolio_id: UUID,
        strategy_id: UUID,
        config: dict[str, Any],
        instruments: list[str],
        weight: Decimal,
    ) -> LivePortfolioRevisionStrategy:
        """Add a strategy to the portfolio's draft revision.

        Raises :class:`StrategyNotGraduatedError` if the strategy has
        no promoted :class:`GraduationCandidate`. Raises ``ValueError``
        if already a member.
        """
        if not await self._is_graduated(strategy_id):
            raise StrategyNotGraduatedError(
                f"Strategy {strategy_id} has no promoted GraduationCandidate"
            )

        draft = await self._get_or_create_draft_revision(portfolio_id)

        # Re-acquire the draft under ``SELECT … FOR UPDATE`` so a
        # concurrent ``RevisionService.snapshot`` on the same portfolio
        # blocks until this ``add_strategy`` commits. Without the lock,
        # snapshot could freeze the draft + compute ``composition_hash``
        # from the pre-insert member set, then this insert would append
        # a member to a now-frozen revision whose hash no longer matches
        # its rows (Codex review, 2026-04-16). The FOR UPDATE also
        # guarantees that if snapshot has already taken the lock and
        # flipped ``is_frozen=True``, this caller sees the updated row
        # (``is_frozen=True``) after the wait and raises instead of
        # silently corrupting it.
        locked = (
            await self._session.execute(
                select(LivePortfolioRevision)
                .where(LivePortfolioRevision.id == draft.id)
                .with_for_update()
            )
        ).scalar_one()
        if locked.is_frozen:
            raise RevisionImmutableError(
                f"Draft revision {draft.id} was frozen by a concurrent snapshot; "
                "re-invoke ``add_strategy`` to create a fresh draft."
            )

        existing = await self._session.execute(
            select(LivePortfolioRevisionStrategy.id).where(
                LivePortfolioRevisionStrategy.revision_id == draft.id,
                LivePortfolioRevisionStrategy.strategy_id == strategy_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Strategy {strategy_id} is already a member of this draft")

        order_index = await self._next_order_index(draft.id)

        member = LivePortfolioRevisionStrategy(
            revision_id=draft.id,
            strategy_id=strategy_id,
            config=config,
            instruments=instruments,
            weight=weight,
            order_index=order_index,
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def list_draft_members(self, portfolio_id: UUID) -> list[LivePortfolioRevisionStrategy]:
        """Return the draft-revision members in insertion order.
        Empty list if no draft yet."""
        draft = await self.get_current_draft(portfolio_id)
        if draft is None:
            return []
        result = await self._session.execute(
            select(LivePortfolioRevisionStrategy)
            .where(LivePortfolioRevisionStrategy.revision_id == draft.id)
            .order_by(LivePortfolioRevisionStrategy.order_index)
        )
        return list(result.scalars().all())

    async def get_current_draft(self, portfolio_id: UUID) -> LivePortfolioRevision | None:
        """Public accessor — returns the portfolio's unfrozen revision,
        or ``None`` if no draft yet.

        The partial unique index ``uq_one_draft_per_portfolio``
        guarantees there is at most one.
        """
        result = await self._session.execute(
            select(LivePortfolioRevision).where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _is_graduated(self, strategy_id: UUID) -> bool:
        result = await self._session.execute(
            select(GraduationCandidate.id).where(
                GraduationCandidate.strategy_id == strategy_id,
                GraduationCandidate.stage == "promoted",
            )
        )
        return result.first() is not None

    async def _get_or_create_draft_revision(self, portfolio_id: UUID) -> LivePortfolioRevision:
        """Return the existing draft, or create a new one.

        Under concurrent callers on the same portfolio, the partial
        unique index ``uq_one_draft_per_portfolio`` guarantees at most
        one draft row: the loser's flush raises ``IntegrityError``,
        which propagates up so the caller can choose to retry by
        re-invoking ``add_strategy`` (which re-enters here and finds
        the winner's draft via :meth:`get_current_draft`).
        """
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
            # Placeholder — replaced by real hash when RevisionService
            # snapshots the draft. Safe because no UNIQUE constraint
            # across ``composition_hash`` applies to unfrozen rows
            # (UNIQUE(portfolio_id, composition_hash) is enforced for
            # ALL rows, but the partial draft-uniqueness index ensures
            # at most one draft per portfolio, which in turn means at
            # most one placeholder hash per portfolio).
            composition_hash="0" * 64,
            is_frozen=False,
        )
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def _next_order_index(self, revision_id: UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(LivePortfolioRevisionStrategy.order_index), -1)).where(
                LivePortfolioRevisionStrategy.revision_id == revision_id
            )
        )
        return int(result.scalar_one()) + 1
