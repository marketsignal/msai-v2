"""Redis + arq job queue foundation for MSAI v2.

Provides helpers to parse Redis URLs, create connection pools,
and enqueue backtest / data-ingest jobs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

__all__ = [
    "ArqRedis",
    "RedisSettings",
    "enqueue_backtest",
    "enqueue_ingest",
    "enqueue_portfolio_run",
    "enqueue_research",
    "get_redis_pool",
]

_DEFAULT_REDIS_PORT = 6379


def _parse_redis_url(url: str) -> RedisSettings:
    """Parse a ``redis://host:port/db`` URL into arq :class:`RedisSettings`.

    Extracts *host*, *port*, and *database* components.  When the port is
    omitted the default Redis port (6379) is used.  When the database path
    is omitted, database 0 is used.

    Args:
        url: A Redis connection URL, e.g. ``redis://localhost:6379/0``.

    Returns:
        An :class:`arq.connections.RedisSettings` configured with the
        parsed host, port, and database.
    """
    parsed = urlparse(url)
    host: str = parsed.hostname or "localhost"
    port: int = parsed.port or _DEFAULT_REDIS_PORT
    database: int = int(parsed.path.lstrip("/") or "0")
    return RedisSettings(host=host, port=port, database=database)


async def get_redis_pool() -> ArqRedis:
    """Create an arq Redis connection pool from application settings.

    The ``settings`` module is imported lazily inside this function to
    avoid circular-import issues when ``core.queue`` is imported at
    module level elsewhere.

    Returns:
        An :class:`arq.connections.ArqRedis` pool ready for job enqueuing.
    """
    from msai.core.config import settings  # lazy import to avoid circular deps

    redis_settings = _parse_redis_url(settings.redis_url)
    pool: ArqRedis = await create_pool(redis_settings)
    return pool


async def enqueue_backtest(
    pool: ArqRedis,
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
) -> str | None:
    """Enqueue a ``run_backtest`` job.

    Args:
        pool: An active arq Redis connection pool.
        backtest_id: Unique identifier for the backtest run.
        strategy_path: Dotted Python path to the strategy class/module.
        config: Arbitrary configuration dict forwarded to the worker.

    Returns:
        The arq job ID if enqueued, or None if the job was deduplicated.
    """
    job = await pool.enqueue_job(
        "run_backtest",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
        config=config,
    )
    return job.job_id if job else None


async def enqueue_research(
    pool: ArqRedis,
    job_id: str,
    job_type: str,
    payload: dict[str, Any],
) -> str | None:
    """Enqueue a ``run_research_job`` job to the dedicated research queue.

    Args:
        pool: An active arq Redis connection pool.
        job_id: UUID string of the :class:`ResearchJob` row.
        job_type: Either ``"parameter_sweep"`` or ``"walk_forward"``.
        payload: Full request payload forwarded verbatim to the worker.

    Returns:
        The arq job ID if enqueued, or None if the job was deduplicated.
    """
    from msai.core.config import settings as _settings  # lazy to avoid circular deps

    job = await pool.enqueue_job(
        "run_research_job",
        job_id=job_id,
        job_type=job_type,
        payload=payload,
        _queue_name=_settings.research_queue_name,
    )
    return job.job_id if job else None


async def enqueue_portfolio_run(
    pool: ArqRedis,
    run_id: str,
    portfolio_id: str,
) -> str | None:
    """Enqueue a ``run_portfolio`` job.

    Args:
        pool: An active arq Redis connection pool.
        run_id: UUID string of the :class:`PortfolioRun` row.
        portfolio_id: UUID string of the owning :class:`Portfolio`.

    Returns:
        The arq job ID if enqueued, or None if the job was deduplicated.
    """
    from msai.core.config import settings as _settings  # lazy to avoid circular deps

    job = await pool.enqueue_job(
        "run_portfolio",
        run_id=run_id,
        portfolio_id=portfolio_id,
        _queue_name=_settings.portfolio_queue_name,
    )
    return job.job_id if job else None


async def enqueue_ingest(
    pool: ArqRedis,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> None:
    """Enqueue a ``run_ingest`` job.

    Args:
        pool: An active arq Redis connection pool.
        asset_class: Asset class identifier (e.g. ``"stocks"``, ``"futures"``).
        symbols: List of ticker symbols to ingest.
        start: Start date as an ISO-8601 string (``"2024-01-01"``).
        end: End date as an ISO-8601 string (``"2024-12-31"``).
        provider: Data provider (``"auto"``, ``"databento"``, or ``"polygon"``).
        dataset: Override the default Databento dataset.
        schema: Override the default Databento schema.
    """
    await pool.enqueue_job(
        "run_ingest",
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
        provider=provider,
        dataset=dataset,
        schema=schema,
    )
