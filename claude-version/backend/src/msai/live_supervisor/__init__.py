"""Live supervisor — standalone Python service that owns trading subprocesses.

Runs as ``python -m msai.live_supervisor`` in its own Docker container.
Not part of the FastAPI app or the arq worker — arq awaits
``on_startup`` completion before entering its poll loop (Codex v2 P0)
so a long-running supervisor loop inside it would block the worker.

Top-level responsibilities:

- Consume start/stop commands from the ``msai:live:commands`` Redis
  stream via :class:`msai.services.live_command_bus.LiveCommandBus`
  (PEL recovery + DLQ included).
- Maintain an in-memory handle map of spawned ``mp.Process`` children
  so the reap loop can surface exit codes instantly.
- Run the watchdog as the SOLE liveness authority for startup rows
  (``starting`` / ``building``) — lock-first atomic SIGKILL + UPDATE
  path (v9 Codex v8 P0+P1).
- Run the heartbeat monitor as the SOLE liveness authority for
  post-startup rows (``ready`` / ``running`` / ``stopping``).

The supervisor does NOT send SIGTERM to running trading subprocesses
on shutdown — they're owned by the container's OS and will be reaped
when the container exits. The next supervisor start re-discovers
surviving children via heartbeat-fresh rows.
"""

from msai.live_supervisor.process_manager import ProcessManager

__all__ = ["ProcessManager"]
