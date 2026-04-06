# CLAUDE.md - MSAI v2 (codex-version)

## Project Overview
MSAI v2 is a personal hedge-fund operating system.
It supports strategy discovery, backtesting, live deployment, market-data ingestion, IB account monitoring, and a web control panel.

## Tech Stack
- Backend: Python 3.12, FastAPI, SQLAlchemy (async), Alembic, arq, Redis, NautilusTrader (`nautilus_trader[ib]`), `ib_async`
- Frontend: Next.js 15, React 19, TypeScript, Tailwind 4, Recharts, MSAL (Azure Entra)
- Data: PostgreSQL, Redis, Parquet files under `data/parquet`
- Runtime: Docker Compose (dev/prod)
- CI: GitHub Actions (`ubuntu-24.04`, pinned setup actions)

## Repository Layout
```
codex-version/
├── backend/
│   ├── src/msai/
│   │   ├── api/                  # FastAPI routes
│   │   ├── core/                 # config, auth, db, queue, logging
│   │   ├── models/               # SQLAlchemy models
│   │   ├── schemas/              # Pydantic request/response models
│   │   ├── services/             # IB, Nautilus, ingestion, reporting, registry
│   │   └── workers/              # arq jobs and worker settings
│   ├── tests/                    # unit + integration tests
│   ├── pyproject.toml
│   └── uv.lock
├── frontend/
│   ├── src/app/                  # Next.js app routes
│   ├── src/components/           # UI components
│   └── src/lib/                  # auth/api helpers
├── strategies/                   # Strategy source files (must be importable package)
├── data/                         # Parquet + reports + ingestion status
├── docker-compose.dev.yml
├── docker-compose.prod.yml
└── .github/workflows/ci.yml
```

## Local Development

### Backend
```bash
cd backend
uv sync --extra dev
uv run uvicorn msai.main:app --host 0.0.0.0 --port 8000 --reload
```

### Worker
```bash
cd backend
uv run arq msai.workers.settings.WorkerSettings
```

### Frontend
```bash
cd frontend
pnpm install
pnpm dev
```

### Full Stack via Docker
```bash
cd /Users/pablomarin/Code/msai-v2/codex-version
docker compose -f docker-compose.dev.yml up --build
```

## Quality Gates (Required)
Run these before merging:

```bash
cd backend
uv run ruff check src tests
uv run mypy src/
uv run mypy --strict --follow-imports=skip \
  src/msai/services/nautilus/strategy_loader.py \
  src/msai/services/nautilus/backtest_runner.py \
  src/msai/services/nautilus/trading_node.py \
  src/msai/services/ib_account.py
uv run pytest tests -q

cd ../frontend
pnpm lint
pnpm build
```

## Critical Contracts And Gotchas

### Strategy Files
- Strategy files must live under `strategies/` and be importable as a Python package.
- Keep `strategies/__init__.py` and subpackage `__init__.py` files.
- `StrategyRegistry` and Nautilus loader depend on valid import paths.

### Live Start API Contract
`POST /api/v1/live/start` requires:
```json
{
  "strategy_id": "<db strategy id>",
  "config": {},
  "instruments": ["AAPL"],
  "paper_trading": true
}
```
Do not send `deployment_id` to this endpoint.

### Live Status / Positions Contracts
- `/api/v1/live/status` returns `strategy` as strategy name when resolvable.
- `/api/v1/live/positions` may omit `current_price`; UI computes fallback from `market_value / quantity`.

### Backtest Results Contract
- `/api/v1/backtests/{id}/results` trades use `executed_at` (not `timestamp`).
- Backtest job now tolerates multiple timestamp field names when persisting Nautilus trades.

### Nautilus / IB Runtime Notes
- Backtests run in subprocesses (`spawn`) due Nautilus engine process constraints.
- Live trading node also runs in a dedicated process.
- IB account access is shared via singleton `ib_account_service` to avoid client-id collision behavior.

### WebSocket Auth
- `/api/v1/live/stream` expects bearer token as first websocket text message within timeout.

## Auth And Environment

### Backend env vars (core)
- `DATABASE_URL`
- `REDIS_URL`
- `DATA_ROOT`
- `ENVIRONMENT`
- `JWT_TENANT_ID`
- `JWT_CLIENT_ID`
- `IB_GATEWAY_HOST`
- `IB_GATEWAY_PORT_PAPER`
- `IB_GATEWAY_PORT_LIVE`
- `IB_ACCOUNT_ID` (optional)

### Frontend env vars (core)
- `NEXT_PUBLIC_API_URL`
- `NEXT_PUBLIC_ENTRA_CLIENT_ID`
- `NEXT_PUBLIC_ENTRA_TENANT_ID`
- `NEXT_PUBLIC_ENTRA_REDIRECT_URI`
- `NEXT_PUBLIC_ENTRA_API_SCOPE` (recommended)

If `NEXT_PUBLIC_ENTRA_API_SCOPE` is not set, frontend defaults to:
`api://<NEXT_PUBLIC_ENTRA_CLIENT_ID>/access_as_user`.

## CI Notes
Pinned actions are in `.github/workflows/ci.yml`:
- `actions/checkout@v6.0.2`
- `astral-sh/setup-uv@v7.3.0` with `uv 0.10.6`
- `pnpm/action-setup@v4.2.0`
- `actions/setup-node@v6.2.0`

## Known Non-Blocking Warnings
- `testcontainers` emits deprecation warnings during integration tests.
- These warnings are from dependency internals; tests are still green.
