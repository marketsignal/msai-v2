"""Portfolio management service -- CRUD and orchestration for portfolio backtests.

Provides methods to create portfolios with weighted strategy allocations,
list/get portfolios, create portfolio-level backtest runs, query run history,
and execute multi-strategy backtests that combine weighted returns into a
single portfolio-level result with QuantStats reporting.
"""

from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import defer, selectinload

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_enums import PortfolioObjective, PortfolioRunStatus
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
from msai.services.report_generator import ReportGenerator

if TYPE_CHECKING:
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


class PortfolioService:
    """Manages portfolio lifecycle: creation, allocation, and combined backtest runs."""

    async def create(
        self,
        session: AsyncSession,
        data: PortfolioCreate,
        user_id: UUID | None = None,
    ) -> Portfolio:
        """Create a portfolio with its allocation rows.

        Args:
            session: Active async database session.
            data: Validated portfolio creation payload including allocations.
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
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()

        # Validate: no duplicate candidate IDs
        seen_ids = set()
        for alloc in data.allocations:
            if alloc.candidate_id in seen_ids:
                raise ValueError(f"Duplicate candidate {alloc.candidate_id} in allocations")
            seen_ids.add(alloc.candidate_id)

        # Validate all candidate IDs exist before inserting allocations
        for alloc in data.allocations:
            candidate = await session.get(GraduationCandidate, alloc.candidate_id)
            if candidate is None:
                raise ValueError(f"Graduation candidate {alloc.candidate_id} not found")

        for alloc in data.allocations:
            allocation = PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=alloc.candidate_id,
                weight=alloc.weight,
            )
            session.add(allocation)

        await session.flush()

        log.info(
            "portfolio_created",
            portfolio_id=str(portfolio.id),
            name=data.name,
            num_allocations=len(data.allocations),
        )
        return portfolio

    async def list(
        self,
        session: AsyncSession,
        limit: int = 100,
    ) -> list[Portfolio]:
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

    async def get(
        self,
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

    async def get_allocations(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> list[PortfolioAllocation]:
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

    async def create_run(
        self,
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

        run = PortfolioRun(
            portfolio_id=portfolio_id,
            start_date=data.start_date,
            end_date=data.end_date,
            max_parallelism=data.max_parallelism,
            status=PortfolioRunStatus.PENDING.value,
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
        )
        return run

    async def list_runs(
        self,
        session: AsyncSession,
        portfolio_id: UUID | None = None,
        limit: int = 100,
    ) -> list[PortfolioRun]:
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

    async def get_run(
        self,
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

    async def count(self, session: AsyncSession) -> int:
        """Return the total number of portfolios."""
        result = await session.execute(select(func.count()).select_from(Portfolio))
        return result.scalar_one()

    async def count_runs(
        self,
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
    ) -> PortfolioRun:
        """Execute a portfolio backtest end-to-end and persist the result.

        Orchestrates:
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
        runner = runner or BacktestRunner()
        report_generator = report_generator or ReportGenerator()
        market_data_query = market_data_query or MarketDataQuery(str(settings.data_root))
        factory = session_factory or async_session_factory

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
            await loop.run_in_executor(
                None,
                lambda ac=asset_class, syms=sorted(symbols): ensure_catalog_data(  # noqa: B008
                    symbols=syms,
                    raw_parquet_root=settings.parquet_root,
                    catalog_root=settings.nautilus_catalog_root,
                    asset_class=ac,
                ),
            )

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

        effective_leverage = _effective_leverage(
            weighted_series=weighted_series,
            requested_leverage=requested_leverage,
            downside_target=downside_target,
        )
        combined_returns = combine_weighted_returns(weighted_series, leverage=effective_leverage)
        benchmark_returns = _load_benchmark_returns(
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
        # frequency-aligned pair (``_load_benchmark_returns`` returns a
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
        metrics["effective_leverage"] = effective_leverage
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

        # ---- Phase 3: persist results ----
        now = datetime.now(UTC)
        async with factory() as session:
            persisted = await session.get(PortfolioRun, run_id)
            if persisted is None:
                raise PortfolioOrchestrationError(
                    f"Portfolio run {run_id} disappeared during execution"
                )
            persisted.status = PortfolioRunStatus.COMPLETED.value
            persisted.metrics = metrics
            persisted.series = dataframe_to_series_payload(series_frame)
            persisted.allocations = strategy_results
            persisted.report_path = report_path
            persisted.heartbeat_at = now
            persisted.completed_at = now
            persisted.error_message = None
            await session.commit()
            await session.refresh(persisted)
            return persisted

    async def mark_run_running(
        self,
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

    async def heartbeat_run(
        self,
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

    async def mark_run_failed(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        error_message: str,
    ) -> PortfolioRun:
        """Mark a run ``failed`` with an operator-visible error message.

        Idempotent on already-failed rows (refresh completed_at / error);
        refuses to overwrite a ``completed`` row to avoid data loss.
        """
        run = await session.get(PortfolioRun, run_id)
        if run is None:
            raise PortfolioOrchestrationError(f"Portfolio run {run_id} not found")
        if run.status == PortfolioRunStatus.COMPLETED.value:
            raise PortfolioRunTerminalStateError(
                f"Portfolio run {run_id} is already completed; refusing to mark failed"
            )
        run.status = PortfolioRunStatus.FAILED.value
        run.error_message = error_message
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        return run

    # ------------------------------------------------------------------
    # Internal orchestration helpers
    # ------------------------------------------------------------------

    async def _load_allocations(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> list[PortfolioAllocation]:
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
        allocations: list[PortfolioAllocation],
        *,
        objective: PortfolioObjective,
    ) -> list[dict[str, Any]]:
        """Flatten DB allocations into orchestration-ready dicts.

        Pulls strategy file/class/config from the related
        :class:`GraduationCandidate` and :class:`Strategy`.  An allocation
        with ``weight is None`` triggers heuristic derivation from the
        candidate's metrics; an explicit weight (including fractional
        values like 0.001) is preserved.  Weights are normalized to sum to
        1.0 across the resolved allocations.
        """
        rows: list[dict[str, Any]] = []
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
                weight = _heuristic_weight(dict(candidate.metrics or {}), objective)
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
        allocations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        max_parallelism: int | None,
    ) -> list[dict[str, Any]]:
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
            return list(await asyncio.gather(*tasks))

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
# Module-level helpers (pure functions, easy to unit-test)
# ----------------------------------------------------------------------


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


def _heuristic_weight(metrics: dict[str, Any], objective: PortfolioObjective) -> float:
    """Derive a heuristic pre-normalization weight from candidate metrics.

    Returns the relevant metric when it is positive; falls back to ``1.0``
    when the metric is zero, negative, or missing so the candidate still
    participates in the portfolio.  Normalization downstream rescales the
    weight proportionally.
    """
    if objective is PortfolioObjective.MAXIMIZE_PROFIT:
        return max(float(metrics.get("total_return") or 0.0), 0.0) or 1.0
    if objective is PortfolioObjective.MAXIMIZE_SORTINO:
        return max(float(metrics.get("sortino") or 0.0), 0.0) or 1.0
    if objective is PortfolioObjective.MAXIMIZE_SHARPE:
        return max(float(metrics.get("sharpe") or 0.0), 0.0) or 1.0
    # EQUAL_WEIGHT / MANUAL → equal notional pre-normalization
    return 1.0


def _effective_leverage(
    *,
    weighted_series: list[tuple[str, float, pd.Series]],
    requested_leverage: float,
    downside_target: float | None,
) -> float:
    """Scale requested leverage down so combined downside risk ≤ target.

    Computes the combined portfolio's downside risk at ``leverage=1.0``;
    if it exceeds ``downside_target``, scales leverage proportionally.
    Never scales up past the requested leverage and never below ``0.1``
    (operator safety floor).  A zero or missing ``downside_target`` (or a
    requested leverage of zero) disables scaling and returns the
    requested value verbatim.
    """
    leverage = max(0.0, float(requested_leverage))
    if leverage <= 0.0 or downside_target is None or downside_target <= 0.0:
        return leverage

    combined = combine_weighted_returns(weighted_series, leverage=1.0)
    metrics = compute_series_metrics(combined)
    downside_risk = float(metrics.downside_risk)
    if downside_risk <= 0.0 or not math.isfinite(downside_risk):
        return leverage
    scale = min(1.0, float(downside_target) / downside_risk)
    return max(0.1, leverage * scale)


def _raw_benchmark_symbol(symbol: str) -> str:
    """Derive the parquet-key ticker from an operator-provided symbol.

    The MSAI ingestion pipeline stores bars under
    ``{asset_class}/{ticker}`` where ``ticker`` is the raw symbol (no
    venue suffix).  Operators sometimes type the symbol with a venue
    suffix for clarity (``SPY.NASDAQ``) and sometimes without
    (``BRK.B`` — a share-class symbol that contains a dot).

    Strip ONLY when the trailing segment looks like an uppercase venue
    code (≥2 chars, all letters).  Single-letter suffixes like ``.B`` /
    ``.A`` are share classes, never venues, so they're preserved.  This
    prevents the silent substitution of ``BRK.B`` → ``BRK`` when the
    parquet store happens to have ``BRK`` data — that would compute
    alpha/beta and the tearsheet against the wrong asset.
    """
    if "." not in symbol:
        return symbol
    head, _, tail = symbol.rpartition(".")
    if len(tail) >= 2 and tail.isalpha() and tail.isupper():
        return head
    return symbol


def _load_benchmark_returns(
    market_data_query: MarketDataQuery,
    *,
    benchmark_symbol: Any,
    start_date: str,
    end_date: str,
) -> pd.Series | None:
    """Fetch a benchmark returns series, or ``None`` when unavailable.

    Returns ``None`` in one of two ways:

    * **By design** — the portfolio has no benchmark symbol configured
      (silent, no log).
    * **Data problem** — benchmark requested but unavailable or malformed
      (logged at warning level with ``symbol`` / ``start_date`` /
      ``end_date`` so the operator can diagnose).  Alpha/beta will simply
      be absent from the resulting metrics.

    The benchmark is optional by contract, so any malformed data
    (unparseable timestamps, coerce-to-NaN closes) degrades to ``None``
    rather than failing the whole portfolio run.  The intraday series is
    resampled to daily returns — for multi-year portfolios, fetching
    minute bars and computing alpha/beta against ~500k intraday points
    both wastes memory and mismatches the typical analytics frequency.
    """
    symbol = str(benchmark_symbol or "").strip()
    if not symbol:
        return None

    # Try the full symbol first (preserves share-class tickers like
    # ``BRK.B``); fall back to stripping a trailing segment for operators
    # who typed a venue suffix (``SPY.NASDAQ``).
    rows = market_data_query.get_bars(symbol, start_date, end_date, interval="1m")
    used_symbol = symbol
    if not rows:
        stripped = _raw_benchmark_symbol(symbol)
        if stripped != symbol:
            rows = market_data_query.get_bars(stripped, start_date, end_date, interval="1m")
            used_symbol = stripped
    if not rows:
        log.warning(
            "benchmark_returns_no_bars",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        return None
    frame = pd.DataFrame(rows)
    if "timestamp" not in frame.columns or "close" not in frame.columns:
        log.warning(
            "benchmark_returns_missing_columns",
            symbol=used_symbol,
            columns=list(frame.columns),
        )
        return None
    # ``errors="coerce"`` turns unparseable timestamps into NaT so we can
    # drop them cleanly.  The previous ``errors="raise"`` (default) would
    # abort the entire portfolio run on a single bad row even though the
    # benchmark is optional.
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    close = frame.dropna(subset=["timestamp", "close"]).set_index("timestamp")["close"]
    if close.empty:
        log.warning(
            "benchmark_returns_empty_after_clean",
            symbol=used_symbol,
            raw_rows=len(rows),
        )
        return None
    # Resample to daily close so alpha/beta are computed at the same
    # frequency as portfolio-level analytics; intraday bars would
    # otherwise load ~500k rows per year of benchmark data.
    daily_close = close.resample("1D").last().dropna()
    if daily_close.empty:
        return None
    returns = daily_close.pct_change().fillna(0.0)
    returns.name = "benchmark_returns"
    return returns


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
    """
    prepared = dict(config)
    if "instrument_id" not in prepared and instrument_ids:
        prepared["instrument_id"] = instrument_ids[0]
    if "bar_type" not in prepared and instrument_ids:
        prepared["bar_type"] = f"{instrument_ids[0]}-1-MINUTE-LAST-EXTERNAL"
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
