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
        await _pool.aclose()
        _pool = None


async def enqueue_backtest(
    pool: ArqRedis,
    backtest_id: str,
    strategy_path: str,
    config: dict,
) -> str | None:
    job = await pool.enqueue_job(
        "run_backtest",
        _job_id=backtest_id,
        _queue_name=settings.backtest_queue_name,
        backtest_id=backtest_id,
        strategy_path=strategy_path,
        config=config,
    )
    job_id = getattr(job, "job_id", None)
    return job_id if isinstance(job_id, str) else None


async def enqueue_ingestion(
    pool: ArqRedis,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> str | None:
    job = await pool.enqueue_job(
        "run_ingest",
        _queue_name=settings.ingest_queue_name,
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
        provider=provider,
        dataset=dataset,
        schema=schema,
    )
    job_id = getattr(job, "job_id", None)
    return job_id if isinstance(job_id, str) else None


async def enqueue_research_job(
    pool: ArqRedis,
    job_id: str,
    job_type: str,
    payload: dict,
) -> str | None:
    job = await pool.enqueue_job(
        "run_research_job",
        _queue_name=settings.research_queue_name,
        job_id=job_id,
        job_type=job_type,
        payload=payload,
    )
    queue_job_id = getattr(job, "job_id", None)
    return queue_job_id if isinstance(queue_job_id, str) else None


async def enqueue_portfolio_run(
    pool: ArqRedis,
    run_id: str,
) -> str | None:
    job = await pool.enqueue_job(
        "run_portfolio_job",
        _job_id=run_id,
        _queue_name=settings.portfolio_queue_name,
        run_id=run_id,
    )
    job_id = getattr(job, "job_id", None)
    return job_id if isinstance(job_id, str) else None


async def enqueue_live_runtime(
    pool: ArqRedis,
    function: str,
    **kwargs: object,
):
    return await pool.enqueue_job(
        function,
        _queue_name=settings.live_runtime_queue_name,
        **kwargs,
    )


async def remove_queued_job(
    pool: ArqRedis,
    *,
    queue_name: str,
    queue_job_id: str,
) -> None:
    await pool.zrem(queue_name, queue_job_id)
    await pool.delete(
        f"arq:job:{queue_job_id}",
        f"arq:in-progress:{queue_job_id}",
        f"arq:retry:{queue_job_id}",
        f"arq:result:{queue_job_id}",
    )


async def queued_job_state(
    pool: ArqRedis,
    *,
    queue_name: str,
    queue_job_id: str,
) -> str | None:
    if await pool.exists(f"arq:in-progress:{queue_job_id}"):
        return "in_progress"
    if await pool.exists(f"arq:retry:{queue_job_id}"):
        return "retry"
    if await pool.zscore(queue_name, queue_job_id) is not None:
        return "queued"
    if await pool.exists(f"arq:job:{queue_job_id}"):
        return "reserved"
    return None
