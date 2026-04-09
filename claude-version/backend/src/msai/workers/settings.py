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
from msai.core.queue import _parse_redis_url
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
) -> None:
    """arq job function for historical data ingestion.

    Uses Polygon and Databento clients when API keys are configured and
    persists the downloaded bars via :class:`ParquetStore`.

    Args:
        ctx: arq worker context.
        asset_class: Asset class name (``"stocks"``, ``"futures"``, ...).
        symbols: List of ticker symbols to ingest.
        start: ISO-8601 start date.
        end: ISO-8601 end date.
    """
    _ = ctx
    from msai.services.data_ingestion import DataIngestionService
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.data_sources.polygon_client import PolygonClient
    from msai.services.parquet_store import ParquetStore

    store = ParquetStore(str(settings.parquet_root))
    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None
    databento = (
        DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None
    )

    service = DataIngestionService(store, polygon=polygon, databento=databento)
    await service.ingest_historical(asset_class, symbols, start, end)


class WorkerSettings:
    """arq worker configuration used by ``arq msai.workers.settings.WorkerSettings``."""

    functions = [run_backtest, run_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    max_jobs: int = 2
    job_timeout: int = max(settings.backtest_timeout_seconds, 60 * 60)
    max_tries: int = 2  # 1 retry

    # Cron jobs — scheduled background work.
    # Times are UTC; 20:30 UTC ≈ 4:30 PM ET (EDT), 05:00 UTC ≈ 1:00 AM ET (EDT).
    from arq.cron import cron as _cron

    from msai.workers.nightly_ingest import run_nightly_ingest as _nightly
    from msai.workers.pnl_aggregation import aggregate_daily_pnl as _pnl

    cron_jobs = [
        _cron(_pnl, hour=20, minute=30),
        _cron(_nightly, hour=5, minute=0),
    ]
