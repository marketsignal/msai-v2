# MSAI v2

Personal hedge fund platform for automated trading via Interactive
Brokers. Strategies are Python files, backtested against historical
minute-level data, deployed to paper or live trading, and monitored
through a web dashboard.

Built by Claude Opus 4.6. See `docs/architecture/` for the full
technical documentation.

## Current Status

Phase 1 is complete. The system can ingest market data, run backtests,
deploy strategies to paper trading via IB Gateway, stream live events
to the dashboard, and emergency halt all trading with a four-layer
kill switch.

**814 test functions** in `backend/tests/`.

### Known Stubs

- `/api/v1/live/trades` returns order-intent data (submitted price, not fill price)
- `/api/v1/account/summary` and `/portfolio` return zero-valued data when IB Gateway is offline
- Dashboard equity curve renders from mock data
- `StrategyStatus` component renders from mock deployments
- `AlertService` requires SMTP configuration to send emails

## Tech Stack

| Layer    | Technology                                                                                                     |
| -------- | -------------------------------------------------------------------------------------------------------------- |
| Backend  | Python 3.12 + FastAPI + NautilusTrader + arq (Redis job queue)                                                 |
| Frontend | Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Charts                                             |
| Database | PostgreSQL 16 (app state) + Parquet files (market data) + DuckDB (queries) + Redis 7 (queue + pub/sub + cache) |
| Auth     | Azure Entra ID (MSAL frontend, PyJWT backend) + API key for dev                                                |
| Deploy   | Docker Compose (7 services default, 9 with `broker` profile)                                                   |
| Data     | Polygon.io (stocks/options) + Databento (futures)                                                              |

## Quick Start

### Development (No Live Trading)

```bash
# Start 5 services: postgres, redis, backend, backtest-worker, frontend
docker compose -f docker-compose.dev.yml up -d

# Frontend: http://localhost:3300
# Backend:  http://localhost:8800
# Health:   curl http://localhost:8800/health
```

### Development (With Paper Trading)

```bash
# 1. Copy .env.example to .env and fill in IB credentials
cp .env.example .env
# Edit .env: TWS_USERID, TWS_PASSWORD, IB_ACCOUNT_ID (DU...)

# 2. Start all 9 services including ib-gateway + live-supervisor
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml up -d

# 3. Wait for ib-gateway to become healthy (60-180s)
docker compose -f docker-compose.dev.yml ps
```

### Stop

```bash
docker compose -f docker-compose.dev.yml down
```

## Ports

| Service    | Host Port      | Internal Port |
| ---------- | -------------- | ------------- |
| Frontend   | 3300           | 3000          |
| Backend    | 8800           | 8000          |
| PostgreSQL | 5433           | 5432          |
| Redis      | 6380           | 6379          |
| IB Gateway | 127.0.0.1:4002 | 4002          |
| VNC (IB)   | 127.0.0.1:5900 | 5900          |

## Repository Layout

```
.
  backend/
    src/msai/
      api/              # FastAPI routers
      core/             # Config, auth, database, logging, queue
      models/           # SQLAlchemy 2.0 models
      schemas/          # Pydantic request/response schemas
      services/         # Business logic
        nautilus/       # NautilusTrader integration
          projection/   # Event projection pipeline
          risk/         # RiskAwareStrategy mixin
        live/           # Deployment identity, failure kinds, idempotency
        data_sources/   # Polygon, Databento clients
        observability/  # Prometheus metrics
      live_supervisor/  # Process manager, heartbeat, watchdog
      workers/          # arq background jobs + cron
      main.py           # FastAPI entrypoint
      cli.py            # Typer CLI
    tests/              # 814 test functions
    alembic/            # Database migrations
  frontend/
    src/
      app/              # Next.js 15 pages
      components/       # React components + shadcn/ui
      lib/              # Auth, API client, utilities
  strategies/           # Trading strategy Python files
  scripts/              # deploy-azure.sh, verify-paper-soak.sh, etc.
  docs/
    architecture/       # Architecture documentation (7 files)
    runbooks/           # VM setup, disaster recovery, IB troubleshooting
  docker-compose.dev.yml   # Dev (hot reload, 5+2 services)
  docker-compose.prod.yml  # Prod (resource limits, 7 services)
  .env.example             # Environment variable template
  .github/workflows/ci.yml # GitHub Actions CI
```

## Test Commands

```bash
# Run all backend tests (814 test functions)
cd backend && uv run pytest tests/ -v

# Lint
cd backend && uv run ruff check src/

# Type check
cd backend && uv run mypy src/ --strict

# Frontend build check
cd frontend && pnpm build

# Frontend lint
cd frontend && pnpm lint
```

## Auth

Two authentication modes:

**Azure Entra ID (Production):** Set `AZURE_TENANT_ID` and
`AZURE_CLIENT_ID` in `.env`. The frontend acquires tokens via MSAL;
the backend validates JWTs via PyJWT against the Entra OIDC JWKS
endpoint.

**API Key (Development):** Set `MSAI_API_KEY` in `.env`. Pass via
`Authorization: Bearer <key>` header or `X-API-Key: <key>` header.
The backend creates a synthetic `api-key-user` in the users table
on startup.

All endpoints except `/health`, `/ready`, and `/metrics` require
authentication.

## API Endpoints

### Unauthenticated

| Method | Path     | Description                         |
| ------ | -------- | ----------------------------------- |
| GET    | /health  | Liveness probe                      |
| GET    | /ready   | Readiness probe (checks PostgreSQL) |
| GET    | /metrics | Prometheus metrics (text format)    |

### Auth (`/api/v1/auth`)

| Method | Path                | Description                                        |
| ------ | ------------------- | -------------------------------------------------- |
| GET    | /api/v1/auth/me     | Current user profile (auto-creates on first login) |
| POST   | /api/v1/auth/logout | Placeholder (MSAL logout is frontend-driven)       |

### Strategies (`/api/v1/strategies`)

| Method | Path                             | Description                        |
| ------ | -------------------------------- | ---------------------------------- |
| GET    | /api/v1/strategies/              | List strategies (syncs disk to DB) |
| GET    | /api/v1/strategies/{id}          | Get strategy detail                |
| PATCH  | /api/v1/strategies/{id}          | Update config/description          |
| POST   | /api/v1/strategies/{id}/validate | Validate strategy file loads       |
| DELETE | /api/v1/strategies/{id}          | Unregister strategy                |

### Backtests (`/api/v1/backtests`)

| Method | Path                           | Description                     |
| ------ | ------------------------------ | ------------------------------- |
| POST   | /api/v1/backtests/run          | Start backtest (201, arq job)   |
| GET    | /api/v1/backtests/history      | List past backtests (paginated) |
| GET    | /api/v1/backtests/{id}/status  | Poll job status                 |
| GET    | /api/v1/backtests/{id}/results | Metrics + trade log             |
| GET    | /api/v1/backtests/{id}/report  | Download QuantStats HTML        |

### Live Trading (`/api/v1/live`)

| Method | Path                     | Description                                           |
| ------ | ------------------------ | ----------------------------------------------------- |
| POST   | /api/v1/live/start       | Deploy strategy (risk-validated, 3-layer idempotency) |
| POST   | /api/v1/live/stop        | Stop deployment                                       |
| POST   | /api/v1/live/kill-all    | Emergency halt all (four-layer kill switch)           |
| POST   | /api/v1/live/resume      | Clear halt flag                                       |
| GET    | /api/v1/live/status      | All deployments                                       |
| GET    | /api/v1/live/status/{id} | Single deployment detail                              |
| GET    | /api/v1/live/positions   | Open positions (from ProjectionState)                 |
| GET    | /api/v1/live/trades      | Recent executions (from order_attempt_audits)         |
| WS     | /api/v1/live/stream/{id} | Real-time events (JWT first-message auth)             |

### Market Data (`/api/v1/market-data`)

| Method | Path                              | Description                          |
| ------ | --------------------------------- | ------------------------------------ |
| GET    | /api/v1/market-data/bars/{symbol} | OHLCV bars (DuckDB on Parquet)       |
| GET    | /api/v1/market-data/symbols       | Available symbols by asset class     |
| GET    | /api/v1/market-data/status        | Storage stats                        |
| POST   | /api/v1/market-data/ingest        | Trigger data download (202, arq job) |

### Account (`/api/v1/account`)

| Method | Path                      | Description                            |
| ------ | ------------------------- | -------------------------------------- |
| GET    | /api/v1/account/summary   | IB account metrics (zero when offline) |
| GET    | /api/v1/account/portfolio | IB positions (empty when offline)      |
| GET    | /api/v1/account/health    | IB Gateway connection status           |

## Paper Soak

The `scripts/verify-paper-soak.sh` script automates the full paper soak
verification:

1. Brings up the full Compose stack with the `broker` profile
2. Waits for all services to become healthy (including ib-gateway)
3. Seeds the smoke strategy row
4. Runs the Phase 1 E2E harness: start deployment, verify running,
   verify audit row, crash + recovery, stop, verify clean shutdown
5. Captures logs on failure

```bash
# Prerequisites: .env with IB credentials, Docker, uv
./scripts/verify-paper-soak.sh
```

## CLI Commands

```bash
# Data ingestion
cd backend && uv run msai ingest stocks AAPL,MSFT 2024-01-01 2025-01-01
cd backend && uv run msai ingest-daily stocks all
cd backend && uv run msai data-status

# Live trading control
cd backend && uv run msai live-start <strategy-uuid> AAPL,MSFT --paper
cd backend && uv run msai live-stop <deployment-uuid>
cd backend && uv run msai live-status
cd backend && uv run msai live-kill-all
```

## Safety Defaults

- Paper trading ONLY in dev compose (`TRADING_MODE: paper` hard-coded)
- IB port 4002 (paper) by default; 4001 (live) requires explicit env override
- Port/account consistency validation rejects paper port + live account (and vice versa)
- Kill switch halt flag has 24h TTL (safety net against forgotten halts)
- No auto-resume after kill switch or IB disconnect
- `READ_ONLY_API=no` by default; set to `yes` for safe data-only dev runs
- All IB Gateway ports bound to 127.0.0.1 only (not exposed to LAN)
- `manage_stop=True` on every strategy (auto-flatten on stop)
- Write-through cache and message bus (no buffered state loss on crash)
- Startup reconciliation against IB with 24h lookback

## Architecture Documentation

See `docs/architecture/` for the full documentation:

1. [Platform Overview](docs/architecture/platform-overview.md)
2. [System Topology](docs/architecture/system-topology.md)
3. [Module Map](docs/architecture/module-map.md)
4. [Data Flows](docs/architecture/data-flows.md)
5. [Live Trading Subsystem](docs/architecture/live-trading-subsystem.md)
6. [Nautilus Integration](docs/architecture/nautilus-integration.md)
7. [Decision Log](docs/architecture/decision-log.md)
