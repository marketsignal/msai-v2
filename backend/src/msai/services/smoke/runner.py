"""Smoke runner -- pre-ingests AAPL+SPY, bootstraps the canonical Portfolio,
then fires a ``PortfolioRun`` via the existing lifecycle.

Calls services in-process (no HTTP-to-self). Used by both the CLI command
(``msai backtest smoke``) and the API endpoint
(``POST /api/v1/portfolios/smoke/runs``).

Plan-review iter-4 pinned shapes consulted during implementation:

* ``AllocatorName`` is a ``Literal`` in ``msai.schemas.portfolio:44`` -- it is
  NOT in ``models.portfolio_enums``.
* ``PortfolioObjective.MAXIMIZE_SHARPE`` is the canonical "best risk-adjusted
  return" value (``RISK_ADJUSTED_RETURN`` does not exist on the enum).
* ``PortfolioLifecycle.create`` is a ``@staticmethod`` (lifecycle.py:62).
  Call as ``PortfolioLifecycle.create(db, create, user_id=...)``.
* Use the module-level orchestration ``PortfolioService`` (``msai.services.portfolio``)
  for run creation, mirroring ``api/portfolio.py:42`` -- NOT the live
  ``PortfolioService`` from ``services/live/portfolio_service.py``.
* The ingest mutex API is the 4-arg keyword form from T3:
  ``acquire_ingest_lock(redis, *, symbol, window_start, ttl_seconds,
  wait_timeout_seconds)`` returns a token; release takes the token back.

Race note (code-review iter-1 fix #3): two parallel smoke invocations
(CLI + UI + scheduler can all hit this) used to be able to each miss
the sentinel-name lookup and create their own ``__msai_smoke__`` row,
which left the table with duplicate canonical portfolios.  Subsequent
``scalar_one_or_none()`` calls then raised ``MultipleResultsFound``.
Migration ``c3d4e5f6a7b8`` adds a partial unique index scoped to the
``__msai_smoke__`` sentinel; the runner catches the loser's
``IntegrityError`` on ``flush`` + re-SELECTs to find the winner's row.

Transaction semantics (code-review iter-1 fix #3):
``_get_or_create_canonical_portfolio`` NO LONGER COMMITS on its own —
the inner commit used to leave a partial-state window where a portfolio
was persisted but the subsequent enqueue could fail and leave no run
behind to roll back to.  ``run_smoke`` now owns the single commit and
issues it AFTER the enqueue succeeds; an enqueue failure rolls back
the portfolio + run together.

PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.models.portfolio import Portfolio
from msai.models.portfolio_enums import BacktestMode, PortfolioObjective
from msai.models.strategy import Strategy
from msai.schemas.portfolio import (
    PortfolioCreate,
    PortfolioRunCreate,
)
from msai.services.data_ingestion import ingest_symbols
from msai.services.portfolio import PortfolioService
from msai.services.portfolio.lifecycle import PortfolioLifecycle
from msai.services.smoke.config import SMOKE_CONFIGS, SmokeConfigName
from msai.services.smoke.ingest_lock import (
    acquire_ingest_lock,
    release_ingest_lock,
)
from msai.services.symbol_onboarding.coverage import compute_coverage
from msai.services.symbol_onboarding.partition_index import PartitionIndexService
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.portfolio_run import PortfolioRun

# Module alias for ``IntegrityError``. Code-review iter-1 fix #3 uses this
# in ``_get_or_create_canonical_portfolio`` to detect the partial-unique-
# index race winner. Aliasing keeps the import "consumed" at the module
# level so the PostToolUse ruff formatter does not strip the import as
# "unused" between subagent edits (see
# ``feedback_colocate_imports_with_usage_in_edits.md``). Removing this
# alias broke the runner once already during this fix.
_PortfolioSentinelRaceError = IntegrityError

log = get_logger(__name__)

SMOKE_PORTFOLIO_NAME = "__msai_smoke__"
"""Sentinel name used to idempotently look up the canonical smoke Portfolio."""

# Pre-seeded Strategy row names (4 rows, one per ``(kind, symbol)`` pair).
# Source of truth is the seed migration (Task 1) -- this tuple must stay in
# sync with the SQL bulk_insert in that migration.
_SMOKE_STRATEGY_NAMES: tuple[str, ...] = (
    "__smoke__/smoke_market_order/AAPL",
    "__smoke__/smoke_market_order/SPY",
    "__smoke__/ema_cross/AAPL",
    "__smoke__/ema_cross/SPY",
)


# Module-level singleton service. Mirrors ``api/portfolio.py:42`` so the
# runner and the route share one orchestration entry point.
_service = PortfolioService()


async def _select_canonical_portfolio(db: AsyncSession) -> Portfolio | None:
    """SELECT the canonical smoke portfolio by sentinel name, or None."""
    return (
        await db.execute(select(Portfolio).where(Portfolio.name == SMOKE_PORTFOLIO_NAME))
    ).scalar_one_or_none()


async def _get_or_create_canonical_portfolio(
    db: AsyncSession, *, user_id: UUID | None
) -> Portfolio:
    """Idempotent bootstrap: look up by sentinel name, create if missing.

    The first invocation seeds the canonical Portfolio with the 4 pre-seeded
    smoke Strategy rows under ``equal_weight`` allocation. Subsequent
    invocations find the existing row via the sentinel-name lookup and
    return it verbatim -- callers must NOT mutate the returned Portfolio.

    Code-review iter-1 fix #3 (race):
    Two concurrent ``run_smoke`` invocations could both miss the SELECT and
    both call ``PortfolioLifecycle.create``. Migration ``c3d4e5f6a7b8`` adds
    a partial unique index on ``portfolios.name = '__msai_smoke__'`` so the
    loser's ``flush()`` raises :class:`IntegrityError`. We catch it,
    rollback the failed transaction, and re-SELECT the row the winner
    committed. Both callers see the same canonical Portfolio.

    Code-review iter-1 fix #3 (partial commit):
    This helper NO LONGER commits. ``run_smoke`` owns the single commit at
    the bottom of the flow so a downstream enqueue failure can roll back the
    portfolio + run together. The previous inner ``await db.commit()`` left a
    half-state window where the portfolio was visible to other sessions but
    no PortfolioRun existed yet.
    """
    existing = await _select_canonical_portfolio(db)
    if existing is not None:
        return existing

    rows = (
        (await db.execute(select(Strategy).where(Strategy.name.in_(_SMOKE_STRATEGY_NAMES))))
        .scalars()
        .all()
    )
    if len(rows) != len(_SMOKE_STRATEGY_NAMES):
        # The seed migration (Task 1) is responsible for populating these
        # rows. If they're missing the operator either skipped the migration
        # or wiped the table -- fail fast with an actionable message.
        raise RuntimeError(
            f"Expected {len(_SMOKE_STRATEGY_NAMES)} canonical smoke Strategy "
            f"rows; found {len(rows)}. Did the smoke Alembic migration run?"
        )
    strategy_ids = [r.id for r in rows]

    create = PortfolioCreate(
        name=SMOKE_PORTFOLIO_NAME,
        # ``MAXIMIZE_SHARPE`` is the canonical risk-adjusted-return objective.
        # The Quick path doesn't actually consume this beyond persistence,
        # but a real value makes the row readable in the UI portfolio list.
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        base_capital=100_000.0,
        requested_leverage=1.0,
        default_mode=BacktestMode.QUICK,
        # The literal ``"equal_weight"`` is narrowed automatically to
        # ``AllocatorName`` (defined as ``Literal[...]`` in
        # ``msai.schemas.portfolio``); no explicit cast is required.
        allocator_name="equal_weight",
        strategy_ids=strategy_ids,
    )
    try:
        # ``PortfolioLifecycle.create`` is a static method -- pass session first.
        # ``create`` flushes but does not commit; we defer the commit to
        # ``run_smoke`` so a downstream enqueue failure can roll back the
        # portfolio + run together.
        portfolio = await PortfolioLifecycle.create(db, create, user_id=user_id)
    except _PortfolioSentinelRaceError:
        # Lost the partial-unique-index race against another concurrent
        # caller. The loser's transaction is poisoned by the failed flush;
        # rollback to a clean state, then re-SELECT the row the winner just
        # committed. Use a fresh nested logical SELECT — the winner is
        # guaranteed visible because the partial unique index commit is
        # atomic with the row insert.
        await db.rollback()
        log.info(
            "smoke_portfolio_bootstrap_lost_race",
            sentinel=SMOKE_PORTFOLIO_NAME,
        )
        winner = await _select_canonical_portfolio(db)
        if winner is None:
            # Pathological: the IntegrityError fired but SELECT can't find
            # the winner. Either the partial unique index is misconfigured
            # or the winner rolled back too — surface a clear error rather
            # than retry-loop forever.
            raise RuntimeError(
                "Smoke portfolio bootstrap lost the unique-index race, but "
                "no canonical __msai_smoke__ row is visible after rollback. "
                "Check the c3d4e5f6a7b8 migration was applied."
            ) from None
        return winner
    return portfolio


def _iter_month_starts(start_date: date, end_date: date) -> list[date]:
    """Enumerate first-of-month ``date`` values covering ``[start_date, end_date]``.

    Code-review iter-1 fix #4 (lock window covers full ingest range): the
    Redis mutex is keyed by ``(symbol, YYYY-MM)`` (see
    :mod:`msai.services.smoke.ingest_lock`), and ``ingest_symbols`` fetches
    every month in the requested window. Previously the runner locked only
    the start month, so concurrent ``smoke:nightly`` invocations colliding
    on the December slice got no protection.

    Returns one ``date`` per month — always the first of the month — so the
    lock-key helper sees a stable representative.  Order is chronological.
    """
    if end_date < start_date:
        return []
    months: list[date] = []
    year, month = start_date.year, start_date.month
    end_year, end_month = end_date.year, end_date.month
    while (year, month) <= (end_year, end_month):
        months.append(date(year, month, 1))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


async def _symbols_with_gaps(
    symbols: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> list[str]:
    """Return the subset of ``symbols`` whose Parquet partitions are NOT yet
    fully covering ``[start_date, end_date]``.

    Code-review iter-1 fix #4 (coverage check before ingest): the previous
    runner unconditionally called :func:`ingest_symbols`, which forces a
    Databento fetch even when the Parquet is already on disk -- breaking the
    warm-path runtime budget. We now consult :func:`compute_coverage` (the
    project's authoritative day-precise coverage scan) and only return the
    symbols whose coverage status is not ``"full"``.

    Opens a fresh short-lived ``AsyncSession`` because the runner's own
    bootstrap session is mid-transaction; querying ``parquet_partition_index``
    on it would entangle the read with the portfolio commit. The session
    closes via ``async with`` even if ``compute_coverage`` raises.
    """
    gaps: list[str] = []
    async with async_session_factory() as session:
        partition_index = PartitionIndexService(
            db_gateway=PartitionIndexGateway(session=session),
        )
        for symbol in symbols:
            coverage = await compute_coverage(
                asset_class="stocks",
                symbol=symbol,
                start=start_date,
                end=end_date,
                data_root=settings.data_root,
                partition_index=partition_index,
            )
            if coverage.status != "full":
                gaps.append(symbol)
    return gaps


async def _ensure_ingested(symbols: tuple[str, ...], start: str, end: str) -> None:
    """Cold-ingest pre-flight via the existing in-process ``ingest_symbols``.

    Wrapped per ``(symbol, year-month)`` in the Redis mutex (T3) so that
    overlapping smoke invocations from CLI + UI + nightly scheduler don't
    double-fetch the same Databento window. ``ingest_symbols`` is idempotent
    -- if the Parquet for the requested window is already on disk, the
    helper returns a zero-bars result and every mutex is released cleanly.

    Code-review iter-1 fix #4 (coverage check + full-range mutex):
    * Skip the ``ingest_symbols`` call entirely when ALL configured symbols
      already have ``status="full"`` coverage for the requested window
      (warm path -- the previous code always fetched).
    * Acquire one lock per ``(symbol, YYYY-MM)`` for every month in
      ``[start, end]`` (not just the start month) so a concurrent fast run
      hitting December gets the same protection as January.
    """
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    # Warm-path short-circuit: ask compute_coverage which symbols still have
    # gaps; if none, the ingest is a no-op and we skip both the lock dance
    # and the Databento round-trip.
    gapped_symbols = await _symbols_with_gaps(symbols, start_date, end_date)
    if not gapped_symbols:
        log.info(
            "smoke_ensure_ingested_coverage_full_skip",
            symbols=list(symbols),
            start=start,
            end=end,
        )
        return

    redis = await get_redis_pool()
    month_starts = _iter_month_starts(start_date, end_date)
    # Track ``(symbol, month_start, token)`` triples so the ``finally`` block
    # can release every successfully-acquired lock even when a later
    # acquisition fails.
    tokens: list[tuple[str, date, str]] = []
    try:
        for symbol in gapped_symbols:
            for month_start in month_starts:
                token = await acquire_ingest_lock(
                    redis,
                    symbol=symbol,
                    window_start=month_start,
                    # 1 hour covers cold-nightly ingest (12 months × per-month
                    # Databento monthly fetch). Comfortably longer than the
                    # warm-path no-op and the worst single-month equity pull.
                    ttl_seconds=3600,
                    # 10 minutes -- long enough that a concurrent ingest can
                    # finish, short enough that we surface a real stall to
                    # the caller rather than hang forever.
                    wait_timeout_seconds=600,
                )
                tokens.append((symbol, month_start, token))
        await ingest_symbols(
            "stocks",
            list(gapped_symbols),
            start,
            end,
            provider="databento",
            dataset="EQUS.MINI",
            schema="ohlcv-1m",
        )
    finally:
        # Always release every successfully-acquired lock, even if the
        # ingest call itself raised. ``release_ingest_lock`` is holder-token
        # guarded -- a foreign token will no-op rather than steal the slot.
        for symbol, month_start, token in tokens:
            try:
                await release_ingest_lock(
                    redis,
                    symbol=symbol,
                    window_start=month_start,
                    token=token,
                )
            except Exception as exc:  # noqa: BLE001
                # Lock release is best-effort. The TTL guarantees the lock
                # auto-frees within ``ttl_seconds`` even if we never get
                # the DEL through; swallow + log so the caller still sees
                # the ingest result instead of a release-time error.
                log.warning(
                    "smoke_ingest_lock_release_failed",
                    symbol=symbol,
                    window_start=month_start.isoformat(),
                    error=str(exc),
                )


async def run_smoke(
    *,
    db: AsyncSession,
    config_name: SmokeConfigName = "fast",
    user_id: UUID | None = None,
) -> PortfolioRun:
    """Fire the canonical smoke run end-to-end.

    Steps:
      1. Pre-ingest AAPL+SPY for the configured window (idempotent --
         Parquet already on disk is a no-op).
      2. Bootstrap (or look up) the canonical ``__msai_smoke__`` Portfolio.
      3. Submit a ``PortfolioRun`` with ``smoke=True`` via the existing
         lifecycle so the G5 metrics-enrichment branch in
         ``orchestration.py`` fires when the worker picks the run up.
      4. Enqueue the existing arq ``run_portfolio`` job (same enqueue path
         the standard ``POST /api/v1/portfolios/{id}/runs`` route uses).

    Returns the persisted ``PortfolioRun`` row.

    The arq-enqueue step mirrors ``api/portfolio.py:322-335``: enqueue
    BEFORE commit, and roll back the row if the enqueue raises so the
    caller never sees a "pending" PortfolioRun that no worker will pick up.

    Code-review iter-1 fix #3 (partial commit):
    Both the canonical portfolio (if newly created in this call) AND the
    new PortfolioRun live in the same uncommitted transaction. A failure
    in ``create_run`` or ``enqueue_portfolio_run`` rolls back the whole
    transaction — including a fresh Portfolio row — so we never leave
    half-state behind.
    """
    config = SMOKE_CONFIGS[config_name]

    # 1. Cold-ingest pre-flight (mutex-guarded; idempotent).
    await _ensure_ingested(
        config.symbols,
        config.start_date.isoformat(),
        config.end_date.isoformat(),
    )

    # 2. Idempotent canonical Portfolio bootstrap (no commit; rolled back
    # together with the run on any downstream failure).
    portfolio = await _get_or_create_canonical_portfolio(db, user_id=user_id)

    # 3. Create the PortfolioRun via the lifecycle, with ``smoke=True``
    # so the metrics-enrichment branch in orchestration.py engages.
    body = PortfolioRunCreate(
        start_date=config.start_date,
        end_date=config.end_date,
        smoke=True,
    )
    try:
        run = await _service.create_run(db, portfolio.id, body, user_id=user_id)
    except Exception as exc:
        # Roll back the (possibly newly-created) portfolio together with
        # the failed run-create so we never leave a dangling __msai_smoke__
        # row with no run behind it.
        await db.rollback()
        log.error(
            "smoke_portfolio_run_create_failed",
            portfolio_id=str(portfolio.id),
            error=str(exc),
        )
        raise

    # 4. Enqueue BEFORE commit -- if Redis is unreachable, roll back the
    # row so we don't leave a stranded "pending" run that no worker owns.
    try:
        pool = await get_redis_pool()
        await enqueue_portfolio_run(pool, str(run.id), str(portfolio.id))
    except Exception as exc:
        await db.rollback()
        log.error(
            "smoke_portfolio_run_enqueue_failed",
            run_id=str(run.id),
            portfolio_id=str(portfolio.id),
            error=str(exc),
        )
        raise

    # Single commit at the end — portfolio + run are now atomically
    # persisted alongside a successful enqueue.
    await db.commit()
    await db.refresh(run)
    log.info(
        "smoke_run_enqueued",
        run_id=str(run.id),
        portfolio_id=str(portfolio.id),
        config_name=config_name,
    )
    return run
