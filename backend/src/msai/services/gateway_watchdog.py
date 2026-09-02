"""Pure decision logic for the prod IB Gateway watchdog (see docs/prds/ib-gateway-watchdog.md).

PURE: decide() takes all inputs (including `now` and prior counter state) and returns a
Decision with the next state. No I/O, no clock, no Redis — the CLI tick wires those.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class GatewayHealth(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"  # docker start_period / IBC login in progress — NOT down
    UNKNOWN = "unknown"


class Action(StrEnum):
    NONE = "none"
    RESTART = "restart"
    ALERT_ONLY = "alert_only"  # down but must not restart (live deployment / unknown)
    ESCALATE = "escalate"  # persistent failure — stop restarting, CRITICAL alert
    RECOVERED = "recovered"  # came back after prior down — emit recovery alert


@dataclass(frozen=True)
class WatchdogConfig:
    grace_secs: float = 60.0
    restart_cap: int = 3  # K restarts within window before escalate
    window_secs: float = 900.0  # 15 min
    cooldown_secs: float = 1800.0  # 30 min
    alert_throttle_secs: float = (
        1800.0  # re-emit a *repeating* same-reason alert at most this often
    )


@dataclass(frozen=True)
class WatchdogState:
    down_since: float | None = None
    restart_events: list[float] = field(default_factory=list)
    cooldown_until: float | None = None
    escalated: bool = False  # tracks "was previously escalated" → recovery alert on return
    post_cooldown_retry_used: bool = False  # PRD "cooldown → ONE retry → re-escalate"
    last_alert_reason: str | None = None  # anti-storm: throttle repeating same-reason alerts
    last_alert_at: float | None = None


@dataclass(frozen=True)
class Signals:
    container_running: bool
    health: GatewayHealth
    gateway_connected: bool
    consecutive_failures: int
    live_deployment_active: bool | None  # None = could not determine (conservative)
    state: WatchdogState
    now: float
    config: WatchdogConfig


@dataclass(frozen=True)
class Decision:
    action: Action
    alert_level: str | None  # "warning" | "critical" | "info" | None
    alert_title: str | None
    alert_message: str | None
    new_state: WatchdogState
    reason: str


def _is_confirmed_healthy(s: Signals) -> bool:
    """Strict 'up': container running AND healthcheck healthy AND IB API connected.

    ONLY this resets state. STARTING/ambiguous must NOT reset — otherwise the several
    ``starting`` ticks Docker reports after a ``--force-recreate`` would wipe the
    anti-flap counters (restart_events/escalated/post_cooldown_retry_used) before the
    gateway ever proves healthy, defeating the restart cap + post-cooldown logic.
    """
    return s.container_running and s.health is GatewayHealth.HEALTHY and s.gateway_connected


def _is_down(s: Signals) -> bool:
    if not s.container_running:
        return True  # stopped/exited container is DOWN even if last health was 'starting'
    if s.health is GatewayHealth.STARTING:
        return False  # RUNNING + transitional — respect the 180s start_period / IBC login
    if s.health is GatewayHealth.UNHEALTHY:
        return True
    return not s.gateway_connected


def decide(s: Signals) -> Decision:
    cfg = s.config
    st = s.state

    if not _is_down(s):
        if _is_confirmed_healthy(s):
            # CONFIRMED healthy: reset. Emit a RECOVERED (info) alert ONLY if we had
            # ESCALATED — that clears a prior CRITICAL operator alert. A self-recovery
            # from a transient blip (or a routine idle-down restart, which already fired
            # its own WARNING) resets silently to avoid alert noise.
            recovered = st.escalated
            return Decision(
                action=Action.RECOVERED if recovered else Action.NONE,
                alert_level="info" if recovered else None,
                alert_title="IB Gateway recovered" if recovered else None,
                alert_message="ib-gateway session is healthy again." if recovered else None,
                new_state=WatchdogState(),
                reason="recovered" if recovered else "healthy",
            )
        # Not down, but not confirmed-healthy → STARTING / up-but-unconfirmed.
        # PRESERVE state, take no action (this is what survives a recreate's `starting`
        # ticks so the counters aren't wiped before recovery is confirmed).
        return Decision(Action.NONE, None, None, None, st, "transitional")

    # Down. Stamp down_since on first down tick.
    down_since = st.down_since if st.down_since is not None else s.now
    base = replace(st, down_since=down_since)

    # Grace: ignore brief blips.
    if s.now - down_since < cfg.grace_secs:
        return Decision(Action.NONE, None, None, None, base, "within-grace")

    # Cooldown after escalation: do nothing but stay escalated.
    if st.cooldown_until is not None and s.now < st.cooldown_until:
        return Decision(Action.NONE, None, None, None, base, "cooldown")

    # Halt-awareness / unknown live status → never restart; alert. THROTTLED: this
    # condition persists tick-after-tick, so de-dup the warning (alert once, then
    # suppress for alert_throttle_secs) to avoid a 30s alert storm. The ALERT_ONLY
    # action still returns every tick (so the host never restarts); only the alert
    # emission is rate-limited.
    if s.live_deployment_active is None or s.live_deployment_active:
        reason = "live-active" if s.live_deployment_active else "live-status-unknown"
        throttled = (
            st.last_alert_reason == reason
            and st.last_alert_at is not None
            and s.now - st.last_alert_at < cfg.alert_throttle_secs
        )
        if throttled:
            return Decision(Action.ALERT_ONLY, None, None, None, base, reason)
        return Decision(
            Action.ALERT_ONLY,
            "warning",
            "IB Gateway down during live trading",
            f"ib-gateway is down/unhealthy ({reason}); NOT restarting until the deployment "
            "halts/flattens. Node disconnect-handler will halt it.",
            replace(base, last_alert_reason=reason, last_alert_at=s.now),
            reason,
        )

    # Post-cooldown phase: a prior escalation's cooldown has elapsed (it passed the
    # cooldown guard above). PRD = "cooldown, then ONE retry + re-alert" — NOT a fresh
    # K-restart budget. (A successful retry hits the healthy/RECOVERED branch next tick,
    # which resets all state; the 180s start_period STARTING handling gives it time.)
    if st.escalated and st.cooldown_until is not None:
        if not st.post_cooldown_retry_used:
            return Decision(
                Action.RESTART,
                "warning",
                "IB Gateway post-cooldown retry",
                "Cooldown elapsed after a persistent-failure escalation — making ONE "
                "recovery attempt (recreate).",
                # Clear down_since so the recreate gets a fresh grace window before the
                # next down-assessment (the IBProbe refresh lags the recreate ~30s).
                replace(base, post_cooldown_retry_used=True, down_since=None),
                "post-cooldown-retry",
            )
        # Single retry already spent and still down → re-escalate + re-cooldown.
        return Decision(
            Action.ESCALATE,
            "critical",
            "IB Gateway STILL failing after cooldown retry",
            f"ib-gateway did not recover after the post-cooldown retry. Auto-restart "
            f"STOPPED again for {int(cfg.cooldown_secs / 60)} min. Operator intervention "
            "needed — likely an IBKR auth/config issue (paper-vs-live login / IB Key 2FA).",
            replace(
                base,
                restart_events=[],
                cooldown_until=s.now + cfg.cooldown_secs,
                escalated=True,
                post_cooldown_retry_used=False,
            ),
            "re-escalate-after-retry",
        )

    # Idle + down → normal anti-flap restart path.
    recent = [t for t in base.restart_events if s.now - t < cfg.window_secs]
    if len(recent) >= cfg.restart_cap:
        return Decision(
            Action.ESCALATE,
            "critical",
            "IB Gateway persistent failure — operator needed",
            f"ib-gateway failed to recover after {len(recent)} restarts in "
            f"{int(cfg.window_secs / 60)} min. Auto-restart STOPPED for "
            f"{int(cfg.cooldown_secs / 60)} min. Likely an IBKR auth/config issue "
            "(e.g. paper-vs-live login / IB Key 2FA) — needs manual fix.",
            replace(
                base,
                restart_events=recent,
                cooldown_until=s.now + cfg.cooldown_secs,
                escalated=True,
                post_cooldown_retry_used=False,
            ),
            "escalate-restart-cap",
        )

    return Decision(
        Action.RESTART,
        "warning",
        "IB Gateway auto-restart",
        f"ib-gateway down/unhealthy while idle — recreating (attempt {len(recent) + 1}).",
        # Clear down_since: after issuing a recreate, the next tick must re-stamp and
        # re-serve the full grace window so the recreate's start_period + IBProbe refresh
        # complete before we'd ever recreate again (prevents post-restart reflap).
        replace(base, restart_events=[*recent, s.now], down_since=None),
        "idle-down-restart",
    )
