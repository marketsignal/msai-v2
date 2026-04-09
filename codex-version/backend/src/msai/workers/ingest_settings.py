from __future__ import annotations

import asyncio

from arq.connections import RedisSettings

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.services.data_ingestion import run_ingest
from msai.workers.worker_lifecycle import worker_shutdown, worker_startup

asyncio.set_event_loop_policy(None)


class IngestWorkerSettings:
    functions = [run_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name = settings.ingest_queue_name
    max_jobs = settings.ingest_max_worker_jobs
    job_timeout = settings.ingestion_timeout_seconds
    max_tries = settings.queue_retry_attempts + 1
    allow_abort_jobs = settings.queue_allow_abort_jobs
    ctx = {
        "worker_role": "ingest-worker",
        "queue_name": settings.ingest_queue_name,
        "max_jobs": settings.ingest_max_worker_jobs,
    }
    on_startup = worker_startup
    on_shutdown = worker_shutdown
