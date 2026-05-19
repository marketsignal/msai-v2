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
from datetime import UTC, datetime
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
from msai.services.live.snapshot_binding import (
    instruments_match,
    strip_for_comparison,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


def _canonical_config_for_binding(config: dict[str, Any]) -> str:
    """Canonical JSON of ``strip_for_comparison(config)`` — same shape
    ``verify_member_matches_candidate`` uses to compare member.config
    against candidate.config. Wrapping it here keeps the equality logic
    in one place: the live_candidate we synthesize during promotion is
    only considered "matching" when this canonical form is identical to
    the member's, so the downstream binding check is guaranteed to pass.
    """
    return json.dumps(strip_for_comparison(config), sort_keys=True, separators=(",", ":"))


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

        # Codex bot iter-6 P2 on PR #73: prefer the weights the RUN actually
        # used. Quick mode's data-driven allocator branch (orchestration.py)
        # syncs allocator output into ``run.allocations[i]["weight"]`` after
        # the per-strategy backtests complete, so a portfolio with
        # ``allocator_name="inverse_vol"`` could be backtested as 70/30 but
        # — with only the compose-time ``PortfolioAllocation.weight``
        # visible here — would be materialized as 50/50 (equal_weight
        # fallback for the bridge case). Build a candidate_id → weight
        # map so the live revision uses the validated composition.
        # Missing entries fall back to the legacy explicit/equal path.
        run_weights_by_candidate: dict[str, float] = {}
        run_allocations_payload = run.allocations or []
        for item in run_allocations_payload:
            if not isinstance(item, dict):
                continue
            cid = item.get("candidate_id")
            w = item.get("weight")
            if cid is not None and isinstance(w, (int, float)):
                run_weights_by_candidate[str(cid)] = float(w)

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

            # Codex bot iter-3 P1 on PR #73: ``best_config`` from a Full-mode
            # run contains the optimizer's PORTFOLIO-level params
            # (``leverage``, ``position_size`` — see
            # ``services/portfolio_backtest/optimizer.py::build_search_space``).
            # Those are not part of each strategy's ``default_config`` and
            # must NOT leak into per-strategy member configs — if they did,
            # the downstream ``verify_member_matches_candidate`` (which
            # compares strict-equality on ``strip_for_comparison(config)``)
            # would reject the binding with ``BINDING_MISMATCH``.
            #
            # Filter ``best_config`` to keys the strategy already knows about
            # (i.e., that appear in ``default_config``). This lets per-strategy
            # tunables — which would by convention be added to a strategy's
            # ``default_config`` — flow through, while keeping pure portfolio
            # risk controls (``leverage`` / ``position_size``) at the
            # portfolio layer where they belong.
            strategy_param_keys = set(default_config.keys())
            best_config_for_member = {
                k: v for k, v in best_config.items() if k in strategy_param_keys
            }
            merged_config: dict[str, Any] = {
                **default_config,
                **candidate_config,
                **best_config_for_member,
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

            run_weight_for_candidate = run_weights_by_candidate.get(str(candidate.id))
            if run_weight_for_candidate is not None and run_weight_for_candidate > 0:
                # Trust the run's recorded weight: it incorporates Quick-mode
                # allocator output (inverse_vol / vol_targeted) and the
                # orchestrator's ``normalize_weights`` step, so it already
                # represents the validated live composition.
                weight = Decimal(str(run_weight_for_candidate))
            elif explicit_normalize and alloc.weight is not None:
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

        # Codex bot iter-3 P1 on PR #73: ensure each promoted member has a
        # deployable ``GraduationCandidate`` at ``stage="live_candidate"``
        # so the downstream ``/api/v1/live/start-portfolio`` first-deploy
        # binding lookup succeeds.
        #
        # WHY: ``materialize_from_backtest`` intentionally bypasses
        # ``add_strategy``'s ``ELIGIBLE_FOR_LIVE_PORTFOLIO`` check
        # (default-portfolio candidates created by the F1c bridge sit at
        # ``portfolio_default``, paper-test candidates at ``paper_candidate``
        # — neither is in ``ELIGIBLE_FOR_LIVE_PORTFOLIO``). Promote-to-live
        # IS the Phase 1 stand-in for the graduation path, so it must also
        # MATERIALIZE the live-eligible candidate row rather than just
        # bypassing the check at compose time. Without this step, every
        # promoted revision would 422 with ``BINDING_NOT_GRADUATED`` on
        # the very first ``start-portfolio`` attempt.
        #
        # HOW: For each member, look for an existing unlinked
        # ``live_candidate`` (``deployment_id IS NULL``) whose config and
        # instruments match the frozen member. If one matches, reuse it
        # (avoids manufacturing duplicates when an operator pre-graduated
        # the strategy through the canonical pipeline). Otherwise create
        # a fresh ``live_candidate`` mirroring the member exactly, with
        # ``instruments`` stamped into ``config`` so
        # ``candidate_instruments(candidate)`` and
        # ``verify_member_matches_candidate(member, candidate)`` both
        # succeed on the first deploy attempt.
        promotion_ts = datetime.now(UTC)
        for row in member_rows:
            strategy_id = row["strategy_id"]
            member_config: dict[str, Any] = row["config"]
            member_instruments: list[str] = row["instruments"]

            # The candidate's config must include ``instruments`` (that's
            # the contract ``candidate_instruments(candidate)`` reads).
            target_candidate_config: dict[str, Any] = {
                **member_config,
                "instruments": list(member_instruments),
            }
            target_canonical = _canonical_config_for_binding(target_candidate_config)

            unlinked_live = list(
                (
                    await self._session.execute(
                        select(GraduationCandidate).where(
                            GraduationCandidate.strategy_id == strategy_id,
                            GraduationCandidate.stage == "live_candidate",
                            GraduationCandidate.deployment_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            reuse_match: GraduationCandidate | None = None
            for existing in unlinked_live:
                existing_cfg = existing.config or {}
                existing_instruments = [str(x) for x in existing_cfg.get("instruments", [])]
                if _canonical_config_for_binding(existing_cfg) == target_canonical and (
                    instruments_match(existing_instruments, member_instruments)
                ):
                    reuse_match = existing
                    break

            if reuse_match is None:
                # Codex bot iter-5 P2 on PR #73: surface ambiguous-candidate
                # conflicts at promotion time rather than letting them slip
                # downstream as ``BINDING_AMBIGUOUS`` at start-portfolio.
                #
                # If the operator already has an UNLINKED ``live_candidate``
                # for this strategy whose config/instruments don't match the
                # member we're about to freeze, creating another unlinked
                # ``live_candidate`` would leave two of them — and
                # ``api/live.py``'s first-deploy lookup rejects that with
                # ``BINDING_AMBIGUOUS``. Raising a clear domain error here
                # tells the operator the actionable next step (archive the
                # existing graduation OR retry promotion under matching
                # config) before they spend time wiring up a deploy that
                # would only fail downstream.
                if unlinked_live:
                    conflicts = [
                        {
                            "candidate_id": str(c.id),
                            "stage": c.stage,
                            "config_keys": sorted((c.config or {}).keys()),
                        }
                        for c in unlinked_live
                    ]
                    raise PortfolioDomainError(
                        f"Strategy {strategy_id} already has "
                        f"{len(unlinked_live)} unlinked `live_candidate` "
                        f"row(s) whose config does not match the member "
                        f"being promoted. Synthesizing another live_candidate "
                        f"would leave the strategy ambiguous and deploy would "
                        f"fail with BINDING_AMBIGUOUS. Either archive the "
                        f"existing candidate(s) via the graduation pipeline "
                        f"and retry promotion, or re-graduate them under the "
                        f"merged config this promotion would write. "
                        f"Conflicts: {conflicts}"
                    )
                self._session.add(
                    GraduationCandidate(
                        strategy_id=strategy_id,
                        stage="live_candidate",
                        config=target_candidate_config,
                        # Empty metrics — this candidate was synthesized as
                        # part of promotion, not graduated through paper
                        # testing. The promotion path is the Phase 1 stand-in
                        # for graduation; metrics would come from a real
                        # graduation pipeline run.
                        metrics={},
                        promoted_by=created_by,
                        promoted_at=promotion_ts,
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
