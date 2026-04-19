"""arq ``WorkerSettings`` for the MSAI background job queue.

Registers every job function the worker pool should expose and wires in
the Redis connection parsed from :mod:`msai.core.config`.

Why the ``asyncio.set_event_loop_policy(None)`` call?
-----------------------------------------------------
Importing :mod:`msai.workers.backtest_job` transitively imports
``nautilus_trader``, which installs uvloop's ``EventLoopPolicy`` as a side
effect of its Rust/Cython initialisation.  On Python 3.12+ that breaks
arq's ``Worker.__init__`` because arq calls the (soon-to-be-removed)
``asyncio.get_event_loop()`` during startup and uvloop's policy now
raises::

    RuntimeError: There is no current event loop in thread 'MainThread'

Resetting the policy to the default after the problematic imports restores
stock-asyncio semantics so arq can start its worker loop cleanly.  The
reset MUST happen after the imports that pull in nautilus_trader, which
is why it lives below the ``from ... import`` block.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.core.queue import _parse_redis_url
from msai.services.job_watchdog import run_watchdog_once
from msai.workers.backtest_job import run_backtest_job

if TYPE_CHECKING:
    from arq.connections import RedisSettings

# See the module docstring for why this reset is necessary.  Do NOT move
# it above the imports -- the reset must happen after nautilus_trader is
# imported for the first time.
asyncio.set_event_loop_policy(None)


async def run_backtest(
    ctx: dict[str, Any],
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
) -> None:
    """Entry point registered with arq for ``run_backtest`` jobs.

    Thin forwarder to :func:`run_backtest_job`.  Keeping the arq-facing
    function separate from the implementation lets us re-use
    ``run_backtest_job`` from tests and CLI scripts without going through
    the queue.

    Args:
        ctx: arq worker context (Redis pool etc.).
        backtest_id: UUID string of the :class:`Backtest` row.
        strategy_path: Absolute filesystem path to the strategy source.
        config: Strategy configuration dict forwarded verbatim to
            Nautilus's ``StrategyConfig``.
    """
    await run_backtest_job(ctx, backtest_id, strategy_path, config)


async def run_ingest(
    ctx: dict[str, Any],
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> None:
    """arq job function for historical data ingestion.

    Delegates to :func:`msai.services.data_ingestion.run_ingest` which
    uses the plan-based routing (Databento for equities/futures, Polygon
    as fallback).

    Args:
        ctx: arq worker context.
        asset_class: Asset class name (``"stocks"``, ``"futures"``, ...).
        symbols: List of ticker symbols to ingest.
        start: ISO-8601 start date.
        end: ISO-8601 end date.
        provider: Data provider (``"auto"``, ``"databento"``, ``"polygon"``).
        dataset: Override the default Databento dataset.
        schema: Override the default Databento schema.
    """
    from msai.services.data_ingestion import run_ingest as _run_ingest

    await _run_ingest(
        ctx,
        asset_class,
        symbols,
        start,
        end,
        provider=provider,
        dataset=dataset,
        schema=schema,
    )


_watchdog_log = get_logger("workers.watchdog")


async def _watchdog(ctx: dict[str, Any]) -> None:
    """arq cron wrapper for the job watchdog.

    Runs :func:`~msai.services.job_watchdog.run_watchdog_once` and logs
    a summary when any jobs were cleaned up.
    """
    _ = ctx
    result = await run_watchdog_once()
    if result["backtests_cleaned"] or result["research_cleaned"]:
        _watchdog_log.info("watchdog_cleaned", **result)


class WorkerSettings:
    """arq worker configuration used by ``arq msai.workers.settings.WorkerSettings``."""

    functions = [run_backtest, run_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    max_jobs: int = 2
    job_timeout: int = max(settings.backtest_timeout_seconds, 60 * 60)
    max_tries: int = 2  # 1 retry

    # Cron jobs — scheduled background work.
    # PnL: 21:30 UTC = safe for both EST (4:30 PM) and EDT (5:30 PM),
    # always after US equity close (4:00 PM ET).
    # Ingest: tz-aware via wrapper. arq fires `run_nightly_ingest_if_due`
    #   every minute; the wrapper consults DAILY_INGEST_TIMEZONE / HOUR
    #   / MINUTE / ENABLED + a JSON state file (Phase 2 #3 — Codex
    #   parity) so non-US markets can schedule by local close and the
    #   ingest is at-most-once per scheduled-tz calendar day.
    # Watchdog: every 60 seconds (minute=None, second=0 → fires at :00 each minute).
    from arq.cron import cron as _cron

    from msai.workers.nightly_ingest import run_nightly_ingest_if_due as _nightly_if_due
    from msai.workers.pnl_aggregation import aggregate_daily_pnl as _pnl

    cron_jobs = [
        _cron(_pnl, hour=21, minute=30),
        _cron(_nightly_if_due, minute=None, second=0),
        _cron(_watchdog, minute=None, second=0),
    ]
