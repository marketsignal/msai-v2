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
    "get_redis_pool",
]

_DEFAULT_REDIS_PORT = 6379


def _parse_redis_url(url: str) -> RedisSettings:
    """Parse a ``redis://host:port`` URL into arq :class:`RedisSettings`.

    Only the *host* and *port* components are extracted.  When the port is
    omitted the default Redis port (6379) is used.

    Args:
        url: A Redis connection URL, e.g. ``redis://localhost:6379``.

    Returns:
        An :class:`arq.connections.RedisSettings` configured with the
        parsed host and port.
    """
    parsed = urlparse(url)
    host: str = parsed.hostname or "localhost"
    port: int = parsed.port or _DEFAULT_REDIS_PORT
    return RedisSettings(host=host, port=port)


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
) -> None:
    """Enqueue a ``run_backtest`` job.

    Args:
        pool: An active arq Redis connection pool.
        backtest_id: Unique identifier for the backtest run.
        strategy_path: Dotted Python path to the strategy class/module.
        config: Arbitrary configuration dict forwarded to the worker.
    """
    await pool.enqueue_job(
        "run_backtest",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
        config=config,
    )


async def enqueue_ingest(
    pool: ArqRedis,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
) -> None:
    """Enqueue a ``run_ingest`` job.

    Args:
        pool: An active arq Redis connection pool.
        asset_class: Asset class identifier (e.g. ``"equity"``, ``"crypto"``).
        symbols: List of ticker symbols to ingest.
        start: Start date as an ISO-8601 string (``"2024-01-01"``).
        end: End date as an ISO-8601 string (``"2024-12-31"``).
    """
    await pool.enqueue_job(
        "run_ingest",
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
    )
