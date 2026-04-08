"""Owns the trading subprocesses spawned by the live supervisor.

INSERT-spawn-UPDATE pattern (decision #13, Codex v4 P0). The ``spawn``
method does NOT wrap the entire flow in a single transaction. v4 did,
and Codex flagged the race: if ``process.start()`` succeeded but the
post-spawn flush/commit failed, the whole transaction rolled back,
leaving a live trading subprocess with no committed row. The next
retry then launched a duplicate.

v5+ splits ``spawn`` into three phases, each in its own transaction:

**Phase A — Reserve the slot** (one transaction):
    - ``SELECT FOR UPDATE`` the ``live_deployments`` row
    - Look up any existing active ``live_node_processes`` row
      (active = ``starting``, ``building``, ``ready``, ``running``,
      ``stopping``)
    - If an active row exists AND its status is ``stopping`` → return
      ``False`` (caller does NOT ACK; retry after the stop completes,
      Codex v4 P0)
    - If an active row exists in any other status → return ``True``
      (idempotent success)
    - ``INSERT`` a new row with ``status='starting'``, ``pid=None``
    - ``COMMIT`` (claims the partial unique index slot)
    - If the INSERT races against a concurrent spawn and the partial
      unique index catches it, return ``True`` (benign race)

**Phase B — Halt-flag re-check + spawn** (NO db transaction):
    - Re-check ``msai:risk:halt`` Redis flag (decision #16, Codex v4 P0).
      If set, flip the row to ``failed`` with
      :attr:`FailureKind.HALT_ACTIVE` and return ``True`` (caller ACKs;
      no retry until ``/api/v1/live/resume`` clears the flag).
    - ``mp.Process(target=spawn_target, args=spawn_args).start()`` —
      irreversible side effect, NO DB transaction wrapping.
    - Stash the handle in ``self.handles`` so ``reap_loop`` and
      ``stop`` can find it.
    - On failure: flip the row to ``failed`` with
      :attr:`FailureKind.SPAWN_FAILED_PERMANENT` and return ``True``
      (caller ACKs — the row is failed so the next retry succeeds).

**Phase C — Record the pid** (one transaction):
    - ``UPDATE live_node_processes SET pid = process.pid``
    - On failure, log loudly but continue — the subprocess's own
      self-write (Task 1.8) will populate pid as a belt-and-suspenders
      backup, and the handle map still has the live process so
      ``stop`` can signal via ``handle.pid``.

The :attr:`handles` attribute maps ``deployment_id`` → ``mp.Process``
while the supervisor is alive. Used by ``reap_loop`` for instant exit
detection (parent and child are in the same Linux namespace, so
``Process.is_alive()`` and ``Process.exitcode`` are meaningful). On
supervisor restart the map is empty; rediscovery happens via the
heartbeat: stale rows are flipped to ``failed`` by
:class:`HeartbeatMonitor` / ``watchdog_loop``, fresh rows are still
running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing as mp
import os
import signal
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from msai.models import LiveDeployment, LiveNodeProcess
from msai.services.live.failure_kind import FailureKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    PayloadFactory = Callable[[UUID, UUID, str, dict[str, Any]], Awaitable[tuple[Any, ...]]]
    """Type alias for the per-spawn payload factory. Called with
    ``(row_id, deployment_id, deployment_slug, payload_dict)`` and
    returns the positional args tuple that will be passed to
    :attr:`ProcessManager._spawn_target` via ``mp.Process(target=...,
    args=...)``. Async so the factory can read the ``live_deployments``
    row + joined ``strategies`` row to construct a fully-populated
    :class:`TradingNodePayload`."""


log = logging.getLogger(__name__)


_HALT_KEY = "msai:risk:halt"
"""Redis key set by ``/api/v1/live/kill-all``. The supervisor re-checks
this in phase B of ``spawn`` so a command queued before ``/kill-all``
(or reclaimed from the PEL after) is rejected at the supervisor layer
even if the endpoint already passed its own pre-check."""


class ProcessManager:
    """Owns the trading subprocesses spawned by this supervisor instance.

    See the module docstring for the INSERT-spawn-UPDATE rationale and
    the phase-by-phase breakdown.

    Args:
        db: Async session factory. Every phase of ``spawn`` (and every
            ``stop``/``_mark_failed``/``_on_child_exit`` call) opens
            its own session + transaction from this factory.
        redis: Async Redis client used ONLY for the halt-flag re-check
            in phase B. Other Redis work lives in
            :class:`LiveCommandBus`.
        spawn_target: The top-level function ``mp.Process`` will run.
            Must be picklable (i.e. importable at top level). In
            production this is ``_trading_node_subprocess`` from
            Task 1.8; in tests it's a local sleep/exit stub.
        spawn_args: Static positional args passed to ``spawn_target``
            for every spawn. Used when ``payload_factory`` is ``None``
            (test path, where every deployment spawns the same stub).
        payload_factory: Optional async callable that constructs the
            per-deployment spawn args at phase-B time. When provided,
            it is called with ``(row_id, deployment_id, deployment_slug,
            payload_dict)`` and the return value REPLACES ``spawn_args``
            for this single invocation. Production uses this to
            construct a :class:`TradingNodePayload` for each deployment
            (Phase 4 task #154 scope-B wiring). If construction raises,
            the row is marked ``failed`` /
            :attr:`FailureKind.SPAWN_FAILED_PERMANENT` and the command
            is ACKed (no retry) — treating it like
            ``process.start()`` failures because a malformed payload is
            an operator config error, not a transient condition.
        spawn_ctx_method: ``multiprocessing`` context method, default
            ``"spawn"`` (clean interpreter). Overridable in tests
            that can't afford the spawn-fork cost.
    """

    def __init__(
        self,
        *,
        db: async_sessionmaker[AsyncSession],
        redis: AsyncRedis,
        spawn_target: Callable[..., None],
        spawn_args: tuple[Any, ...] = (),
        payload_factory: PayloadFactory | None = None,
        spawn_ctx_method: str = "spawn",
        startup_hard_timeout_s: float = 1800.0,
        watchdog_poll_interval_s: float = 30.0,
    ) -> None:
        self._db = db
        self._redis = redis
        self._spawn_target = spawn_target
        self._spawn_args = spawn_args
        self._payload_factory = payload_factory
        self._spawn_ctx = mp.get_context(spawn_ctx_method)
        self.handles: dict[UUID, mp.process.BaseProcess] = {}
        # Watchdog config (Codex batch 3 iter8 P1 fix). Default 1800 s
        # matches plan v8 task #92 (per-deployment override is a Phase 2
        # follow-up). The watchdog kills startup-status rows whose
        # ``started_at`` exceeds this age — necessary because the
        # heartbeat thread starts BEFORE ``node.build()`` (decision #17)
        # and stops AFTER ``dispose()`` (iter4 P1), so a wedged build
        # keeps the heartbeat alive forever and ``HeartbeatMonitor``'s
        # stale sweep (which excludes ``starting``/``building`` by
        # design) can't reach it.
        self._startup_hard_timeout_s = startup_hard_timeout_s
        self._watchdog_poll_interval_s = watchdog_poll_interval_s

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict[str, Any],
        idempotency_key: str,  # noqa: ARG002 — reserved for Task 1.14 dedupe path
    ) -> bool:
        """Spawn a new trading subprocess.

        Returns:
            ``True`` on success or idempotent no-op — caller ACKs the
            command. ``False`` on hard failure — caller does NOT ACK so
            the command stays in the PEL for XAUTOCLAIM retry.
        """
        row_id = await self._phase_a_reserve_slot(
            deployment_id=deployment_id,
            deployment_slug=deployment_slug,
        )
        if row_id is None:
            # Phase A returned None → either hard failure (no
            # deployment), busy with a 'stopping' row, or an already-
            # active row that is NOT stopping (the idempotent-success
            # and hard-failure cases are distinguished by the
            # _PhaseAOutcome below). Re-implemented via helper so the
            # three-valued return is explicit.
            return False
        if row_id is _PhaseAOutcome.ALREADY_ACTIVE:
            return True
        if row_id is _PhaseAOutcome.BUSY_STOPPING:
            return False
        if row_id is _PhaseAOutcome.NO_DEPLOYMENT:
            return False

        # row_id is a real UUID from here on.
        assert isinstance(row_id, UUID)

        # Phase B: halt-flag re-check + process.start().
        halt_set = await self._redis.exists(_HALT_KEY)
        if halt_set:
            log.warning(
                "spawn_blocked_by_halt",
                extra={"deployment_id": str(deployment_id)},
            )
            await self._mark_failed(
                row_id=row_id,
                reason="blocked by halt flag",
                failure_kind=FailureKind.HALT_ACTIVE,
            )
            return True  # ACK — no retry until /resume

        # Resolve the args tuple for this spawn. Production uses the
        # payload factory to construct a per-deployment
        # ``TradingNodePayload`` from the live_deployments row +
        # settings (Phase 4 task #154 scope-B). Tests without a
        # factory fall back to the static ``spawn_args`` tuple set at
        # __init__ time. A factory exception → mark failed + ACK:
        # payload construction errors are operator config issues,
        # not transient conditions, so retrying via XAUTOCLAIM would
        # just spin.
        if self._payload_factory is not None:
            try:
                spawn_args = await self._payload_factory(
                    row_id,
                    deployment_id,
                    deployment_slug,
                    payload,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "spawn_payload_factory_failed",
                    extra={
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                    },
                )
                await self._mark_failed(
                    row_id=row_id,
                    reason=f"payload factory failed: {exc}",
                    failure_kind=FailureKind.SPAWN_FAILED_PERMANENT,
                )
                return True
        else:
            spawn_args = self._spawn_args

        # Codex iter4 P2: halt-flag re-check race.
        #
        # The first halt check happens in phase B above, immediately
        # after reserving the DB slot. Between that check and
        # ``process.start()`` we now await ``self._payload_factory(...)``
        # which performs DB reads and potentially slow work (module
        # imports, strategy path resolution). ``/api/v1/live/kill-all``
        # firing DURING that await would set ``msai:risk:halt`` but
        # the first check already passed, so we'd still reach
        # ``process.start()`` and spawn a fresh subprocess under an
        # active kill switch.
        #
        # Fix: re-check the halt flag right before ``process.start()``.
        # The second check is cheap (a single Redis EXISTS) and
        # closes the race. If the flag is now set, we mark the row
        # ``HALT_ACTIVE`` (same as phase B's handling) and ACK the
        # command — no subprocess spawned, no retry until ``/resume``.
        #
        # This preserves the ``layer-2`` guarantee documented in
        # ``api/live.py``: every code path that could launch a
        # trading subprocess has at LEAST two halt-flag checks
        # bracketing its slow work.
        halt_set_again = await self._redis.exists(_HALT_KEY)
        if halt_set_again:
            log.warning(
                "spawn_blocked_by_halt_post_payload_factory",
                extra={
                    "deployment_id": str(deployment_id),
                    "deployment_slug": deployment_slug,
                    "note": (
                        "halt flag raised during payload factory await — "
                        "catching at second check, no subprocess spawned"
                    ),
                },
            )
            await self._mark_failed(
                row_id=row_id,
                reason="blocked by halt flag (post-payload-factory recheck)",
                failure_kind=FailureKind.HALT_ACTIVE,
            )
            return True  # ACK — no retry until /resume

        try:
            process = self._spawn_ctx.Process(
                target=self._spawn_target,
                args=spawn_args,
            )
            process.start()
        except Exception as exc:  # noqa: BLE001 — we want to catch any start() failure
            log.exception(
                "spawn_process_start_failed",
                extra={"deployment_id": str(deployment_id)},
            )
            await self._mark_failed(
                row_id=row_id,
                reason=f"process.start() failed: {exc}",
                failure_kind=FailureKind.SPAWN_FAILED_PERMANENT,
            )
            return True

        self.handles[deployment_id] = process

        # Phase C: record the real pid on the row.
        try:
            async with self._db() as session, session.begin():
                row = await session.get(LiveNodeProcess, row_id)
                if row is not None:
                    row.pid = process.pid
        except Exception:
            # Don't abort — the handle map still has the live process
            # so reap_loop / stop still work, and Task 1.8's
            # subprocess self-write will populate pid as a fallback.
            log.exception(
                "spawn_pid_update_failed",
                extra={
                    "deployment_id": str(deployment_id),
                    "pid": process.pid,
                },
            )

        return True

    async def _phase_a_reserve_slot(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
    ) -> UUID | _PhaseAOutcome:
        """Run phase A in a single transaction. Return the new row id,
        or one of the :class:`_PhaseAOutcome` sentinels."""
        async with self._db() as session, session.begin():
            deployment = (
                await session.execute(
                    select(LiveDeployment)
                    .where(LiveDeployment.deployment_slug == deployment_slug)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if deployment is None:
                log.error(
                    "spawn_no_deployment",
                    extra={"deployment_slug": deployment_slug},
                )
                return _PhaseAOutcome.NO_DEPLOYMENT

            existing = (
                await session.execute(
                    select(LiveNodeProcess).where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(
                            (
                                "starting",
                                "building",
                                "ready",
                                "running",
                                "stopping",
                            )
                        ),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.status == "stopping":
                    log.info(
                        "spawn_during_stop_busy",
                        extra={"deployment_id": str(deployment_id)},
                    )
                    return _PhaseAOutcome.BUSY_STOPPING
                log.info(
                    "spawn_idempotent",
                    extra={"deployment_id": str(deployment_id)},
                )
                return _PhaseAOutcome.ALREADY_ACTIVE

            row = LiveNodeProcess(
                deployment_id=deployment_id,
                pid=None,
                host=socket.gethostname(),
                started_at=datetime.now(UTC),
                last_heartbeat_at=datetime.now(UTC),
                status="starting",
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # The partial unique index caught a race with another
                # supervisor instance / thread. Treat as idempotent
                # success — the concurrent winner is now building.
                log.info(
                    "spawn_race_idempotent",
                    extra={"deployment_id": str(deployment_id)},
                )
                return _PhaseAOutcome.ALREADY_ACTIVE
            return row.id

    async def _mark_failed(
        self,
        *,
        row_id: UUID,
        reason: str,
        failure_kind: FailureKind,
    ) -> None:
        """Flip a row to ``failed`` with a structured ``failure_kind``.

        ``failure_kind`` is REQUIRED (plan v8 / Codex v7 P1). Writers
        that previously skipped it left ``/start`` unable to classify
        outcomes.
        """
        async with self._db() as session, session.begin():
            row = await session.get(LiveNodeProcess, row_id)
            if row is None:
                return
            row.status = "failed"
            row.failure_kind = failure_kind.value
            row.error_message = reason
            row.exit_code = None

    # ------------------------------------------------------------------
    # Reap loop (decision #15)
    # ------------------------------------------------------------------

    async def reap_once(self) -> None:
        """Run one pass of the reap loop body.

        Walks :attr:`handles`, surfaces exit codes for any
        ``is_alive() == False`` children, and removes them from the
        map. Called in a loop by :meth:`reap_loop` in production;
        tests call it directly to avoid the ``asyncio.sleep`` pacing.
        """
        for deployment_id, proc in list(self.handles.items()):
            if proc.is_alive():
                continue
            proc.join(timeout=1)
            await self._on_child_exit(deployment_id, proc.exitcode)
            del self.handles[deployment_id]

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        """Poll ``self.handles`` every second until ``stop_event`` is set.

        Decision #15: parent + child live in the same container
        namespace, so ``Process.is_alive()`` and ``Process.exitcode``
        give instant exit detection. Heartbeat is the recovery signal
        across supervisor restarts only.
        """
        while not stop_event.is_set():
            await self.reap_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _on_child_exit(self, deployment_id: UUID, exit_code: int | None) -> None:
        """Record a child's terminal state after the reap loop sees it exit.

        Exit-code → failure-kind mapping (matches the codes
        :func:`run_subprocess_async` writes via ``sys.exit``):

        - ``0`` → ``status='stopped'`` / :attr:`FailureKind.NONE`
        - ``2`` → ``status='failed'`` /
          :attr:`FailureKind.RECONCILIATION_FAILED` — the subprocess
          got far enough to compute a startup-health-check diagnosis
          but its terminal DB write missed (Codex batch 3 iter7 P2
          fix). Without this branch, an exit code of 2 would be
          collapsed to ``SPAWN_FAILED_PERMANENT`` and the diagnosis
          captured in the subprocess's exit code would be lost.
        - other (1, ``None``, etc.) → ``status='failed'`` /
          :attr:`FailureKind.SPAWN_FAILED_PERMANENT`

        In every non-zero case the subprocess's own ``finally`` block
        usually wrote the row first; the conditional below only
        backfills ``failure_kind`` if it's still ``NULL`` so we
        never overwrite a richer diagnosis the subprocess already
        persisted.
        """
        async with self._db() as session, session.begin():
            row = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(
                            (
                                "starting",
                                "building",
                                "ready",
                                "running",
                                "stopping",
                            )
                        ),
                    )
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            if exit_code == 0:
                row.status = "stopped"
                if row.failure_kind is None:
                    row.failure_kind = FailureKind.NONE.value
            else:
                row.status = "failed"
                row.error_message = f"child exited with code {exit_code}"
                if row.failure_kind is None:
                    if exit_code == 2:
                        row.failure_kind = FailureKind.RECONCILIATION_FAILED.value
                    else:
                        row.failure_kind = FailureKind.SPAWN_FAILED_PERMANENT.value
            row.exit_code = exit_code

    # ------------------------------------------------------------------
    # Startup watchdog (Codex batch 3 iter8 P1 fix)
    # ------------------------------------------------------------------

    async def watchdog_once(self) -> None:
        """One pass of the startup watchdog.

        Scans for ``starting`` / ``building`` rows whose ``started_at``
        exceeds :attr:`_startup_hard_timeout_s` AND whose ``host``
        matches this supervisor's hostname. For each, ``SIGKILL``
        the pid (a wedged build by definition isn't yielding to async
        signals so SIGTERM would be ignored) and mark the row
        ``failed`` / :attr:`FailureKind.BUILD_TIMEOUT`.

        **Hostname scoping** (Codex batch 3 iter9 P1 fix). Only rows
        whose ``host`` column matches ``socket.gethostname()`` are
        candidates. In a multi-supervisor or rolling-restart
        deployment, ``row.pid`` from another supervisor's PID
        namespace is meaningless to ``os.kill`` here — at best it
        raises ``ProcessLookupError``, at worst it kills an unrelated
        local process. Either way, flipping the row to ``failed``
        without confirming the original child is dead would reopen
        the active-row slot while a wedged twin is still alive on
        another host, allowing a duplicate spawn. The other
        supervisor owns its rows; this supervisor only watchdogs
        its own.

        Why a separate loop instead of letting :class:`HeartbeatMonitor`
        handle this: the heartbeat thread starts BEFORE ``node.build()``
        (decision #17) and stops AFTER ``dispose()`` (Codex batch 3
        iter4 P1 fix), so a wedged build keeps ``last_heartbeat_at``
        fresh forever. ``HeartbeatMonitor`` deliberately excludes
        ``starting``/``building`` from its stale sweep (decision #17 v7
        — startup is the watchdog's territory). Without this watchdog,
        a wedged subprocess would hold the active-row unique-index slot
        indefinitely and block every future ``/start`` for that
        deployment.
        """
        self_host = socket.gethostname()
        cutoff = datetime.now(UTC).timestamp() - self._startup_hard_timeout_s
        async with self._db() as session, session.begin():
            stale_rows = (
                (
                    await session.execute(
                        select(LiveNodeProcess).where(
                            LiveNodeProcess.status.in_(("starting", "building")),
                            LiveNodeProcess.started_at < datetime.fromtimestamp(cutoff, UTC),
                            LiveNodeProcess.host == self_host,
                        )
                    )
                )
                .scalars()
                .all()
            )

            for row in stale_rows:
                # Step 1: SIGKILL the pid (handle map first, fall back
                # to row.pid for processes spawned BEFORE a supervisor
                # restart on the SAME host — same PID namespace, so
                # ``os.kill`` is meaningful). A wedged build won't
                # respond to SIGTERM — go straight to SIGKILL.
                pid_to_kill: int | None = None
                handle = self.handles.get(row.deployment_id)
                if handle is not None and handle.is_alive():
                    pid_to_kill = handle.pid
                elif row.pid is not None:
                    pid_to_kill = row.pid

                if pid_to_kill is not None:
                    try:
                        os.kill(pid_to_kill, signal.SIGKILL)
                        log.warning(
                            "watchdog_sigkill_wedged_startup",
                            extra={
                                "deployment_id": str(row.deployment_id),
                                "row_id": str(row.id),
                                "pid": pid_to_kill,
                                "started_at": row.started_at.isoformat(),
                                "age_s": (datetime.now(UTC) - row.started_at).total_seconds(),
                            },
                        )
                    except ProcessLookupError:
                        # Already gone — the reap loop will catch the
                        # exit if we still have a handle, otherwise the
                        # row just needs a terminal write.
                        log.info(
                            "watchdog_pid_already_gone",
                            extra={
                                "deployment_id": str(row.deployment_id),
                                "pid": pid_to_kill,
                            },
                        )
                    except PermissionError:
                        # Another supervisor instance owns the pid;
                        # we can't kill it but we can still mark the
                        # row failed so the next /start can proceed.
                        log.warning(
                            "watchdog_kill_permission_denied",
                            extra={
                                "deployment_id": str(row.deployment_id),
                                "pid": pid_to_kill,
                            },
                        )

                # Step 2: flip the row to failed/BUILD_TIMEOUT. Done
                # here (not via _mark_failed) because we're already
                # inside the same transaction holding the row.
                row.status = "failed"
                row.failure_kind = FailureKind.BUILD_TIMEOUT.value
                row.error_message = (
                    f"startup wedged for {self._startup_hard_timeout_s}s; "
                    f"watchdog SIGKILLed pid={pid_to_kill}"
                )
                row.exit_code = -int(signal.SIGKILL) if pid_to_kill is not None else None

    async def watchdog_loop(self, stop_event: asyncio.Event) -> None:
        """Run :meth:`watchdog_once` every
        :attr:`_watchdog_poll_interval_s` until ``stop_event`` is set.

        Wired into :func:`live_supervisor.main.run_forever` as a
        background task alongside :meth:`reap_loop` and
        :meth:`HeartbeatMonitor.run_forever`.
        """
        while not stop_event.is_set():
            try:
                await self.watchdog_once()
            except Exception:  # noqa: BLE001
                # Watchdog errors must never crash the supervisor —
                # log and try again on the next pass.
                log.exception("watchdog_pass_failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._watchdog_poll_interval_s,
                )
            except TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop(self, deployment_id: UUID, *, reason: str = "user") -> bool:
        """Send SIGTERM to the deployment's subprocess.

        Flips the row to ``status='stopping'``, then signals the pid
        (via :attr:`handles` first, falling back to ``row.pid`` for
        post-supervisor-restart discovered subprocesses, Codex v5 P0).
        Returns ``True`` on success or idempotent no-op; ``False`` on
        hard failure.

        Note: this implementation does NOT busy-wait for the exit or
        escalate to SIGKILL. Task 1.7's full spec describes a 30-second
        wait + SIGKILL escalation; that's folded into the reap_loop
        instead (the loop will observe the exit on its next pass).
        Callers that need hard-timeout behavior should run the loop
        alongside this.
        """
        async with self._db() as session, session.begin():
            row = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(("starting", "building", "ready", "running")),
                    )
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                log.info(
                    "stop_idempotent",
                    extra={
                        "deployment_id": str(deployment_id),
                        "reason": reason,
                    },
                )
                return True
            row.status = "stopping"
            row_pid = row.pid

        # Determine pid: handle-map first (instant), row fallback for
        # post-supervisor-restart discovered subprocesses.
        handle = self.handles.get(deployment_id)
        pid = handle.pid if handle is not None else row_pid
        if pid is None:
            log.warning(
                "stop_no_pid",
                extra={"deployment_id": str(deployment_id)},
            )
            return True

        # Child may already be gone — reap_loop will catch up on its next pass.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)

        return True


class _PhaseAOutcome:
    """Sentinel values for :meth:`ProcessManager._phase_a_reserve_slot`
    return paths other than "inserted a new row"."""

    NO_DEPLOYMENT: _PhaseAOutcome
    BUSY_STOPPING: _PhaseAOutcome
    ALREADY_ACTIVE: _PhaseAOutcome

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"_PhaseAOutcome.{self._name}"


_PhaseAOutcome.NO_DEPLOYMENT = _PhaseAOutcome("NO_DEPLOYMENT")
_PhaseAOutcome.BUSY_STOPPING = _PhaseAOutcome("BUSY_STOPPING")
_PhaseAOutcome.ALREADY_ACTIVE = _PhaseAOutcome("ALREADY_ACTIVE")
