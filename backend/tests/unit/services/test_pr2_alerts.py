"""Unit tests for PR 2 mandatory fleet alerts (T9).

Two detectors guard the per-account supervisor fleet:

* **Router-SPOF alert** — the single ``live-supervisor`` is a SPOF in the
  Opt-4-refined topology. If its heartbeat goes stale
  (``router_heartbeat_age_s > 30``) the whole fleet is unmonitored and no
  auto-restart will fire. Operators must be paged.
* **Flat-and-unmonitored alert** — the single most important alert. A
  deployment is in ``failed`` with a stale heartbeat
  (``last_heartbeat_at`` age > 60s) AND no successful respawn happened:
  the account may be flat (or holding an un-monitored position) with no
  live node watching it. This is the money-losing case.

A healthy fleet (fresh router heartbeat, all deployments running) fires
neither alert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from msai.services.fleet_alerts import (
    FLEET_ALERT_COOLDOWN_S,
    ROUTER_HEARTBEAT_SPOF_THRESHOLD_S,
    STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S,
    DeploymentHealth,
    FleetAlert,
    FleetAlertDeduper,
    FleetHealthSnapshot,
    _env_float,
    detect_fleet_alerts,
    evaluate_and_alert_fleet,
)


def _running(account_id: str = "DU111", **kw: object) -> DeploymentHealth:
    """A healthy running deployment with a fresh heartbeat."""
    defaults: dict[str, object] = {
        "deployment_id": "dep-1",
        "account_id": account_id,
        "status": "running",
        "last_heartbeat_age_s": 2.0,
        "respawned_successfully": True,
    }
    defaults.update(kw)
    return DeploymentHealth(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure detection layer
# ---------------------------------------------------------------------------


class TestDetectFleetAlerts:
    def test_stale_router_heartbeat_fires_spof_alert(self) -> None:
        # Arrange — router heartbeat older than the 30s SPOF threshold.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 5.0,
            deployments=[_running()],
        )

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert — exactly one SPOF alert, critical, names the age.
        spof = [a for a in alerts if a.kind == "router_spof"]
        assert len(spof) == 1
        assert spof[0].level == "critical"
        assert "supervisor" in spof[0].subject.lower()
        assert str(int(snapshot.router_heartbeat_age_s)) in spof[0].body  # type: ignore[arg-type]

    def test_missing_router_heartbeat_fires_spof_alert(self) -> None:
        # Arrange — a missing heartbeat (None) is treated as fail-closed:
        # the router has never published, so it is NOT monitoring.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=None,
            deployments=[_running()],
        )

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert
        assert any(a.kind == "router_spof" for a in alerts)

    def test_failed_stale_no_respawn_fires_flat_and_unmonitored(self) -> None:
        # Arrange — failed deployment, heartbeat older than 60s, no respawn.
        bad = _running(
            account_id="DU999",
            deployment_id="dep-flat",
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 10.0,
            respawned_successfully=False,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[bad])

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert — the flat-and-unmonitored alert, critical, names the account.
        flat = [a for a in alerts if a.kind == "flat_and_unmonitored"]
        assert len(flat) == 1
        assert flat[0].level == "critical"
        assert "DU999" in flat[0].body
        assert "dep-flat" in flat[0].body

    def test_failed_no_heartbeat_row_fires_and_renders_never_reported(self) -> None:
        # Arrange — the worst real-money case: a ``failed`` deployment with NO
        # ``live_node_processes`` row at all (the node never reported a single
        # heartbeat). The snapshot builder represents "never reported" as
        # ``last_heartbeat_age_s=None``. The flat-and-unmonitored alert — the
        # single most important alert — MUST still FIRE for exactly this case,
        # and must NOT raise (a prior bug stored ``float('inf')`` here, which
        # made ``int(inf)`` raise OverflowError and SILENTLY SUPPRESSED the
        # alert before it could send).
        never_reported = _running(
            account_id="DU000",
            deployment_id="dep-never",
            status="failed",
            last_heartbeat_age_s=None,
            respawned_successfully=False,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[never_reported])

        # Act — must not raise.
        alerts = detect_fleet_alerts(snapshot)

        # Assert — the alert fires, critical, names the account, and the age
        # field reads sensibly (a never-reported sentinel, not a number).
        flat = [a for a in alerts if a.kind == "flat_and_unmonitored"]
        assert len(flat) == 1
        assert flat[0].level == "critical"
        assert "DU000" in flat[0].body
        assert "dep-never" in flat[0].body
        assert "never reported" in flat[0].body.lower()

    def test_two_failed_deployments_same_account_have_distinct_dedupe_keys(self) -> None:
        # FINDING 1 (P1): a single account can hold MULTIPLE failed deployments at
        # once (``LiveDeployment`` is unique on ``(portfolio_revision_id,
        # account_id)``). Each is a genuinely-distinct money-losing
        # flat-and-unmonitored condition. The dedupe key must therefore embed the
        # DEPLOYMENT — not just the account — so the deduper does not collapse the
        # two and suppress the SECOND alert for the whole cooldown window.
        #
        # Pre-fix: subject was account-only, so both alerts shared one dedupe key
        # and the second was suppressed.
        same_account = "DU777"
        dep_a = _running(
            account_id=same_account,
            deployment_id="dep-a",
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 10.0,
            respawned_successfully=False,
        )
        dep_b = _running(
            account_id=same_account,
            deployment_id="dep-b",
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 20.0,
            respawned_successfully=False,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[dep_a, dep_b])

        # Act
        flat = [a for a in detect_fleet_alerts(snapshot) if a.kind == "flat_and_unmonitored"]

        # Assert — two distinct alerts, each naming its own deployment, with
        # DISTINCT dedupe keys (so neither suppresses the other).
        assert len(flat) == 2
        assert {a.subject for a in flat} != {flat[0].subject}, "subjects must differ per deployment"
        assert flat[0].dedupe_key != flat[1].dedupe_key, (
            "two failed deployments on the same account must be distinct dedupe "
            "conditions — keying on the account alone would suppress the second"
        )
        bodies = " ".join(a.body for a in flat)
        assert "dep-a" in bodies
        assert "dep-b" in bodies

    def test_two_failed_deployments_same_account_both_pass_deduper(self) -> None:
        # FINDING 1 (P1): the falsifying end-to-end check. A SINGLE snapshot tick
        # carrying two failed deployments on the SAME account must let BOTH alerts
        # through the deduper on first occurrence. Pre-fix the deduper, keyed on
        # the account-only subject, suppressed the second.
        same_account = "DU888"
        dep_a = _running(
            account_id=same_account,
            deployment_id="dep-x",
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 10.0,
            respawned_successfully=False,
        )
        dep_b = _running(
            account_id=same_account,
            deployment_id="dep-y",
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 11.0,
            respawned_successfully=False,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[dep_a, dep_b])
        detected = [a for a in detect_fleet_alerts(snapshot) if a.kind == "flat_and_unmonitored"]
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=_Clock())

        # Act — first occurrence: both must pass.
        sent = [a for a in deduper.filter_and_record(detected) if a.kind == "flat_and_unmonitored"]

        # Assert
        assert len(sent) == 2, (
            "both flat-and-unmonitored alerts on the same account must page on "
            "first occurrence — the deduper must NOT collapse them"
        )

    def test_failed_but_respawned_does_not_fire(self) -> None:
        # Arrange — failed + stale, but a successful respawn happened, so a
        # live node IS watching the account again. No alert.
        recovered = _running(
            status="failed",
            last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 10.0,
            respawned_successfully=True,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[recovered])

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert
        assert not any(a.kind == "flat_and_unmonitored" for a in alerts)

    def test_failed_but_fresh_heartbeat_does_not_fire(self) -> None:
        # Arrange — status failed but heartbeat still fresh (< 60s): the
        # reaper is mid-decision; not yet flat-and-unmonitored.
        fresh_failed = _running(
            status="failed",
            last_heartbeat_age_s=5.0,
            respawned_successfully=False,
        )
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[fresh_failed])

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert
        assert not any(a.kind == "flat_and_unmonitored" for a in alerts)

    def test_healthy_fleet_fires_neither(self) -> None:
        # Arrange — fresh router heartbeat, all deployments running fresh.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=3.0,
            deployments=[_running("DU111"), _running("DU222", deployment_id="dep-2")],
        )

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert — empty.
        assert alerts == []

    def test_router_threshold_boundary_is_exclusive(self) -> None:
        # Arrange — age exactly at the threshold must NOT fire (> is strict).
        at = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S,
            deployments=[_running()],
        )
        over = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 0.001,
            deployments=[_running()],
        )

        # Act / Assert
        assert not any(a.kind == "router_spof" for a in detect_fleet_alerts(at))
        assert any(a.kind == "router_spof" for a in detect_fleet_alerts(over))

    def test_active_account_without_consumer_fires_uncovered_alert(self) -> None:
        # Arrange — router heartbeat fresh, but an account with an active
        # deployment has NO running command consumer (P2: liveness decoupled
        # from consumption). A STOP/kill-all/drain for it would strand.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=2.0,
            deployments=[_running("DU-COVERED")],
            accounts_with_active_deployments=["DU-COVERED", "DU-UNCOVERED"],
            consumed_accounts=["DU-COVERED"],
        )

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert — exactly one uncovered-account alert, critical, names it.
        uncovered = [a for a in alerts if a.kind == "account_consumer_missing"]
        assert len(uncovered) == 1
        assert uncovered[0].level == "critical"
        assert "DU-UNCOVERED" in uncovered[0].body

    def test_all_active_accounts_covered_does_not_fire_uncovered(self) -> None:
        # Arrange — every active-deployment account has a running consumer.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=2.0,
            deployments=[_running("DU-A")],
            accounts_with_active_deployments=["DU-A", "DU-B"],
            consumed_accounts=["DU-A", "DU-B", "DU-EXTRA"],
        )

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert
        assert not any(a.kind == "account_consumer_missing" for a in alerts)

    def test_uncovered_detector_inert_without_coverage_data(self) -> None:
        # Arrange — legacy snapshots (T9) carry no coverage fields. The
        # detector must not fire when it has no consumer-coverage data to
        # reason about (default empty lists => nothing uncovered).
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[_running()])

        # Act
        alerts = detect_fleet_alerts(snapshot)

        # Assert
        assert not any(a.kind == "account_consumer_missing" for a in alerts)

    def test_both_alerts_fire_together(self) -> None:
        # Arrange — both conditions present at once.
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 1.0,
            deployments=[
                _running(
                    status="failed",
                    last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 1.0,
                    respawned_successfully=False,
                )
            ],
        )

        # Act
        kinds = {a.kind for a in detect_fleet_alerts(snapshot)}

        # Assert
        assert kinds == {"router_spof", "flat_and_unmonitored"}


# ---------------------------------------------------------------------------
# Async fire layer (wires the detector to AlertService)
# ---------------------------------------------------------------------------


class TestEvaluateAndAlertFleet:
    @pytest.mark.asyncio
    async def test_sends_one_alert_per_detected_condition(self) -> None:
        # Arrange
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 1.0,
            deployments=[
                _running(
                    status="failed",
                    last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 1.0,
                    respawned_successfully=False,
                )
            ],
        )

        # Act
        fired = await evaluate_and_alert_fleet(snapshot, alert_service=alert_service)

        # Assert — both alerts sent through the alert service.
        assert {a.kind for a in fired} == {"router_spof", "flat_and_unmonitored"}
        assert alert_service.send_alert.await_count == 2
        levels = {call.kwargs["level"] for call in alert_service.send_alert.await_args_list}
        assert levels == {"critical"}

    @pytest.mark.asyncio
    async def test_never_reported_failed_deployment_is_not_suppressed(self) -> None:
        # Arrange — a ``failed`` deployment that never reported a heartbeat
        # (no node row → ``last_heartbeat_age_s=None``). This is the exact case
        # the OverflowError bug silently suppressed: the formatter's ``int(...)``
        # raised before the send, the loop caught it, and the single most
        # important alert NEVER paged. It must now flow through to the service.
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=2.0,
            deployments=[
                _running(
                    account_id="DU000",
                    status="failed",
                    last_heartbeat_age_s=None,
                    respawned_successfully=False,
                )
            ],
        )

        # Act
        fired = await evaluate_and_alert_fleet(snapshot, alert_service=alert_service)

        # Assert — the alert fired AND was actually sent (not suppressed).
        assert {a.kind for a in fired} == {"flat_and_unmonitored"}
        alert_service.send_alert.assert_awaited_once()
        assert alert_service.send_alert.await_args.kwargs["level"] == "critical"

    @pytest.mark.asyncio
    async def test_healthy_fleet_sends_nothing(self) -> None:
        # Arrange
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[_running()])

        # Act
        fired = await evaluate_and_alert_fleet(snapshot, alert_service=alert_service)

        # Assert
        assert fired == []
        alert_service.send_alert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dedupe / cooldown layer (edge-triggered re-send suppression)
# ---------------------------------------------------------------------------


class _Clock:
    """A deterministic, hand-advanced monotonic time source for the deduper."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _spof(age: float = 999.0) -> FleetAlert:
    """A router-SPOF alert (one stable dedupe key — no entity)."""
    return FleetAlert(
        kind="router_spof",
        level="critical",
        subject="Live supervisor heartbeat stale (fleet unmonitored)",
        body=f"stale {int(age)}s",
    )


def _flat(account_id: str = "DU999", deployment_id: str = "dep-1") -> FleetAlert:
    """A flat-and-unmonitored alert. The dedupe key distinguishes the DEPLOYMENT
    (FINDING 1): a single account can hold multiple failed deployments at once, so
    the subject embeds both the account AND the deployment id — mirroring
    :func:`msai.services.fleet_alerts._flat_and_unmonitored_alert`."""
    return FleetAlert(
        kind="flat_and_unmonitored",
        level="critical",
        subject=f"FLAT AND UNMONITORED: account {account_id} deployment {deployment_id}",
        body=f"account {account_id} deployment {deployment_id} is flat",
    )


class TestFleetAlertDedupeKey:
    def test_same_kind_and_subject_share_a_key(self) -> None:
        # Two SPOF alerts (same condition) collapse to one key.
        assert _spof(10.0).dedupe_key == _spof(900.0).dedupe_key

    def test_distinct_accounts_have_distinct_keys(self) -> None:
        # Two different flat-and-unmonitored accounts are distinct conditions.
        assert _flat("DU111").dedupe_key != _flat("DU222").dedupe_key

    def test_same_account_distinct_deployments_have_distinct_keys(self) -> None:
        # FINDING 1 (P1): two failed deployments on the SAME account are distinct
        # conditions — the dedupe key must distinguish them by deployment.
        assert _flat("DU111", "dep-a").dedupe_key != _flat("DU111", "dep-b").dedupe_key, (
            "same-account different-deployment flat alerts must be distinct dedupe conditions"
        )

    def test_distinct_kinds_have_distinct_keys(self) -> None:
        assert _spof().dedupe_key != _flat().dedupe_key


class TestFleetAlertDeduper:
    def test_first_occurrence_passes_through(self) -> None:
        # Arrange
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert = _spof()

        # Act
        out = deduper.filter_and_record([alert])

        # Assert — first occurrence is always sent.
        assert out == [alert]

    def test_persisting_condition_suppressed_within_cooldown(self) -> None:
        # Arrange — same condition on two consecutive ticks, no time advance.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert = _spof()

        # Act — tick 1 sends, tick 2 (a few seconds later) suppresses.
        first = deduper.filter_and_record([alert])
        clock.advance(10.0)  # the 10s evaluation cadence
        second = deduper.filter_and_record([alert])

        # Assert
        assert first == [alert]
        assert second == [], "a persisting condition must not re-send every tick"

    def test_reminder_resend_after_cooldown_elapses(self) -> None:
        # Arrange — condition persists across the whole cooldown window.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert = _spof()

        # Act
        first = deduper.filter_and_record([alert])
        clock.advance(10.0)
        suppressed = deduper.filter_and_record([alert])
        clock.advance(1800.0)  # cooldown elapsed (10 + 1800 >= 1800)
        reminder = deduper.filter_and_record([alert])

        # Assert — exactly one reminder re-send after the cooldown.
        assert first == [alert]
        assert suppressed == []
        assert reminder == [alert], (
            "a persisting condition must re-send ONE reminder after cooldown"
        )

    def test_cleared_then_reoccurring_rearms_immediately(self) -> None:
        # Arrange — condition fires, clears (absent next tick), then re-occurs
        # well within the cooldown window.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert = _spof()

        # Act
        first = deduper.filter_and_record([alert])
        clock.advance(10.0)
        cleared = deduper.filter_and_record([])  # condition gone this tick
        clock.advance(10.0)
        reoccur = deduper.filter_and_record([alert])  # back again, well inside cooldown

        # Assert — the re-occurrence alerts IMMEDIATELY (edge-triggered re-arm),
        # NOT suppressed by the still-unelapsed cooldown.
        assert first == [alert]
        assert cleared == []
        assert reoccur == [alert], "a cleared-then-reoccurring condition must re-arm and re-send"

    def test_distinct_conditions_are_independent(self) -> None:
        # Arrange — a persisting SPOF must not suppress a NEW flat alert.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        spof = _spof()
        flat = _flat("DU999")

        # Act — tick 1: only SPOF. tick 2: SPOF persists + flat newly appears.
        first = deduper.filter_and_record([spof])
        clock.advance(10.0)
        second = deduper.filter_and_record([spof, flat])

        # Assert — SPOF suppressed (persisting), flat sent (new).
        assert first == [spof]
        assert second == [flat], "a persisting alert must not suppress a different new alert"

    def test_state_does_not_accumulate_cleared_keys(self) -> None:
        # Arrange — a churn of distinct accounts that each clear next tick must
        # not leave their keys behind unbounded.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)

        # Act — three different accounts, each present for exactly one tick.
        deduper.filter_and_record([_flat("DU-A")])
        clock.advance(10.0)
        deduper.filter_and_record([_flat("DU-B")])
        clock.advance(10.0)
        deduper.filter_and_record([_flat("DU-C")])

        # Assert — only the currently-active key is retained.
        assert set(deduper._last_sent) == {_flat("DU-C").dedupe_key}


# ---------------------------------------------------------------------------
# evaluate_and_alert_fleet with a deduper (the wired, stateful contract)
# ---------------------------------------------------------------------------


class TestEvaluateAndAlertFleetDedupe:
    @pytest.mark.asyncio
    async def test_persisting_condition_sends_once_then_suppresses(self) -> None:
        # Arrange — a SPOF condition that persists across evaluation ticks.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 120.0,
            deployments=[_running()],
        )

        # Act — evaluate twice (10s apart) with the SAME deduper.
        first = await evaluate_and_alert_fleet(
            snapshot, alert_service=alert_service, deduper=deduper
        )
        clock.advance(10.0)
        second = await evaluate_and_alert_fleet(
            snapshot, alert_service=alert_service, deduper=deduper
        )

        # Assert — sent exactly once across the two ticks; return reflects only
        # what was sent.
        assert {a.kind for a in first} == {"router_spof"}
        assert second == []
        assert alert_service.send_alert.await_count == 1

    @pytest.mark.asyncio
    async def test_reminder_after_cooldown(self) -> None:
        # Arrange
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 120.0,
            deployments=[_running()],
        )

        # Act
        await evaluate_and_alert_fleet(snapshot, alert_service=alert_service, deduper=deduper)
        clock.advance(10.0)
        await evaluate_and_alert_fleet(snapshot, alert_service=alert_service, deduper=deduper)
        clock.advance(1800.0)
        reminder = await evaluate_and_alert_fleet(
            snapshot, alert_service=alert_service, deduper=deduper
        )

        # Assert — one reminder re-send after the cooldown window.
        assert {a.kind for a in reminder} == {"router_spof"}
        assert alert_service.send_alert.await_count == 2

    @pytest.mark.asyncio
    async def test_cleared_then_reoccurring_realerts(self) -> None:
        # Arrange — SPOF fires, clears (healthy snapshot), re-occurs.
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert_service = AsyncMock()
        bad = FleetHealthSnapshot(
            router_heartbeat_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 120.0,
            deployments=[_running()],
        )
        healthy = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[_running()])

        # Act
        await evaluate_and_alert_fleet(bad, alert_service=alert_service, deduper=deduper)
        clock.advance(10.0)
        cleared = await evaluate_and_alert_fleet(
            healthy, alert_service=alert_service, deduper=deduper
        )
        clock.advance(10.0)
        reoccur = await evaluate_and_alert_fleet(bad, alert_service=alert_service, deduper=deduper)

        # Assert — the re-occurrence pages again immediately (re-armed), even
        # though the cooldown window has NOT elapsed.
        assert cleared == []
        assert {a.kind for a in reoccur} == {"router_spof"}
        assert alert_service.send_alert.await_count == 2

    @pytest.mark.asyncio
    async def test_healthy_fleet_with_deduper_sends_nothing(self) -> None:
        # Arrange
        clock = _Clock()
        deduper = FleetAlertDeduper(cooldown_s=1800.0, time_source=clock)
        alert_service = AsyncMock()
        snapshot = FleetHealthSnapshot(router_heartbeat_age_s=2.0, deployments=[_running()])

        # Act
        fired = await evaluate_and_alert_fleet(
            snapshot, alert_service=alert_service, deduper=deduper
        )

        # Assert
        assert fired == []
        alert_service.send_alert.assert_not_awaited()


class TestFleetAlertCooldownConstant:
    def test_default_is_a_sensible_reminder_interval(self) -> None:
        # 30 min default — frequent enough to remind, rare enough not to flood.
        assert FLEET_ALERT_COOLDOWN_S == 1800.0

    def test_env_override_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mirrors the existing supervisor env-constant pattern.
        monkeypatch.setenv("MSAI_FLEET_ALERT_COOLDOWN_S", "60")
        assert _env_float("MSAI_FLEET_ALERT_COOLDOWN_S", 1800.0) == 60.0


# ---------------------------------------------------------------------------
# Threshold env-override parsing (fail-safe: never silently disable an alert)
# ---------------------------------------------------------------------------


class TestEnvFloat:
    def test_missing_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MSAI_X", raising=False)
        assert _env_float("MSAI_X", 30.0) == 30.0

    def test_valid_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MSAI_X", "15")
        assert _env_float("MSAI_X", 30.0) == 15.0

    def test_unparsable_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A fat-fingered override must NOT silently disable the alert.
        monkeypatch.setenv("MSAI_X", "not-a-number")
        assert _env_float("MSAI_X", 30.0) == 30.0

    def test_non_positive_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 0 or negative would make every check fire or never fire — reject it.
        monkeypatch.setenv("MSAI_X", "0")
        assert _env_float("MSAI_X", 30.0) == 30.0
        monkeypatch.setenv("MSAI_X", "-5")
        assert _env_float("MSAI_X", 30.0) == 30.0
