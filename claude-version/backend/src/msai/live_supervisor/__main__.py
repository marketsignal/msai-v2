"""Entry point: ``python -m msai.live_supervisor``.

Wires the three production services (database session factory, Redis
client, LiveCommandBus) and starts the supervisor loop. SIGTERM is
translated into a clean ``stop_event`` set so the loop drains
gracefully.

This module is intentionally thin — every piece of real logic lives
in :mod:`msai.live_supervisor.main`, :mod:`process_manager`, and
:mod:`heartbeat_monitor` so unit/integration tests can exercise each
piece without standing up the full Docker stack.

Task 1.8 will replace the ``_trading_node_subprocess`` placeholder
with the real Nautilus entry point.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.config import settings
from msai.core.logging import setup_logging
from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
from msai.live_supervisor.main import run_forever
from msai.live_supervisor.process_manager import ProcessManager
from msai.services.live_command_bus import LiveCommandBus


def _placeholder_trading_subprocess() -> None:
    """Placeholder entry point for Task 1.7 until Task 1.8 lands.

    Exits immediately with code 0 so any deployments spawned before
    Task 1.8 arrives are observed by the reap loop as clean exits.
    Task 1.8 will replace this with the real Nautilus subprocess
    entry point (``msai.services.nautilus.trading_node._trading_node_subprocess``).
    """


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Translate SIGTERM + SIGINT into ``stop_event.set()``.

    We use the event loop's signal handler so the flag gets set inside
    the async context; calling ``stop_event.set()`` from a raw signal
    handler would be a thread/async-context mismatch.
    """

    def _shutdown(sig: signal.Signals) -> None:
        logging.getLogger(__name__).info(
            "live_supervisor_shutdown_signal",
            extra={"signal": sig.name},
        )
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)


async def _async_main() -> int:
    setup_logging(settings.environment)
    logger = logging.getLogger(__name__)
    logger.info("live_supervisor_starting")

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    bus = LiveCommandBus(redis=redis_client)
    process_manager = ProcessManager(
        db=session_factory,
        redis=redis_client,
        spawn_target=_placeholder_trading_subprocess,
    )
    heartbeat_monitor = HeartbeatMonitor(db=session_factory)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        await run_forever(
            bus=bus,
            process_manager=process_manager,
            heartbeat_monitor=heartbeat_monitor,
            stop_event=stop_event,
        )
    finally:
        await redis_client.aclose()
        await engine.dispose()
    logger.info("live_supervisor_stopped")
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    sys.exit(main())
