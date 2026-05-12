#!/usr/bin/env bash
# Deploy-time data-path smoke (Phase 12 of deploy-on-vm.sh).
#
# After /health, /ready, and /TLS probes pass, this script exercises the
# real data path end-to-end against the just-deployed image:
#
#   1. ``msai instruments bootstrap``  — proves the registry write path works
#   2. ``msai ingest stocks``           — proves Databento + Parquet writes
#   3. POST /api/v1/backtests/run       — proves the resolver + arq worker
#   4. Poll status until terminal       — proves the worker completes a run
#   5. GET /api/v1/backtests/{id}/results — proves results materialization
#
# Each phase emits a typed FAIL_SMOKE_* marker on failure so the deploy
# pipeline (deploy-on-vm.sh) can pinpoint which subsystem broke and the
# operator can re-run that exact step locally with the printed command.
#
# Side-effect contract:
#
# - The smoke backtest is tagged ``smoke=true`` in the ``backtests`` table.
#   The /api/v1/backtests/history endpoint filters it out by default.
# - On smoke failure, deploy-on-vm.sh's rollback path runs
#   ``DELETE FROM backtests WHERE smoke=true AND created_at >= $DEPLOY_START_TS``.
#   Pre-existing smoke rows (other deploys) are untouched.
# - Ingest writes to /app/data/parquet/stocks/<symbol>/ — atomic-per-month
#   writes mean re-running the same date range overwrites, no cumulative
#   growth across deploys.
# - Registry rows for the sentinel symbol persist across deploys; the
#   ``bootstrap`` step is idempotent (NOOP outcome on second run).
#
# Failure classification (per Hawk's BLOCKING #1):
#
# - Our 4xx/5xx (FAIL_SMOKE_*)   → deploy-on-vm.sh rolls back.
# - Databento 429/timeout/5xx    → exit 2 (WARN_SMOKE_UPSTREAM); deploy
#                                  proceeds, operator is paged via the
#                                  separate ``msai-data-path-broken`` alert
#                                  follow-up (Hawk's hourly heartbeat — not
#                                  in this PR's scope but referenced).
# - Live-deployments active      → exit 3 (SKIP_SMOKE_LIVE_ACTIVE); deploy
#                                  proceeds without smoke (shared
#                                  Parquet/DuckDB with a running broker
#                                  is unsafe — Hawk's BLOCKING #3).
#
# Usage (called from deploy-on-vm.sh, env vars pre-set):
#   bash scripts/deploy-smoke.sh

set -euo pipefail

# Source the rendered KV env so MSAI_API_KEY (and other runtime secrets)
# are available to ``curl -H X-API-Key:`` below. deploy-on-vm.sh's parent
# shell does NOT source /run/msai.env — it only passes it to ``docker
# compose --env-file``. Sourcing here keeps this script self-contained.
# ``set -a``/``set +a`` braces ensure each ``KEY=value`` line becomes an
# exported var.
RENDERED_ENV="${RENDERED_ENV:-/run/msai.env}"
if [[ -s "$RENDERED_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$RENDERED_ENV"
    set +a
fi

# Sentinel symbol: AAPL has the widest Databento entitlement coverage
# and is the canonical example in CLAUDE.md + the project Goal. Override
# via ``SMOKE_SYMBOL`` for projects that don't have AAPL entitled.
SMOKE_SYMBOL="${SMOKE_SYMBOL:-AAPL}"

# Window: 5 trading days ending 5 days ago, so we never depend on
# yesterday's data being published yet. Both ends are weekdays in any
# 7-day rolling window.
SMOKE_END=$(date -u -d '5 days ago' +%Y-%m-%d 2>/dev/null \
    || python3 -c 'import datetime; print((datetime.datetime.utcnow()-datetime.timedelta(days=5)).strftime("%Y-%m-%d"))')
SMOKE_START=$(date -u -d '12 days ago' +%Y-%m-%d 2>/dev/null \
    || python3 -c 'import datetime; print((datetime.datetime.utcnow()-datetime.timedelta(days=12)).strftime("%Y-%m-%d"))')

API_BASE="${API_BASE:-http://localhost:8000}"
API_KEY="${MSAI_API_KEY:-}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-msai}"
# Codex P1 catches (PR #61):
#
# 1. ``docker compose`` without ``-f`` resolves the compose file from
#    the CALLER's working directory. deploy-on-vm.sh invokes this
#    script as ``sudo bash /opt/msai/scripts/deploy-smoke.sh`` but the
#    shell's cwd is whatever the deploy-on-vm.sh parent set —
#    typically NOT ``/opt/msai/``. Pin the compose file explicitly.
#
# 2. The prod compose file has ``${MSAI_GIT_SHA}``,
#    ``${MSAI_REGISTRY}``, etc. interpolations that need to be
#    resolved at parse time. ``docker compose exec`` parses the file
#    even though it doesn't START containers — so without
#    ``--env-file`` the parser errors on unresolved vars. Match
#    deploy-on-vm.sh's ``COMPOSE_FLAGS`` shape: both ``/run/msai.env``
#    (KV-rendered secrets) and ``/run/msai-images.env`` (image SHAs).
COMPOSE_FILE="${COMPOSE_FILE:-/opt/msai/docker-compose.prod.yml}"
RENDERED_ENV_FILE="${RENDERED_ENV_FILE:-/run/msai.env}"
IMAGES_ENV_FILE="${IMAGES_ENV_FILE:-/run/msai-images.env}"
COMPOSE_FLAGS=(
    --project-name "$COMPOSE_PROJECT"
    -f "$COMPOSE_FILE"
    --env-file "$RENDERED_ENV_FILE"
    --env-file "$IMAGES_ENV_FILE"
)
DEPLOY_START_TS="${DEPLOY_START_TS:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
export DEPLOY_START_TS

echo "=== deploy-smoke.sh — symbol=$SMOKE_SYMBOL window=$SMOKE_START..$SMOKE_END ==="

# ─── Step 0 — Gate on active live deployments ──────────────────────────────────
# Hawk BLOCKING #3: do NOT run a backtest worker when a live deployment is
# active. Shared Parquet/DuckDB during real-money trading is operationally
# unsafe. Skip-with-WARN, don't fail-the-deploy.
if [[ -n "$API_KEY" ]]; then
    set +e
    LIVE_RESP=$(curl -sf --max-time 5 -H "X-API-Key: $API_KEY" \
        "$API_BASE/api/v1/live/status?active_only=true" 2>/dev/null)
    LIVE_CURL=$?
    set -e
    if [[ "$LIVE_CURL" -eq 0 ]]; then
        ACTIVE_IDS=$(echo "$LIVE_RESP" | python3 -c \
            'import json,sys; d=json.load(sys.stdin); print(",".join(x["id"] for x in d.get("deployments",[]) if x.get("status") in {"starting","building","ready","running"}))' \
            2>/dev/null || echo "")
        if [[ -n "$ACTIVE_IDS" ]]; then
            echo "SKIP_SMOKE_LIVE_ACTIVE: live deployments [$ACTIVE_IDS] running — refusing to smoke against shared Parquet/DuckDB during real-money trading. Deploy proceeds; smoke skipped." >&2
            exit 3
        fi
    fi
fi

# ─── Step 1 — Bootstrap the sentinel into the registry ─────────────────────────
echo "[1/5] Bootstrap: $SMOKE_SYMBOL"
if ! docker compose "${COMPOSE_FLAGS[@]}" exec -T backend \
        python -m msai.cli instruments bootstrap \
        --provider databento --symbols "$SMOKE_SYMBOL" >/tmp/smoke_bootstrap.log 2>&1; then
    if grep -qE "429|TooManyRequests|rate.limit|TimeoutError|ConnectionError|5[0-9][0-9]" /tmp/smoke_bootstrap.log; then
        echo "WARN_SMOKE_UPSTREAM: Databento upstream issue during bootstrap (see /tmp/smoke_bootstrap.log) — deploy proceeds; operator should investigate Databento status" >&2
        exit 2
    fi
    echo "FAIL_SMOKE_BOOTSTRAP: bootstrap of $SMOKE_SYMBOL failed. Reproduce locally with:" >&2
    echo "    docker compose --project-name msai exec backend python -m msai.cli instruments bootstrap --provider databento --symbols $SMOKE_SYMBOL" >&2
    cat /tmp/smoke_bootstrap.log >&2
    exit 1
fi

# ─── Step 2 — Ingest a small window of bars ────────────────────────────────────
echo "[2/5] Ingest: $SMOKE_SYMBOL ${SMOKE_START}..${SMOKE_END}"
if ! docker compose "${COMPOSE_FLAGS[@]}" exec -T backend \
        python -m msai.cli ingest stocks "$SMOKE_SYMBOL" "$SMOKE_START" "$SMOKE_END" \
        --provider databento >/tmp/smoke_ingest.log 2>&1; then
    if grep -qE "429|TooManyRequests|rate.limit|TimeoutError|ConnectionError|5[0-9][0-9]" /tmp/smoke_ingest.log; then
        echo "WARN_SMOKE_UPSTREAM: Databento upstream issue during ingest (see /tmp/smoke_ingest.log) — deploy proceeds" >&2
        exit 2
    fi
    echo "FAIL_SMOKE_INGEST: ingest of $SMOKE_SYMBOL failed. Reproduce locally with:" >&2
    echo "    docker compose --project-name msai exec backend python -m msai.cli ingest stocks $SMOKE_SYMBOL $SMOKE_START $SMOKE_END --provider databento" >&2
    cat /tmp/smoke_ingest.log >&2
    exit 1
fi

# ─── Step 3 — Resolve via the backtest API (path 2 — dotted alias) ─────────────
# Pick a registered strategy. The local API needs a strategy_id to
# accept a backtest run.
echo "[3/5] Resolve: find a registered strategy"
STRATEGY_ID=$(curl -sf --max-time 8 -H "X-API-Key: $API_KEY" \
    "$API_BASE/api/v1/strategies/" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["items"][0]["id"] if d.get("items") else "")' \
    2>/dev/null)
if [[ -z "$STRATEGY_ID" ]]; then
    echo "FAIL_SMOKE_RESOLVE: no strategies registered (empty registry after deploy). Reproduce locally with:" >&2
    echo "    curl -H \"X-API-Key: \$MSAI_API_KEY\" $API_BASE/api/v1/strategies/" >&2
    echo "Expected non-empty items[] after Item 3 (STRATEGIES_ROOT) lands." >&2
    exit 1
fi
echo "    strategy_id=$STRATEGY_ID"

# ─── Step 4 — Submit a smoke backtest ──────────────────────────────────────────
echo "[4/5] Submit smoke backtest (smoke=true)"
# Submit the BARE symbol (no venue suffix). Codex P2 catch (PR #61
# round 5): the previous ``${SMOKE_SYMBOL}.XNAS`` hardcoded the XNAS
# venue, which is correct for AAPL but wrong for any non-XNAS
# sentinel (e.g. ``SMOKE_SYMBOL=BRK.B`` lives on XNYS;
# ``SMOKE_SYMBOL=SPY`` on ARCX). The backtest resolver's path-3
# (bare-ticker) lookup finds the right active alias by raw_symbol
# without the caller having to know the listing venue. The
# canonical alias the resolver returns flows into
# ``Backtest.instruments`` + worker config so downstream
# subscription / catalog reads land on the right path. The smoke
# script then reads ``trade_count`` from the results endpoint,
# which doesn't need to know the instrument string.
SUBMIT_BODY=$(python3 -c "
import json
print(json.dumps({
    'strategy_id': '$STRATEGY_ID',
    'instruments': ['$SMOKE_SYMBOL'],
    'start_date': '$SMOKE_START',
    'end_date': '$SMOKE_END',
    'config': {
        # ``instrument_id`` and ``bar_type`` are left to the
        # ``_prepare_and_validate_backtest_config`` injection helper —
        # the resolver canonicalizes the bare ticker via path 3 and
        # the API helper injects ``{canonical_id}-1-MINUTE-LAST-EXTERNAL``
        # symmetrically into both the persisted row and the worker
        # config (this PR's other fix).
        'fast_ema_period': 10,
        'slow_ema_period': 30,
        'trade_size': '1',
    },
    'smoke': True,
}))
")
     # Codex P2 catch (PR #61): ``curl -f`` exits non-zero on any HTTP
# 4xx/5xx — combined with ``set -e`` the script aborts BEFORE the
# typed FAIL_SMOKE_RESOLVE marker is emitted. Disable errexit
# locally so we can capture the curl exit code, emit the marker, and
# exit deliberately.
set +e
SUBMIT_RESP=$(curl -sf --max-time 15 -X POST -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    "$API_BASE/api/v1/backtests/run" -d "$SUBMIT_BODY" 2>&1)
SUBMIT_RC=$?
set -e
if [[ "$SUBMIT_RC" -ne 0 ]]; then
    echo "FAIL_SMOKE_RESOLVE: POST /api/v1/backtests/run failed (curl=$SUBMIT_RC). Reproduce locally:" >&2
    echo "    curl -X POST -H \"X-API-Key: \$MSAI_API_KEY\" -H 'Content-Type: application/json' $API_BASE/api/v1/backtests/run -d '$SUBMIT_BODY'" >&2
    echo "Response: $SUBMIT_RESP" >&2
    exit 1
fi

BT_ID=$(echo "$SUBMIT_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "")
if [[ -z "$BT_ID" ]]; then
    echo "FAIL_SMOKE_RESOLVE: backtest submit did not return id. Response: $SUBMIT_RESP" >&2
    exit 1
fi
echo "    backtest_id=$BT_ID"

# ─── Step 5 — Poll until terminal + verify results ─────────────────────────────
echo "[5/5] Poll smoke backtest to completion"
TERMINAL_STATE=""
for i in $(seq 1 24); do
    sleep 5
    STATE=$(curl -sf --max-time 5 -H "X-API-Key: $API_KEY" \
        "$API_BASE/api/v1/backtests/$BT_ID/status" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
    echo "    T+$((i*5))s state=$STATE"
    if [[ "$STATE" == "completed" || "$STATE" == "failed" ]]; then
        TERMINAL_STATE="$STATE"
        break
    fi
done

if [[ "$TERMINAL_STATE" != "completed" ]]; then
    echo "FAIL_SMOKE_BACKTEST: backtest $BT_ID did not complete in 120s (final state=$TERMINAL_STATE). Reproduce locally:" >&2
    echo "    curl -H \"X-API-Key: \$MSAI_API_KEY\" $API_BASE/api/v1/backtests/$BT_ID/status" >&2
    exit 1
fi

RESULTS=$(curl -sf --max-time 8 -H "X-API-Key: $API_KEY" \
    "$API_BASE/api/v1/backtests/$BT_ID/results" 2>&1)
TRADE_COUNT=$(echo "$RESULTS" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("trade_count","NULL"))' 2>/dev/null || echo "NULL")
if [[ "$TRADE_COUNT" == "NULL" ]]; then
    echo "FAIL_SMOKE_BACKTEST: results payload missing trade_count. Response: $RESULTS" >&2
    exit 1
fi
# Codex P2 catch (PR #61): ``trade_count = 0`` IS the exact failure mode
# the 2026-05-12 prod incident produced — the resolver succeeded, the
# worker ran, but the Nautilus subprocess found zero bars because the
# catalog path was wrong. Treating zero trades as PASS would silently
# ship this regression. EMA-Cross on a known-liquid 5-7 day window of
# AAPL minute bars produces trades reliably; anything else is a real
# failure of the data path the smoke is supposed to prove.
if [[ "$TRADE_COUNT" == "0" ]]; then
    echo "FAIL_SMOKE_BACKTEST: backtest $BT_ID completed with trade_count=0 — the catalog/discovery layer dropped bars even though the API path succeeded. Reproduce locally:" >&2
    echo "    curl -H \"X-API-Key: \$MSAI_API_KEY\" $API_BASE/api/v1/backtests/$BT_ID/results" >&2
    echo "Check the worker logs + the Parquet catalog under /app/data/nautilus/data/bar/${SMOKE_SYMBOL}.* — there should be coverage for ${SMOKE_START}..${SMOKE_END}. The persisted Backtest.instruments column has the canonical ID; ``msai instruments bootstrap`` output also names it." >&2
    exit 1
fi

echo "=== Smoke PASS — backtest_id=$BT_ID trade_count=$TRADE_COUNT ==="
echo "$BT_ID" >/tmp/smoke_backtest_id
