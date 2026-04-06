from __future__ import annotations

from urllib.parse import urlparse

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from msai.core.config import settings

_pool: ArqRedis | None = None


def _parse_redis_url(url: str) -> RedisSettings:
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or "0"),
    )


async def get_redis_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(_parse_redis_url(settings.redis_url))
    return _pool


async def close_redis_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def enqueue_backtest(
    pool: ArqRedis,
    backtest_id: str,
    strategy_path: str,
    config: dict,
) -> None:
    await pool.enqueue_job(
        "run_backtest",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
        config=config,
    )


async def enqueue_ingestion(
    pool: ArqRedis,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
) -> None:
    await pool.enqueue_job(
        "run_ingest",
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
    )
