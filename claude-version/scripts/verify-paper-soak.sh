#!/usr/bin/env bash
#
# Automated paper-soak verification — zero manual steps.
#
# What this does:
#
#   1. Brings up the full Compose stack with the `live` profile
#      activated (postgres, redis, backend, backtest-worker,
#      live-supervisor, ib-gateway, frontend)
#   2. ``docker compose up --wait`` blocks until EVERY service is
#      healthy, including ib-gateway (which takes 60-180s to boot
#      IBC + log in + open port 4002)
#   3. Seeds the smoke strategy row so the Phase 1 E2E harness has
#      something to deploy
#   4. Runs the Phase 1 E2E harness end-to-end (tests/e2e/test_live_trading_phase1.py)
#      which drives: POST /api/v1/live/start → verify
#      status=running → verify one audit row with client_order_id →
#      backend crash + recovery → POST /api/v1/live/stop → verify
#      status=stopped + zero open positions
#   5. Captures ib-gateway, live-supervisor, and backend logs to
#      ./logs/paper-soak-*.log if any step fails
#   6. Leaves the stack running on success so the operator can poke
#      at it; tears down on Ctrl-C
#
# Prerequisites:
#
#   - Docker + Docker Compose
#   - .env file at the project root with TWS_USERID, TWS_PASSWORD,
#     IB_ACCOUNT_ID filled in with real paper-account credentials
#     (see .env.example)
#   - uv installed (for the Python snippets that seed the DB)
#
# Usage:
#
#   # from claude-version/ directory
#   ./scripts/verify-paper-soak.sh
#
# Exit codes:
#
#   0  — all checks passed; stack still running
#   1  — env file missing or missing required variable
#   2  — compose up --wait failed (ib-gateway never became healthy)
#   3  — smoke strategy seed failed
#   4  — Phase 1 E2E harness failed

set -euo pipefail

cd "$(dirname "$0")/.."

# Which compose file to drive. Defaults to dev; the release
# sign-off checklist's "Production compose stack validation" step
# sets ``COMPOSE_FILE=docker-compose.prod.yml`` to exercise the
# prod wiring. Bound to a local var once so the rest of the script
# can reference it without re-expanding the default everywhere.
readonly _COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"

# ---------------------------------------------------------------------------
# 1. Pre-flight: verify .env has the required credentials
# ---------------------------------------------------------------------------

if [[ ! -f .env ]]; then
  echo "ERROR: .env file not found at $(pwd)/.env" >&2
  echo "       Copy .env.example to .env and fill in TWS_USERID," >&2
  echo "       TWS_PASSWORD, and IB_ACCOUNT_ID with paper-account values." >&2
  exit 1
fi

# Codex P1 iter1 fix: DO NOT ``source .env``. A Compose .env file
# is a KEY=VALUE format that Compose parses verbatim — it is NOT a
# shell script. Passwords with ``$`` characters, ``#`` mid-value,
# or any ``$(...)`` substring get mangled or executed when sourced
# by bash. Compose itself reads .env natively for variable
# interpolation; this script only needs to READ specific values
# for validation, which we do with a literal grep.
_get_env_value() {
  # Returns the RHS of the first line matching ``^<key>=``,
  # stripping surrounding double or single quotes. Lines starting
  # with ``#`` are comments and skipped by the anchor. Empty match
  # prints empty string — caller decides if that's OK.
  local key=$1
  local raw
  raw=$(grep -E "^${key}=" .env | head -n1 | cut -d'=' -f2-) || true
  # Strip one pair of outer quotes if present
  raw=${raw#\"}
  raw=${raw%\"}
  raw=${raw#\'}
  raw=${raw%\'}
  printf '%s' "${raw}"
}

TWS_USERID=$(_get_env_value TWS_USERID)
TWS_PASSWORD=$(_get_env_value TWS_PASSWORD)
IB_ACCOUNT_ID=$(_get_env_value IB_ACCOUNT_ID)

missing=()
[[ -z "${TWS_USERID}" || "${TWS_USERID}" == "your_ib_username" ]] && missing+=("TWS_USERID")
[[ -z "${TWS_PASSWORD}" || "${TWS_PASSWORD}" == "your_ib_password" ]] && missing+=("TWS_PASSWORD")
[[ -z "${IB_ACCOUNT_ID}" || "${IB_ACCOUNT_ID}" == "DU0000000" ]] && missing+=("IB_ACCOUNT_ID")

if ((${#missing[@]} > 0)); then
  echo "ERROR: .env is missing real values for: ${missing[*]}" >&2
  echo "       These must be real IB paper credentials for the smoke" >&2
  echo "       test to reach IB Gateway. Placeholder values are rejected." >&2
  exit 1
fi

# Trading-mode / account-id consistency — matches what
# ``_validate_port_account_consistency`` in ``live_node_config.py``
# enforces inside the subprocess. Paper mode (default) requires a
# ``DU*`` account + port 4002; live mode requires a non-paper
# account (not starting with ``DU``) + port 4001. This preflight
# rejects the combinations that would crash the subprocess with
# ``RECONCILIATION_FAILED`` seconds after startup.
#
# Codex iter2 P2: the checklist's prod-stack-validation step needs
# to run this script with ``TRADING_MODE=live`` and a real live
# account id (``U*``). Before that fix, the preflight hard-rejected
# any non-DU account, making the documented live-mode validation
# impossible.
#
# Codex iter3 P2: read ``TRADING_MODE`` from .env the same way the
# other vars are read. Before this fix, the script read
# ``TRADING_MODE`` from the shell via ``${TRADING_MODE:-paper}`` and
# everything else from .env — so an operator who set
# ``TRADING_MODE=live`` in .env without exporting it to the shell
# got a split-brain state where the script assumed paper but
# ``docker compose`` started live.
_trading_mode_from_env=$(_get_env_value TRADING_MODE)
_trading_mode="${TRADING_MODE:-${_trading_mode_from_env:-paper}}"
if [[ "${_trading_mode}" == "paper" ]]; then
  if [[ ! "${IB_ACCOUNT_ID}" =~ ^DU ]]; then
    echo "ERROR: TRADING_MODE=paper requires IB_ACCOUNT_ID starting with 'DU'." >&2
    echo "       Got IB_ACCOUNT_ID='${IB_ACCOUNT_ID}'. Either use a paper" >&2
    echo "       account or set TRADING_MODE=live IB_PORT=4001." >&2
    exit 1
  fi
elif [[ "${_trading_mode}" == "live" ]]; then
  if [[ "${IB_ACCOUNT_ID}" =~ ^DU ]]; then
    echo "ERROR: TRADING_MODE=live requires a live IB_ACCOUNT_ID (not 'DU*')." >&2
    echo "       Got IB_ACCOUNT_ID='${IB_ACCOUNT_ID}'. Either flip to paper" >&2
    echo "       (unset TRADING_MODE + IB_PORT) or use a real live account." >&2
    exit 1
  fi
  echo "[paper-soak] WARNING: running in LIVE trading mode — orders will" >&2
  echo "[paper-soak]          hit real money. This should only be invoked" >&2
  echo "[paper-soak]          from the release sign-off checklist's" >&2
  echo "[paper-soak]          'Production compose stack validation' step." >&2
else
  echo "ERROR: TRADING_MODE='${_trading_mode}' — must be 'paper' or 'live'." >&2
  exit 1
fi

# Force live profile activation so `docker compose up` starts
# ib-gateway and live-supervisor. Exported so compose picks it up.
export COMPOSE_PROFILES=live

mkdir -p logs

echo "[paper-soak] Pre-flight OK — IB_ACCOUNT_ID=${IB_ACCOUNT_ID}, TRADING_MODE=${_trading_mode}, compose=${_COMPOSE_FILE}" >&2

# ---------------------------------------------------------------------------
# 2. Bring up the full stack and wait for health
# ---------------------------------------------------------------------------

echo "[paper-soak] docker compose up -d --wait (this takes 2-4 minutes)" >&2
# --wait blocks until every service with a healthcheck reports
# healthy, or the timeout expires. --wait-timeout is generous
# because IBC login is slow on first boot. start_period on the
# gateway healthcheck suppresses "unhealthy" during the window,
# so --wait doesn't trip prematurely.
if ! docker compose -f "${_COMPOSE_FILE}" up -d --wait --wait-timeout 360; then
  echo "[paper-soak] ERROR: docker compose up --wait failed" >&2
  echo "[paper-soak] Capturing ib-gateway + live-supervisor logs..." >&2
  docker compose -f "${_COMPOSE_FILE}" logs ib-gateway > logs/paper-soak-ibgateway.log 2>&1 || true
  docker compose -f "${_COMPOSE_FILE}" logs live-supervisor > logs/paper-soak-supervisor.log 2>&1 || true
  docker compose -f "${_COMPOSE_FILE}" logs backend > logs/paper-soak-backend.log 2>&1 || true
  echo "[paper-soak] Logs saved to ./logs/paper-soak-*.log" >&2
  exit 2
fi

echo "[paper-soak] Stack healthy — all services reported healthy by compose" >&2

# ---------------------------------------------------------------------------
# 2b. Derive backend URL + container name from the running compose stack
# ---------------------------------------------------------------------------
# Codex iter2 P2: the script previously hardcoded dev compose defaults
# (``localhost:8800`` + ``msai-claude-backend``). Prod compose uses
# port 8000 and has no fixed container_name, so overriding
# COMPOSE_FILE=docker-compose.prod.yml silently broke both the E2E
# harness's HTTP calls and its ``docker kill`` crash-simulation step.
#
# Derive both values dynamically from ``docker compose port`` and
# ``docker compose ps`` so the same script works against any compose
# file — including operators' custom variants.
_backend_port_info=$(
  docker compose -f "${_COMPOSE_FILE}" port backend 8000 2>/dev/null || true
)
if [[ -z "${_backend_port_info}" ]]; then
  echo "[paper-soak] ERROR: could not resolve backend host port via" >&2
  echo "[paper-soak]        ``docker compose port backend 8000``. The" >&2
  echo "[paper-soak]        backend service may not expose port 8000 in" >&2
  echo "[paper-soak]        '${_COMPOSE_FILE}' — check the ports: section." >&2
  exit 2
fi
# Output format is "host:port" (e.g. "0.0.0.0:8800" or "[::]:8000").
# Extract the port portion from the last colon, accepting IPv6
# addresses that have internal colons.
_backend_host_port="${_backend_port_info##*:}"
_BACKEND_URL="http://localhost:${_backend_host_port}"

_BACKEND_CONTAINER=$(
  docker compose -f "${_COMPOSE_FILE}" ps --format '{{.Name}}' backend 2>/dev/null | head -n1
)
if [[ -z "${_BACKEND_CONTAINER}" ]]; then
  echo "[paper-soak] ERROR: could not resolve backend container name via" >&2
  echo "[paper-soak]        ``docker compose ps``. Is the backend service" >&2
  echo "[paper-soak]        actually running under '${_COMPOSE_FILE}'?" >&2
  exit 2
fi

echo "[paper-soak] Resolved backend URL=${_BACKEND_URL} container=${_BACKEND_CONTAINER}" >&2

# ---------------------------------------------------------------------------
# 3. Seed the smoke strategy row
# ---------------------------------------------------------------------------
# Codex P1 iter1 fix: run the seed INSIDE the backend container via
# ``docker compose exec`` so it uses the container's DATABASE_URL
# (``postgres:5432`` via Docker DNS). Running host-side used
# ``settings.database_url`` which defaults to ``localhost:5432`` and
# missed the published Postgres port (5433 in dev compose), breaking
# the seed on any fresh operator setup.

echo "[paper-soak] Seeding smoke strategy..." >&2
SEED_PY=$(cat <<'PY'
import asyncio, uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from msai.core.config import settings
from msai.models.strategy import Strategy
from msai.models.user import User

async def main() -> None:
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    SMOKE_FILE = '/app/strategies/example/smoke_market_order.py'
    SMOKE_CLASS = 'SmokeMarketOrderStrategy'
    async with Session() as session:
        user = (await session.execute(select(User).where(User.entra_id == 'e2e-operator'))).scalar_one_or_none()
        if user is None:
            user = User(id=uuid.uuid4(), entra_id='e2e-operator', email='e2e@example.com', role='operator')
            session.add(user)
            await session.flush()
        # Codex iter5 P2: match by file_path + strategy_class, not
        # name. ``strategies.name`` isn't unique, so a DB with two
        # rows named ``smoke_market_order`` (leftover from a prior
        # run with a different file path) would either raise
        # MultipleResultsFound or — worse — reuse the wrong row and
        # point the harness at the wrong strategy. file_path +
        # class_name uniquely identifies the code we want to run.
        existing = (await session.execute(
            select(Strategy)
            .where(Strategy.file_path == SMOKE_FILE)
            .where(Strategy.strategy_class == SMOKE_CLASS)
            .order_by(Strategy.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            print(str(existing.id))
            return
        strat = Strategy(
            id=uuid.uuid4(),
            name='smoke_market_order',
            file_path=SMOKE_FILE,
            strategy_class=SMOKE_CLASS,
            default_config={},
            created_by=user.id,
        )
        session.add(strat)
        await session.commit()
        print(str(strat.id))
    await engine.dispose()

asyncio.run(main())
PY
)

if ! STRATEGY_ID=$(
  # Codex iter2 P2: use ``python`` directly, NOT ``uv run python``.
  # The prod Dockerfile's runtime stage does not copy the uv binary,
  # so ``uv run python`` fails in prod compose before the E2E harness
  # starts. Plain ``python`` is always on PATH in both dev and prod
  # images; the project's deps are already installed in the venv
  # that ``python`` resolves to.
  docker compose -f "${_COMPOSE_FILE}" exec -T backend python -c "${SEED_PY}" 2>&1
); then
  echo "[paper-soak] ERROR: smoke strategy seed failed: ${STRATEGY_ID}" >&2
  exit 3
fi

STRATEGY_ID=$(echo "${STRATEGY_ID}" | tail -n 1 | tr -d '[:space:]')
echo "[paper-soak] Smoke strategy seeded: ${STRATEGY_ID}" >&2

# ---------------------------------------------------------------------------
# 4. Run the Phase 1 E2E harness against the live stack
# ---------------------------------------------------------------------------

echo "[paper-soak] Running Phase 1 E2E harness against live IB Gateway..." >&2

# Export the env vars the harness expects. Backend URL + container
# name come from the dynamic ``docker compose port|ps`` probes above
# (Codex iter2 P2 fix) so the same script works for dev AND prod
# compose files. Operator overrides via ``MSAI_E2E_*`` still win.
export MSAI_E2E_IB_ENABLED=1
export MSAI_E2E_STRATEGY_ID="${STRATEGY_ID}"
export MSAI_E2E_BACKEND_URL="${MSAI_E2E_BACKEND_URL:-${_BACKEND_URL}}"
export MSAI_E2E_BACKEND_CONTAINER="${MSAI_E2E_BACKEND_CONTAINER:-${_BACKEND_CONTAINER}}"
export MSAI_E2E_COMPOSE_FILE="${MSAI_E2E_COMPOSE_FILE:-${_COMPOSE_FILE}}"
export MSAI_E2E_IB_ACCOUNT_ID="${IB_ACCOUNT_ID}"
# Codex iter5 P1: tell the E2E harness whether to request a paper
# or live deployment so the POST body matches the supervisor's
# expected trading mode. Without this, the harness hard-codes
# ``paper_trading: true`` and my new paper/live safety guard in
# the payload factory rejects every live-mode run.
if [[ "${_trading_mode}" == "paper" ]]; then
  export MSAI_E2E_PAPER_TRADING=true
else
  export MSAI_E2E_PAPER_TRADING=false
fi

if ! (cd backend && uv run pytest tests/e2e/test_live_trading_phase1.py -vv); then
  echo "[paper-soak] ERROR: Phase 1 E2E harness failed" >&2
  echo "[paper-soak] Capturing logs..." >&2
  docker compose -f "${_COMPOSE_FILE}" logs ib-gateway > logs/paper-soak-ibgateway.log 2>&1 || true
  docker compose -f "${_COMPOSE_FILE}" logs live-supervisor > logs/paper-soak-supervisor.log 2>&1 || true
  docker compose -f "${_COMPOSE_FILE}" logs backend > logs/paper-soak-backend.log 2>&1 || true
  echo "[paper-soak] Logs saved to ./logs/paper-soak-*.log" >&2
  exit 4
fi

echo "" >&2
echo "[paper-soak] ✓ ALL CHECKS PASSED" >&2
echo "" >&2
echo "Stack is still running. Inspect via:" >&2
echo "  docker compose -f "${_COMPOSE_FILE}" ps" >&2
echo "  docker compose -f "${_COMPOSE_FILE}" logs -f ib-gateway" >&2
echo "  curl http://localhost:8800/api/v1/live/status" >&2
echo "" >&2
echo "To tear down: docker compose -f "${_COMPOSE_FILE}" down" >&2
