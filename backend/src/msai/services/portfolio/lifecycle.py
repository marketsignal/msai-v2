"""Portfolio + PortfolioRun CRUD and status transitions.

Pure database access — no Nautilus, no Optuna, no QuantStats.
:mod:`msai.services.portfolio.orchestration` delegates here for storage.
Keeping CRUD separate makes the orchestration code unit-testable against a
fake lifecycle and removes ~500 LOC of mechanical SQLAlchemy plumbing from
the file that owns the engine wiring.

All methods are ``@staticmethod`` — callers manage their own ``AsyncSession``
and pass it in.  The class is a namespace, not a stateful service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_enums import BacktestMode, PortfolioObjective, PortfolioRunStatus
from msai.models.portfolio_run import PortfolioRun
from msai.models.strategy import Strategy

if TYPE_CHECKING:
    import builtins
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.schemas.portfolio import PortfolioCreate, PortfolioRunCreate


# F1c sentinel: ``stage`` value used for default candidates auto-created by
# the strategy-first compose bridge. NOT in the standard graduation pipeline
# vocabulary (``discovery`` / ``paper_candidate`` / ``live_candidate`` /
# ``archived``) — chosen so the bridge's idempotency lookup is unambiguous
# and so graduation reporting can filter these candidates out by stage.
_PORTFOLIO_DEFAULT_STAGE = "portfolio_default"

log = get_logger(__name__)


class PortfolioLifecycle:
    """CRUD + status transitions for :class:`Portfolio` and :class:`PortfolioRun`.

    All methods take an :class:`AsyncSession` explicitly — callers manage the
    transaction boundary.  The class is a namespace; do not instantiate.
    """

    # ------------------------------------------------------------------
    # Portfolio CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create(
        session: AsyncSession,
        data: PortfolioCreate,
        user_id: UUID | None = None,
    ) -> Portfolio:
        """Create a portfolio with its allocation rows.

        Two compose paths (mutually exclusive — enforced at the schema
        layer by ``PortfolioCreate._require_one_compose_path``):

        - **explicit candidates** (``data.allocations``): legacy path,
          one :class:`PortfolioAllocation` per provided candidate id with
          the operator-set weight passed through verbatim.
        - **strategy-first compose** (``data.strategy_ids``, F1c): for
          each strategy id, idempotently get-or-create a default
          :class:`GraduationCandidate` (``stage = "portfolio_default"``,
          ``config`` seeded from ``strategy.default_config``); then bind
          one :class:`PortfolioAllocation` per strategy with
          ``weight=None`` so the orchestration layer derives weights from
          the configured allocator.

        Args:
            session: Active async database session.
            data: Validated portfolio creation payload.
            user_id: Optional user UUID for the ``created_by`` field.

        Returns:
            The newly created :class:`Portfolio` row (flushed, not committed).
        """
        portfolio = Portfolio(
            name=data.name,
            description=data.description,
            # ``.value`` makes the contract explicit (and survives a future
            # switch from StrEnum to Enum, which would change ``str(...)``).
            objective=data.objective.value,
            base_capital=data.base_capital,
            requested_leverage=data.requested_leverage,
            downside_target=data.downside_target,
            benchmark_symbol=data.benchmark_symbol,
            # B5 safety caps + mode + allocator. The schema validator
            # guarantees these are present (with defaults), so writing them
            # unconditionally is safe.
            max_position_size=data.max_position_size,
            max_drawdown_halt=data.max_drawdown_halt,
            default_mode=data.default_mode,
            allocator_name=data.allocator_name,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()

        if data.strategy_ids is not None:
            # F1c bridge path — auto-create one default candidate per strategy
            # (idempotent: repeat compose with the same strategy reuses the
            # same candidate).  Validate no duplicates so we don't try to
            # insert two allocations against the same candidate (the upstream
            # unique index would catch it, but the operator-visible error
            # here is clearer).
            seen_strategy_ids: set[UUID] = set()
            for sid in data.strategy_ids:
                if sid in seen_strategy_ids:
                    raise ValueError(f"Duplicate strategy {sid} in strategy_ids")
                seen_strategy_ids.add(sid)

            for sid in data.strategy_ids:
                candidate = await _get_or_create_default_candidate(session, sid)
                session.add(
                    PortfolioAllocation(
                        portfolio_id=portfolio.id,
                        candidate_id=candidate.id,
                        # Allocator computes from candidate metrics at
                        # orchestration time — operator did not pin a weight.
                        weight=None,
                    )
                )
            await session.flush()

            log.info(
                "portfolio_created",
                portfolio_id=str(portfolio.id),
                name=data.name,
                num_allocations=len(data.strategy_ids),
                compose_path="strategy_ids",
            )
            return portfolio

        # Legacy explicit-candidate path. ``data.allocations`` is guaranteed
        # non-None by the schema validator when ``strategy_ids`` is absent.
        assert data.allocations is not None  # noqa: S101 — schema invariant

        # Validate: no duplicate candidate IDs
        seen_ids: set[UUID] = set()
        for alloc in data.allocations:
            if alloc.candidate_id in seen_ids:
                raise ValueError(f"Duplicate candidate {alloc.candidate_id} in allocations")
            seen_ids.add(alloc.candidate_id)

        # Validate all candidate IDs exist before inserting allocations
        for alloc in data.allocations:
            candidate_row = await session.get(GraduationCandidate, alloc.candidate_id)
            if candidate_row is None:
                raise ValueError(f"Graduation candidate {alloc.candidate_id} not found")

        # Codex bot iter-9 P1 on PR #73: reject duplicate ``strategy_id``
        # across allocations, mirroring the strategy_ids-compose path's
        # check. Full-mode optimization's ``returns_cache`` keys by
        # strategy_id (see ``_run_full_mode``), so two allocations
        # pointing at different candidates of the SAME strategy would
        # collapse — later entries overwrite earlier ones, dropping one
        # member's returns and silently corrupting IS/OOS scores +
        # best_config. Surface the duplicate at compose time so the
        # optimizer never sees an ill-formed composition.
        seen_strategy_ids_alloc: set[UUID] = set()
        for alloc in data.allocations:
            candidate_row = await session.get(GraduationCandidate, alloc.candidate_id)
            assert candidate_row is not None  # noqa: S101 — checked above
            if candidate_row.strategy_id in seen_strategy_ids_alloc:
                raise ValueError(
                    f"Duplicate strategy {candidate_row.strategy_id} across allocations "
                    f"(via candidate {alloc.candidate_id}); a strategy may only appear "
                    "ONCE per portfolio so Full-mode optimization's per-strategy "
                    "returns_cache doesn't collapse distinct allocations."
                )
            seen_strategy_ids_alloc.add(candidate_row.strategy_id)

        for alloc in data.allocations:
            session.add(
                PortfolioAllocation(
                    portfolio_id=portfolio.id,
                    candidate_id=alloc.candidate_id,
                    weight=alloc.weight,
                )
            )

        await session.flush()

        log.info(
            "portfolio_created",
            portfolio_id=str(portfolio.id),
            name=data.name,
            num_allocations=len(data.allocations),
            compose_path="allocations",
        )
        return portfolio

    @staticmethod
    async def list(
        session: AsyncSession,
        limit: int = 100,
    ) -> builtins.list[Portfolio]:
        """List portfolios ordered by creation time (newest first).

        Args:
            session: Active async database session.
            limit: Maximum number of rows to return.

        Returns:
            A list of :class:`Portfolio` rows.
        """
        stmt = select(Portfolio).order_by(Portfolio.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get(
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> Portfolio:
        """Get a single portfolio by ID. Raises ValueError if not found.

        Args:
            session: Active async database session.
            portfolio_id: Primary key of the portfolio row.

        Returns:
            The :class:`Portfolio` row.

        Raises:
            ValueError: If the portfolio does not exist.
        """
        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")
        return portfolio

    @staticmethod
    async def get_allocations(
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> builtins.list[PortfolioAllocation]:
        """List allocations for a portfolio.

        Args:
            session: Active async database session.
            portfolio_id: FK to the owning portfolio.

        Returns:
            A list of :class:`PortfolioAllocation` rows for the given portfolio.
        """
        stmt = (
            select(PortfolioAllocation)
            .where(PortfolioAllocation.portfolio_id == portfolio_id)
            .order_by(PortfolioAllocation.created_at)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def count(session: AsyncSession) -> int:
        """Return the total number of portfolios."""
        result = await session.execute(select(func.count()).select_from(Portfolio))
        return result.scalar_one()

    # ------------------------------------------------------------------
    # PortfolioRun CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_run(
        session: AsyncSession,
        portfolio_id: UUID,
        data: PortfolioRunCreate,
        user_id: UUID | None = None,
    ) -> PortfolioRun:
        """Create a portfolio backtest run.

        Args:
            session: Active async database session.
            portfolio_id: FK to the portfolio being evaluated.
            data: Validated run creation payload (date range).
            user_id: Optional user UUID for the ``created_by`` field.

        Returns:
            The newly created :class:`PortfolioRun` row (flushed, not committed).

        Raises:
            ValueError: If the referenced portfolio does not exist.
        """
        # Verify portfolio exists
        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")

        # Persist the Full-mode ``n_trials`` override (if any) into the
        # ``metrics`` JSONB column under a sentinel key.  Lighter than a
        # dedicated schema column (no migration delta) and the run-row's
        # ``metrics`` payload is already the "everything the run produced /
        # was configured with" bag.  Quick mode ignores it.  See
        # ``PortfolioRunCreate.n_trials`` docstring for the operator-facing
        # rationale (the FAIL_STALE classification from the first E2E pass
        # blocked UC-PB-API-003 because there was no API-level way to cap
        # trials inside a smoke time budget).
        initial_metrics: dict[str, Any] | None = None
        if data.n_trials is not None:
            initial_metrics = {"n_trials_override": int(data.n_trials)}

        # Resolve the mode: explicit per-run override wins; otherwise inherit
        # from the parent portfolio's ``default_mode``.  Codex-bot P2 finding
        # on PR #73 caught the previous bug: schema defaulted to ``QUICK`` so
        # a portfolio created with ``default_mode='full'`` silently launched
        # Quick runs whenever a client omitted ``mode``.
        if data.mode is not None:
            resolved_mode = data.mode
        else:
            # ``portfolio.default_mode`` may load as either a ``BacktestMode``
            # enum or a raw string depending on the SQLAlchemy load path
            # (model defaults via ``server_default`` come back as strings on
            # some drivers). Coerce defensively so downstream ``.value``
            # access doesn't trip on mocked/fresh rows.
            raw = portfolio.default_mode
            resolved_mode = raw if isinstance(raw, BacktestMode) else BacktestMode(raw)

        # Schema validator on PortfolioRunCreate enforced the 90-day minimum
        # only when ``mode=FULL`` was explicit.  When mode is inherited (None
        # in the request body) the validator skipped — enforce here too so
        # the worker doesn't crash mid-flight on a too-short Full range.
        if resolved_mode is BacktestMode.FULL:
            range_days = (data.end_date - data.start_date).days + 1
            if range_days < 90:
                raise ValueError(
                    f"Full mode (inherited from portfolio default_mode) requires "
                    f"at least 90 days between start_date and end_date "
                    f"(got {range_days} day{'s' if range_days != 1 else ''}); "
                    "use mode='quick' explicitly for shorter ranges or extend the window."
                )

            # Codex bot iter-6 P2 on PR #73: reject objectives that have no
            # scorer in ``OBJECTIVES`` (``equal_weight`` and ``manual``).
            # The Optuna trial body calls ``objective_score(metrics, obj)``
            # which raises ``ValueError`` for unregistered objectives;
            # the optimizer's bare-except catches every trial and the run
            # finishes "completed" with all-zero IS/OOS scores and no
            # usable best_config — silently useless. Surface the
            # incompatibility at run-creation time with an actionable
            # message instead.
            from msai.services.portfolio_backtest.objectives import (  # noqa: PLC0415
                OBJECTIVES,
            )

            objective_value = portfolio.objective
            if isinstance(objective_value, PortfolioObjective):
                objective_enum = objective_value
            else:
                # Codex bot iter-8 P2 on PR #73: legacy DB rows store the
                # pre-rename ``max_sharpe`` spelling. The rest of the
                # portfolio stack normalizes that via ``_coerce_objective``;
                # mirror the alias here so existing portfolios can still
                # launch Full runs after this gate landed.
                raw_str = str(objective_value)
                normalized = "maximize_sharpe" if raw_str == "max_sharpe" else raw_str
                try:
                    objective_enum = PortfolioObjective(normalized)
                except ValueError as exc:
                    raise ValueError(
                        f"Full mode portfolio has an unknown objective "
                        f"`{raw_str}`; valid values: "
                        f"{sorted(o.value for o in PortfolioObjective)}"
                    ) from exc
            if objective_enum not in OBJECTIVES:
                raise ValueError(
                    f"Full mode requires an objective with a scorer "
                    f"(one of {sorted(o.value for o in OBJECTIVES)}); "
                    f"portfolio objective is `{objective_enum.value}`, "
                    "which has no optimizer scoring function. Re-create "
                    "the portfolio with a maximize_* / minimize_* objective "
                    "or run in Quick mode."
                )

        run = PortfolioRun(
            portfolio_id=portfolio_id,
            start_date=data.start_date,
            end_date=data.end_date,
            max_parallelism=data.max_parallelism,
            status=PortfolioRunStatus.PENDING.value,
            # F2 + Codex-bot PR-73 P2: mode resolution = explicit > portfolio default.
            mode=resolved_mode,
            metrics=initial_metrics,
            created_by=user_id,
        )
        session.add(run)
        await session.flush()

        log.info(
            "portfolio_run_created",
            run_id=str(run.id),
            portfolio_id=str(portfolio_id),
            start_date=str(data.start_date),
            end_date=str(data.end_date),
            mode=resolved_mode.value,
            mode_inherited=data.mode is None,
            n_trials_override=data.n_trials,
        )
        return run

    @staticmethod
    async def list_runs(
        session: AsyncSession,
        portfolio_id: UUID | None = None,
        limit: int = 100,
    ) -> builtins.list[PortfolioRun]:
        """List portfolio runs, optionally filtered by portfolio.

        Args:
            session: Active async database session.
            portfolio_id: Optional FK filter. If provided, only runs for this
                portfolio are returned.
            limit: Maximum number of rows to return.

        Returns:
            A list of :class:`PortfolioRun` rows.  ``series`` and
            ``allocations`` are **not** loaded (defer) — those columns
            can be multi-MB JSONB for completed intraday runs, and a
            list-history view doesn't need them.  Callers that require
            the full payload should use :meth:`get_run` with the row id.
        """
        stmt = (
            select(PortfolioRun)
            .options(defer(PortfolioRun.series), defer(PortfolioRun.allocations))
            .order_by(PortfolioRun.created_at.desc())
            .limit(limit)
        )
        if portfolio_id is not None:
            stmt = stmt.where(PortfolioRun.portfolio_id == portfolio_id)
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        # Detach + null the deferred attrs so Pydantic serialization
        # does not trigger a lazy-load per row (which would defeat the
        # defer and introduce an N+1).  Full payload is still available
        # via :meth:`get_run`.
        for row in rows:
            session.expunge(row)
            row.series = None
            row.allocations = None
        return rows

    @staticmethod
    async def get_run(
        session: AsyncSession,
        run_id: UUID,
    ) -> PortfolioRun:
        """Get a single portfolio run by ID. Raises ValueError if not found.

        Args:
            session: Active async database session.
            run_id: Primary key of the run row.

        Returns:
            The :class:`PortfolioRun` row.

        Raises:
            ValueError: If the run does not exist.
        """
        run = await session.get(PortfolioRun, run_id)
        if run is None:
            raise ValueError(f"Portfolio run {run_id} not found")
        return run

    @staticmethod
    async def count_runs(
        session: AsyncSession,
        portfolio_id: UUID | None = None,
    ) -> int:
        """Return the total number of portfolio runs, optionally filtered."""
        stmt = select(func.count()).select_from(PortfolioRun)
        if portfolio_id is not None:
            stmt = stmt.where(PortfolioRun.portfolio_id == portfolio_id)
        result = await session.execute(stmt)
        return result.scalar_one()

    # ------------------------------------------------------------------
    # PortfolioRun status transitions
    # ------------------------------------------------------------------

    @staticmethod
    async def mark_run_running(
        session: AsyncSession,
        run_id: UUID,
    ) -> PortfolioRun:
        """Mark a run ``running`` and stamp the heartbeat.

        Refuses to transition out of a terminal state (``completed`` /
        ``failed``).  This protects against arq retry loops silently
        re-running a run that has already finished — an arq-level retry
        would otherwise pick the row back up and flip ``failed`` back to
        ``running``, overwriting the persisted error.

        Raises:
            PortfolioOrchestrationError: If the run does not exist.
            PortfolioRunTerminalStateError: If the run is already in a
                terminal state.
        """
        # Local import to avoid an import cycle with orchestration.py
        from msai.services.portfolio.orchestration import (
            PortfolioOrchestrationError,
            PortfolioRunTerminalStateError,
        )

        run = await session.get(PortfolioRun, run_id)
        if run is None:
            raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
        current = PortfolioRunStatus(run.status)
        if current.is_terminal:
            raise PortfolioRunTerminalStateError(
                f"Portfolio run {run_id} is already {current.value}; refusing to restart"
            )
        run.status = PortfolioRunStatus.RUNNING.value
        run.heartbeat_at = datetime.now(UTC)
        run.error_message = None
        await session.commit()
        await session.refresh(run)
        return run

    @staticmethod
    async def heartbeat_run(
        session: AsyncSession,
        run_id: UUID,
    ) -> None:
        """Refresh ``heartbeat_at`` on a run row.

        Called by the worker's lease-renewal loop so a future stale-job
        scanner (job_watchdog extension) can distinguish an actively-
        executing run from an abandoned ``running`` row.  Silent no-op
        if the row is already terminal or has been deleted — a heartbeat
        on a finished row would be pointless, and crashing the renewal
        loop would impact the compute-slot lease.
        """
        run = await session.get(PortfolioRun, run_id)
        if run is None:
            return
        try:
            status = PortfolioRunStatus(run.status)
        except ValueError:
            return
        if status.is_terminal:
            return
        run.heartbeat_at = datetime.now(UTC)
        await session.commit()

    @staticmethod
    async def mark_run_failed(
        session: AsyncSession,
        run_id: UUID,
        *,
        error_message: str,
    ) -> PortfolioRun:
        """Mark a run ``failed`` with an operator-visible error message.

        Idempotent on already-failed rows (refresh completed_at / error);
        refuses to overwrite a ``completed`` row to avoid data loss.
        """
        # Local import to avoid an import cycle with orchestration.py
        from msai.services.portfolio.orchestration import (
            PortfolioOrchestrationError,
            PortfolioRunTerminalStateError,
        )

        run = await session.get(PortfolioRun, run_id)
        if run is None:
            raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
        # Refuse to overwrite ANY terminal status — not just ``completed``.
        # A canceled run must STAY canceled (the operator's explicit stop
        # wins over a worker that crashes mid-flight). An already-failed
        # row should also not have its error_message replaced — the FIRST
        # observed failure is the diagnostic signal.
        current = PortfolioRunStatus(run.status)
        if current.is_terminal:
            raise PortfolioRunTerminalStateError(
                f"Portfolio run {run_id} is already {current.value}; refusing to mark failed"
            )
        run.status = PortfolioRunStatus.FAILED.value
        run.error_message = error_message
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        return run


# ----------------------------------------------------------------------
# F1c bridge: strategy → default-GraduationCandidate
# ----------------------------------------------------------------------


async def _get_or_create_default_candidate(
    session: AsyncSession,
    strategy_id: UUID,
) -> GraduationCandidate:
    """Idempotently return the default :class:`GraduationCandidate` for a strategy.

    Uses ``stage == "portfolio_default"`` as the deterministic idempotency
    key — ``GraduationCandidate`` has no ``name`` column (verified against
    ``backend/src/msai/models/graduation_candidate.py``: only id,
    strategy_id, research_job_id, stage, config, metrics, deployment_id,
    notes, promoted_by, promoted_at). The ``stage`` column is
    ``String(32)`` with no DB enum constraint, so adding a new stage
    value is purely additive.

    On miss, creates a new candidate seeded from the strategy's
    ``default_config`` (or ``{}`` when null) and an empty ``metrics``
    dict. The empty ``metrics`` will make heuristic weight derivation
    fall back to equal-weight at orchestration time — operators who want
    metric-driven weights should run a real research job to populate
    them, then compose the portfolio against that promoted candidate via
    the legacy ``allocations`` path instead.

    Args:
        session: Active async database session.
        strategy_id: Primary key of the strategy this candidate represents.

    Returns:
        The canonical default candidate row for ``strategy_id`` (flushed,
        not committed).

    Raises:
        ValueError: If the referenced strategy does not exist.
    """
    stmt = select(GraduationCandidate).where(
        GraduationCandidate.strategy_id == strategy_id,
        GraduationCandidate.stage == _PORTFOLIO_DEFAULT_STAGE,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    strategy = await session.get(Strategy, strategy_id)
    if strategy is None:
        raise ValueError(f"Strategy {strategy_id} not found")

    # Seed from strategy.default_config so the orchestration layer's
    # _resolve_allocations finds ``instruments`` / ``asset_class`` etc.
    # without needing a separate post-create patch step.
    candidate_config: dict[str, object] = dict(strategy.default_config or {})

    # F1c contract bridge: strategy ``default_config`` schemas are
    # per-strategy and typically capture a singular ``instrument_id``
    # (Nautilus ``StrategyConfig`` contract), but the orchestrator's
    # ``_resolve_allocations`` reads the *plural* ``instruments`` list
    # (portfolio-level concern).  Translate singular → plural here so
    # the bridge produces immediately-runnable candidates.  If the
    # strategy author already supplied a plural ``instruments`` list,
    # leave it alone (operator override wins).  If neither shape exists,
    # fail loudly here rather than at run-time inside the worker — a
    # bridge that creates a candidate the orchestrator cannot resolve
    # is worse than a clear compose-time error.
    if "instruments" not in candidate_config:
        singular = candidate_config.get("instrument_id") or candidate_config.get("symbol")
        if singular is not None:
            candidate_config["instruments"] = [singular]
        else:
            raise ValueError(
                f"Strategy {strategy_id} default_config has no 'instrument_id' / 'symbol' "
                "/ 'instruments' field; cannot auto-derive a runnable portfolio candidate. "
                "Either set default_config['instrument_id'] in the strategy file, or compose "
                "the portfolio via the explicit allocations path with a pre-populated "
                "GraduationCandidate."
            )

    # Portfolio orchestration defaults asset_class to "stocks" when missing,
    # but materializing it on the candidate keeps the resolved allocation
    # rows self-describing and stops a future asset-class default change
    # from silently retro-routing existing candidates.
    candidate_config.setdefault("asset_class", "stocks")

    # Codex bot iter-10 P1 on PR #73: wrap the INSERT in a SAVEPOINT
    # (``session.begin_nested``) so an IntegrityError from the partial
    # unique index (``uq_portfolio_default_candidate_per_strategy``,
    # migration ``72ea2fd4dda2``) rolls back ONLY this INSERT — not the
    # whole portfolio creation transaction. The loser re-reads and
    # returns the winning candidate; the helper stays idempotent under
    # concurrent contention.
    candidate = GraduationCandidate(
        strategy_id=strategy_id,
        stage=_PORTFOLIO_DEFAULT_STAGE,
        config=candidate_config,
        # Empty metrics — heuristic weight derivation will fall back to
        # equal-weight; promote-to-live gating treats this as "untested".
        metrics={},
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
    except IntegrityError:
        # The losing concurrent caller. Re-read the winning row.
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            # Index violated but the row vanished — operator-driven race
            # (e.g. concurrent delete). Re-raise so the caller sees it.
            raise
        log.info(
            "portfolio_default_candidate_reused_after_conflict",
            strategy_id=str(strategy_id),
            candidate_id=str(existing.id),
        )
        return existing
    log.info(
        "portfolio_default_candidate_created",
        strategy_id=str(strategy_id),
        candidate_id=str(candidate.id),
    )
    return candidate
