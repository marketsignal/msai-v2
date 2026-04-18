# CLAUDE.md - MSAI v2 (MarketSignal AI)

## Project Overview

### What Is This?

MSAI v2 is a personal hedge fund platform for automated trading via Interactive Brokers. It enables defining trading strategies as Python files, backtesting them against historical minute-level data, deploying them to live/paper trading, and monitoring portfolio performance through a web dashboard. It replaces MSAI v1 (Jupyter notebook-driven, mixed Python/C# stack on AKS).

### Tech Stack

- **Backend:** Python 3.12 + FastAPI + async/await + NautilusTrader + arq (Redis job queue)
- **Frontend:** Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Lightweight Charts + Recharts
- **Database:** PostgreSQL 16 (app state) + Parquet files (market data) + DuckDB (dashboard queries) + Redis 7 (job queue + WebSocket pub/sub)
- **Auth:** Azure Entra ID (MSAL on frontend, PyJWT validation on backend)
- **Deploy:** Docker Compose on Azure VM (6 containers: backend, frontend, postgres, redis, backtest-worker, ib-gateway). Dev ports: Frontend 3300, Backend 8800, PostgreSQL 5433, Redis 6380.
- **Data Sources:** Polygon.io (stocks/options), Databento (futures), IB Gateway (execution only)

### File Structure

```
claude-version/
├── backend/
│   ├── src/msai/
│   │   ├── api/                # FastAPI routers (auth, strategies, backtests, live, market-data, account, websocket)
│   │   ├── core/               # Foundation: config, auth, database, logging, queue, secrets, audit, data_integrity
│   │   ├── models/             # SQLAlchemy 2.0 models (user, strategy, backtest, trade, live_deployment, audit_log, strategy_daily_pnl)
│   │   ├── schemas/            # Pydantic request/response schemas
│   │   ├── services/           # Business logic (parquet_store, market_data_query, risk_engine, alerting, ib_account, ib_probe, data_ingestion, strategy_registry)
│   │   │   ├── nautilus/       # NautilusTrader integration (catalog, backtest_runner, trading_node)
│   │   │   └── data_sources/   # External data clients (polygon_client, databento_client)
│   │   ├── workers/            # arq background workers (backtest_job, settings)
│   │   ├── main.py             # FastAPI app entrypoint
│   │   └── cli.py              # Typer CLI (ingest, backtest, live commands)
│   ├── tests/
│   │   ├── unit/               # 153 unit tests
│   │   └── integration/        # Integration tests (requires PostgreSQL)
│   ├── strategies/example/     # Example EMA Cross strategy
│   ├── alembic/                # Database migrations
│   ├── Dockerfile              # Production multi-stage build
│   ├── Dockerfile.dev          # Dev with hot reload
│   └── pyproject.toml          # Dependencies + tooling config
├── frontend/
│   ├── src/
│   │   ├── app/                # Next.js pages (dashboard, strategies, backtests, live-trading, market-data, data-management, settings, login)
│   │   ├── components/         # React components (layout, charts, ui via shadcn)
│   │   └── lib/                # Utilities (auth, api, msal-config, format, mock-data)
│   ├── Dockerfile              # Production multi-stage build
│   └── Dockerfile.dev          # Dev with hot reload
├── scripts/                    # deploy-azure.sh, backup-to-blob.sh
├── docs/runbooks/              # vm-setup.md, disaster-recovery.md, ib-gateway-troubleshooting.md
├── docker-compose.dev.yml      # Dev environment (5 services + hot reload)
├── docker-compose.prod.yml     # Production (6 services + IB Gateway + resource limits)
└── .github/workflows/ci.yml    # GitHub Actions CI (lint, type check, tests)
```

### Design Direction

- Premium, dark-mode-first aesthetic (Linear.app / Vercel.com style)
- Font: Geist (system default from Next.js)
- Color: shadcn/ui dark theme with CSS custom properties
- No generic "AI slop" — clean, minimal, professional trading dashboard

### Deployment

- Single Azure VM (D4s_v5: 4 vCPU, 16GB RAM) with Docker Compose
- Phase 2: Split into 2 VMs (trading VM-A + compute VM-B) for real money
- Azure Blob Storage for Parquet data backup (nightly cron)
- Azure Key Vault for production secrets

### Key Commands

```bash
# Backend development
cd backend && uv run pytest tests/ -v              # Run all 153 tests
cd backend && uv run ruff check src/               # Lint
cd backend && uv run mypy src/ --strict            # Type check
cd backend && uv run uvicorn msai.main:app --reload # Dev server on :8000 (Docker maps to :8800)

# Frontend development
cd frontend && pnpm dev                             # Dev server on :3000 (Docker maps to :3300)
cd frontend && pnpm build                           # Production build
cd frontend && pnpm lint                            # ESLint

# Docker (development with hot reload)
docker compose -f docker-compose.dev.yml up -d      # Start all services
docker compose -f docker-compose.dev.yml logs -f    # Follow logs
docker compose -f docker-compose.dev.yml down       # Stop services

# Docker (production)
docker compose -f docker-compose.prod.yml up -d     # Start production stack

# CLI tools
cd backend && uv run msai ingest --asset stocks --symbols AAPL,MSFT --start 2024-01-01 --end 2025-01-01
cd backend && uv run msai ingest-daily --asset stocks --symbols all
cd backend && uv run msai data-status
cd backend && uv run msai live-status
cd backend && uv run msai live-kill-all
cd backend && uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers
cd backend && uv run msai instruments refresh --symbols ES.Z.5 --provider databento

# Database migrations
cd backend && uv run alembic upgrade head           # Apply migrations
cd backend && uv run alembic revision --autogenerate -m "description"  # New migration

# Worker stale-import refresh (run after merges touching src/msai/services|workers|live_supervisor)
./scripts/restart-workers.sh                       # Restart workers without rebuilding images
./scripts/restart-workers.sh --with-broker         # Also restart live-supervisor + ib-gateway
```

### API Endpoints

```
/health                              # Liveness probe (unauthenticated)
/ready                               # Readiness probe (unauthenticated)
/api/v1/auth/me                      # GET  Current user from JWT
/api/v1/auth/logout                  # POST Placeholder logout
/api/v1/strategies/                  # GET  List registered strategies
/api/v1/strategies/{id}              # GET  Strategy detail
/api/v1/strategies/{id}              # PATCH Update config
/api/v1/strategies/{id}/validate     # POST  Validate strategy loads
/api/v1/strategies/{id}              # DELETE Unregister
/api/v1/backtests/run                # POST  Start backtest (arq job)
/api/v1/backtests/{id}/status        # GET  Poll job status
/api/v1/backtests/{id}/results       # GET  Metrics + trade log
/api/v1/backtests/{id}/report        # GET  Download QuantStats HTML
/api/v1/backtests/history            # GET  List past backtests
/api/v1/live/start                   # POST  Deploy strategy (risk-validated)
/api/v1/live/stop                    # POST  Stop deployment
/api/v1/live/kill-all                # POST  Emergency halt all
/api/v1/live/status                  # GET  All deployments
/api/v1/live/positions               # GET  Open positions
/api/v1/live/trades                  # GET  Recent executions
/api/v1/live/stream                  # WS   Real-time updates (JWT first-message auth)
/api/v1/market-data/bars/{symbol}    # GET  OHLCV bars from Parquet via DuckDB
/api/v1/market-data/symbols          # GET  Available symbols
/api/v1/market-data/status           # GET  Storage stats
/api/v1/market-data/ingest           # POST  Trigger data download (arq job)
/api/v1/account/summary              # GET  IB account data
/api/v1/account/portfolio            # GET  IB positions
/api/v1/account/health               # GET  IB Gateway status
```

All endpoints except /health and /ready require Azure Entra ID JWT authentication.

---

## Architecture Notes

### Data Flow

- **Historical data**: Polygon/Databento → Python ingestion → atomic Parquet writes → `{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`
- **Backtesting**: FastAPI → arq queue → backtest worker → NautilusTrader BacktestRunner → QuantStats report → results in PostgreSQL
- **Live trading**: FastAPI → risk engine validation → TradingNodeManager → NautilusTrader TradingNode subprocess → IB Gateway
- **Dashboard queries**: Frontend → FastAPI → DuckDB (in-memory, reads Parquet) → JSON response

### Key Design Decisions

- `DATA_ROOT` env var controls all Parquet/report paths (Docker: `/app/data`, local: `./data`)
- Strategies are Python files in `strategies/` dir (no uploads in Phase 1 — git-only)
- `strategy_code_hash` (SHA256) stored on every backtest/deployment for reproducibility
- arq (not multiprocessing) for job queue — handles retry, timeout, dead-letter
- PyJWT for backend JWT validation (NOT MSAL — MSAL is frontend only)
- Risk engine validates before every live deployment start
- WebSocket auth: first message must be JWT token within 5 seconds

### Instrument Registry (2026-04-17)

New tables `instrument_definitions` + `instrument_aliases` hold control-plane metadata for instrument resolution. UUID-keyed, with effective-date windowing on aliases for futures rolls. Schema + `SecurityMaster.resolve_for_backtest` extensions + `msai instruments refresh` CLI ship in this PR.

**Deferred to follow-up PRs (not yet scheduled):**

- Live-path wiring — `/api/v1/live/start` + supervisor still use closed-universe `canonical_instrument_id()`. Deferred to a follow-up PR — see the skeleton at the end of `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"Split-off PR Skeleton".
- `instrument_cache` table coexists with the new registry and is not migrated yet. Deferred to a follow-up PR — see the skeleton at the end of `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"Split-off PR Skeleton".
- Strategy config-schema extraction for UI form generation. Deferred to a follow-up PR — see the skeleton at the end of `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"Split-off PR Skeleton".

The `msai instruments refresh --provider interactive_brokers` path is currently deferred — follow-up PR will add the required `Settings` fields (`ib_request_timeout_seconds`, `ib_instrument_client_id`, etc.) plus the full IBQualifier factory.

### Environment Variables

```
DATABASE_URL=postgresql+asyncpg://msai:password@postgres:5432/msai
REDIS_URL=redis://redis:6379
DATA_ROOT=/app/data
ENVIRONMENT=development|production
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
CORS_ORIGINS=["http://localhost:3000"]
POLYGON_API_KEY=your-key
DATABENTO_API_KEY=your-key
```

---

## Detailed Rules

All coding standards, workflow rules, and policies are in the parent `.claude/rules/`.
These files are auto-loaded by Claude Code with the same priority as this file.

**What's in `.claude/rules/`:**

- `principles.md` — Top-level principles and design philosophy
- `workflow.md` — Decision matrix for choosing the right command
- `critical-rules.md` — Non-negotiable rules (branch safety, TDD, etc.)
- `security.md`, `testing.md`, `api-design.md` — Coding standards
- `python-style.md`, `typescript-style.md`, `database.md`, `frontend-design.md` — Language-specific
