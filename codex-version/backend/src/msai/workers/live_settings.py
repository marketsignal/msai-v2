from __future__ import annotations

import asyncio

from arq.connections import RedisSettings

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.live_runtime import (
    run_live_kill_all,
    run_live_start,
    run_live_status,
    run_live_stop,
)
from msai.workers.worker_lifecycle import worker_shutdown, worker_startup

# Reset the event loop policy after imports so arq creates a clean loop even
# when transitive Nautilus imports set uvloop policies.
asyncio.set_event_loop_policy(None)


class LiveWorkerSettings:
    functions = [
        run_live_start,
        run_live_stop,
        run_live_status,
        run_live_kill_all,
    ]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name = settings.live_runtime_queue_name
    max_jobs = 2
    job_timeout = max(int(settings.live_runtime_request_timeout_seconds * 2), 300)
    max_tries = 1
    allow_abort_jobs = settings.queue_allow_abort_jobs
    ctx = {
        "worker_role": "live-runtime",
        "queue_name": settings.live_runtime_queue_name,
        "max_jobs": 2,
    }
    on_startup = worker_startup
    on_shutdown = worker_shutdown
