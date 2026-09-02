from msai.services.gateway_watchdog import (
    Action,
    GatewayHealth,
    Signals,
    WatchdogConfig,
    WatchdogState,
    decide,
)

CFG = WatchdogConfig(grace_secs=60, restart_cap=3, window_secs=900, cooldown_secs=1800)


def _sig(**kw):
    base = dict(
        container_running=True,
        health=GatewayHealth.HEALTHY,
        gateway_connected=True,
        consecutive_failures=0,
        live_deployment_active=False,
        state=WatchdogState(),
        now=1000.0,
        config=CFG,
    )
    base.update(kw)
    return Signals(**base)


def test_healthy_resets_counter():
    s = _sig(state=WatchdogState(restart_events=[100.0, 200.0], down_since=50.0))
    d = decide(s)
    assert d.action is Action.NONE
    assert d.new_state.restart_events == []
    assert d.new_state.down_since is None


def test_idle_down_past_grace_restarts():
    # down_since old enough to clear grace
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        state=WatchdogState(down_since=900.0),
    )  # now=1000, grace=60
    d = decide(s)
    assert d.action is Action.RESTART
    assert len(d.new_state.restart_events) == 1


def test_down_within_grace_waits():
    s = _sig(gateway_connected=False, health=GatewayHealth.UNHEALTHY, state=WatchdogState())
    d = decide(s)  # down_since just set this tick → within grace
    assert d.action is Action.NONE
    assert d.new_state.down_since == s.now


def test_starting_is_not_down():
    s = _sig(
        health=GatewayHealth.STARTING,
        gateway_connected=False,
        container_running=True,
        state=WatchdogState(down_since=900.0),
    )
    d = decide(s)
    assert d.action is Action.NONE  # RUNNING + starting → respect start_period


def test_stopped_container_with_stale_starting_health_is_down():
    # container exited but docker still reports last health 'starting' → must be DOWN,
    # not transitional-forever (container_running dominates STARTING).
    s = _sig(
        container_running=False,
        health=GatewayHealth.STARTING,
        gateway_connected=False,
        state=WatchdogState(down_since=900.0),
    )
    d = decide(s)
    assert d.action is Action.RESTART  # idle + down past grace


def test_live_active_down_alerts_no_restart():
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        live_deployment_active=True,
        state=WatchdogState(down_since=900.0),
    )
    d = decide(s)
    assert d.action is Action.ALERT_ONLY
    assert d.alert_level == "warning"
    assert d.new_state.restart_events == []  # did NOT restart under a live deployment


def test_escalates_after_cap():
    evts = [400.0, 500.0, 600.0]  # 3 within window (now=1000, window=900 → >100)
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        state=WatchdogState(down_since=300.0, restart_events=evts),
    )
    d = decide(s)
    assert d.action is Action.ESCALATE
    assert d.alert_level == "critical"
    assert d.new_state.cooldown_until == s.now + CFG.cooldown_secs


def test_cooldown_suppresses_restart():
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        state=WatchdogState(down_since=300.0, cooldown_until=2000.0),
    )  # now=1000 < 2000
    d = decide(s)
    assert d.action is Action.NONE


def test_live_status_unknown_is_conservative():
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        live_deployment_active=None,
        state=WatchdogState(down_since=900.0),
    )
    d = decide(s)
    assert d.action is Action.ALERT_ONLY  # do NOT restart when we can't confirm idle


def test_post_cooldown_single_retry():
    # escalated; cooldown elapsed (now=1000 >= cooldown_until=900); retry not yet used
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        state=WatchdogState(down_since=300.0, cooldown_until=900.0, escalated=True),
    )
    d = decide(s)
    assert d.action is Action.RESTART
    assert d.reason == "post-cooldown-retry"
    assert d.new_state.post_cooldown_retry_used is True


def test_post_cooldown_reescalates_after_retry_still_down():
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        state=WatchdogState(
            down_since=300.0, cooldown_until=900.0, escalated=True, post_cooldown_retry_used=True
        ),
    )
    d = decide(s)
    assert d.action is Action.ESCALATE
    assert d.alert_level == "critical"
    assert d.new_state.cooldown_until == s.now + CFG.cooldown_secs
    assert d.new_state.post_cooldown_retry_used is False


def test_recovery_after_escalation_emits_info():
    # confirmed-healthy after a prior escalation → RECOVERED (clears the CRITICAL)
    s = _sig(
        state=WatchdogState(escalated=True, down_since=300.0, cooldown_until=2000.0)
    )  # healthy signals (defaults)
    d = decide(s)
    assert d.action is Action.RECOVERED
    assert d.alert_level == "info"
    assert d.new_state == WatchdogState()  # full reset


def test_self_recovery_without_escalation_is_silent():
    # healthy after a transient blip we never escalated → silent reset, no alert
    s = _sig(state=WatchdogState(down_since=300.0, restart_events=[400.0]))
    d = decide(s)
    assert d.action is Action.NONE
    assert d.alert_level is None
    assert d.new_state == WatchdogState()


def test_alert_only_throttled_within_window():
    # same live-active reason already alerted at t=999 (now=1000, throttle=1800) → suppress
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        live_deployment_active=True,
        state=WatchdogState(down_since=300.0, last_alert_reason="live-active", last_alert_at=999.0),
    )
    d = decide(s)
    assert d.action is Action.ALERT_ONLY  # still ALERT_ONLY (host never restarts)
    assert d.alert_level is None  # but the alert is throttled (no storm)


def test_alert_only_reemits_after_throttle_window():
    s = _sig(
        gateway_connected=False,
        health=GatewayHealth.UNHEALTHY,
        live_deployment_active=True,
        state=WatchdogState(
            down_since=300.0, last_alert_reason="live-active", last_alert_at=-1000.0
        ),
    )  # now=1000 → past throttle window
    d = decide(s)
    assert d.action is Action.ALERT_ONLY
    assert d.alert_level == "warning"
    assert d.new_state.last_alert_at == s.now
