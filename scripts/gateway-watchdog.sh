#!/usr/bin/env bash
# MSAI v2 — IB Gateway watchdog (host side).
#
# Runs as a systemd oneshot fired every ~30s by msai-gateway-watchdog.timer.
# Gathers `docker inspect` health for the ib-gateway container, delegates the
# decision to the PURE app function via
#   `docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-tick …`
# and acts on the host (recreates the gateway) ONLY when the tick prints the
# `restart` action token. All alerting + anti-flap counters live in the app/Redis;
# this script is intentionally thin.
#
# Decision tokens (last stdout line of the tick):
#   restart      → recreate ib-gateway (idle + down past grace)
#   none         → no action
#   alert_only   → down but live deployment active / live-status unknown (already alerted)
#   escalate     → persistent failure (already alerted CRITICAL; do NOT restart)
#   recovered    → came back healthy (already alerted info)
#
# A failed host action (missing rendered env, tick exec failure, compose recreate
# failure) is surfaced as a distinct CRITICAL via `--report-host-failure` so a broken
# host action is NEVER silent. See docs/plans/2026-06-10-ib-gateway-watchdog.md Task 3
# and docs/runbooks/ib-gateway.md.

set -euo pipefail

LOG=/var/log/msai-gateway-watchdog.log
RENDERED_ENV=/run/msai.env
# /run is tmpfs: msai-render-env.service regenerates /run/msai.env on boot, but
# /run/msai-images.env is written ONLY at deploy. After a reboot it's gone, so we
# fall back to the non-volatile mirror deploy-on-vm.sh persists at
# IMAGES_ENV_PERSIST — otherwise this boot-enabled watchdog could never recreate
# an unhealthy gateway post-reboot until the next deploy. Resolved below.
IMAGES_ENV=/run/msai-images.env
IMAGES_ENV_PERSIST=/opt/msai/msai-images.env
COMPOSE_FILE=/opt/msai/docker-compose.prod.yml

log() {
    # Timestamped line to both the journal (stdout) and the dedicated log file.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) gateway-watchdog: $*" | tee -a "$LOG"
}

# best_effort_report TEXT — emit a CRITICAL host-failure alert via the in-app CLI.
# Best-effort: if even this fails, the journal/log line above remains the record.
best_effort_report() {
    docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-tick \
        --report-host-failure "$1" >>"$LOG" 2>&1 \
        || log "WARN: --report-host-failure invocation itself failed (alert may be lost)"
}

# ── Secure temp file for the tick output (created just before use, below).
# This script runs as ROOT; a fixed world-writable path like /tmp/wd-tick.out is a
# pre-creation/symlink/DoS vector (a non-root user could pre-make it a dir/FIFO/symlink
# and wedge every tick). We mktemp under root-owned /run instead, and rm on EXIT.
TICK_OUT=""
# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT`
cleanup() { [[ -n "$TICK_OUT" ]] && rm -f "$TICK_OUT"; }
trap cleanup EXIT

# ── Non-overlap guard (belt-and-suspenders on top of systemd oneshot serialization).
exec 9>/run/msai-gateway-watchdog.lock
if ! flock -n 9; then
    log "another watchdog tick is still running; skipping this fire"
    exit 0
fi

# ── Resolve the images env: prefer the deploy-written /run copy, else the
# non-volatile mirror that survives a reboot (see IMAGES_ENV_PERSIST above).
if [[ -f "$IMAGES_ENV" ]]; then
    IMAGES_ENV_USE="$IMAGES_ENV"
elif [[ -f "$IMAGES_ENV_PERSIST" ]]; then
    IMAGES_ENV_USE="$IMAGES_ENV_PERSIST"
    log "using persistent images env ($IMAGES_ENV_PERSIST); /run copy absent (post-reboot)"
else
    IMAGES_ENV_USE=""
fi

# ── Fail-safe: both the KV-rendered env (boot-recreated by the render service)
# and an images env (from either location) must exist to recreate the gateway.
if [[ ! -f "$RENDERED_ENV" || -z "$IMAGES_ENV_USE" ]]; then
    log "env missing (rendered=$RENDERED_ENV present?$([[ -f $RENDERED_ENV ]] && echo y || echo n); images=$IMAGES_ENV|$IMAGES_ENV_PERSIST) — cannot recreate gateway"
    best_effort_report "watchdog env missing (rendered=$RENDERED_ENV, images=$IMAGES_ENV|$IMAGES_ENV_PERSIST) — render/deploy may have failed; gateway cannot be recreated"
    # Don't crash-loop the timer; the render/deploy own these files.
    exit 0
fi

# ── Gather container running + health from docker inspect. `none` → unknown.
GW=$(docker inspect msai-ib-gateway-1 \
    --format '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    2>/dev/null || echo "false|none")
RUNNING="${GW%%|*}"
HEALTH_RAW="${GW##*|}"
case "$HEALTH_RAW" in
    healthy | unhealthy | starting) HEALTH="$HEALTH_RAW" ;;
    *) HEALTH="unknown" ;; # none / empty / unexpected → unknown
esac

# Map .State.Running → the BARE Typer flag. Typer's dual-bool flag rejects
# `--container-running=true` (exit 2); pass the bare form.
if [[ "$RUNNING" == "true" ]]; then
    RUNNING_FLAG="--container-running"
else
    RUNNING_FLAG="--no-container-running"
fi

# ── Run the tick, capturing rc + output WITHOUT a pipe masking the exit code.
# Use `python -m msai.cli` (the prod image is built with `uv sync
# --no-install-project`, so the `msai` console-script is NOT installed).
# `rc=0; … || rc=$?` keeps the real exit code under `set -e` (a bare `cmd; rc=$?`
# would let `set -e` exit on a non-zero tick before the rc-guard ever runs).
TICK_OUT=$(mktemp /run/msai-gateway-watchdog.tick.XXXXXX) || {
    log "could not create secure temp file under /run; skipping this fire"
    best_effort_report "gateway-watchdog could not create secure temp file under /run (disk full / /run unwritable?)"
    exit 0
}
rc=0
docker exec msai-backend-1 python -m msai.cli system gateway-watchdog-tick \
    "$RUNNING_FLAG" --container-health "$HEALTH" \
    >"$TICK_OUT" 2>>"$LOG" || rc=$?
# Parse the sentinel-prefixed token (NOT `tail -1`): prod JSON logs share the
# tick's stdout, so a stray log line could be the literal last line. `|| true`
# keeps the empty-on-no-match case (set -o pipefail would else abort here),
# which the rc/empty guard below treats as a failed tick.
ACTION=$(grep -oE 'WATCHDOG_ACTION=[a-z_]+' "$TICK_OUT" | tail -n1 | cut -d= -f2 || true)

if [[ $rc -ne 0 || -z "$ACTION" ]]; then
    log "tick exec failed (rc=$rc); ACTION='$ACTION' — NOT restarting"
    best_effort_report "gateway-watchdog tick exec failed (rc=$rc); ACTION='$ACTION'"
    exit 0
fi

log "running=$RUNNING health=$HEALTH → action=$ACTION"

case "$ACTION" in
    restart)
        log "recreating ib-gateway (--force-recreate)"
        crc=0
        COMPOSE_PROFILES=broker docker compose \
            --project-name msai \
            --env-file "$RENDERED_ENV" \
            --env-file "$IMAGES_ENV_USE" \
            -f "$COMPOSE_FILE" \
            up -d --force-recreate ib-gateway >>"$LOG" 2>&1 || crc=$?
        if [[ $crc -ne 0 ]]; then
            log "docker compose --force-recreate ib-gateway FAILED (rc=$crc)"
            best_effort_report "docker compose --force-recreate ib-gateway FAILED (rc=$crc) — gateway not recreated despite watchdog decision; manual intervention needed"
            exit 0
        fi
        log "ib-gateway recreate issued OK"
        ;;
    none | alert_only | escalate | recovered)
        # No host action — the in-app tick already alerted where needed.
        ;;
    *)
        log "unexpected action token '$ACTION' — no host action"
        ;;
esac

exit 0
