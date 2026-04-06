"""arq WorkerSettings for background job processing.

Defines the worker configuration and job functions for the arq task queue.
The ``run_backtest`` function delegates to :func:`msai.workers.backtest_job.run_backtest_job`
for the full backtest execution pipeline.
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.backtest_job import run_backtest_job


async def run_backtest(
    ctx: dict[str, Any],
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
) -> None:
    """Execute a backtest job by delegating to :func:`run_backtest_job`.

    Args:
        ctx: arq worker context (contains Redis pool, etc.).
        backtest_id: UUID string of the Backtest record.
        strategy_path: Filesystem path to the strategy module.
        config: Strategy configuration parameters.
    """
    await run_backtest_job(ctx, backtest_id, strategy_path, config)


async def run_ingest(
    ctx: dict[str, Any],
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
) -> None:
    """arq worker function for data ingestion.

    Instantiates the data ingestion service with Polygon and Databento
    clients (when API keys are configured) and runs a historical ingest.

    Args:
        ctx: arq worker context.
        asset_class: Asset class name (``"stocks"``, ``"futures"``, etc.).
        symbols: List of ticker symbols to ingest.
        start: ISO-8601 start date.
        end: ISO-8601 end date.
    """
    from msai.services.data_ingestion import DataIngestionService
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.data_sources.polygon_client import PolygonClient
    from msai.services.parquet_store import ParquetStore

    store = ParquetStore(str(settings.data_root / "parquet"))

    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None
    databento = DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None

    service = DataIngestionService(store, polygon=polygon, databento=databento)
    await service.ingest_historical(asset_class, symbols, start, end)


class WorkerSettings:
    """arq worker configuration."""

    functions = [run_backtest, run_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    max_jobs: int = 2
    job_timeout: int = 1800  # 30 minutes
    max_tries: int = 2  # 1 retry
