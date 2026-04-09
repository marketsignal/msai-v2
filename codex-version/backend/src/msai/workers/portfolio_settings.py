from __future__ import annotations

import asyncio

from arq.connections import RedisSettings

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.portfolio_job import run_portfolio_job
from msai.workers.worker_lifecycle import worker_shutdown, worker_startup

asyncio.set_event_loop_policy(None)


class PortfolioWorkerSettings:
    functions = [run_portfolio_job]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name = settings.portfolio_queue_name
    max_jobs = settings.portfolio_max_worker_jobs
    job_timeout = settings.research_timeout_seconds
    max_tries = settings.queue_retry_attempts + 1
    allow_abort_jobs = settings.queue_allow_abort_jobs
    ctx = {
        "worker_role": "portfolio-worker",
        "queue_name": settings.portfolio_queue_name,
        "max_jobs": settings.portfolio_max_worker_jobs,
    }
    on_startup = worker_startup
    on_shutdown = worker_shutdown
