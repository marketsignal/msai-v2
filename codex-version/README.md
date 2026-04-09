# MSAI v2 (Codex Version)

MSAI v2 is an API-first research-to-live trading platform built around
NautilusTrader.

Today, the intended workflow is:

1. Ingest historical data from Databento.
2. Build Nautilus-compatible research catalogs.
3. Run backtests, parameter sweeps, and walk-forward validation.
4. Review results in the UI or through the API.
5. Promote a selected configuration into paper/live deployment.
6. Run the same strategy code in a Nautilus live node against
   Interactive Brokers.

The control plane is this repo's FastAPI backend, Next.js frontend, workers,
and file-backed research artifacts. The trading engine is NautilusTrader.

## Current Status

What is materially working today:

- Databento-first historical ingest for US equities and CME futures
- persisted Databento instrument definitions for research catalogs
- Nautilus backtests, parameter sweeps, and walk-forward jobs
- research reports, comparisons, and promotion drafts
- live deployment control through a dedicated live runtime service
- API-first access with `X-API-Key`
- browser UI for data, research, backtests, and live monitoring

What is still not fully certified:

- broker-connected Interactive Brokers paper E2E still depends on an active
  IB paper login
- the system is not yet hedge-fund production certified
- Azure hardening and long-burn operational validation are still separate
  phases

## Repository Layout

- [backend](/Users/pablomarin/Code/msai-v2/codex-version/backend): FastAPI,
  workers, Nautilus integration, CLI, models, migrations
- [frontend](/Users/pablomarin/Code/msai-v2/codex-version/frontend): Next.js
  operator UI
- [strategies](/Users/pablomarin/Code/msai-v2/codex-version/strategies):
  Nautilus strategy modules
- [data](/Users/pablomarin/Code/msai-v2/codex-version/data): local runtime
  data, catalogs, reports, scheduler state, alerts
- [docs/architecture](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture):
  architecture and flow documentation
- [docs/plans](/Users/pablomarin/Code/msai-v2/codex-version/docs/plans):
  rollout and roadmap documents
- [docs/runbooks](/Users/pablomarin/Code/msai-v2/codex-version/docs/runbooks):
  operations runbooks

## Local Quick Start

### 1. Export the keys you want to use

```bash
export MSAI_API_KEY=msai-dev-key
export DATABENTO_API_KEY=your_databento_key
```

Only set the IB variables if you are validating the live paper-trading lane:

```bash
export TWS_USERID=your_paper_username
export TWS_PASSWORD=your_paper_password
export IB_ACCOUNT_ID=DU1234567
```

### 2. Start the local services

```bash
cd /Users/pablomarin/Code/msai-v2/codex-version
docker compose -f docker-compose.dev.yml up -d postgres redis
cd /Users/pablomarin/Code/msai-v2/codex-version/backend
uv run alembic upgrade head
cd /Users/pablomarin/Code/msai-v2/codex-version
docker compose -f docker-compose.dev.yml up -d
```

This default dev startup is research-first. It does not start the broker-facing
services.

### 3. Start paper trading safely

Use the broker profile and an explicit env file so `ib-gateway` never falls
back to placeholder credentials:

```bash
cd /Users/pablomarin/Code/msai-v2/codex-version
docker compose \
  --profile broker \
  --env-file .env.paper-e2e.local \
  -f docker-compose.dev.yml \
  up -d ib-gateway live-runtime
```

If `TWS_USERID` or `TWS_PASSWORD` are missing, or still set to placeholder
values, `ib-gateway` now exits immediately with a clear error instead of trying
to log in with fake credentials.

Recommended paper-gateway settings:

- persistent gateway settings are stored under `data/ib-gateway`
- daily restart is handled with `IB_AUTO_RESTART_TIME`
- weekly reauthentication is still required by IBKR, so “never log out” is not
  a realistic target
- if the gateway session is interrupted during 2FA, `IB_RELOGIN_AFTER_TWOFA_TIMEOUT=yes`
  and `IB_TWOFA_TIMEOUT_ACTION=restart` allow IBC to retry the login sequence
- `IB_EXISTING_SESSION_ACTION=primary` keeps the gateway as the primary session
  if another IB session is detected

Useful broker env overrides for `.env.paper-e2e.local`:

```bash
IB_AUTO_RESTART_TIME=11:45 PM
IB_RELOGIN_AFTER_TWOFA_TIMEOUT=yes
IB_TWOFA_TIMEOUT_ACTION=restart
IB_EXISTING_SESSION_ACTION=primary
IB_TWS_ACCEPT_INCOMING=accept
IB_BYPASS_WARNING=yes
IB_TIME_ZONE=America/Chicago
IB_JAVA_HEAP_SIZE=1024
```

Reserved IB API client-id ranges in the local stack:

- `backend` account probe and broker-status checks use `IB_CLIENT_ID=10`
- `backend` instrument qualification uses `IB_INSTRUMENT_CLIENT_ID=20`
- `live-runtime` broker reconciliation and kill-all controls use `IB_CLIENT_ID=30`
- managed Nautilus live deployments allocate execution/data client ids from `101+`

This separation matters because IBKR only allows one active session per API
`clientId`. Reusing the same id across `backend`, `live-runtime`, and a running
live node can cause `client id is already in use` failures during start, stop,
or reconciliation.

Operating rule:

- never start the paper-trading lane with plain `docker compose up`
- always use `--profile broker --env-file .env.paper-e2e.local`
- if the broker env file is not present, the correct behavior is for
  `ib-gateway` to fail fast, not to guess or fall back
- inter-container services must use the forwarded gateway ports `4004` (paper)
  and `4003` (live), not the raw local API listener ports
- do not retry IB paper login repeatedly after a failed attempt; confirm the
  credentials manually first, then do one clean restart

### 4. Check health

```bash
curl http://127.0.0.1:8400/health
curl http://127.0.0.1:8400/ready
```

## Local Services And Ports

The development Compose stack in
[docker-compose.dev.yml](/Users/pablomarin/Code/msai-v2/codex-version/docker-compose.dev.yml)
starts these services:

- `frontend`: Next.js UI on `http://127.0.0.1:3400`
- `backend`: FastAPI control plane on `http://127.0.0.1:8400`
- `research-worker`: async research and ingest worker
- `live-runtime`: dedicated live trading runtime worker
- `daily-scheduler`: daily historical refresh scheduler
- `postgres`: PostgreSQL on `localhost:5434`
- `redis`: Redis on `localhost:6381`
- `ib-gateway`: Interactive Brokers Gateway container

## Authentication

The backend supports two auth modes:

- Browser JWT auth against Azure Entra ID
- `X-API-Key` auth for scripts, CLI-adjacent automation, tests, and local dev

The default development key from Compose is:

```text
msai-dev-key
```

This can be overridden with `MSAI_API_KEY`.

The implementation lives in
[auth.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/auth.py).

## Using The Platform

### Browser UI

The easiest local browser path is API-key mode:

```bash
cd /Users/pablomarin/Code/msai-v2/codex-version/frontend
NEXT_PUBLIC_AUTH_MODE=api-key \
NEXT_PUBLIC_E2E_API_KEY=msai-dev-key \
NEXT_PUBLIC_API_URL=http://127.0.0.1:8400 \
pnpm dev
```

Then open:

- `http://127.0.0.1:3000/dashboard`
- `http://127.0.0.1:3000/data`
- `http://127.0.0.1:3000/backtests`
- `http://127.0.0.1:3000/research`
- `http://127.0.0.1:3000/live`

The Compose frontend at `http://127.0.0.1:3400` is also available, but local
API-key mode is the simplest path when Entra auth is not configured.

### API

The API is the canonical surface. The browser and background workers are built
on top of it.

OpenAPI docs:

- `http://127.0.0.1:8400/docs`

Basic examples:

```bash
curl http://127.0.0.1:8400/health

curl -H 'X-API-Key: msai-dev-key' \
  http://127.0.0.1:8400/api/v1/strategies/

curl -H 'X-API-Key: msai-dev-key' \
  http://127.0.0.1:8400/api/v1/research/reports

curl -H 'X-API-Key: msai-dev-key' \
  http://127.0.0.1:8400/api/v1/live/status

curl -H 'X-API-Key: msai-dev-key' \
  http://127.0.0.1:8400/api/v1/market-data/daily-universe
```

Run a research sweep through the API:

```bash
curl -X POST \
  -H 'X-API-Key: msai-dev-key' \
  -H 'Content-Type: application/json' \
  -d '{
    "strategy_id": "<strategy-db-id>",
    "instruments": ["SPY.EQUS"],
    "start_date": "2026-03-31",
    "end_date": "2026-04-03",
    "base_config": {"lookback": 20, "zscore_threshold": 1.5},
    "parameter_grid": {
      "lookback": [10, 20, 30],
      "zscore_threshold": [1.0, 1.5, 2.0]
    },
    "objective": "sharpe",
    "max_parallelism": 2
  }' \
  http://127.0.0.1:8400/api/v1/research/sweeps
```

### CLI

The local CLI is installed from
[pyproject.toml](/Users/pablomarin/Code/msai-v2/codex-version/backend/pyproject.toml)
as `msai`.

The CLI is a local operator/developer convenience tool. It is not an HTTP
wrapper around FastAPI, so the browser and agent-facing contract should still
be treated as the API.

General help:

```bash
cd /Users/pablomarin/Code/msai-v2/codex-version/backend
uv run msai --help
```

Common commands:

```bash
uv run msai health
uv run msai strategy list
uv run msai data-status
```

Historical ingest:

```bash
uv run msai ingest equities SPY,QQQ 2026-04-01 2026-04-07 \
  --provider databento \
  --dataset ARCX.PILLAR \
  --schema ohlcv-1m

uv run msai ingest futures ES.v.0,NQ.v.0 2026-04-01 2026-04-07 \
  --provider databento \
  --dataset GLBX.MDP3 \
  --schema ohlcv-1m
```

Backtests and research:

```bash
uv run msai backtest run example.mean_reversion SPY.EQUS 2026-03-31 2026-04-03 \
  --config-json '{"lookback": 20, "zscore_threshold": 1.5}'

uv run msai backtest sweep example.mean_reversion SPY.EQUS 2026-03-31 2026-04-03 \
  '{"lookback":[10,20,30],"zscore_threshold":[1.0,1.5,2.0]}' \
  --objective sharpe

uv run msai backtest walk-forward example.mean_reversion SPY.EQUS 2026-01-01 2026-04-01 \
  '{"lookback":[10,20,30],"zscore_threshold":[1.0,1.5,2.0]}' \
  30 10 \
  --mode rolling \
  --objective sharpe
```

Live control:

```bash
uv run msai live status

uv run msai live start example.mean_reversion SPY \
  --paper \
  --config-json '{"lookback": 20, "zscore_threshold": 1.5}'
```

## How Research Works

The core research lifecycle is:

1. Ingest Databento historical data and Databento instrument definitions.
2. Write raw bars to `data/parquet`.
3. Build the Nautilus catalog under `data/nautilus`.
4. Run one-off backtests, parameter sweeps, or walk-forward jobs.
5. Save research reports under `data/research`.
6. Review the reports in the Research UI or through `/api/v1/research/...`.
7. Create a promotion draft for paper/live deployment.

Important behavior:

- the best sweep result is a candidate, not an automatic live deployment
- promotion is an explicit operator checkpoint
- the strategy code under
  [strategies](/Users/pablomarin/Code/msai-v2/codex-version/strategies)
  is shared between backtest and live execution

## How Live Works

The live runtime is intentionally separated from the API process.

1. The API receives `/api/v1/live/start`.
2. The API sends a request to the `live-runtime` queue.
3. The `live-runtime` worker starts or manages the Nautilus trading node.
4. Nautilus connects to Interactive Brokers for live market data and execution.
5. Nautilus publishes runtime snapshots and events through Redis.
6. The backend reads those snapshots and serves them to the UI and WebSocket
   clients.

Current provider split:

- historical research data: Databento
- live streaming and live execution: Interactive Brokers

## Data Locations

Under [data](/Users/pablomarin/Code/msai-v2/codex-version/data):

- `parquet/`: raw historical bars
- `databento/definitions/`: Databento `DEFINITION` DBN files
- `nautilus/`: Nautilus `ParquetDataCatalog`
- `reports/`: backtest report artifacts
- `research/`: sweep and walk-forward reports plus promotion drafts
- `scheduler/`: daily ingest configuration and scheduler state
- `alerts/`: persisted alert feed

## Architecture Docs

Start here:

- [Architecture Index](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/README.md)
- [System Topology](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/system-topology.md)
- [Module Map](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/module-map.md)
- [Data Flows](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/data-flows.md)
- [Platform Overview](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/platform-overview.md)
- [Research To Live Flow](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/research-to-live-flow.md)
- [Decision Log](/Users/pablomarin/Code/msai-v2/codex-version/docs/architecture/decision-log.md)

Related planning docs:

- [Research Roadmap](/Users/pablomarin/Code/msai-v2/codex-version/docs/plans/2026-04-07-research-roadmap.md)
- [Azure Rollout Plan](/Users/pablomarin/Code/msai-v2/codex-version/docs/plans/2026-04-07-azure-rollout-plan.md)

## External References

These docs shaped the current design:

- [NautilusTrader concepts](https://nautilustrader.io/docs/latest/concepts/)
- [NautilusTrader live trading](https://nautilustrader.io/docs/latest/concepts/live/)
- [NautilusTrader message bus](https://nautilustrader.io/docs/latest/concepts/message_bus/)
- [NautilusTrader Interactive Brokers integration](https://nautilustrader.io/docs/latest/integrations/ib/)
- [NautilusTrader Databento integration](https://nautilustrader.io/docs/latest/integrations/databento/)
- [Databento examples](https://databento.com/docs/examples)
