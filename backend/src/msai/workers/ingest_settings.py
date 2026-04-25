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
from msai.workers.settings import run_ingest
from msai.workers.symbol_onboarding_job import run_symbol_onboarding

if TYPE_CHECKING:
    from arq.connections import RedisSettings

# See the module docstring for why this reset is necessary.  Do NOT move
# it above the imports -- the reset must happen after nautilus_trader is
# imported for the first time.
asyncio.set_event_loop_policy(None)


class IngestWorkerSettings:
    """arq worker configuration for ``arq msai.workers.ingest_settings.IngestWorkerSettings``.

    Registers the nightly cron ingest, on-demand ingest, and symbol-onboarding
    jobs so the dedicated ``msai:ingest`` queue has consumers for all three
    paths. Isolating these from the backtest worker (``max_jobs=2``) prevents
    ingest-vs-backtest starvation -- see
    ``docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md`` §3.
    ``run_ingest`` remains registered on the default-queue
    ``WorkerSettings.functions`` too for zero-downtime migration; a
    follow-up cleanup PR can drop it from the default worker once the
    queue drains.
    """

    functions = [run_nightly_ingest, run_ingest, run_symbol_onboarding]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name: str = "msai:ingest"
    max_jobs: int = 1
    job_timeout: int = 3600  # 1 hour
    max_tries: int = 2  # 1 retry
