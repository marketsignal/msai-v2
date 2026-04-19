"""arq ``WorkerSettings`` for data ingestion jobs.

Registers :func:`run_nightly_ingest` and wires it to a dedicated Redis
queue (``msai:ingest``) so that ingestion workloads are isolated from
backtest / research / portfolio jobs.

Why the ``asyncio.set_event_loop_policy(None)`` call?
-----------------------------------------------------
Importing modules that transitively import ``nautilus_trader`` installs
uvloop's ``EventLoopPolicy`` as a side effect.  On Python 3.12+ that
breaks arq's ``Worker.__init__``.  Resetting the policy to the default
after the problematic imports restores stock-asyncio semantics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.nightly_ingest import run_nightly_ingest

if TYPE_CHECKING:
    from arq.connections import RedisSettings

# See the module docstring for why this reset is necessary.  Do NOT move
# it above the imports -- the reset must happen after nautilus_trader is
# imported for the first time.
asyncio.set_event_loop_policy(None)


class IngestWorkerSettings:
    """arq worker configuration for ``arq msai.workers.ingest_settings.IngestWorkerSettings``."""

    functions = [run_nightly_ingest]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name: str = "msai:ingest"
    max_jobs: int = 1
    job_timeout: int = 3600  # 1 hour
    max_tries: int = 2  # 1 retry
