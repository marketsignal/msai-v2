"""arq ``WorkerSettings`` for research jobs (parameter sweeps, walk-forward).

Registers the :func:`run_research_job` worker function and wires it to a
dedicated Redis queue (``msai:research``) so that research workloads are
isolated from backtest / ingest jobs.

Why the ``asyncio.set_event_loop_policy(None)`` call?
-----------------------------------------------------
Importing :mod:`msai.workers.research_job` transitively imports
``nautilus_trader`` via the :class:`ResearchEngine`, which installs
uvloop's ``EventLoopPolicy`` as a side-effect of its Rust/Cython init.
On Python 3.12+ that breaks arq's ``Worker.__init__`` because arq calls
the (deprecated) ``asyncio.get_event_loop()`` during startup and uvloop's
policy raises::

    RuntimeError: There is no current event loop in thread 'MainThread'

Resetting the policy to the default after the problematic imports restores
stock-asyncio semantics so arq can start its worker loop cleanly.  The
reset MUST happen after the imports that pull in nautilus_trader.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.research_job import run_research_job

if TYPE_CHECKING:
    from arq.connections import RedisSettings

# See the module docstring for why this reset is necessary.  Do NOT move
# it above the imports -- the reset must happen after nautilus_trader is
# imported for the first time.
asyncio.set_event_loop_policy(None)


class ResearchWorkerSettings:
    """arq worker configuration for ``arq msai.workers.research_settings.ResearchWorkerSettings``."""

    functions = [run_research_job]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name: str = settings.research_queue_name
    max_jobs: int = settings.research_worker_jobs
    job_timeout: int = settings.research_timeout_seconds
    max_tries: int = 2  # 1 retry
