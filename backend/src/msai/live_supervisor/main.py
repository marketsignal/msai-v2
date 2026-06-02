"""The supervisor main loop — consumes commands and dispatches them.

Runs until ``stop_event`` is set (wired to SIGTERM by ``__main__.py``).
Owns four background tasks:

- **Command consumer** — ``LiveCommandBus.consume`` yields ``start`` /
  ``stop`` commands; this function dispatches them to
  :class:`FleetRouter`.
- **Reap loop** — :meth:`FleetRouter.reap_loop` surfaces exit codes
  for children the supervisor spawned.
- **Heartbeat monitor** — :meth:`HeartbeatMonitor.run_forever` flips
  stale post-startup rows to ``failed``.
- **Startup watchdog** — :meth:`FleetRouter.watchdog_loop`
  SIGKILLs wedged ``starting`` / ``building`` rows that exceed
  ``startup_hard_timeout_s``. Necessary because the heartbeat thread
  starts BEFORE ``node.build()`` (decision #17) and stops AFTER
  ``dispose()`` (Codex batch 3 iter4 P1), so a wedged build keeps
  the heartbeat fresh forever and ``HeartbeatMonitor`` deliberately
  excludes startup statuses (Codex batch 3 iter8 P1 fix).
- **Periodic reconciling rescan** — :func:`_periodic_rescan_loop`
  runs :meth:`FleetRouter.rescan_for_restart` on a fixed cadence
  (first pass IMMEDIATE at boot), the authoritative state-driven
  recovery backstop that re-drives every stranded ``failed``+eligible
  deployment through the SAME halt-gated bounded restart path
  (council #4 OPT C Part 1; subsumes the prior one-shot startup rescan).

Plus the per-account command consumers, the ``router_heartbeat``
publisher, the account-consumer refresh loop, and the fleet-alert loop
(PR 2 T4/T9), all started by :func:`run_forever`.

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
import os
import random
from typing import TYPE_CHECKING

from msai.services.fleet_alerts import ROUTER_HEARTBEAT_SPOF_THRESHOLD_S
from msai.services.live_command_bus import (
    LiveCommand,
    LiveCommandType,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from msai.live_supervisor.fleet_router import FleetRouter
    from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
    from msai.services.alerting import AlertService
    from msai.services.fleet_alerts import FleetHealthSnapshot
    from msai.services.live_command_bus import LiveCommandBus

    AccountRefresher = Callable[[], Awaitable[Sequence[str]]]
    """Async callable the supervisor calls on each refresh pass to re-derive
    the current known-account set (static pool ∪ live-deployment scan). Lets
    a per-account consumer be lazily started for an account that became known
    AFTER boot, with no supervisor restart (review P1 strand fix)."""

    FleetHealthProvider = Callable[[list[str]], Awaitable[FleetHealthSnapshot]]
    """Async callable the supervisor calls on each fleet-alert pass to build a
    point-in-time :class:`FleetHealthSnapshot`. It receives the supervisor's
    AUTHORITATIVE in-memory ``consumed_accounts`` list (the live per-account
    consumer registry — NOT the Redis-published copy, which lags by a publish
    cadence and would race the alert loop) and returns the snapshot with
    router-heartbeat age + active-deployment health filled in. Injected so the
    snapshot's DB scan is testable in isolation (review P1/P3 — the mandatory
    fleet alerts MUST be evaluated by a running loop, not left as dead code)."""


log = logging.getLogger(__name__)

# How often the supervisor stamps ``router_heartbeat`` (PR 2 T4). Must be
# comfortably below ``ROUTER_HEARTBEAT_TTL_S`` and the SPOF threshold so a
# brief event-loop stall doesn't expire the key or trip the SPOF alert.
ROUTER_HEARTBEAT_PUBLISH_INTERVAL_S = 5.0

# How often the supervisor re-derives the known-account set and lazily
# starts a per-account consumer for any newly-discovered account (review P1
# strand fix — spec step 2: "lazily starts the consumer ... via a
# lightweight known-accounts refresh on each reaper pass"). Comfortably
# below the SPOF/coverage threshold so a just-deployed account's consumer
# attaches well before its first command can strand.
ACCOUNT_REFRESH_INTERVAL_S = 5.0

# Backoff after a per-account consumer task's consume loop crashes, before
# it is restarted. Bounds a tight crash-loop (e.g. a persistent Redis error
# on one account's stream) without stalling the OTHER accounts' consumers.
_CONSUMER_RESTART_BACKOFF_S = 1.0

# How often the supervisor evaluates the mandatory fleet alerts (PR 2 T9,
# wired here by T4 review P1/P3). Frequent enough that the router-SPOF /
# flat-and-unmonitored / account-consumer-missing conditions surface within a
# loop pass or two of the threshold (30s / 60s), without spamming the alert
# service. The per-condition thresholds live in ``fleet_alerts``.
FLEET_ALERT_INTERVAL_S = 10.0


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back on default.

    A missing, blank, non-positive, or unparsable value yields *default* so a
    fat-fingered override never wedges or disables the periodic rescan backstop.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Council #4 OPT C Part 1 — how often the supervisor runs the AUTHORITATIVE
# reconciling rescan (``FleetRouter.rescan_for_restart``) that re-drives every
# stranded ``failed``+eligible deployment through the SAME halt-gated, bounded
# restart path. This is the state-driven backstop that closes the WHOLE
# transient-strand class regardless of which dispatch path failed; the fast
# per-path reaper retry (OPT C Part 2) handles the common transient in seconds,
# and this loop catches anything the fast retry gave up on (or never saw,
# e.g. a node that died while the supervisor was down). 30s sits between
# ``FLEET_ALERT_INTERVAL_S`` (10s) and the RestartPolicy ``BACKOFF_CAP_S``
# (300s) and matches the HeartbeatMonitor / ``rescan_stale_seconds`` staleness
# sense. Env-overridable so the operator drill can tighten it.
RESCAN_INTERVAL_S: float = _env_float("MSAI_RESCAN_INTERVAL_S", 30.0)

# Symmetric jitter fraction applied to each rescan interval so a fleet-wide
# restart-after-outage doesn't synchronise every supervisor incarnation's
# rescan onto the same instant (de-syncs load against a recovering shared IB
# gateway). The first pass is always IMMEDIATE (no jitter); jitter applies only
# to the inter-pass wait.
RESCAN_INTERVAL_JITTER_FRAC: float = 0.1


def enumerate_known_accounts(
    *,
    static_accounts: Iterable[str],
    active_deployment_account_ids: Iterable[str | None],
) -> list[str]:
    """Return the ordered, de-duplicated union of the static configured
    account pool and the account_ids of active/recent deployments.

    The supervisor starts one per-account command consumer for each
    returned account_id (PR 2 T4 startup discovery). Empty/``None`` ids
    are dropped (a legacy row with no account_id has nothing to consume).
    Order is deterministic: static-pool accounts first (config order),
    then any additional active-deployment accounts in first-seen order —
    so logs/tests are stable.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for acct in (*static_accounts, *active_deployment_account_ids):
        if not acct:
            continue
        if acct in seen:
            continue
        seen.add(acct)
        ordered.append(acct)
    return ordered


async def handle_command(command: LiveCommand, *, process_manager: FleetRouter) -> bool:
    """Dispatch a single command to the :class:`FleetRouter`.

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
            # PR 1 T6 + council 2026-05-27 obj #2 / 2026-05-29 obj #12: pass
            # the gateway-session key from the START payload so per-session
            # startup serialization in FleetRouter isn't silently degraded
            # to global ("default") via the DB-row fallback.
            gateway_session_key=command.payload.get("gateway_session_key"),
        )
    if command.command_type is LiveCommandType.STOP:
        return await process_manager.stop(
            command.deployment_id,
            reason=str(command.payload.get("reason", "user")),
        )
    if command.command_type is LiveCommandType.STOP_AND_REPORT_FLATNESS:
        # Push the per-request flatness "ticket" onto a per-deployment
        # list so the child can drain it in its shutdown-finally hook
        # and write the stop_report:{nonce} key (see plan §Bug #2). Then
        # invoke the existing STOP path (SIGTERM). The supervisor ACKs
        # after signaling — the API is the sole report collector.
        await process_manager.push_flatness_request(
            command.deployment_id,
            stop_nonce=str(command.payload.get("stop_nonce", "")),
            member_strategy_id_fulls=list(command.payload.get("member_strategy_id_fulls") or []),
        )
        return await process_manager.stop(
            command.deployment_id,
            reason=str(command.payload.get("reason", "stop_and_report_flatness")),
        )
    log.warning(
        "unknown_command",
        extra={
            "deployment_id": str(command.deployment_id),
            "entry_id": command.entry_id,
            "command_type": str(command.command_type),
        },
    )
    return True  # ACK so we don't loop forever on a malformed command


async def _consume_account_forever(
    *,
    bus: LiveCommandBus,
    process_manager: FleetRouter,
    stop_event: asyncio.Event,
    account_id: str,
    consumer_id: str,
) -> None:
    """Consume + dispatch one account's command stream until ``stop_event``.

    This is the PR 2 T4 per-account failure boundary: the WHOLE consume
    loop for one account runs inside its own task wrapped in a broad
    try/except. A crash in account A's consume loop (a poison command
    that raises, a transient Redis error on A's stream) is logged and the
    loop is restarted after a short backoff — it can NEVER propagate into
    or stall account B's consumer task. The per-COMMAND handler exception
    is also caught (so one bad command leaves the entry in the PEL for
    XAUTOCLAIM retry rather than tearing down the whole account loop).

    ACK semantics (decision #13): ACK only on a ``True`` handler return,
    and ACK on the SAME per-account stream the entry came from.
    """
    # Derive the per-account stream from the bus's CONFIGURED base so consume +
    # ACK agree with publish on a non-default-``stream`` bus (Codex iter-20 — the
    # consume-side mirror of the iter-19 publish fix). Default bus → unchanged.
    stream = bus.account_stream(account_id)
    while not stop_event.is_set():
        try:
            async for command in bus.consume(consumer_id, stop_event, stream=stream):
                ok = False
                try:
                    ok = await handle_command(command, process_manager=process_manager)
                except Exception:
                    # Per-command failure: leave the entry in the PEL for
                    # XAUTOCLAIM retry. Decision #13 — NEVER ACK from a
                    # finally block. Log + continue to the next command.
                    log.exception(
                        "command_handler_failed",
                        extra={
                            "account_id": account_id,
                            "deployment_id": str(command.deployment_id),
                            "entry_id": command.entry_id,
                            "command_type": str(command.command_type),
                        },
                    )
                    ok = False

                if ok:
                    await bus.ack(command.entry_id, stream=stream)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Whole-loop failure boundary: a crash here must not stall the
            # other accounts' consumers. Log, back off, and restart this
            # account's consume loop (unless we're shutting down).
            log.exception(
                "account_consumer_loop_crashed",
                extra={"account_id": account_id},
            )
            if stop_event.is_set():
                return
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(_CONSUMER_RESTART_BACKOFF_S)


async def _router_heartbeat_loop(
    *,
    bus: LiveCommandBus,
    stop_event: asyncio.Event,
    consumed_accounts: Callable[[], list[str]],
    alert_eval_allowed: asyncio.Event | None = None,
    alert_eval_done: asyncio.Event | None = None,
    prior_operation_probe: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    """Stamp ``router_heartbeat`` + the consumed-account set on a fixed
    cadence until ``stop_event``.

    The heartbeat is the supervisor-liveness signal that
    ``_supervisor_is_alive`` (the ``/start-portfolio`` 503 gate) and T8's
    ``router_heartbeat_age_s`` read. The consumed-account set is the
    CONSUMPTION-coverage signal (review P2): publishing it alongside the
    heartbeat means liveness is never decoupled from "which accounts are
    actually being consumed", so the coverage alert can fire when an
    active-deployment account has no running consumer.

    Boot coordination with the fleet-alert loop (council #4 OPT C Part 3 — SPOF
    evaluate-stale-before-publish). The publisher OWNS the boot ordering via a
    deterministic two-event handshake (NOT task-scheduling luck), branching on a
    probe of the PRIOR ``router_heartbeat`` key:

    - **Restart-after-outage** — the prior key is STALE past the SPOF threshold,
      OR it is ABSENT/None but durable evidence shows the supervisor has
      operated before (PR 2 / F3). The second case is the EXPIRED-after-outage
      gap: an outage longer than the ~90s ``router_heartbeat`` TTL expires the
      key, so ``read_router_heartbeat_age_s`` returns ``None`` — which the SPOF
      detector treats as maximally stale (CRITICAL). The publisher signals
      ``alert_eval_allowed`` so the fleet-alert loop evaluates the stale/absent
      prior key FIRST (firing ``router_spof`` for the outage gap), waits
      (bounded) for ``alert_eval_done``, and THEN publishes the fresh stamp. The
      fresh stamp therefore never self-masks the gap.
    - **Clean / fresh boot** — a fresh prior key that beat its TTL, OR an
      ABSENT/None key on a genuine FIRST-EVER boot (no prior-operation
      evidence). There is no outage gap to surface, so the publisher publishes
      the fresh stamp FIRST, then signals ``alert_eval_allowed`` so the alert
      loop's first eval reads the fresh heartbeat and does NOT fire a spurious
      SPOF on a healthy boot.

    PR 2 / F3 — distinguishing FIRST-EVER boot from EXPIRED-after-outage when the
    key is ``None`` (both give ``None``): ``prior_operation_probe`` returns the
    durable evidence (any ``live_deployments`` / ``live_node_processes`` row — a
    brand-new install has none). ``None`` heartbeat + evidence → treat as an
    OUTAGE (fire SPOF on the first eval). ``None`` heartbeat + NO evidence →
    genuine first boot (no spurious SPOF). If the probe is unavailable (not
    wired) OR raises, we FAIL TOWARD the outage path on a ``None`` key — safer to
    over-alert once than to mask a real outage longer than the TTL (the rare
    first-boot false-positive is documented + acceptable).

    Both waits are BOUNDED (they also wake on ``stop_event`` / a short timeout)
    so a degraded alert loop can NEVER wedge liveness. When the alert loop is not
    wired (``alert_eval_allowed`` / ``alert_eval_done`` are ``None``) the
    publisher simply publishes immediately.

    Wrapped so a transient Redis error on one publish doesn't kill the
    loop — the next pass re-stamps; the key's TTL is the fail-closed
    backstop if the supervisor is genuinely down.
    """
    if alert_eval_allowed is not None and alert_eval_done is not None:
        prior_age_s: float | None = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            prior_age_s = await bus.read_router_heartbeat_age_s()

        if prior_age_s is not None:
            # A present prior key: outage iff it is stale past the SPOF threshold.
            outage_gap = prior_age_s > ROUTER_HEARTBEAT_SPOF_THRESHOLD_S
        else:
            # PR 2 / F3 — the prior key is ABSENT/None. This is EITHER a genuine
            # first-ever boot (benign) OR an expired-after-outage gap longer than
            # the key's TTL (a real outage the SPOF detector treats as CRITICAL).
            # Distinguish via durable prior-operation evidence; fail TOWARD the
            # outage path when the probe is unavailable or raises (over-alert
            # once rather than mask an outage).
            prior_operation = True  # fail-toward-outage default
            if prior_operation_probe is not None:
                try:
                    prior_operation = await prior_operation_probe()
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    log.exception(
                        "router_heartbeat_prior_operation_probe_failed",
                        extra={
                            "note": (
                                "prior-operation probe failed on a None heartbeat; "
                                "failing toward the outage path (firing SPOF) to avoid "
                                "masking a real outage"
                            )
                        },
                    )
                    prior_operation = True
            outage_gap = prior_operation
        if outage_gap:
            # Restart-after-outage: let the alert loop evaluate the STALE/ABSENT
            # prior key (router_spof) BEFORE we overwrite it with a fresh stamp.
            log.warning(
                "router_heartbeat_first_publish_gated_on_stale_prior",
                extra={
                    "prior_age_s": prior_age_s,
                    "note": (
                        "restart-after-outage: holding the first heartbeat publish until "
                        "the fleet-alert loop evaluates the stale/absent prior heartbeat "
                        "(router_spof) — an absent key here is an expired-after-outage gap"
                    ),
                },
            )
            alert_eval_allowed.set()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    _wait_first_of(alert_eval_done, stop_event),
                    timeout=ROUTER_HEARTBEAT_PUBLISH_INTERVAL_S,
                )
            await _publish_router_heartbeat_once(bus, consumed_accounts)
        else:
            # Clean/fresh boot: publish the fresh stamp FIRST so the alert loop's
            # first eval reads it (no spurious SPOF on a healthy boot), then
            # release the alert loop.
            await _publish_router_heartbeat_once(bus, consumed_accounts)
            alert_eval_allowed.set()
        # The boot publish is done; fall into the steady-state loop's interval
        # wait before the NEXT publish.
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=ROUTER_HEARTBEAT_PUBLISH_INTERVAL_S)
    while not stop_event.is_set():
        await _publish_router_heartbeat_once(bus, consumed_accounts)
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=ROUTER_HEARTBEAT_PUBLISH_INTERVAL_S)


async def _publish_router_heartbeat_once(
    bus: LiveCommandBus,
    consumed_accounts: Callable[[], list[str]],
) -> None:
    """Stamp ``router_heartbeat`` + the consumed-account set once.

    Shared by the boot path and the steady-state loop in
    :func:`_router_heartbeat_loop`. A transient Redis error on one publish is
    logged + swallowed (the next pass re-stamps; the key TTL is the fail-closed
    backstop). ``CancelledError`` propagates so shutdown drains cleanly."""
    try:
        await bus.publish_router_heartbeat()
        await bus.publish_consumed_accounts(consumed_accounts())
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("router_heartbeat_publish_failed")


async def _wait_first_of(*events: asyncio.Event) -> None:
    """Return as soon as ANY of ``events`` is set (council #4 OPT C Part 3).

    Used by the heartbeat publisher to wake on EITHER the first-alert-eval gate
    OR ``stop_event`` (so a shutdown during the pre-first-publish wait doesn't
    stall). Caller wraps this in a bounded ``wait_for`` so a never-set gate
    still releases after the timeout."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        for w in waiters:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w


async def _periodic_rescan_loop(
    *,
    process_manager: FleetRouter,
    stop_event: asyncio.Event,
    interval_s: float,
    rng: random.Random | None = None,
) -> None:
    """Run :meth:`FleetRouter.rescan_for_restart` on a fixed cadence until
    ``stop_event`` — the AUTHORITATIVE state-driven recovery backstop (council
    #4 OPT C Part 1).

    The FIRST pass runs IMMEDIATELY at boot, then every ``interval_s`` (± a
    small jitter). This single rescan code path SUBSUMES the prior one-shot
    startup rescan: boot recovery (a node that died while the supervisor was
    down) AND ongoing runtime recovery (any transient strand the fast per-path
    reaper retry gave up on, or never observed) both flow through the SAME
    ``rescan_for_restart`` — which routes every candidate through the SAME
    ``_maybe_auto_restart`` (halt-gate-FIRST fail-closed, RestartPolicy ceiling,
    Phase-A unique-index serialisation, max-concurrent-respawn=1 per gateway).
    The loop NEVER bypasses those gates; it only changes the CADENCE.

    Sibling of the other background loops: its own task, honors ``stop_event``,
    exception-guarded (one bad pass logs + continues rather than killing the
    backstop — node death is still eventually caught by the next pass).
    """
    rng = rng or random.Random()
    # First pass is IMMEDIATE (no initial wait) so boot recovery doesn't wait a
    # full interval — this is what subsumes the one-shot startup rescan.
    while not stop_event.is_set():
        try:
            await process_manager.rescan_for_restart()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient DB/Redis blip on one pass must not kill the backstop.
            # The next interval re-runs; the reaper + HeartbeatMonitor remain
            # independent liveness authorities in the meantime.
            log.exception("auto_restart_periodic_rescan_pass_failed")
        # Jittered inter-pass wait; wake instantly on shutdown.
        spread = interval_s * RESCAN_INTERVAL_JITTER_FRAC
        wait_s = max(0.0, interval_s + rng.uniform(-spread, spread))
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)


async def _account_consumer_supervisor_loop(
    *,
    stop_event: asyncio.Event,
    account_refresher: AccountRefresher,
    refresh_interval_s: float,
    ensure_consumer: Callable[[str], None],
) -> None:
    """Periodically re-derive the known-account set and lazily start a
    per-account consumer for any newly-discovered account (review P1 strand
    fix — spec step 2).

    Without this, the supervisor only consumes the accounts present in the
    boot-time ``known_accounts`` set. Both inputs to that set can be empty
    (an empty ``GATEWAY_CONFIG`` + a supervisor that booted before the first
    deploy, or a transient DB blip during the boot scan), so a STOP /
    kill-all / drain published to an uncovered account's stream would
    strand in the PEL forever and a surviving live node would be
    unstoppable. Re-deriving the set on a short cadence and lazily
    attaching a consumer via ``ensure_consumer`` (idempotent) closes that
    gap; ``ensure_consumer`` already captures the bus + router it needs.

    Best-effort: a refresher exception is logged and the loop retries on
    the next pass — the existing consumers keep running.
    """
    while not stop_event.is_set():
        try:
            for account_id in await account_refresher():
                if account_id:
                    ensure_consumer(account_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("account_refresh_failed")
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=refresh_interval_s)


async def _fleet_alert_loop(
    *,
    stop_event: asyncio.Event,
    alert_service: AlertService,
    fleet_health_provider: FleetHealthProvider,
    consumed_accounts: Callable[[], list[str]],
    interval_s: float,
    alert_eval_allowed: asyncio.Event | None = None,
    alert_eval_done: asyncio.Event | None = None,
) -> None:
    """Build a :class:`FleetHealthSnapshot` and evaluate the mandatory fleet
    alerts on a fixed cadence until ``stop_event`` (PR 2 T4 review P1/P3).

    Without this loop the T9 detectors (router-SPOF, flat-and-unmonitored,
    account-consumer-missing) are DEAD CODE — the supervisor would publish the
    ``router_heartbeat`` + ``consumed_accounts`` keys but nothing would read
    them to page an operator. This is the running loop that closes that gap:
    the producer half (keys) AND the consumer half (alerts) are both live in
    this PR.

    The detector is router-independent in spirit but runs inside the
    supervisor here because the supervisor is the only process that knows its
    own ``consumed_accounts`` set. The companion supervisor-OUTAGE detector
    (when the supervisor is wholly down, so this loop can't run) is the SPOF
    alert's reason for existing — a separate watcher (or the next supervisor
    incarnation) sees the stale ``router_heartbeat`` and fires it. T12's
    outage harness exercises that path.

    Boot coordination (council #4 OPT C Part 3 — SPOF evaluate-stale-before-
    publish). The heartbeat publisher OWNS the boot ordering via a two-event
    handshake: this loop waits (bounded) on ``alert_eval_allowed`` before its
    FIRST evaluation, then sets ``alert_eval_done`` after it. The publisher
    branches on a probe of the prior ``router_heartbeat`` key:

    - restart-after-outage (stale prior) → publisher sets ``alert_eval_allowed``
      FIRST, so this loop's first eval reads the STALE prior key and fires
      ``router_spof`` for the gap; the publisher waits for ``alert_eval_done``
      and only THEN publishes the fresh stamp (no self-mask).
    - clean/fresh boot → publisher publishes FIRST, then sets
      ``alert_eval_allowed``; this loop's first eval reads the fresh heartbeat
      and does NOT fire a spurious SPOF on a healthy boot.

    ``alert_eval_done`` is set in a ``finally`` so even a first-pass EXCEPTION
    releases the publisher's bounded wait promptly — liveness is never wedged on
    the alert loop. Both this loop's wait and the publisher's are bounded.

    NOTE (council #4 OPT C Part 3 scope): this fires ``router_spof`` only for the
    gap a RESTARTED supervisor can observe (the prior incarnation's stale key).
    A TRUE full-supervisor-outage detector — one that fires while the supervisor
    is wholly DOWN, so this in-process loop cannot run — still requires an
    out-of-process external watcher reading the stale ``router_heartbeat`` key.
    That is DEFERRED to a later PR; it is NOT built here.

    Dedupe (Codex P2 — fleet alerts flood the operator): this loop owns ONE
    :class:`~msai.services.fleet_alerts.FleetAlertDeduper` across all ticks, so a
    condition that PERSISTS across the ~10s evaluation cadence pages ONCE on
    first occurrence and then only on a cooldown reminder, instead of re-paging
    every tick. A condition that CLEARS and later re-occurs re-arms and pages
    immediately. Without this the same critical alert re-sends every tick,
    flooding SMTP and evicting other entries from the bounded alert history.

    Best-effort: a snapshot-build or send failure is logged and the loop
    retries next pass — a transient DB/SMTP blip must not silence later
    alerts (``AlertService.send_alert`` itself already swallows SMTP/history
    failures).
    """
    from msai.services.fleet_alerts import FleetAlertDeduper, evaluate_and_alert_fleet

    # One deduper for the WHOLE loop lifetime (constructed OUTSIDE the while so
    # its last-sent timestamps persist across the fixed-cadence evaluations).
    # This is what makes a PERSISTING condition (a stale router heartbeat, a
    # flat-and-unmonitored account, a missing consumer) page the operator ONCE
    # on first occurrence + a cooldown reminder, instead of re-paging every
    # ``interval_s`` tick — which would flood SMTP and evict other entries from
    # the bounded alert history (Codex P2). A cleared-then-reoccurring condition
    # re-arms and pages again immediately (edge-triggered).
    deduper = FleetAlertDeduper()

    # SPOF Part 3: hold the FIRST evaluation until the publisher says the
    # heartbeat key is in the state we should evaluate (stale prior left intact
    # for the outage case, or freshly published for the clean-boot case).
    # Bounded so a publisher that never signals can't wedge the alert loop.
    if alert_eval_allowed is not None and not alert_eval_allowed.is_set():
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(
                _wait_first_of(alert_eval_allowed, stop_event),
                timeout=FLEET_ALERT_INTERVAL_S,
            )

    first_pass = True
    while not stop_event.is_set():
        try:
            snapshot = await fleet_health_provider(consumed_accounts())
            await evaluate_and_alert_fleet(snapshot, alert_service=alert_service, deduper=deduper)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fleet_alert_evaluation_failed")
        finally:
            # Release the heartbeat publisher's gated first publish AFTER the
            # first evaluation has read the (stale prior) heartbeat — even if
            # that pass raised, so liveness is never wedged on the alert loop.
            if first_pass and alert_eval_done is not None:
                alert_eval_done.set()
            first_pass = False
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)


async def run_forever(
    *,
    bus: LiveCommandBus,
    process_manager: FleetRouter,
    heartbeat_monitor: HeartbeatMonitor,
    stop_event: asyncio.Event,
    consumer_id: str = "supervisor-1",
    known_accounts: Sequence[str] | None = None,
    account_refresher: AccountRefresher | None = None,
    account_refresh_interval_s: float = ACCOUNT_REFRESH_INTERVAL_S,
    alert_service: AlertService | None = None,
    fleet_health_provider: FleetHealthProvider | None = None,
    fleet_alert_interval_s: float = FLEET_ALERT_INTERVAL_S,
    run_startup_rescan: bool = True,
    rescan_interval_s: float = RESCAN_INTERVAL_S,
) -> None:
    """The supervisor's main loop.

    Starts the reap + heartbeat background tasks, then fans out one
    per-account command consumer (PR 2 T4) for every account in
    ``known_accounts`` — each in its own task with a failure boundary so
    one account's command exception can't stall the others. Also runs the
    ``router_heartbeat`` publisher loop (the supervisor-liveness signal
    that replaces the retired global-stream consumer-group probe), the
    PERIODIC reconciling rescan loop (council #4 OPT C Part 1 — the
    authoritative state-driven recovery backstop; ``run_startup_rescan``
    gates it, ``rescan_interval_s`` paces it, first pass IMMEDIATE at boot so
    it subsumes the old one-shot startup rescan), and — when
    ``account_refresher`` is supplied — a refresh loop that lazily starts a
    consumer for any account discovered AFTER boot. On shutdown, cancels every
    task and waits for them to drain.

    ``known_accounts`` is the union of the static configured account pool
    and the active-deployment account_ids, computed by the caller via
    :func:`enumerate_known_accounts` (``__main__`` resolves it from the
    :class:`GatewayRouter` + a ``live_deployments`` scan).

    ``account_refresher`` (review P1 strand fix) is an async callable that
    re-derives the current known-account set on each refresh pass. The
    supervisor lazily attaches a per-account consumer for any newly-seen
    account, so a command for an account that wasn't in the boot set (empty
    ``GATEWAY_CONFIG`` + pre-deploy boot, or a boot-scan DB blip) is no
    longer stranded — its consumer attaches within one refresh interval and
    Redis Streams retains the entry until then. PR 2 still relies on the
    static pool covering the steady state; this refresh is the safety net
    for the boot-time edge cases. (Full operator-driven dynamic add-account
    at runtime is hardened in PR 3.)
    """
    # Live registry of running consumers, shared with the refresh loop and
    # the consumed-account publisher. ``ensure_consumer`` is the single
    # idempotent entry point that starts a consumer for an account exactly
    # once (a duplicate call is a no-op — same XGROUP, same PEL ownership).
    consumer_tasks: dict[str, asyncio.Task[None]] = {}

    def _ensure_consumer(account_id: str) -> None:
        if account_id in consumer_tasks:
            return
        consumer_tasks[account_id] = asyncio.create_task(
            _consume_account_forever(
                bus=bus,
                process_manager=process_manager,
                stop_event=stop_event,
                account_id=account_id,
                # Namespaced per-account consumer_id so XINFO CONSUMERS /
                # PEL ownership is account-distinct.
                consumer_id=f"{consumer_id}:{account_id}",
            )
        )
        log.info("supervisor_consumer_started", extra={"account_id": account_id})

    def _consumed_accounts() -> list[str]:
        return sorted(consumer_tasks.keys())

    # Bind the router's reap stop_event to THIS loop's event up front so the
    # PR 2 T6 startup re-scan's cancellable auto-restart backoff honors
    # shutdown even before the reap loop task starts running.
    process_manager._reap_stop_event = stop_event

    monitor_task = asyncio.create_task(heartbeat_monitor.run_forever(stop_event))
    reap_task = asyncio.create_task(process_manager.reap_loop(stop_event))
    # Codex batch 3 iter8 P1 fix: the startup watchdog is the SOLE
    # killer of wedged ``starting`` / ``building`` rows. Without it,
    # a stuck ``node.build()`` would hold the active-row slot
    # indefinitely (heartbeat keeps the row fresh; HeartbeatMonitor
    # excludes startup statuses by design) and block every future
    # ``/start`` for that deployment.
    watchdog_task = asyncio.create_task(process_manager.watchdog_loop(stop_event))

    # Council #4 OPT C Part 3 — SPOF evaluate-stale-before-publish. The heartbeat
    # publisher and the fleet-alert loop coordinate their FIRST actions with a
    # deterministic two-event handshake (NOT task-scheduling luck):
    #   - ``alert_eval_allowed`` — the publisher signals when the alert loop may
    #     run its first eval (BEFORE the publisher's first publish in the
    #     restart-after-outage case so router_spof fires on the stale prior key;
    #     AFTER it in the clean-boot case so no spurious SPOF fires).
    #   - ``alert_eval_done`` — the alert loop signals it finished its first eval,
    #     releasing the publisher's gated first publish in the outage case.
    # When the fleet-alert loop is NOT wired (unit tests of the consumer fan-out),
    # the publisher gets neither event (None) and simply publishes immediately.
    fleet_alerts_wired = alert_service is not None and fleet_health_provider is not None
    alert_eval_allowed = asyncio.Event() if fleet_alerts_wired else None
    alert_eval_done = asyncio.Event() if fleet_alerts_wired else None

    heartbeat_pub_task = asyncio.create_task(
        _router_heartbeat_loop(
            bus=bus,
            stop_event=stop_event,
            consumed_accounts=_consumed_accounts,
            alert_eval_allowed=alert_eval_allowed,
            alert_eval_done=alert_eval_done,
            # PR 2 / F3 — durable prior-operation probe so a None heartbeat at
            # boot is classified expired-after-outage (fire SPOF) vs genuine
            # first-ever boot (no spurious SPOF). The FleetRouter owns the DB
            # session factory; this reads any live_deployments / node-process row.
            prior_operation_probe=process_manager.has_prior_operation_evidence,
        )
    )

    # Start one consumer per boot-time known account.
    for account_id in known_accounts or []:
        if account_id:
            _ensure_consumer(account_id)
    log.info(
        "supervisor_consumers_started",
        extra={
            "accounts": _consumed_accounts(),
            "consumer_count": len(consumer_tasks),
        },
    )

    # F1 — ALSO consume the BASE (account-less) stream. ``LiveCommandBus``
    # routes a command published with an empty/None ``account_id`` to the base
    # stream (``bus.account_stream(None)`` → the configured base, the documented
    # legacy / single-account path). Without a reader, such a command is never
    # consumed/ACKed/handled. We run ONE base-stream consumer alongside the
    # per-account fan-out, on the SAME ``_consume_account_forever`` machinery
    # (same failure boundary + ACK-on-True semantics). It reads ONLY the base
    # stream (``base``); per-account commands live on ``base:account`` streams and
    # are read by their own consumers — so there is no double-consume. The base
    # consumer is kept SEPARATE from the per-account ``consumer_tasks`` set so it
    # is never mis-reported as a "consumed account" in the heartbeat/health set.
    base_consumer_task = asyncio.create_task(
        _consume_account_forever(
            bus=bus,
            process_manager=process_manager,
            stop_event=stop_event,
            # ``account_id=""`` resolves to the base stream via
            # ``bus.account_stream("")`` (``command_stream_for_account`` treats a
            # falsy id as the base) — honors a custom-stream bus too. It is NOT
            # added to ``consumer_tasks``, so ``_consumed_accounts`` excludes it.
            account_id="",
            consumer_id=f"{consumer_id}:base",
        )
    )

    background_tasks: list[asyncio.Task[None]] = [
        monitor_task,
        reap_task,
        watchdog_task,
        heartbeat_pub_task,
        base_consumer_task,
    ]

    # PR 2 T6 + council #4 OPT C Part 1 — the PERIODIC reconciling rescan: the
    # AUTHORITATIVE state-driven recovery backstop. A node can die while the
    # supervisor is DOWN (container recreate / OOM — fresh NodeHandleCache is
    # empty so the reaper never fires for it) OR a transient strand can leak
    # through any dispatch path. The periodic rescan re-drives EVERY stranded
    # ``failed``+eligible deployment through the SAME halt-gate + bounded policy
    # the reaper uses, on a fixed cadence. Its FIRST pass is IMMEDIATE at boot,
    # which SUBSUMES the prior one-shot startup rescan — there is now ONE rescan
    # code path used at boot AND at runtime (NOT a one-shot plus a periodic). It
    # runs as a sibling background task (so a long halt-gated backoff inside a
    # candidate's restart doesn't delay the per-account consumers coming up) and
    # is cancelled + drained in the finally below alongside the other loops.
    if run_startup_rescan:
        background_tasks.append(
            asyncio.create_task(
                _periodic_rescan_loop(
                    process_manager=process_manager,
                    stop_event=stop_event,
                    interval_s=rescan_interval_s,
                )
            )
        )

    if account_refresher is not None:
        background_tasks.append(
            asyncio.create_task(
                _account_consumer_supervisor_loop(
                    stop_event=stop_event,
                    account_refresher=account_refresher,
                    refresh_interval_s=account_refresh_interval_s,
                    ensure_consumer=_ensure_consumer,
                )
            )
        )

    # PR 2 T4 review P1/P3: wire the mandatory fleet alerts (T9) into a running
    # loop. Opt-in (both an ``alert_service`` AND a ``fleet_health_provider``
    # must be supplied) so unit tests of the consumer fan-out don't need the
    # alert plumbing; production (``__main__``) always supplies both. Without
    # this loop the SPOF / flat-and-unmonitored / account-consumer-missing
    # detectors would be dead code.
    if alert_service is not None and fleet_health_provider is not None:
        background_tasks.append(
            asyncio.create_task(
                _fleet_alert_loop(
                    stop_event=stop_event,
                    alert_service=alert_service,
                    fleet_health_provider=fleet_health_provider,
                    consumed_accounts=_consumed_accounts,
                    interval_s=fleet_alert_interval_s,
                    # Council #4 OPT C Part 3: the publisher-owned two-event
                    # handshake — wait for ``alert_eval_allowed`` before the first
                    # eval, then signal ``alert_eval_done`` after it (so the
                    # publisher's gated first publish releases in the outage case).
                    alert_eval_allowed=alert_eval_allowed,
                    alert_eval_done=alert_eval_done,
                )
            )
        )

    try:
        # Block until shutdown is requested. The per-account consumers and
        # background loops all honor ``stop_event`` themselves.
        await stop_event.wait()
    finally:
        # PR 2 F1 — cancel + drain any in-flight per-account auto-restart tasks
        # FIRST so a pending respawn never lands a fresh live node while the
        # fleet is draining (the cancellable backoff also abandons on the
        # stop_event, but a task already past the backoff and mid-spawn must be
        # cancelled explicitly).
        with contextlib.suppress(Exception):
            await process_manager.cancel_restart_tasks()
        all_tasks = [*background_tasks, *consumer_tasks.values()]
        for t in all_tasks:
            t.cancel()
        for t in all_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
