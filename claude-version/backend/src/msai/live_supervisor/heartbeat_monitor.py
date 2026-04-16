"""Post-startup orphan detector for ``live_node_processes``.

Cross-restart recovery for deployments that were running but lost
their parent supervisor. The HeartbeatMonitor walks
``live_node_processes`` once every ``sleep_interval_s`` seconds and
flips any row whose last heartbeat is older than ``stale_seconds`` to
``status='failed'`` with :attr:`FailureKind.HEARTBEAT_TIMEOUT`, and
syncs the parent ``LiveDeployment.status`` to ``failed`` so the HTTP
layer and UI observe the terminal state (X3 pattern, 2026-04-15).

Ownership split (plan v7, Codex v6 P0)
--------------------------------------

There are TWO liveness authorities in the supervisor:

- The **watchdog** (``ProcessManager.watchdog_loop``) is the SOLE
  authority for STARTUP rows (``status IN ('starting','building')``).
  It SIGKILLs the pid BEFORE flipping the row, so there's no window
  where the row is out of the active set but the process is still alive.
- The **HeartbeatMonitor** (this module) is the SOLE authority for
  POST-STARTUP rows (``status IN ('ready','running','stopping')``).
  It never looks at startup statuses.

v6 had both of them include ``'starting'`` + ``'building'``, which
raced the watchdog's wall-clock deadline and allowed retries to spawn
duplicate children. v7 removes the overlap — this module's query
excludes startup statuses.

Why ``'stopping'`` is included
------------------------------

A stop command that never completes (supervisor crashed mid-stop)
leaves the row in ``'stopping'``. If the subprocess later dies without
the supervisor observing the exit, the HeartbeatMonitor's stale sweep
catches it on the next pass.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import update

from msai.models import LiveDeployment, LiveNodeProcess
from msai.services.live.failure_kind import FailureKind

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


log = logging.getLogger(__name__)


_POST_STARTUP_STATUSES: tuple[str, ...] = ("ready", "running", "stopping")


class HeartbeatMonitor:
    """Post-startup stale-heartbeat sweep.

    Args:
        db: Async session factory.
        stale_seconds: A row whose ``last_heartbeat_at`` is older than
            this many seconds is considered dead. Default 30.
        sleep_interval_s: How long to sleep between sweep passes in
            :meth:`run_forever`. Default 10.
    """

    def __init__(
        self,
        *,
        db: async_sessionmaker[AsyncSession],
        stale_seconds: int = 30,
        sleep_interval_s: float = 10.0,
    ) -> None:
        self._db = db
        self._stale_seconds = stale_seconds
        self._sleep_interval_s = sleep_interval_s

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run ``_mark_stale_as_failed`` every ``sleep_interval_s`` seconds.

        The outer loop honors ``stop_event`` promptly via an
        ``asyncio.wait_for`` on the sleep, so a shutdown signal doesn't
        have to wait the full interval.
        """
        while not stop_event.is_set():
            try:
                await self._mark_stale_as_failed()
            except Exception:  # noqa: BLE001
                # Never let a sweep failure kill the loop — log and
                # continue. A stuck DB will raise again next pass.
                log.exception("heartbeat_monitor_sweep_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._sleep_interval_s)
            except TimeoutError:
                continue

    async def _mark_stale_as_failed(self) -> list[str]:
        """One pass of the stale sweep. Returns the list of
        ``deployment_id`` hex strings the sweep flipped, so tests can
        assert on the batch."""
        flipped: list[str] = []
        flipped_uuids: list[UUID] = []
        cutoff = datetime.now(UTC) - timedelta(seconds=self._stale_seconds)
        async with self._db() as session, session.begin():
            result = await session.execute(
                update(LiveNodeProcess)
                .where(
                    # Post-startup ONLY — the watchdog owns startup.
                    LiveNodeProcess.status.in_(_POST_STARTUP_STATUSES),
                    LiveNodeProcess.last_heartbeat_at < cutoff,
                )
                .values(
                    status="failed",
                    error_message="heartbeat timeout",
                    failure_kind=FailureKind.HEARTBEAT_TIMEOUT.value,
                )
                .returning(LiveNodeProcess.deployment_id)
            )
            for (deployment_id,) in result.fetchall():
                log.error(
                    "heartbeat_stale_marked_failed",
                    extra={
                        "deployment_id": str(deployment_id),
                        "stale_seconds": self._stale_seconds,
                    },
                )
                flipped.append(str(deployment_id))
                flipped_uuids.append(deployment_id)

            # Sync parent deployment row so the HTTP layer and UI see
            # the terminal state. Same pattern as ProcessManager._mark_failed
            # (X3 fix, 2026-04-15 live drill).
            if flipped_uuids:
                await session.execute(
                    update(LiveDeployment)
                    .where(
                        LiveDeployment.id.in_(flipped_uuids),
                        LiveDeployment.status.in_(
                            ("starting", "building", "ready", "running", "stopping")
                        ),
                    )
                    .values(status="failed")
                )
        return flipped
