"""The supervisor main loop — consumes commands and dispatches them.

Runs until ``stop_event`` is set (wired to SIGTERM by ``__main__.py``).
Owns four background tasks:

- **Command consumer** — ``LiveCommandBus.consume`` yields ``start`` /
  ``stop`` commands; this function dispatches them to
  :class:`ProcessManager`.
- **Reap loop** — :meth:`ProcessManager.reap_loop` surfaces exit codes
  for children the supervisor spawned.
- **Heartbeat monitor** — :meth:`HeartbeatMonitor.run_forever` flips
  stale post-startup rows to ``failed``.
- **Startup watchdog** — :meth:`ProcessManager.watchdog_loop`
  SIGKILLs wedged ``starting`` / ``building`` rows that exceed
  ``startup_hard_timeout_s``. Necessary because the heartbeat thread
  starts BEFORE ``node.build()`` (decision #17) and stops AFTER
  ``dispose()`` (Codex batch 3 iter4 P1), so a wedged build keeps
  the heartbeat fresh forever and ``HeartbeatMonitor`` deliberately
  excludes startup statuses (Codex batch 3 iter8 P1 fix).

ACK-on-success-only semantics (decision #13)
--------------------------------------------

``LiveCommandBus`` does not auto-ACK on yield. This loop only calls
``bus.ack(entry_id)`` when the handler returned ``True`` AND the
handler didn't raise. A ``False`` return or an exception leaves the
command in the PEL so a future ``_recover_pending`` sweep retries it.

A malformed command (unknown ``command_type``) is ACKed so it doesn't
bounce forever — if we left it in the PEL it would hit
``MAX_DELIVERY_ATTEMPTS`` and land in the DLQ, but by the time that
happens the operator has already been alerted to an unknown command.
ACK immediately so the DLQ stays clean for genuine poison messages.

Shutdown
--------

The supervisor does NOT send SIGTERM to running trading subprocesses
on shutdown. They're owned by the container's OS and will be reaped
when the container exits. The next supervisor start re-discovers
surviving children via heartbeat-fresh rows (the heartbeat monitor
leaves them alone; only stale rows get flipped to ``failed``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from msai.services.live_command_bus import LiveCommand, LiveCommandType

if TYPE_CHECKING:
    from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
    from msai.live_supervisor.process_manager import ProcessManager
    from msai.services.live_command_bus import LiveCommandBus


log = logging.getLogger(__name__)


async def handle_command(command: LiveCommand, *, process_manager: ProcessManager) -> bool:
    """Dispatch a single command to the :class:`ProcessManager`.

    Returns ``True`` if the caller should ACK, ``False`` if the
    command should stay in the PEL for retry. A ``True`` return on a
    malformed command is intentional (see module docstring).
    """
    if command.command_type is LiveCommandType.START:
        return await process_manager.spawn(
            deployment_id=command.deployment_id,
            deployment_slug=command.payload.get("deployment_slug", ""),
            payload=command.payload,
            idempotency_key=command.idempotency_key,
        )
    if command.command_type is LiveCommandType.STOP:
        return await process_manager.stop(
            command.deployment_id,
            reason=str(command.payload.get("reason", "user")),
        )
    log.warning(
        "unknown_command",
        extra={
            "entry_id": command.entry_id,
            "command_type": str(command.command_type),
        },
    )
    return True  # ACK so we don't loop forever on a malformed command


async def run_forever(
    *,
    bus: LiveCommandBus,
    process_manager: ProcessManager,
    heartbeat_monitor: HeartbeatMonitor,
    stop_event: asyncio.Event,
    consumer_id: str = "supervisor-1",
) -> None:
    """The supervisor's main loop.

    Starts the reap + heartbeat background tasks, then consumes
    commands from the bus until ``stop_event`` is set. On shutdown,
    cancels the background tasks and waits for them to drain.
    """
    monitor_task = asyncio.create_task(heartbeat_monitor.run_forever(stop_event))
    reap_task = asyncio.create_task(process_manager.reap_loop(stop_event))
    # Codex batch 3 iter8 P1 fix: the startup watchdog is the SOLE
    # killer of wedged ``starting`` / ``building`` rows. Without it,
    # a stuck ``node.build()`` would hold the active-row slot
    # indefinitely (heartbeat keeps the row fresh; HeartbeatMonitor
    # excludes startup statuses by design) and block every future
    # ``/start`` for that deployment.
    watchdog_task = asyncio.create_task(process_manager.watchdog_loop(stop_event))

    try:
        async for command in bus.consume(consumer_id, stop_event):
            ok = False
            try:
                ok = await handle_command(command, process_manager=process_manager)
            except Exception:
                # Any exception in the handler means the command
                # stays in the PEL for XAUTOCLAIM retry. Decision #13:
                # NEVER ACK from a finally block. Log + continue.
                log.exception(
                    "command_handler_failed",
                    extra={"entry_id": command.entry_id},
                )
                ok = False

            if ok:
                await bus.ack(command.entry_id)
    finally:
        monitor_task.cancel()
        reap_task.cancel()
        watchdog_task.cancel()
        for t in (monitor_task, reap_task, watchdog_task):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
