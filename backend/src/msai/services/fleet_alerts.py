"""Mandatory fleet alerts for the per-account supervisor (PR 2 / T9).

The Opt-4-refined topology keeps a single ``live-supervisor`` process
owning every account's TradingNode child. That single supervisor is a
SPOF: if it stops publishing its heartbeat, nothing is reaping crashed
nodes, nothing is auto-restarting them, and no command is being consumed.
The fleet is silently unmonitored.

Two detectors guard against that, with no overlap:

* :func:`detect_fleet_alerts` is a **pure** function over a
  :class:`FleetHealthSnapshot`. It returns zero or more
  :class:`FleetAlert` records and performs no I/O, so it is trivially
  unit-testable and free of timing flakiness.
* :func:`evaluate_and_alert_fleet` is the thin async wrapper that runs
  the detector and forwards each detected condition to the existing
  :class:`~msai.services.alerting.AlertService` (SMTP + history).

Wiring (PR 2 T4 review P1/P3)
-----------------------------

The supervisor's fleet-alert loop
(:func:`msai.live_supervisor.main._fleet_alert_loop`, started by
``run_forever``) calls :func:`evaluate_and_alert_fleet` on a fixed cadence,
building the :class:`FleetHealthSnapshot` from the ``router_heartbeat`` age,
the supervisor's authoritative in-memory consumed-account set, and a DB scan
of active / failed deployments (``__main__._build_fleet_health_provider``).
Both halves are live in this PR: the supervisor PUBLISHES the
``router_heartbeat`` + ``consumed_accounts`` keys AND evaluates the alerts —
they are not dead code.

The three conditions:

1. **Router SPOF** — ``router_heartbeat_age_s > 30`` (or ``None``: the
   router has never published → fail-closed → treated as unmonitored).
   This is the supervisor-OUTAGE alert: when the supervisor process is
   wholly down its own loop can't run, so the stale heartbeat is meant to
   be seen by the NEXT supervisor incarnation (or an external watcher /
   T12's outage harness) which fires it.
2. **Flat-and-unmonitored** — the single most important alert. A
   deployment is in ``failed`` with a heartbeat older than 60s AND no
   successful respawn has happened. The account may be flat or holding
   an un-monitored position with no live node watching it.
   ``respawned_successfully`` is the "a live node is watching again" signal;
   until the auto-restart reaper (T6) supplies it, the production snapshot
   builder reports ``False`` conservatively so a ``failed`` + stale
   deployment ALWAYS pages (the safe direction for real money). T6 refines
   this to clear the alert once a restart reconciles.
3. **Account-consumer-missing** — an account with an active deployment but
   NO running per-account command consumer (its STOP / kill-all / drain
   would strand un-consumed — the live node is un-stoppable via the
   platform). Needs neither T6 nor T8; wired now.

A healthy fleet (fresh router heartbeat, all deployments running fresh,
every active account consumed) fires none.

Thresholds are module constants, env-overridable, so the supervisor-
outage drill (T12) can tighten them without a code change.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from msai.services.alerting import AlertService

log = get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment, falling back on default.

    A missing, blank, or unparsable value yields *default* so a fat-
    fingered override never disables a safety alert silently.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("fleet_alert_threshold_unparsable", name=name, raw=raw, default=default)
        return default
    if value <= 0:
        log.warning("fleet_alert_threshold_non_positive", name=name, value=value, default=default)
        return default
    return value


# Router heartbeat older than this (seconds) => the supervisor SPOF is
# silently down and the fleet is unmonitored. Matches T8's
# ``router_heartbeat_age_s`` surfaced on ``/live/status``.
ROUTER_HEARTBEAT_SPOF_THRESHOLD_S: float = _env_float("MSAI_ROUTER_SPOF_THRESHOLD_S", 30.0)

# A ``failed`` deployment whose last heartbeat is older than this (seconds)
# with no successful respawn => flat-and-unmonitored.
STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S: float = _env_float(
    "MSAI_STALE_DEPLOYMENT_THRESHOLD_S", 60.0
)

# While a condition PERSISTS, the deduper re-sends it as a reminder no more
# often than this (seconds). The first occurrence is ALWAYS sent immediately;
# this governs only the reminder cadence so a stale router heartbeat / flat
# account does not re-page the operator on every ~10s evaluation tick (which
# would flood SMTP and evict other entries from the alert history). 30 min is
# frequent enough to keep an unresolved critical visible, rare enough not to
# flood. Env-overridable so the supervisor-outage drill can tighten it.
FLEET_ALERT_COOLDOWN_S: float = _env_float("MSAI_FLEET_ALERT_COOLDOWN_S", 1800.0)

AlertKind = Literal["router_spof", "flat_and_unmonitored", "account_consumer_missing"]


@dataclass(frozen=True)
class DeploymentHealth:
    """A single live deployment's health, as seen by the supervisor.

    ``respawned_successfully`` is the authoritative "is a live node
    watching this account again?" signal supplied by the reaper / restart
    policy (T6): ``True`` once a restarted node reached ``is_running`` and
    reconciled. A ``failed`` + stale deployment with this ``False`` is the
    flat-and-unmonitored case.

    ``last_heartbeat_age_s`` is ``None`` for the NEVER-REPORTED case: a
    ``failed`` deployment with NO ``live_node_processes`` row at all (the node
    died before writing a single heartbeat). ``None`` is maximally stale by
    definition — it ALWAYS qualifies for the flat-and-unmonitored alert.
    (Codex bug fix: the snapshot builder previously used ``float('inf')`` here,
    which made the formatter's ``int(inf)`` raise ``OverflowError`` and SILENTLY
    SUPPRESS the alert for exactly the case it must fire on.)
    """

    deployment_id: str
    account_id: str
    status: str
    last_heartbeat_age_s: float | None
    respawned_successfully: bool


@dataclass(frozen=True)
class FleetHealthSnapshot:
    """A point-in-time view of the whole fleet's supervision health.

    ``accounts_with_active_deployments`` / ``consumed_accounts`` (PR 2 T4
    review P2) decouple liveness from consumption: the supervisor publishes
    the set of accounts it has a running per-account command consumer for
    (``consumed_accounts``), and the snapshot builder supplies the set of
    accounts that actually have active deployments. An active-deployment
    account NOT in ``consumed_accounts`` is silently un-consumable — a STOP /
    kill-all / drain for it would strand in the PEL — and fires the
    coverage alert. Both default empty so legacy T9 snapshots (which carry
    neither) never trip the new detector."""

    router_heartbeat_age_s: float | None
    deployments: list[DeploymentHealth] = field(default_factory=list)
    accounts_with_active_deployments: list[str] = field(default_factory=list)
    consumed_accounts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FleetAlert:
    """A detected, ready-to-send alert condition."""

    kind: AlertKind
    level: str
    subject: str
    body: str

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """A stable key identifying this distinct alert CONDITION (not this
        evaluation pass).

        Used by :class:`FleetAlertDeduper` to suppress re-sends of a persisting
        condition. ``(kind, subject)`` is the right grain: ``kind`` separates the
        three detectors, and ``subject`` carries the GENUINELY-DISTINCT ENTITY for
        that detector:

        * ``router_spof`` — one fleet-wide subject (the supervisor is a single
          SPOF, so one condition).
        * ``account_consumer_missing`` — one subject per account (a missing
          consumer is naturally one-per-account; there is exactly one consumer
          per account).
        * ``flat_and_unmonitored`` — one subject per *deployment*, NOT per
          account. ``LiveDeployment`` is unique on
          ``(portfolio_revision_id, account_id)``, so a single account can hold
          MULTIPLE failed deployments at once. Keying on the account alone would
          collapse two distinct money-losing flat-and-unmonitored conditions to
          ONE dedupe key — the deduper would then suppress the SECOND alert for
          the whole cooldown window. The subject therefore embeds BOTH the
          account id AND the deployment id so each failed deployment is its own
          dedupe condition.

        The ``body`` is deliberately EXCLUDED — it carries the volatile stale-age
        number, which changes every tick while the condition itself does not, so
        keying on it would defeat dedupe entirely."""
        return (self.kind, self.subject)


def _router_spof_alert(age_s: float | None) -> FleetAlert | None:
    """Return a SPOF alert if the router heartbeat is stale or missing.

    ``None`` (never published) is fail-closed: an absent heartbeat means
    the router is not proving it is alive, so treat it as down.
    """
    if age_s is not None and age_s <= ROUTER_HEARTBEAT_SPOF_THRESHOLD_S:
        return None
    if age_s is None:
        body = (
            "The live-supervisor (router) has not published a heartbeat. "
            "It may have crashed on startup or never started. The fleet is "
            "UNMONITORED: no crashed node is being reaped or auto-restarted. "
            f"Threshold: {int(ROUTER_HEARTBEAT_SPOF_THRESHOLD_S)}s."
        )
    else:
        body = (
            f"The live-supervisor (router) heartbeat is {int(age_s)}s stale "
            f"(threshold {int(ROUTER_HEARTBEAT_SPOF_THRESHOLD_S)}s). The fleet "
            "is UNMONITORED: no crashed node is being reaped or auto-restarted. "
            "Check the live-supervisor container."
        )
    return FleetAlert(
        kind="router_spof",
        level="critical",
        subject="Live supervisor heartbeat stale (fleet unmonitored)",
        body=body,
    )


def _flat_and_unmonitored_alert(dep: DeploymentHealth) -> FleetAlert | None:
    """Return the flat-and-unmonitored alert for *dep*, if it qualifies.

    Qualifies when the deployment is ``failed`` AND its heartbeat is older
    than the stale threshold (or has NEVER been reported) AND no successful
    respawn has happened. A fresh heartbeat (reaper mid-decision) or a
    successful respawn (a live node is watching again) both clear it.

    ``dep.last_heartbeat_age_s is None`` is the NEVER-REPORTED case (a
    ``failed`` deployment with no node-process row): maximally stale by
    definition, so it ALWAYS qualifies and the age field renders as
    "never reported" rather than a number. Handling ``None`` explicitly is
    what keeps the formatter from raising (the prior ``int(float('inf'))``
    OverflowError silently suppressed this exact alert).
    """
    if dep.status != "failed":
        return None
    if dep.respawned_successfully:
        return None
    age_s = dep.last_heartbeat_age_s
    if age_s is not None and age_s <= STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S:
        return None
    age_phrase = (
        "never reported a heartbeat" if age_s is None else f"a {int(age_s)}s-stale heartbeat"
    )
    body = (
        f"Account {dep.account_id} deployment {dep.deployment_id} is in 'failed' "
        f"with {age_phrase} (threshold "
        f"{int(STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S)}s) and NO successful "
        "respawn. The account may be flat or holding an un-monitored position "
        "with no live node watching it. Investigate immediately."
    )
    return FleetAlert(
        kind="flat_and_unmonitored",
        level="critical",
        # Subject embeds the deployment id (NOT just the account id): a single
        # account can hold multiple failed deployments at once, and each is a
        # genuinely-distinct flat-and-unmonitored condition. Keying dedupe on the
        # account alone would suppress the SECOND deployment's (money-losing)
        # alert for the whole cooldown window. See FleetAlert.dedupe_key.
        subject=f"FLAT AND UNMONITORED: account {dep.account_id} deployment {dep.deployment_id}",
        body=body,
    )


def _account_consumer_missing_alerts(snapshot: FleetHealthSnapshot) -> list[FleetAlert]:
    """Return one alert per active-deployment account with no running consumer.

    PR 2 T4 review P2: the router heartbeat can be fresh while a per-account
    command consumer is missing (e.g. the boot-time account set was empty
    and the lazy refresh hasn't attached yet, or a refresher fault). An
    account that has an active deployment but is NOT in
    ``consumed_accounts`` cannot have a STOP / kill-all / drain consumed —
    the command would strand in the PEL and a live node would be
    un-stoppable via the platform. Page the operator.

    Inert when the snapshot carries no active-deployment accounts (legacy
    T9 snapshots, or a genuinely empty fleet).
    """
    consumed = set(snapshot.consumed_accounts)
    alerts: list[FleetAlert] = []
    for account_id in snapshot.accounts_with_active_deployments:
        if not account_id or account_id in consumed:
            continue
        body = (
            f"Account {account_id} has an active deployment but NO running "
            "command consumer on the live-supervisor. A STOP / kill-all / "
            "drain published for this account would strand un-consumed in "
            "the command stream — the live node is effectively UN-STOPPABLE "
            "via the platform until a consumer attaches. Check the "
            "live-supervisor's per-account consumer fan-out / GATEWAY_CONFIG."
        )
        alerts.append(
            FleetAlert(
                kind="account_consumer_missing",
                level="critical",
                subject=f"NO COMMAND CONSUMER: account {account_id} (un-stoppable)",
                body=body,
            )
        )
    return alerts


def detect_fleet_alerts(snapshot: FleetHealthSnapshot) -> list[FleetAlert]:
    """Evaluate *snapshot* and return every alert condition it triggers.

    Pure: no I/O, no logging side effects on the happy path. A healthy
    fleet returns an empty list.
    """
    alerts: list[FleetAlert] = []

    spof = _router_spof_alert(snapshot.router_heartbeat_age_s)
    if spof is not None:
        alerts.append(spof)

    for dep in snapshot.deployments:
        flat = _flat_and_unmonitored_alert(dep)
        if flat is not None:
            alerts.append(flat)

    alerts.extend(_account_consumer_missing_alerts(snapshot))

    return alerts


class FleetAlertDeduper:
    """Edge-triggered + cooldown dedupe state for the fleet-alert loop.

    The supervisor evaluates the fleet alerts every ~10s. Without dedupe a
    PERSISTING condition (a stale router heartbeat, a flat-and-unmonitored
    account, a missing per-account consumer) would re-send the SAME critical
    alert every tick — flooding the operator's SMTP inbox and evicting other
    entries from the bounded alert history.

    This deduper makes each distinct condition (keyed by
    :attr:`FleetAlert.dedupe_key`) behave like an edge-triggered latch with a
    reminder timer:

    * **First occurrence** of a key → SENT (the edge).
    * **Persisting** (same key on the next tick, within ``cooldown_s``) →
      SUPPRESSED.
    * **Persisting past ``cooldown_s``** → ONE reminder re-send, then the timer
      re-arms for the next window.
    * **Cleared** (the key is ABSENT from a later evaluation's detected set) →
      its state is DROPPED, so a future re-occurrence is treated as a fresh
      first occurrence and alerts IMMEDIATELY (edge re-arm). Dropping cleared
      keys also bounds the state to the currently-active conditions.

    State is the ``{key: last_sent_monotonic}`` dict, owned by the caller (the
    supervisor's ``_fleet_alert_loop`` constructs ONE instance OUTSIDE the loop
    and passes it to every tick, so the last-sent timestamps persist across the
    10s evaluations).

    ``time_source`` is injected (default :func:`time.monotonic`) so tests can
    advance a deterministic clock and exercise the cooldown without sleeping.
    Monotonic is correct here: we only ever compare elapsed deltas, never wall
    times, so a clock step (NTP, DST) can't spuriously suppress or re-fire.
    """

    def __init__(
        self,
        *,
        cooldown_s: float = FLEET_ALERT_COOLDOWN_S,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_s = cooldown_s
        self._now = time_source
        # key -> monotonic timestamp of the last send for that key.
        self._last_sent: dict[tuple[str, str], float] = {}

    def filter_and_record(self, detected: list[FleetAlert]) -> list[FleetAlert]:
        """Return only the alerts to SEND this tick, recording their send time.

        Suppresses persisting conditions inside the cooldown, lets first
        occurrences + post-cooldown reminders through, and drops state for any
        key NOT in *detected* so a cleared-then-reoccurring condition re-arms.
        """
        now = self._now()
        active_keys = {a.dedupe_key for a in detected}

        # Edge re-arm + unbounded-growth guard: forget any condition that is no
        # longer present so its next occurrence alerts immediately.
        for stale_key in self._last_sent.keys() - active_keys:
            del self._last_sent[stale_key]

        to_send: list[FleetAlert] = []
        for alert in detected:
            key = alert.dedupe_key
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self._cooldown_s:
                # Persisting within the cooldown window — suppress the re-send.
                continue
            self._last_sent[key] = now
            to_send.append(alert)
        return to_send


async def evaluate_and_alert_fleet(
    snapshot: FleetHealthSnapshot,
    *,
    alert_service: AlertService,
    deduper: FleetAlertDeduper | None = None,
) -> list[FleetAlert]:
    """Detect fleet alerts in *snapshot* and send each via *alert_service*.

    Returns the list of alerts that were ACTUALLY SENT this call so callers
    (and tests) can inspect what happened. A healthy fleet sends nothing and
    returns ``[]``.

    When *deduper* is supplied (the supervisor's running loop owns one across
    ticks), a PERSISTING condition is sent ONCE on first occurrence and then
    suppressed until ``FleetAlertDeduper`` lets a cooldown reminder through —
    so a stuck condition no longer re-pages the operator every ~10s evaluation.
    A cleared-then-reoccurring condition re-arms and pages again immediately.
    When *deduper* is ``None`` (e.g. a one-shot evaluation), every detected
    alert is sent — backward-compatible with the stateless caller.

    Each send is best-effort: :meth:`AlertService.send_alert` already
    swallows SMTP/history failures and records to the alert history, so a
    transient SMTP outage cannot suppress a later alert on the next pass. The
    ``log.critical`` is emitted only for alerts actually sent (a suppressed
    reminder logs nothing, keeping the log as quiet as the inbox).
    """
    detected = detect_fleet_alerts(snapshot)
    to_send = deduper.filter_and_record(detected) if deduper is not None else detected
    for alert in to_send:
        log.critical(
            "fleet_alert",
            alert_kind=alert.kind,
            subject=alert.subject,
        )
        await alert_service.send_alert(alert.subject, alert.body, level=alert.level)
    return to_send
