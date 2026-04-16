"""Revision service — snapshot (freeze) + active lookup + immutability guard.

No denormalized ``latest_revision_id`` pointer — the active revision
is computed on demand via a query ordered by ``revision_number`` desc
with ``is_frozen=true``. The FK would otherwise form a cycle against
``live_portfolio_revisions.portfolio_id`` and complicate
``Base.metadata.drop_all/create_all`` fixtures.

Immutability is two-layer: the ``is_frozen`` boolean drives
:meth:`enforce_immutability`, and a partial unique index at the DB
level ensures at most one unfrozen row per portfolio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from msai.models import LivePortfolioRevision, LivePortfolioRevisionStrategy
from msai.services.live.portfolio_composition import compute_composition_hash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RevisionImmutableError(Exception):
    """Raised when a caller attempts to mutate a frozen revision."""


class RevisionService:
    """Freeze drafts into immutable revisions + fetch active + guard."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, portfolio_id: UUID) -> LivePortfolioRevision:
        """Freeze the portfolio's draft into a hashed, numbered revision.

        If an existing frozen revision of the same portfolio has the
        same composition hash, the draft is deleted and the existing
        revision is returned (identical compositions collapse).

        Raises ``ValueError`` if there is no draft to snapshot.

        Concurrency: uses ``SELECT … FOR UPDATE`` on the draft row so
        two concurrent ``snapshot`` callers on the same portfolio
        serialize. Without this, caller B could load the draft while
        caller A is mid-flush, observe A's just-frozen row as
        "existing with matching hash", and delete it via
        ``session.delete(draft)`` — because it's the SAME row that's
        already been frozen.

        After the lock releases (A commits with ``is_frozen=True``),
        B's ``_lock_draft_revision`` query — which filters
        ``is_frozen = false`` — no longer matches the now-frozen row
        and returns ``None``. ``snapshot`` then raises ``ValueError``.
        The caller is expected to recover by calling
        :meth:`get_active_revision` to retrieve A's frozen revision;
        we deliberately do NOT silently return it here because a
        ``snapshot`` call that finds no draft to freeze is a semantic
        error, not a no-op.
        """
        draft = await self._lock_draft_revision(portfolio_id)
        if draft is None:
            # No unfrozen row — either the portfolio never had a draft
            # OR a concurrent snapshot already froze it. Surface a
            # clean error so the caller retries via
            # ``get_active_revision`` rather than silently treating
            # "nothing to snapshot" as success.
            raise ValueError(
                f"Portfolio {portfolio_id} has no draft revision to snapshot"
            )

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

        computed_hash = compute_composition_hash(
            [
                {
                    "strategy_id": m.strategy_id,
                    "order_index": m.order_index,
                    "config": m.config,
                    "instruments": list(m.instruments),
                    "weight": m.weight,
                }
                for m in members
            ]
        )

        existing = (
            await self._session.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id,
                    LivePortfolioRevision.is_frozen.is_(True),
                    LivePortfolioRevision.composition_hash == computed_hash,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            await self._session.delete(draft)
            await self._session.flush()
            return existing

        draft.composition_hash = computed_hash
        draft.is_frozen = True
        await self._session.flush()
        return draft

    async def get_active_revision(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision | None:
        """Return the portfolio's latest frozen revision, or ``None``."""
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

    async def enforce_immutability(self, revision_id: UUID) -> None:
        """Raise :class:`RevisionImmutableError` if the revision is frozen.

        Call at the top of any method that mutates member rows under
        ``revision_id``. Drafts pass silently.
        """
        revision = await self._session.get(LivePortfolioRevision, revision_id)
        if revision is None:
            raise ValueError(f"Revision {revision_id} not found")
        if revision.is_frozen:
            raise RevisionImmutableError(
                f"Revision {revision_id} is frozen and cannot be mutated"
            )

    # ------------------------------------------------------------------

    async def _lock_draft_revision(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision | None:
        """``SELECT … FOR UPDATE`` on the portfolio's draft row.

        Blocks concurrent snapshot callers on the same portfolio
        until the current transaction commits. ``.with_for_update()``
        takes a row-level lock that's released on commit/rollback.
        """
        result = await self._session.execute(
            select(LivePortfolioRevision)
            .where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()
