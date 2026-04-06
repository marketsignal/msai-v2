from __future__ import annotations

import asyncio

from arq.connections import RedisSettings

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.services.data_ingestion import run_ingest
from msai.workers.backtest_job import run_backtest

# Reset event loop policy AFTER imports — nautilus_trader (imported
# transitively via backtest_job) installs uvloop's EventLoopPolicy which
# breaks arq's Worker.__init__ on Python 3.12+ with:
# "RuntimeError: There is no current event loop in thread 'MainThread'"
asyncio.set_event_loop_policy(None)


class WorkerSettings:
    functions = [run_backtest, run_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    max_jobs = settings.max_worker_jobs
    job_timeout = max(settings.backtest_timeout_seconds, settings.ingestion_timeout_seconds)
    max_tries = settings.queue_retry_attempts + 1
