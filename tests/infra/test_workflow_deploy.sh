#!/usr/bin/env bash
# CI test for .github/workflows/deploy.yml + reap-orphan-nsg-rules.yml + scripts/deploy-on-vm.sh.
#
# Runs:
#   1. actionlint on the workflows (if installed; otherwise warn + skip lint, still grep)
#   2. shellcheck on deploy-on-vm.sh + backup-to-blob.sh (if installed)
#   3. bash -n syntax check on the shell scripts
#   4. Grep assertions for Slice 3 must-haves (workflow_run gate, concurrency, ssh-agent,
#      transient rule create+delete, separate cleanup job, reaper cron)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

DEPLOY_YML=".github/workflows/deploy.yml"
REAPER_YML=".github/workflows/reap-orphan-nsg-rules.yml"
DEPLOY_SH="scripts/deploy-on-vm.sh"
BACKUP_SH="scripts/backup-to-blob.sh"

for f in "$DEPLOY_YML" "$REAPER_YML" "$DEPLOY_SH" "$BACKUP_SH"; do
    [[ -f "$f" ]] || { echo "FAIL: $f missing" >&2; exit 1; }
done

# ─── 1. actionlint ─────────────────────────────────────────────────────────────

if command -v actionlint &>/dev/null; then
    echo "=== actionlint ==="
    actionlint "$DEPLOY_YML" "$REAPER_YML"
    echo "actionlint clean."
else
    echo "WARN: actionlint not installed; skipping yaml lint"
fi

# ─── 2. shellcheck ─────────────────────────────────────────────────────────────

if command -v shellcheck &>/dev/null; then
    echo "=== shellcheck ==="
    shellcheck "$DEPLOY_SH" "$BACKUP_SH"
    echo "shellcheck clean."
else
    echo "WARN: shellcheck not installed; skipping shell-script lint"
fi

# ─── 3. bash -n syntax ─────────────────────────────────────────────────────────

bash -n "$DEPLOY_SH"
bash -n "$BACKUP_SH"
echo "bash -n syntax clean."

# ─── 4. Grep assertions ────────────────────────────────────────────────────────

echo "=== deploy.yml grep assertions ==="

grep -q "workflow_run:" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing workflow_run trigger" >&2; exit 1; }
grep -q 'workflows: \["Build and Push Images"\]' "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml workflow_run must depend on 'Build and Push Images'" >&2; exit 1; }
grep -qE "github\.event\.workflow_run\.conclusion == 'success'" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must explicitly gate on workflow_run.conclusion=='success'" >&2; exit 1; }
grep -q "id-token: write" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing id-token: write permission for OIDC" >&2; exit 1; }
grep -q "cancel-in-progress: false" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml concurrency must NOT cancel in-progress (Hawk + Contrarian)" >&2; exit 1; }
grep -q "webfactory/ssh-agent@" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must use webfactory/ssh-agent (research §1)" >&2; exit 1; }
grep -q 'azure/login@v2' "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing azure/login@v2 OIDC step" >&2; exit 1; }
grep -q 'az network nsg rule create' "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must open transient SSH rule (council deploy-ssh-jit.md)" >&2; exit 1; }
grep -qE 'gha-transient-\$\{\{ ?github\.run_id ?\}\}' "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml rule name must be gha-transient-\${{ github.run_id }}-* for greppability" >&2; exit 1; }
grep -qE '^  cleanup:' "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must have a separate cleanup job (Hawk + Contrarian)" >&2; exit 1; }
grep -q "needs: \[deploy\]" "$DEPLOY_YML" \
    || { echo "FAIL: cleanup job must depend on deploy job" >&2; exit 1; }
grep -q "if: always()" "$DEPLOY_YML" \
    || { echo "FAIL: cleanup job must run if: always() so cancellation triggers cleanup" >&2; exit 1; }

echo "=== reap-orphan-nsg-rules.yml grep assertions ==="

grep -qE "schedule:" "$REAPER_YML" \
    || { echo "FAIL: reaper missing schedule trigger" >&2; exit 1; }
grep -qE "starts_with\(name, 'gha-transient-'\)" "$REAPER_YML" \
    || { echo "FAIL: reaper must filter by gha-transient- prefix" >&2; exit 1; }

echo "=== deploy-on-vm.sh grep assertions ==="

# Failure markers must be present (the contract documented in the script header)
for marker in FAIL_ENV FAIL_AZ_LOGIN FAIL_ACR_LOGIN FAIL_RENDER_ENV FAIL_CADDY_VALIDATE \
              FAIL_PULL FAIL_MIGRATE FAIL_PROBE_HEALTH FAIL_PROBE_READY FAIL_PROBE_TLS \
              FAIL_ROLLBACK_OK FAIL_ROLLBACK_BROKEN; do
    grep -q "$marker" "$DEPLOY_SH" \
        || { echo "FAIL: deploy-on-vm.sh missing failure marker $marker" >&2; exit 1; }
done

# az login --identity must precede az acr login (uses VM MI per research §6)
grep -q "az login --identity" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must use 'az login --identity' (research §6)" >&2; exit 1; }
grep -q "az acr login --name" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must use 'az acr login --name' (NOT --expose-token)" >&2; exit 1; }

# Caddyfile validate: official caddy image has no ENTRYPOINT, only CMD ['caddy', 'run', ...].
# So `docker compose run --rm caddy validate` replaces CMD with just 'validate' and PID 1
# can't find an executable — must be 'run --rm caddy caddy validate ...' (1st 'caddy' is
# service name, 2nd is binary). Verified empirically 2026-05-10.
grep -qE "run --rm caddy caddy validate" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must invoke 'run --rm caddy caddy validate' (image has no ENTRYPOINT — second 'caddy' is the binary)" >&2; exit 1; }

# Compose project name pinned (predictable container names for redis-cli probe)
grep -q "project-name msai" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must set --project-name msai (predictable container names)" >&2; exit 1; }

echo "=== Slice 4 deploy.yml grep assertions ==="

# Active-live_deployments gate must exist + reference active_count (NOT .deployments[].status — that path doesn't include `ready` and active_count is the simpler field per LiveStatusResponse).
grep -q "Refuse if active live_deployments" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing 'Refuse if active live_deployments' step (Slice 4 T05)" >&2; exit 1; }
grep -q "FAIL_ACTIVE_DEPLOYMENTS_REFUSAL" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing FAIL_ACTIVE_DEPLOYMENTS_REFUSAL marker" >&2; exit 1; }
grep -q "FAIL_CANNOT_DETERMINE_LIVE_STATE" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml missing FAIL_CANNOT_DETERMINE_LIVE_STATE marker (fail-closed when backend is unreachable)" >&2; exit 1; }
grep -qE '\.deployments\[\] \| select\(\.status' "$DEPLOY_YML" \
    || { echo "FAIL: gate must parse .deployments[].status (NOT .active_count — Codex bot PR-review caught that active_count is backend-local _node_manager state and does NOT track supervisor-owned subprocesses; gate would fail open during real broker trading)" >&2; exit 1; }
# Round-8 P1 guard: curl 6/7 bypass MUST be gated on inputs.bootstrap, not
# always-allowed. Caddy down does NOT mean broker (separate compose profile) is
# also down — bypassing without explicit bootstrap=true could allow a deploy
# while broker subprocesses are still trading.
grep -q "inputs.bootstrap" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must accept 'bootstrap' workflow_dispatch input (PR #58 round-8 P1)" >&2; exit 1; }
grep -qF "6|7)" "$DEPLOY_YML" \
    || { echo "FAIL: curl-exit case statement missing 6|7 branch" >&2; exit 1; }

# Regression guard: gate must NOT jq-parse .active_count (see PR #58 Codex review).
# Strip YAML comments first so the explanatory `# active_count is …` block doesn't trip
# the check; only inspect actual jq commands.
if sed 's/#.*//' "$DEPLOY_YML" | grep -qE "jq -r '.active_count"; then
    echo "FAIL: regression — gate must NOT jq-parse .active_count (see PR #58 Codex review — that field is backend-local; gate would fail open during broker trading)" >&2
    exit 1
fi

# Regression guard: NO force-bypass flag (plan-review iter-2 P1 removed it — run_id-bound token was impractical).
if grep -qE "force_during_active_deploys|confirmation_token|FAIL_FORCE_TOKEN" "$DEPLOY_YML"; then
    echo "FAIL: deploy.yml reintroduced force-bypass flag — plan-review iter-2 P1 deliberately removed (impractical token scheme)" >&2
    exit 1
fi

# Slice 4: also scp install-azcopy.sh + backup-to-blob.{service,timer}
grep -q "install-azcopy.sh" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must scp install-azcopy.sh (Slice 4 T04)" >&2; exit 1; }
grep -q "backup-to-blob.timer" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must scp backup-to-blob.timer (Slice 4 T04)" >&2; exit 1; }

echo "=== Slice 4 deploy-on-vm.sh grep assertions ==="
grep -q "FAIL_BACKUP_TIMER" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh missing FAIL_BACKUP_TIMER marker (Slice 4 T04)" >&2; exit 1; }
grep -q "systemctl enable --now backup-to-blob.timer" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must enable + start backup-to-blob.timer" >&2; exit 1; }
grep -q "/opt/msai/scripts/install-azcopy.sh" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must invoke install-azcopy.sh" >&2; exit 1; }

echo "=== Fresh-VM data-path closure (2026-05-12) grep assertions ==="
# Phase 2.6 — heal the existing msai_app_data named volume on the prod VM.
# Docker's "named volume inherits image-path ownership at first mount" rule
# is strictly first-mount; an already-initialized root:root volume stays
# root:root forever unless explicitly healed. Sentinel-gated for idempotency.
grep -q "FAIL_VOLUME_HEAL" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh missing FAIL_VOLUME_HEAL marker (fresh-VM data-path closure)" >&2; exit 1; }
grep -qE "msai-data-volume-chowned|SENTINEL_PATH=\".msai-data-volume" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must use a sentinel file inside the volume for idempotency" >&2; exit 1; }
grep -qE "chown -R 999:999 /d" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh volume-heal must chown to 999:999 (msai uid in the prod image)" >&2; exit 1; }

# Dockerfile must pre-create /app/data so the named volume inherits msai:msai
# ownership at first mount (fresh-VM path).
DOCKERFILE="backend/Dockerfile"
grep -qE "mkdir -p /app/data /app/reports" "$DOCKERFILE" \
    || { echo "FAIL: backend/Dockerfile must pre-create /app/data AND /app/reports before USER msai (fresh-VM data-path closure)" >&2; exit 1; }

# docker-compose.prod.yml must set STRATEGIES_ROOT on every service that
# reads the strategy registry (backend + 4 workers + live-supervisor = 6).
PROD_COMPOSE="docker-compose.prod.yml"
STRAT_ROOT_COUNT=$(grep -c "STRATEGIES_ROOT: /app/strategies" "$PROD_COMPOSE")
if [[ "$STRAT_ROOT_COUNT" -lt 6 ]]; then
    echo "FAIL: docker-compose.prod.yml has $STRAT_ROOT_COUNT STRATEGIES_ROOT lines; needs 6 (backend + 4 workers + live-supervisor)" >&2
    exit 1
fi

echo "=== Phase 12 (data-path smoke) grep assertions ==="
SMOKE_SH="scripts/deploy-smoke.sh"
[[ -f "$SMOKE_SH" ]] \
    || { echo "FAIL: scripts/deploy-smoke.sh missing" >&2; exit 1; }
bash -n "$SMOKE_SH" \
    || { echo "FAIL: deploy-smoke.sh has bash syntax errors" >&2; exit 1; }

# Per-step failure markers — each must point at the exact CLI to reproduce.
for marker in FAIL_SMOKE_BOOTSTRAP FAIL_SMOKE_INGEST FAIL_SMOKE_RESOLVE FAIL_SMOKE_BACKTEST; do
    grep -q "$marker" "$SMOKE_SH" \
        || { echo "FAIL: deploy-smoke.sh missing $marker marker (per-step diagnostic — Maintainer + Contrarian BLOCKING)" >&2; exit 1; }
done

# Fail-open classification for upstream outages (Hawk BLOCKING #1).
grep -q "WARN_SMOKE_UPSTREAM" "$SMOKE_SH" \
    || { echo "FAIL: deploy-smoke.sh missing WARN_SMOKE_UPSTREAM (Databento 429/5xx must fail-open, not rollback)" >&2; exit 1; }

# Live-deployment conflict skip (Hawk BLOCKING #3).
grep -q "SKIP_SMOKE_LIVE_ACTIVE" "$SMOKE_SH" \
    || { echo "FAIL: deploy-smoke.sh missing SKIP_SMOKE_LIVE_ACTIVE (refuse to run backtest worker against shared Parquet/DuckDB during live trading)" >&2; exit 1; }

# Phase 12 invocation in deploy-on-vm.sh.
grep -q "Phase 12: data-path smoke" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh missing Phase 12 invocation" >&2; exit 1; }
grep -q "/opt/msai/scripts/deploy-smoke.sh" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must invoke deploy-smoke.sh from /opt/msai/scripts/" >&2; exit 1; }

# Rollback path must delete smoke=true backtest rows created during THIS deploy.
grep -q "smoke = true AND created_at >=" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh rollback must delete smoke=true backtests created since DEPLOY_START_TS (Hawk + Contrarian BLOCKING: idempotency + history pollution)" >&2; exit 1; }
grep -q "DEPLOY_START_TS=" "$DEPLOY_SH" \
    || { echo "FAIL: deploy-on-vm.sh must set DEPLOY_START_TS early so rollback cleanup window is well-defined" >&2; exit 1; }

# deploy.yml must scp deploy-smoke.sh + chmod +x.
grep -q "scripts/deploy-smoke.sh" "$DEPLOY_YML" \
    || { echo "FAIL: deploy.yml must scp deploy-smoke.sh" >&2; exit 1; }

echo "All Slice 3 + Slice 4 + fresh-VM-data-path-closure deploy-pipeline tests passed."
