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
    - Stash the handle in ``self.node_handle_cache`` so ``reap_loop``
      and ``stop`` can find it.
    - On failure: flip the row to ``failed`` with
      :attr:`FailureKind.SPAWN_FAILED_PERMANENT` and return ``True``
      (caller ACKs — the row is failed so the next retry succeeds).

**Phase C — Record the pid** (one transaction):
    - ``UPDATE live_node_processes SET pid = process.pid``
    - On failure, log loudly but continue — the subprocess's own
      self-write (Task 1.8) will populate pid as a belt-and-suspenders
      backup, and the handle cache still has the live process so
      ``stop`` can signal via ``handle.pid``.

Source of truth vs. local cache
--------------------------------
The authoritative record of which deployments are running is the
``live_node_processes`` table PLUS the heartbeat each subprocess writes
to it. :attr:`FleetRouter.node_handle_cache` (a :data:`NodeHandleCache`)
is a NON-AUTHORITATIVE, in-memory map of ``deployment_id`` →
``mp.Process`` for the subprocesses THIS supervisor instance spawned.
It exists purely as a fast local optimization: parent and child share a
Linux PID namespace, so ``Process.is_alive()`` / ``Process.exitcode``
give ``reap_loop`` instant exit detection without polling the DB, and
``stop`` can SIGTERM via the live handle instead of the persisted pid.

Crucially, the cache is EMPTY after a supervisor restart — the
subprocesses it spawned may still be alive, but their handles are gone.
Recovery does NOT depend on the cache: stale rows are flipped to
``failed`` by :class:`HeartbeatMonitor` / ``watchdog_loop`` (heartbeat
staleness), fresh rows are still running, and ``stop`` falls back to the
persisted ``row.pid`` when the cache misses. Never treat a cache miss as
"the deployment isn't running" — consult the DB + heartbeat instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import multiprocessing as mp
import os
import signal
import socket
import uuid as uuid_module
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.live_supervisor.restart_policy import RestartAction, RestartPolicy
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
    :attr:`FleetRouter._spawn_target` via ``mp.Process(target=...,
    args=...)``. Async so the factory can read the ``live_deployments``
    row + joined ``strategies`` row to construct a fully-populated
    :class:`TradingNodePayload`."""


@dataclass(slots=True)
class _CachedNode:
    """The value stored in :attr:`FleetRouter.node_handle_cache` (council
    2026-05-31 — reaper own-by-row-id / F3 fix).

    Carries BOTH the live ``mp.Process`` handle for the subprocess this
    supervisor spawned AND the ``owned_row_id`` — the ``LiveNodeProcess.id``
    Phase A reserved for THIS spawn. Threading the owned row id through the
    cache is what lets the reaper (``reap_once`` → ``_on_child_exit``) classify
    and terminal-write the row it OWNS by id, rather than ``ORDER BY started_at
    DESC LIMIT 1`` — a periodic rescan can concurrently INSERT a fresher
    ``starting`` row for the same deployment, and a latest-row reaper would
    clobber THAT (the F3 bug).

    ``proc`` is the handle (``.pid`` / ``.is_alive()`` / ``.exitcode`` / ``.join``
    are read by ``reap_once`` / ``watchdog_once`` / ``stop``). ``owned_row_id``
    may be ``None`` only on the legacy/test path where the spawn was driven
    without a reserved row (production always supplies it).
    """

    proc: mp.process.BaseProcess
    owned_row_id: UUID | None = None


# Module-level (not under TYPE_CHECKING) so it's available as a runtime
# annotation on the instance attribute.
NodeHandleCache = dict[UUID, _CachedNode]
"""Type alias for :attr:`FleetRouter.node_handle_cache`.

A NON-AUTHORITATIVE, in-memory map of ``deployment_id`` →
:class:`_CachedNode` (the ``mp.Process`` handle PLUS its reserved
``owned_row_id``) for the subprocesses the local :class:`FleetRouter`
spawned. The authoritative source of truth is the
``live_node_processes`` table + the per-subprocess heartbeat; this cache
is a local fast-path only and is EMPTY after a supervisor restart. A
cache miss never means "not running" — consult the DB + heartbeat. See
the module docstring's "Source of truth vs. local cache" section."""


log = logging.getLogger(__name__)


_HALT_KEY = fleet_halt_key()
"""Redis key set by ``/api/v1/live/kill-all`` (consolidated into
``msai.core.halt_keys`` via PR 1 T2). The supervisor re-checks
this in phase B of ``spawn`` so a command queued before ``/kill-all``
(or reclaimed from the PEL after) is rejected at the supervisor layer
even if the endpoint already passed its own pre-check."""


# Granularity of the cancellable auto-restart backoff (PR 2 T6). The backoff
# is implemented as a poll loop so a fleet OR account halt arriving DURING the
# wait abandons the restart within at most this interval (Claude iter-1 3-B).
# A bare ``asyncio.sleep(delay)`` could not observe a halt latch raised
# mid-wait. Kept small so the abandon latency is sub-second while still being
# cheap (one Redis EXISTS pair per tick).
_BACKOFF_POLL_INTERVAL_S = 0.5


# PR 2 F1/F2 — bounded retry of a TRANSIENT auto-restart dispatch failure
# inside the per-account restart task. A transient DB/Redis blip while loading
# the restart context, reserving the Phase-A slot, or spawning must NOT leave
# the deployment permanently stranded (``failed`` with the sentinel set,
# nothing re-driving it at runtime). The task retries a bounded number of times
# with a short backoff; if it ultimately can't dispatch it clears the
# idempotency sentinel + re-fails LOUDLY (alertable) so a later reap/rescan can
# re-drive it. The Phase-A partial unique index + the in-flight-task dedupe —
# NOT the sentinel being set-before-dispatch — are what serialise against a
# double-spawn, so clearing the sentinel on give-up is safe.
_RESTART_TASK_MAX_ATTEMPTS = 4
"""Total dispatch attempts (1 initial + 3 retries) before the per-account
restart task gives up on a persistently-transient dispatch."""

_RESTART_TASK_RETRY_BACKOFF_S = 2.0
"""Short pause between transient dispatch retries inside the restart task.
Small (the policy's own BACKOFF already paces a crash-loop); this only paces a
RETRYING dispatch whose DB/Redis dependency is momentarily unavailable."""


# PR 2 F1 (review) — fixed lock-key sentinel for the GLOBAL same-gateway
# serialisation. ``_phase_a_reserve_slot`` takes a Postgres transaction-level
# advisory lock keyed by the effective gateway BEFORE the concurrent-startup
# check+insert, so two START commands for DIFFERENT deployments that share one
# IB Gateway can never both observe "no other starting row" and both launch a
# TradingNode against the same gateway (Nautilus gotcha #3 — same client_id →
# silent disconnect). When the caller passes a ``gateway_session_key`` the lock
# is keyed by THAT gateway (different gateways → different lock keys → no
# cross-gateway contention, the multi-login enabler). When it's ``None`` the
# concurrent-startup guard is GLOBAL (any other starting deployment blocks), so
# the lock must be global too — keyed by this fixed sentinel so every legacy
# reservation serialises through the same key, matching the guard's scope.
_GLOBAL_GATEWAY_LOCK_KEY = "msai:phase-a:global-gateway-lock"


# =============================================================================
# LOCK-ORDER INVARIANT (council 2026-06-01 — real-money concurrency core)
# =============================================================================
#
# The GLOBAL lock-acquisition order for every path that touches the
# ``live_deployments`` + ``live_node_processes`` rows of a deployment is:
#
#     advisory(gateway)  →  live_deployments FOR UPDATE  →  live_node_processes
#                                                            FOR UPDATE
#
# NO path may hold a node-row lock while waiting on its (or any) deployment row.
# There is exactly ONE node→deployment-free direction.
#
# Per-path lock-order table (every both-row-locking participant):
#
#   path                                  | locks               | order
#   --------------------------------------|---------------------|-----------------
#   _phase_a_reserve_slot                  | advisory, dep FU,   | advisory → dep
#     (FleetRouter)                        | (node INSERT only,  |   (never locks an
#                                          |  no existing-row FU)|    existing node row)
#   stop (FleetRouter)                     | dep FU, node FU     | dep → node
#   _clear_sentinel_and_refail_after_giveup| dep FU, node FU     | dep → node
#   _refail_stranded_restart               | dep FU, node FU     | dep → node
#   _terminalize_reserved_row              | dep FU, node FU     | dep → node
#   _reap_ownerless_active_rows            | dep FU, node FU     | dep → node
#   live_stop (api/live.py)                | dep FU, node FU     | dep → node
#   _mark_running (trading_node_subprocess)| dep FU, node FU     | dep → node
#   _mark_terminal (trading_node_subproc.) | dep FU, node FU     | dep → node
#                                          |                     |   (Finding 1)
#   _mark_failed (FleetRouter)             | dep FU, node FU     | dep → node
#                                          |                     |   (Finding 1)
#   watchdog_once (FleetRouter)            | dep UPDATE,         | dep → node
#                                          |  node UPDATE by id  |   (bulk; dep first;
#                                          |                     |    ORDER-BY-id PRE-LOCK
#                                          |                     |    before UPDATE — Inv 2)
#   HeartbeatMonitor._mark_stale_as_failed | dep FU + node       | dep → node
#                                          |  UPDATE...RETURNING  |   (bulk; dep first,
#                                          |                     |    ORDER BY id (Inv 2);
#                                          |                     |    flip tied to
#                                          |                     |    node RETURNING)
#   _load_rescan_candidates Step-1 flip    | dep FU + node       | dep → node
#                                          |  UPDATE...RETURNING  |   (bulk; dep first,
#                                          |                     |    ORDER BY id (Inv 2);
#                                          |                     |    scoped to locked
#                                          |                     |    dep ids; flip tied
#                                          |                     |    to node RETURNING)
#   HeartbeatMonitor._mark_stale_as_failed | dep FU + node       | dep → node
#     (heartbeat_monitor.py)               |  UPDATE...RETURNING  |   (bulk; dep first,
#                                          |  scoped to locked    |    scoped to locked
#                                          |  dep ids (Finding 1) |    dep ids; flip tied
#                                          |                     |    to node RETURNING)
#   _on_child_exit (FleetRouter)           | dep FU, node FU      | dep → node
#                                          |                     |   (Finding 2 — both-
#                                          |                     |    row writer:
#                                          |                     |    terminal node +
#                                          |                     |    deployment sync)
#
# Node-ONLY (NOT both-row-lock participants — no deployment FOR UPDATE held):
#   _operator_stop_requested (node FU only).
#   Phase C pid-record / stop-race re-read (spawn_with_outcome → _phase_b_spawn_guarded,
#     node FU only — Codex/pr-toolkit iter-27/28). A single LiveNodeProcess FOR UPDATE
#     with NO deployment lock held ⇒ adds no node→deployment edge ⇒ the acyclic proof
#     below is unaffected.
#
# Finding 1 (council 2026-06-01): ``_mark_failed`` (here) and ``_mark_terminal``
# (trading_node_subprocess) were PREVIOUSLY listed as node-only with the false
# claim "plain session.get on BOTH — no FOR UPDATE — holds no lock." That was
# WRONG: a dirty-flush ORM UPDATE acquires a row WRITE lock held until commit, and
# SQLAlchemy flushes UPDATEs in dirty-order (NOT FK order), so writing the node
# row before the deployment row was a node→deployment lock-acquisition edge that
# DEADLOCKED concurrently with /stop. Both are now deployment-FIRST (LiveDeployment
# FOR UPDATE before LiveNodeProcess FOR UPDATE) — moved into the table above.
#
# Finding 1 — BULK-UPDATE SCOPING (council 2026-06-01): every bulk
# ``UPDATE live_node_processes`` that runs after locking a candidate deployment
# set is SCOPED to that locked set (``deployment_id IN (locked_ids)``), so it can
# NEVER flip (and acquire the row lock of) a node whose deployment was not locked
# FIRST. ``HeartbeatMonitor._mark_stale_as_failed`` Step-2 and the
# ``_load_rescan_candidates`` Step-1 flip both carry this scope; ``watchdog_once``
# flips node rows BY ID; ``_reap_ownerless_active_rows`` flips a single row BY ID.
# So no bulk node flip can open an unlocked ``node→deployment`` edge.
#
# Finding 2 — NO STALE-SNAPSHOT SKEW (council 2026-06-01): the bulk stale sweeps
# that flip BOTH rows (HeartbeatMonitor stale sweep + rescan Step-1 stale-active
# flip) derive the deployment flip from the node UPDATE's ``RETURNING
# deployment_id`` set, NOT from the prior unlocked candidate SELECT. So a heartbeat
# that lands between the SELECT and the node flip (making the node UPDATE match
# zero rows) can NEVER leave a live node paired with a ``failed`` deployment (skew).
# The deployment rows are still locked FOR UPDATE FIRST (deployment-first; the node
# UPDATE...RETURNING runs UNDER those locks → acyclic).
#
# Finding 2 — TERMINAL-WRITE DEPLOYMENT SYNC (council 2026-06-01): every path that
# writes a NODE row to a terminal status syncs the parent ``LiveDeployment`` (with
# the non-terminal guard) in the SAME deployment-first transaction. ``_on_child_exit``
# (the reaper) was the last node-only terminal writer that did NOT — on the
# stale-active path (SIGKILL/OOM) it wrote the node ``failed`` but left the
# deployment ``running``, wedging a halt/pause/no-policy-suppressed restart out of
# the rescan. It is now a DEPLOYMENT-FIRST both-row writer (moved into the table
# above). ``_mark_running`` writes ``running`` (NOT terminal) so it is N/A;
# Phase-A only INSERTs the new node row (no terminal write).
#
# ACYCLIC WAIT-FOR-GRAPH PROOF. The only lock-wait edges in the entire
# supervision subsystem are:
#     advisory(gateway) → live_deployments      (Phase A only)
#     live_deployments  → live_node_processes   (every both-row path above,
#                                                 incl. the now-deployment-first
#                                                 reaper _on_child_exit)
# There is NO ``live_node_processes → live_deployments`` edge anywhere: every
# both-row participant acquires the deployment lock FIRST, every bulk node UPDATE
# is scoped to a pre-locked deployment set (Finding 1), and the one remaining
# node-only path (``_operator_stop_requested``) never takes a deployment FOR UPDATE
# while holding the node lock. A deadlock cycle requires at least a D→N→D path;
# since N→D does not exist, no cycle can form. ⇒ the global lock order is acyclic
# ⇒ no deadlock.
#
# =============================================================================
# THREE-INVARIANT COMPLETENESS PROOF (council 2026-06-01 follow-up — the
# real-money operator-stop / multi-row-lock / cancellation core)
# =============================================================================
#
# INVARIANT 1 — OPERATOR-STOP GATES EVERY NODE-SPAWN PATH. After an operator
# /stop (or /kill-all / /drain halt) it is IMPOSSIBLE for a node to spawn. Every
# spawn path funnels through ``_phase_a_reserve_slot`` (Phase A), which is the
# single chokepoint, plus the pre-start gate + Phase-B halt re-checks in
# ``_phase_b_spawn_guarded``. Per-path gate:
#   (a) initial /start (queued, then consumed) → Phase A OPERATOR-TERMINAL
#       DEPLOYMENT GATE: ``deployment.status == "stopped"`` ⇒ OPERATOR_STOPPED
#       abort. /stop's no-active-row PRE-ACTIVE branch (FleetRouter.stop +
#       live_stop) marks the deployment ``stopped`` when the START was queued but
#       unconsumed (no node row to carry ``stop_requested_at``). PLUS the fleet/
#       account halt re-checks (Phase B) + the pre-start ``stop_requested_at``
#       gate for an active-row /stop.
#   (b) redelivered START (XAUTOCLAIM / PEL) → SAME Phase-A ``stopped`` gate
#       (restart_carry is None) + the pre-start gate.
#   (c) reaper ``_attempt_auto_restart`` (restart_carry) → halt gate FIRST
#       (``_halt_active`` fail-closed) + pre/post-backoff ``_operator_stop_
#       requested`` + Phase-A ``stopped`` gate (restart_carry path) + Phase-A
#       atomic ``stop_requested_at`` re-read.
#   (d) periodic ``rescan_for_restart`` → candidate query gates
#       ``stop_requested_at IS NULL`` AND ``LiveDeployment.status == 'failed'``
#       (a ``stopped`` deployment is never a candidate) + the same Phase-A gates.
#   (e) give-up retry / ``_refail_stranded_restart`` → routes through the same
#       Phase A, so the same ``stopped`` + ``stop_requested_at`` gates apply.
#   ``stopped`` is OPERATOR-TERMINAL: Phase A NEVER resets it to ``starting`` and
#   NEVER respawns it (only ``failed`` is recoverable). Legitimate restart of a
#   ``failed`` deployment is unchanged.
#
# INVARIANT 2 — EVERY MULTI-ROW LOCK ACQUIRES IN ONE DETERMINISTIC TOTAL ORDER.
# The deployment-FIRST rule above kills D→N→D cycles but NOT a D↔D (AB-BA) cycle
# between two concurrent multi-row DEPLOYMENT-SET lockers with overlapping sets.
# Every such locker now orders by ``LiveDeployment.id``:
#   - HeartbeatMonitor._mark_stale_as_failed Step-1 : SELECT ... id IN(...) ORDER
#                                                      BY LiveDeployment.id FOR UPDATE
#   - _load_rescan_candidates Step-1a (rescan)       : SELECT ... id IN(...) ORDER
#                                                      BY LiveDeployment.id FOR UPDATE
#   - watchdog_once Step-3                            : ordered SELECT ... ORDER BY
#                                                      id FOR UPDATE PRE-LOCK before
#                                                      the bulk UPDATE (re-locks
#                                                      held rows; no reorder)
#   - live_stop /drain terminal-sync loop (api)       : iterates sorted(dep ids)
#   Every other deployment lock is SINGLE-ROW by id (session.get / WHERE id == X /
#   WHERE deployment_slug == X) — inherently order-safe. NO path locks a SET of
#   NODE rows: node writes are single-row by id, or bulk UPDATEs scoped to a
#   pre-locked deployment set (re-locking under the held deployment locks). ⇒ all
#   multi-row deployment-set acquisitions share one ascending-id order ⇒ no AB-BA.
#
# INVARIANT 3 — CANCELLATION NEVER ORPHANS A RESERVED ROW. Once Phase A commits
# the reserved ``starting`` row, the ENTIRE reserved→handle-install span runs
# inside ``_phase_b_spawn_guarded``'s single ``try / except asyncio.CancelledError``
# wrapper: the fleet/account halt re-checks, the account-halt lookups, the
# ``await self._payload_factory(...)`` (Codex P2 — the gap the prior wrap missed,
# CancelledError is BaseException so the payload-factory's ``except Exception``
# never caught it), the pre-start stop-intent gate, and ``process.start()``. A
# cancellation at ANY of those awaits, while ``handle_installed is False``,
# terminalizes the reserved row (deployment→node lock order) before re-raising —
# no orphan ``starting`` pid=NULL row. Once the live handle is installed
# (``handle_installed = True``) the running node owns the row (the reap loop reaps
# it). The ownerless-active-row backstop (``_reap_ownerless_active_rows``) is the
# defense-in-depth net for any row a cancellation cleanup itself couldn't reach.
# =============================================================================


@dataclass(frozen=True, slots=True)
class _RestartContext:
    """Everything the reaper needs to (re-)evaluate an auto-restart for one
    dead deployment, loaded in a single DB read (PR 2 T6).

    ``process`` is the latest (terminal) ``live_node_processes`` row — it
    carries the restart-authority counters the :class:`RestartPolicy` reads
    and the ``gateway_session_key`` the respawn must preserve so per-session
    startup serialisation isn't silently degraded to global. ``account_id``
    keys the account halt latch (``DUP…`` / ``U…``) and may be ``None`` for a
    legacy deployment row.
    """

    deployment_id: UUID
    deployment_slug: str
    account_id: str | None
    gateway_session_key: str | None
    process: LiveNodeProcess


@dataclass(frozen=True, slots=True)
class _RestartCarry:
    """The restart-authority counter state carried FORWARD onto the new
    per-spawn row when an auto-restart respawns a dead deployment (PR 2 T6).

    The bounded crash-loop brake only works if the consecutive-failure
    counter survives a respawn. The counter lives on the per-spawn
    ``live_node_processes`` row, but each respawn INSERTs a BRAND-NEW row
    (Phase A) — so without carrying the prior row's counter forward, the new
    row would default to 0 and the ceiling could never trip (prior-review
    P1). This struct snapshots the PRIOR (terminal) row's counter state so
    Phase A can (a) apply the policy's :meth:`RestartPolicy.record_failure`
    increment to it under the same row-lock that reserves the new slot, and
    (b) stamp the incremented value onto the new row — keying the counter to
    the logical DEPLOYMENT, not the ephemeral per-spawn row.

    ``prior_*`` are the values read off the terminal row at decision time.
    """

    prior_consecutive_respawn_failures: int
    prior_last_restart_at: datetime | None
    prior_auto_restart_paused: bool
    prior_auto_restart_pause_reason: str | None


class _RestartOutcome(Enum):
    """Structured result of one auto-restart attempt (council #4 OPT C Part 2).

    The reaper's fast per-path retry (``_run_restart_task``) needs to tell a
    TRANSIENT no-ACK failure (which it should RETRY in ~seconds) apart from a
    DELIBERATE terminal suppression (which it must NOT retry, or it would churn
    a halted / paused / operator-stopped / permanent / already-active
    deployment). A bare ``bool`` can't carry that distinction, so
    :meth:`FleetRouter._attempt_auto_restart` returns this enum and
    :meth:`FleetRouter._maybe_auto_restart` collapses it back to the legacy
    "did a restart actually happen" bool for the re-scan + the existing callers.
    """

    RESTARTED = "restarted"
    """A process genuinely (re)started — the honest "a restart happened" signal
    the periodic re-scan tallies and ``/live/status`` reports. Terminal."""

    SUPPRESSED = "suppressed"
    """A DELIBERATE terminal decision NOT to (re)start: no policy injected, no
    restart context, halt gate (fleet OR account) active, ``RestartPolicy``
    PAUSED (ceiling tripped), backoff abandoned, OR a spawn ACK-drop
    (``ack=True, process_started=False`` — halt raced post-decision / terminal /
    paused / payload PERMANENT) / idempotent ``ALREADY_ACTIVE`` race-loser.
    The fast retry must NOT re-drive these — doing so would churn a deliberately
    suppressed deployment. The authoritative periodic re-scan re-evaluates them
    later if (and only if) their suppressor clears."""

    TRANSIENT_NO_ACK = "transient_no_ack"
    """A TRANSIENT no-ACK spawn outcome (``ack=False, process_started=False`` —
    ``CONCURRENT_STARTUP`` while a sibling on the same gateway is still
    initialising, or a transient post-Phase-A payload-factory blip). NOT a
    deliberate suppression: it is expected to succeed on a retry once the
    momentary condition clears. The fast per-path retry RE-DRIVES this so the
    common transient recovers in ~seconds instead of waiting a periodic-rescan
    tick (council #4 OPT C Part 2 — closes the iter-4 no-ACK P1)."""


class FleetRouter:
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
        restart_policy: Optional bounded auto-restart guard (PR 2 T6).
            When provided, ``_on_child_exit`` and ``rescan_for_restart``
            drive halt-gated, bounded, reconcile-verified auto-restart
            of crashed nodes through it. When ``None`` (the legacy /
            test wiring), auto-restart is OFF — ``_on_child_exit`` only
            records terminal state, exactly as before.
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
        restart_policy: RestartPolicy | None = None,
        rescan_stale_seconds: int = 30,
    ) -> None:
        self._db = db
        self._redis = redis
        self._spawn_target = spawn_target
        self._spawn_args = spawn_args
        self._payload_factory = payload_factory
        self._spawn_ctx = mp.get_context(spawn_ctx_method)
        # NON-AUTHORITATIVE local cache (see module docstring). The DB +
        # heartbeat are the source of truth; this map is empty after a
        # restart and a miss never implies "not running".
        self.node_handle_cache: NodeHandleCache = {}
        # Row ids THIS supervisor is actively spawning (reserved in Phase A,
        # not yet handle-installed). Codex iter-27 P1: the ownerless-row reaper
        # matches ``pid IS NULL`` rows older than the grace window — but a slow
        # legitimate spawn (slow ``_payload_factory`` / Databento resolution /
        # imports during the pre-start awaits, BEFORE the handle is installed)
        # is exactly such a row. Without this set the reaper would reap a live
        # in-flight spawn and reopen its slot, risking a hidden/duplicate node.
        # The reaper excludes any row_id in this set (in-memory, per-supervisor —
        # a row reserved by a CRASHED supervisor is correctly NOT here, so a
        # genuinely-stranded row is still reaped).
        self._in_flight_spawn_rows: set[UUID] = set()
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
        # PR 2 T6 auto-restart authority. ``None`` → auto-restart OFF.
        self._restart_policy = restart_policy
        # PR 2 T6 (prior-review P1): the startup re-scan must also pick up
        # STALE-ACTIVE rows (status running/ready/building whose heartbeat is
        # older than this many seconds), not only ``failed`` rows — the exact
        # scenario the re-scan exists for is a node that died while the
        # supervisor was DOWN, where ``_on_child_exit`` never ran so the row
        # is still ``running``/``ready``/``building`` with a STALE heartbeat
        # (NOT ``failed``). Matches ``HeartbeatMonitor.stale_seconds`` default
        # so the two liveness authorities agree on "dead".
        self._rescan_stale_seconds = rescan_stale_seconds
        # The reap loop's stop_event, stored so the cancellable auto-restart
        # backoff can abandon a pending respawn the moment the supervisor
        # starts draining (Claude iter-1 3-B). A never-set default lets
        # ``reap_once`` / ``_maybe_auto_restart`` be called directly in tests
        # without a loop.
        self._reap_stop_event: asyncio.Event = asyncio.Event()
        # PR 2 F1 — in-flight auto-restart tasks, keyed by deployment_id. The
        # reaper (``_on_child_exit``) is called SYNCHRONOUSLY by the single
        # ``reap_once`` loop; a BACKOFF restart used to ``await`` the
        # cancellable backoff (up to ``BACKOFF_CAP_S`` = 300s) INSIDE
        # ``_on_child_exit``, stalling classification of every OTHER account's
        # exit and defeating per-account failure isolation. The restart
        # (backoff + spawn) is now scheduled as a SEPARATE per-account task so
        # the reaper returns immediately. The handles are tracked here so they
        # can be (a) cancelled on supervisor shutdown and (b) deduped — a
        # second crash for a deployment whose restart is already in flight does
        # NOT start a second task (no two live nodes for one account; the
        # Phase-A unique index is the authoritative backstop regardless).
        self._restart_tasks: dict[UUID, asyncio.Task[None]] = {}
        # PR 2 F2 — pause between transient-dispatch retries inside a restart
        # task (instance attribute so tests can shrink it).
        self._restart_retry_backoff_s: float = _RESTART_TASK_RETRY_BACKOFF_S

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
        gateway_session_key: str | None = None,
        restart_carry: _RestartCarry | None = None,
    ) -> bool:
        """Spawn a new trading subprocess.

        Args:
            gateway_session_key: When provided, the concurrent-startup
                guard in phase A only blocks spawns targeting the SAME
                gateway session (same IB login). Deployments on different
                gateways can start concurrently. When ``None`` (legacy),
                the guard is global — any other starting deployment
                blocks this one (backward compatible).
            restart_carry: Set ONLY by the auto-restart path (PR 2 T6 —
                ``_maybe_auto_restart`` / the startup re-scan). When present,
                Phase A (a) RESETS a terminal (``failed`` / ``stopped``)
                deployment row back to ``starting`` so this DELIBERATE
                respawn is not refused as a stale START, and (b) CARRIES the
                prior row's restart-authority counter forward — applying the
                policy's ``record_failure`` increment under the same row-lock
                that reserves the new slot and stamping the result on the new
                row — so the crash-loop ceiling survives a respawn (the
                counter is keyed to the deployment, not the ephemeral
                per-spawn row). If the increment trips the ceiling, Phase A
                returns ``NOT_STARTABLE`` (paused) without reserving a slot.

        Returns:
            ``True`` on success or idempotent no-op — caller ACKs the
            command. ``False`` on hard failure — caller does NOT ACK so
            the command stays in the PEL for XAUTOCLAIM retry.

        Note: the bool return conflates "process started", "idempotent
            no-op", and several ACK-without-spawn outcomes. The auto-restart
            path needs to know whether a process was GENUINELY (re)started
            (to count the attempt + report a restart honestly), so it uses
            :meth:`spawn_with_outcome` instead and inspects the structured
            outcome — see prior-review P1.
        """
        outcome, _started = await self.spawn_with_outcome(
            deployment_id=deployment_id,
            deployment_slug=deployment_slug,
            payload=payload,
            idempotency_key=idempotency_key,
            gateway_session_key=gateway_session_key,
            restart_carry=restart_carry,
        )
        return outcome

    async def spawn_with_outcome(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict[str, Any],
        idempotency_key: str,  # noqa: ARG002 — reserved for Task 1.14 dedupe path
        gateway_session_key: str | None = None,
        restart_carry: _RestartCarry | None = None,
        _reserved_row_sink: list[UUID] | None = None,
    ) -> tuple[bool, bool]:
        """Like :meth:`spawn` but also report whether a process actually started.

        ``_reserved_row_sink`` (council 2026-05-31, item 3 — internal plumbing):
        an optional caller-supplied list. When Phase A reserves a real slot for
        THIS attempt, the reserved ``LiveNodeProcess.id`` is APPENDED to it so the
        auto-restart caller (:meth:`_attempt_auto_restart`) can thread the OWNED
        row id into :meth:`_refail_stranded_restart` — acting on the row THIS
        attempt created rather than ``ORDER BY started_at DESC LIMIT 1`` (a
        concurrent rescan / operator-retry can insert a newer active row across
        the ``await`` boundaries between Phase A and the transient-failure
        cleanup). The return signature is unchanged (``(ack, process_started)``)
        so the many callers that mock this method are unaffected; a caller that
        doesn't pass a sink keeps the legacy latest-row fallback behaviour.

        Returns a ``(ack, process_started)`` tuple:

        - ``ack`` — the existing :meth:`spawn` bool (caller ACKs on ``True``).
        - ``process_started`` — ``True`` ONLY when this call genuinely reserved
          a fresh slot AND ``process.start()`` succeeded. ``False`` for every
          ACK-without-spawn outcome (``ALREADY_ACTIVE`` idempotent no-op,
          ``NOT_STARTABLE``, ``HALT_ACTIVE`` / ``ACCOUNT_HALT_ACTIVE``,
          payload-factory permanent/transient failure, ``process.start()``
          failure) AND for the transient no-ACK outcomes
          (``BUSY_STOPPING`` / ``CONCURRENT_STARTUP`` / ``NO_DEPLOYMENT`` /
          transient payload-factory error).

        The auto-restart path (prior-review P1) inspects ``process_started``
        so it only counts an attempt against the crash-loop ceiling — and only
        reports ``auto_restart_respawning`` — when a process was REALLY
        (re)started. Otherwise it would tally restarts that never happened.
        """
        row_id = await self._phase_a_reserve_slot(
            deployment_id=deployment_id,
            deployment_slug=deployment_slug,
            gateway_session_key=gateway_session_key,
            restart_carry=restart_carry,
        )
        # Phase A returns either a real UUID (newly reserved slot) or a
        # ``_PhaseAOutcome`` sentinel. ``ALREADY_ACTIVE`` and
        # ``NOT_STARTABLE`` are the ACK (do-not-retry) outcomes; the rest
        # leave the command in the PEL:
        #   - ``ALREADY_ACTIVE``: idempotent success → ACK.
        #   - ``NOT_STARTABLE`` (review P2): a stale START for a terminal
        #     deployment the operator abandoned → ACK-and-drop so it can't
        #     resurrect a live node.
        #   - ``BUSY_STOPPING``: stop is still draining; do NOT ACK so
        #     the command stays in the PEL for XAUTOCLAIM redelivery.
        #   - ``NO_DEPLOYMENT``: deployment row was deleted between the
        #     idempotency check and the slot reservation; do NOT ACK.
        #   - ``CONCURRENT_STARTUP`` (Bug X1): another deployment is
        #     still initializing on the same gateway; do NOT ACK so the
        #     consumer group picks it up via ``XAUTOCLAIM`` once the
        #     first deployment reaches ``running``.
        #   - ``OPERATOR_STOPPED`` (FIX 2): an operator /stop won the race
        #     against a respawn reservation (its durable ``stop_requested_at``
        #     intent was set, observed atomically in the slot-reservation
        #     transaction). ACK-without-spawn — a DELIBERATE terminal
        #     suppression in the same family as a halt-suppressed restart: the
        #     auto-restart caller must NOT retry it and must NOT count it as a
        #     transient failure (resurrecting an operator-stopped node would let
        #     it submit fresh real-money orders for a stopped account).
        if row_id in (
            _PhaseAOutcome.ALREADY_ACTIVE,
            _PhaseAOutcome.NOT_STARTABLE,
            _PhaseAOutcome.OPERATOR_STOPPED,
        ):
            # ACK without spawning a process: idempotent no-op, a terminal /
            # paused deployment Phase A refused to (re)start, or an operator /stop
            # that won the race against the respawn reservation.
            return True, False
        if isinstance(row_id, _PhaseAOutcome):
            # Transient no-ACK outcomes (BUSY_STOPPING / CONCURRENT_STARTUP /
            # NO_DEPLOYMENT). No process started; do NOT count a restart.
            return False, False

        # row_id is a real UUID from here on.
        assert isinstance(row_id, UUID)

        # Item 3 (own-by-id): publish the reserved row id to the caller's sink so
        # a transient-failure cleanup can target THIS attempt's row by id (never
        # "the latest row" — a concurrent rescan can insert a newer active row
        # across the await boundaries below).
        if _reserved_row_sink is not None:
            _reserved_row_sink.append(row_id)

        # INVARIANT 3 (council 2026-06-01 follow-up — CANCELLATION SAFETY, full
        # span; Codex P2 fleet_router.py:764). Phase A has now COMMITTED the
        # reserved ``starting`` row (``row_id``). The reserved→handle-install
        # window — the halt re-checks, the account-halt lookups, the
        # ``await self._payload_factory(...)``, the pre-start stop-intent gate, and
        # ``process.start()`` — is delegated to :meth:`_phase_b_spawn_guarded` so
        # the ENTIRE span runs inside ONE cancellation-safe wrapper. A cancellation
        # (``asyncio.CancelledError`` — a ``BaseException``, NOT caught by the
        # payload-factory's ``except (ValueError, ...)`` / ``except Exception``)
        # landing in ANY of those awaits before the live handle is installed
        # terminalizes the reserved row (so it never orphans the active
        # unique-index slot — the F2 class of bug) before re-raising. The original
        # cleanup wrapped only ``process.start()`` + handle-install; this covers
        # the whole span.
        return await self._phase_b_spawn_guarded(
            row_id=row_id,
            deployment_id=deployment_id,
            deployment_slug=deployment_slug,
            payload=payload,
        )

    async def _phase_b_spawn_guarded(
        self,
        *,
        row_id: UUID,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict[str, Any],
    ) -> tuple[bool, bool]:
        """Phase B + Phase C of a spawn, run inside ONE cancellation-safe wrapper
        (council 2026-06-01 follow-up — INVARIANT 3, full reserved→handle-install
        span).

        Phase A (:meth:`_phase_a_reserve_slot`) has already COMMITTED the reserved
        ``starting`` row ``row_id``. This method runs every step from there through
        the live-handle install: the fleet/account halt re-checks, the
        ``await self._payload_factory(...)``, the pre-start operator-stop-intent
        gate, ``process.start()``, and the handle install (Phase C records the
        pid). EVERY one of those is an ``await`` point at which the spawn task can
        be CANCELLED by a shutdown ``cancel_restart_tasks()``.

        ``asyncio.CancelledError`` is a ``BaseException`` (NOT ``Exception``), so
        the payload-factory's ``except (ValueError, ...)`` / ``except Exception``
        clauses never catch it. The OUTER ``except asyncio.CancelledError`` here is
        the only thing that does: if the live handle was NOT yet installed
        (``handle_installed is False``), it TERMINALIZES the reserved row (so the
        ``starting`` pid=NULL row never orphans the deployment's active
        unique-index slot — the F2 class of bug, now covered for the FULL span,
        not just ``process.start()``) before re-raising. Once the handle IS
        installed the running node owns the row (the reap loop reaps it on
        shutdown), so a later cancellation must NOT terminalize.

        Returns the same ``(ack, process_started)`` tuple as the enclosing
        :meth:`spawn_with_outcome`. The many ``return (...)`` early-exits inside
        (halt / account-halt / payload-factory / pre-start gate) are NORMAL returns
        that pass through the try untouched — only an actual ``CancelledError``
        escaping an await is handled by the wrapper."""
        # Resolve the deployment's account_id once for the account-scoped logs +
        # the account-halt re-checks below (kept local to this method).
        deployment_account_id: str | None = None
        # Mark this row as an in-flight spawn so the ownerless-row reaper does
        # NOT reap it during the slow pre-start awaits (before the handle is
        # installed). Discarded in the ``finally`` once the spawn ends — by then
        # either the live handle covers the exclusion (running node) or the row
        # is terminal (failed/stopped). Codex iter-27 P1.
        self._in_flight_spawn_rows.add(row_id)
        handle_installed = False
        process: mp.process.BaseProcess | None = None
        try:
            # Phase B: halt-flag re-check + process.start().
            halt_set = await self._redis.exists(_HALT_KEY)
            if halt_set:
                log.warning(
                    "spawn_blocked_by_halt",
                    extra={
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "note": (
                            "fleet halt latch set at Phase B re-check — "
                            "account_id not yet resolved at this site (pre-lookup)"
                        ),
                    },
                )
                await self._mark_failed(
                    row_id=row_id,
                    reason="blocked by halt flag",
                    failure_kind=FailureKind.HALT_ACTIVE,
                )
                return True, False  # ACK — no retry until /resume; no process started

            # Account-scoped halt latch re-check (PR 1 T8 / Codex iter 1 P2-1).
            # ``/api/v1/live/drain/{account_id}`` writes ``account_halt_key``
            # to block the drained sub-account. A queued START — or any
            # START arriving after drain — must NOT spawn the subprocess.
            # Mirrors the fleet halt-re-check above but is keyed by the
            # deployment's ``account_id`` so sibling accounts under the
            # same TWS login keep running. Council 2026-05-29 obj #11:
            # halt latch is per ``account_id``, NOT per ``ib_login_key``.

            # Codex iter 6 P1-1: bound the account-halt lookup. If
            # ``_account_id_for`` (DB) or ``self._redis.exists`` (Redis) raise
            # transiently, the exception would escape AFTER phase A already
            # committed the ``starting`` row. On redelivery, ``_phase_a_reserve_slot``
            # sees the row as active and ACKs without spawning — the deployment
            # gets stuck until watchdog timeout. Treat lookup failures the same
            # way as payload-factory failures (the existing transient-cleanup
            # path below covers this class of error).
            try:
                deployment_account_id = await self._account_id_for(deployment_slug)
                account_halt_set = False
                if deployment_account_id:
                    account_halt_set = bool(
                        await self._redis.exists(account_halt_key(deployment_account_id))
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "spawn_account_halt_lookup_failed",
                    extra={
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "exc_type": type(exc).__name__,
                    },
                )
                # Codex iter 10 P2: ``_mark_failed`` uses the same DB path
                # that just blipped. If it raises here, the exception escapes
                # AFTER Phase A committed the ``starting`` row; redelivery
                # then sees the row as active and ACKs without spawning,
                # leaving the deployment stuck until the watchdog recovers
                # it. Make cleanup best-effort (matches the payload-factory
                # transient-cleanup pattern lower in this method).
                try:
                    await self._mark_failed(
                        row_id=row_id,
                        reason=f"account-halt lookup failed: {exc!r}",
                        failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "spawn_account_halt_lookup_cleanup_failed",
                        extra={
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                        },
                    )
                # ACK — let the operator retry via /start after Redis/DB
                # recovers; no process started.
                return True, False

            if deployment_account_id and account_halt_set:
                log.warning(
                    "spawn_blocked_by_account_halt",
                    extra={
                        "deployment_id": str(deployment_id),
                        "account_id": deployment_account_id,
                    },
                )
                await self._mark_failed(
                    row_id=row_id,
                    reason=(
                        f"blocked by account halt latch for account_id={deployment_account_id}"
                    ),
                    failure_kind=FailureKind.ACCOUNT_HALT_ACTIVE,
                )
                return (
                    True,
                    False,
                )  # ACK — no retry until the drain latch clears; no process started

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
                except (
                    ValueError,
                    ImportError,
                    ModuleNotFoundError,
                    FileNotFoundError,
                    AttributeError,
                ) as exc:
                    # Codex iter5 P2: distinguish permanent errors from
                    # transient ones.
                    #
                    # Permanent failures are OPERATOR CONFIG BUGS that
                    # retrying won't fix — the payload factory raises
                    # ``ValueError`` for our paper/live safety guards,
                    # ``ImportError`` / ``ModuleNotFoundError`` /
                    # ``FileNotFoundError`` when the strategy file
                    # doesn't exist at ``strategy.file_path``, and
                    # ``AttributeError`` when the strategy class isn't
                    # found in the module. All of these require the
                    # operator to fix the deployment row or strategy
                    # file — XAUTOCLAIM retry would just spin.
                    #
                    # Mark failed + ACK so the command is removed from
                    # the PEL.
                    #
                    # Task 9 (live-path-wiring-registry): dispatch on
                    # resolver-specific subtypes so the endpoint can
                    # return distinct HTTP error codes (REGISTRY_MISS vs
                    # REGISTRY_INCOMPLETE vs UNSUPPORTED_ASSET_CLASS vs
                    # AMBIGUOUS_REGISTRY vs the generic
                    # SPAWN_FAILED_PERMANENT fallback). Lazy import to
                    # avoid pulling the resolver into fleet_router's
                    # startup cost.
                    from msai.services.nautilus.security_master.live_resolver import (
                        AmbiguousRegistryError,
                        LiveResolverError,
                        RegistryIncompleteError,
                        RegistryMissError,
                        UnsupportedAssetClassError,
                    )

                    if isinstance(exc, RegistryMissError):
                        kind = FailureKind.REGISTRY_MISS
                    elif isinstance(exc, RegistryIncompleteError):
                        kind = FailureKind.REGISTRY_INCOMPLETE
                    elif isinstance(exc, UnsupportedAssetClassError):
                        kind = FailureKind.UNSUPPORTED_ASSET_CLASS
                    elif isinstance(exc, AmbiguousRegistryError):
                        kind = FailureKind.AMBIGUOUS_REGISTRY
                    else:
                        kind = FailureKind.SPAWN_FAILED_PERMANENT

                    # For resolver-class errors, persist the structured
                    # JSON envelope as reason so the EndpointOutcome
                    # factory (Task 12) can parse back into {code,
                    # message, details}. For other errors, preserve the
                    # existing "payload factory failed (permanent): "
                    # prefix.
                    if isinstance(exc, LiveResolverError):
                        reason = exc.to_error_message()
                    else:
                        reason = f"payload factory failed (permanent): {exc}"

                    log.exception(
                        "spawn_payload_factory_failed_permanent",
                        extra={
                            "account_id": deployment_account_id,
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                            "exception_type": type(exc).__name__,
                            "failure_kind": kind.value,
                        },
                    )
                    await self._mark_failed(
                        row_id=row_id,
                        reason=reason,
                        failure_kind=kind,
                    )
                    return True, False  # ACK — permanent config error; no process started
                except Exception as exc:  # noqa: BLE001
                    # TRANSIENT failure path (Codex iter5 P2).
                    #
                    # Everything else is treated as transient: SQLAlchemy
                    # ``OperationalError`` / ``DBAPIError`` when Postgres
                    # is briefly down, Redis errors, network timeouts,
                    # interpreter hiccups during module imports, etc.
                    # Retrying via XAUTOCLAIM once the dependency
                    # recovers is the right behavior — losing a
                    # ``/start`` because Postgres had a 3-second blip
                    # would be a user-visible outage.
                    #
                    # We do NOT call ``_mark_failed`` here because the
                    # row is still in ``starting`` state and a fresh
                    # XAUTOCLAIM attempt needs the row to remain
                    # available. Returning ``False`` keeps the command
                    # in the PEL for redelivery.
                    log.exception(
                        "spawn_payload_factory_failed_transient",
                        extra={
                            "account_id": deployment_account_id,
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                            "exception_type": type(exc).__name__,
                            "note": (
                                "treating as transient — command stays in PEL for XAUTOCLAIM retry"
                            ),
                        },
                    )
                    # Release the reserved row so the next retry can
                    # re-reserve it. We flip to ``failed`` with a
                    # transient failure_kind that the retry path
                    # ignores when deciding whether to spawn.
                    #
                    # Codex iter6 P1: the most likely transient failure
                    # is a Postgres outage — the same outage that's
                    # blocking ``self._mark_failed``. Wrap in a
                    # try/except so a second DB failure doesn't escape
                    # as an unhandled exception. If _mark_failed ALSO
                    # fails, the row stays in ``starting``. The
                    # supervisor's ``startup_hard_timeout_s`` watchdog
                    # (default 1800s) is the backstop — it kills stale
                    # starting rows via a separate DB query path that
                    # may succeed even if _mark_failed is flaky. Log
                    # critical so operators notice.
                    try:
                        await self._mark_failed(
                            row_id=row_id,
                            reason=f"payload factory failed (transient): {exc}",
                            failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
                        )
                    except Exception:  # noqa: BLE001
                        log.critical(
                            "spawn_transient_cleanup_mark_failed_also_failed",
                            extra={
                                "account_id": deployment_account_id,
                                "deployment_id": str(deployment_id),
                                "deployment_slug": deployment_slug,
                                "row_id": str(row_id),
                                "note": (
                                    "row may be stuck in 'starting' until the "
                                    "spawn watchdog clears it via its separate "
                                    "DB path — command stays in PEL for retry"
                                ),
                            },
                        )
                    return False, False  # NO ACK — retry via PEL; no process started
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
                        "account_id": deployment_account_id,
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
                return True, False  # ACK — no retry until /resume; no process started

            # F3 fix (Codex iter 2 P1 / silent-failure-hunter F4): account-halt
            # re-check race. The account-halt check above (phase B, before the
            # payload factory) closes one race but leaves another: the payload
            # factory await is seconds of wall clock (DB reads, Databento
            # resolution, module imports). ``/api/v1/live/drain/{account_id}``
            # firing DURING that await would set ``account_halt_key(account_id)``
            # but the earlier check already passed — the subprocess would
            # spawn under an active drain.
            #
            # Mirror the fleet-halt re-check pattern above. ``account_id`` is
            # already cached from the phase-B check via ``_account_id_for``
            # so this is a single Redis EXISTS, no extra DB hit. Mark the row
            # ``ACCOUNT_HALT_ACTIVE`` (NOT the fleet-wide ``HALT_ACTIVE`` —
            # F1 + F3 are intentionally distinct kinds) and ACK.
            if deployment_account_id:
                # Codex iter 7 P2-3: wrap the post-payload Redis EXISTS in the
                # same try/except as the pre-payload account-halt lookup. A
                # transient Redis hiccup here would otherwise escape after Phase
                # A has committed the ``starting`` row, leaving the deployment
                # stuck on redelivery (active row → ACK without spawn).
                try:
                    account_halt_set_again = bool(
                        await self._redis.exists(account_halt_key(deployment_account_id))
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "spawn_account_halt_post_payload_lookup_failed",
                        extra={
                            "account_id": deployment_account_id,
                            "deployment_id": str(deployment_id),
                            "exc_type": type(exc).__name__,
                        },
                    )
                    # Codex iter 10 P2 (applied symmetrically): cleanup is
                    # best-effort so a paired DB blip doesn't leak the row.
                    try:
                        await self._mark_failed(
                            row_id=row_id,
                            reason=f"post-payload account-halt lookup failed: {exc!r}",
                            failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "spawn_account_halt_post_payload_cleanup_failed",
                            extra={
                                "account_id": deployment_account_id,
                                "deployment_id": str(deployment_id),
                            },
                        )
                    return True, False  # ACK; no process started
                if account_halt_set_again:
                    log.warning(
                        "spawn_blocked_by_account_halt_post_payload_factory",
                        extra={
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                            "account_id": deployment_account_id,
                            "note": (
                                "account drain latch raised during payload factory "
                                "await — catching at second check, no subprocess spawned"
                            ),
                        },
                    )
                    await self._mark_failed(
                        row_id=row_id,
                        reason=(
                            f"blocked by account halt latch (post-payload-factory "
                            f"recheck) for account_id={deployment_account_id}"
                        ),
                        failure_kind=FailureKind.ACCOUNT_HALT_ACTIVE,
                    )
                    # ACK — no retry until the drain latch clears; no process started.
                    return True, False

            # Council 2026-06-01 (item 2 — PRE-START STOP-INTENT GATE, the chairman's
            # blocking objection). After Phase A committed the ``starting`` row but
            # IMMEDIATELY BEFORE ``process.start()``, do a FINAL re-read of the latest
            # node row's durable ``stop_requested_at`` intent. This closes the
            # post-Phase-A-commit → pre-process.start window that NO other gate covers:
            # a plain /stop sets no halt latch (so the halt re-checks above miss it),
            # and the Phase-A OPERATOR_STOPPED re-check only runs for a RESPAWN
            # (``restart_carry is not None``) and only at reservation time — a /stop
            # that lands in THIS window, after the reservation, would otherwise spawn
            # a live node for a stopped account. The halt re-checks above already
            # cover the fleet/account latch for this window; this adds the plain-/stop
            # intent. If the intent is set → do NOT start a process; terminalize the
            # just-reserved row (it leaves the active set) and ACK without spawning
            # (the OPERATOR_STOPPED family — a deliberate terminal suppression the
            # auto-restart caller must NOT retry).
            try:
                latest_stop_intent = await self._latest_stop_requested_at(deployment_id)
            except Exception:  # noqa: BLE001 — fail-closed: unreadable intent == stopped
                log.exception(
                    "spawn_prestart_stop_intent_check_failed_fail_closed",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                    },
                )
                latest_stop_intent = datetime.now(UTC)
            if latest_stop_intent is not None:
                log.warning(
                    "spawn_blocked_by_operator_stop_prestart_gate",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "note": (
                            "operator /stop stamped stop_requested_at after Phase A "
                            "reserved the slot but before process.start() — aborting the "
                            "spawn + terminalizing the reserved row (no live node for a "
                            "stopped account)"
                        ),
                    },
                )
                await self._terminalize_reserved_row(
                    row_id=row_id,
                    deployment_id=deployment_id,
                    reason="operator /stop observed at the pre-start stop-intent gate",
                    failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
                )
                # ACK without spawning — a DELIBERATE terminal suppression
                # (OPERATOR_STOPPED family). No process started.
                return True, False

            # ``process.start()`` — the irreversible side effect. A
            # ``CancelledError`` here (or in any await ABOVE in this guarded span)
            # propagates to the OUTER ``except asyncio.CancelledError`` below, which
            # terminalizes the reserved row (``handle_installed`` is still False).
            # A NON-cancellation start failure is a permanent config/OS error: mark
            # the row failed + ACK (the inner ``except Exception`` does NOT catch
            # ``CancelledError`` — it is a ``BaseException``).
            try:
                process = self._spawn_ctx.Process(  # type: ignore[attr-defined]  # BaseContext typed too wide; concrete ctxs (spawn/fork) expose Process
                    target=self._spawn_target,
                    args=spawn_args,
                )
                process.start()
            except Exception as exc:  # noqa: BLE001 — we want to catch any start() failure
                log.exception(
                    "spawn_process_start_failed",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                    },
                )
                await self._mark_failed(
                    row_id=row_id,
                    reason=f"process.start() failed: {exc}",
                    failure_kind=FailureKind.SPAWN_FAILED_PERMANENT,
                )
                return True, False  # ACK — start() failed; no process started

            # Cache BOTH the live handle AND the row id this spawn reserved
            # (council 2026-05-31 / F3): the reaper threads ``owned_row_id`` from
            # here so it classifies the row it OWNS, never "the latest row". Once
            # this handle is installed the row is OWNED by the reaper / Phase C —
            # a later cancellation no longer orphans it (the reap loop reaps the
            # live process on shutdown), so ``handle_installed`` flips True and the
            # outer cancellation handler stops terminalizing.
            self.node_handle_cache[deployment_id] = _CachedNode(proc=process, owned_row_id=row_id)
            handle_installed = True

            from msai.services.observability.trading_metrics import DEPLOYMENTS_STARTED

            DEPLOYMENTS_STARTED.inc()

            # Phase C: record the real pid on the row — AND close the
            # post-pre-start-gate /stop race (Codex iter-26 P1). The pre-start
            # gate above read ``stop_requested_at`` BEFORE ``process.start()``; a
            # /stop that stamps it in the window [pre-start read → here] takes the
            # ``stop_no_pid`` branch of :meth:`stop`, which could NOT SIGTERM a
            # child whose pid was not yet recorded — so without this re-check the
            # child would keep trading a stopped account. Re-read the intent under
            # the SAME ``live_node_processes`` row lock the /stop uses to stamp it:
            # the two transactions serialize, so whichever commits SECOND observes
            # the other (we see ``stop_requested_at``, or /stop sees our pid and
            # SIGTERMs). If we observe the intent, terminalize the row instead of
            # recording the pid, then kill the child we just started.
            stop_raced = False
            try:
                async with self._db() as session, session.begin():
                    # ``with_for_update=True`` is LOAD-BEARING (pr-toolkit iter-27 P2):
                    # a PLAIN ``get`` under READ COMMITTED would NOT serialize against
                    # the /stop handler's own ``SELECT ... FOR UPDATE`` stamp, leaving a
                    # residual window (Phase C reads NULL → /stop commits the intent +
                    # ACKs its no-pid branch without a SIGTERM → Phase C blind-writes the
                    # pid → child trades a stopped account). Locking the node row here
                    # makes the two transactions serialize: we observe the committed
                    # ``stop_requested_at``, OR /stop blocks until we commit the pid and
                    # then SIGTERMs it via ``row.pid``. This is a SINGLE node-row lock with
                    # NO deployment lock held → no deployment-first lock-order cycle.
                    row = await session.get(LiveNodeProcess, row_id, with_for_update=True)
                    if row is not None:
                        # Always record the real pid so the row reflects the live
                        # child (the reaper / a redelivered /stop can find it).
                        row.pid = process.pid
                        # Codex iter-27 P2: if a /stop raced in during start/
                        # handle-install the row already carries stop_requested_at
                        # (and the /stop set status="stopping"). Do NOT overwrite the
                        # status or drop the handle — leaving the row active
                        # ("stopping") keeps the slot occupied (no duplicate START) and
                        # keeps the handle so the NORMAL reaper terminalizes the row
                        # when the SIGTERM'd child actually EXITS (it already suppresses
                        # auto-restart when stop_requested_at is set). ``last_stopped_at``
                        # lives on LiveDeployment, not LiveNodeProcess — the reaper
                        # syncs it on the child's exit.
                        stop_raced = row.stop_requested_at is not None
            except Exception:
                # Don't abort — the handle cache still has the live process
                # so reap_loop / stop still work, and Task 1.8's
                # subprocess self-write will populate pid as a fallback. The
                # no-pid STOP branch's no-ACK redelivery (Codex iter-26 backstop)
                # finds the live pid via the handle cache and SIGTERMs it if a
                # /stop raced AND this DB write failed.
                log.exception(
                    "spawn_pid_update_failed",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "pid": process.pid,
                    },
                )

            if stop_raced:
                # A /stop won the race during process.start()/handle-install (its
                # no-pid branch ACKed but could not SIGTERM a pid that was not yet
                # recorded). SIGTERM the child we just started so it stops trading
                # the stopped account. We KEEP the handle (Codex iter-27 P2): the
                # normal reap loop observes the child's exit and terminalizes the
                # row (stop_requested_at suppresses any auto-restart), and SIGKILL-
                # escalates if the child ignores SIGTERM. Terminalizing here instead
                # would reopen the active slot before the child is confirmed dead.
                log.warning(
                    "spawn_terminating_child_after_stop_raced_prestart_gate",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "pid": process.pid,
                        "note": (
                            "operator /stop stamped stop_requested_at in the "
                            "[pre-start gate → pid record] window; SIGTERM the child "
                            "we just started — the reaper terminalizes the row on exit"
                        ),
                    },
                )
                with contextlib.suppress(Exception):
                    process.terminate()
                from msai.services.observability.trading_metrics import DEPLOYMENTS_STOPPED

                DEPLOYMENTS_STOPPED.inc()
                return True, False  # ACK — child SIGTERM'd; reaper finishes terminalization

            return True, True  # ACK + a process genuinely started
        except asyncio.CancelledError:
            # INVARIANT 3 (council 2026-06-01 follow-up — FULL-SPAN CANCELLATION
            # SAFETY). A shutdown ``cancel_restart_tasks()`` cancelled the spawn
            # task at SOME await in the reserved→handle-install window — the halt
            # re-checks, the account-halt lookups, the ``await
            # self._payload_factory(...)`` (the gap Codex flagged at line 764), the
            # pre-start stop-intent gate, or ``process.start()`` itself. If the live
            # handle was NOT installed, the reserved ``starting`` pid=NULL row would
            # orphan the deployment's active unique-index slot forever. Terminalize
            # it (deployment→node lock order) so it leaves the active set, then
            # re-raise so the task ends cancelled. Once ``handle_installed`` is True
            # the running node owns the row (the reap loop reaps it on shutdown), so
            # we do NOT terminalize.
            if not handle_installed:
                log.warning(
                    "spawn_cancelled_in_reserved_prestart_window",
                    extra={
                        "account_id": deployment_account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "note": (
                            "spawn task cancelled before the live handle was installed "
                            "(any await in the reserved→handle-install span — incl. the "
                            "payload-factory); terminalizing the reserved row so it "
                            "leaves the active set"
                        ),
                    },
                )
                await self._terminalize_reserved_row(
                    row_id=row_id,
                    deployment_id=deployment_id,
                    reason="spawn cancelled in the reserved→handle-install window",
                    failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
                )
            raise
        finally:
            # Spawn finished (success / failure / cancellation) — drop the
            # in-flight marker. A running node is now covered by its live handle
            # in ``node_handle_cache``; a failed/stopped/cancelled row is terminal
            # and no longer matches the reaper's active-status scan. Codex iter-27 P1.
            self._in_flight_spawn_rows.discard(row_id)

    async def _account_id_for(self, deployment_slug: str) -> str | None:
        """Read the ``account_id`` for ``deployment_slug``.

        Used by phase B of :meth:`spawn` to re-check the account-scoped
        halt latch (PR 1 T8 / Codex iter 1 P2-1). Returns ``None`` when
        the deployment row is gone (which phase A would have caught
        with :attr:`_PhaseAOutcome.NO_DEPLOYMENT` before phase B runs;
        the ``None`` return is purely defensive against a races where
        the row was deleted between phases).
        """
        async with self._db() as session:
            row = (
                await session.execute(
                    select(LiveDeployment.account_id).where(
                        LiveDeployment.deployment_slug == deployment_slug,
                    )
                )
            ).scalar_one_or_none()
            return row

    async def _latest_stop_requested_at(self, deployment_id: UUID) -> datetime | None:
        """Read the latest node row's durable ``stop_requested_at`` intent
        (council 2026-06-01, item 2 — pre-start stop-intent gate).

        A lightweight (no row-lock) read used by the pre-``process.start()`` gate
        in :meth:`spawn_with_outcome`. It does NOT need ``FOR UPDATE``: the gate's
        job is to catch a /stop that ALREADY committed its durable intent before
        we reach ``process.start()`` — and a /stop that commits AFTER this read
        but before the process starts is still caught by the reaper's
        ``stop_requested_at`` suppression (the reaper classifies the just-started
        node's eventual exit under its own ``FOR UPDATE`` and suppresses the
        auto-restart). Reads the LATEST row by ``started_at`` — the SAME row the
        /stop writers stamp and the reaper classifies."""
        async with self._db() as session:
            return (
                await session.execute(
                    select(LiveNodeProcess.stop_requested_at)
                    .where(LiveNodeProcess.deployment_id == deployment_id)
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def _phase_a_reserve_slot(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        gateway_session_key: str | None = None,
        restart_carry: _RestartCarry | None = None,
    ) -> UUID | _PhaseAOutcome:
        """Run phase A in a single transaction. Return the new row id,
        or one of the :class:`_PhaseAOutcome` sentinels.

        Args:
            gateway_session_key: When provided, the concurrent-startup
                guard only blocks spawns on the SAME gateway session.
                When ``None``, the guard is global (backward compat).
                Also persisted on the ``LiveNodeProcess`` row so the
                guard can query it on subsequent spawns.
            restart_carry: Set ONLY by the auto-restart path (PR 2 T6).
                When present this is a DELIBERATE respawn of a crashed node,
                so Phase A (a) does NOT treat a terminal (``failed`` /
                ``stopped``) deployment as a stale START — instead it RESETS
                the deployment row back to ``starting`` so ``_mark_running``
                can later forward-sync it; and (b) CARRIES the prior row's
                restart-authority counter forward — applying the policy's
                ``record_failure`` increment to the carried prior values
                UNDER the same deployment row-lock that reserves the new
                slot, and stamping the result on the new ``LiveNodeProcess``
                row (so the crash-loop ceiling is keyed to the logical
                deployment and survives the per-spawn row recreate —
                prior-review P1). If the increment trips the ceiling the
                new row is created ``auto_restart_paused=True`` and the slot
                is still reserved (so the failed-state row reflects the
                pause) — but the caller has already short-circuited PAUSED
                before getting here, so in practice ``record_failure`` only
                trips the latch on the increment that REACHES the ceiling.
        """
        async with self._db() as session, session.begin():
            # PR 2 F1 (review P1) — serialise same-gateway slot reservations.
            #
            # T4 introduced per-account command consumers that run CONCURRENTLY.
            # Two START commands for DIFFERENT deployments that share one
            # ``gateway_session_key`` can now run ``_phase_a_reserve_slot``
            # concurrently. Each transaction ``SELECT ... FOR UPDATE``s only ITS
            # OWN ``live_deployments`` row, then reads the CONCURRENT_STARTUP
            # guard ("is any OTHER deployment starting on this gateway?") BEFORE
            # inserting its own ``starting`` row. Without serialisation both
            # transactions can observe no-other-starting, both insert, and both
            # launch a TradingNode against the SAME IB Gateway → client_id
            # collision / silent disconnect (Nautilus gotcha #3). The
            # per-deployment row lock does NOT serialise two DIFFERENT
            # deployments on one gateway, and the guard's check+insert is not
            # atomic across them.
            #
            # Take a Postgres TRANSACTION-LEVEL advisory lock keyed by the
            # effective gateway, FIRST — before the deployment ``FOR UPDATE``
            # and before the guard's check+insert — so the check+insert is
            # atomic per gateway. ``pg_advisory_xact_lock`` auto-releases at
            # tx commit/rollback (no leak on the failure paths below).
            #
            # Lock-key SCOPE mirrors the guard's scope so the lock and the guard
            # agree on what "same gateway" means:
            #   - ``gateway_session_key`` provided → key by THAT gateway (the
            #     guard filters ``gateway_session_key == ...``). Different
            #     gateways → different lock keys → NO cross-gateway contention
            #     (the multi-login enabler — concurrent starts on distinct IB
            #     logins are intended).
            #   - ``None`` (legacy) → the guard is GLOBAL (no gateway predicate),
            #     so the lock is global too (the fixed sentinel) — every legacy
            #     reservation serialises through one key.
            #
            # Lock ORDERING (no deadlock): the advisory lock is acquired FIRST,
            # CONSISTENTLY, in every Phase-A transaction, strictly before the
            # per-deployment ``FOR UPDATE`` row lock. There is no path that holds
            # the row lock while waiting for the advisory lock, so the two locks
            # can never form a cycle. Two concurrent same-gateway reservations:
            # the loser blocks on the advisory lock; the winner takes its row
            # lock, runs the guard + insert, commits (releasing BOTH); the loser
            # then proceeds and now SEES the winner's ``starting`` row → returns
            # CONCURRENT_STARTUP. Exactly one node per gateway.
            gateway_lock_key = (
                gateway_session_key if gateway_session_key is not None else _GLOBAL_GATEWAY_LOCK_KEY
            )
            await session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(gateway_lock_key, 0)))
            )

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
                    extra={
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                    },
                )
                return _PhaseAOutcome.NO_DEPLOYMENT

            # Account-scoped logging (PR 2 T10): the deployment row is loaded
            # here so its ``account_id`` is in scope at no extra DB cost — bind
            # it once and thread it through every Phase A supervision log below.
            account_id = (deployment.account_id or "").strip() or None

            # Council 2026-06-01 follow-up (INVARIANT 1 — OPERATOR-TERMINAL
            # DEPLOYMENT GATE, the queued-START gap). An operator ``/stop`` of a
            # deployment whose START was QUEUED-but-unconsumed (deployment
            # ``starting`` / ``building``, NO node row yet — Phase A had not run,
            # so there was no node row to carry ``stop_requested_at``) marks the
            # ``LiveDeployment`` row itself operator-terminal (``status='stopped'``)
            # under THIS same ``FOR UPDATE`` lock (see ``FleetRouter.stop`` /
            # ``live_stop`` no-active-row PRE-ACTIVE branch). The
            # ``stop_requested_at`` node-row intent CANNOT cover this case — there
            # is no node row to stamp.
            #
            # ``stopped`` is therefore OPERATOR-TERMINAL and is honoured on EVERY
            # spawn path, BEFORE the respawn stop-intent re-read below and BEFORE
            # the stale-START / restart_carry logic:
            #   - the queued INITIAL START (``restart_carry is None``) — ABORT so a
            #     command the operator stopped while still starting can never spawn
            #     a live node (the gap this gate closes);
            #   - a redelivered START (XAUTOCLAIM / PEL recovery) — same;
            #   - an AUTO-RESTART respawn (``restart_carry is not None``: reaper /
            #     rescan / give-up retry) — ABORT too. ``stopped`` is operator-
            #     terminal, NOT a recoverable crash: restart_carry resurrects a
            #     ``failed`` deployment, never a ``stopped`` one (resurrecting it
            #     would let a node submit fresh real-money orders for a stopped
            #     account). This is the ``stopped`` (abort) vs ``failed``
            #     (recoverable) distinction the lock-order proof block mandates.
            # No new lock edge: this read is on the ALREADY-LOCKED deployment row
            # (no node row touched), so it cannot deadlock.
            if deployment.status == "stopped":
                log.warning(
                    "spawn_aborted_operator_stopped_deployment",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "is_respawn": restart_carry is not None,
                        "note": (
                            "deployment is operator-terminal (stopped); Phase A "
                            "ABORTS the spawn on every path (initial / redelivered "
                            "START + restart_carry respawn) — never resurrect a "
                            "node the operator stopped"
                        ),
                    },
                )
                return _PhaseAOutcome.OPERATOR_STOPPED

            # FIX 2 (Codex P1 #2 / pr-toolkit P2) — ATOMIC operator-stop backstop.
            #
            # Phase A is the SINGLE chokepoint EVERY respawn passes through (the
            # reaper-driven ``_attempt_auto_restart``, the periodic
            # ``rescan_for_restart``, and the give-up retry all reach here with
            # ``restart_carry is not None``). The pre/post-backoff
            # ``_operator_stop_requested`` gates in ``_attempt_auto_restart`` are
            # fast-path immediacy checks that COMMIT their own transaction and
            # then hand off to ``spawn_with_outcome`` → here in a SEPARATE
            # transaction. A plain ``/stop`` that lands in that gap stamps the
            # failed row's ``stop_requested_at`` + returns "stopped" but does NOT
            # abort the in-flight respawn — so without this re-check Phase A would
            # insert a fresh live ``starting`` row for a stopped account, and
            # nothing would reap it (a plain /stop publishes no STOP; the rescan
            # only acts on ``failed`` rows). R2 would trade for a stopped account.
            #
            # Make the operator-stop check ATOMIC with the reservation: we already
            # hold the ``live_deployments FOR UPDATE`` lock in THIS transaction;
            # RE-READ the latest ``live_node_processes`` row's ``stop_requested_at``
            # for this deployment in the SAME transaction, and if it is set ABORT —
            # do NOT insert the new row, return ``OPERATOR_STOPPED``. This is the
            # SAME atomicity the gateway advisory lock gives the slot reservation,
            # and it covers EVERY respawn path through Phase A at once.
            #
            # Scoped to RESPAWNS (``restart_carry is not None``): a fresh operator
            # ``/start`` carries no ``restart_carry`` and must never be suppressed
            # by a historical stop intent on a prior, since-superseded node row.
            #
            # Lock ordering (no new deadlock): Phase A already acquires
            # advisory(gateway) → ``live_deployments FOR UPDATE``; this added read
            # is on the ``live_node_processes`` row in the SAME transaction, taken
            # AFTER both. ``/stop`` (the only concurrent writer of
            # ``stop_requested_at``) stamps the node row under its OWN ``FOR
            # UPDATE`` then COMMITS, holding no ``live_deployments`` lock across
            # the boundary — so it cannot form a cycle with this transaction. The
            # reaper's ``_operator_stop_requested`` reads the node row under ``FOR
            # UPDATE`` in a self-contained transaction it commits before calling
            # ``spawn_with_outcome``; it never holds the deployment lock while
            # waiting for the node-row lock either. No lock cycle is possible.
            if restart_carry is not None:
                respawn_stop_intent = (
                    await session.execute(
                        select(LiveNodeProcess.stop_requested_at)
                        .where(LiveNodeProcess.deployment_id == deployment_id)
                        .order_by(LiveNodeProcess.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if respawn_stop_intent is not None:
                    log.warning(
                        "auto_restart_suppressed_operator_stop_at_reservation",
                        extra={
                            "account_id": account_id,
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                            "note": (
                                "operator /stop stamped stop_requested_at on the "
                                "latest node row; Phase A ABORTS the respawn "
                                "reservation atomically (no new active row) so a "
                                "/stop that landed in the gap between the pre-spawn "
                                "operator-stop gate and the slot reservation still "
                                "wins — never resurrect a node the operator stopped"
                            ),
                        },
                    )
                    return _PhaseAOutcome.OPERATOR_STOPPED

            # Compute effective gateway_session_key: caller override
            # takes precedence, then fall back to the deployment's
            # ib_login_key, then "default" (single-gateway legacy).
            effective_gw_key = (
                gateway_session_key or getattr(deployment, "ib_login_key", None) or "default"
            )

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
                        extra={
                            "account_id": account_id,
                            "deployment_id": str(deployment_id),
                            "deployment_slug": deployment_slug,
                            "db_status": existing.status,
                            "pid": existing.pid,
                        },
                    )
                    return _PhaseAOutcome.BUSY_STOPPING
                log.info(
                    "spawn_idempotent",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "db_status": existing.status,
                        "pid": existing.pid,
                    },
                )
                return _PhaseAOutcome.ALREADY_ACTIVE

            # PR 2 T4 review P2 — stranded-START resurrection guard.
            #
            # Reached ONLY when there is NO active ``live_node_processes`` row
            # (a genuinely-building node already returned ``ALREADY_ACTIVE``
            # above). A START left un-ACKed in the per-account command stream
            # after a ``/start-portfolio`` poll-timeout (the operator saw a
            # 504 and abandoned it) must NOT resurrect a live TradingNode when
            # a later supervisor re-attaches its consumer and XAUTOCLAIM
            # re-delivers the stale entry. The endpoint flips the abandoned
            # deployment row to ``failed`` on poll-timeout, so a START arriving
            # for a deployment whose row is terminal AND has no active node
            # process is, by definition, stale: ACK it (drop from the PEL)
            # without spawning. A genuine (re-)deployment always re-sets the
            # row to ``starting`` BEFORE publishing its own fresh START, so
            # this can never drop a live operator-intended start.
            #
            # PR 2 T6 (prior-review P0): an AUTO-RESTART (``restart_carry`` is
            # set) is the ONE deliberate respawn that legitimately targets a
            # terminal deployment — when a node crashes, its own
            # ``_mark_terminal`` flips the deployment to ``failed`` BEFORE the
            # reaper observes the exit, so by the time the restart spawn runs
            # the row is already terminal. Refusing it here (the original
            # behavior) made auto-restart a silent no-op (the headline US-1/
            # US-2 capability) — leaving a real-money account flat and
            # unmonitored. So when ``restart_carry`` is present we ALLOW the
            # terminal deployment to proceed and reset its row to ``starting``
            # LATER — only after the CONCURRENT_STARTUP guard below passes and
            # we are actually about to reserve a slot (so a restart that loses
            # the concurrent-startup check leaves the deployment ``failed``,
            # correctly flat-and-unmonitored / re-scannable, rather than stuck
            # at ``starting`` with no node). A normal (un-carried) stale START
            # still ACK-and-drops.
            #
            # Codex P2 — TRANSIENT-failure exemption. A transient spawn failure
            # (Postgres/Redis/import blip in ``spawn_with_outcome``) marks the
            # row + deployment ``failed`` with ``SPAWN_FAILED_TRANSIENT`` and
            # returns ``False`` so the Redis command is REDELIVERED (the no-ACK
            # retry path). The redelivered START carries NO ``restart_carry`` —
            # so without this exemption the stale-START guard would treat it as
            # stale and ACK-DROP it, silently breaking the retry the no-ACK path
            # promised (the transient outage never gets its automatic retry).
            # When the deployment's MOST-RECENT terminal node row is a transient
            # failure we let the redelivered START fall through to re-spawn. The
            # deployment row is reset to ``starting`` LATER (same slot-commit
            # point as the auto-restart path) so a transient retry that bails at
            # CONCURRENT_STARTUP stays ``failed`` / re-scannable, not orphaned.
            # INVARIANT 1 (council 2026-06-01 follow-up): ``stopped`` is
            # operator-terminal and already ABORTED above — so from here on only
            # ``failed`` (recoverable) is treated as a re-startable terminal
            # deployment. NEVER lump ``stopped`` in with ``failed`` again: a
            # ``stopped`` deployment must never be reset to ``starting`` or
            # respawned.
            latest_failure_retry_eligible = False
            if deployment.status == "failed" and restart_carry is None:
                # latest-is-correct READ (council 2026-05-31 item 5): a genuine
                # read of the MOST-RECENT terminal failure_kind, reached only
                # AFTER the active-row guard above returned no active row — so
                # there is no concurrent active row to confuse "latest" with. Not
                # a lifecycle mutation; never owns a row.
                latest_terminal_row = (
                    await session.execute(
                        select(
                            LiveNodeProcess.failure_kind,
                            LiveNodeProcess.started_at,
                        )
                        .where(LiveNodeProcess.deployment_id == deployment_id)
                        .order_by(LiveNodeProcess.started_at.desc())
                        .limit(1)
                    )
                ).one_or_none()
                latest_terminal = (
                    latest_terminal_row.failure_kind if latest_terminal_row is not None else None
                )
                # Codex P2 (council 2026-06-01 follow-up) — TIE THE TRANSIENT
                # EXEMPTION TO THE CURRENT ATTEMPT. The exemption exists so a
                # transient spawn failure's no-ACK REDELIVERY (which carries no
                # ``restart_carry``) is not mistaken for a stale START and
                # ACK-dropped. But honoring ANY historical transient row is a
                # vector: a LATER ``/start`` that timed out at the endpoint (the
                # operator saw a 504 and abandoned it) without ever creating a node
                # row could let a STALE redelivery of THIS deployment's OLD
                # transient row bypass the stale-START drop and spawn a node AFTER
                # the caller already gave up. Bind the exemption to the CURRENT
                # START command: the latest terminal row must have been created
                # at/after ``deployment.last_started_at`` (stamped by the endpoint
                # for THIS attempt). A transient row from a SUPERSEDED earlier
                # attempt (``started_at`` < ``last_started_at``) is NOT eligible →
                # the redelivery is ACK-dropped as stale. ``last_started_at`` is
                # ``None`` only for legacy rows that predate the column — treat that
                # as "cannot prove currency" and fall back to honoring the kind (no
                # behavior change for those).
                bound_to_current_attempt = True
                if (
                    latest_terminal_row is not None
                    and deployment.last_started_at is not None
                    and latest_terminal_row.started_at < deployment.last_started_at
                ):
                    bound_to_current_attempt = False
                latest_failure_retry_eligible = (
                    bound_to_current_attempt
                    and FailureKind.parse_or_unknown(latest_terminal).is_retry_eligible()
                )

            if (
                deployment.status == "failed"
                and restart_carry is None
                and not latest_failure_retry_eligible
            ):
                log.warning(
                    "spawn_dropped_stale_start_terminal_deployment",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "deployment_status": deployment.status,
                        "note": (
                            "START re-delivered for a terminal deployment with "
                            "no active node process — ACK-and-drop so a stale "
                            "queued command can't resurrect a node the operator "
                            "abandoned"
                        ),
                    },
                )
                return _PhaseAOutcome.NOT_STARTABLE

            # Bug X1 fix: IB Gateway's connection semantics don't
            # survive two concurrent subprocess startups. When a second
            # Nautilus ``TradingNode`` connects its data + exec clients
            # while another deployment is still reconciling (``starting``
            # / ``building`` / ``ready``), IB Gateway disconnects the
            # first subprocess's clients mid-startup and both fall into
            # ``startup_health_check_failed``. Serialize spawns at the
            # supervisor: if any OTHER deployment is still initializing
            # ON THE SAME GATEWAY, reject fast with
            # ``CONCURRENT_STARTUP`` so the operator retries once the
            # first is ``running``. Rows already in ``running`` state
            # don't block — once a subprocess has completed
            # reconciliation its connections are stable.
            #
            # PR#3 enhancement: when ``gateway_session_key`` is provided,
            # only block spawns targeting the SAME gateway session. Two
            # deployments on DIFFERENT gateways (different IB logins)
            # can now start concurrently — this is the core enabler for
            # multi-login topologies.
            startup_filters = [
                LiveNodeProcess.deployment_id != deployment_id,
                LiveNodeProcess.status.in_(
                    ("starting", "building", "ready"),
                ),
            ]
            if gateway_session_key is not None:
                startup_filters.append(
                    LiveNodeProcess.gateway_session_key == gateway_session_key,
                )
            other_starting = (
                await session.execute(select(LiveNodeProcess).where(*startup_filters))
            ).scalar_one_or_none()
            if other_starting is not None:
                log.warning(
                    "spawn_blocked_concurrent_startup",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "other_deployment_id": str(other_starting.deployment_id),
                        "other_status": other_starting.status,
                        "gateway_session_key": gateway_session_key,
                    },
                )
                return _PhaseAOutcome.CONCURRENT_STARTUP

            # PR 2 T6 (prior-review P0): we are now committed to reserving a
            # slot — reset a terminal (auto-restart) deployment back to
            # ``starting`` so ``_mark_running`` can later forward-sync it and
            # /live/status reflects the in-flight restart. Done HERE (not at
            # the terminal-status guard above) so a restart that bailed out at
            # CONCURRENT_STARTUP leaves the deployment ``failed`` rather than
            # orphaned at ``starting`` with no node.
            #
            # Codex P2 — the transient-failure redelivery (exempted from the
            # stale-START drop above) needs the SAME reset: it has no
            # ``restart_carry`` but is a legitimate re-spawn of a deployment
            # that the prior transient attempt flipped to ``failed``. Reset it
            # here too so the deployment doesn't linger ``failed`` while the new
            # node row is ``starting``; same CONCURRENT_STARTUP-safe placement.
            # INVARIANT 1 (council 2026-06-01 follow-up): only ``failed`` is reset
            # to ``starting`` — ``stopped`` is operator-terminal and already
            # ABORTED at the top of Phase A, so it can never reach this reset.
            if deployment.status == "failed" and (
                restart_carry is not None or latest_failure_retry_eligible
            ):
                log.info(
                    "auto_restart_reset_terminal_deployment_to_starting",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "prior_deployment_status": deployment.status,
                        "trigger": (
                            "restart_carry" if restart_carry is not None else "transient_retry"
                        ),
                    },
                )
                deployment.status = "starting"

            now = datetime.now(UTC)
            row = LiveNodeProcess(
                deployment_id=deployment_id,
                pid=None,
                host=socket.gethostname(),
                started_at=now,
                last_heartbeat_at=now,
                status="starting",
                gateway_session_key=effective_gw_key,
            )

            # PR 2 T6 (prior-review P1): CARRY the restart-authority counter
            # forward onto the new per-spawn row. Without this every respawn
            # would start the new row at the server-default 0 and the
            # crash-loop ceiling could never trip — the bounded-restart brake
            # would be defeated. We seed the new row with the PRIOR terminal
            # row's counter state, then apply the policy's ``record_failure``
            # increment so THIS attempt is counted exactly once, under the
            # same FOR-UPDATE deployment lock that reserves the slot (so a
            # re-scan / PEL-recovery race can't double-count — the loser hits
            # the partial unique index below and ACKs idempotently without
            # counting). The counter is thereby keyed to the logical
            # deployment, not the ephemeral per-spawn row.
            if restart_carry is not None and self._restart_policy is not None:
                row.consecutive_respawn_failures = restart_carry.prior_consecutive_respawn_failures
                row.last_restart_at = restart_carry.prior_last_restart_at
                row.auto_restart_paused = restart_carry.prior_auto_restart_paused
                row.auto_restart_pause_reason = restart_carry.prior_auto_restart_pause_reason
                # Count this attempt (relative increment within the rolling
                # window, trips the pause latch at the ceiling).
                self._restart_policy.record_failure(row, now=now)

            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # The partial unique index caught a race with another
                # supervisor instance / thread. Treat as idempotent
                # success — the concurrent winner is now building. The
                # counter increment above is rolled back with this
                # transaction, so the loser of an auto-restart race does
                # NOT double-count the attempt (prior-review P2).
                log.info(
                    "spawn_race_idempotent",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                    },
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
        from msai.services.observability.trading_metrics import DEPLOYMENTS_FAILED

        DEPLOYMENTS_FAILED.inc()

        # Best-effort email alert for spawn failures.
        try:
            from msai.services.alerting import AlertService

            await AlertService().alert_strategy_error(strategy_name=str(row_id), error=reason)
        except Exception:  # noqa: BLE001
            pass

        # Bug X3 fix: when a spawn fails permanently the parent
        # ``live_deployments.status`` must also flip to ``failed``. The
        # previous code only updated the ``live_node_processes`` row,
        # leaving the deployment row stuck at ``starting`` forever —
        # visible in the UI, the CLI, and the API as a zombie deployment
        # that never cleans up. Transition the deployment row in the
        # same transaction so /live/status reflects reality on the
        # next read.
        #
        # Council 2026-06-01 — Finding 1 (P0 DEADLOCK FIX): deployment-FIRST.
        # The prior implementation used a plain ``session.get`` on the node row
        # THEN the deployment row, with NO row locks. That was WRONGLY exempted
        # from the lock-order invariant: a dirty-flush ORM UPDATE still acquires a
        # row WRITE lock held until commit, and SQLAlchemy flushes UPDATEs in
        # dirty-order (NOT FK order) — so node-then-deployment is a
        # node→deployment lock-acquisition edge that cycles with the operator-
        # /stop deployment-first path and DEADLOCKS the real-money stop path. We
        # now lock the parent ``LiveDeployment`` row ``FOR UPDATE`` FIRST (resolve
        # its id from the node row via a PLAIN unlocked column read — no lock, no
        # edge), then the node row ``FOR UPDATE``, mirroring the already-correct
        # ``_mark_running`` / ``_terminalize_reserved_row``. The X3 parent-flip and
        # the don't-stomp-``stopped`` guard are unchanged — only the lock order is.
        async with self._db() as session, session.begin():
            deployment_id = (
                await session.execute(
                    select(LiveNodeProcess.deployment_id).where(LiveNodeProcess.id == row_id)
                )
            ).scalar_one_or_none()
            if deployment_id is None:
                return
            # FOR UPDATE FIRST so the non-terminal guard below races correctly
            # with a concurrent /live/stop / kill-all on the same deployment row,
            # and the global lock order stays deployment→node.
            deployment = await session.get(LiveDeployment, deployment_id, with_for_update=True)

            row = await session.get(LiveNodeProcess, row_id, with_for_update=True)
            if row is None:
                return
            row.status = "failed"
            row.failure_kind = failure_kind.value
            row.error_message = reason
            row.exit_code = None

            # Flip parent deployment to ``failed`` only if it's still in
            # a non-terminal state — don't stomp ``stopped`` rows that a
            # concurrent /live/stop may have already written. Evaluated UNDER the
            # deployment lock taken above (deployment-first).
            if deployment is not None and deployment.status in (
                "starting",
                "building",
                "ready",
                "running",
            ):
                deployment.status = "failed"

    async def _terminalize_reserved_row(
        self,
        *,
        row_id: UUID,
        deployment_id: UUID,
        reason: str,
        failure_kind: FailureKind,
    ) -> None:
        """Flip a just-reserved (Phase-A ``starting``) row OUT of the active set
        so it never orphans the deployment's active unique-index slot (council
        2026-06-01, items 2 + 3).

        Used by two pre-``process.start()`` exit paths in
        :meth:`spawn_with_outcome` that must leave NO ``starting`` pid=NULL row
        in the active set:

        - the pre-start stop-intent gate (item 2): an operator /stop landed in
          the post-Phase-A-commit → pre-process.start window;
        - the cancellation cleanup (item 3): the spawn task was cancelled (or
          exited early) before the live handle was installed.

        Lock order is deployment-FIRST: ``live_deployments FOR UPDATE`` THEN the
        owned node row ``FOR UPDATE`` — the global invariant (no node→deployment
        edge). Only flips the OWNED row, and only while it is still in a
        non-terminal active status (so a concurrent ``_mark_running`` promotion or
        a prior terminal write is never clobbered). Best-effort: a DB blip is
        logged, not raised — the ownerless-active-row backstop
        (:meth:`_reap_ownerless_active_rows`) is the ultimate safety net for any
        row this cleanup couldn't reach."""
        try:
            async with self._db() as session, session.begin():
                deployment = (
                    await session.execute(
                        select(LiveDeployment)
                        .where(LiveDeployment.id == deployment_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                row = (
                    await session.execute(
                        select(LiveNodeProcess)
                        .where(LiveNodeProcess.id == row_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is not None and row.status in (
                    "starting",
                    "building",
                    "ready",
                    "running",
                ):
                    row.status = "failed"
                    if row.failure_kind is None:
                        row.failure_kind = failure_kind.value
                    row.error_message = reason
                    # Sync the parent deployment to ``failed`` only if it is still
                    # non-terminal — don't stomp a concurrent /stop's terminal
                    # state. (We hold the deployment lock, taken first.)
                    if deployment is not None and deployment.status in (
                        "starting",
                        "building",
                        "ready",
                        "running",
                    ):
                        deployment.status = "failed"
        except Exception:  # noqa: BLE001 — best-effort; ownerless backstop is the net
            log.exception(
                "spawn_terminalize_reserved_row_failed",
                extra={
                    "deployment_id": str(deployment_id),
                    "row_id": str(row_id),
                    "note": (
                        "could not terminalize the reserved row; the ownerless-row "
                        "backstop will reap it once it is stale"
                    ),
                },
            )

    # ------------------------------------------------------------------
    # Reap loop (decision #15)
    # ------------------------------------------------------------------

    async def reap_once(self) -> None:
        """Run one pass of the reap loop body.

        Walks :attr:`node_handle_cache`, surfaces exit codes for any
        ``is_alive() == False`` children, and removes them from the
        cache. Called in a loop by :meth:`reap_loop` in production;
        tests call it directly to avoid the ``asyncio.sleep`` pacing.
        """
        for deployment_id, cached in list(self.node_handle_cache.items()):
            proc = cached.proc
            if proc.is_alive():
                continue
            proc.join(timeout=1)
            # Thread the OWNED row id + the dead child's pid (council 2026-05-31 /
            # F3): the reaper terminal-writes the row THIS supervisor reserved for
            # this spawn, never "the latest row" — a concurrent periodic rescan
            # can insert a fresher ``starting`` row for the same deployment, and a
            # latest-row reaper would clobber THAT.
            await self._on_child_exit(
                deployment_id,
                proc.exitcode,
                owned_row_id=cached.owned_row_id,
                proc_pid=proc.pid,
            )
            # ``_on_child_exit`` may dispatch a same-pass auto-restart (PR 2 T6)
            # whose ``spawn_with_outcome`` installs the NEW live process (a fresh
            # ``_CachedNode``) under this SAME ``deployment_id`` key. Only evict
            # the handle if it's STILL the dead one we reaped — an unconditional
            # ``del`` would drop the fresh handle and blind the reaper to the
            # restarted node's next crash (prior-review P1).
            if self.node_handle_cache.get(deployment_id) is cached:
                del self.node_handle_cache[deployment_id]

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        """Poll ``self.node_handle_cache`` every second until ``stop_event`` is set.

        Decision #15: parent + child live in the same container
        namespace, so ``Process.is_alive()`` and ``Process.exitcode``
        give instant exit detection. Heartbeat is the recovery signal
        across supervisor restarts only.
        """
        # Stash the loop's stop_event so the cancellable auto-restart backoff
        # (PR 2 T6) can abandon a pending respawn the instant the supervisor
        # starts draining.
        self._reap_stop_event = stop_event
        while not stop_event.is_set():
            try:
                await self.reap_once()
            except Exception:  # noqa: BLE001
                # Prior-review P1: ``reap_once`` MUST NOT crash the reaper.
                # T6 routes un-guarded DB work through this path
                # (``_on_child_exit`` → ``_maybe_auto_restart`` →
                # ``_load_restart_context`` / ``spawn_with_outcome`` →
                # ``_phase_a_reserve_slot`` SELECT-FOR-UPDATE / flush /
                # commit). A transient Postgres error in an auto-restart
                # decision would otherwise unwind through ``reap_once`` and
                # permanently kill ``reap_task`` (a bare, unsupervised
                # ``asyncio.create_task`` in ``main.run_forever``) — silently
                # disabling the fast-path crash reaper AND fleet-wide US-2
                # auto-restart while ``router_heartbeat`` still reports the
                # supervisor healthy. Mirror the guarded sibling loops
                # (``watchdog_loop`` / ``HeartbeatMonitor.run_forever``): log
                # and retry on the next pass. Node death is still eventually
                # caught by the independent HeartbeatMonitor stale sweep.
                log.exception("reap_pass_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _on_child_exit(
        self,
        deployment_id: UUID,
        exit_code: int | None,
        *,
        owned_row_id: UUID | None = None,
        proc_pid: int | None = None,
    ) -> None:
        """Record a child's terminal state after the reap loop sees it exit.

        Row targeting (council 2026-05-31 / F3 — OWN-BY-ROW-ID). The reaper
        classifies and terminal-writes the SPECIFIC row it OWNS, never "the
        latest row by ``started_at`` DESC". T6's periodic rescan concurrently
        INSERTs fresh ``starting`` rows for the same deployment, which
        invalidated the old "latest = the row I own" assumption — a latest-row
        reaper would clobber a fresher concurrent ``starting`` row (the F3 bug).
        Targeting precedence:

        1. ``owned_row_id`` present → SELECT/lock THAT row by id and classify it.
           Production always supplies it (threaded from spawn via the
           :class:`_CachedNode`).
        2. ``owned_row_id`` missing but ``proc_pid`` present → fall back to
           ``(deployment_id, pid == proc_pid)`` and emit ``reap_pid_legacy_
           fallback``. If no row matches, emit ``reap_no_owned_row_no_pid_match``
           and write NOTHING (let watchdog / HeartbeatMonitor / rescan reconcile).
        3. BOTH absent (the legacy/test direct-call path — production never hits
           it, ``reap_once`` always supplies both) → fall back to the latest row
           by ``started_at`` DESC and emit ``reap_owned_row_id_missing``.

        Under NO circumstance does the reaper fall back to "latest active row"
        (or "latest active row with NULL pid") when an identity (owned_row_id OR
        proc_pid) WAS supplied — that is precisely the F3 bug.

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

        PR 2 T6 + council #3 (2026-05-31) — the COMMON-CRASH reaper fix. The
        subprocess writes its OWN terminal ``failed`` row LAST in its ``finally``
        ("Terminal write LAST" in ``trading_node_subprocess.py``) before exiting,
        so for the common crash the row is ALREADY ``failed`` by reap time. The
        old SELECT filtered the ACTIVE status set and early-returned on a terminal
        row, so the restart dispatch NEVER fired at runtime (only SIGKILL/OOM,
        which leave the row stale at ``running``, reached it). The reaper now
        CLASSIFIES the latest row regardless of status (under
        ``SELECT ... FOR UPDATE``) and routes an eligible non-zero-exit failure
        into the SAME :meth:`_maybe_auto_restart` — preserving the subprocess as
        the SOLE terminal writer (the reaper never pre-empts the terminal-write-
        LAST ordering; it only writes terminal state on the stale-active path the
        subprocess never reached).

        Council 2026-06-01 (Finding 2 — DEPLOYMENT-FIRST BOTH-ROW WRITER). On the
        stale-active terminal-write path the reaper now ALSO syncs the parent
        ``LiveDeployment`` to its terminal status (``failed``/``stopped``) in the
        SAME transaction, with the non-terminal guard the watchdog / heartbeat
        sweeps use. This closes a wedge: if the subsequent restart is SUPPRESSED or
        delayed (fleet/account halt, ``auto_restart_paused``, no policy, or a
        backed-off task), a terminal node row paired with a still-``running``
        deployment would be reported active by the status/deploy gate AND skipped
        by the periodic rescan (which requires ``LiveDeployment.status ==
        'failed'``). Because the reaper now writes BOTH rows, it acquires the locks
        deployment-FIRST (``LiveDeployment FOR UPDATE`` before the node row) —
        keeping the global wait-for graph acyclic (no new ``node→deployment`` edge).
        When the restart DOES dispatch, Phase-A's restart_carry path resets the
        deployment to ``starting`` afterward (the transient ``failed``→``starting``
        is the same flow the non-stale-active terminal path already takes).

        Council #3 binding conditions:

        - **Durable operator-stop intent** — suppress whenever
          ``stop_requested_at IS NOT NULL`` (set by ``/stop`` BEFORE the SIGTERM),
          EVEN on a non-zero exit (F5: a ``/stop`` whose graceful shutdown then
          crashes must NOT be resurrected). This survives the subprocess's
          terminal write, unlike the old ``status == 'stopping'`` signal which
          the terminal write clobbers.
        - **Idempotency sentinel** — stamp ``restart_dispatched_at`` under the
          same ``FOR UPDATE`` lock BEFORE dispatching, and skip dispatch if it is
          already set, so a duplicate reap pass on the same still-``failed``
          terminal row never re-dispatches.
        - **Per-decision structured logging** — every decision emits a structured
          log (``auto_restart_dispatched`` / ``auto_restart_suppressed_operator_
          stop`` / ``auto_restart_skipped_no_row`` here; the halt/policy
          suppression logs live in :meth:`_maybe_auto_restart`).

        A CLEAN stop (exit 0) is NOT auto-restarted. The halt gate + bounded
        policy run inside :meth:`_maybe_auto_restart`. Auto-restart only runs when
        a :class:`RestartPolicy` was injected; ``record_failure`` / attempt
        counting stays INSIDE ``_phase_a_reserve_slot`` (never counted here).
        """
        active_statuses = ("starting", "building", "ready", "running", "stopping")
        dispatch_restart = False
        was_operator_stop = False
        was_failure = False
        # PR 2 / F2: captured for the post-commit suppression log when the
        # reaper observed a non-zero exit but the row's kind is a pre-spawn /
        # never-ran permanent failure (not a recoverable runtime crash).
        recoverable = True
        effective_kind = FailureKind.UNKNOWN
        # Account-scoped logging (PR 2 T10): captured inside the classify txn so
        # the post-commit restart-decision logs below carry account context. The
        # ``LiveNodeProcess`` row has no ``account_id`` column, so we read it off
        # the parent ``LiveDeployment`` row — now locked FOR UPDATE at the top of
        # the txn (Finding 2 deployment-first) — so the read is free (no new
        # round-trip / hot-path I/O). ``None`` for a legacy deployment row with no
        # account_id.
        account_id: str | None = None
        db_status: str | None = None
        pid: int | None = None
        heartbeat_age_s: float | None = None
        # FIX 2 (P2): the SPECIFIC row this reaper stamped ``restart_dispatched_at``
        # on — the OWNED row. Threaded through the restart task so the give-up
        # cleanup targets THIS row, never "the latest row" (which a concurrent
        # rescan / operator-retry may have already replaced with a fresh active
        # row that must not be clobbered).
        stamped_row_id: UUID | None = None
        # F3 (council 2026-05-31): did the reaper fall back to pid matching? Used
        # to distinguish "pid given but no row matched → write nothing" (a real
        # production identity attempt) from the both-absent legacy/test path.
        used_pid_fallback = proc_pid is not None and owned_row_id is None
        async with self._db() as session, session.begin():
            # Council 2026-06-01 (Finding 2 — DEPLOYMENT-FIRST BOTH-ROW WRITER):
            # the reaper is now a BOTH-ROW WRITER on the stale-active terminal-write
            # path — it writes the node row terminal AND syncs the parent
            # ``LiveDeployment`` to ``failed`` in the SAME transaction (so a
            # suppressed/delayed restart never leaves a terminal node paired with an
            # active deployment — the wedge Finding 2 closes). It MUST therefore
            # acquire the locks deployment-FIRST, matching the global invariant
            # ``advisory(gateway) → live_deployments FOR UPDATE →
            # live_node_processes FOR UPDATE``. We lock the parent
            # ``LiveDeployment`` row ``FOR UPDATE`` FIRST (``deployment_id`` is the
            # function arg — trivial), THEN the owned node row ``FOR UPDATE``. This
            # keeps the reaper acyclic with /stop / give-up / Phase-A (no new
            # ``node→deployment`` edge from the added deployment write) and lets the
            # non-terminal deployment-sync guard race correctly against a concurrent
            # /stop on the same deployment row. The account_id is read off this SAME
            # locked deployment row (account-scoped decision logs below).
            deployment = await session.get(LiveDeployment, deployment_id, with_for_update=True)

            # Build the classify SELECT by OWN-BY-ROW-ID precedence (council
            # 2026-05-31 / F3). The node lock is taken under FOR UPDATE (AFTER the
            # deployment lock above) so a coincident ``/stop`` either has committed
            # its ``stop_requested_at`` before we read, or blocks until it does
            # (closes the stop-vs-self-crash race, council #3 case 4).
            classify_stmt = select(LiveNodeProcess)
            if owned_row_id is not None:
                # (1) PRIMARY: classify the row this spawn OWNS by id. Production
                # always takes this path (id threaded from the _CachedNode).
                classify_stmt = classify_stmt.where(LiveNodeProcess.id == owned_row_id)
            elif proc_pid is not None:
                # (2) Legacy pid fallback: no owned id but a real dead-child pid.
                # Match (deployment_id, pid). NEVER widen to "latest active row".
                log.info(
                    "reap_pid_legacy_fallback",
                    extra={
                        "deployment_id": str(deployment_id),
                        "proc_pid": proc_pid,
                        "exit_code": exit_code,
                        "note": (
                            "no owned_row_id threaded; falling back to "
                            "(deployment_id, pid) matching"
                        ),
                    },
                )
                classify_stmt = classify_stmt.where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.pid == proc_pid,
                )
            else:
                # (3) Legacy/test direct-call path — production never reaches it
                # (``reap_once`` always supplies owned_row_id + proc_pid). Fall
                # back to the latest row by ``started_at`` DESC.
                log.info(
                    "reap_owned_row_id_missing",
                    extra={
                        "deployment_id": str(deployment_id),
                        "exit_code": exit_code,
                        "note": (
                            "no owned_row_id and no proc_pid — legacy/test direct call; "
                            "classifying the latest row by started_at"
                        ),
                    },
                )
                classify_stmt = (
                    classify_stmt.where(LiveNodeProcess.deployment_id == deployment_id)
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )

            row = (await session.execute(classify_stmt.with_for_update())).scalar_one_or_none()
            if row is None:
                # No row to act on. account_id may still be resolvable if only the
                # node-process row (not the parent deployment) is gone — read it off
                # the deployment row we locked above (no extra round-trip).
                missing_account_id = (
                    (deployment.account_id or "").strip() or None
                    if deployment is not None
                    else None
                )
                if used_pid_fallback:
                    # F3: a pid was supplied but matched NO row. NEVER widen to
                    # "latest active row" — write NOTHING and let watchdog /
                    # HeartbeatMonitor / rescan reconcile (the F3 safety boundary).
                    log.warning(
                        "reap_no_owned_row_no_pid_match",
                        extra={
                            "account_id": missing_account_id,
                            "deployment_id": str(deployment_id),
                            "proc_pid": proc_pid,
                            "exit_code": exit_code,
                            "note": (
                                "owned_row_id missing AND proc_pid matched no row — "
                                "wrote nothing; watchdog/HeartbeatMonitor/rescan will "
                                "reconcile"
                            ),
                        },
                    )
                    return
                # The reaper saw a dead node but there is no row to act on
                # (operator deleted the deployment between crash and reap, or an
                # owned_row_id no longer exists). Emit a structured log so an
                # operator can alert on the silent no-op (council #3 case 8).
                log.info(
                    "auto_restart_skipped_no_row",
                    extra={
                        "account_id": missing_account_id,
                        "deployment_id": str(deployment_id),
                        "exit_code": exit_code,
                        "owned_row_id": str(owned_row_id) if owned_row_id is not None else None,
                        "note": "reaper observed an exit but found no node-process row",
                    },
                )
                return

            # Read the parent deployment's account_id off the deployment row we
            # locked FOR UPDATE at the top (account scoping for the decision logs).
            # No extra round-trip beyond the locked read already done.
            account_id = (
                (deployment.account_id or "").strip() or None if deployment is not None else None
            )
            db_status = row.status
            pid = row.pid
            if row.last_heartbeat_at is not None:
                heartbeat_age_s = (datetime.now(UTC) - row.last_heartbeat_at).total_seconds()

            # Durable operator-stop intent (council #3 F5) — survives the
            # subprocess's terminal write, so it MUST be read from the column,
            # not from ``status`` (which the terminal write clobbers).
            was_operator_stop = row.stop_requested_at is not None
            already_dispatched = row.restart_dispatched_at is not None
            was_failure = exit_code != 0

            # The subprocess is the SOLE terminal writer. Only the stale-active
            # path (SIGKILL/OOM — the ``finally`` never ran, row still in the
            # active set) needs the reaper to write terminal state. A row the
            # subprocess already moved to a terminal status is LEFT UNTOUCHED so
            # the reaper never overwrites the richer diagnosis it persisted.
            if row.status in active_statuses:
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
                            # PR 2 / F2: a generic non-zero exit on a still-ACTIVE
                            # row means the OS process had STARTED and was then
                            # SIGKILL/OOM'd (the subprocess never reached its own
                            # ``finally`` terminal write) — i.e. the node RAN.
                            # Classify it as a RUNTIME crash (recoverable), NOT a
                            # pre-spawn SPAWN_FAILED_PERMANENT (which the recovery
                            # paths must NOT re-drive). Resolves the
                            # SPAWN_FAILED_PERMANENT overload that made
                            # ``failure_kind`` alone unable to tell "ran then
                            # crashed" from "never ran".
                            row.failure_kind = FailureKind.NODE_CRASHED.value
                row.exit_code = exit_code

                # Council 2026-06-01 (Finding 2 — TERMINAL-WRITE DEPLOYMENT SYNC):
                # the reaper OWNS this terminal node write (the subprocess's own
                # ``finally`` never ran — SIGKILL/OOM left the row in the active
                # set). It must ALSO sync the parent ``LiveDeployment`` to its
                # terminal status in this SAME deployment-first transaction, with
                # the SAME non-terminal guard the watchdog / heartbeat sweeps use.
                # Without this, when the subsequent restart is SUPPRESSED or delayed
                # (fleet/account halt latch, ``auto_restart_paused``, no
                # RestartPolicy injected, or a backed-off restart task), the node
                # row reads ``failed``/``stopped`` while ``LiveDeployment`` stays
                # ``running``/active — the status/deploy gate reports an active
                # deployment AND the periodic rescan (which requires
                # ``LiveDeployment.status == 'failed'``) never picks it up after the
                # suppressor clears → a permanent wedge. When the restart DOES
                # dispatch, Phase-A's restart_carry path resets the deployment to
                # ``starting`` afterward, so this transient ``failed``→``starting``
                # is the same flow the non-stale-active terminal path already takes.
                # The guard mirrors ``_mark_terminal`` / the watchdog: never stomp a
                # concurrent /stop's ``stopped``/``stopping`` (evaluated UNDER the
                # deployment lock taken at the top → deployment-first).
                if deployment is not None and deployment.status in (
                    "starting",
                    "building",
                    "ready",
                    "running",
                ):
                    deployment.status = "stopped" if exit_code == 0 else "failed"

            # The row's EFFECTIVE failure_kind after any reaper write above (or
            # the subprocess's already-persisted terminal kind on the common-
            # crash path). This is the recovery-eligibility signal shared with
            # the periodic rescan (PR 2 / F2): re-drive ONLY a recoverable
            # runtime crash, never a pre-spawn / never-ran permanent failure
            # (a permanent payload-config error, a registry kind, or a
            # halt-blocked START that never ran). A NULL/unrecognised kind →
            # UNKNOWN → recoverable (the genuine outage-window crash).
            effective_kind = FailureKind.parse_or_unknown(row.failure_kind)
            recoverable = effective_kind.is_recoverable_crash()

            # Decide whether to dispatch a restart, and stamp the idempotency
            # sentinel UNDER this FOR UPDATE lock BEFORE the dispatch so a
            # duplicate reap pass on the same terminal row can never re-dispatch.
            if (
                was_failure
                and recoverable
                and not was_operator_stop
                and not already_dispatched
                and self._restart_policy is not None
            ):
                row.restart_dispatched_at = datetime.now(UTC)
                dispatch_restart = True
                # FIX 2 (P2): remember the SPECIFIC row we stamped so the restart
                # task's give-up cleanup targets it by id, not "the latest row".
                # In the own-by-id path this is exactly ``owned_row_id``; in the
                # pid/legacy paths it is the row we resolved + locked.
                stamped_row_id = row.id

        if was_operator_stop and was_failure:
            # The operator asked this node to stop; its shutdown crashed but we
            # must NOT auto-restart it (would fight an operator /stop — a live
            # node trading the account again after the operator halted it). The
            # halt gate would NOT catch this (a plain /stop sets no latch), so
            # the durable ``stop_requested_at`` intent is what suppresses it.
            log.info(
                "auto_restart_suppressed_operator_stop",
                extra={
                    "account_id": account_id,
                    "deployment_id": str(deployment_id),
                    "exit_code": exit_code,
                    "db_status": db_status,
                    "pid": pid,
                    "heartbeat_age_s": heartbeat_age_s,
                    "restart_decision": "suppressed_operator_stop",
                    "note": "node crashed during operator-initiated stop; not auto-restarting",
                },
            )
            return

        if was_failure and not was_operator_stop and not recoverable:
            # PR 2 / F2: a non-zero exit whose row carries a pre-spawn /
            # never-ran permanent kind (SPAWN_FAILED_PERMANENT, a registry
            # kind, or a halt-blocked HALT_ACTIVE/ACCOUNT_HALT_ACTIVE start).
            # The node never RAN, so auto-restart would churn a permanent
            # error / re-trade a halt-blocked account — both operator-START
            # concerns. Suppress here (the rescan sibling applies the SAME
            # ``is_recoverable_crash`` predicate, so the two agree). The
            # operator fixes config (or re-issues after /resume) and re-starts.
            log.info(
                "auto_restart_suppressed_not_recoverable",
                extra={
                    "account_id": account_id,
                    "deployment_id": str(deployment_id),
                    "exit_code": exit_code,
                    "db_status": db_status,
                    "pid": pid,
                    "heartbeat_age_s": heartbeat_age_s,
                    "failure_kind": effective_kind.value,
                    "restart_decision": "suppressed_not_recoverable",
                    "note": (
                        "non-zero exit but the failure kind is a pre-spawn / "
                        "never-ran permanent (or halt-blocked) failure; the node "
                        "never ran, so this is an operator-START concern — not "
                        "auto-restarting"
                    ),
                },
            )
            return

        # Auto-restart runs AFTER the terminal-state / sentinel transaction
        # commits so a fresh ``_load_restart_context`` read sees the persisted
        # ``failed`` row + counters, and so the row-lock the restart-counter
        # write takes never contends with this writer's lock. The halt gate +
        # bounded policy + their suppression logs live inside
        # ``_maybe_auto_restart``.
        #
        # PR 2 F1 — the reaper must NOT block on the restart. ``_on_child_exit``
        # is called SYNCHRONOUSLY by the single ``reap_once`` loop, so awaiting
        # the (up-to-300s, cancellable) backoff here would stall classification
        # of every OTHER account's exit — defeating per-account failure
        # isolation. SCHEDULE the restart as a detached per-account task and
        # RETURN control to the reaper immediately. The task performs the
        # cancellable backoff + the actual ``_maybe_auto_restart`` spawn, and is
        # tracked so it can be cancelled on shutdown and deduped.
        if dispatch_restart:
            log.info(
                "auto_restart_dispatched",
                extra={
                    "account_id": account_id,
                    "deployment_id": str(deployment_id),
                    "exit_code": exit_code,
                    "db_status": db_status,
                    "pid": pid,
                    "heartbeat_age_s": heartbeat_age_s,
                    "restart_decision": "dispatched",
                    "note": (
                        "reaper classified a non-operator-stop failure; scheduling restart task"
                    ),
                },
            )
            self._schedule_restart_task(deployment_id, account_id, owned_row_id=stamped_row_id)

    # ------------------------------------------------------------------
    # PR 2 F1/F2 — per-account restart TASK (off the reaper's hot path)
    # ------------------------------------------------------------------

    def _schedule_restart_task(
        self,
        deployment_id: UUID,
        account_id: str | None,
        *,
        owned_row_id: UUID | None = None,
    ) -> None:
        """Schedule a detached per-account auto-restart task (PR 2 F1).

        Called by the reaper (``_on_child_exit``) instead of awaiting the
        restart inline, so the single ``reap_once`` loop is never blocked by one
        account's (up-to-300s) backoff. Idempotent per deployment: if a restart
        task for ``deployment_id`` is already in flight this is a NO-OP — never
        two concurrent restart tasks (and hence never two live nodes) for one
        account. (The Phase-A partial unique index is the authoritative
        backstop; this dedupe just avoids the wasted work + the duplicate
        log noise.)

        Synchronous (not ``async``) so the reaper can fire-and-forget it.

        ``owned_row_id`` (FIX 2, P2): the SPECIFIC node-process row the reaper
        stamped ``restart_dispatched_at`` on — threaded to the give-up cleanup so
        it acts on THAT row, not "the latest row" (which a concurrent rescan /
        operator-retry may have replaced with a fresh active row). ``None`` for
        callers that don't own a specific row (e.g. test wiring); the cleanup
        then falls back to the latest-row behaviour.
        """
        existing = self._restart_tasks.get(deployment_id)
        if existing is not None and not existing.done():
            log.info(
                "auto_restart_task_already_in_flight",
                extra={
                    "account_id": account_id,
                    "deployment_id": str(deployment_id),
                    "note": (
                        "a restart task is already running for this deployment; not duplicating"
                    ),
                },
            )
            return
        task = asyncio.create_task(
            self._run_restart_task(deployment_id, account_id, owned_row_id=owned_row_id)
        )
        self._restart_tasks[deployment_id] = task

        def _done(t: asyncio.Task[None]) -> None:
            # Deregister ONLY if this is still the handle we stored — a second
            # crash that scheduled a fresh task after this one finished must not
            # have its handle dropped by this stale callback.
            if self._restart_tasks.get(deployment_id) is t:
                del self._restart_tasks[deployment_id]
            # Retrieve any exception so it is not surfaced as "Task exception
            # was never retrieved". ``_run_restart_task`` already catches +
            # logs everything; CancelledError on shutdown is expected.
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:  # pragma: no cover — defensive; body swallows
                log.error(
                    "auto_restart_task_unhandled_exception",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "exc_type": type(exc).__name__,
                    },
                )

        task.add_done_callback(_done)

    async def _run_restart_task(
        self,
        deployment_id: UUID,
        account_id: str | None,
        *,
        owned_row_id: UUID | None = None,
    ) -> None:
        """Body of a per-account restart task (PR 2 F1/F2).

        Performs the SAME halt-gated, reconcile-verified, ceiling-bounded
        restart the reaper used to run inline — just relocated off the reaper's
        hot path. ``_attempt_auto_restart`` itself still owns the halt-gate-FIRST
        check, the bounded ``RestartPolicy`` decision, the CANCELLABLE backoff
        (which abandons on a fleet/account halt OR supervisor shutdown observed
        mid-wait), and the reconcile-verified respawn. Nothing about those
        semantics changes — they just run here.

        PR 2 F2 + council #4 OPT C Part 2 — transient dispatch resilience. Two
        transient shapes are RETRIED on this fast path; a deliberate terminal
        suppression is NOT:

          1. A transient DB/Redis blip while loading the restart context,
             reserving the Phase-A slot, or spawning RAISES out of
             ``_attempt_auto_restart`` — caught + retried below.
          2. A transient NO-ACK respawn outcome
             (:attr:`_RestartOutcome.TRANSIENT_NO_ACK` —
             ``CONCURRENT_STARTUP`` while a sibling on the same gateway is still
             initialising, or a transient post-Phase-A payload-factory blip)
             returns WITHOUT raising. Pre-OPT-C this non-raising ``False`` was
             treated as terminal → the common transient strand (a node left
             ``failed`` + flat-and-unmonitored until a periodic-rescan tick).
             OPT C Part 2 makes it RETRYABLE so the common transient recovers in
             ~seconds.

        A DELIBERATE terminal suppression (:attr:`_RestartOutcome.SUPPRESSED` —
        halt / paused / no-context / ack-drop / permanent / ``ALREADY_ACTIVE``
        race-loser) and a genuine restart (:attr:`_RestartOutcome.RESTARTED`)
        are both terminal: the task returns immediately, NEVER retrying a
        deliberately-suppressed (halted/paused/operator-stopped/permanent)
        deployment.

        If the transient retries are exhausted the task CLEARS the idempotency
        sentinel + re-fails the deployment LOUDLY (alertable) so a later
        reap/periodic-rescan can re-drive it — never silently stranded. The
        periodic reconciling rescan (council #4 OPT C Part 1) is the
        authoritative backstop if this fast retry gives up.

        Double-spawn safety does NOT depend on the sentinel being set before
        dispatch: the Phase-A partial unique index ``uq_live_node_processes_
        active_deployment`` serialises any duplicate (the loser gets
        ``ALREADY_ACTIVE`` and ACKs idempotently), and ``_schedule_restart_task``
        dedupes in-flight tasks. So clearing the sentinel on give-up is safe.
        """
        last_exc: Exception | None = None
        for attempt in range(1, _RESTART_TASK_MAX_ATTEMPTS + 1):
            # Abandon early if the supervisor is draining — never respawn during
            # shutdown (the cancellable backoff inside ``_attempt_auto_restart``
            # also enforces this, but checking here avoids a needless retry).
            if self._reap_stop_event.is_set():
                return
            transient_no_ack = False
            try:
                # ``_attempt_auto_restart`` re-checks the halt gate FIRST every
                # call, so each retry is independently halt-gated. The structured
                # outcome tells a TRANSIENT no-ACK (retry) apart from a
                # DELIBERATE terminal decision (do NOT retry).
                outcome = await self._attempt_auto_restart(deployment_id)
                if outcome is not _RestartOutcome.TRANSIENT_NO_ACK:
                    # RESTARTED (a process started) or SUPPRESSED (a deliberate
                    # halt / paused / no-context / ack-drop / permanent /
                    # already-active decision). Both are terminal — retrying a
                    # deliberate suppression would churn a halted/paused/
                    # operator-stopped/permanent deployment. Nothing to retry.
                    return
                # TRANSIENT_NO_ACK (CONCURRENT_STARTUP / transient
                # payload-factory): expected to succeed on a retry once the
                # momentary condition clears. Fall through to the bounded backoff.
                transient_no_ack = True
                log.info(
                    "auto_restart_task_transient_no_ack_retry",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "attempt": attempt,
                        "max_attempts": _RESTART_TASK_MAX_ATTEMPTS,
                        "note": (
                            "transient no-ACK respawn (CONCURRENT_STARTUP / transient "
                            "payload-factory); retrying under the bounded ceiling"
                        ),
                    },
                )
            except asyncio.CancelledError:
                # Supervisor shutdown cancelled the task mid-dispatch.
                raise
            except Exception as exc:  # noqa: BLE001 — transient dispatch failure
                last_exc = exc
                log.warning(
                    "auto_restart_task_transient_dispatch_failed",
                    extra={
                        "account_id": account_id,
                        "deployment_id": str(deployment_id),
                        "attempt": attempt,
                        "max_attempts": _RESTART_TASK_MAX_ATTEMPTS,
                        "exc_type": type(exc).__name__,
                        "note": "transient dispatch failure; will retry under the bounded ceiling",
                    },
                )
            # Reached only on a transient outcome (raised OR no-ACK). Pace the
            # next attempt with the cancellable backoff.
            if (transient_no_ack or last_exc is not None) and attempt < _RESTART_TASK_MAX_ATTEMPTS:
                # Cancellable pause before the next retry — wake instantly on
                # supervisor shutdown so a drain isn't delayed by the backoff.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._reap_stop_event.wait(),
                        timeout=self._restart_retry_backoff_s,
                    )
                if self._reap_stop_event.is_set():
                    return

        # Exhausted the bounded retries on a persistently-transient dispatch.
        # Clear the sentinel + re-fail LOUDLY so a later reap/rescan re-drives
        # it instead of leaving the account flat-and-unmonitored.
        await self._clear_sentinel_and_refail_after_giveup(deployment_id, owned_row_id)
        log.error(
            "auto_restart_task_gave_up",
            extra={
                "account_id": account_id,
                "deployment_id": str(deployment_id),
                "attempts": _RESTART_TASK_MAX_ATTEMPTS,
                "exc_type": type(last_exc).__name__ if last_exc is not None else None,
                "note": (
                    "transient dispatch failed every attempt; cleared the restart "
                    "sentinel + re-failed so a later reap/rescan can retry — "
                    "ALERT: this account may be flat-and-unmonitored until then"
                ),
            },
        )

    async def _clear_sentinel_and_refail_after_giveup(
        self, deployment_id: UUID, owned_row_id: UUID | None = None
    ) -> None:
        """Clear ``restart_dispatched_at`` on the OWNED node-process row and
        re-fail the deployment after the restart task gave up (PR 2 F2 / FIX 2).

        Clearing the sentinel is what makes the deployment RE-DRIVABLE: the
        reaper's ``_on_child_exit`` only dispatches when the sentinel is unset,
        and the rescan candidate query only matches ``failed`` rows. Best-effort
        + idempotent: a second DB blip here is logged, not raised (the task is
        already giving up; the startup watchdog / rescan remains the backstop).
        Only touches the OWNED row by id, and only while it is still in a
        non-running active status, so it never clobbers a concurrent
        ``_mark_running`` promotion.

        FIX 2 (P2) — act ONLY on the OWNED row by id. The give-up cleanup acts on
        the SPECIFIC row the reaper stamped ``restart_dispatched_at`` on
        (``owned_row_id``), NOT on a blind ``ORDER BY started_at DESC LIMIT 1``.
        If a concurrent rescan / operator-retry already started a FRESH row for
        the same deployment (now the latest, active), a latest-row cleanup would
        clobber that fresh running node to ``failed`` WITHOUT signalling its child
        — dropping a live node from the active set and risking a duplicate start.
        So: lock + clear the owned row by id; if it is still in a non-terminal
        active status, flip it to ``failed`` so the rescan can re-drive it.

        Re-fail the parent ``LiveDeployment`` ONLY if NO newer active row exists
        for the deployment — otherwise a concurrent restart has already brought
        the deployment back to active and must not be clobbered.

        Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST): acquire
        the parent ``LiveDeployment`` row lock ``FOR UPDATE`` FIRST, THEN the
        owned ``LiveNodeProcess`` row, matching the global invariant
        ``advisory(gateway) → live_deployments FOR UPDATE → live_node_processes
        FOR UPDATE``. The prior order locked node-then-deployment, which combined
        with the operator-/stop path (now deployment-then-node) could form a
        D→N→D cycle and deadlock the real-money stop path. The deployment lock
        held first ALSO collapses the newer-active-row probe and the re-fail into
        ONE serialised snapshot (the council 2026-05-31 iter-9 TOCTOU close
        survives the re-order — the probe + re-fail still run under this lock). A
        concurrent Phase-A respawn (which holds the deployment lock then only
        INSERTs a new node row) is serialised against this: either it commits its
        fresh ``starting`` row + ``starting`` deployment BEFORE we take the lock
        (so we SEE the newer active row and yield), or it blocks on our lock until
        we commit (so it then resets the deployment off our ``failed`` state). The
        global wait-for graph is acyclic: the only edges are advisory→deployment
        and deployment→node; no node→deployment edge exists anywhere.

        ``owned_row_id is None`` (legacy / test wiring with no specific owner)
        falls back to the latest-row behaviour (and unconditionally re-fails the
        deployment) — there is no concurrent-restart row to protect in that case.
        """
        try:
            async with self._db() as session, session.begin():
                # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST):
                # acquire the parent ``live_deployments`` row lock FIRST, then the
                # owned node row, mirroring the global invariant
                # ``advisory(gateway) → live_deployments FOR UPDATE →
                # live_node_processes FOR UPDATE``. The prior order was
                # node-then-deployment, which combined with /stop's
                # deployment-then-node could form a D→N→D cycle. The deployment
                # lock ALSO serialises the newer-active-row probe + the re-fail
                # into one snapshot (the iter-9 TOCTOU close still holds — the
                # probe + re-fail run under this same lock).
                deployment = (
                    await session.execute(
                        select(LiveDeployment)
                        .where(LiveDeployment.id == deployment_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                stmt = select(LiveNodeProcess)
                if owned_row_id is not None:
                    stmt = stmt.where(LiveNodeProcess.id == owned_row_id)
                else:
                    stmt = (
                        stmt.where(LiveNodeProcess.deployment_id == deployment_id)
                        .order_by(LiveNodeProcess.started_at.desc())
                        .limit(1)
                    )
                row = (await session.execute(stmt.with_for_update())).scalar_one_or_none()

                # REVALIDATE (council 2026-06-01): re-ordering must not weaken the
                # owned-row targeting from council #5. The owned-row select above
                # still targets the SPECIFIC row by id (``owned_row_id``), so the
                # status/id semantics are unchanged — we only mutate the row we
                # OWN, and only while it is still in a non-terminal active status.
                if row is not None:
                    # Clear the sentinel so a future reap/rescan can re-dispatch.
                    row.restart_dispatched_at = None
                    # If the row is still in a non-terminal state (the respawn
                    # never reserved a live slot), mark it failed so the rescan
                    # candidate query (which requires ``failed``) can pick it up.
                    if row.status in ("starting", "building", "ready", "running"):
                        row.status = "failed"
                        if row.failure_kind is None:
                            row.failure_kind = FailureKind.SPAWN_FAILED_TRANSIENT.value
                        row.error_message = (
                            "auto-restart task exhausted transient-dispatch retries; "
                            "re-failed so a later reap/rescan can retry"
                        )

                # FIX 2 (P2): only re-fail the parent deployment if no NEWER
                # active row exists. A concurrent rescan / operator-retry that
                # started a fresh active row (later ``started_at``) has already
                # revived the deployment — re-failing it here would fight that
                # restart. With ``owned_row_id is None`` there is no specific
                # owner to compare against, so re-fail unconditionally (legacy).
                newer_active_exists = False
                if owned_row_id is not None and row is not None:
                    newer_active_exists = (
                        await session.execute(
                            select(LiveNodeProcess.id)
                            .where(
                                LiveNodeProcess.deployment_id == deployment_id,
                                LiveNodeProcess.started_at > row.started_at,
                                LiveNodeProcess.status.in_(
                                    ("starting", "building", "ready", "running", "stopping")
                                ),
                            )
                            .limit(1)
                        )
                    ).first() is not None

                if newer_active_exists:
                    log.info(
                        "giveup_yielded_to_newer_active",
                        extra={
                            "deployment_id": str(deployment_id),
                            "owned_row_id": str(owned_row_id) if owned_row_id is not None else None,
                            "note": (
                                "a newer active node row exists (concurrent restart / "
                                "operator-retry) — give-up cleanup did NOT re-fail the "
                                "deployment so it doesn't clobber the live node"
                            ),
                        },
                    )
                elif deployment is not None and deployment.status in (
                    "starting",
                    "building",
                    "ready",
                    "running",
                ):
                    deployment.status = "failed"
        except Exception:  # noqa: BLE001 — best-effort; rescan/watchdog is the backstop
            log.exception(
                "auto_restart_giveup_cleanup_failed",
                extra={
                    "deployment_id": str(deployment_id),
                    "owned_row_id": str(owned_row_id) if owned_row_id is not None else None,
                    "note": "could not clear the restart sentinel; rescan/watchdog is the backstop",
                },
            )

    async def cancel_restart_tasks(self) -> None:
        """Cancel + drain every in-flight per-account restart task (PR 2 F1).

        Called on supervisor shutdown so a pending respawn never lands a fresh
        live node while the fleet is draining. Idempotent.
        """
        tasks = list(self._restart_tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._restart_tasks.clear()

    # Council 2026-06-01 (item 3): the per-deployment ``cancel_restart_task``
    # method has been REMOVED. Its only caller was the operator-stop path
    # (``FleetRouter.stop``), where the naked cancel orphaned a committed
    # ``starting`` row mid-reservation (the F2 bug). Operator-stop correctness now
    # rests on the durable ``stop_requested_at`` intent + the serialized Phase-A
    # OPERATOR_STOPPED re-check + the pre-start stop-intent gate
    # (:meth:`spawn_with_outcome`). The plural shutdown-wide
    # :meth:`cancel_restart_tasks` remains (it is cancellation-safe — the spawn
    # path terminalizes any reserved row on ``CancelledError``).

    async def _await_restart_tasks_for_test(self) -> None:
        """Await every in-flight restart task to completion (test support only).

        Lets a unit/integration test that drives ``_on_child_exit`` /
        ``rescan_for_restart`` (which now SCHEDULE the restart as a detached
        task) deterministically observe the dispatch outcome without racing the
        detached task. NOT used in production — shutdown uses
        :meth:`cancel_restart_tasks`.

        A task's ``_done`` callback (which deregisters it from
        ``self._restart_tasks``) is scheduled via ``loop.call_soon`` and does
        NOT run synchronously when ``await t`` returns — so after awaiting a
        finished task the dict can still hold it for one more loop turn. We
        ``await asyncio.sleep(0)`` after each drain pass to YIELD to the event
        loop and let those callbacks run; without it a tight re-check loop over
        already-done tasks busy-spins forever (it never yields to drain the
        callback queue). A finished task that scheduled ANOTHER restart task
        (the give-up re-fail path can't, but a crash-loop could) is picked up on
        the next pass; the loop exits once the dict is genuinely empty.
        """
        while self._restart_tasks:
            tasks = list(self._restart_tasks.values())
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            # Yield so the just-completed tasks' ``_done`` callbacks (queued via
            # ``call_soon``) deregister themselves before we re-check the dict.
            await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # US-2 halt-gated bounded auto-restart (PR 2 T6)
    # ------------------------------------------------------------------

    async def _halt_active(self, account_id: str | None) -> bool:
        """Return True if a fleet OR account halt latch is set.

        The gate is fleet-OR-account (Codex iter-8): ``/kill-all`` sets ONLY
        ``fleet_halt_key`` (``api/live.py``) while ``/drain`` sets ONLY
        ``account_halt_key(account_id)`` — so an account-only gate would let
        auto-restart fight a fleet emergency kill-all. A restart is suppressed
        if EITHER key is set.

        Fail-closed: any Redis error is treated as "halt active" (return
        True) so a Redis blip can never let a node be respawned under an
        emergency halt the supervisor simply couldn't read. A respawn under a
        live kill-all is the one outcome a real-money kill switch must never
        allow.
        """
        try:
            # Fleet latch first (short-circuits the account lookup when a
            # kill-all is active).
            if await self._redis.exists(_HALT_KEY):
                return True
            if not account_id:
                return False
            return bool(await self._redis.exists(account_halt_key(account_id)))
        except Exception:  # noqa: BLE001 — fail-closed: unreadable latch == halted
            log.exception(
                "auto_restart_halt_check_failed_fail_closed",
                extra={"account_id": account_id},
            )
            return True

    async def _operator_stop_requested(self, deployment_id: UUID) -> bool:
        """Return True if the deployment's latest node-process row carries a
        durable operator-stop intent (``stop_requested_at IS NOT NULL``).

        FINDING 1 (P1) — never fight an operator /stop. A plain ``/stop`` sets
        NO halt latch (unlike ``/kill-all`` / ``/drain``), so the halt gate
        ``_halt_active`` can't see it. When the operator stops a deployment
        whose latest row has ALREADY been classified ``failed`` with an
        in-flight auto-restart task backing off, the ``/stop`` handler stamps
        ``stop_requested_at`` on that failed row — and the in-flight restart
        task MUST observe it and abort the respawn (no resurrection of a node
        the operator just stopped). This re-reads the intent UNDER ``FOR
        UPDATE`` right before each respawn decision so a ``/stop`` that
        committed during the (up-to-300s) backoff is honored.

        Reads the LATEST row by ``started_at`` — the SAME row the ``/stop``
        handler stamps and the reaper classifies — so the intent the operator
        recorded is the one we see.

        Fail-closed: any DB error is treated as "stop requested" (return True)
        so a transient DB blip can never let a node be resurrected against a
        pending operator stop. The periodic reconciling rescan re-evaluates
        later if (and only if) the stop intent is genuinely absent.
        """
        try:
            async with self._db() as session, session.begin():
                stop_at = (
                    await session.execute(
                        select(LiveNodeProcess.stop_requested_at)
                        .where(LiveNodeProcess.deployment_id == deployment_id)
                        .order_by(LiveNodeProcess.started_at.desc())
                        .limit(1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            return stop_at is not None
        except Exception:  # noqa: BLE001 — fail-closed: unreadable intent == stopped
            log.exception(
                "auto_restart_stop_intent_check_failed_fail_closed",
                extra={"deployment_id": str(deployment_id)},
            )
            return True

    async def _load_restart_context(self, deployment_id: UUID) -> _RestartContext | None:
        """Load everything the restart decision needs in one DB read.

        Returns ``None`` when the deployment row is gone (operator deleted it
        between the crash and the reap) OR there is no terminal node-process
        row to read counters from — either way there is nothing to restart.
        """
        async with self._db() as session:
            deployment = (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.id == deployment_id)
                )
            ).scalar_one_or_none()
            if deployment is None:
                return None
            # latest-is-correct READ (council 2026-05-31 item 5): reads the
            # CURRENT restart context (the prior terminal row's counters +
            # gateway_session_key) to DECIDE a restart. It must YIELD to a newer
            # active row — if a concurrent restart already created one, the
            # subsequent Phase-A reservation hits the partial unique index and
            # returns ALREADY_ACTIVE (the race-loser ACKs idempotently). A read,
            # not a mutation; owns no row.
            process = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(LiveNodeProcess.deployment_id == deployment_id)
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if process is None:
                return None
            return _RestartContext(
                deployment_id=deployment_id,
                deployment_slug=deployment.deployment_slug,
                account_id=(deployment.account_id or "").strip() or None,
                gateway_session_key=process.gateway_session_key,
                process=process,
            )

    async def _cancellable_backoff(self, delay_s: float, account_id: str | None) -> bool:
        """Wait ``delay_s`` seconds, abandoning early on shutdown OR halt.

        Returns ``True`` if the full backoff elapsed (proceed with the
        restart) and ``False`` if it was abandoned — either the supervisor
        started draining (``stop_event`` set) or a fleet/account halt latch
        was raised mid-wait (Claude iter-1 3-B: a halt arriving DURING the
        backoff abandons the restart). Implemented as a poll loop so the halt
        latch is observed within :data:`_BACKOFF_POLL_INTERVAL_S`; a bare
        ``asyncio.sleep(delay)`` could not see a latch raised mid-wait.
        """
        remaining = max(0.0, delay_s)
        while remaining > 0.0:
            if self._reap_stop_event.is_set():
                return False
            if await self._halt_active(account_id):
                return False
            tick = min(_BACKOFF_POLL_INTERVAL_S, remaining)
            with contextlib.suppress(TimeoutError):
                # Wait on the stop_event so a shutdown wakes us immediately;
                # the timeout is the per-tick slice.
                await asyncio.wait_for(self._reap_stop_event.wait(), timeout=tick)
            remaining -= tick
        # Final shutdown + halt re-check on the boundary.
        if self._reap_stop_event.is_set():
            return False
        return not await self._halt_active(account_id)

    async def _maybe_auto_restart(self, deployment_id: UUID, *, skip_backoff: bool = False) -> bool:
        """Decide and (if eligible) carry out a halt-gated bounded restart.

        Thin bool wrapper over :meth:`_attempt_auto_restart` (council #4 OPT C
        Part 2). Returns ``True`` only when a process was GENUINELY (re)started
        (:attr:`_RestartOutcome.RESTARTED`), and ``False`` for every other
        outcome — a deliberate suppression OR a transient no-ACK. The bool is
        the honest "a restart actually happened" signal the periodic re-scan
        tallies and ``/live/status`` reports; the re-scan does NOT need the
        transient-vs-terminal distinction (the periodic loop simply re-drives a
        transient candidate on its NEXT pass), so collapsing to a bool here is
        correct. Only the fast per-path retry (:meth:`_run_restart_task`) needs
        the richer outcome, and it calls :meth:`_attempt_auto_restart` directly.

        ``skip_backoff`` (PR 2 F2): when True (the periodic re-scan path), a
        BACKOFF policy decision is SKIPPED rather than slept on — so one
        crash-looping candidate never blocks the rescan loop from evaluating the
        others. The next rescan pass retries it once the backoff window passes.
        """
        return (
            await self._attempt_auto_restart(deployment_id, skip_backoff=skip_backoff)
            is _RestartOutcome.RESTARTED
        )

    async def _attempt_auto_restart(
        self, deployment_id: UUID, *, skip_backoff: bool = False
    ) -> _RestartOutcome:
        """Decide and (if eligible) carry out a halt-gated bounded restart,
        returning a structured :class:`_RestartOutcome` (council #4 OPT C Part 2).

        The outcome lets the fast per-path retry (:meth:`_run_restart_task`)
        tell a TRANSIENT no-ACK failure (RETRY in ~seconds) apart from a
        DELIBERATE terminal suppression (do NOT retry — would churn a halted /
        paused / operator-stopped / permanent / already-active deployment):

        - :attr:`_RestartOutcome.RESTARTED` — a process genuinely (re)started.
        - :attr:`_RestartOutcome.TRANSIENT_NO_ACK` — ``spawn_with_outcome``
          returned ``(ack=False, process_started=False)`` (``CONCURRENT_STARTUP``
          or a transient post-Phase-A payload-factory blip). RETRYABLE.
        - :attr:`_RestartOutcome.SUPPRESSED` — every deliberate
          do-not-restart decision: no policy, no context, halt gate (fleet OR
          account) active, ``PAUSED`` ceiling, backoff abandoned, OR a spawn
          ACK-drop (``ack=True, process_started=False``) / idempotent
          ``ALREADY_ACTIVE`` race-loser. NOT retried.

        Order (Claude iter-1 3-A — gate at the DECISION point, before queuing
        any restart):

        1. **Halt gate FIRST** — fleet OR account latch set → SUPPRESSED.
        2. **Policy** — ``RESTART`` / ``BACKOFF(delay)`` / ``PAUSED``.
        3. **Cancellable backoff** — a halt (fleet OR account) or shutdown
           arriving during the wait abandons the restart (SUPPRESSED).
        4. **Respawn** — ``spawn_with_outcome`` re-runs
           ``_phase_a_reserve_slot`` with a ``_RestartCarry`` so the terminal
           deployment row is reset to ``starting`` and the attempt is counted
           on the NEW per-spawn row UNDER the slot-reservation lock (carried
           forward from the prior row so the crash-loop ceiling survives the
           respawn — prior-review P0/P1/P2). The restarted node verifies IB
           reconciliation itself before accepting orders (subprocess
           ``wait_until_ready`` / ``reconciliation_startup_delay_secs=10``),
           and clears the counter via ``record_success`` once it reaches
           ``is_running`` + reconciled (``_mark_running``).
        """
        if self._restart_policy is None:
            return _RestartOutcome.SUPPRESSED

        ctx = await self._load_restart_context(deployment_id)
        if ctx is None:
            log.info(
                "auto_restart_no_context",
                extra={
                    "deployment_id": str(deployment_id),
                    "restart_decision": "no_context",
                    "note": (
                        "deployment/node-process row gone — account_id "
                        "unavailable (nothing to restart)"
                    ),
                },
            )
            return _RestartOutcome.SUPPRESSED

        # 1. Halt gate FIRST — never queue a restart for a halted/drained
        # account or under a fleet kill-all. DELIBERATE suppression (the
        # account is intentionally halted) → terminal, never retried.
        if await self._halt_active(ctx.account_id):
            log.info(
                "auto_restart_suppressed_by_halt",
                extra={
                    "account_id": ctx.account_id,
                    "deployment_id": str(deployment_id),
                    "restart_decision": "suppressed_by_halt",
                },
            )
            return _RestartOutcome.SUPPRESSED

        # 1b. Operator-stop gate (FINDING 1, P1) — never fight an operator
        # /stop. A plain /stop sets NO halt latch, so the halt gate above can't
        # see it; instead the /stop handler stamps ``stop_requested_at`` on the
        # latest (failed-with-pending-restart) row. Re-read that durable intent
        # UNDER ``FOR UPDATE`` here so a /stop that committed AFTER the reaper
        # dispatched this restart aborts the respawn. DELIBERATE suppression →
        # terminal, never retried (resurrecting an operator-stopped node would
        # let it submit fresh real-money orders for a stopped account).
        if await self._operator_stop_requested(deployment_id):
            log.info(
                "auto_restart_suppressed_by_operator_stop",
                extra={
                    "account_id": ctx.account_id,
                    "deployment_id": str(deployment_id),
                    "restart_decision": "suppressed_by_operator_stop",
                    "note": (
                        "operator /stop recorded stop_requested_at on the latest "
                        "row; aborting the pending auto-restart (never resurrect a "
                        "node the operator stopped)"
                    ),
                },
            )
            return _RestartOutcome.SUPPRESSED

        # 2. Bounded policy decision. ``decide`` reads the PRIOR (terminal)
        # row's counter; if the ceiling already tripped ``auto_restart_paused``
        # it returns PAUSED and we never respawn (an operator must intervene).
        # DELIBERATE suppression → terminal, never retried (no crash-loop churn).
        decision = self._restart_policy.decide(ctx.process)
        if decision.action is RestartAction.PAUSED:
            log.warning(
                "auto_restart_paused",
                extra={
                    "account_id": ctx.account_id,
                    "deployment_id": str(deployment_id),
                    "restart_decision": "paused",
                    "reason": decision.pause_reason,
                    "consecutive_respawn_failures": ctx.process.consecutive_respawn_failures,
                },
            )
            return _RestartOutcome.SUPPRESSED

        # 3. Backoff handling (BACKOFF only). RESTART proceeds at once.
        #
        # PR 2 F2 — ``skip_backoff`` (the periodic re-scan path): a BACKOFF
        # candidate is SKIPPED (no sleep) rather than blocking the caller. The
        # rescan iterates ALL candidates serially, so sleeping the (up-to-300s)
        # cancellable backoff inline here would stall evaluation of every LATER
        # candidate — leaving unrelated accounts flat-and-unmonitored. Skipping
        # treats this pass as "not yet"; the NEXT periodic rescan re-evaluates
        # once the backoff window has elapsed (and typically RESTARTs then). The
        # crash-looping deployment the reaper OBSERVED owns its own detached
        # restart task, which DOES perform the backoff — so the deployment is
        # still paced; the rescan just declines to block on it.
        #
        # Otherwise (the reaper's detached restart task), perform the CANCELLABLE
        # backoff: a halt/shutdown arriving during the wait is a DELIBERATE
        # abandon → terminal (the next halt-gated decision re-evaluates if it
        # clears).
        if decision.action is RestartAction.BACKOFF:
            if skip_backoff:
                log.info(
                    "auto_restart_rescan_skipped_backoff",
                    extra={
                        "account_id": ctx.account_id,
                        "deployment_id": str(deployment_id),
                        "restart_decision": "rescan_skip_backoff",
                        "backoff_delay_s": decision.delay_s,
                        "note": (
                            "candidate is in its RestartPolicy backoff window; the "
                            "re-scan SKIPS it (no inline sleep) so it never blocks "
                            "evaluation of the other candidates — the next rescan "
                            "pass retries it once the backoff elapses"
                        ),
                    },
                )
                return _RestartOutcome.SUPPRESSED
            proceed = await self._cancellable_backoff(decision.delay_s or 0.0, ctx.account_id)
            if not proceed:
                log.info(
                    "auto_restart_backoff_abandoned",
                    extra={
                        "account_id": ctx.account_id,
                        "deployment_id": str(deployment_id),
                        "restart_decision": "backoff_abandoned",
                        "note": "fleet/account halt or supervisor shutdown during backoff",
                    },
                )
                return _RestartOutcome.SUPPRESSED

            # 3b. Re-check the operator-stop intent AFTER the backoff (FINDING 1,
            # P1). The cancellable backoff abandons on a fleet/account HALT, but
            # a plain /stop sets NO halt latch — so an operator /stop that
            # committed ``stop_requested_at`` DURING the (up-to-300s) backoff is
            # invisible to ``_cancellable_backoff``. Re-read the durable intent
            # here, immediately before the respawn, so the operator stop still
            # wins the race against a backed-off restart.
            if await self._operator_stop_requested(deployment_id):
                log.info(
                    "auto_restart_suppressed_by_operator_stop",
                    extra={
                        "account_id": ctx.account_id,
                        "deployment_id": str(deployment_id),
                        "restart_decision": "suppressed_by_operator_stop",
                        "note": (
                            "operator /stop recorded stop_requested_at DURING the "
                            "backoff; aborting the pending auto-restart before respawn"
                        ),
                    },
                )
                return _RestartOutcome.SUPPRESSED

        # 4. Respawn through the standard spawn path with a ``_RestartCarry``.
        # The attempt is counted INSIDE ``_phase_a_reserve_slot`` (carried
        # onto the new row, under the slot-reservation lock) — NOT here —
        # so the count happens exactly once and only when a slot is genuinely
        # reserved. The partial unique index serialises any re-scan /
        # PEL-recovery duplicate (the loser hits ``ALREADY_ACTIVE`` and ACKs
        # idempotently without counting — prior-review P2).
        carry = _RestartCarry(
            prior_consecutive_respawn_failures=ctx.process.consecutive_respawn_failures,
            prior_last_restart_at=ctx.process.last_restart_at,
            prior_auto_restart_paused=ctx.process.auto_restart_paused,
            prior_auto_restart_pause_reason=ctx.process.auto_restart_pause_reason,
        )
        # Item 3 (own-by-id): collect the row id THIS respawn reserves so the
        # transient-failure cleanup re-fails the row THIS attempt created — never
        # "the latest row" (a concurrent rescan / operator-retry can insert a
        # newer active row across the await boundaries inside spawn_with_outcome).
        reserved_rows: list[UUID] = []
        ack, process_started = await self.spawn_with_outcome(
            deployment_id=ctx.deployment_id,
            deployment_slug=ctx.deployment_slug,
            payload={
                "deployment_slug": ctx.deployment_slug,
                "gateway_session_key": ctx.gateway_session_key,
            },
            idempotency_key=f"auto-restart:{uuid_module.uuid4()}",
            gateway_session_key=ctx.gateway_session_key,
            restart_carry=carry,
            _reserved_row_sink=reserved_rows,
        )
        if not process_started:
            # No process (re)started. Two distinct shapes, distinguished by
            # ``ack`` (council #4 OPT C Part 2):
            #
            #   - ``ack is False`` → TRANSIENT no-ACK (``CONCURRENT_STARTUP`` /
            #     transient post-Phase-A payload-factory blip). Expected to
            #     succeed on a retry once the momentary condition clears, so the
            #     fast per-path retry (``_run_restart_task``) RE-DRIVES it. The
            #     periodic rescan is the backstop if the fast retries give up.
            #     We STILL re-fail the stranded ``starting`` row here (idempotent;
            #     only flips a still-non-terminal row) so a between-retries
            #     window — or the rescan backstop — sees a rescannable ``failed``
            #     row rather than a phantom "coming up" (prior-review P2).
            #
            #   - ``ack is True`` → ACK-drop: a DELIBERATE terminal suppression
            #     (halt raced post-decision / terminal / paused / payload
            #     PERMANENT) OR the idempotent ``ALREADY_ACTIVE`` race-loser.
            #     These happen BEFORE the Phase-A reset so the deployment is
            #     already terminal — NOT re-failed (would be a redundant write
            #     against a possibly-concurrent operator action) and NOT retried
            #     (retrying a deliberate suppression would churn it).
            if not ack:
                # Own-by-id: target the row THIS attempt reserved (if any). On a
                # CONCURRENT_STARTUP / NO_DEPLOYMENT no-ACK Phase A reserved
                # nothing → the sink is empty → owned_row_id=None falls back to the
                # legacy latest-row behaviour (there is no concurrent-restart row
                # to protect because this attempt created none).
                reserved = reserved_rows[-1] if reserved_rows else None
                await self._refail_stranded_restart(deployment_id, owned_row_id=reserved)
            log.info(
                "auto_restart_no_process_started",
                extra={
                    "account_id": ctx.account_id,
                    "deployment_id": str(deployment_id),
                    "restart_decision": "no_process_started",
                    "transient_no_ack": not ack,
                    "note": "spawn did not start a process (ack-drop, transient, or race loser)",
                },
            )
            return _RestartOutcome.TRANSIENT_NO_ACK if not ack else _RestartOutcome.SUPPRESSED

        log.warning(
            "auto_restart_respawning",
            extra={
                "account_id": ctx.account_id,
                "deployment_id": str(deployment_id),
                "restart_decision": "respawning",
                "gateway_session_key": ctx.gateway_session_key,
                "prior_consecutive_respawn_failures": ctx.process.consecutive_respawn_failures,
            },
        )
        return _RestartOutcome.RESTARTED

    async def _refail_stranded_restart(
        self, deployment_id: UUID, *, owned_row_id: UUID | None = None
    ) -> None:
        """Flip the OWNED node-process row that a TRANSIENT respawn stranded at
        ``starting`` back to ``failed`` so the deployment stays rescannable
        (prior-review P2 / council 2026-05-31 item 3).

        Only the reaper / re-scan auto-restart path reaches here — there is no
        PEL entry behind a direct ``spawn_with_outcome`` call, so a transient
        no-ACK outcome (the PEL-retry shape, returned AFTER Phase A reset the
        deployment to ``starting``) has no caller to re-drive it. Without this
        the deployment lingers at ``starting`` with no live node until the
        30-min startup watchdog clears it.

        Council 2026-05-31 (item 3) — OWN-BY-ROW-ID, not "the latest row". The
        chairman confirmed the old latest-row select is unsafe: after Phase A
        creates the row, ``spawn_with_outcome`` crosses ``await`` boundaries
        (halt re-checks, payload factory, ``process.start()``) and may mark the
        row ``failed`` before this method runs; a concurrent periodic rescan /
        operator-retry can create a NEWER active row in that window, which a
        ``started_at DESC`` select would then wrongly clobber. ``owned_row_id``
        is the row THIS attempt reserved (threaded via ``spawn_with_outcome``'s
        ``_reserved_row_sink``); act ONLY on it.

        FINDING 2 (P2) — ``owned_row_id is None`` is a strict NO-OP. When this
        attempt reserved NO row (``CONCURRENT_STARTUP`` / ``NO_DEPLOYMENT`` /
        a pre-Phase-A payload failure), there is NOTHING this attempt owns to
        clean up. The old ``started_at DESC LIMIT 1`` fallback was unsafe: a
        CONCURRENT operator-retry / periodic rescan can have created a fresh
        ``starting`` / ``running`` row that becomes "the latest" — and the
        fallback would then flip that LIVE row (and re-fail the deployment) with
        the child still running. So when no row was reserved we SKIP both the row
        mutation and the deployment re-fail entirely and return; the genuine
        backstops (startup watchdog, periodic reconciling rescan) handle any
        actually-stranded state.

        Idempotent and concurrency-safe: the owned row is flipped ONLY while
        still in a non-terminal active status — so a row that ``_mark_failed``
        already failed (the common transient cleanup) is left untouched, and a
        row a concurrent ``_mark_running`` already promoted is NOT clobbered. The
        parent deployment is re-failed ONLY if no NEWER active row exists (a
        concurrent restart already revived it). Council 2026-06-01 LOCK-ORDER
        MIGRATION: the deployment ``FOR UPDATE`` lock is acquired FIRST, BEFORE
        the owned node-row lock (deployment-then-node — the global invariant; no
        node→deployment edge). Best-effort:
        a second DB blip here is logged, not raised — the startup watchdog
        remains the ultimate backstop and the reaper loop is exception-guarded
        regardless (prior-review P1).
        """
        # FINDING 2 (P2): this attempt reserved NO row → it owns nothing to
        # clean up. NEVER fall back to "the latest row" — a concurrent
        # operator-retry / rescan may have created a fresh active row that
        # the fallback would wrongly clobber (with the child still running).
        # Strict no-op; the backstops handle any genuinely-stranded state.
        if owned_row_id is None:
            log.info(
                "refail_stranded_no_owned_row_skipped",
                extra={
                    "deployment_id": str(deployment_id),
                    "outcome": "TRANSIENT_NO_ACK",
                    "note": (
                        "no row reserved (CONCURRENT_STARTUP / NO_DEPLOYMENT / "
                        "pre-Phase-A payload failure); nothing owned to clean up "
                        "— skipped the re-fail entirely so a concurrent fresh "
                        "active row is never clobbered"
                    ),
                },
            )
            return
        try:
            async with self._db() as session, session.begin():
                # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST):
                # lock the parent ``LiveDeployment`` row FIRST, THEN the owned
                # node row — matching the global invariant ``advisory(gateway) →
                # live_deployments FOR UPDATE → live_node_processes FOR UPDATE``.
                # The prior order locked node-then-deployment (a node→deployment
                # edge that could cycle with the operator-/stop path). The
                # owned-row case below still targets the SPECIFIC row by id, so
                # the re-order does NOT weaken the council-#5 own-by-id targeting.
                deployment = (
                    await session.execute(
                        select(LiveDeployment)
                        .where(LiveDeployment.id == deployment_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                row = (
                    await session.execute(
                        select(LiveNodeProcess)
                        .where(LiveNodeProcess.id == owned_row_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                # REVALIDATE: act ONLY on the owned row, and only while it is
                # still in a non-terminal active status (so a row a concurrent
                # ``_mark_running`` already promoted, or that ``_mark_failed``
                # already failed, is left untouched).
                if row is not None and row.status in (
                    "starting",
                    "building",
                    "ready",
                    "running",
                ):
                    row.status = "failed"
                    row.failure_kind = FailureKind.SPAWN_FAILED_TRANSIENT.value
                    row.error_message = (
                        "auto-restart respawn hit a transient failure; "
                        "re-failed so the next rescan can retry"
                    )

                # Re-fail the deployment ONLY if no NEWER active row exists — a
                # concurrent rescan / operator-retry that started a fresh active
                # row (later ``started_at``) has already revived the deployment.
                # ``owned_row_id`` is provably non-None here (the
                # ``owned_row_id is None`` early-return above guarantees it), so
                # this guard only needs to check the owned row was found.
                newer_active_exists = False
                if row is not None:
                    newer_active_exists = (
                        await session.execute(
                            select(LiveNodeProcess.id)
                            .where(
                                LiveNodeProcess.deployment_id == deployment_id,
                                LiveNodeProcess.started_at > row.started_at,
                                LiveNodeProcess.status.in_(
                                    ("starting", "building", "ready", "running", "stopping")
                                ),
                            )
                            .limit(1)
                        )
                    ).first() is not None

                if newer_active_exists:
                    log.info(
                        "giveup_yielded_to_newer_active",
                        extra={
                            "deployment_id": str(deployment_id),
                            "owned_row_id": str(owned_row_id) if owned_row_id is not None else None,
                            "note": (
                                "transient-respawn re-fail yielded: a newer active node "
                                "row exists (concurrent restart) — did NOT re-fail the "
                                "deployment so it doesn't clobber the live node"
                            ),
                        },
                    )
                # Sync the parent deployment so /live/status stops showing
                # "coming up" and the re-scan candidate query (which requires
                # deployment.status == 'failed') can pick it up.
                elif deployment is not None and deployment.status in (
                    "starting",
                    "building",
                    "ready",
                    "running",
                ):
                    deployment.status = "failed"
            log.warning(
                "auto_restart_transient_respawn_refailed",
                extra={
                    "deployment_id": str(deployment_id),
                    "owned_row_id": str(owned_row_id) if owned_row_id is not None else None,
                },
            )
        except Exception:  # noqa: BLE001 — best-effort; watchdog is the backstop
            log.exception(
                "auto_restart_refail_stranded_failed",
                extra={
                    "deployment_id": str(deployment_id),
                    "note": ("deployment may stay 'starting' until the startup watchdog clears it"),
                },
            )

    async def _load_rescan_candidates(self) -> list[UUID]:
        """Return deployment_ids of dead nodes eligible for a startup restart.

        Closes the gap where a node died while the supervisor was DOWN: the
        fresh supervisor's :attr:`node_handle_cache` is empty so the reaper's
        ``_on_child_exit`` never fires. Two candidate classes (prior-review
        P1 — the re-scan previously only matched ``failed`` rows and so MISSED
        the exact scenario it exists for):

        1. **Terminal-failed (recoverable runtime crash)** — the latest
           node-process row is ``failed`` AND the deployment row is ``failed``
           (the reaper or HeartbeatMonitor already observed the exit) AND its
           ``failure_kind`` is a RECOVERABLE runtime crash
           (:meth:`FailureKind.is_recoverable_crash` — NODE_CRASHED,
           RECONCILIATION_FAILED, HEARTBEAT_TIMEOUT, BUILD_TIMEOUT,
           SPAWN_FAILED_TRANSIENT, or NULL→UNKNOWN). A pre-spawn / never-ran
           PERMANENT failure (SPAWN_FAILED_PERMANENT, a registry kind) or a
           halt-blocked START (HALT_ACTIVE / ACCOUNT_HALT_ACTIVE) is EXCLUDED
           (PR 2 / F2): those nodes NEVER ran — re-driving them would churn a
           permanent error up to the ceiling, or re-trade a halt-blocked
           account after the halt clears. Such failures are operator-START
           concerns; the operator fixes config (or re-issues after /resume).
           This is the SAME ``is_recoverable_crash`` predicate the reaper
           (``_on_child_exit``) gates its dispatch on, so the two AGREE.
        2. **Stale-active / stale-starting** (the supervisor-was-DOWN case) —
           the latest node-process row is still in an active status (``running``
           / ``ready`` / ``building`` / ``starting``) — the supervisor never
           observed the exit, so the row was never flipped to ``failed`` — AND
           its ``last_heartbeat_at`` is older than :attr:`_rescan_stale_seconds`.
           ``starting`` is included (PR 2 / F1): a Phase-A-inserted ``starting``
           row whose supervisor/container died BEFORE the child self-wrote
           ``building`` is recovered by NOTHING otherwise — ``watchdog_once`` is
           host-scoped (skips the dead container's host), ``HeartbeatMonitor``
           excludes startup statuses, and the rescan previously skipped
           ``starting`` — so the partial-unique active ``starting`` row would be
           stuck FOREVER, blocking all future starts for that deployment. The
           staleness threshold is what makes this safe: a node legitimately
           mid-startup RIGHT NOW (fresh heartbeat) is left ALONE; only an
           orphaned ``starting`` whose heartbeat has not progressed past the
           threshold is reaped. Without this class the re-scan also reads 0
           candidates at boot (it races the HeartbeatMonitor's first sweep with
           no ordering guarantee) and the dead node stays flat-and-unmonitored.

        Eligibility filtering is split by phase (FIX 1, P1 + council 2026-05-31
        F5 — FLIP-BUT-SUPPRESS): the Step-1 CLEANUP flip (transition a dead
        stale-active row to ``failed`` so it leaves the partial-unique active
        set) is UNCONDITIONAL on BOTH ``auto_restart_paused`` AND
        ``stop_requested_at`` — a dead PAUSED or operator-/stopped row would
        otherwise stay stuck-active forever (a dead node masquerading as
        ``running``/``starting`` on the real-money dashboard, blocking future
        starts). The Step-2 RESPAWN candidate select STILL gates
        ``auto_restart_paused=False`` AND ``stop_requested_at IS NULL`` (and a
        recoverable kind), so a flipped-but-paused or flipped-but-stopped row
        leaves the active set but is NEVER respawned — the don't-resurrect intent
        is preserved; only the terminal state changes. They are
        deliberately NOT host-scoped (Codex P1): in the Docker Compose
        deployment the ``live-supervisor`` container has no stable hostname, so
        a container recreate gives the RETURNING supervisor a fresh
        ``socket.gethostname()`` while the orphaned ``live_node_processes`` rows
        still carry the OLD (now-dead) container's hostname. A host predicate
        would EXCLUDE exactly the stale-active / failed orphan rows the re-scan
        exists to recover — leaving the account failed + flat-and-unmonitored
        until manual intervention. The de-scoping is safe because the deploy
        contract guarantees a SINGLE supervisor (F4: routine deploy excludes
        the broker profile; F5: deploy refuses while any live deployment is
        active) — there is no second supervisor to split-brain with, and after
        a container recreate ALL prior node processes are dead (the old
        container is gone), so every active/stale/failed row IS an orphan this
        supervisor must recover.

        NOTE: the re-SPAWN rescan is host-agnostic, but the paths that signal a
        live OS pid stay host-scoped — :meth:`watchdog_once` (SIGKILL-by-pid of
        a wedged build) and :meth:`stop` (pid-fallback SIGTERM) only act on a
        pid that must be on THIS host. The re-scan never signals a pid; it only
        re-evaluates rows for a fresh spawn via :meth:`_maybe_auto_restart`,
        which loads its own fresh terminal-row context and resets a terminal
        deployment to ``starting`` before the respawn.

        The query reads only the latest node-process row per deployment (a
        correlated ``NOT EXISTS`` on a newer row for the same deployment) so a
        deployment with an OLD stale row but a NEWER active one is judged on
        the new row, not resurrected off a stale historical record.

        For class-2 (stale-active) rows we FIRST transition the stale row to
        ``failed`` (+ sync the deployment to ``failed``) in the same pass —
        exactly what the HeartbeatMonitor would do — because otherwise Phase A
        of the respawn would see the still-``running`` row as ACTIVE and
        return ``ALREADY_ACTIVE`` (an idempotent no-op), and the dead node
        would never restart. Doing the transition here removes the dependency
        on the HeartbeatMonitor's first sweep (which the re-scan would
        otherwise race with no ordering guarantee — the original P1).

        kill-all → /resume → supervisor-restart (PR 2 / F2 — now PRINCIPLED,
        previously rated a P3 known-limitation). ``/kill-all`` halts a RUNNING
        node via the FLEET HALT LATCH; that running node crashes terminal with a
        RECOVERABLE kind (NODE_CRASHED / HEARTBEAT_TIMEOUT — it RAN), NOT with a
        halt-blocked HALT_ACTIVE kind. A deployment whose START was BLOCKED by
        the halt (it never ran) is marked HALT_ACTIVE / ACCOUNT_HALT_ACTIVE,
        which ``is_recoverable_crash`` now EXCLUDES — so after the operator
        ``/resume``\\s (clearing the latch), the rescan will NOT auto-start a
        deployment whose start was halt-blocked and never ran; the OPERATOR
        re-issues that start. A deployment that genuinely RAN under a kill-all
        and then crashed is still recovered once the halt clears (consistent
        with US-2 self-heal of a now-unhalted account). This is the F2
        eligibility split doing exactly what it should; there is no residual
        known-limitation here.
        """
        from sqlalchemy import and_, exists, not_, or_, update

        from msai.services.live.failure_kind import _PRE_SPAWN_NEVER_RAN_KINDS

        # NOTE (Codex P1): intentionally NOT host-scoped — see the docstring.
        # The re-scan must recover orphan rows carrying a DEAD container's
        # hostname after a container recreate, which a ``lnp.host == ...``
        # predicate would exclude.
        stale_cutoff = datetime.now(UTC) - timedelta(seconds=self._rescan_stale_seconds)
        lnp = LiveNodeProcess
        newer = LiveNodeProcess.__table__.alias("lnp_newer")
        async with self._db() as session, session.begin():
            # Latest-row-per-deployment predicate, reused for both the
            # stale-active transition and the candidate select.
            is_latest = ~exists().where(
                and_(
                    newer.c.deployment_id == lnp.deployment_id,
                    newer.c.started_at > lnp.started_at,
                )
            )

            # The active set the rescan reaps as STALE orphans. Includes
            # ``starting`` (PR 2 / F1): a Phase-A-inserted ``starting`` row
            # whose supervisor/container died before the child self-wrote
            # ``building`` is recovered by nothing else (watchdog is
            # host-scoped, HeartbeatMonitor excludes startup statuses), so a
            # stale ``starting`` orphan would block all future starts for that
            # deployment FOREVER via the partial-unique active index. The
            # staleness threshold (``last_heartbeat_at < stale_cutoff``) below
            # is what keeps a node legitimately mid-startup RIGHT NOW (fresh
            # heartbeat) SAFE — only an orphan whose heartbeat has not advanced
            # past the threshold is flipped. ``stopping`` is intentionally NOT
            # included: a ``stopping`` row is an operator /stop in flight
            # (``stop_requested_at`` set), recovered by the stop path / its
            # durable intent, never auto-restarted.
            stale_active_statuses = ("starting", "building", "ready", "running")
            # Step 1 — transition stale-ACTIVE latest rows (the supervisor was
            # DOWN when they died, so their exit was never observed) to
            # ``failed`` so they leave the active set and the respawn's Phase A
            # doesn't see them as ALREADY_ACTIVE. Capture their deployment_ids.
            stale_active_dep_ids = (
                (
                    await session.execute(
                        select(lnp.deployment_id).where(
                            # FIX 1 (P1): the Step-1 CLEANUP flip is UNCONDITIONAL
                            # on ``auto_restart_paused``. A DEAD stale-active row
                            # that carries ``auto_restart_paused=True`` (the
                            # ceiling-tripping restart attempt died while the
                            # supervisor/container was DOWN before ``_mark_running``
                            # cleared the latch) must STILL be flipped to ``failed``
                            # so it leaves the partial-unique active set — otherwise
                            # it stays stuck-active forever, blocking every future
                            # manual /start and showing active on /live/status. The
                            # invariant: CLEANUP of a dead row is unconditional;
                            # respawn-ELIGIBILITY (pause) is the separately-gated
                            # decision, enforced by Step 2 below (which still gates
                            # ``auto_restart_paused.is_(False)``). So a flipped-but-
                            # paused row leaves the active set but is NOT respawned.
                            #
                            # Council 2026-05-31 (F5 — FLIP-BUT-SUPPRESS): the
                            # Step-1 CLEANUP flip is ALSO unconditional on
                            # ``stop_requested_at`` — a DEAD stale-active row whose
                            # operator-/stop intent is set must STILL be flipped to
                            # ``failed`` so it leaves the partial-unique active set.
                            # Otherwise a dead node masquerades as ``running`` /
                            # ``starting`` on the real-money dashboard forever (the
                            # host-scoped watchdog can't reach an orphan carrying a
                            # DEAD container's hostname after a container recreate).
                            # The don't-resurrect INTENT is preserved by Step 2,
                            # which STILL gates ``stop_requested_at IS NULL`` — so a
                            # flipped-but-stopped row leaves the active set but is
                            # NEVER respawned. CLEANUP of a dead row is
                            # unconditional; respawn-eligibility (stop intent /
                            # pause) is the separately-gated Step-2 decision.
                            lnp.status.in_(stale_active_statuses),
                            lnp.last_heartbeat_at < stale_cutoff,
                            is_latest,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if stale_active_dep_ids:
                # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST) +
                # Finding 2 (NO STALE-SNAPSHOT SKEW). ``stale_active_dep_ids`` came
                # from a PLAIN unlocked SELECT above (no row lock) and tells us
                # which ``live_deployments`` rows to LOCK. Two invariants held
                # SIMULTANEOUSLY by the RETURNING-under-deployment-lock pattern:
                #
                # - Deadlock-freedom: lock the deployment rows FOR UPDATE FIRST,
                #   THEN the node rows (node locks acquired AFTER deployment locks
                #   → no node→deployment edge; cannot cycle with the
                #   deployment-first stop / give-up / Phase-A paths).
                # - No skew: the PRIOR fix flipped deployments from the UNLOCKED
                #   ``stale_active_dep_ids`` SELECT, then flipped nodes WHERE
                #   <stale predicate>. A heartbeat landing between the SELECT and
                #   the node flip made the node UPDATE match ZERO rows while the
                #   deployment was already flipped → a live (``running``/
                #   ``starting``) node paired with a ``failed`` deployment (hidden
                #   from active-only status/deploy gates). We now derive the
                #   deployment flip from the node UPDATE's ``RETURNING
                #   deployment_id`` set, so ONLY deployments whose node was ACTUALLY
                #   terminalized are flipped.
                #
                # Step 1a — lock the candidate ``live_deployments`` rows FOR UPDATE
                # FIRST (deployment-first; the node UPDATE below takes its locks
                # after these).
                #
                # INVARIANT 2 (council 2026-06-01 follow-up — DETERMINISTIC TOTAL
                # ORDER, pr-toolkit P1): ``ORDER BY LiveDeployment.id`` so this
                # multi-row deployment ``FOR UPDATE`` shares the SAME global row-
                # lock order as the HeartbeatMonitor sweep and the watchdog Step-3
                # pre-lock. The deployment-first invariant rules out D→N→D cycles
                # but NOT a D↔D (AB-BA) deadlock between two concurrent multi-row
                # deployment-set lockers with overlapping sets (rescan × heartbeat
                # sweep, rescan × watchdog). Ascending-id ordering gives them one
                # total order so they serialise rather than cycle.
                await session.execute(
                    select(LiveDeployment.id)
                    .where(LiveDeployment.id.in_(stale_active_dep_ids))
                    .order_by(LiveDeployment.id)
                    .with_for_update()
                )
                # Step 1b — flip the stale node rows (node locks, AFTER the
                # deployment locks) and RETURN the deployment_ids ACTUALLY flipped.
                # Re-applies the SAME stale predicate (incl. ``is_latest``) so a row
                # that advanced its heartbeat between the Step-0 read and here is
                # NOT flipped and does NOT appear in the RETURNING set — closing the
                # stale-snapshot skew.
                flipped_active_dep_ids = (
                    (
                        await session.execute(
                            update(lnp)
                            .where(
                                lnp.deployment_id.in_(stale_active_dep_ids),
                                lnp.status.in_(stale_active_statuses),
                                lnp.last_heartbeat_at < stale_cutoff,
                                is_latest,
                            )
                            .values(
                                status="failed",
                                error_message=("rescan: stale heartbeat (supervisor was down)"),
                                failure_kind=FailureKind.HEARTBEAT_TIMEOUT.value,
                            )
                            .returning(lnp.deployment_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if flipped_active_dep_ids:
                    # Step 1c — sync ONLY the deployments whose node was ACTUALLY
                    # flipped (the RETURNING set), under the locks taken in Step 1a.
                    # The non-terminal guard preserves the X3 semantics and never
                    # stomps a concurrent /stop's terminal state.
                    await session.execute(
                        update(LiveDeployment)
                        .where(
                            LiveDeployment.id.in_(list(flipped_active_dep_ids)),
                            LiveDeployment.status.in_(
                                ("starting", "building", "ready", "running", "stopping")
                            ),
                        )
                        .values(status="failed")
                    )
                    log.warning(
                        "auto_restart_rescan_flipped_stale_active",
                        extra={
                            "deployment_ids": [str(d) for d in flipped_active_dep_ids],
                            "stale_seconds": self._rescan_stale_seconds,
                        },
                    )

            # PR 2 / F2 — restrict the terminal-``failed`` candidate set to
            # RECOVERABLE runtime crashes. The SAME ``is_recoverable_crash``
            # predicate the reaper gates its dispatch on (so the two AGREE),
            # expressed in SQL via the pre-spawn / never-ran EXCLUSION set: a
            # node whose START never RAN (SPAWN_FAILED_PERMANENT, a registry
            # kind) or whose START was halt-blocked (HALT_ACTIVE /
            # ACCOUNT_HALT_ACTIVE) is EXCLUDED — re-driving it would churn a
            # permanent error to the ceiling or re-trade a halt-blocked account
            # after the halt clears. A NULL ``failure_kind`` (the genuine
            # outage-window crash: ``_mark_terminal`` wrote ``failed`` with no
            # observed kind → UNKNOWN) is INCLUDED (recoverable) — US-2 self-heal
            # must recover it; the RestartPolicy ceiling brakes a loop.
            non_recoverable_kind_values = sorted(k.value for k in _PRE_SPAWN_NEVER_RAN_KINDS)
            recoverable_kind_filter = or_(
                lnp.failure_kind.is_(None),
                not_(lnp.failure_kind.in_(non_recoverable_kind_values)),
            )

            # Step 2 — now select ALL restart candidates: the latest row is
            # ``failed`` AND the deployment is ``failed`` AND not paused AND a
            # recoverable runtime-crash kind. After step 1 the freshly-flipped
            # stale-active rows (HEARTBEAT_TIMEOUT — recoverable) are included.
            rows = (
                (
                    await session.execute(
                        select(lnp.deployment_id)
                        .join(LiveDeployment, LiveDeployment.id == lnp.deployment_id)
                        .where(
                            lnp.auto_restart_paused.is_(False),
                            # Council #3 F5: durable operator-stop intent
                            # suppresses the re-scan restart path (mirrors the
                            # reaper's stop_requested_at suppression).
                            lnp.stop_requested_at.is_(None),
                            lnp.status == "failed",
                            LiveDeployment.status == "failed",
                            recoverable_kind_filter,
                            is_latest,
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def rescan_for_restart(self) -> int:
        """Re-evaluate dead deployments for auto-restart on supervisor start.

        Runs each candidate through the SAME halt-gate + bounded policy the
        reaper uses (:meth:`_maybe_auto_restart`) — the re-scan never bypasses
        the gate with its own restart path. Best-effort per candidate: one row
        raising must not strand the rest of the fleet. Returns the number of
        candidates that issued a respawn. A no-op (returns 0) when no policy
        was injected.

        PR 2 F2 (review P2) — NON-BLOCKING per candidate. The re-scan invokes
        :meth:`_maybe_auto_restart` with ``skip_backoff=True`` so a candidate
        whose ``RestartPolicy`` decision is BACKOFF is SKIPPED (treated as a
        no-respawn-this-pass), NOT slept on. Previously the rescan awaited the
        cancellable backoff sleep INLINE (up to ``BACKOFF_CAP_S`` = 300s), so one
        crash-looping candidate sitting in its backoff window blocked the rescan
        from even EVALUATING every LATER candidate — leaving unrelated accounts
        flat-and-unmonitored for up to the backoff cap (the same per-account-
        isolation class as the iter-3 reaper-blocking fix, now in the rescan
        loop). Skipping (not sleeping) keeps the loop moving; the NEXT periodic
        rescan pass (every ~30s) re-evaluates the skipped candidate, and by then
        the backoff has typically elapsed → it RESTARTs. A genuinely-crashing
        deployment the reaper observed already owns its OWN detached restart task
        (which does perform the backoff); the rescan is the backstop for the
        supervisor-was-DOWN orphans the reaper never saw, and those self-heal on
        a later pass once the backoff window passes. The council-#4 rescan
        ELIGIBILITY (only recoverable crashes / stale-active orphans, paused /
        stop-intent excluded) is unchanged — it lives in
        :meth:`_load_rescan_candidates`.

        The re-scan AND per-account PEL recovery can both target the same dead
        deployment; the partial-unique-index inside ``spawn`` serialises them
        (the second hits ``ALREADY_ACTIVE`` and ACKs idempotently — no
        double-count). No separate distributed lock (Claude iter-2 P2-A).
        """
        if self._restart_policy is None:
            return 0
        try:
            candidates = await self._load_rescan_candidates()
        except Exception:  # noqa: BLE001 — re-scan is best-effort startup work
            log.exception("auto_restart_rescan_candidate_scan_failed")
            return 0

        restarted = 0
        for deployment_id in candidates:
            try:
                # skip_backoff=True: a BACKOFF candidate is SKIPPED (no inline
                # sleep), so one crash-looping account never blocks evaluation
                # of the others. The next rescan pass retries it.
                if await self._maybe_auto_restart(deployment_id, skip_backoff=True):
                    restarted += 1
            except Exception:  # noqa: BLE001 — one bad row must not strand the fleet
                log.exception(
                    "auto_restart_rescan_candidate_failed",
                    extra={"deployment_id": str(deployment_id)},
                )
        log.info(
            "auto_restart_rescan_complete",
            extra={"candidates": len(candidates), "restarted": restarted},
        )
        return restarted

    async def has_prior_operation_evidence(self) -> bool:
        """Return whether this install has DURABLE evidence that the supervisor
        has operated before (PR 2 / F3 — distinguish a genuine FIRST-EVER boot
        from an EXPIRED-after-outage boot).

        On boot the ``router_heartbeat`` Redis key is ``None`` in TWO distinct
        cases that the boot handshake must NOT conflate:

        - **First-ever boot** of a brand-new install — there has NEVER been a
          supervisor, so a missing heartbeat is expected and benign.
        - **Expired-after-outage** — a prior supervisor ran, then was down long
          enough (> the ~90s ``router_heartbeat`` TTL) for the key to EXPIRE.
          A missing heartbeat here is a real outage gap the SPOF detector must
          fire on; publishing a fresh stamp before the first alert eval would
          MASK it (the bug F3 fixes).

        The durable discriminator is the presence of ANY ``live_deployments`` /
        ``live_node_processes`` row: a brand-new install has none, whereas any
        install that has ever started a deployment has at least one. We probe
        ``live_node_processes`` first (a node row implies a deployment existed)
        and fall back to ``live_deployments`` so a deployment created but never
        started still counts as prior operation.

        Best-effort: a DB blip raises to the caller, which treats the probe as
        inconclusive and fails TOWARD firing the SPOF (over-alert once rather
        than mask an outage)."""
        from sqlalchemy import exists as sa_exists

        async with self._db() as session:
            node_seen = (
                await session.execute(select(sa_exists().where(LiveNodeProcess.id.isnot(None))))
            ).scalar()
            if node_seen:
                return True
            dep_seen = (
                await session.execute(select(sa_exists().where(LiveDeployment.id.isnot(None))))
            ).scalar()
            return bool(dep_seen)

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
            # Account-scoped logging (PR 2 T10): outer-join the parent
            # deployment's account_id onto the same stale-rows scan (one extra
            # column, same WHERE filters, same rows) so the watchdog's
            # supervision logs below carry account context. LEFT join so a row
            # whose deployment was deleted still surfaces (account_id None).
            stale_rows = (
                await session.execute(
                    select(LiveNodeProcess, LiveDeployment.account_id)
                    .outerjoin(LiveDeployment, LiveDeployment.id == LiveNodeProcess.deployment_id)
                    .where(
                        LiveNodeProcess.status.in_(("starting", "building")),
                        LiveNodeProcess.started_at < datetime.fromtimestamp(cutoff, UTC),
                        LiveNodeProcess.host == self_host,
                    )
                )
            ).all()

            # PR 2 F3 (review P2): collect the deployments whose wedged node the
            # watchdog kills so we can sync their parent ``LiveDeployment`` rows
            # to ``failed`` in the SAME transaction (below). Without this the
            # deployment lingers non-terminal (``starting``/``building``) — the
            # rescan candidate query requires ``LiveDeployment.status ==
            # 'failed'`` so a watchdog-killed node (no local reaper handle) is
            # MISSED by the bounded auto-restart, and /live/status shows the dead
            # deployment as still active.
            #
            # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST): the
            # SIGKILL loop below does NOT mutate the loaded node rows (the
            # ``stale_rows`` SELECT is unlocked + nothing is marked dirty), so no
            # node-row lock is acquired during the loop. We collect the killed row
            # + deployment ids, then sync the parent ``live_deployments`` rows
            # FIRST (deployment locks), then flip the node rows by id LAST (node
            # locks). The prior code mutated the node rows in the loop (autoflush
            # acquired node locks) BEFORE the deployment UPDATE — a node→deployment
            # edge that could cycle with the deployment-first stop / give-up /
            # Phase-A paths.
            killed_deployment_ids: list[UUID] = []
            killed_row_ids: list[UUID] = []
            killed_pid_by_row: dict[UUID, int | None] = {}

            for row, row_account_id in stale_rows:
                account_id = (row_account_id or "").strip() or None
                # Step 1: SIGKILL the pid (handle cache first, fall back
                # to row.pid for processes spawned BEFORE a supervisor
                # restart on the SAME host — same PID namespace, so
                # ``os.kill`` is meaningful). A wedged build won't
                # respond to SIGTERM — go straight to SIGKILL.
                pid_to_kill: int | None = None
                cached = self.node_handle_cache.get(row.deployment_id)
                handle = cached.proc if cached is not None else None
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
                                "account_id": account_id,
                                "deployment_id": str(row.deployment_id),
                                "row_id": str(row.id),
                                "db_status": row.status,
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
                                "account_id": account_id,
                                "deployment_id": str(row.deployment_id),
                                "db_status": row.status,
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
                                "account_id": account_id,
                                "deployment_id": str(row.deployment_id),
                                "db_status": row.status,
                                "pid": pid_to_kill,
                            },
                        )

                # Step 2: remember which rows to terminal-flip (the actual
                # bulk UPDATE happens AFTER the deployment sync below so the
                # lock order stays deployment-then-node). Do NOT mutate the ORM
                # row here — that would mark it dirty and autoflush a node-row
                # lock before the deployment UPDATE.
                killed_row_ids.append(row.id)
                killed_pid_by_row[row.id] = pid_to_kill
                killed_deployment_ids.append(row.deployment_id)

            # Step 3 (PR 2 F3): sync the parent ``LiveDeployment`` rows to
            # ``failed`` FIRST — the SAME X3 pattern as
            # ``HeartbeatMonitor._mark_stale_as_failed`` / ``_mark_failed``, and
            # the deployment-FIRST half of the council 2026-06-01 lock order. The
            # ``status.in_(<non-terminal>)`` guard mirrors the HeartbeatMonitor's
            # so a concurrent operator ``/stop`` (which moves the deployment to
            # ``stopping``/``stopped``) is NOT clobbered — only a deployment
            # still in a non-terminal startup/run state is flipped. This makes
            # the watchdog-killed node recoverable by the bounded auto-restart
            # rescan (its candidate query keys off ``LiveDeployment.status ==
            # 'failed'``) and shows it terminal on /live/status.
            if killed_deployment_ids:
                from sqlalchemy import update

                # INVARIANT 2 (council 2026-06-01 follow-up — DETERMINISTIC TOTAL
                # ORDER, pr-toolkit P1): PRE-LOCK the deployment rows the bulk
                # UPDATE below will write, in ascending-id order, FIRST. A bare
                # ``UPDATE live_deployments WHERE id IN (...)`` acquires the row
                # write locks in the UPDATE's scan order (NOT a deterministic
                # order) — so two watchdog/sweep/rescan passes with overlapping
                # deployment sets could lock {depA, depB} vs {depB, depA} and
                # AB-BA deadlock. An ordered ``SELECT ... ORDER BY id FOR UPDATE``
                # immediately before the UPDATE takes the locks in the SAME global
                # id order the HeartbeatMonitor sweep + rescan Step-1a use; the
                # subsequent UPDATE then re-locks ALREADY-HELD rows (no reorder).
                # The deployment-first invariant alone does not cover this D↔D
                # cycle (it only rules out D→N→D).
                await session.execute(
                    select(LiveDeployment.id)
                    .where(LiveDeployment.id.in_(killed_deployment_ids))
                    .order_by(LiveDeployment.id)
                    .with_for_update()
                )
                await session.execute(
                    update(LiveDeployment)
                    .where(
                        LiveDeployment.id.in_(killed_deployment_ids),
                        LiveDeployment.status.in_(
                            ("starting", "building", "ready", "running", "stopping")
                        ),
                    )
                    .values(status="failed")
                )
                # Step 4: now flip the node rows (node lock LAST). One bulk
                # UPDATE per (row, pid) group keeps the exit_code accurate.
                for killed_row_id in killed_row_ids:
                    pid_killed = killed_pid_by_row[killed_row_id]
                    await session.execute(
                        update(LiveNodeProcess)
                        .where(LiveNodeProcess.id == killed_row_id)
                        .values(
                            status="failed",
                            failure_kind=FailureKind.BUILD_TIMEOUT.value,
                            error_message=(
                                f"startup wedged for {self._startup_hard_timeout_s}s; "
                                f"watchdog SIGKILLed pid={pid_killed}"
                            ),
                            exit_code=-int(signal.SIGKILL) if pid_killed is not None else None,
                        )
                    )

        # Council 2026-06-01 (item 4): the ownerless-active-row backstop. Runs
        # AFTER the host-scoped wedged-build sweep above, in its own transaction.
        await self._reap_ownerless_active_rows()

    async def _reap_ownerless_active_rows(self) -> None:
        """Reap any ACTIVE node row that is provably ownerless (council
        2026-06-01, item 4 — mandatory defense-in-depth).

        This is the LAST-RESORT backstop that guarantees NO cancel/crash variant
        can permanently wedge a deployment's active unique-index slot. It catches
        the gap that the host-scoped startup watchdog and the rescan don't:

        - A row INSERTed by Phase A and left in ``starting`` (``pid IS NULL``)
          because the spawn task was cancelled / crashed in the
          reserved→pre-start window before ``process.start()`` ran or before
          Phase C wrote the pid — and whose owning supervisor is gone (no live
          cached handle). The cancellation cleanup in :meth:`spawn_with_outcome`
          terminalizes such a row on the happy path; this sweep is the safety net
          for the variant where even that cleanup never ran (hard kill of the
          supervisor mid-window).
        - A ``stopping`` row whose ``/stop`` SIGTERM never completed and whose
          supervisor died, leaving the slot held with no live process.

        Criteria for an OWNERLESS reap:
        - status in the ACTIVE set INCLUDING ``starting`` AND ``stopping`` (the
          two startup/teardown statuses the other authorities skip);
        - ``pid IS NULL`` — a row that never recorded a real OS pid (a row WITH a
          pid is the host-scoped watchdog's / reaper's territory, by pid);
        - no live cached handle for the deployment in :attr:`node_handle_cache`
          (a cached handle means THIS supervisor owns the row — the reap loop /
          Phase C own it, not this backstop);
        - ``last_heartbeat_at`` older than the bounded grace window
          (:attr:`_rescan_stale_seconds`) — a node legitimately mid-startup RIGHT
          NOW (fresh heartbeat, pid not yet written) is LEFT ALONE.

        HOST-AGNOSTIC (council 2026-06-01, mirrors the rescan rationale): the
        sweep does NOT scope to ``socket.gethostname()``. After a container
        recreate the returning supervisor has a fresh hostname while the orphan
        ``starting``/``stopping`` rows carry the DEAD container's hostname; a host
        predicate would EXCLUDE exactly the orphans this backstop exists to
        recover. The deploy contract guarantees a SINGLE supervisor (the broker
        profile is excluded from routine deploys; deploy refuses while a live
        deployment is active), and ``pid IS NULL`` means there is no live OS
        process to signal — so reaping a foreign-host ownerless row never races a
        real process.

        Terminal branch (the don't-resurrect intent is honored):
        - ``stop_requested_at`` set → flip to ``failed`` but leave the durable
          stop intent in place so the rescan candidate query (which gates
          ``stop_requested_at IS NULL``) NEVER respawns it — terminal, no respawn.
        - else → flip to ``failed`` with a RECOVERABLE kind
          (:attr:`FailureKind.HEARTBEAT_TIMEOUT`) so the rescan can re-drive it
          (a genuinely-orphaned recoverable start/stop should self-heal).

        Lock order: ``live_deployments FOR UPDATE`` FIRST, THEN the node row
        ``FOR UPDATE`` (the global invariant — no node→deployment edge). Emits a
        structured ``ownerless_active_row_reaped`` log per row reaped.
        """
        active_statuses = ("starting", "building", "ready", "running", "stopping")
        cutoff = datetime.now(UTC) - timedelta(seconds=self._rescan_stale_seconds)
        # PLAIN unlocked candidate scan (acquires no lock — adds no edge). We
        # exclude rows whose deployment has a LIVE cached handle (owned by THIS
        # supervisor's reap loop / Phase C), so the in-memory check is done
        # against the cache snapshot here before we take any lock.
        owned_deployment_ids = {
            dep_id for dep_id, cached in self.node_handle_cache.items() if cached.proc.is_alive()
        }
        async with self._db() as session:
            candidate_rows = (
                await session.execute(
                    select(LiveNodeProcess.id, LiveNodeProcess.deployment_id).where(
                        LiveNodeProcess.status.in_(active_statuses),
                        LiveNodeProcess.pid.is_(None),
                        LiveNodeProcess.last_heartbeat_at < cutoff,
                    )
                )
            ).all()
        # Snapshot the in-flight spawn set alongside the handle cache (Codex
        # iter-27 P1): exclude BOTH deployments with a live cached handle AND
        # rows THIS supervisor is actively spawning (reserved, pre-handle) — a
        # slow legitimate spawn must never be reaped out from under itself.
        in_flight_rows = set(self._in_flight_spawn_rows)
        candidates = [
            (row_id, dep_id)
            for (row_id, dep_id) in candidate_rows
            if dep_id not in owned_deployment_ids and row_id not in in_flight_rows
        ]
        if not candidates:
            return

        for row_id, deployment_id in candidates:
            try:
                async with self._db() as session, session.begin():
                    # Deployment FOR UPDATE FIRST (council 2026-06-01 invariant),
                    # then the node row FOR UPDATE.
                    deployment = (
                        await session.execute(
                            select(LiveDeployment)
                            .where(LiveDeployment.id == deployment_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    row = (
                        await session.execute(
                            select(LiveNodeProcess)
                            .where(LiveNodeProcess.id == row_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    # REVALIDATE under the locks: a row that advanced its
                    # heartbeat, got a pid, or already left the active set since
                    # the unlocked scan must NOT be reaped (the slot is genuinely
                    # in use / already terminal).
                    if (
                        row is None
                        or row.status not in active_statuses
                        or row.pid is not None
                        or (row.last_heartbeat_at is not None and row.last_heartbeat_at >= cutoff)
                    ):
                        continue
                    # A live cached handle appearing between the scan and here also
                    # means the row is owned — skip.
                    cached = self.node_handle_cache.get(deployment_id)
                    if cached is not None and cached.proc.is_alive():
                        continue

                    had_stop_intent = row.stop_requested_at is not None
                    age_s = (
                        (datetime.now(UTC) - row.last_heartbeat_at).total_seconds()
                        if row.last_heartbeat_at is not None
                        else None
                    )
                    prior_status = row.status
                    row.status = "failed"
                    row.error_message = (
                        "ownerless active row (pid IS NULL, no live handle, stale "
                        "heartbeat) reaped by the ownerless-row backstop"
                    )
                    # A RECOVERABLE kind in BOTH branches: when there is NO stop
                    # intent the rescan should re-drive the genuine orphan; when
                    # the stop intent IS set the rescan candidate query already
                    # excludes it via ``stop_requested_at IS NULL`` — so the stop
                    # INTENT (which we deliberately leave in place), not the kind,
                    # is the terminal-no-respawn suppressor. We never CLEAR the
                    # stop intent here, so a stop-intent row stays un-rescannable.
                    row.failure_kind = FailureKind.HEARTBEAT_TIMEOUT.value

                    # Sync the parent deployment to ``failed`` (under its lock,
                    # taken first) so /live/status reflects reality and the rescan
                    # candidate query (LiveDeployment.status == 'failed') matches.
                    # Don't clobber a concurrent /stop's terminal/stopping state.
                    if deployment is not None and deployment.status in active_statuses:
                        deployment.status = "failed"

                    log.warning(
                        "ownerless_active_row_reaped",
                        extra={
                            "deployment_id": str(deployment_id),
                            "row_id": str(row_id),
                            "prior_status": prior_status,
                            "pid": None,
                            "age_s": age_s,
                            "reason": (
                                "stop_intent_terminal_no_respawn"
                                if had_stop_intent
                                else "recoverable_rescan_eligible"
                            ),
                        },
                    )
            except Exception:  # noqa: BLE001 — best-effort backstop; never crash the watchdog
                log.exception(
                    "ownerless_active_row_reap_failed",
                    extra={"deployment_id": str(deployment_id), "row_id": str(row_id)},
                )

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

    async def push_flatness_request(
        self,
        deployment_id: UUID,
        *,
        stop_nonce: str,
        member_strategy_id_fulls: list[str],
    ) -> None:
        """RPUSH a flatness ticket onto ``flatness_pending:{deployment_id}``.

        Called by the supervisor handler when a STOP_AND_REPORT_FLATNESS
        command arrives, BEFORE invoking :meth:`stop` (which SIGTERMs the
        child). The child reads this list in its shutdown-finally hook
        and writes ``stop_report:{stop_nonce}`` for the API to GET.
        Stored as JSON so multiple concurrent stops queue cleanly
        (Codex iter-4 P2 #1 — per-nonce list, not singleton key).
        TTL 120s — long enough to outlive a slow shutdown, short enough
        to clean up after the request is abandoned.

        See ``docs/plans/2026-05-13-live-deploy-safety-trio.md`` §Bug #2.
        """
        if not stop_nonce:
            log.warning(
                "flatness_request_missing_nonce",
                extra={"deployment_id": str(deployment_id)},
            )
            return
        key = f"flatness_pending:{deployment_id}"
        ticket = json.dumps(
            {
                "stop_nonce": stop_nonce,
                "member_strategy_id_fulls": list(member_strategy_id_fulls),
            },
            separators=(",", ":"),
        )
        # redis-py's async client returns int / str directly; cast to
        # placate mypy --strict (declared as Awaitable[int] | int).
        # redis-py async typing declares Awaitable[X] | X; in practice
        # the async client returns awaitables. Suppress mypy's union.
        new_length: int = await self._redis.rpush(key, ticket)  # type: ignore[misc]
        await self._redis.expire(key, 120)
        # Bound the list at 32 entries so a coalescing failure or a
        # /kill-all storm against a stuck child can't grow it unbounded.
        if new_length > 32:
            await self._redis.ltrim(key, -32, -1)  # type: ignore[misc]
            new_length = 32
        # Surface list length as a gauge so an operator dashboard can
        # alert when coalescing isn't holding (healthy sustained = 1).
        try:
            from msai.services.observability.trading_metrics import (
                FLATNESS_PENDING_LIST_LENGTH,
            )

            FLATNESS_PENDING_LIST_LENGTH.labels(deployment_id=str(deployment_id)).set(
                float(new_length)
            )
        except Exception:  # noqa: BLE001
            # Observability MUST NOT break stop semantics.
            log.exception("flatness_pending_length_gauge_failed")

    async def stop(self, deployment_id: UUID, *, reason: str = "user") -> bool:
        """Send SIGTERM to the deployment's subprocess.

        Flips the row to ``status='stopping'``, then signals the pid
        (via :attr:`node_handle_cache` first, falling back to ``row.pid``
        for post-supervisor-restart discovered subprocesses, Codex v5 P0).
        Returns ``True`` on success or idempotent no-op; ``False`` on
        hard failure.

        Note: this implementation does NOT busy-wait for the exit or
        escalate to SIGKILL. Task 1.7's full spec describes a 30-second
        wait + SIGKILL escalation; that's folded into the reap_loop
        instead (the loop will observe the exit on its next pass).
        Callers that need hard-timeout behavior should run the loop
        alongside this.
        """
        # Council 2026-06-01 (item 3): the naked per-deployment
        # ``cancel_restart_task`` that used to run HERE is REMOVED. It was the
        # direct cause of F2 — cancelling a restart task mid-reservation orphaned
        # a committed ``starting`` row, wedging the deployment's active
        # unique-index slot forever. Operator-stop correctness now rests on three
        # things that need no task cancellation: (1) the durable
        # ``stop_requested_at`` intent stamped below; (2) the serialized Phase-A
        # OPERATOR_STOPPED re-check (under the deployment lock the respawn also
        # takes); and (3) the pre-start stop-intent gate in
        # :meth:`spawn_with_outcome` (re-read right before ``process.start()``).
        # The in-flight restart task will observe the durable intent and abort at
        # its next halt-gated decision / pre-start re-check — it is no longer
        # force-cancelled.

        async with self._db() as session, session.begin():
            # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST): take
            # the parent ``live_deployments`` row lock ``FOR UPDATE`` FIRST, THEN
            # the active node row — matching the global invariant
            # ``advisory(gateway) → live_deployments FOR UPDATE →
            # live_node_processes FOR UPDATE``. This closes F3: the deployment
            # lock serialises /stop against Phase-A's slot reservation. If /stop
            # wins the deployment lock, Phase-A then blocks and sees the durable
            # ``stop_requested_at`` intent (suppresses, via the OPERATOR_STOPPED
            # re-check); if Phase-A wins, /stop blocks then sees the reserved
            # active row and stops + stamps it. The account_id is read off this
            # same locked deployment row (one read; account-scoped logs below).
            deployment_row = (
                await session.execute(
                    select(LiveDeployment)
                    .where(LiveDeployment.id == deployment_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            stop_account_id = (
                (deployment_row.account_id or "").strip() or None
                if deployment_row is not None
                else None
            )

            # FOR UPDATE (council #3 case 4): take the node row lock so the
            # reaper's classify read — also FOR UPDATE — either observes the
            # committed ``stop_requested_at`` intent we set below, or blocks until
            # this transaction commits it. This serialises a coincident /stop +
            # node self-crash so the operator stop is never lost across the race.
            # latest-is-correct READ (council 2026-05-31 item 5): this filters to
            # the ACTIVE-status set, and the partial unique index
            # ``uq_live_node_processes_active_deployment`` guarantees ≤1 active
            # row per deployment — so this resolves to THE single current active
            # row (the one /stop must signal), not an arbitrary "latest". The
            # ``order_by(...).limit(1)`` is therefore REDUNDANT given the index;
            # it is kept as a belt-and-suspenders guard so a (theoretical) index-
            # invariant violation degrades to "stop the most-recent active row"
            # rather than ``scalar_one_or_none`` raising ``MultipleResultsFound``
            # on the real-money stop path.
            #
            # FINDING 3 (P2): ``stopping`` is INCLUDED in the active-status set
            # (mirroring the API ``live_stop`` short-circuit). The first STOP
            # flips the row to ``stopping`` (SIGTERM sent, child tearing down). A
            # REDELIVERED / retried STOP must FIND that ``stopping`` row and treat
            # it as an IDEMPOTENT in-progress stop (re-stamp the durable intent,
            # harmlessly re-signal the pid below) — it must NOT fall through to the
            # no-active-row → mark-deployment-``stopped`` branch, which would
            # prematurely mark the deployment ``stopped`` WHILE the child is still
            # in teardown and hide a live teardown from active-only status /
            # deploy gates (an ``active_deployments`` gate could then recreate the
            # container mid-teardown — an F5 binding-deploy-contract risk). The
            # reaper syncs ``LiveDeployment.status`` to ``stopped``/``failed`` when
            # the child actually exits. ``stopping`` is in the active set, so the
            # partial unique index still guarantees ≤1 active row — this lookup
            # still resolves to the single current active row.
            row = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(
                            ("starting", "building", "ready", "running", "stopping")
                        ),
                    )
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                # Council 2026-06-01 (item 1, no-active-row branch): with the
                # naked ``cancel_restart_task`` removed (item 3), a STOP consumed
                # for a deployment whose node has ALREADY crashed-to-``failed``
                # (an in-flight restart task backing off) needs the durable stop
                # intent stamped on that latest failed row so the respawn paths
                # honor it. We are UNDER the ``live_deployments FOR UPDATE`` lock,
                # so this stamp serialises with Phase-A's OPERATOR_STOPPED
                # re-check exactly like the active-row branch does. Touch ONLY the
                # durable intent column — never ``status`` / ``LiveDeployment.
                # status`` — so the failed row is NOT resurrected; the intent just
                # becomes visible to the reaper / rescan / pre-start gate (and
                # survives a supervisor restart). Mirrors the API ``live_stop``
                # no-active-row failed-pending stamp. Skipped when the deployment
                # is already terminally ``stopped`` (nothing pending to suppress).
                if deployment_row is not None and deployment_row.status != "stopped":
                    latest_failed = (
                        await session.execute(
                            select(LiveNodeProcess)
                            .where(LiveNodeProcess.deployment_id == deployment_id)
                            .order_by(LiveNodeProcess.started_at.desc())
                            .limit(1)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if (
                        latest_failed is not None
                        and latest_failed.status == "failed"
                        and latest_failed.stop_requested_at is None
                    ):
                        latest_failed.stop_requested_at = datetime.now(UTC)
                        log.info(
                            "stop_suppressed_pending_restart",
                            extra={
                                "account_id": stop_account_id,
                                "deployment_id": str(deployment_id),
                                "failed_row_id": str(latest_failed.id),
                                "reason": reason,
                                "note": (
                                    "no active node row; stamped durable stop intent on "
                                    "the latest failed row so the in-flight / rescan "
                                    "restart paths abort the respawn"
                                ),
                            },
                        )
                        return True

                    # INVARIANT 1 (council 2026-06-01 follow-up — QUEUED-START
                    # GAP). No active node row AND no pending-failed row to stamp,
                    # but the deployment is still in a NON-TERMINAL PRE-ACTIVE
                    # state (``starting`` / ``building`` / ``ready`` / ``running``
                    # with no node row). This is the precise gap: a START was
                    # PUBLISHED to the per-account stream but NOT yet consumed (so
                    # Phase A never reserved a node row), and the operator /stops.
                    # The ``stop_requested_at`` node-row intent CANNOT cover this —
                    # there is no node row to stamp. Without recording anything,
                    # /stop returns "stopped", then the queued START is consumed,
                    # Phase A sees ``deployment.status == 'starting'`` (NOT a stale
                    # terminal), no ``restart_carry`` (so the OPERATOR_STOPPED
                    # respawn re-check does not fire), and SPAWNS a live node for
                    # an account the operator already stopped.
                    #
                    # Fix: mark the ``LiveDeployment`` row itself operator-terminal
                    # (``status='stopped'``) UNDER this ``FOR UPDATE`` lock — the
                    # operator's explicit stop of a still-starting deployment. The
                    # Phase-A OPERATOR-TERMINAL DEPLOYMENT GATE then aborts the
                    # queued START (and any redelivery, and any restart_carry
                    # respawn) because ``deployment.status == 'stopped'``. The
                    # deployment lock is held until ``db.commit()`` so this
                    # serialises with Phase-A's slot reservation (which also locks
                    # this deployment row first): if /stop wins, Phase A then sees
                    # ``stopped`` and aborts; if Phase A wins, it reserves a node
                    # row → /stop takes the active-row branch instead (this branch
                    # is only reached when there is genuinely no active row).
                    if deployment_row.status in (
                        "starting",
                        "building",
                        "ready",
                        "running",
                    ):
                        prior_status = deployment_row.status
                        deployment_row.status = "stopped"
                        deployment_row.last_stopped_at = datetime.now(UTC)
                        await session.commit()
                        log.info(
                            "stop_marked_queued_start_deployment_stopped",
                            extra={
                                "account_id": stop_account_id,
                                "deployment_id": str(deployment_id),
                                "prior_deployment_status": prior_status,
                                "reason": reason,
                                "note": (
                                    "no active node row and no failed row to stamp; "
                                    "the deployment is still pre-active (a queued, "
                                    "unconsumed START) — marked operator-terminal "
                                    "(stopped) so Phase A aborts the queued START "
                                    "and never spawns a node for a stopped account"
                                ),
                            },
                        )
                        return True
                log.info(
                    "stop_idempotent",
                    extra={
                        "account_id": stop_account_id,
                        "deployment_id": str(deployment_id),
                        "reason": reason,
                    },
                )
                return True

            # PR#1 Codex P1 regression: cross-host PID kill guard.
            #
            # In Phase 1 there is exactly one supervisor per host so
            # ``row.host`` always matches ``socket.gethostname()``. In
            # Phase 2 the architecture splits into a trading VM and a
            # compute VM that share one Redis command stream. A STOP
            # command published on the shared stream can be consumed
            # by EITHER supervisor's command bus loop — and the
            # supervisor on the wrong host would read ``row.pid``,
            # ``os.kill(pid, SIGTERM)`` a local PID that happens to
            # exist but belongs to a completely different process,
            # while the actual trading subprocess continues running
            # on its original host untouched.
            #
            # Fix: check ``row.host`` against our own hostname. If
            # they don't match, return ``False`` (NOT an ACK) so the
            # command stays in the Redis PEL for XAUTOCLAIM
            # redelivery to the correct supervisor. Log a warning so
            # the operator can see which command was routed to the
            # wrong host.
            #
            # We do NOT flip ``row.status = 'stopping'`` in the
            # wrong-host branch — that would be a remote-host state
            # mutation and the right supervisor's own stop flow will
            # do it when it picks up the redelivery.
            local_host = socket.gethostname()
            if row.host != local_host:
                log.warning(
                    "stop_wrong_host",
                    extra={
                        "account_id": stop_account_id,
                        "deployment_id": str(deployment_id),
                        "db_status": row.status,
                        "pid": row.pid,
                        "row_host": row.host,
                        "local_host": local_host,
                        "reason": reason,
                        "note": (
                            "STOP command routed to the wrong supervisor; "
                            "not ACKing so redelivery reaches the host "
                            "that owns the subprocess"
                        ),
                    },
                )
                return False  # NO ACK — let XAUTOCLAIM redeliver

            # Set-intent-BEFORE-signal (council #3): persist the durable
            # operator-stop intent and COMMIT it (on ``session.begin()`` exit)
            # BEFORE the SIGTERM below. The reaper suppresses auto-restart
            # whenever ``stop_requested_at`` is set — EVEN on a non-zero exit —
            # so a /stop whose graceful shutdown then crashes is never
            # resurrected (F5). Idempotent: a re-issued /stop just re-stamps it.
            row.status = "stopping"
            row.stop_requested_at = datetime.now(UTC)
            row_pid = row.pid

        # Determine pid: handle cache first (instant), row fallback for
        # post-supervisor-restart discovered subprocesses.
        cached = self.node_handle_cache.get(deployment_id)
        pid = cached.proc.pid if cached is not None else row_pid
        if pid is None:
            log.warning(
                "stop_no_pid",
                extra={
                    "account_id": stop_account_id,
                    "deployment_id": str(deployment_id),
                    "reason": reason,
                },
            )
            return True

        # Child may already be gone — reap_loop will catch up on its next pass.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)

        from msai.services.observability.trading_metrics import DEPLOYMENTS_STOPPED

        DEPLOYMENTS_STOPPED.inc()

        return True


class _PhaseAOutcome:
    """Sentinel values for :meth:`FleetRouter._phase_a_reserve_slot`
    return paths other than "inserted a new row"."""

    NO_DEPLOYMENT: _PhaseAOutcome
    BUSY_STOPPING: _PhaseAOutcome
    ALREADY_ACTIVE: _PhaseAOutcome
    CONCURRENT_STARTUP: _PhaseAOutcome
    NOT_STARTABLE: _PhaseAOutcome
    OPERATOR_STOPPED: _PhaseAOutcome

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"_PhaseAOutcome.{self._name}"


_PhaseAOutcome.NO_DEPLOYMENT = _PhaseAOutcome("NO_DEPLOYMENT")
_PhaseAOutcome.BUSY_STOPPING = _PhaseAOutcome("BUSY_STOPPING")
_PhaseAOutcome.ALREADY_ACTIVE = _PhaseAOutcome("ALREADY_ACTIVE")
_PhaseAOutcome.CONCURRENT_STARTUP = _PhaseAOutcome("CONCURRENT_STARTUP")
_PhaseAOutcome.NOT_STARTABLE = _PhaseAOutcome("NOT_STARTABLE")
# FIX 2 (Codex P1 #2 / pr-toolkit P2) — operator /stop won the race against a
# RESPAWN reservation. A respawn (``restart_carry is not None``) re-reads the
# latest node row's durable ``stop_requested_at`` intent IN THE SAME
# slot-reservation transaction; if it is set, Phase A ABORTS (does NOT insert a
# fresh active row) and returns this sentinel. Mapped through ``spawn_with_outcome``
# as ``(ack=True, process_started=False)`` so the auto-restart paths treat it as
# a DELIBERATE terminal suppression — same family as a halt-suppressed restart:
# ACK, no spawn, NOT retried, NOT counted as a transient failure.
_PhaseAOutcome.OPERATOR_STOPPED = _PhaseAOutcome("OPERATOR_STOPPED")
