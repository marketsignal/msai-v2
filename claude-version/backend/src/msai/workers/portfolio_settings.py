"""arq ``WorkerSettings`` for the portfolio background job queue.

Registers the portfolio run job function and wires in the Redis connection.

Why the ``asyncio.set_event_loop_policy(None)`` call?
-----------------------------------------------------
Importing modules that transitively import ``nautilus_trader`` installs
uvloop's ``EventLoopPolicy`` as a side effect.  On Python 3.12+ that
breaks arq's ``Worker.__init__``.  Resetting the policy to the default
after the problematic imports restores stock-asyncio semantics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from msai.core.config import settings
from msai.core.queue import _parse_redis_url
from msai.workers.portfolio_job import run_portfolio_job

if TYPE_CHECKING:
    from arq.connections import RedisSettings

# See the module docstring for why this reset is necessary.  Do NOT move
# it above the imports -- the reset must happen after nautilus_trader is
# imported for the first time.
asyncio.set_event_loop_policy(None)


async def run_portfolio(
    ctx: dict[str, Any],
    run_id: str,
    portfolio_id: str,
) -> None:
    """Entry point registered with arq for ``run_portfolio`` jobs.

    Thin forwarder to :func:`run_portfolio_job`.

    Args:
        ctx: arq worker context (Redis pool etc.).
        run_id: UUID string of the :class:`PortfolioRun` row.
        portfolio_id: UUID string of the owning :class:`Portfolio`.
    """
    await run_portfolio_job(ctx, run_id, portfolio_id)


class WorkerSettings:
    """arq worker configuration used by ``arq msai.workers.portfolio_settings.WorkerSettings``."""

    functions = [run_portfolio]
    redis_settings: RedisSettings = _parse_redis_url(settings.redis_url)
    queue_name: str = settings.portfolio_queue_name
    max_jobs: int = 2
    job_timeout: int = 60 * 60  # 1 hour
    max_tries: int = 2  # 1 retry
