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

- The **watchdog** (``FleetRouter.watchdog_loop``) is the SOLE
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

from sqlalchemy import select, update

from msai.models import LiveDeployment, LiveNodeProcess
from msai.services.live.failure_kind import FailureKind

if TYPE_CHECKING:
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
        assert on the batch.

        Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST) +
        Finding 2 (NO STALE-SNAPSHOT SKEW). This sweep touches BOTH
        ``live_node_processes`` and ``live_deployments``. Two invariants must hold
        SIMULTANEOUSLY:

        - **Deadlock-freedom (deployment-FIRST).** The bulk UPDATEs acquire row
          locks held until commit, so the order in which row locks are acquired is
          the lock-acquisition order. The deployment row locks must be acquired
          BEFORE the node row locks (the global invariant ``advisory(gateway) →
          live_deployments → live_node_processes``) or this sweep can cycle with
          the deployment-first stop / give-up / Phase-A paths.
        - **No skew (deployment flip tied to the node RETURNING).** The PRIOR fix
          derived the deployments-to-flip from an UNLOCKED candidate SELECT, then
          flipped those deployments, then conditionally flipped nodes WHERE
          <stale predicate>. A heartbeat landing between the SELECT and the node
          flip made the node UPDATE match ZERO rows while the deployment was
          already flipped → a live (``running``) node row paired with a ``failed``
          deployment (hidden from active-only status/deploy gates). The deployment
          flip MUST follow the nodes ACTUALLY terminalized.

        The RETURNING-under-deployment-lock pattern satisfies BOTH:
          (0) PLAIN unlocked SELECT only to find which deployments to LOCK (no
              row lock, no edge);
          (1) lock those ``live_deployments`` rows FOR UPDATE (deployment-FIRST —
              deadlock-safe);
          (2) ``UPDATE live_node_processes ... WHERE deployment_id IN
              (candidate_dep_ids) AND <stale predicate re-evaluated> RETURNING
              deployment_id`` (node locks acquired AFTER the deployment locks →
              acyclic; the RETURNING set = the rows ACTUALLY flipped, with the
              predicate re-checked atomically — a heartbeat that landed since Step 0
              excludes the row here). The ``deployment_id IN (candidate_dep_ids)``
              SCOPE (Finding 1 — BULK-UPDATE SCOPING) restricts the node flip to the
              EXACT deployments LOCKED in Step 1, so the UPDATE can NEVER flip (and
              lock) a node whose deployment was not locked first — closing the
              ``node→deployment`` edge a deployment-that-went-stale-mid-sweep would
              otherwise open;
          (3) ``UPDATE live_deployments ... WHERE id IN (<RETURNING set>)`` (+ the
              non-terminal guard) — flip ONLY the deployments whose nodes were
              actually terminalized.

        Returns the deployment_id hex strings ACTUALLY flipped (the RETURNING
        set), so tests assert on the rows that genuinely transitioned."""
        flipped: list[str] = []
        cutoff = datetime.now(UTC) - timedelta(seconds=self._stale_seconds)
        async with self._db() as session, session.begin():
            # Step 0 — PLAIN unlocked read of which deployments have a stale
            # post-startup node row. No row lock acquired here (it adds no edge);
            # this only tells us which ``live_deployments`` rows to LOCK in Step 1.
            candidate_dep_ids = (
                (
                    await session.execute(
                        select(LiveNodeProcess.deployment_id).where(
                            LiveNodeProcess.status.in_(_POST_STARTUP_STATUSES),
                            LiveNodeProcess.last_heartbeat_at < cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_dep_ids:
                return flipped

            # Step 1 — lock the candidate parent ``live_deployments`` rows FOR
            # UPDATE FIRST (deployment-FIRST — the node UPDATE below acquires its
            # locks AFTER these, keeping the wait-for graph acyclic). We don't act
            # on the result here; the lock is what matters.
            #
            # INVARIANT 2 (council 2026-06-01 follow-up — DETERMINISTIC TOTAL
            # ORDER, pr-toolkit P1): ``ORDER BY LiveDeployment.id`` so this
            # multi-row deployment ``FOR UPDATE`` acquires the row locks in a
            # GLOBALLY DETERMINISTIC order. The deployment-FIRST invariant alone
            # only rules out D→N→D cycles; it does NOT prevent a D↔D (AB-BA)
            # deadlock between TWO concurrent multi-row deployment-set lockers
            # (this sweep, the rescan Step-1a, the watchdog Step-3 pre-lock) whose
            # candidate sets overlap. Without a shared row-lock order, sweep could
            # lock {depA, depB} while rescan locks {depB, depA} → Postgres
            # deadlock-aborts one (the recovery pass silently lost). Locking every
            # multi-row deployment set in ascending-id order gives all of them ONE
            # total order, so two overlapping sweeps serialise instead of cycling.
            await session.execute(
                select(LiveDeployment.id)
                .where(LiveDeployment.id.in_(candidate_dep_ids))
                .order_by(LiveDeployment.id)
                .with_for_update()
            )

            # Step 2 — flip the stale node rows (node locks, AFTER the deployment
            # locks) and RETURN the deployment_ids ACTUALLY terminalized. The
            # stale predicate is RE-EVALUATED here, so a row that advanced its
            # heartbeat between Step 0 and now is NOT flipped and does NOT appear
            # in the RETURNING set — closing the stale-snapshot skew.
            #
            # Council 2026-06-01 (Finding 1 — BULK-UPDATE SCOPING): the UPDATE is
            # SCOPED to ``deployment_id IN (candidate_dep_ids)`` — the EXACT set of
            # deployments LOCKED in Step 1. Without this scope, a DIFFERENT
            # deployment that went stale BETWEEN Step 0 and Step 2 would also match
            # the bare ``status / last_heartbeat_at`` predicate and be flipped here
            # — acquiring that node's row lock while its parent deployment was NEVER
            # locked in Step 1. That is a ``node→deployment``-free-direction
            # violation: a node row write whose deployment is unlocked can cycle
            # with a concurrent deployment-first /stop / give-up / Phase-A path and
            # deadlock. Restricting the UPDATE to the locked candidate set means a
            # node can only be flipped if its deployment was locked FIRST, so no
            # unlocked ``node→deployment`` edge can ever form. (A deployment that
            # goes stale after Step 0 is simply caught on the NEXT sweep pass, where
            # it is in that pass's locked candidate set.)
            flipped_dep_ids = (
                (
                    await session.execute(
                        update(LiveNodeProcess)
                        .where(
                            LiveNodeProcess.deployment_id.in_(candidate_dep_ids),
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
                )
                .scalars()
                .all()
            )
            if not flipped_dep_ids:
                # Every candidate refreshed its heartbeat between Step 0 and the
                # node flip — nothing was terminalized, so NO deployment is flipped
                # (no skew). The deployment locks release on commit.
                return flipped

            for deployment_id in flipped_dep_ids:
                log.error(
                    "heartbeat_stale_marked_failed",
                    extra={
                        "deployment_id": str(deployment_id),
                        "stale_seconds": self._stale_seconds,
                    },
                )
                flipped.append(str(deployment_id))

            # Step 3 — sync ONLY the parent deployments whose node was ACTUALLY
            # flipped (the RETURNING set), under the locks taken in Step 1. The
            # non-terminal guard preserves the X3 terminal-sync semantics and
            # never stomps a concurrent /stop's ``stopped``/``stopping`` state.
            await session.execute(
                update(LiveDeployment)
                .where(
                    LiveDeployment.id.in_(list(flipped_dep_ids)),
                    LiveDeployment.status.in_(
                        ("starting", "building", "ready", "running", "stopping")
                    ),
                )
                .values(status="failed")
            )
        return flipped
