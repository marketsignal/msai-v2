"""Portfolio orchestration -- combined-backtest execution + thin CRUD facade.

Hosts :class:`PortfolioService` plus the two error classes
(:class:`PortfolioOrchestrationError`, :class:`PortfolioRunTerminalStateError`)
and the orchestration-only module-level helpers used during a portfolio
backtest run.

After Task A2 the CRUD / lifecycle / status-transition method **bodies** live
in :class:`msai.services.portfolio.lifecycle.PortfolioLifecycle`.  The
matching ``PortfolioService.*`` instance methods are thin delegates kept for
back-compat with existing callers; Task A4 sweeps the call sites onto
``PortfolioLifecycle`` directly and the delegates go away.

After Task A3 the pure-computation helpers (:func:`heuristic_weight`,
:func:`effective_leverage`, :func:`load_benchmark_returns`,
:func:`raw_benchmark_symbol`) live in :mod:`msai.services.portfolio.computation`
and are imported below.  The remaining module-level helpers
(``_coerce_objective``, ``_prepare_strategy_config``,
``_extract_returns_from_account``) are orchestration-shaped — they translate
DB rows / runner outputs into the orchestration DAG and stay here.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_enums import BacktestMode, PortfolioObjective, PortfolioRunStatus
from msai.models.portfolio_run import PortfolioRun
from msai.services.analytics_math import (
    build_series_from_returns,
    combine_weighted_returns,
    compute_alpha_beta,
    compute_series_metrics,
    dataframe_to_series_payload,
    normalize_weights,
)
from msai.services.market_data_query import MarketDataQuery
from msai.services.nautilus.backtest_runner import BacktestResult, BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.portfolio.computation import (
    effective_leverage,
    heuristic_weight,
    load_benchmark_returns,
)
from msai.services.portfolio.lifecycle import PortfolioLifecycle
from msai.services.report_generator import ReportGenerator

if TYPE_CHECKING:
    import builtins
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.strategy import Strategy
    from msai.schemas.portfolio import PortfolioCreate, PortfolioRunCreate

log = get_logger(__name__)


class PortfolioOrchestrationError(Exception):
    """Raised when a portfolio backtest cannot be executed.

    Distinguishes orchestration/data-shape problems (missing instruments,
    missing candidates, run-not-found) from infrastructure errors (DB,
    Redis, subprocess crashes).  Subclasses ``Exception`` directly — NOT
    ``ValueError`` — so a stray Pydantic-style ``except ValueError`` at an
    HTTP boundary cannot silently swallow orchestration failures.
    """


class PortfolioRunTerminalStateError(PortfolioOrchestrationError):
    """Raised when a caller tries to transition a run out of a terminal state.

    Terminal states (``completed``, ``failed``) are sticky — this protects
    against arq retry loops re-running a run that has already finished (or
    permanently failed) and silently overwriting its persisted state.
    """


class PortfolioRunMemberFailureError(PortfolioOrchestrationError):
    """Raised when one or more member backtests fail during Quick-mode execution.

    Carries a structured ``per_strategy_errors`` payload — a list of dicts
    each with ``strategy_id`` / ``candidate_id`` / ``error_type`` /
    ``message`` — so the worker can persist exactly WHICH member raised on
    the :class:`PortfolioRun` row.  PRD US-002a requires the failure
    response to identify the broken member; the bare-``asyncio.gather``
    behaviour (first-exception-propagates with no attribution) was the gap
    Phase 5.1 review flagged.
    """

    def __init__(self, per_strategy_errors: list[dict[str, str]]) -> None:
        self.per_strategy_errors = per_strategy_errors
        names = [
            err.get("strategy_name") or err.get("strategy_id") or "<unknown>"
            for err in per_strategy_errors
        ]
        summary = (
            f"{len(per_strategy_errors)} strategy failed: {names[0]}"
            if len(per_strategy_errors) == 1
            else f"{len(per_strategy_errors)} strategies failed: {', '.join(names)}"
        )
        super().__init__(summary)


class PortfolioService:
    """Manages portfolio lifecycle: creation, allocation, and combined backtest runs."""

    # ------------------------------------------------------------------
    # CRUD / lifecycle delegates
    #
    # The actual SQLAlchemy + validation bodies live in
    # :class:`msai.services.portfolio.lifecycle.PortfolioLifecycle` (Task A2
    # split).  These instance-method wrappers exist so existing callers that
    # do ``PortfolioService().create(...)`` keep working unchanged.  Task A4
    # will sweep call sites onto ``PortfolioLifecycle`` directly and these
    # delegates will be deleted.
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        data: PortfolioCreate,
        user_id: UUID | None = None,
    ) -> Portfolio:
        """Delegate to :meth:`PortfolioLifecycle.create`."""
        return await PortfolioLifecycle.create(session, data, user_id)

    async def list(
        self,
        session: AsyncSession,
        limit: int = 100,
    ) -> builtins.list[Portfolio]:
        """Delegate to :meth:`PortfolioLifecycle.list`."""
        return await PortfolioLifecycle.list(session, limit)

    async def get(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> Portfolio:
        """Delegate to :meth:`PortfolioLifecycle.get`."""
        return await PortfolioLifecycle.get(session, portfolio_id)

    async def get_allocations(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> builtins.list[PortfolioAllocation]:
        """Delegate to :meth:`PortfolioLifecycle.get_allocations`."""
        return await PortfolioLifecycle.get_allocations(session, portfolio_id)

    async def create_run(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
        data: PortfolioRunCreate,
        user_id: UUID | None = None,
    ) -> PortfolioRun:
        """Delegate to :meth:`PortfolioLifecycle.create_run`."""
        return await PortfolioLifecycle.create_run(session, portfolio_id, data, user_id)

    async def list_runs(
        self,
        session: AsyncSession,
        portfolio_id: UUID | None = None,
        limit: int = 100,
    ) -> builtins.list[PortfolioRun]:
        """Delegate to :meth:`PortfolioLifecycle.list_runs`."""
        return await PortfolioLifecycle.list_runs(session, portfolio_id, limit)

    async def get_run(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> PortfolioRun:
        """Delegate to :meth:`PortfolioLifecycle.get_run`."""
        return await PortfolioLifecycle.get_run(session, run_id)

    async def count(self, session: AsyncSession) -> int:
        """Delegate to :meth:`PortfolioLifecycle.count`."""
        return await PortfolioLifecycle.count(session)

    async def count_runs(
        self,
        session: AsyncSession,
        portfolio_id: UUID | None = None,
    ) -> int:
        """Delegate to :meth:`PortfolioLifecycle.count_runs`."""
        return await PortfolioLifecycle.count_runs(session, portfolio_id)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run_portfolio_backtest(
        self,
        run_id: UUID,
        *,
        runner: BacktestRunner | None = None,
        report_generator: ReportGenerator | None = None,
        market_data_query: MarketDataQuery | None = None,
        session_factory: Any = None,
        max_workers: int | None = None,
        cancel_check: Any = None,
        progress_callback: Any = None,
    ) -> PortfolioRun:
        """Execute a portfolio backtest end-to-end and persist the result.

        Branches on :attr:`PortfolioRun.mode`:

        - ``QUICK`` — the existing single-shot path (unchanged): one
          backtest per candidate, weighted-return combine, QuantStats
          tearsheet, results persisted to the run row.
        - ``FULL`` — delegates to :meth:`_run_full_mode` which drives
          :func:`msai.services.portfolio_backtest.optimizer.run_portfolio_walk_forward`
          (walk-forward + Optuna optimization, IS/OOS scores, trial trace).

        Orchestrates (Quick path):
            1. Load portfolio + allocations + graduation candidates + strategies.
            2. Resolve effective weights (explicit or heuristic) and normalize.
            3. Run each candidate's backtest in parallel via
               :class:`BacktestRunner`.
            4. Combine weighted returns and apply downside-target leverage
               scaling.
            5. Compute portfolio-level metrics (optionally vs. a benchmark).
            6. Generate a QuantStats tearsheet and persist all outputs to
               the :class:`PortfolioRun` row.

        Args:
            run_id: UUID of the :class:`PortfolioRun` to execute.
            runner: Backtest runner to use.  A new
                :class:`BacktestRunner` is constructed if omitted.  Tests
                can inject a stub runner to avoid subprocess overhead.
            report_generator: Report generator to use.  Defaults to a new
                :class:`ReportGenerator`.
            market_data_query: Market-data query service for benchmark
                returns.  Defaults to one wired to ``settings.data_root``.
            session_factory: Async session factory to use.  Defaults to the
                module-level :func:`async_session_factory`.  Tests can inject
                a factory bound to an isolated Postgres container.
            max_workers: Hard cap on parallel candidate backtests — must
                be the compute-slot reservation the caller holds so we
                never oversubscribe the cluster semaphore.  ``None``
                falls back to ``min(max_parallelism, compute_slot_limit)``
                for direct invocations (e.g. tests).

        Returns:
            The completed :class:`PortfolioRun` row (status ``completed``).

        Raises:
            PortfolioOrchestrationError: On data-shape problems (missing
                candidate, empty instruments, unknown portfolio).
        """
        factory = session_factory or async_session_factory

        # ---- Phase 0: mode branch ----
        # Peek at run.mode in a short-lived session so the Full path doesn't
        # pay the cost of building the Quick runner/report-generator/
        # market-data-query defaults below.  Quick path keeps the original
        # behaviour verbatim — every assertion in existing tests against
        # the Quick orchestration DAG continues to hold.
        async with factory() as preamble_session:
            preamble_run = await preamble_session.get(PortfolioRun, run_id)
            if preamble_run is None:
                raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
            run_mode = (
                preamble_run.mode.value
                if isinstance(preamble_run.mode, BacktestMode)
                else str(preamble_run.mode)
            )

        if run_mode == BacktestMode.FULL.value:
            # Full mode owns its own session/persistence lifecycle inside
            # _run_full_mode.  Returning early keeps the Quick body below
            # untouched so this change is a strict extension, not a rewrite.
            # ``runner`` threads through for the Phase 5.1 P0-A fix — the
            # Full path now caches REAL per-strategy returns by running each
            # member through the same backtest runner before invoking the
            # optimizer, so tests can inject a stub the same way the Quick
            # path does.  ``max_workers`` is the compute-slot lease held by
            # the caller (the arq worker) and MUST win over the per-run
            # ``max_parallelism`` knob so the Full-mode cache warmup does
            # not oversubscribe the cluster semaphore (Phase 5.1 iter-2 P1).
            return await self._run_full_mode(
                run_id,
                factory=factory,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
                runner=runner,
                max_workers=max_workers,
            )

        runner = runner or BacktestRunner()
        report_generator = report_generator or ReportGenerator()
        market_data_query = market_data_query or MarketDataQuery(str(settings.data_root))

        # ---- Phase 1: read-only load ----
        # Intentionally non-transactional — we release the session before the
        # slow CPU-bound backtests so a single connection is not held across
        # 10+ minute subprocess runs.  Do not add writes inside this block.
        async with factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
            portfolio = await session.get(Portfolio, run.portfolio_id)
            if portfolio is None:
                raise PortfolioOrchestrationError(
                    f"Portfolio {run.portfolio_id} not found for run {run_id}"
                )
            allocations = await self._load_allocations(session, run.portfolio_id)
            if not allocations:
                raise PortfolioOrchestrationError(
                    f"Portfolio {run.portfolio_id} has no allocations"
                )

            resolved = self._resolve_allocations(
                allocations=allocations,
                objective=_coerce_objective(portfolio.objective),
            )
            start_date = str(run.start_date)
            end_date = str(run.end_date)
            max_parallelism = run.max_parallelism
            requested_leverage = float(portfolio.requested_leverage)
            downside_target = (
                float(portfolio.downside_target) if portfolio.downside_target is not None else None
            )
            benchmark_symbol = portfolio.benchmark_symbol
            base_capital = float(portfolio.base_capital)

        # ---- Phase 2: run the backtests (no DB session held) ----
        # Caller-supplied ``max_workers`` is the reserved slot count and
        # wins unconditionally — we must not launch more threads than
        # the compute-slot lease authorizes, even if the run row says
        # otherwise.  When unset (tests / direct invocation), fall back
        # to the run's own cap.
        effective_max_workers = max_workers if max_workers is not None else max_parallelism

        # Pre-build catalogs serially so parallel candidate backtests
        # don't race on shared symbols.  ``ensure_catalog_data`` only
        # checks for catalog existence with an unsynchronized read; two
        # threads racing on a cold ``SPY`` catalog could double-write.
        # Pre-warming makes the per-candidate ``ensure_catalog_data``
        # call inside ``_run_candidate_backtest`` a fast no-op.
        #
        # Run in an executor — building a cold catalog can stream and
        # convert minutes of Parquet, which blocks the event loop and
        # would stall the worker's background lease-renewal task.
        symbols_by_asset: dict[str, set[str]] = {}
        for allocation in resolved:
            asset = str(allocation.get("asset_class") or "stocks")
            symbols_by_asset.setdefault(asset, set()).update(allocation["instruments"])
        loop = asyncio.get_running_loop()
        for asset_class, symbols in symbols_by_asset.items():
            # Default-arg capture via a named nested function. Mypy can't
            # infer types through the default-arg-lambda pattern, and a blanket
            # `type: ignore` on the lambda is worse than this explicit form.
            def _run_ingest(
                ac: str = asset_class,
                syms: list[str] = sorted(symbols),  # noqa: B008
            ) -> None:
                ensure_catalog_data(
                    symbols=syms,
                    raw_parquet_root=settings.parquet_root,
                    catalog_root=settings.nautilus_catalog_root,
                    asset_class=ac,
                )

            await loop.run_in_executor(None, _run_ingest)

        strategy_results = await self._execute_candidate_backtests(
            runner=runner,
            allocations=resolved,
            start_date=start_date,
            end_date=end_date,
            max_parallelism=effective_max_workers,
        )

        weighted_series = [
            (
                str(item["candidate_id"]),
                float(item["weight"]),
                pd.Series(
                    item["returns"],
                    index=pd.to_datetime(item["timestamps"], utc=True),
                ),
            )
            for item in strategy_results
        ]

        # Local variable kept distinct from the imported ``effective_leverage``
        # function to avoid shadowing — the function is used in tests / future
        # optimizer (Task E1) callers, and a local rebind would block re-import.
        portfolio_leverage = effective_leverage(
            weighted_series=weighted_series,
            requested_leverage=requested_leverage,
            downside_target=downside_target,
        )
        combined_returns = combine_weighted_returns(weighted_series, leverage=portfolio_leverage)
        benchmark_returns = load_benchmark_returns(
            market_data_query,
            benchmark_symbol=benchmark_symbol,
            start_date=start_date,
            end_date=end_date,
        )
        # Core metrics (sharpe, sortino, max_drawdown, win_rate, vol,
        # downside_risk, total_return) stay on the strategy's native
        # frequency — resampling would silently change their meaning
        # depending on whether a benchmark is set, making the numbers
        # non-comparable across runs.  Only alpha/beta need the
        # frequency-aligned pair (``load_benchmark_returns`` returns a
        # daily series for memory reasons), so we compute those
        # separately on compounded daily portfolio returns and merge.
        core = compute_series_metrics(combined_returns).as_dict()
        alpha: float | None = None
        beta: float | None = None
        if benchmark_returns is not None and not benchmark_returns.empty:
            daily_portfolio = (
                combined_returns.resample("1D").apply(lambda r: (1.0 + r).prod() - 1.0).dropna()
            )
            alpha, beta = compute_alpha_beta(daily_portfolio, benchmark_returns)
        metrics = {**core, "alpha": alpha, "beta": beta}
        metrics["num_strategies"] = len(strategy_results)
        metrics["effective_leverage"] = portfolio_leverage
        # Equity curve stays at the strategy's native frequency for the
        # UI chart — never resampled so intraday detail is preserved.
        series_frame = build_series_from_returns(combined_returns, base_value=base_capital)

        # QuantStats / ReportGenerator.generate_tearsheet expects the benchmark
        # as a *returns* series, not a cumulative equity curve — pass it
        # through as-is.  Converting to (1+r).cumprod()-1 here would overlay a
        # meaningfully wrong benchmark on the tearsheet.
        #
        # Offload the blocking QuantStats call + file write to a thread so
        # the compute-slot lease-renewal task keeps getting scheduled; for
        # intraday runs the tearsheet generation alone can exceed the
        # 120s lease TTL and another job would otherwise reclaim our slots.
        loop = asyncio.get_running_loop()
        html = await loop.run_in_executor(
            None,
            lambda: report_generator.generate_tearsheet(
                combined_returns, benchmark=benchmark_returns
            ),
        )
        report_path = await loop.run_in_executor(
            None,
            lambda: report_generator.save_report(
                html, backtest_id=str(run_id), data_root=str(settings.data_root)
            ),
        )

        # ---- Phase 2.5: per-strategy results enrichment (Task H+ backend) ----
        # Builds the per-strategy equity / drawdown / correlation matrices
        # the H7 results page consumes, before persisting them on
        # ``walk_forward_payload``.  Done outside the DB session block so
        # the pandas frame builds + dict serialisation never hold a
        # connection.  Failure here MUST NOT fail the whole run — the
        # primary metrics + tearsheet are already computed, so we log and
        # ship without enrichment if anything throws.
        results_payload = _build_results_payload(
            strategy_results=strategy_results,
            initial_capital=base_capital,
        )

        # ---- Phase 3: persist results ----
        now = datetime.now(UTC)
        async with factory() as session:
            persisted = await session.get(PortfolioRun, run_id)
            if persisted is None:
                raise PortfolioOrchestrationError(
                    f"Portfolio run {run_id} disappeared during execution"
                )
            # Operator may have flipped the row to ``canceled`` while the
            # backtest was running.  The cancel is a terminal state that
            # MUST win — silently overwriting it with COMPLETED would lose
            # the user's explicit stop signal.  Re-read here (the
            # ``session.get`` above already issued a SELECT, so this is
            # the freshest value available at the moment of the write).
            if PortfolioRunStatus(persisted.status).is_terminal:
                log.warning(
                    "portfolio_run_skip_completed_write_terminal",
                    run_id=str(run_id),
                    terminal_status=persisted.status,
                )
                # Return the row as-is; the caller (arq worker) treats this
                # as a successful no-op so it does not surface as a job
                # failure that would block subsequent runs.
                return persisted
            persisted.status = PortfolioRunStatus.COMPLETED.value
            persisted.metrics = metrics
            persisted.series = dataframe_to_series_payload(series_frame)
            persisted.allocations = strategy_results
            persisted.report_path = report_path
            # Repurpose ``walk_forward_payload`` as the general "results
            # payload" for both Quick and Full modes (see Task spec).  Quick
            # mode has no walk-forward windows, so the field carried the
            # per-strategy enrichment only.  Full mode merges the optimizer's
            # ``walk_forward_payload`` with the enrichment below
            # (``_run_full_mode``).
            persisted.walk_forward_payload = results_payload
            persisted.heartbeat_at = now
            persisted.completed_at = now
            persisted.error_message = None
            await session.commit()
            await session.refresh(persisted)
            return persisted

    async def _run_full_mode(
        self,
        run_id: UUID,
        *,
        factory: Any,
        cancel_check: Any = None,
        progress_callback: Any = None,
        runner: BacktestRunner | None = None,
        max_workers: int | None = None,
    ) -> PortfolioRun:
        """Full mode: walk-forward + Optuna optimization via portfolio_backtest.optimizer.

        Loads the portfolio + allocations (using the same DB helpers as the
        Quick path), constructs a :class:`SafetyCaps` from the portfolio's
        risk knobs, defines a closure ``_trial_body`` that the optimizer
        injects as ``portfolio_backtest_fn``, and persists the IS/OOS scores
        + optimization trace + walk-forward payload to the run row.

        **Real cached returns (Phase 5.1 fix).**  Before the optimizer
        starts, each member strategy runs ONCE through the same Nautilus
        backtest path that Quick mode uses (``_execute_candidate_backtests``)
        over the full ``[run.start_date, run.end_date]`` window.  The
        per-strategy returns Series are cached in memory and the
        per-trial body slices the cache to the trial's window, applies the
        allocator + leverage scaler, and computes metrics from real data
        — NOT random noise.  The synthetic-returns generator that previously
        backed trials was a P0 finding (the optimizer's best_config was
        fitting noise, and that config was being merged into live revisions
        on promote-to-live).

        Compute envelope: caching N member returns at start adds ~N * Quick
        backtest cost (~3-5 min each for the typical 1-year window); the
        optimizer trial loop is then near-free (~50 ms per trial × 100 trials
        ≈ 5 s).  Total Full-mode wall clock for N=5 members ≈ 30 min — well
        within the PRD's 8-hour cap (§4.1).

        ``cancel_check`` and ``progress_callback`` propagate to the
        optimizer so the arq worker's status-poll + heartbeat writers can
        observe the run.
        """
        # Local imports keep optimizer + safety_caps off the import path of
        # Quick-mode callers (Optuna is heavy to import).
        from msai.services.portfolio_backtest.optimizer import run_portfolio_walk_forward
        from msai.services.portfolio_backtest.safety_caps import SafetyCaps

        async with factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
            portfolio = await session.get(Portfolio, run.portfolio_id)
            if portfolio is None:
                raise PortfolioOrchestrationError(
                    f"Portfolio {run.portfolio_id} not found for run {run_id}"
                )
            # Reuse the Quick-path loader so allocations come back with the
            # same eager-loaded shape (candidate + strategy attached).  The
            # plan reviewer flagged that Portfolio has no ``allocations``
            # reverse relationship — _load_allocations is the canonical
            # path.
            allocations = await self._load_allocations(session, portfolio.id)
            if not allocations:
                raise PortfolioOrchestrationError(f"Portfolio {portfolio.id} has no allocations")
            resolved = self._resolve_allocations(
                allocations=allocations,
                objective=_coerce_objective(portfolio.objective),
            )
            member_strategy_ids = [str(row["strategy_id"]) for row in resolved]
            start_date = run.start_date
            end_date = run.end_date
            initial_capital = float(portfolio.base_capital)
            allocator_name = portfolio.allocator_name or "equal_weight"
            objective = _coerce_objective(portfolio.objective)
            max_parallelism = run.max_parallelism
            # FAIL_STALE fix: honor the per-run ``n_trials`` override
            # (persisted by ``PortfolioLifecycle.create_run`` under
            # ``metrics['n_trials_override']``).  Quick-mode smoke tests
            # cap at 2-10 trials; production Full runs use the
            # ``settings.portfolio_full_trial_count`` default.
            n_trials_override: int | None = None
            run_metrics = run.metrics or {}
            raw_override = run_metrics.get("n_trials_override")
            if isinstance(raw_override, int) and raw_override > 0:
                n_trials_override = raw_override
            safety_caps = SafetyCaps(
                max_leverage=float(portfolio.requested_leverage or 1.0),
                max_position_size=(
                    float(portfolio.max_position_size)
                    if portfolio.max_position_size is not None
                    else None
                ),
                max_drawdown_halt=(
                    float(portfolio.max_drawdown_halt)
                    if portfolio.max_drawdown_halt is not None
                    else None
                ),
            )

        # ---- Phase 0: real per-strategy returns cache (Phase 5.1 P0-A fix) ----
        # Run each member strategy ONCE through the Quick-mode backtest path
        # over the full [start, end] window.  Cache the returns Series so the
        # per-trial body below can slice them by window without re-running
        # Nautilus per trial.  See docstring trade-off note.
        runner = runner or BacktestRunner()
        full_start_date = str(start_date)
        full_end_date = str(end_date)

        # Catalog pre-warm — identical to the Quick path so the per-candidate
        # ``ensure_catalog_data`` inside ``_run_candidate_backtest`` is a
        # fast no-op.
        symbols_by_asset: dict[str, set[str]] = {}
        for allocation in resolved:
            asset = str(allocation.get("asset_class") or "stocks")
            symbols_by_asset.setdefault(asset, set()).update(allocation["instruments"])
        loop = asyncio.get_running_loop()
        for asset_class, symbols in symbols_by_asset.items():

            def _run_ingest(
                ac: str = asset_class,
                syms: list[str] = sorted(symbols),  # noqa: B008
            ) -> None:
                ensure_catalog_data(
                    symbols=syms,
                    raw_parquet_root=settings.parquet_root,
                    catalog_root=settings.nautilus_catalog_root,
                    asset_class=ac,
                )

            await loop.run_in_executor(None, _run_ingest)

        # Reuse the existing parallel-candidate executor; failures bubble up
        # as ``PortfolioRunMemberFailureError`` exactly like Quick mode so
        # the operator gets attributed errors even on Full-mode caching
        # failures.
        #
        # ``max_workers`` is the compute-slot lease the worker holds and
        # MUST win over the operator-supplied ``max_parallelism`` to stay
        # within the cluster budget — otherwise a run with
        # ``max_parallelism=16`` would launch 16 parallel candidate
        # backtests even though the worker reserved only N slots (Phase
        # 5.1 iter-2 P1).  Direct invocations / tests that omit
        # ``max_workers`` fall back to the run's own cap.
        effective_max_workers = (
            max_workers
            if max_workers is not None
            else (max_parallelism if max_parallelism is not None else len(resolved))
        )
        cached_strategy_results = await self._execute_candidate_backtests(
            runner=runner,
            allocations=resolved,
            start_date=full_start_date,
            end_date=full_end_date,
            max_parallelism=effective_max_workers,
        )

        # Build the per-strategy returns cache, keyed by strategy_id so the
        # trial body can address it via the same ids the allocator does.
        returns_cache: dict[str, pd.Series] = {}
        for entry in cached_strategy_results:
            sid = str(entry["strategy_id"])
            timestamps = entry.get("timestamps") or []
            returns = entry.get("returns") or []
            if not timestamps or not returns or len(timestamps) != len(returns):
                # Empty / mismatched series — leave the strategy out of the
                # cache; the trial body's missing-cache fallback gives it a
                # zero-return Series so the optimizer keeps running.
                continue
            idx = pd.to_datetime(timestamps, utc=True)
            returns_cache[sid] = pd.Series(list(returns), index=idx, name=sid)

        # F2 + P0-A: returns-aggregation trial body backed by REAL cached
        # returns.  ``_aggregate_returns_trial`` slices the cache by the
        # trial's (start_date, end_date), applies the allocator + leverage,
        # and computes metrics from real per-strategy returns.
        def _trial_body(
            *,
            member_strategy_ids: list[str],
            allocator_name: str,
            risk_params: dict[str, float],
            start_date: Any,
            end_date: Any,
            initial_capital: float,
        ) -> dict[str, Any]:
            del initial_capital  # not used by the returns-aggregation path
            return _aggregate_returns_trial(
                member_strategy_ids=member_strategy_ids,
                allocator_name=allocator_name,
                risk_params=risk_params,
                start_date=start_date,
                end_date=end_date,
                returns_cache=returns_cache,
            )

        # The optimizer is sync (Optuna's ask/tell loop is blocking) — run
        # it in a worker thread so the event loop stays responsive for the
        # worker's lease-renewal + heartbeat tasks. The sync callbacks
        # passed in by the worker schedule async DB ops via
        # ``run_coroutine_threadsafe`` so they don't trip on the running
        # event loop.
        effective_n_trials = (
            n_trials_override
            if n_trials_override is not None
            else settings.portfolio_full_trial_count
        )
        # Scale the walk-forward train/test/step trio to the requested
        # date range.  The optimizer's defaults (252+63 days) require a
        # 315-day minimum; without scaling, a smoke / exploratory Full
        # run on a 6-month window raises ``ValueError("No walk-forward
        # windows fit ...")`` from ``build_walk_forward_windows`` before
        # the first heartbeat, leaving the run row stuck in ``running``
        # (Bug 1's deterministic-failure path now marks it failed, but
        # the user still gets a 0-window run with no results).  Scaling
        # in the orchestrator (not in ``build_walk_forward_windows``)
        # preserves the helper's tight contract — the helper is also
        # called by ``ResearchEngine`` for strategy-singular walk-forward,
        # whose defaults are intentionally annual.
        range_days = max(1, (end_date - start_date).days + 1)
        scaled_train, scaled_test, scaled_step = _scaled_walk_forward_params(range_days=range_days)
        result = await asyncio.to_thread(
            run_portfolio_walk_forward,
            portfolio_id=str(portfolio.id),
            member_strategy_ids=member_strategy_ids,
            allocator_name=allocator_name,
            objective=objective,
            safety_caps=safety_caps,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            train_days=scaled_train,
            test_days=scaled_test,
            step_days=scaled_step,
            n_trials=effective_n_trials,
            portfolio_backtest_fn=_trial_body,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )

        # ---- Per-strategy results enrichment (Task H+ backend) ----
        # Build per-strategy returns over the full run window from the
        # SAME real cached returns the optimizer used for trial evaluation,
        # then run the same enrichment helper as Quick mode.  This keeps
        # the H7 results page's correlation matrices + per-strategy
        # equity / drawdown faithful to the data the optimizer saw — no
        # synthetic-noise fallback in the production path.
        full_strategy_results = _build_full_mode_strategy_results(
            member_strategy_ids=member_strategy_ids,
            returns_cache=returns_cache,
        )
        enrichment = _build_results_payload(
            strategy_results=full_strategy_results,
            initial_capital=initial_capital,
        )
        # Merge the enrichment ON TOP of the optimizer's payload so the
        # walk-forward windows + trial metadata stay intact AND the
        # per-strategy / correlation keys land.  An accidental optimizer
        # key collision with one of our enrichment keys would clobber
        # silently, so an explicit merge order makes the precedence visible.
        merged_payload: dict[str, Any] = dict(result.walk_forward_payload or {})
        merged_payload.update(enrichment)

        # ---- Persist IS/OOS + trace + walk-forward payload ----
        now = datetime.now(UTC)
        async with factory() as session:
            persisted = await session.get(PortfolioRun, run_id)
            if persisted is None:
                raise PortfolioOrchestrationError(
                    f"Portfolio run {run_id} disappeared during execution"
                )
            # Same cancel-guard discipline as the Quick path — operator's
            # cancel during a long Full-mode run must NOT be silently
            # overwritten by the completion write.  The optimizer's own
            # ``cancel_check`` polls the row between trials and breaks out
            # early; this guard catches the race where the cancel lands
            # AFTER the last poll but BEFORE this commit.
            if PortfolioRunStatus(persisted.status).is_terminal:
                log.warning(
                    "portfolio_run_skip_completed_write_terminal",
                    run_id=str(run_id),
                    terminal_status=persisted.status,
                    mode="full",
                )
                return cast("PortfolioRun", persisted)
            persisted.status = PortfolioRunStatus.COMPLETED.value
            persisted.is_metric = result.is_metric
            persisted.oos_metric = result.oos_metric
            persisted.optimization_trace = result.optimization_trace
            persisted.walk_forward_payload = merged_payload
            persisted.metrics = {
                "is_metric": result.is_metric,
                "oos_metric": result.oos_metric,
                "generalization_gap": result.generalization_gap,
                "stability_ratio": result.stability_ratio,
                "best_config": result.best_config,
            }
            persisted.heartbeat_at = now
            persisted.completed_at = now
            persisted.error_message = None
            await session.commit()
            await session.refresh(persisted)
            # mypy can't follow Any-through-Any factory back to the
            # concrete PortfolioRun typing — cast to silence the warning
            # without weakening the runtime check above (None branch raises).
            return cast("PortfolioRun", persisted)

    async def mark_run_running(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> PortfolioRun:
        """Delegate to :meth:`PortfolioLifecycle.mark_run_running`."""
        return await PortfolioLifecycle.mark_run_running(session, run_id)

    async def heartbeat_run(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> None:
        """Delegate to :meth:`PortfolioLifecycle.heartbeat_run`."""
        await PortfolioLifecycle.heartbeat_run(session, run_id)

    async def mark_run_failed(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        error_message: str,
    ) -> PortfolioRun:
        """Delegate to :meth:`PortfolioLifecycle.mark_run_failed`."""
        return await PortfolioLifecycle.mark_run_failed(
            session, run_id, error_message=error_message
        )

    # ------------------------------------------------------------------
    # Internal orchestration helpers
    # ------------------------------------------------------------------

    async def _load_allocations(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> builtins.list[PortfolioAllocation]:
        """Eager-load allocations with candidate + strategy for orchestration."""
        stmt = (
            select(PortfolioAllocation)
            .where(PortfolioAllocation.portfolio_id == portfolio_id)
            .options(
                selectinload(PortfolioAllocation.candidate).selectinload(
                    GraduationCandidate.strategy
                )
            )
            .order_by(PortfolioAllocation.created_at)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    def _resolve_allocations(
        self,
        allocations: builtins.list[PortfolioAllocation],
        *,
        objective: PortfolioObjective,
    ) -> builtins.list[dict[str, Any]]:
        """Flatten DB allocations into orchestration-ready dicts.

        Pulls strategy file/class/config from the related
        :class:`GraduationCandidate` and :class:`Strategy`.  An allocation
        with ``weight is None`` triggers heuristic derivation from the
        candidate's metrics; an explicit weight (including fractional
        values like 0.001) is preserved.  Weights are normalized to sum to
        1.0 across the resolved allocations.
        """
        rows: builtins.list[dict[str, Any]] = []
        for allocation in allocations:
            candidate = allocation.candidate
            if candidate is None:
                raise PortfolioOrchestrationError(
                    f"Allocation {allocation.id} has no candidate loaded"
                )
            strategy: Strategy | None = candidate.strategy
            if strategy is None:
                raise PortfolioOrchestrationError(
                    f"Candidate {candidate.id} has no strategy loaded"
                )

            default_config = dict(strategy.default_config or {})
            candidate_config = dict(candidate.config or {})
            merged_config = {**default_config, **candidate_config}

            instruments = list(candidate_config.get("instruments") or []) or list(
                default_config.get("instruments") or []
            )
            if not instruments:
                raise PortfolioOrchestrationError(
                    f"Candidate {candidate.id} has no instruments configured"
                )
            asset_class = str(
                candidate_config.get("asset_class") or default_config.get("asset_class") or "stocks"
            )

            # ``None`` = derive heuristically.  Explicit weights (including
            # fractional ones) pass through verbatim — Pydantic's ``gt=0.0``
            # at the API boundary ensures a zero cannot reach us.
            if allocation.weight is None:
                weight = heuristic_weight(dict(candidate.metrics or {}), objective)
            else:
                weight = float(allocation.weight)

            rows.append(
                {
                    "candidate_id": str(candidate.id),
                    "strategy_id": str(strategy.id),
                    "strategy_name": strategy.name,
                    "strategy_file_path": strategy.file_path,
                    "strategy_class": strategy.strategy_class,
                    "config": merged_config,
                    "instruments": instruments,
                    "asset_class": asset_class,
                    "weight": weight,
                }
            )
        return normalize_weights(rows)

    async def _execute_candidate_backtests(
        self,
        *,
        runner: BacktestRunner,
        allocations: builtins.list[dict[str, Any]],
        start_date: str,
        end_date: str,
        max_parallelism: int | None,
    ) -> builtins.list[dict[str, Any]]:
        """Run every allocation's backtest, in parallel when configured.

        **Concurrency cap:** ``worker_count`` is clamped to
        ``settings.compute_slot_limit`` — the global semaphore that the
        caller reserved against.  Letting ``max_parallelism`` exceed it
        would launch more candidate backtests than the host is sized to
        run and defeat the slot budget.

        **Event-loop discipline:** even the single-worker path runs the
        blocking ``_run_candidate_backtest`` in an executor so the
        worker's lease-renewal task (and any other async background
        work) continue to be scheduled during long Nautilus subprocess
        runs.  A sequential inline call would starve the event loop.

        **Failure semantics:** any candidate raising propagates and the
        entire portfolio run fails.  This is intentional — a broken
        candidate would silently dilute the portfolio with a zero-return
        stream and lie about ``num_strategies`` in metrics.
        """
        requested = int(max_parallelism or 1)
        worker_count = max(
            1,
            min(len(allocations), requested, settings.compute_slot_limit),
        )

        loop = asyncio.get_running_loop()
        # Known limitation: when one candidate raises, ``asyncio.gather``
        # surfaces the exception immediately, but exiting the
        # ``with ThreadPoolExecutor`` block calls ``shutdown(wait=True)``.
        # The sibling threads are blocked inside Nautilus subprocess
        # ``.join()`` calls that can't be cancelled cleanly from here, so
        # the portfolio job continues to hold its compute slots until
        # every sibling backtest finishes or hits its own timeout.
        # Fixing this cleanly requires cooperative cancellation plumbed
        # through the subprocess boundary — tracked as a follow-up;
        # failing portfolios still complete with the correct error, they
        # just hold slots for slightly longer than strictly necessary.
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            tasks = [
                loop.run_in_executor(
                    executor,
                    self._run_candidate_backtest,
                    runner,
                    allocation,
                    start_date,
                    end_date,
                )
                for allocation in allocations
            ]
            # ``return_exceptions=True`` keeps every task's outcome visible
            # so we can attribute failures back to the specific candidate /
            # strategy that raised.  Bare ``asyncio.gather`` would surface
            # the FIRST exception and discard the rest — PRD US-002a
            # requires per-member attribution on Quick-mode failures.
            results_or_excs = await asyncio.gather(*tasks, return_exceptions=True)

        per_strategy_errors: list[dict[str, str]] = []
        successes: list[dict[str, Any]] = []
        for allocation, outcome in zip(allocations, results_or_excs, strict=True):
            if isinstance(outcome, BaseException):
                per_strategy_errors.append(
                    {
                        "strategy_id": str(allocation.get("strategy_id") or ""),
                        "strategy_name": str(allocation.get("strategy_name") or ""),
                        "candidate_id": str(allocation.get("candidate_id") or ""),
                        "error_type": type(outcome).__name__,
                        "message": str(outcome),
                    }
                )
            else:
                successes.append(outcome)

        if per_strategy_errors:
            # Don't swallow the diagnostic — wrap into the domain exception
            # the worker layer knows how to persist (see
            # ``portfolio_job.run_portfolio_job``).
            raise PortfolioRunMemberFailureError(per_strategy_errors)
        return successes

    def _run_candidate_backtest(
        self,
        runner: BacktestRunner,
        allocation: dict[str, Any],
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """Execute a single allocation's backtest and extract returns."""
        instrument_ids = ensure_catalog_data(
            symbols=list(allocation["instruments"]),
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
            asset_class=str(allocation.get("asset_class") or "stocks"),
        )

        # Mirror backtest_job's contract: inject instrument_id / bar_type
        # defaults so Nautilus StrategyConfig subclasses can instantiate
        # from a portfolio-level config that only captures instruments.
        strategy_config = _prepare_strategy_config(dict(allocation["config"]), instrument_ids)

        result: BacktestResult = runner.run(
            strategy_file=str(allocation["strategy_file_path"]),
            strategy_config=strategy_config,
            instrument_ids=instrument_ids,
            start_date=start_date,
            end_date=end_date,
            catalog_path=settings.nautilus_catalog_root,
            # Honor the operator-tuned timeout (matches the single-backtest
            # worker); otherwise the runner's 30-minute default silently
            # wins and per-deployment tuning is ignored.
            timeout_seconds=settings.backtest_timeout_seconds,
        )

        returns, timestamps = _extract_returns_from_account(
            result.account_df,
            candidate_id=str(allocation["candidate_id"]),
        )
        return {
            "candidate_id": str(allocation["candidate_id"]),
            "strategy_id": str(allocation["strategy_id"]),
            "strategy_name": str(allocation["strategy_name"]),
            "instruments": list(instrument_ids),
            "weight": float(allocation["weight"]),
            "metrics": dict(result.metrics),
            "returns": returns,
            "timestamps": timestamps,
        }


# ----------------------------------------------------------------------
# Module-level orchestration helpers
#
# Pure computation helpers (heuristic_weight, effective_leverage,
# raw_benchmark_symbol, load_benchmark_returns) live in ``computation.py``
# after Task A3 — the helpers below are orchestration-shaped: they
# translate DB rows / runner outputs into the orchestration DAG, so they
# stay with :class:`PortfolioService`.
# ----------------------------------------------------------------------


def _build_results_payload(
    *,
    strategy_results: list[dict[str, Any]],
    initial_capital: float,
) -> dict[str, Any]:
    """Build the per-strategy results payload for the H7 results page.

    From the orchestration's ``strategy_results`` list — each entry has
    ``candidate_id`` / ``strategy_id`` / ``returns`` / ``timestamps`` —
    build a wide returns DataFrame indexed by date with one column per
    strategy_id, then compute:

    - ``per_strategy_equity``: compounded equity curve per strategy.
    - ``per_strategy_drawdown``: underwater (drawdown) series per strategy.
    - ``return_correlation``: Pearson correlation matrix of returns.
    - ``drawdown_correlation``: Pearson correlation matrix of drawdowns.
    - ``drawdown_breakdown``: per-strategy max-drawdown + duration table.

    Serialised shape:

    - Matrices → nested dicts ``{row_id: {col_id: value}}``.
    - Equity/drawdown series → list-of-records
      ``[{"timestamp": iso, "strategy_id": sid, "value": float}, ...]``.
    - Drawdown breakdown → dict ``{strategy_id: {"max_drawdown": x,
      "duration_days": d, "recovered": bool}}`` so the H7 page's
      :func:`extractDrawdownBreakdown` consumes it directly.

    Any failure in shape construction (missing returns, mismatched
    timestamps) returns an empty ``{}`` rather than raising — the primary
    Quick-mode metrics + tearsheet are already persisted by the caller,
    so an enrichment failure must not fail the entire run.

    Imports are co-located with usage so the PostToolUse ruff formatter
    does not strip them as "unused" between subagent edits
    (see ``feedback_colocate_imports_with_usage_in_edits.md``).
    """
    # Local imports — co-located with usage so the formatter cannot strip
    # them as "unused" when the orchestration body is edited in isolation.
    from msai.services.portfolio_backtest.results import (
        compute_drawdown_breakdown,
        compute_drawdown_correlation,
        compute_drawdown_curves,
        compute_per_strategy_equity,
        compute_return_correlation,
    )

    if not strategy_results:
        return {}

    # ---- Build wide returns DataFrame (one column per strategy_id) ----
    series_by_strategy: dict[str, pd.Series] = {}
    for entry in strategy_results:
        sid = str(entry.get("strategy_id") or entry.get("candidate_id") or "")
        if not sid:
            continue
        returns = entry.get("returns") or []
        timestamps = entry.get("timestamps") or []
        if not returns or len(returns) != len(timestamps):
            continue
        idx = pd.to_datetime(timestamps, utc=True)
        series_by_strategy[sid] = pd.Series(list(returns), index=idx)

    if not series_by_strategy:
        return {}

    # Outer-join on the union of timestamps so partial-overlap windows are
    # represented as NaN (then dropped per-pair by ``.corr``); ``.fillna(0)``
    # for the equity compounding lets a strategy with a missing bar pass
    # through as zero return rather than collapsing the whole row.
    returns_df = pd.concat(series_by_strategy, axis=1).sort_index()

    try:
        equity = compute_per_strategy_equity(returns_df.fillna(0.0), initial_capital)
        drawdowns = compute_drawdown_curves(equity)
        return_corr = compute_return_correlation(returns_df)
        drawdown_corr = compute_drawdown_correlation(drawdowns)
        breakdown = compute_drawdown_breakdown(equity)
    except (ValueError, KeyError) as exc:  # narrow — pandas raises these
        log.warning("results_payload_build_failed", error=str(exc))
        return {}

    return {
        "per_strategy_equity": _series_records(equity, value_key="equity"),
        "per_strategy_drawdown": _series_records(drawdowns, value_key="drawdown"),
        "return_correlation": _matrix_to_dict(return_corr),
        "drawdown_correlation": _matrix_to_dict(drawdown_corr),
        "drawdown_breakdown": _breakdown_to_dict(breakdown, equity=equity),
    }


def _matrix_to_dict(matrix: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Serialise a square correlation matrix as ``{row_id: {col_id: val}}``.

    NaN entries are skipped — the H7 heatmap component renders missing
    cells distinctly from zero, and shipping ``null`` in JSON would break
    the strict ``typeof === "number"`` guard in ``extractMatrix``.
    """
    out: dict[str, dict[str, float]] = {}
    for row_id in matrix.index:
        row: dict[str, float] = {}
        for col_id in matrix.columns:
            value = matrix.at[row_id, col_id]
            if pd.notna(value):
                row[str(col_id)] = float(value)
        out[str(row_id)] = row
    return out


def _series_records(
    frame: pd.DataFrame,
    *,
    value_key: str,
) -> list[dict[str, Any]]:
    """Serialise a wide-by-strategy time series as a flat record list.

    The task spec calls for ``[{timestamp, strategy_id, value}]`` records,
    which lets the frontend group/pivot however it likes without paying
    the cost of a dict-of-arrays JSON unroll. ``value_key`` controls the
    field name (``"equity"`` for the equity curve, ``"drawdown"`` for
    underwater) so existing H7 extractor field names keep working.
    """
    records: list[dict[str, Any]] = []
    for ts in frame.index:
        iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        for sid in frame.columns:
            v = frame.at[ts, sid]
            if pd.isna(v):
                continue
            records.append(
                {
                    "timestamp": iso,
                    "strategy_id": str(sid),
                    value_key: float(v),
                }
            )
    return records


def _breakdown_to_dict(
    breakdown: pd.DataFrame,
    *,
    equity: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    """Serialise the drawdown-breakdown DataFrame as the H7-consumed dict.

    Output keyed by strategy_id; each row has ``max_drawdown`` (float),
    ``duration_days`` (int), and ``recovered`` (bool — true when the
    equity curve eventually recovered to its peak within the window).
    """
    out: dict[str, dict[str, Any]] = {}
    for sid in breakdown.index:
        max_dd = float(breakdown.at[sid, "max_drawdown"])
        duration_days = int(breakdown.at[sid, "drawdown_duration"])
        # Recovered iff the final equity equals or exceeds the running
        # max — same test ``compute_drawdown_breakdown`` uses internally.
        eq = equity[sid]
        recovered = bool(eq.iloc[-1] >= eq.cummax().iloc[-1])
        out[str(sid)] = {
            "max_drawdown": max_dd,
            "duration_days": duration_days,
            "recovered": recovered,
        }
    return out


def _build_full_mode_strategy_results(
    *,
    member_strategy_ids: list[str],
    returns_cache: dict[str, pd.Series],
) -> list[dict[str, Any]]:
    """Reconstruct a Quick-mode-shaped ``strategy_results`` for Full mode.

    Reads the real per-strategy returns cache that ``_run_full_mode``
    populated by running each member through the Quick-mode backtest path
    over the full run window.  The shape matches Quick mode's
    ``strategy_results`` so ``_build_results_payload`` consumes it
    unchanged — correlation matrices, drawdown breakdowns, and the H7
    results page all see real backtested data instead of the synthetic
    noise the pre-fix path emitted.
    """
    out: list[dict[str, Any]] = []
    for sid in member_strategy_ids:
        series = returns_cache.get(sid)
        if series is None or series.empty:
            continue
        out.append(
            {
                "candidate_id": sid,
                "strategy_id": sid,
                "returns": [float(v) for v in series.tolist()],
                "timestamps": [pd.Timestamp(ts).isoformat() for ts in series.index],
            }
        )
    return out


# Walk-forward defaults inside :func:`run_portfolio_walk_forward` are sized
# for a full year of training + a quarter of test (252+63 = 315 days), which
# is the right shape for an annual ``portfolio_full_trial_count`` run.  For
# shorter ranges (smoke runs, exploratory windows) those defaults yield zero
# windows and :func:`build_walk_forward_windows` raises ``ValueError`` before
# the first heartbeat.  ``_scaled_walk_forward_params`` rescales the trio
# proportionally to the requested ``range_days`` so any range >= 60 days
# fits at least one IS+OOS pair.  Floors at 30 days per leg keep statistical
# power non-zero; the schema-level 90-day minimum on ``mode=FULL`` runs
# guarantees the floors always have room (60d+30d window step still fits in
# a 90d range, leaving the optimizer at least one walk-forward window).
_DEFAULT_WALK_FORWARD_TRAIN_DAYS = 252
_DEFAULT_WALK_FORWARD_TEST_DAYS = 63
_DEFAULT_WALK_FORWARD_TOTAL_DAYS = (
    _DEFAULT_WALK_FORWARD_TRAIN_DAYS + _DEFAULT_WALK_FORWARD_TEST_DAYS
)
_WALK_FORWARD_LEG_FLOOR_DAYS = 30


def _scaled_walk_forward_params(*, range_days: int) -> tuple[int, int, int]:
    """Compute (train_days, test_days, step_days) sized to ``range_days``.

    Keeps the defaults (252 / 63 / 63) when the range comfortably fits them
    (``range_days >= 315``).  For shorter ranges, scales train/test
    proportionally — ~70% train, ~20% test — with a 30-day floor per leg.
    ``step_days`` always equals ``test_days`` so adjacent windows tile
    without overlap (matches the default behaviour of
    :func:`build_walk_forward_windows`).

    Args:
        range_days: Number of inclusive days in ``[start_date, end_date]``.

    Returns:
        Tuple of ``(train_days, test_days, step_days)`` suitable for
        :func:`build_walk_forward_windows`.
    """
    if range_days >= _DEFAULT_WALK_FORWARD_TOTAL_DAYS:
        return (
            _DEFAULT_WALK_FORWARD_TRAIN_DAYS,
            _DEFAULT_WALK_FORWARD_TEST_DAYS,
            _DEFAULT_WALK_FORWARD_TEST_DAYS,
        )

    # 70/20 split mirrors the default ratio (252/315 ≈ 0.80, but we keep
    # 70% to leave a step-buffer so at least one window fits when the
    # range is close to the floor).
    test_days = max(_WALK_FORWARD_LEG_FLOOR_DAYS, int(range_days * 0.2))
    # Train must leave room for the test leg inside the range, otherwise
    # no walk-forward window fits.  After flooring test_days at 30, the
    # available train budget is (range_days - test_days); clamp the
    # proportional train against it so the helper's contract holds even
    # at the 90-day schema minimum (where 70% would otherwise overshoot).
    proportional_train = int(range_days * 0.7)
    available_train = range_days - test_days
    train_days = max(_WALK_FORWARD_LEG_FLOOR_DAYS, min(proportional_train, available_train))
    step_days = test_days
    return train_days, test_days, step_days


def _coerce_objective(raw: Any) -> PortfolioObjective:
    """Map a stored/incoming objective value to the canonical enum.

    Accepts the :class:`PortfolioObjective` enum directly or any of its
    string values, and translates the legacy ``max_sharpe`` spelling
    (present in some existing rows) to ``maximize_sharpe``.  Raises
    :class:`PortfolioOrchestrationError` on an unknown string so we fail
    loudly rather than silently equal-weighting a misspelled objective.
    """
    if isinstance(raw, PortfolioObjective):
        return raw
    if isinstance(raw, str):
        # Legacy alias — older DB rows used "max_sharpe" before the rename.
        normalized = "maximize_sharpe" if raw == "max_sharpe" else raw
        try:
            return PortfolioObjective(normalized)
        except ValueError as exc:
            raise PortfolioOrchestrationError(f"Unknown portfolio objective: {raw!r}") from exc
    raise PortfolioOrchestrationError(f"Unexpected portfolio objective type: {type(raw).__name__}")


def _prepare_strategy_config(
    config: dict[str, Any],
    instrument_ids: list[str],
) -> dict[str, Any]:
    """Inject default ``instrument_id`` / ``bar_type`` for Nautilus strategies.

    Nautilus ``StrategyConfig`` subclasses typically require both fields;
    graduation-candidate configs often only capture ``instruments`` /
    ``asset_class`` (the portfolio-level concern) and rely on this helper
    to translate into the per-strategy contract before dispatch.  Mirrors
    the behavior of :func:`msai.workers.backtest_job._prepare_strategy_config`.

    **Empty-``order_id_tag`` scrub.**  Nautilus's strategy constructor builds
    ``StrategyId`` as ``f"{component_id}-{config.order_id_tag}"``
    (``nautilus_trader/trading/strategy.pyx:149``).  An empty-string
    ``order_id_tag`` produces ``"Strategy-"`` which the Rust validator
    rejects with a panic (``crates/model/src/identifiers/strategy_id.rs``:
    ``Condition failed: 'value' tag part (after '-') cannot be empty``) —
    the subprocess panics before the Python error-handler can write a
    pickle, and the parent runner surfaces an opaque ``EOFError``.  The
    base class default is ``order_id_tag=None``, which the validator
    accepts, so we strip empty values here; legacy stored configs (created
    when a strategy file shipped with ``order_id_tag: str = ""``) are
    transparently corrected at dispatch time, and operators don't have to
    re-create their portfolios after the strategy-file fix.
    """
    prepared = dict(config)
    if "instrument_id" not in prepared and instrument_ids:
        prepared["instrument_id"] = instrument_ids[0]
    if "bar_type" not in prepared and instrument_ids:
        prepared["bar_type"] = f"{instrument_ids[0]}-1-MINUTE-LAST-EXTERNAL"
    if prepared.get("order_id_tag") == "":
        del prepared["order_id_tag"]
    return prepared


def _extract_returns_from_account(
    account_df: pd.DataFrame,
    *,
    candidate_id: str = "",
) -> tuple[list[float], list[str]]:
    """Pull a ``(returns, timestamps)`` tuple out of a BacktestResult account frame.

    Tries the most-normalized column first (``returns``), then derives from
    equity columns if needed.  Returns empty lists when the frame has no
    usable data — the portfolio still runs; that candidate just contributes
    a zero-return stream.

    Logs at warning level on every non-empty-frame fall-through so that a
    silently zero-contributing candidate is visible to operators (it is
    still "graceful degradation" at the portfolio level, but the UI only
    sees ``num_strategies = N`` and hiding the degradation would mislead).
    """
    if account_df is None or account_df.empty:
        log.warning("portfolio_candidate_empty_account", candidate_id=candidate_id)
        return [], []

    frame = account_df.copy()

    # Prefer ``returns`` (already computed by the runner's account normalizer).
    if "returns" in frame.columns and "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame["returns"] = pd.to_numeric(frame["returns"], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "returns"])
        if frame.empty:
            log.warning(
                "portfolio_candidate_returns_all_nan",
                candidate_id=candidate_id,
            )
            return [], []
        return (
            [float(v) for v in frame["returns"].tolist()],
            [pd.Timestamp(ts).isoformat() for ts in frame["timestamp"].tolist()],
        )

    # Fall back to deriving from equity/net_liquidation via pct_change.
    for equity_col in ("equity", "net_liquidation", "total_equity", "balance"):
        if equity_col in frame.columns and "timestamp" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            frame[equity_col] = pd.to_numeric(frame[equity_col], errors="coerce")
            frame = frame.dropna(subset=["timestamp", equity_col])
            if frame.empty:
                log.warning(
                    "portfolio_candidate_equity_all_nan",
                    candidate_id=candidate_id,
                    equity_col=equity_col,
                )
                return [], []
            frame = frame.sort_values("timestamp").set_index("timestamp")
            returns = frame[equity_col].pct_change().fillna(0.0)
            return (
                [float(v) for v in returns.tolist()],
                [pd.Timestamp(ts).isoformat() for ts in returns.index.tolist()],
            )

    # Schema drift — none of the expected columns are present.  Log the
    # columns we did see so the operator can reconcile with the runner.
    log.warning(
        "portfolio_candidate_unknown_account_schema",
        candidate_id=candidate_id,
        columns=list(frame.columns),
    )
    return [], []


# ----------------------------------------------------------------------
# F2: returns-aggregation trial body (Full-mode optimizer)
# ----------------------------------------------------------------------


def _slice_cached_returns(
    series: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Series:
    """Slice a cached returns Series to the trial's [start, end] window.

    Tolerant of empty / missing series — returns an empty Series so the
    trial body's downstream "no usable data" branch can degrade gracefully
    (the optimizer keeps running and the trial scores zero).
    """
    if series is None or series.empty:
        return pd.Series(dtype=float)
    return series.loc[(series.index >= start) & (series.index <= end)]


def _aggregate_returns_trial(
    *,
    member_strategy_ids: list[str],
    allocator_name: str,
    risk_params: dict[str, float],
    start_date: Any,
    end_date: Any,
    returns_cache: dict[str, pd.Series] | None = None,
) -> dict[str, Any]:
    """Trial body: combine REAL cached per-strategy returns into portfolio metrics.

    Phase 5.1 P0-A fix: the trial body now reads from a pre-populated
    ``returns_cache`` populated once at the start of ``_run_full_mode`` by
    actually running each member through the Nautilus backtest path.  The
    previous implementation generated synthetic random returns per trial,
    which made the optimizer fit noise (and that "best_config" was then
    merged into live revisions on promote-to-live — a real-money bug).

    Steps:

    1. Slice each member's cached returns Series to ``[start_date,
       end_date]`` — the trial's window.
    2. Stack into a DataFrame so allocator helpers that need cross-strategy
       vol can compute weights (``inverse_vol``, ``vol_targeted``).
    3. Look up the allocator from
       :data:`msai.services.portfolio_backtest.allocators.ALLOCATORS`.
    4. Combine via :func:`combine_weighted_returns` scaled by the trial's
       ``leverage`` parameter.
    5. Compute portfolio metrics + emit ``total_leverage`` / ``max_position``
       so the post-evaluation cap-check in the optimizer has values to
       compare against :class:`SafetyCaps`.

    ``returns_cache`` is optional only for back-compat with existing unit
    tests that exercise the helper directly; production callers
    (``_run_full_mode``) MUST supply it.
    """
    from msai.services.portfolio_backtest.allocators import ALLOCATORS, EqualWeightAllocator

    cache = returns_cache or {}
    empty_metrics: dict[str, Any] = {
        "sharpe": 0.0,
        "sortino": 0.0,
        "total_return": 0.0,
        "max_drawdown": 0.0,
        "total_leverage": float(risk_params.get("leverage", 0.0)),
        "max_position": float(risk_params.get("position_size", 0.0)),
    }

    if not member_strategy_ids:
        return empty_metrics

    start = pd.to_datetime(start_date, utc=True)
    end = pd.to_datetime(end_date, utc=True)
    if end < start:
        return empty_metrics

    per_strategy: dict[str, pd.Series] = {}
    for sid in member_strategy_ids:
        sliced = _slice_cached_returns(cache.get(sid, pd.Series(dtype=float)), start, end)
        if sliced.empty:
            # Strategy has no data in this window; surface as a zero-return
            # contributor so the optimizer keeps running and the per-trial
            # metrics reflect the missing-data signal honestly (Sharpe and
            # friends fall toward zero rather than being papered over with
            # noise).
            per_strategy[sid] = pd.Series(dtype=float)
        else:
            per_strategy[sid] = sliced

    returns_df = pd.concat(per_strategy, axis=1).sort_index().fillna(0.0)
    if returns_df.empty or returns_df.shape[1] == 0:
        return empty_metrics

    # Allocator selection.  ``fixed_weight`` is rejected at the schema
    # layer for Full mode (see ``PortfolioCreate._full_mode_rejects_fixed_weight``)
    # so we never have to silently fall back to equal-weight here — an
    # unknown allocator name is a programming error and uses the
    # ``EqualWeightAllocator`` default.  Concrete allocators
    # (``inverse_vol`` / ``vol_targeted``) still need real per-strategy
    # vols to compute weights, which the cached real returns provide.
    allocator_cls = ALLOCATORS.get(allocator_name, EqualWeightAllocator)
    if allocator_cls is ALLOCATORS["fixed_weight"]:
        # Defense in depth — should never happen due to the schema validator.
        allocator_cls = EqualWeightAllocator
    try:
        allocator = allocator_cls()
        weights = allocator.compute(member_strategy_ids, returns_df)
    except (TypeError, ValueError):
        # Allocator preconditions not met for this trial's window (e.g.
        # zero-vol on every strategy) — equal-weight keeps the trial alive
        # so the rest of the sweep continues; the optimizer's IS/OOS
        # scoring will down-weight a degenerate window naturally.
        weights = EqualWeightAllocator().compute(member_strategy_ids, returns_df)

    leverage = float(risk_params.get("leverage", 1.0))
    max_position_param = float(risk_params.get("position_size", 0.0))
    weighted = [(sid, weights[sid], returns_df[sid]) for sid in member_strategy_ids]
    combined = combine_weighted_returns(weighted, leverage=leverage)
    if combined.empty:
        return {
            "sharpe": 0.0,
            "sortino": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "total_leverage": leverage,
            "max_position": max_position_param,
        }

    core = compute_series_metrics(combined).as_dict()
    # ``total_leverage`` = applied leverage scaler (matches what the safety
    # cap check expects); ``max_position`` = the trial's requested
    # position_size — the optimizer trusts the search-space clip to keep
    # this within bounds; we surface it back so post-eval cap-check sees
    # a consistent value.
    core["total_leverage"] = leverage
    # Largest absolute per-strategy weight is a reasonable proxy for "max
    # position" — operators reading the trace will see how concentrated
    # the winning trial was without us having to track per-bar positions.
    core["max_position"] = max(abs(w) for w in weights.values())
    return core
