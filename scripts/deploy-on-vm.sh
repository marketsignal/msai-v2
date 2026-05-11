#!/usr/bin/env bash
# MSAI v2 — per-deploy script that runs ON the VM (invoked via SSH from
# .github/workflows/deploy.yml). Idempotent. Owns ACR auth, image roll-forward,
# success-signal probes, and 1-step automatic rollback.
#
# Usage:
#   sudo bash deploy-on-vm.sh <git_sha7> <env_file_path>
#
# Args:
#   $1  git_sha7        7-char hex SHA pinning the image pair (validated upstream by deploy.yml)
#   $2  env_file_path   Path to a temp dotenv file scp'd by deploy.yml. Sourced as the
#                       FIRST action; carries MSAI_ACR_NAME, MSAI_REGISTRY, MSAI_HOSTNAME,
#                       KV_NAME, RESOURCE_GROUP, etc. Avoids sudo env-strip / sudoers brittleness.
#
# Exit codes / failure markers (last line of stderr on failure):
#   0                       SUCCESS
#   FAIL_ENV                Env file unreadable or required var missing
#   FAIL_AZ_LOGIN           az login --identity failed (MI not propagated or VM has no MI)
#   FAIL_ACR_LOGIN          az acr login failed (RBAC propagating? AcrPull missing?)
#   FAIL_RENDER_ENV         msai-render-env.service failed (KV unreachable, RBAC, secret missing)
#   FAIL_CADDY_VALIDATE     Caddyfile syntax error — deploy aborts before pull
#   FAIL_PULL               docker compose pull failed (network, registry, image not found)
#   FAIL_MIGRATE            One-shot migrate service exited non-zero
#   FAIL_PROBE_HEALTH       backend /health did not return 200 within budget
#   FAIL_PROBE_READY        backend /ready did not return 200 within budget
#   FAIL_PROBE_TLS          https://${MSAI_HOSTNAME}/health did not return 200 within budget
#   FAIL_ROLLBACK_OK        Deploy failed but rollback to last-good SHA succeeded
#   FAIL_ROLLBACK_BROKEN    Deploy failed AND rollback also failed — manual intervention
#
# Council Plan-Review Iter 1 (NSG SSH gap, Contrarian P0): the deploy.yml workflow owns
# NSG transient-rule lifecycle — this script never touches network rules.

set -euo pipefail

readonly COMPOSE_FILE="/opt/msai/docker-compose.prod.yml"
readonly IMAGES_ENV="/run/msai-images.env"
readonly IMAGES_LAST_GOOD="/run/msai-images.last-good.env"
readonly RENDERED_ENV="/run/msai.env"
readonly DEFAULT_PROFILE_SERVICES=(
    postgres redis migrate
    backend backtest-worker research-worker portfolio-worker ingest-worker
    frontend caddy
)

GIT_SHA="${1:-}"
ENV_FILE="${2:-}"

# ─── Phase 1: Source env file ──────────────────────────────────────────────────

if [[ -z "$GIT_SHA" || -z "$ENV_FILE" ]]; then
    echo "Usage: $0 <git_sha7> <env_file_path>" >&2
    echo "FAIL_ENV" >&2
    exit 1
fi

if [[ ! -r "$ENV_FILE" ]]; then
    echo "Env file not readable: $ENV_FILE" >&2
    echo "FAIL_ENV" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# ─── Phase 2: Validate required env ────────────────────────────────────────────

require_env() {
    local var
    for var in "$@"; do
        if [[ -z "${!var:-}" ]]; then
            echo "Required env var missing: $var" >&2
            echo "FAIL_ENV" >&2
            exit 1
        fi
    done
}

require_env \
    MSAI_ACR_NAME MSAI_ACR_LOGIN_SERVER MSAI_REGISTRY \
    MSAI_BACKEND_IMAGE MSAI_FRONTEND_IMAGE MSAI_HOSTNAME \
    KV_NAME RESOURCE_GROUP DEPLOYMENT_NAME

if [[ ! "$GIT_SHA" =~ ^[0-9a-f]{7}$ ]]; then
    echo "git_sha must be 7 hex chars, got: $GIT_SHA" >&2
    echo "FAIL_ENV" >&2
    exit 1
fi

echo "=== deploy-on-vm.sh — sha=$GIT_SHA hostname=$MSAI_HOSTNAME rg=$RESOURCE_GROUP ==="

# ─── Phase 3: Azure / ACR auth via VM system-assigned MI ───────────────────────

if ! az login --identity --output none 2>/dev/null; then
    echo "az login --identity failed — VM may not have MI assigned, or IMDS unreachable" >&2
    echo "FAIL_AZ_LOGIN" >&2
    exit 1
fi

if ! az acr login --name "$MSAI_ACR_NAME" --output none 2>/dev/null; then
    echo "az acr login failed — verify VM MI has AcrPull on $MSAI_ACR_NAME (Slice 1 vmAcrPullAssignment)" >&2
    echo "FAIL_ACR_LOGIN" >&2
    exit 1
fi

# ─── Phase 4: Record last-good + write new images env ──────────────────────────

if [[ -f "$IMAGES_ENV" ]]; then
    cp -p "$IMAGES_ENV" "$IMAGES_LAST_GOOD"
else
    # First deploy on this VM — write a sentinel so rollback path knows there's
    # nothing to roll back to (graceful FAIL_ROLLBACK_BROKEN with clear message).
    cat >"$IMAGES_LAST_GOOD" <<EOF
# First deploy on this VM — no last-good image to roll back to.
MSAI_FIRST_DEPLOY=1
EOF
fi

umask 077
cat >"${IMAGES_ENV}.tmp" <<EOF
MSAI_GIT_SHA=$GIT_SHA
MSAI_REGISTRY=$MSAI_REGISTRY
MSAI_BACKEND_IMAGE=$MSAI_BACKEND_IMAGE
MSAI_FRONTEND_IMAGE=$MSAI_FRONTEND_IMAGE
MSAI_HOSTNAME=$MSAI_HOSTNAME
EOF
mv "${IMAGES_ENV}.tmp" "$IMAGES_ENV"

# ─── Phase 5: Refresh KV-rendered env (msai-render-env.service) ────────────────

# KV_NAME is a placeholder in the Slice 1 systemd unit; install a drop-in override
# so KV_NAME resolves at every render. Idempotent — overwrite each deploy.
mkdir -p /etc/systemd/system/msai-render-env.service.d
cat >/etc/systemd/system/msai-render-env.service.d/kv-name.conf <<EOF
[Service]
Environment="KV_NAME=$KV_NAME"
EOF

systemctl daemon-reload

# Combined enable + restart in a single systemctl call. enable (no --now) makes the
# unit boot-active; restart re-runs the oneshot to refresh secrets even if KV was
# rotated since last deploy. Using `enable --now` + separate `restart` would be a
# race on first deploy (enable --now starts; restart restarts immediately) with two
# identical failure messages — coalesce for clarity (code-review iter 1 P2).
if ! systemctl enable msai-render-env.service 2>&1; then
    echo "msai-render-env.service enable failed — see journalctl -u msai-render-env" >&2
    echo "FAIL_RENDER_ENV" >&2
    exit 1
fi

if ! systemctl restart msai-render-env.service; then
    echo "msai-render-env.service start/restart failed — see journalctl -u msai-render-env" >&2
    echo "FAIL_RENDER_ENV" >&2
    exit 1
fi

# Wait for /run/msai.env to be written (oneshot RemainAfterExit=yes).
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -s "$RENDERED_ENV" ]] && break
    sleep 1
done

if [[ ! -s "$RENDERED_ENV" ]]; then
    echo "$RENDERED_ENV missing or empty after render — see journalctl -u msai-render-env" >&2
    echo "FAIL_RENDER_ENV" >&2
    exit 1
fi

# ─── Phase 6: Compose flags + Caddyfile validate ───────────────────────────────

readonly COMPOSE_FLAGS=(
    --project-name msai
    -f "$COMPOSE_FILE"
    --env-file "$RENDERED_ENV"
    --env-file "$IMAGES_ENV"
)

# Pre-pull caddy so the validate step doesn't auto-pull silently (one-time cost
# on first deploy; no-op once cached). caddy:2-alpine is from Docker Hub, no ACR auth.
docker compose "${COMPOSE_FLAGS[@]}" pull caddy --quiet >/dev/null 2>&1 || true

if ! docker compose "${COMPOSE_FLAGS[@]}" run --rm caddy caddy validate --config /etc/caddy/Caddyfile; then
    echo "Caddyfile syntax error — fix and re-deploy. Existing Caddy keeps running." >&2
    echo "FAIL_CADDY_VALIDATE" >&2
    exit 1
fi

# ─── Phase 7: Pull + Up + Probes ───────────────────────────────────────────────

probe_failed=""
rollback_required=0

# rollback() restores last-good images and re-runs up. Called from trap on
# FAIL_PULL / FAIL_MIGRATE / FAIL_PROBE_*.
# shellcheck disable=SC2329
rollback() {
    local exit_code=$?

    if [[ "$rollback_required" -ne 1 ]]; then
        # Either we exited cleanly (success) or we failed BEFORE state changed
        # (FAIL_ENV / FAIL_AZ_LOGIN / FAIL_ACR_LOGIN / FAIL_RENDER_ENV / FAIL_CADDY_VALIDATE).
        # No rollback needed.
        exit "$exit_code"
    fi

    if grep -q '^MSAI_FIRST_DEPLOY=1' "$IMAGES_LAST_GOOD" 2>/dev/null; then
        echo "First-deploy failure — no last-good to roll back to." >&2
        echo "FAIL_ROLLBACK_BROKEN" >&2
        exit 1
    fi

    echo "→ Rolling back to last-good SHA"
    cp -p "$IMAGES_LAST_GOOD" "$IMAGES_ENV"

    if docker compose "${COMPOSE_FLAGS[@]}" pull "${DEFAULT_PROFILE_SERVICES[@]}" --quiet >/dev/null 2>&1 \
        && docker compose "${COMPOSE_FLAGS[@]}" up -d --wait --wait-timeout 300 "${DEFAULT_PROFILE_SERVICES[@]}"; then
        echo "Rollback succeeded — last-good SHA restored. Original failure: ${probe_failed:-FAIL_PULL_OR_MIGRATE}" >&2
        echo "FAIL_ROLLBACK_OK" >&2
        exit 1
    fi

    echo "Rollback ALSO failed — manual intervention required. Check 'docker compose ps' and journalctl." >&2
    echo "FAIL_ROLLBACK_BROKEN" >&2
    exit 1
}

trap rollback EXIT

echo "→ Pulling images for SHA $GIT_SHA"
if ! docker compose "${COMPOSE_FLAGS[@]}" pull "${DEFAULT_PROFILE_SERVICES[@]}"; then
    rollback_required=1
    probe_failed="FAIL_PULL"
    echo "FAIL_PULL" >&2
    exit 1
fi

echo "→ Bringing stack up (--wait --wait-timeout 300)"
if ! docker compose "${COMPOSE_FLAGS[@]}" up -d --wait --wait-timeout 300 "${DEFAULT_PROFILE_SERVICES[@]}"; then
    # Distinguish migrate failure from generic compose-up failure for triage.
    if docker compose "${COMPOSE_FLAGS[@]}" ps --format json migrate 2>/dev/null \
        | grep -q '"ExitCode":[^0]'; then
        rollback_required=1
        probe_failed="FAIL_MIGRATE"
        echo "FAIL_MIGRATE" >&2
        exit 1
    fi
    rollback_required=1
    probe_failed="FAIL_PROBE_HEALTH"
    echo "FAIL_PROBE_HEALTH (compose up --wait timeout)" >&2
    exit 1
fi

# Caddy bind-mounts ./Caddyfile read-only. When the host file changes via the
# `Stage compose file + Caddyfile + scripts on VM` step in deploy.yml, the
# in-container file changes too — but Caddy itself doesn't auto-reload. Compose
# also won't recreate the container because no compose-level config changed.
# Reload Caddy explicitly so route changes take effect on every deploy. Cheap
# (no downtime) and idempotent.
echo "→ Reloading Caddy config (in case Caddyfile changed)"
docker compose "${COMPOSE_FLAGS[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
    || echo "  (caddy reload failed or container not yet ready; will rely on next probe to surface)"

# Probe: backend /health (VM-loopback, bypasses Caddy).
echo "→ Probe: backend /health"
for i in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 5 http://127.0.0.1:8000/health; then
        echo "  /health 200 (attempt $i)"
        break
    fi
    if [[ "$i" -eq 30 ]]; then
        rollback_required=1
        probe_failed="FAIL_PROBE_HEALTH"
        echo "FAIL_PROBE_HEALTH" >&2
        exit 1
    fi
    sleep 2
done

echo "→ Probe: backend /ready"
for i in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 5 http://127.0.0.1:8000/ready; then
        echo "  /ready 200 (attempt $i)"
        break
    fi
    if [[ "$i" -eq 30 ]]; then
        rollback_required=1
        probe_failed="FAIL_PROBE_READY"
        echo "FAIL_PROBE_READY" >&2
        exit 1
    fi
    sleep 2
done

# Soft probe: msai:live:commands stream existence. Default-profile deploy doesn't
# start live-supervisor (broker profile), so the stream may not exist yet — log
# and continue, never hard-fail (PRD §6 D-1, research §9).
if docker compose "${COMPOSE_FLAGS[@]}" exec -T redis redis-cli EXISTS msai:live:commands 2>/dev/null \
    | grep -q '^1$'; then
    echo "  msai:live:commands stream present (broker stack was previously started)"
else
    echo "  WARN: msai:live:commands stream not yet created — OK for default-profile deploy"
fi

# Probe: TLS through Caddy (LE issuance can take ~30s on first deploy).
echo "→ Probe: https://${MSAI_HOSTNAME}/health (Caddy + LE end-to-end, up to 60×5s = 5min)"
for i in $(seq 1 60); do
    if curl -sf -o /dev/null --max-time 10 "https://${MSAI_HOSTNAME}/health"; then
        echo "  TLS probe 200 (attempt $i)"
        break
    fi
    if [[ "$i" -eq 60 ]]; then
        rollback_required=1
        probe_failed="FAIL_PROBE_TLS"
        echo "FAIL_PROBE_TLS — common causes: DNS A record drift, NSG 443 misconfig, LE rate limit" >&2
        exit 1
    fi
    sleep 5
done

# ─── Phase 8 (Slice 4): Install azcopy + enable backup-to-blob.timer ───────────

# Idempotent — both succeed silently if state is already correct. Non-fatal on
# failure: deploy is "done", nightly backup is "operational layer". A failure here
# alerts via FAIL_BACKUP_TIMER but does NOT roll back the deploy (rollback would
# regress app code over an ops-layer hiccup; backup-failure alert covers it).
echo "→ Slice 4: install azcopy if needed"
if [[ -x /opt/msai/scripts/install-azcopy.sh ]]; then
    /opt/msai/scripts/install-azcopy.sh \
        || echo "  WARN: install-azcopy.sh failed — Parquet mirror will fall back to azcopy-not-present error in the script" >&2
else
    echo "  WARN: /opt/msai/scripts/install-azcopy.sh missing — first deploy?" >&2
fi

echo "→ Slice 4: stage systemd units + drop-in override for RG + enable backup-to-blob.timer"
cp /opt/msai/scripts/backup-to-blob.service /etc/systemd/system/
cp /opt/msai/scripts/backup-to-blob.timer /etc/systemd/system/
# Drop-in override carries the actual RG this VM was provisioned into. Codex P2
# review on PR #58: hardcoded RESOURCE_GROUP=msaiv2_rg in the unit file would
# either break rehearsal-RG backups (can't read prod outputs) or, with broadened
# perms, write rehearsal backups into prod storage. Same drop-in pattern Slice 3
# used for KV_NAME on msai-render-env.service.
mkdir -p /etc/systemd/system/backup-to-blob.service.d
cat >/etc/systemd/system/backup-to-blob.service.d/env.conf <<EOF
[Service]
Environment="RESOURCE_GROUP=$RESOURCE_GROUP"
Environment="DEPLOYMENT_NAME=$DEPLOYMENT_NAME"
EOF
systemctl daemon-reload
if ! systemctl enable --now backup-to-blob.timer; then
    echo "FAIL_BACKUP_TIMER: systemctl enable --now backup-to-blob.timer failed" >&2
    echo "  Check: sudo journalctl -u backup-to-blob.timer + 'systemctl status backup-to-blob.timer'" >&2
    # PR #58 Codex round-5 P2: was previously non-fatal on the rationale that
    # 'backup-failure alert covers it'. WRONG — that alert only fires if the
    # SERVICE runs and fails. If the TIMER never enables, the service never
    # runs, and nightly backups are silently disabled. Make this fatal so the
    # deploy log surfaces the issue immediately rather than discovering days
    # later when no backup blob has appeared.
    exit 1
fi
# Code-review P2 fix: `enable --now` does NOT re-load the running timer's
# definition if the unit-file content changed (since restart). Explicit restart
# is idempotent and ensures future timer-content edits land without operator
# action. `enable` is still needed for the WantedBy linkage on first install.
systemctl restart backup-to-blob.timer 2>&1 \
    || echo "  WARN: backup-to-blob.timer restart non-zero (already-running OK; see journalctl)" >&2

# Confirm the timer is actually active+enabled — defense against the case where
# `enable --now` returned 0 but the timer immediately fell out of active state.
timer_state=$(systemctl is-active backup-to-blob.timer)
if [[ "$timer_state" != "active" ]]; then
    echo "FAIL_BACKUP_TIMER_INACTIVE: timer reports state='$timer_state' after enable+restart" >&2
    exit 1
fi
echo "  backup-to-blob.timer: $timer_state"

# Success — clear the rollback flag so trap exits cleanly.
rollback_required=0
trap - EXIT

echo "=== SUCCESS sha=$GIT_SHA hostname=$MSAI_HOSTNAME ==="
exit 0
