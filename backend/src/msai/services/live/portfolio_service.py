"""Portfolio service — CRUD on LivePortfolio + draft-revision mutation.

Invariants enforced:
- Only graduated strategies (``GraduationCandidate`` exists at a live-
  eligible stage; see ``ELIGIBLE_FOR_LIVE_PORTFOLIO`` in
  ``services/graduation.py``) can be added.
- A strategy appears at most once per revision (DB UNIQUE + service
  pre-check for better error message).
- At most one draft (``is_frozen=false``) revision per portfolio
  (DB partial unique index ``uq_one_draft_per_portfolio``).
- ``order_index`` auto-increments in insertion order.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from msai.models import (
    GraduationCandidate,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
)
from msai.models.portfolio import (
    Portfolio as BacktestPortfolio,  # noqa: TC001 — used at runtime in materialize_from_backtest signature
)
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_run import (
    PortfolioRun,  # noqa: TC001 — used at runtime in materialize_from_backtest signature
)
from msai.services.graduation import ELIGIBLE_FOR_LIVE_PORTFOLIO
from msai.services.live.revision_service import (
    PortfolioDomainError,
    RevisionImmutableError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


class StrategyNotGraduatedError(PortfolioDomainError):
    """Raised when adding a strategy whose :class:`GraduationCandidate`
    is not at a live-eligible stage (see
    :data:`ELIGIBLE_FOR_LIVE_PORTFOLIO` in ``services.graduation``)."""


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
        no :class:`GraduationCandidate` at a live-eligible stage
        (:data:`ELIGIBLE_FOR_LIVE_PORTFOLIO`). Raises ``ValueError``
        if already a member.
        """
        if not await self._is_graduated(strategy_id):
            raise StrategyNotGraduatedError(
                f"Strategy {strategy_id} has no GraduationCandidate at a "
                f"live-eligible stage (one of: {sorted(ELIGIBLE_FOR_LIVE_PORTFOLIO)}). "
                f"Run the graduation pipeline first: discovery → validation → "
                f"paper_candidate → paper_running → paper_review → live_candidate."
            )

        draft = await self._get_or_create_draft_revision(portfolio_id)

        # Re-acquire the draft under ``SELECT … FOR UPDATE`` so a
        # concurrent ``RevisionService.snapshot`` on the same portfolio
        # blocks until this ``add_strategy`` commits. Without the lock,
        # snapshot could freeze the draft + compute ``composition_hash``
        # from the pre-insert member set, then this insert would append
        # a member to a now-frozen revision whose hash no longer matches
        # its rows.
        #
        # Three post-wait outcomes are possible:
        #   1. Row still present and ``is_frozen=false`` → safe to insert.
        #   2. Row still present but ``is_frozen=true`` → snapshot
        #      froze it in place; raise ``RevisionImmutableError`` so
        #      caller retries by re-invoking ``add_strategy``.
        #   3. Row no longer exists → snapshot collapsed the draft onto
        #      an existing frozen revision with matching hash (which
        #      ``session.delete(draft)``s the original row). Raise
        #      ``RevisionImmutableError`` with the same retry advice;
        #      ``scalar_one_or_none`` + explicit None-check avoids the
        #      raw ``NoResultFound`` the original ``scalar_one`` would
        #      surface. (Codex iter-2 review.)
        locked = (
            await self._session.execute(
                select(LivePortfolioRevision)
                .where(LivePortfolioRevision.id == draft.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None or locked.is_frozen:
            raise RevisionImmutableError(
                f"Draft revision {draft.id} was frozen or collapsed by a "
                "concurrent snapshot; re-invoke ``add_strategy`` to create "
                "a fresh draft."
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
    # G2: materialize a backtest portfolio + run into a frozen live revision.
    # ------------------------------------------------------------------

    async def materialize_from_backtest(
        self,
        *,
        portfolio: BacktestPortfolio,
        run: PortfolioRun,
        account_id: str,
        created_by: UUID | None,
    ) -> tuple[LivePortfolio, LivePortfolioRevision]:
        """Promote a completed backtest run into a frozen ``LivePortfolio`` + revision.

        The backtest-side :class:`Portfolio` and :class:`PortfolioRun`
        carry the composition that was validated by the engine. We
        materialize that into a new ``LivePortfolio`` (one per promotion
        — names are uniquified by appending the run id) and a single
        ``LivePortfolioRevision`` frozen at creation. ``add_strategy``
        is NOT called because:

        1. ``add_strategy`` enforces ``ELIGIBLE_FOR_LIVE_PORTFOLIO``
           graduation stages; default-portfolio candidates created by
           the F1c bridge sit at ``portfolio_default`` and paper-test
           candidates at ``paper_candidate``. Promote-to-live is the
           Phase 1 stand-in for that graduation path and intentionally
           bypasses the stage check.
        2. ``add_strategy`` builds an unfrozen *draft* revision; the
           promotion's revision is frozen at creation so the deploy path
           sees a stable composition from the first moment.

        Full-mode runs carry the winning hyperparameter set in
        ``run.metrics["best_config"]``. When present, it is merged into
        each member's per-strategy ``config`` dict so the live node
        receives the optimized parameters (not the backtest defaults).

        Args:
            portfolio: The backtest-side :class:`Portfolio` row.
            run: The :class:`PortfolioRun` row whose composition we promote.
            account_id: IB account id (paper-only enforcement at API layer).
            created_by: User UUID for the ``LivePortfolio.created_by`` field.

        Returns:
            A tuple ``(live_portfolio, frozen_revision)``. Both are
            ``session.flush``-persisted but NOT committed — the caller
            owns the transaction boundary.
        """
        # ---- Load the source allocations + candidates + strategies ----
        allocations = list(
            (
                await self._session.execute(
                    select(PortfolioAllocation)
                    .where(PortfolioAllocation.portfolio_id == portfolio.id)
                    .options(
                        selectinload(PortfolioAllocation.candidate).selectinload(
                            GraduationCandidate.strategy
                        )
                    )
                    .order_by(PortfolioAllocation.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not allocations:
            raise PortfolioDomainError(f"Portfolio {portfolio.id} has no allocations to promote")

        # ---- Resolve per-strategy config + instruments + weights ----
        # Full-mode runs carry the winning hyperparameters in
        # ``metrics["best_config"]``. Merge it on top of each member's
        # candidate config so the LIVE revision uses the optimizer's
        # picks rather than the candidate's defaults. Quick-mode runs
        # have no best_config; the merge degenerates to a no-op.
        best_config: dict[str, Any] = {}
        if run.metrics and isinstance(run.metrics.get("best_config"), dict):
            best_config = dict(run.metrics["best_config"])

        # Equal weight is the simplest defensible default when an operator
        # has not pinned per-candidate weights (the F1c bridge always
        # leaves them None). For explicit-allocation portfolios we
        # NORMALIZE the operator's weights to sum to 1.0 before writing
        # them into the live revision — matching what the backtest's
        # ``normalize_weights`` does at run time.
        #
        # Codex-bot P2 finding on PR #73: previously the raw weights flowed
        # through, so a portfolio with allocations ``[0.8, 0.8]`` backtested
        # as ``50/50`` (normalized) but promoted as ``80%/80%`` (raw) — the
        # live revision diverged from the composition that was validated.
        explicit_weights_raw = [float(a.weight) for a in allocations if a.weight is not None]
        use_explicit = len(explicit_weights_raw) == len(allocations)
        equal_weight = Decimal("1") / Decimal(len(allocations))
        explicit_weight_sum = sum(explicit_weights_raw) if use_explicit else 0.0
        # Defensive: zero or negative sums collapse to equal-weight rather than
        # producing NaN/zero weights in the live revision. The orchestrator's
        # ``normalize_weights`` raises in this case; here the safest fallback
        # is parity with the F1c-bridge path (equal weight).
        explicit_normalize = use_explicit and explicit_weight_sum > 0

        # ---- Create LivePortfolio + frozen revision ----
        # Append the run id suffix so re-promoting the same backtest
        # portfolio produces a uniquely-named LivePortfolio (the
        # ``name`` column has ``unique=True``). Operators who want to
        # repromote with the same name should rename / archive the
        # previous LivePortfolio first.
        live_portfolio_name = f"{portfolio.name} (run {str(run.id)[:8]})"
        live_portfolio = LivePortfolio(
            name=live_portfolio_name,
            description=(
                f"Promoted from PortfolioRun {run.id} (account_id={account_id}, mode={run.mode})"
            ),
            created_by=created_by,
        )
        self._session.add(live_portfolio)
        await self._session.flush()

        # Build the member rows + composition hash from the canonical
        # composition. We compute the hash deterministically so the same
        # backtest portfolio + run + best_config always produces the same
        # hash — useful for de-dup at the deploy-path layer.
        member_rows: list[dict[str, Any]] = []
        for idx, alloc in enumerate(allocations):
            candidate = alloc.candidate
            if candidate is None or candidate.strategy is None:
                raise PortfolioDomainError(
                    f"Allocation {alloc.id} is missing candidate/strategy "
                    "rows after eager-load — DB corruption?"
                )
            strategy = candidate.strategy

            default_config = dict(strategy.default_config or {})
            candidate_config = dict(candidate.config or {})
            merged_config: dict[str, Any] = {
                **default_config,
                **candidate_config,
                **best_config,
            }

            instruments_raw = (
                candidate_config.get("instruments") or default_config.get("instruments") or []
            )
            instruments = [str(i) for i in instruments_raw]
            if not instruments:
                raise PortfolioDomainError(
                    f"Candidate {candidate.id} has no instruments configured; "
                    "cannot promote a portfolio that would trade nothing."
                )

            if explicit_normalize and alloc.weight is not None:
                # Normalize to sum-to-1.0 so the live composition matches
                # what the backtest validated.
                weight = Decimal(str(float(alloc.weight) / explicit_weight_sum))
            else:
                weight = equal_weight

            member_rows.append(
                {
                    "strategy_id": strategy.id,
                    "config": merged_config,
                    "instruments": instruments,
                    "weight": weight,
                    "order_index": idx,
                }
            )

        composition_hash = self._composition_hash(member_rows)
        revision = LivePortfolioRevision(
            portfolio_id=live_portfolio.id,
            revision_number=1,
            composition_hash=composition_hash,
            is_frozen=True,
        )
        self._session.add(revision)
        await self._session.flush()

        for row in member_rows:
            self._session.add(
                LivePortfolioRevisionStrategy(
                    revision_id=revision.id,
                    strategy_id=row["strategy_id"],
                    config=row["config"],
                    instruments=row["instruments"],
                    weight=row["weight"],
                    order_index=row["order_index"],
                )
            )
        await self._session.flush()

        return live_portfolio, revision

    @staticmethod
    def _composition_hash(member_rows: list[dict[str, Any]]) -> str:
        """SHA256 over the canonical (strategy_id, config, instruments, weight) tuples.

        Sorted by ``order_index`` (the rows are already in that order),
        instruments sorted within each row so a re-ordering of the same
        set of instruments produces the same hash. ``weight`` is
        serialized as ``str(Decimal)`` to preserve precision; ``config``
        is dumped with ``sort_keys=True``.
        """
        payload = []
        for row in member_rows:
            payload.append(
                {
                    "strategy_id": str(row["strategy_id"]),
                    "config": row["config"],
                    "instruments": sorted(row["instruments"]),
                    "weight": str(row["weight"]),
                    "order_index": row["order_index"],
                }
            )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _is_graduated(self, strategy_id: UUID) -> bool:
        result = await self._session.execute(
            select(GraduationCandidate.id).where(
                GraduationCandidate.strategy_id == strategy_id,
                GraduationCandidate.stage.in_(ELIGIBLE_FOR_LIVE_PORTFOLIO),
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
