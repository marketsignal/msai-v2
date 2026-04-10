"""Live trading subprocess entry point (Phase 1 task 1.8).

Runs in a fresh Python interpreter under the ``mp.get_context('spawn')``
context that :meth:`msai.live_supervisor.ProcessManager.spawn` creates.
Owns one Nautilus ``TradingNode`` from construction through clean
(or unclean) shutdown.

Decisions embedded here
-----------------------

- **Self-write pid + status='building' BEFORE anything Nautilus-side**
  (decision #17, Codex v5 P0). The supervisor's ``spawn`` path has a
  best-effort phase-C that also writes the pid, but a phase-C failure
  would leave ``live_node_processes.pid=NULL`` and break ``/stop``
  after a supervisor restart. Having the subprocess self-write makes
  ``pid`` populated on every code path.

- **Heartbeat thread starts BEFORE ``node.build()``** (decision #17).
  Hanging builds must age out via the HeartbeatMonitor / watchdog
  stale sweep — starting the heartbeat after ``build`` would defeat
  that.

- **No ``asyncio.wait_for`` around ``node.build()``** (Codex v5 P0).
  ``wait_for`` only cancels the awaiter, not the C-side thread that
  an IB contract load is blocked in. Wedged builds are killed from
  OUTSIDE by the supervisor's watchdog. Inside the subprocess,
  ``node.build()`` runs normally.

- **Canonical FSM signal** for "trader actually started" is
  ``node.kernel.trader.is_running`` (decision #14, see
  :mod:`msai.services.nautilus.startup_health`). Nautilus's engine
  methods silently early-return on failure, so a "succeeded" return
  from ``node.start_async()`` doesn't prove the trader is live. We
  poll ``is_running`` after start and raise
  :class:`StartupHealthCheckFailed` on timeout.

Testability
-----------

Production ``mp.Process`` can't easily be unit-tested with a real
Nautilus ``TradingNode`` because the IB adapter needs IB Gateway.
Instead, :func:`run_subprocess_async` takes a ``node_factory``
callable that constructs the node from the payload. The default
factory builds a real ``TradingNode`` via
:func:`build_live_trading_node_config` + ``TradingNode(config)``;
tests inject a fake factory that returns a stub with the right
method shape, so every correctness property (order-of-operations,
failure paths, cleanup) is exercised end-to-end without touching
Nautilus.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import signal
import sys
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID  # noqa: TC003 — used at runtime for dataclass field type

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models.live_node_process import LiveNodeProcess
from msai.services.live.failure_kind import FailureKind
from msai.services.nautilus.startup_health import (
    StartupHealthCheckFailed,
    wait_until_ready,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload (picklable by mp.Process under the spawn context)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradingNodePayload:
    """Everything a live trading subprocess needs to do its job.

    ``frozen=True`` + only-primitive fields so ``mp.Process`` can
    pickle it under the spawn context. The subprocess imports
    ``msai.*`` modules freshly inside its own interpreter — the payload
    is the only state transferred across the process boundary.
    """

    row_id: UUID
    """Primary key of the ``live_node_processes`` row the supervisor
    inserted in phase A of ``spawn``. Used by the subprocess to
    self-write pid and status transitions."""

    deployment_id: UUID
    deployment_slug: str
    strategy_path: str
    strategy_config_path: str
    strategy_config: dict[str, Any] = field(default_factory=dict)
    strategy_id: UUID | None = None
    """FK to ``strategies.id``. Needed by the engine-level audit hook
    to write valid ``order_attempt_audits`` rows."""
    strategy_code_hash: str = ""
    """SHA256 of the strategy file. Needed for audit trail."""
    paper_symbols: list[str] = field(default_factory=list)
    canonical_instruments: list[str] = field(default_factory=list)
    """Original canonical instrument IDs (e.g. ``AAPL.NASDAQ``) from the
    deployment row. Used by MarketHoursService to prime trading hours
    from the instrument_cache table (which keys on canonical_id)."""
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_account_id: str = "DU0000000"
    database_url: str = ""
    """Async DB URL the subprocess uses to open its own
    ``AsyncEngine``. Passed explicitly (rather than reading ``settings``
    on import) so tests can point at testcontainers."""

    redis_url: str = ""
    """Redis URL the subprocess uses to construct the
    :class:`IBDisconnectHandler` (Phase 4 task 4.2). The handler
    sets ``msai:risk:halt`` when IB Gateway stays disconnected
    past its grace window so the supervisor's push-based kill
    switch tears down the deployment. Empty string means "no
    disconnect monitoring" — tests that don't care about the
    disconnect halt omit this field entirely and the subprocess
    skips the handler construction."""

    startup_health_timeout_s: float = 60.0


# ---------------------------------------------------------------------------
# Protocol types
# ---------------------------------------------------------------------------


# Type alias for the factory callable. Kept untyped (``Any``) to avoid
# importing ``nautilus_trader`` at module load time — the subprocess's
# imports are expensive and every test that touches this module would
# pay the cost.
NodeFactory = "Callable[[TradingNodePayload], Any]"


# ---------------------------------------------------------------------------
# DB write helpers (async — run inside asyncio.run loop)
# ---------------------------------------------------------------------------


async def _update_row(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
    **values: Any,
) -> None:
    """Atomic UPDATE of a single ``live_node_processes`` row."""
    async with session_factory() as session, session.begin():
        await session.execute(
            update(LiveNodeProcess).where(LiveNodeProcess.id == row_id).values(**values)
        )


async def _self_write_pid(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
) -> None:
    """Write the subprocess's own pid onto its ``live_node_processes`` row.

    Runs BEFORE any Nautilus import so ``pid`` is populated even if
    the build path throws. Also transitions ``status`` from
    ``'starting'`` (set by the supervisor) to ``'building'`` and
    bumps the heartbeat so the heartbeat monitor doesn't immediately
    flag the row stale.
    """
    now = datetime.now(UTC)
    await _update_row(
        session_factory,
        row_id,
        pid=os.getpid(),
        status="building",
        last_heartbeat_at=now,
    )


async def _mark_ready(session_factory: async_sessionmaker[AsyncSession], row_id: UUID) -> None:
    await _update_row(
        session_factory,
        row_id,
        status="ready",
        last_heartbeat_at=datetime.now(UTC),
    )


async def _mark_running(session_factory: async_sessionmaker[AsyncSession], row_id: UUID) -> None:
    await _update_row(
        session_factory,
        row_id,
        status="running",
        last_heartbeat_at=datetime.now(UTC),
    )


async def _mark_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
    *,
    status: str,
    failure_kind: FailureKind,
    error_message: str | None,
    exit_code: int,
) -> None:
    await _update_row(
        session_factory,
        row_id,
        status=status,
        failure_kind=failure_kind.value,
        error_message=error_message,
        exit_code=exit_code,
        last_heartbeat_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Heartbeat thread (Phase 1 task 1.9)
# ---------------------------------------------------------------------------


class _HeartbeatThread(threading.Thread):
    """Background thread that bumps ``live_node_processes.last_heartbeat_at``.

    Why a thread instead of an asyncio task in the main loop: Nautilus's
    event loop owns the async context once ``node.run()`` takes over,
    so we can't schedule coroutines on it from the outside. A daemon
    thread that runs its OWN asyncio loop (with its own asyncpg-backed
    engine) is the simplest way to keep the heartbeat writing through
    ``build`` → ``start_async`` → ``run``. The heartbeat doesn't need
    low latency (the stale threshold is 30s); async is just a
    convenience so we don't need a second sync DB driver.

    **Ordering** (decision #17, enforced in task 1.8): the heartbeat
    starts BEFORE ``node.build()``, immediately after the subprocess
    self-writes ``pid`` and ``status='building'``. It runs continuously
    through build, ``start_async``, ``wait_until_ready``, and
    ``node.run()``. It is stopped in the ``finally`` block BEFORE
    ``node.stop_async()`` + ``node.dispose()`` so the heartbeat thread
    can't outlive the row it's writing to.
    """

    def __init__(
        self,
        *,
        async_database_url: str,
        row_id: UUID,
        interval_s: float = 5.0,
    ) -> None:
        super().__init__(daemon=True, name=f"heartbeat-{row_id.hex[:8]}")
        self._url = async_database_url
        self._row_id = row_id
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._ticks = 0
        self._last_error: Exception | None = None

    def stop(self) -> None:
        """Signal the thread to exit on the next wake-up."""
        self._stop_event.set()

    @property
    def ticks(self) -> int:
        """Number of successful heartbeat writes since start. Used by
        tests to assert the thread actually ran during build."""
        return self._ticks

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def run(self) -> None:  # noqa: D401 — Thread.run override
        """Spin up a private asyncio loop and run the async heartbeat
        loop inside it. ``asyncio.run`` creates + tears down the loop
        cleanly when the loop coroutine returns (on stop())."""
        try:
            asyncio.run(self._async_loop())
        except Exception as exc:  # noqa: BLE001
            # asyncio.run itself could raise on interpreter shutdown —
            # catch it so the thread exits cleanly.
            log.exception("heartbeat_thread_loop_failed")
            self._last_error = exc

    async def _async_loop(self) -> None:
        """Main heartbeat loop — runs in the thread's private loop.

        Uses its OWN ``AsyncEngine`` + ``async_sessionmaker`` so it
        doesn't share connections with the subprocess's main loop
        (which lives in a different thread and therefore owns a
        different event loop).
        """
        engine = create_async_engine(self._url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stop_event.is_set():
                try:
                    async with factory() as session, session.begin():
                        await session.execute(
                            update(LiveNodeProcess)
                            .where(LiveNodeProcess.id == self._row_id)
                            .values(last_heartbeat_at=datetime.now(UTC))
                        )
                    self._ticks += 1
                except Exception as exc:  # noqa: BLE001
                    # Never let a transient DB blip kill the loop —
                    # the supervisor's HeartbeatMonitor stale sweep is
                    # the backstop. Log + keep going.
                    log.exception("heartbeat_tick_failed")
                    self._last_error = exc

                # Interruptible sleep — poll ``_stop_event`` so
                # ``stop()`` returns within ``poll_step`` seconds
                # rather than waiting out the full interval.
                poll_step = min(0.1, self._interval_s)
                elapsed = 0.0
                while elapsed < self._interval_s and not self._stop_event.is_set():
                    await asyncio.sleep(poll_step)
                    elapsed += poll_step
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Subprocess async core — tests call this directly
# ---------------------------------------------------------------------------


async def run_subprocess_async(
    payload: TradingNodePayload,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    node_factory: Any,
    heartbeat_factory: Any = None,
    disconnect_handler_factory: Any = None,
    install_signal_handlers: bool = False,
    shutdown_event: asyncio.Event | None = None,
    skip_dispose: bool = False,
    on_node_constructed: Any = None,
    on_post_build: Any = None,
) -> int:
    """Execute one trading subprocess lifecycle end-to-end.

    Returns the exit code the caller should exit with:

    - ``0`` — clean stop
    - ``1`` — generic exception (build/start/run failure)
    - ``2`` — startup health check timed out

    All terminal writes go through :func:`_mark_terminal` so the
    ``failure_kind`` column is always populated for the
    ``/api/v1/live/start`` endpoint to read.

    Order of operations (decision #17 / Codex v5 P0, refactored
    in iter10 P0 to use the real ``TradingNode.run_async`` API):

    1. Self-write pid + ``status='building'``
    2. Start heartbeat thread (BEFORE node.build — decision #17)
    3. ``node = node_factory(payload)``
    4. ``node.build()``
    5. ``node_run_task = asyncio.create_task(node.run_async())`` —
       Nautilus's ``run_async`` first does ``kernel.start_async``
       (which flips ``trader.is_running`` to True) then blocks on
       ``asyncio.gather`` over the engine queue tasks until
       ``stop_async`` is called or an engine task fails.
    6. ``await wait_until_ready(node)`` — polls
       ``trader.is_running`` concurrently with ``run_async``
    7. If ``run_async`` already crashed during start, surface the
       exception from the task instead of marking ready
    8. ``status='ready'``, ``status='running'``
    9. ``await node_run_task`` — blocks until SIGTERM (which
       schedules ``stop_async``) or internal engine failure
    10. finally: ``node.stop_async()`` (idempotent if already
        stopped) → ``node.dispose()`` → ``heartbeat.stop()`` →
        terminal status write

    Args:
        payload: Everything the subprocess needs to know about the
            deployment.
        session_factory: Async session factory the DB writes go through.
        node_factory: Callable that takes ``payload`` and returns a
            ``TradingNode``-like object (the real factory in production,
            a stub in tests).
        heartbeat_factory: Optional callable that takes ``payload`` and
            returns an object with ``start()`` / ``stop()`` methods
            (typically a :class:`_HeartbeatThread`). Tests that don't
            care about heartbeats pass ``None`` to skip it entirely.
            Production callers always pass a real factory.
        disconnect_handler_factory: Optional async callable that takes
            ``(payload, node)`` and returns an awaitable
            :class:`IBDisconnectHandler` instance (Phase 4 task 4.2
            wiring). When present, the handler runs as a sibling task
            to ``node_run_task`` and fires the Redis halt flag if IB
            Gateway stays disconnected past the grace window. Tests
            pass ``None`` to skip; production passes a real factory
            that opens an aioredis client and wires the connection
            probe against the node's data engine.
        install_signal_handlers: When True, register async-aware
            SIGTERM/SIGINT handlers on the running loop that set the
            ``shutdown_event`` and schedule ``node.stop_async()``.
            Production callers pass True; tests typically pass False
            and drive shutdown via ``shutdown_event`` directly.
        shutdown_event: Optional externally-owned ``asyncio.Event``
            that signals "please abort startup / tear down". If omitted,
            a fresh private event is created. Tests inject their own
            event to deterministically drive the "SIGTERM mid-startup"
            code path without relying on signal timing — see
            ``test_trading_node_subprocess.py`` (Codex batch 3 iter2
            P1 regression tests).
        skip_dispose: When True, the finally block does NOT call
            ``node.dispose()``. Required for production callers
            because Nautilus 1.223.0 ``TradingNode.dispose()`` calls
            ``loop.stop()`` if the kernel's loop is currently
            running, which is exactly the loop ``asyncio.run`` is
            blocked on — that breaks ``asyncio.run`` with
            ``RuntimeError: Event loop stopped before Future
            completed`` (Codex batch 3 iter11 P0 fix). The
            production wrapper passes ``True`` and disposes the
            node AFTER ``asyncio.run`` returns. Tests use the
            default ``False`` because their fake ``dispose()`` is
            a no-op and the test loop is unaffected.
        on_node_constructed: Optional callback invoked the moment
            ``node = node_factory(payload)`` returns. Production
            uses it to capture the node for the post-loop dispose
            step (paired with ``skip_dispose=True``).
    """
    # Note: ``_self_write_pid`` and the heartbeat-thread start are
    # NOT run here — they live inside the main ``try`` block below
    # (Codex batch 3 iter7 P3 fix). If either of those raises before
    # the guard, the function would exit without ever running the
    # ``finally`` block's terminal write, and the operator would see
    # the reap loop's generic ``child exited with code 1`` instead
    # of the actual traceback. Inside the guard, the catch-all
    # ``except`` records the failure into the terminal-state
    # locals and the ``finally`` persists them.
    heartbeat: Any = None
    node: Any = None
    # Phase 4 task 4.2 iter-2 wiring: optional sibling task that
    # watches the node's data-engine connection state and fires
    # the Redis halt flag if IB stays disconnected past the
    # grace window. Nullable so tests that don't care about
    # disconnect monitoring can pass ``disconnect_handler_factory=None``
    # and the whole path is a no-op.
    disconnect_handler: Any = None
    disconnect_task: asyncio.Task[None] | None = None

    # Async-loop-aware SIGTERM handler. Runs in the context of the
    # running event loop (thanks to ``loop.add_signal_handler``), so
    # it can safely schedule ``node.stop_async()`` as a task. A raw
    # ``signal.signal`` handler can't drive async shutdown from a
    # foreign context (Codex batch 3 P1 fix).
    shutdown_requested = shutdown_event if shutdown_event is not None else asyncio.Event()
    loop: asyncio.AbstractEventLoop | None = None
    if install_signal_handlers:
        loop = asyncio.get_running_loop()

        def _on_sigterm() -> None:
            log.info(
                "trading_node_sigterm_received",
                extra={"row_id": str(payload.row_id)},
            )
            shutdown_requested.set()
            if node is not None:
                # Schedule the stop on the loop — this will return
                # node.run() once Nautilus finishes its stop handshake.
                # stop_async is idempotent so double-invocation (e.g.
                # two SIGTERMs) is fine.
                asyncio.create_task(node.stop_async())

        try:
            loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
            loop.add_signal_handler(signal.SIGINT, _on_sigterm)
        except (NotImplementedError, RuntimeError):
            # Some platforms (Windows) don't support add_signal_handler.
            # Fall back to the default terminate-on-SIGTERM behavior;
            # the subprocess will die uncleanly and the supervisor's
            # reap loop will catch up via the child's exit code.
            log.warning(
                "trading_node_signal_handler_unavailable",
                extra={"row_id": str(payload.row_id)},
            )

    # Terminal outcome — recorded on every code path and persisted
    # ONLY in the finally block, AFTER cleanup has finished. Writing
    # terminal status before ``heartbeat.stop()``, ``node.stop_async()``,
    # and ``dispose()`` would drop the row out of the active-status
    # set while this subprocess is still alive holding the IB sockets
    # and the Rust-side logger — a fast stop/restart could then
    # reserve a new row and spawn a second child for the same
    # deployment before this one has finished releasing its resources
    # (Codex batch 3 iter3 P1 fix).
    terminal_status: str = "failed"
    terminal_failure_kind: FailureKind = FailureKind.SPAWN_FAILED_PERMANENT
    terminal_error: str | None = None
    terminal_exit_code: int = 1

    def _record_clean_exit(where: str) -> None:
        """Mark this invocation as a clean ``stopped`` termination.

        Used by the startup-shutdown checkpoints so a SIGTERM arriving
        mid-startup becomes a deterministic clean shutdown. The actual
        DB write is deferred to the ``finally`` block.
        """
        nonlocal terminal_status, terminal_failure_kind, terminal_error, terminal_exit_code
        log.info(
            "trading_node_shutdown_during_startup",
            extra={
                "row_id": str(payload.row_id),
                "deployment_id": str(payload.deployment_id),
                "where": where,
            },
        )
        terminal_status = "stopped"
        terminal_failure_kind = FailureKind.NONE
        terminal_error = None
        terminal_exit_code = 0

    try:
        # Self-write the pid + flip status to ``building`` (decision
        # #17 / Codex v5 P0). Inside the guard so a DB blip here
        # still produces a structured terminal write via the
        # except/finally below (Codex batch 3 iter7 P3 fix).
        await _self_write_pid(session_factory, payload.row_id)

        # Start the heartbeat thread BEFORE node.build() so a hung
        # build continues to advance last_heartbeat_at and ages out
        # via the supervisor's watchdog stale threshold (decision
        # #17). Inside the guard for the same iter7 P3 reason: a
        # thread-start failure here is recorded as a structured
        # ``SPAWN_FAILED_PERMANENT`` instead of being lost.
        if heartbeat_factory is not None:
            heartbeat = heartbeat_factory(payload)
            heartbeat.start()

        # Earliest shutdown checkpoint (Codex batch 3 iter6 P2 fix).
        # If SIGTERM lands between ``loop.add_signal_handler`` and
        # the first ``await``, the handler has already set
        # ``shutdown_requested`` but our checkpoints downstream
        # wouldn't observe it until AFTER ``node_factory`` and a
        # potentially-multi-second ``node.build()``. The
        # ``await asyncio.sleep(0)`` yields once so the loop can
        # dispatch any pending signal callback queued during the
        # handler-install window; the subsequent ``is_set()`` check
        # then catches an early operator stop and skips Nautilus
        # construction entirely.
        if install_signal_handlers:
            await asyncio.sleep(0)
        if shutdown_requested.is_set():
            _record_clean_exit("before_node_factory")
            return terminal_exit_code

        node = node_factory(payload)
        if on_node_constructed is not None:
            on_node_constructed(node)
        # node.build() is called directly on the loop thread — NOT
        # via ``asyncio.to_thread``. Nautilus's IB adapter factories
        # instantiate ``asyncio.Queue`` / ``asyncio.Event`` and call
        # ``self._create_task`` (a Cython-bound loop.create_task) from
        # inside ``InteractiveBrokersClient.__init__`` + ``_start``.
        # Those APIs bind to the running loop in the CALLING thread —
        # running them from a worker thread either binds the queues
        # to the wrong loop or raises ``RuntimeError: no running
        # event loop``. Nautilus's own examples all call ``build()``
        # synchronously from the main thread for this reason.
        #
        # Consequence: while ``build()`` is blocked on IB contract
        # loading, the loop can't dispatch signal-handler callbacks.
        # SIGTERM lands but is processed only AFTER ``build()``
        # returns. That is acceptable because:
        #   1. The heartbeat thread keeps writing
        #      (gotcha #20 / decision #17) so the watchdog sees
        #      progress
        #   2. The supervisor's ``startup_hard_timeout_s`` (default
        #      1800 s) watchdog kills stuck ``building`` rows via
        #      SIGKILL, which Python can't mask
        #   3. The post-build ``shutdown_requested.is_set()`` check
        #      catches any SIGTERM that landed during build
        # NO ``asyncio.wait_for`` around it — the supervisor
        # watchdog is the external kill switch for wedged builds.
        node.build()

        # Post-build hook: lets the production wrapper inject
        # collaborators (e.g., MarketHoursService check) into the
        # strategy after Nautilus has constructed it during build().
        if on_post_build is not None:
            await on_post_build(node, payload, session_factory)

        if shutdown_requested.is_set():
            _record_clean_exit("after_build")
            return terminal_exit_code

        # Nautilus's ``TradingNode.run_async()`` (verified against
        # ``nautilus_trader 1.223.0`` at ``live/node.py:338-377``) is
        # the SOLE async entry point: it does
        # ``await self.kernel.start_async()`` first (which is what
        # flips ``trader.is_running`` to True per decision #14) and
        # then ``asyncio.gather`` over the engine queue tasks, which
        # block forever until cancelled or stopped via
        # ``stop_async()``. There is no separate ``start_async()``
        # method on TradingNode (Codex batch 3 iter10 P0 fix);
        # earlier iterations of this module called a fictional
        # ``start_async()`` and would have crashed the moment the
        # production node factory replaced its stub.
        #
        # We schedule ``run_async()`` as a task on the SAME loop
        # this coroutine is running on (TradingNode binds to the
        # current loop at construction). Then we poll
        # ``wait_until_ready`` concurrently. The task continues
        # running until ``stop_async`` flips the kernel down, at
        # which point ``run_async`` falls out of its
        # ``asyncio.gather`` and returns — the cleanup path in
        # ``finally`` awaits the task to make sure shutdown is
        # observed.
        node_run_task: asyncio.Task[None] = asyncio.create_task(
            node.run_async(),
            name=f"trading_node_run_async-{payload.deployment_slug}",
        )

        try:
            await wait_until_ready(
                node,
                timeout_s=payload.startup_health_timeout_s,
                shutdown_event=shutdown_requested,
            )
        except StartupHealthCheckFailed as exc:
            diagnosis = str(exc)
            log.error(
                "startup_health_check_failed",
                extra={
                    "row_id": str(payload.row_id),
                    "deployment_id": str(payload.deployment_id),
                    "diagnosis": diagnosis,
                },
            )
            # Cancel the still-running ``run_async`` task — finally
            # will await it as part of cleanup.
            node_run_task.cancel()
            terminal_status = "failed"
            terminal_failure_kind = FailureKind.RECONCILIATION_FAILED
            terminal_error = diagnosis
            terminal_exit_code = 2
            return terminal_exit_code

        # ``wait_until_ready`` may also exit because ``run_async``
        # crashed during ``kernel.start_async`` and the task is
        # already done with an exception. Surface that as a
        # spawn-failure rather than misclassifying as ready.
        if node_run_task.done():
            exc_from_task = node_run_task.exception()
            if exc_from_task is not None:
                log.exception(
                    "trading_node_run_async_failed_during_startup",
                    exc_info=exc_from_task,
                )
                terminal_status = "failed"
                terminal_failure_kind = FailureKind.SPAWN_FAILED_PERMANENT
                terminal_error = "".join(
                    traceback.format_exception(
                        type(exc_from_task), exc_from_task, exc_from_task.__traceback__
                    )
                )
                terminal_exit_code = 1
                return terminal_exit_code

        if shutdown_requested.is_set():
            node_run_task.cancel()
            _record_clean_exit("after_wait_until_ready")
            return terminal_exit_code

        await _mark_ready(session_factory, payload.row_id)
        await _mark_running(session_factory, payload.row_id)

        # Phase 4 task 4.2 iter-2 wiring: spawn the IB disconnect
        # monitor as a sibling task. We start it AFTER the node is
        # running (not before) so the ``is_connected`` probe has a
        # valid data engine to call. The handler runs until
        # ``shutdown_requested`` is set, the grace window fires, or
        # the finally block cancels it. A failure to construct the
        # handler logs loudly but does NOT fail the deployment —
        # the supervisor's heartbeat watchdog is the fallback
        # safety net even with no disconnect monitor running.
        if disconnect_handler_factory is not None:
            try:
                disconnect_handler = await _maybe_await(disconnect_handler_factory(payload, node))
                if disconnect_handler is not None:
                    # Codex iter3 P2: local on_halt fallback.
                    #
                    # The handler's primary halt path is setting
                    # the Redis kill-switch flag which the
                    # supervisor watches. But the exact scenario
                    # ``IBDisconnectHandler`` was hardened for — an
                    # extended IB outage with correlated Redis
                    # trouble (network partition, datacenter
                    # issue) — means the Redis writes can fail AND
                    # the handler's retry loop exhausts. Without a
                    # local fallback, ``_fire_halt()`` would just
                    # log critical and exit, leaving the subprocess
                    # running with a dead order channel.
                    #
                    # Fix: inject an ``_on_halt`` callback that
                    # sets the local ``shutdown_requested`` event
                    # and schedules ``node.stop_async()``. These
                    # are purely in-process primitives — no Redis,
                    # no DB, no network. ``_fire_halt()`` runs the
                    # callback unconditionally (even when Redis
                    # writes failed, per ``disconnect_handler.py``
                    # Codex batch 10 P2 fix), so the subprocess
                    # always tears down when the grace window
                    # expires.
                    #
                    # The injection targets the private attribute
                    # so it composes with any on_halt the factory
                    # may have pre-configured (future extensibility).
                    # Test fakes that don't use ``_on_halt`` are
                    # unaffected — we check via ``hasattr``.
                    if hasattr(disconnect_handler, "_on_halt"):
                        _preexisting_on_halt = disconnect_handler._on_halt

                        async def _local_shutdown_on_halt() -> None:
                            """Fail-closed fallback: set the local
                            shutdown event + stop the node. Runs
                            AFTER any pre-existing on_halt the
                            factory configured."""
                            log.critical(
                                "ib_disconnect_handler_local_halt_triggered",
                                extra={
                                    "deployment_id": str(payload.deployment_id),
                                    "deployment_slug": payload.deployment_slug,
                                    "reason": (
                                        "grace window expired, triggering "
                                        "local shutdown regardless of Redis "
                                        "halt-flag write status"
                                    ),
                                },
                            )
                            if _preexisting_on_halt is not None:
                                with contextlib.suppress(Exception):
                                    await _preexisting_on_halt()
                            shutdown_requested.set()
                            with contextlib.suppress(Exception):
                                await node.stop_async()

                        disconnect_handler._on_halt = _local_shutdown_on_halt  # noqa: SLF001

                    disconnect_task = asyncio.create_task(
                        disconnect_handler.run(shutdown_requested),
                        name=f"ib_disconnect_handler-{payload.deployment_slug}",
                    )
            except Exception:  # noqa: BLE001
                log.exception(
                    "ib_disconnect_handler_spawn_failed",
                    extra={"deployment_id": str(payload.deployment_id)},
                )
                disconnect_handler = None
                disconnect_task = None

        if shutdown_requested.is_set():
            node_run_task.cancel()
            _record_clean_exit("before_node_run")
            return terminal_exit_code

        # Now wait for ``run_async`` to return — it blocks until
        # ``stop_async`` is called (which the SIGTERM handler
        # schedules) or an internal engine task fails.
        try:
            await node_run_task
        except asyncio.CancelledError:
            # Cleanly cancelled by the SIGTERM handler — fall
            # through to clean-exit recording.
            pass
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            log.exception(
                "trading_node_run_async_crashed",
                extra={
                    "row_id": str(payload.row_id),
                    "deployment_id": str(payload.deployment_id),
                },
            )
            terminal_status = "failed"
            terminal_failure_kind = FailureKind.SPAWN_FAILED_PERMANENT
            terminal_error = tb
            terminal_exit_code = 1
            return terminal_exit_code

        # Clean exit — record the outcome; the finally block will
        # run cleanup and THEN persist the terminal row.
        terminal_status = "stopped"
        terminal_failure_kind = FailureKind.NONE
        terminal_error = None
        terminal_exit_code = 0
        return terminal_exit_code

    except Exception as exc:  # noqa: BLE001 — catch-all so the finally block always runs
        tb = traceback.format_exc()
        log.exception(
            "trading_node_subprocess_failed",
            extra={
                "row_id": str(payload.row_id),
                "deployment_id": str(payload.deployment_id),
                "exc": str(exc),
            },
        )
        terminal_status = "failed"
        terminal_failure_kind = FailureKind.SPAWN_FAILED_PERMANENT
        terminal_error = tb
        terminal_exit_code = 1
        return terminal_exit_code

    finally:
        # Cleanup order (Codex batch 3 iter4 P1 fix):
        #
        # 1. ``node.stop_async()`` + ``dispose()`` run FIRST, with
        #    the heartbeat thread still alive. That keeps
        #    ``last_heartbeat_at`` advancing for the entire cleanup
        #    window, so if a slow dispose (IB socket teardown, Rust
        #    logger flush) exceeds ``HeartbeatMonitor``'s 30 s stale
        #    threshold, the monitor doesn't flip the still-live
        #    row to ``failed`` out from under us — which would
        #    drop the row out of the active-status set and let a
        #    concurrent ``/start`` reserve a new slot before this
        #    subprocess has released IB sockets + the Rust logger.
        #
        # 2. Heartbeat stops SECOND — only after the node is fully
        #    disposed and nothing else needs the row to stay fresh.
        #
        # 3. Terminal write LAST — row only drops out of the
        #    active-status set at this point. By this time the IB
        #    sockets + Rust logger are released, so a restart
        #    reserving the next slot is safe.
        # Dispose is conditionally skipped (Codex batch 3 iter11 P0
        # fix). When ``skip_dispose=True``, the production wrapper
        # handles dispose AFTER ``asyncio.run`` returns — Nautilus
        # 1.223.0 ``TradingNode.dispose()`` calls ``loop.stop()`` if
        # the kernel's loop is running, which is exactly the loop
        # ``asyncio.run`` is blocked on, and would break asyncio.run
        # with ``Event loop stopped before Future completed``.
        # Tests use the default ``False`` because their fake
        # ``dispose()`` is a no-op and the test loop is unaffected.
        # Cancel the disconnect handler task FIRST — it's a
        # sibling of ``node_run_task`` and should wind down before
        # ``node.stop_async()`` because the handler may still be
        # probing the data engine and we want to stop those probes
        # before the engine tears down. Phase 4 task 4.2 iter-2
        # wiring.
        if disconnect_task is not None:
            disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await disconnect_task
        if disconnect_handler is not None:
            close_fn = getattr(disconnect_handler, "aclose", None)
            if close_fn is not None:
                try:
                    await close_fn()
                except Exception:  # noqa: BLE001
                    log.exception("ib_disconnect_handler_aclose_failed")

        if node is not None:
            try:
                await node.stop_async()
            except Exception:  # noqa: BLE001
                log.exception("trading_node_stop_async_failed")
            if not skip_dispose:
                with _safe_dispose(node):
                    pass

        if heartbeat is not None:
            try:
                heartbeat.stop()
                # Best-effort join with a short timeout so a stuck
                # thread doesn't wedge the subprocess's shutdown.
                if hasattr(heartbeat, "join"):
                    heartbeat.join(timeout=5.0)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat_thread_stop_failed")

        # Terminal write happens LAST so the row only drops out of
        # the active-status set after IB sockets / Rust logger are
        # released. We swallow errors here so a DB blip during
        # shutdown doesn't escape as an unhandled exception —
        # the subprocess is already on the exit path.
        try:
            await _mark_terminal(
                session_factory,
                payload.row_id,
                status=terminal_status,
                failure_kind=terminal_failure_kind,
                error_message=terminal_error,
                exit_code=terminal_exit_code,
            )
        except Exception:  # noqa: BLE001
            log.exception("terminal_mark_failed")


# ---------------------------------------------------------------------------
# Context manager: swallow dispose errors so the terminal write path runs
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """Support both sync and async factories. The
    ``disconnect_handler_factory`` hook can return either a
    ready :class:`IBDisconnectHandler` instance OR an
    awaitable that resolves to one (because constructing the
    handler in production requires opening an async Redis
    client, which is an async operation). Tests typically
    pass a sync stub; production passes an async builder.
    """
    if inspect.isawaitable(value):
        return await value
    return value


@contextmanager
def _safe_dispose(node: Any):
    """Call ``node.dispose()`` swallowing any exception.

    Gotcha #20: ``dispose()`` must run on every code path to release
    the Rust-side logger and the IB sockets. If it raises, log and
    continue — the terminal-status write already happened (or is
    about to happen in the caller) and we don't want a dispose
    exception to mask the real failure reason.
    """
    try:
        yield
    finally:
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            log.exception("trading_node_dispose_failed")


# ---------------------------------------------------------------------------
# Production entry point — top-level function so mp.Process can pickle it
# ---------------------------------------------------------------------------


def _trading_node_subprocess(payload: TradingNodePayload) -> NoReturn:
    """Pickle-safe top-level entry point for ``mp.get_context('spawn').Process``.

    Wires the real Nautilus node factory + a live async engine + a
    real heartbeat thread factory, then runs
    :func:`run_subprocess_async` inside ``asyncio.run``. The SIGTERM
    handler is registered **inside** ``run_subprocess_async`` (via
    ``loop.add_signal_handler``) so it can schedule
    ``node.stop_async()`` on the already-running async loop — a plain
    ``signal.signal`` handler would run in a foreign context and can't
    safely drive the async shutdown path (Codex batch 3 P1 fix).

    Gotcha #1: importing ``nautilus_trader`` installs uvloop as the
    event loop policy globally. Gotcha #18: ``asyncio.run(node.run())``
    would conflict. We reset the policy to ``None`` (default) first
    and then use ``asyncio.run`` on OUR wrapper, which manages its
    own loop.

    **Exit semantics** (Codex batch 3 iter4 P2 fix). The function
    terminates via ``sys.exit(exit_code)`` so ``mp.Process.exitcode``
    reflects the computed terminal outcome. Returning an ``int`` from
    an ``mp.Process`` target does NOT set the OS exit status —
    mp ignores the return value and the child exits 0 regardless.
    Without this, a handled failure whose terminal DB write missed
    (e.g. a transient DB blip inside the finally block) would reach
    ``ProcessManager.reap_once()`` with ``exitcode == 0`` and get
    misclassified as a clean ``stopped`` instead of the actual
    failure_kind we intended to write.
    """
    # Gotcha #1 + #18
    asyncio.set_event_loop_policy(None)

    engine = create_async_engine(payload.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def _real_heartbeat_factory(p: TradingNodePayload) -> _HeartbeatThread:
        # Codex batch 3 P1 fix: the real subprocess entry point MUST
        # construct a heartbeat thread. Without this, the heartbeat
        # stops moving after phase A and the HeartbeatMonitor stale
        # sweep kills any deployment that stays up past the 30s
        # threshold.
        return _HeartbeatThread(
            async_database_url=p.database_url,
            row_id=p.row_id,
        )

    async def _real_disconnect_handler_factory(
        p: TradingNodePayload,
        node: Any,
    ) -> Any:
        """Phase 4 task 4.2 iter-2 wiring: construct a real
        :class:`IBDisconnectHandler` bound to this node's data
        engine + the shared Redis. Returns ``None`` (no-op)
        when ``redis_url`` is empty, which keeps existing
        tests that don't care about disconnect monitoring
        working with the legacy fake subprocess path.

        The handler's ``aclose`` method is defined
        dynamically on the instance so ``run_subprocess_async``
        can close the Redis client without importing
        aioredis up here — the subprocess path keeps its
        imports narrow.
        """
        if not p.redis_url:
            return None
        import redis.asyncio as aioredis

        from msai.services.nautilus.disconnect_handler import (
            DEFAULT_GRACE_SECONDS,
            IBDisconnectHandler,
        )

        redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
            p.redis_url, decode_responses=False
        )

        def _is_connected() -> bool:
            # Probe BOTH engines' connectivity (Codex iter2 P1).
            # Nautilus opens SEPARATE IB clients for data
            # (``InteractiveBrokersDataClient`` with
            # ``ibg_data_client_id``) and exec
            # (``InteractiveBrokersExecutionClient`` with
            # ``ibg_exec_client_id``). If the exec client drops
            # but the data client stays up, the deployment has
            # market data but no working order channel — the
            # disconnect handler MUST treat that as an outage.
            #
            # Each engine's ``check_connected()`` iterates its own
            # registered clients and returns False if ANY client
            # is disconnected (data/engine.pyx:296,
            # execution/engine.pyx:similar). We AND them so either
            # layer failing trips the grace-window countdown. This
            # matches what ``startup_health.diagnose()`` already
            # does for the startup readiness check.
            try:
                data_ok = bool(node.kernel.data_engine.check_connected())
            except Exception:  # noqa: BLE001
                data_ok = False
            try:
                exec_ok = bool(node.kernel.exec_engine.check_connected())
            except Exception:  # noqa: BLE001
                exec_ok = False
            return data_ok and exec_ok

        handler = IBDisconnectHandler(
            redis=redis_client,
            is_connected=_is_connected,
            deployment_slug=p.deployment_slug,
            grace_seconds=DEFAULT_GRACE_SECONDS,
        )

        async def _aclose() -> None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()

        handler.aclose = _aclose  # type: ignore[attr-defined]
        return handler

    # Codex batch 3 iter11 P0 fix: dispose() must run AFTER
    # ``asyncio.run`` exits, because Nautilus 1.223.0
    # ``TradingNode.dispose()`` calls ``loop.stop()`` on the kernel's
    # loop — which IS our ``asyncio.run`` loop, so calling dispose
    # from inside would crash with ``Event loop stopped before
    # Future completed``. We use a one-element list as an out-param
    # so ``run_subprocess_async`` can hand us back the constructed
    # node (it might be ``None`` if construction itself failed,
    # which is fine — nothing to dispose).
    node_box: list[Any] = []

    def _capture_node(n: Any) -> None:
        node_box.append(n)

    async def _wire_market_hours(
        node: Any, p: TradingNodePayload, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Post-build hook: construct MarketHoursService inside the
        subprocess and inject it into any RiskAwareStrategy."""
        try:
            from msai.services.nautilus.market_hours import (
                MarketHoursService,
                make_market_hours_check,
            )
            from msai.services.nautilus.risk import RiskAwareStrategy

            svc = MarketHoursService()
            # Prime with canonical instrument IDs (e.g. "AAPL.NASDAQ").
            # paper_symbols contains bare tickers ("AAPL") which don't
            # match instrument_cache.canonical_id. Codex review P1 fix.
            instrument_ids = list(p.canonical_instruments) if p.canonical_instruments else []
            if instrument_ids:
                async with sf() as session:
                    await svc.prime(session, instrument_ids)

            check = make_market_hours_check(svc)

            # Inject into every strategy that is a RiskAwareStrategy
            for strategy in node.trader.strategies():
                if isinstance(strategy, RiskAwareStrategy):
                    strategy._market_hours_check = check  # noqa: SLF001
        except Exception:  # noqa: BLE001
            log.warning("market_hours_wiring_failed")

        # Engine-level audit hook: subscribe to ALL order events via
        # the message bus so every order is audited regardless of
        # whether the strategy uses RiskAwareStrategy or not.
        # Topic pattern: events.order.{strategy_id}
        try:
            import asyncio as _aio
            from datetime import UTC, datetime
            from decimal import Decimal

            from msai.services.nautilus.audit_hook import OrderAuditWriter, OrderSubmittedFacts

            writer = OrderAuditWriter(db=sf)  # keyword-only init (Codex fix)
            _cache = node.kernel.cache  # for fetching full order details
            _loop = _aio.get_running_loop()

            def _on_order_event_sync(event: Any) -> None:
                """Sync handler bridging to async audit writer.

                The Nautilus msgbus calls handlers synchronously from
                the Cython event loop. We schedule the async DB write
                as a fire-and-forget task on the running loop.
                """
                event_type = type(event).__name__

                async def _write() -> None:
                    try:
                        if event_type == "OrderSubmitted":
                            # OrderSubmitted doesn't carry side/qty/price —
                            # fetch the full order from Nautilus cache (Codex fix)
                            order = _cache.order(event.client_order_id)
                            _side = str(order.side) if order else "UNKNOWN"
                            _qty = Decimal(str(order.quantity)) if order else Decimal("0")
                            _price = None
                            _order_type = str(order.order_type) if order else "UNKNOWN"
                            _instrument = str(order.instrument_id) if order else str(event.instrument_id)

                            await writer.write_submitted(
                                OrderSubmittedFacts(
                                    client_order_id=str(event.client_order_id),
                                    strategy_id=p.strategy_id or p.deployment_id,
                                    strategy_code_hash=p.strategy_code_hash or "engine-audit",
                                    instrument_id=_instrument,
                                    side=_side,
                                    quantity=_qty,
                                    price=_price,
                                    order_type=_order_type,
                                    ts_attempted=datetime.now(UTC),
                                    deployment_id=p.deployment_id,
                                    is_live=True,
                                )
                            )
                        elif event_type == "OrderFilled":
                            await writer.update_filled(str(event.client_order_id))
                        elif event_type == "OrderAccepted":
                            broker_id = str(event.venue_order_id) if hasattr(event, "venue_order_id") else None
                            await writer.update_accepted(str(event.client_order_id), broker_order_id=broker_id)
                        elif event_type == "OrderCanceled":
                            await writer.update_cancelled(str(event.client_order_id), reason=None)
                        elif event_type == "OrderRejected":
                            reason = str(event.reason) if hasattr(event, "reason") else None
                            await writer.update_rejected(str(event.client_order_id), reason=reason)
                    except Exception as _evt_exc:  # noqa: BLE001
                        print(f"[MSAI] Audit event {event_type} FAILED: {_evt_exc!r}", flush=True)  # noqa: T201

                _loop.create_task(_write())

            # Subscribe to ALL order events via wildcard pattern
            node.kernel.msgbus.subscribe(topic="events.order.*", handler=_on_order_event_sync)
            # Use print() because structlog isn't initialized in the subprocess
            print("[MSAI] Engine-level audit hook wired via events.order.*", flush=True)  # noqa: T201
        except Exception as _audit_exc:  # noqa: BLE001
            print(f"[MSAI] Engine audit hook wiring FAILED: {_audit_exc!r}", flush=True)  # noqa: T201

    try:
        exit_code = asyncio.run(
            run_subprocess_async(
                payload,
                session_factory=session_factory,
                node_factory=_build_real_node,
                heartbeat_factory=_real_heartbeat_factory,
                disconnect_handler_factory=_real_disconnect_handler_factory,
                install_signal_handlers=True,
                skip_dispose=True,
                on_node_constructed=_capture_node,
                on_post_build=_wire_market_hours,
            )
        )
    finally:
        # engine.dispose() must run inside an event loop; use a fresh one.
        asyncio.run(engine.dispose())

    # Dispose the Nautilus node from sync context — its kernel loop
    # is no longer running (asyncio.run already exited) so dispose
    # won't try to stop a live loop. Gotcha #20 (must dispose to
    # release Rust logger + IB sockets) is satisfied here, not in
    # the finally above.
    if node_box:
        node = node_box[0]
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            log.exception("trading_node_dispose_failed_post_loop")

    # Propagate the computed terminal code to the OS so
    # ``mp.Process.exitcode`` matches ``terminal_exit_code``. Must be
    # ``sys.exit`` — an ``mp.Process`` target's return value is
    # discarded.
    sys.exit(exit_code)


def _build_real_node(payload: TradingNodePayload) -> Any:
    """Production node factory — constructs a real Nautilus TradingNode.

    Imports are deferred inside the function so test invocations of
    :func:`run_subprocess_async` (with fake factories) never pay
    Nautilus's multi-second import cost. Under the ``mp.Process``
    spawn context this function runs in a fresh interpreter anyway
    so module-level vs function-level imports only matter for tests.

    Steps (Nautilus 1.223.0 blessed pattern, live/node.py:230-281):

    1. Build a ``TradingNodeConfig`` from the payload via
       :func:`build_live_trading_node_config`. The config already
       wires ``data_clients[IB_VENUE.value]`` /
       ``exec_clients[IB_VENUE.value]`` with
       :class:`InteractiveBrokersDataClientConfig` /
       :class:`InteractiveBrokersExecClientConfig` instances.
    2. Construct ``TradingNode(config)``. The node's
       ``TradingNodeBuilder`` captures the current asyncio loop
       during ``__init__`` — we rely on the caller
       (:func:`run_subprocess_async` via ``asyncio.run``) to be
       on the loop thread.
    3. Register the two IB client factories against the ``"INTERACTIVE_BROKERS"``
       key. The key MUST match ``IB_VENUE.value`` — the name that
       :func:`build_live_trading_node_config` used when adding the
       client configs to the ``data_clients`` / ``exec_clients``
       dicts. A mismatch surfaces as "no factory for client X" at
       ``node.build()`` time.

    Gotchas honored:

    - **#3** (unique ``client_id``): ``build_live_trading_node_config``
      derives distinct ``ibg_data_client_id`` /
      ``ibg_exec_client_id`` from the deployment slug so two
      concurrent subprocesses can't silently steal each other's
      IB connections.
    - **#4** (venue name pinning): the config uses ``IB_VENUE``
      (``"INTERACTIVE_BROKERS"``) consistently; we register the
      factories under the same name here.
    - **#6** (port/account consistency): validated inside
      ``build_live_trading_node_config`` via
      ``_validate_port_account_consistency``.
    - **#10** (reconciliation on startup): already set via
      ``LiveExecEngineConfig(reconciliation=True)`` in the config
      builder; we do NOT override it here.
    - **#18** (``asyncio.run`` loop conflict): we do NOT call
      ``node.run()`` — :func:`run_subprocess_async` drives
      ``node.run_async()`` as a scheduled task on the already-
      running ``asyncio.run`` loop.
    """
    from nautilus_trader.adapters.interactive_brokers.common import IB
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
        InteractiveBrokersLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from msai.services.nautilus.live_node_config import (
        IBSettings,
        build_live_trading_node_config,
    )

    ib_settings = IBSettings(
        host=payload.ib_host,
        port=payload.ib_port,
        account_id=payload.ib_account_id,
    )

    config = build_live_trading_node_config(
        deployment_slug=payload.deployment_slug,
        strategy_path=payload.strategy_path,
        strategy_config_path=payload.strategy_config_path,
        strategy_config=payload.strategy_config,
        paper_symbols=payload.paper_symbols,
        ib_settings=ib_settings,
    )

    node = TradingNode(config=config)
    # ``IB`` is the module-level constant ``"INTERACTIVE_BROKERS"``
    # (nautilus_trader/adapters/interactive_brokers/common.py:32).
    # Using the named constant instead of a string literal keeps
    # us insulated from a Nautilus rename.
    node.add_data_client_factory(IB, InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
    return node
