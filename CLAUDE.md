<!-- forge:migrated 2026-04-28 -->

# CLAUDE.md — MSAI v2 (MarketSignal AI)

## Project Overview

### Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

### What Is This?

MSAI v2 is a personal hedge fund platform for automated trading via Interactive Brokers. It enables defining trading strategies as Python files, backtesting them against historical minute-level data, deploying them to live/paper trading, and monitoring portfolio performance through a web dashboard. MSAI v2 is an API-first, CLI-second, UI-third product.

### History

This project was originally built in parallel by two AI implementations from the same PRD, and compared side-by-side through 2026-02 to 2026-04. The comparison concluded 2026-04-19 (council verdict in [`docs/decisions/which-version-to-keep.md`](docs/decisions/which-version-to-keep.md)); the losing implementation was archived at tag `codex-final` and removed. The surviving implementation was then flattened from its subdirectory to the repo root. **This IS the shipping implementation; there is no "version" suffix anywhere.** A brief attempt to port the archived Playwright specs was abandoned when plan review found the UI drift too large — see the decision-doc postscript.

### Stack

- **Backend:** Python 3.12 + FastAPI + NautilusTrader + arq (Redis job queue)
- **Frontend:** Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Charts + Recharts
- **Database:** PostgreSQL 16 + Parquet files + DuckDB + Redis 7
- **Auth:** Azure Entra ID (MSAL frontend, PyJWT backend)
- **Deploy:** Docker Compose on Azure VM (dev: single-host; prod: single-VM D4s_v5, Phase 2 splits to 2-VM for real money)
- **Data Sources:** Polygon.io (stocks/options), Databento (futures), IB Gateway (execution only)

### Ports (dev)

| Service            | Host port | Container port |
| ------------------ | --------- | -------------- |
| Frontend (Next.js) | `3300`    | `3000`         |
| Backend (FastAPI)  | `8800`    | `8000`         |
| PostgreSQL         | `5433`    | `5432`         |
| Redis              | `6380`    | `6379`         |

### Running the stack

```bash
docker compose -f docker-compose.dev.yml up -d

# Health checks
curl http://localhost:8800/health
open http://localhost:3300

# Logs + stop
docker compose -f docker-compose.dev.yml logs -f
docker compose -f docker-compose.dev.yml down
```

IB Gateway is behind the `broker` Compose profile:

```bash
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d
```

### Deploying to production

**Push to main auto-deploys to the Azure VM** via a two-workflow chain:

1. `.github/workflows/build-and-push.yml` (Slice 2) — OIDC → ACR → docker build & push tagged `<sha7>`.
2. `.github/workflows/deploy.yml` (Slice 3 + 4) — `workflow_run` on Slice 2 success → active-deployments gate → OIDC + transient NSG SSH rule → `scp` + `ssh sudo bash deploy-on-vm.sh` → `docker compose pull && up -d --wait` → public probes → rollback on probe failure.

Manual rollback / re-deploy / rehearsal-RG dispatch use `gh workflow run deploy.yml -f git_sha=<sha>` (and full override matrix for rehearsal).

**Read [`docs/how_to_deploy.md`](docs/how_to_deploy.md) first** for the deploy architecture diagram, the active-`live_deployments` safety gate, rehearsal procedure, repo-Variable matrix, and the pointer index to deep-dive runbooks (`vm-setup`, `slice-3-first-deploy`, `disaster-recovery`, `restore-from-backup`, `iac-parity-reapply`).

### File Structure

```
msai-v2/
├── backend/                 # FastAPI + Python (~32.5K LOC, 536 tests)
│   ├── src/msai/
│   │   ├── api/             # FastAPI routers (auth, strategies, backtests, live, portfolios, market-data, account, websocket, alerts)
│   │   ├── core/            # Config, auth, database, logging, queue, secrets, audit, data_integrity, metrics
│   │   ├── live_supervisor/ # Subprocess spawner for TradingNode — heartbeat monitor, process manager, command bus
│   │   ├── models/          # SQLAlchemy 2.0 models (~30 tables)
│   │   ├── schemas/         # Pydantic request/response
│   │   ├── services/        # Business logic (risk_engine, alerting, parquet_store, nautilus/*, security_master/*, live/*, data_sources/*)
│   │   ├── workers/         # arq background workers (backtest, research, portfolio, ingest, live_supervisor)
│   │   ├── main.py          # FastAPI app entrypoint
│   │   └── cli.py           # Typer CLI (8 sub-apps)
│   ├── tests/{unit,integration,e2e}/
│   ├── alembic/             # Database migrations
│   ├── Dockerfile + Dockerfile.dev
│   └── pyproject.toml
├── frontend/                # Next.js 15 + shadcn/ui (15 primitives) + typed API client
│   ├── src/{app,components,lib}/
│   ├── playwright.config.ts       # Playwright scaffold — baseURL http://localhost:3300
│   └── tests/e2e/{specs,fixtures,.auth}/  # Graduated specs + auth fixture
├── strategies/              # Python strategy files (git-only in Phase 1)
├── data/                    # Parquet + reports (gitignored)
├── docs/
│   ├── decisions/           # Architectural decisions (e.g., which-version-to-keep.md)
│   ├── plans/               # Design + implementation plans
│   ├── prds/                # PRDs + discussion logs
│   ├── runbooks/            # Operational runbooks (vm-setup, disaster-recovery, ib-gateway)
│   ├── architecture/        # Platform overview + module maps
│   ├── solutions/           # Post-incident knowledge base
│   ├── research/            # Pre-implementation research briefs
│   ├── CHANGELOG.md
│   ├── nautilus-reference.md    # Full NautilusTrader reference
│   └── nautilus-natives-audit.md
├── tests/e2e/               # Agent artifacts (NOT Playwright scaffold — that lives in frontend/)
│   ├── use-cases/           # Markdown use cases (draft + graduated)
│   └── reports/             # verify-e2e agent output
├── scripts/                 # Operator-invokable scripts (seed_market_data, parity_check, restart-workers, migrate_catalog_to_canonical, etc.)
├── .github/workflows/       # CI
├── .claude/                 # Claude Code configuration (hooks, rules, commands, skills)
├── docker-compose.dev.yml   # Ports: 3300, 8800, 5433, 6380
├── docker-compose.prod.yml
├── CLAUDE.md                # This file
└── README.md
```

### Key Commands

```bash
# Backend development
cd backend && uv run pytest tests/ -v
cd backend && uv run ruff check src/
cd backend && uv run mypy src/ --strict
cd backend && uv run uvicorn msai.main:app --reload   # Dev server on :8000 (Docker maps to :8800)

# Frontend development
cd frontend && pnpm dev                               # Dev server on :3000 (Docker maps to :3300)
cd frontend && pnpm build
cd frontend && pnpm lint

# Docker dev (preferred — hot reload via volume mounts)
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml logs -f
docker compose -f docker-compose.dev.yml down

# Docker prod
docker compose -f docker-compose.prod.yml up -d

# CLI tools (msai is organized as sub-apps: live, strategy, backtest, research,
# graduation, portfolio, account, system, instruments; plus top-level ingest,
# ingest-daily, data-status, health)
cd backend && uv run msai ingest stocks AAPL,MSFT 2024-01-01 2025-01-01   # positional: asset symbols start end
cd backend && uv run msai data-status
cd backend && uv run msai live status
cd backend && uv run msai live kill-all
cd backend && uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers

# Database migrations
cd backend && uv run alembic upgrade head
cd backend && uv run alembic revision --autogenerate -m "description"

# Worker stale-import refresh (after merges touching src/msai/{services,workers,live_supervisor})
./scripts/restart-workers.sh
./scripts/restart-workers.sh --with-broker   # also restart live-supervisor + ib-gateway
```

### API Endpoints

```
/health                              # Liveness probe (unauthenticated)
/ready                               # Readiness probe (unauthenticated)
/api/v1/auth/me                      # GET  Current user from JWT
/api/v1/auth/logout                  # POST Placeholder logout
/api/v1/strategies/                  # GET/PATCH/DELETE  Strategy registry
/api/v1/strategies/{id}/validate     # POST Validate strategy loads
/api/v1/backtests/run                # POST Start backtest (arq job)
/api/v1/backtests/{id}/status        # GET  Poll job status
/api/v1/backtests/{id}/results       # GET  Metrics + trade log
/api/v1/backtests/{id}/report        # GET  Download QuantStats HTML
/api/v1/backtests/history            # GET  List past backtests
/api/v1/live/start-portfolio         # POST Deploy portfolio revision (risk-validated)
/api/v1/live/stop                    # POST Stop deployment
/api/v1/live/kill-all                # POST Emergency halt all
/api/v1/live/status                  # GET  All deployments
/api/v1/live/positions               # GET  Open positions
/api/v1/live/trades                  # GET  Recent executions
/api/v1/live/stream/{deployment_id}  # WS   Real-time updates (JWT first-message auth)
/api/v1/live-portfolios/             # GET/POST/PATCH Portfolio CRUD + revision lifecycle
/api/v1/market-data/bars/{symbol}    # GET  OHLCV bars from Parquet via DuckDB
/api/v1/market-data/symbols          # GET  Available symbols
/api/v1/market-data/status           # GET  Storage stats
/api/v1/market-data/ingest           # POST Trigger data download (arq job)
/api/v1/account/summary              # GET  IB account data
/api/v1/account/portfolio            # GET  IB positions
/api/v1/account/health               # GET  IB Gateway status
/api/v1/alerts/                      # GET  Recent alert history
```

All endpoints except `/health` and `/ready` require Azure Entra ID JWT authentication.

---

## Architecture Notes

### Data Flow

- **Historical data**: Polygon/Databento → Python ingestion → atomic Parquet writes → `{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`
- **Backtesting**: FastAPI → arq queue → backtest worker → NautilusTrader BacktestRunner → QuantStats report → results in PostgreSQL
- **Live trading**: FastAPI → risk engine validation → `live_supervisor` spawns TradingNode subprocess → NautilusTrader → IB Gateway. Supervisor owns heartbeat monitor + command bus (Redis Streams + consumer groups + PEL recovery + DLQ).
- **Dashboard queries**: Frontend → FastAPI → DuckDB (in-memory, reads Parquet) → JSON response

### Key Design Decisions

- `DATA_ROOT` env var controls all Parquet/report paths (Docker: `/app/data`, local: `./data`)
- Strategies are Python files in `strategies/` dir (no UI uploads in Phase 1 — git-only)
- `strategy_code_hash` (SHA256) stored on every backtest/deployment for reproducibility
- Data lineage on Backtest (nautilus_version, python_version, data_snapshot)
- arq (not multiprocessing) for job queue — handles retry, timeout, dead-letter
- PyJWT for backend JWT validation (NOT MSAL — MSAL is frontend only)
- Backend accepts `X-API-Key` header as alternative to Bearer JWT (dev/CLI/testing via `MSAI_API_KEY` env)
- Risk engine validates before every live deployment start; 4-layer kill-all (Redis halt flag + supervisor re-check + push-stop + SIGTERM+flatten)
- Trade dedup via partial unique index on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL` — idempotent reconciliation replay
- WebSocket auth: first message must be JWT token within 5 seconds; reconnect hydrates orders/trades/status/risk_halt from DB

### Portfolio-per-account

Live deployments sit under a `LivePortfolio → LivePortfolioRevision → LiveDeployment` chain. Revisions are immutable once frozen. Multi-strategy TradingNode via `TradingNodeConfig.strategies=[N ImportableStrategyConfigs]`. `FailureIsolatedStrategy` base class wraps event handlers via `__init_subclass__` so one strategy crashing doesn't kill the node.

### Instrument Registry (2026-04-17 / PR #32 + #35)

Tables `instrument_definitions` + `instrument_aliases` hold control-plane metadata for instrument resolution. UUID-keyed with effective-date windowing on aliases for futures rolls. `SecurityMaster.resolve_for_backtest` honors `start` kwarg for historical alias windowing. `msai instruments refresh --provider interactive_brokers` CLI warms the registry via IB qualification.

**Deferred follow-ups:**

- Live-path wiring onto registry (currently `/live/start-portfolio` uses closed-universe `canonical_instrument_id()`)
- `instrument_cache` → registry migration
- Strategy config-schema extraction for UI form generation

### Environment Variables

```
DATABASE_URL=postgresql+asyncpg://msai:password@postgres:5432/msai
REDIS_URL=redis://redis:6379
DATA_ROOT=/app/data
ENVIRONMENT=development|production
MSAI_API_KEY=msai-dev-key               # Alternative to Bearer JWT for dev/CLI/testing
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
JWT_TENANT_ID=your-tenant-id
JWT_CLIENT_ID=your-client-id
CORS_ORIGINS=["http://localhost:3000"]
POLYGON_API_KEY=your-key
DATABENTO_API_KEY=your-key
IB_GATEWAY_HOST=ib-gateway
IB_GATEWAY_PORT_PAPER=4004              # client-side socat proxy port (gateway binds 4002 internally)
IB_GATEWAY_PORT_LIVE=4003               # client-side socat proxy port (gateway binds 4001 internally); documentation-only — flip via IB_PORT=4003
IB_ACCOUNT_ID=DU...                     # paper; real money starts with U
```

### Revival of archived implementation (if ever needed)

Everything in the archived parallel implementation at the time of deletion is preserved at git tag `codex-final`:

```bash
git checkout codex-final -- <path-inside-codex-version>
```

No active work relies on it.

---

### E2E Configuration

**interface_type:** `fullstack` — MSAI v2 exposes an HTTP API (primary) and a Next.js UI (secondary). Per the project ordering rule ("API-first, CLI-second, UI-third"), the `verify-e2e` agent MUST test the API surface first, then the UI. An API failure means the contract/state is broken — stop immediately and diagnose; do not proceed to UI checks.

**Server URLs:**

| Surface    | URL                     |
| ---------- | ----------------------- |
| API base   | `http://localhost:8800` |
| UI base    | `http://localhost:3300` |
| PostgreSQL | `localhost:5433`        |
| Redis      | `localhost:6380`        |

All API routes are versioned under `/api/v1/` (see `.claude/rules/api-design.md`). Health: `GET /health`.

**Pre-flight (before any E2E run):**

1. `curl -sf http://localhost:8800/health` — if it fails, start the stack: `docker compose -f docker-compose.dev.yml up -d`.
2. Confirm the UI responds at `http://localhost:3300` (only if UI use cases are in scope).
3. For live-trading use cases: confirm IB Gateway is reachable (paper account `DU...` on port 4004 — socat proxy to internal 4002; live account on 4003 — socat to internal 4001) — see `.claude/rules/nautilus.md` gotcha #6.

**Auth.** The app uses Azure Entra ID (MSAL on the frontend, PyJWT on the backend). E2E runs should authenticate via the documented login flow OR use a dev-mode bypass token if one is configured — never by forging JWTs or reading secrets from disk.

**ARRANGE (test setup) is allowed via any user-accessible interface:**

- Public API: `POST /api/v1/backtests/run`, `POST /api/v1/live/start-portfolio`, `POST /api/v1/live-portfolios/`, etc. Note: strategies are registered from the filesystem via git, not created through the API (Phase 1 decision — no UI uploads).
- CLI scripts exposed under `backend/` (treat as documented commands only).
- The dev seed/bootstrap scripts if present.

**ARRANGE is NOT allowed via:**

- Direct Postgres queries against `localhost:5433`
- Writing Parquet files into `data/` by hand
- Pushing into Redis queues directly
- Reading environment secrets to mint tokens

**VERIFY (assertions) MUST go through the same interface the use case targets.** API use cases check response bodies and subsequent GETs; UI use cases check what Playwright sees on screen (`data-testid`, role selectors) and reload to confirm persistence. Never peek at Postgres, DuckDB, or Parquet to "confirm" — if it isn't visible through the API or UI, it doesn't count as verified.

**Live-trading safety rails.** Default every E2E use case that touches order submission to a paper IB account (see `reference_ib_accounts.md`). Live-account use cases must be opt-in, explicit in the use-case file, and never triggered from the standard regression suite. Stop-the-world when any API use case returns 5xx during a live/paper flow — do not continue UI verification against a node in unknown state (gotcha #13: stopping Nautilus does not close positions).

**Core use-case categories** (for inventory in `tests/e2e/use-cases/`):

- `strategies/` — create, edit, list, hash versioning
- `backtests/` — submit, poll status, fetch report, download artifacts
- `live/` — portfolio create, deploy, start/stop, positions, order events
- `data/` — instrument lookup, catalog browse, bar chart rendering
- `auth/` — login, token refresh, logout, RBAC

See `.claude/rules/testing.md` for the full use-case lifecycle (draft → execute → graduate) and failure classification (PASS / FAIL_BUG / FAIL_STALE / FAIL_INFRA).

### Playwright Framework

Scaffolded inside `frontend/` because msai-v2 is a backend+frontend split and the forge's `setup.sh --with-playwright` auto-detects the lone `package.json` subdirectory:

- `frontend/playwright.config.ts` — `baseURL` defaults to `http://localhost:3300` (host-exposed Docker port). Override per run with `PLAYWRIGHT_BASE_URL=<url>`.
- `frontend/tests/e2e/specs/` — graduated spec files (currently empty; future feature work should author specs here using `getByTestId` / role-based selectors).
- `frontend/tests/e2e/fixtures/` — auth fixture + helpers.
- `frontend/tests/e2e/.auth/` — gitignored storage state (credentials).

Verify-e2e agent artifacts live at the repo root (independent of the Playwright framework):

- `tests/e2e/use-cases/` — markdown use cases (draft before graduation, then checked in under `backtests/`, `strategies/`, `live/`, etc.).
- `tests/e2e/reports/` — verify-e2e agent output (markdown reports, HTML on failure).

Run specs locally:

```bash
cd frontend && pnpm exec playwright test
```

API-only use cases don't need Playwright — the `verify-e2e` agent hits the REST endpoints directly with curl/httpx.

### Research Enforcement

The `research-first` agent runs in Phase 2 of `/new-feature` (before design begins). It queries Context7, WebSearch, and WebFetch for every external library this feature touches and produces a brief at `docs/research/YYYY-MM-DD-<feature>.md`. The design phase reads this brief to avoid building on stale assumptions.

For bug fixes, targeted research runs after root-cause isolation (Phase 2.5 of `/fix-bug`).

---

### Visual Design Preferences

- Never generate plain static rectangles for hero sections, landing pages, or key visual moments
- Always include at least one dynamic/animated element: SVG waves, Lottie, shader gradients, or canvas particles
- Prefer organic shapes (blobs, curves, clip-paths) over straight edges and 90-degree corners
- Animations must respect `prefers-reduced-motion` — provide static fallbacks
- Premium, dark-mode-first aesthetic (Linear.app / Vercel.com style). Font: Geist. Color: shadcn/ui dark theme via CSS custom properties (oklch).

## No Bugs Left Behind Policy

**NEVER defer known issues "for later."** When a review, test, or tool flags an issue — fix it in the same branch before moving on. This applies to:

- Code bugs found during review
- Deployment/infrastructure issues found during testing
- Configuration mismatches across environments (Docker, K8s, Helm)
- Security findings from any reviewer (Claude, Codex, PR toolkit)
- Test coverage gaps for new code

No "follow-up PRs" for known problems. No "v2" for things that should work in v1. If it's found, it's fixed — or the branch isn't ready.

## Detailed Rules

All coding standards, workflow rules, and policies are in `.claude/rules/`.
These files are auto-loaded by Claude Code with the same priority as this file.

**What's in `.claude/rules/`:**

- `principles.md` — Top-level principles and design philosophy
- `workflow.md` — Decision matrix for choosing the right command
- `worktree-policy.md` — Git worktree isolation rules
- `critical-rules.md` — Non-negotiable rules (branch safety, TDD, etc.)
- `memory.md` — How to use persistent memory and save learnings
- `security.md`, `testing.md`, `api-design.md` — Coding standards
- `nautilus.md` — **NautilusTrader top-20 gotchas** (read before any Nautilus code work). Full reference: `docs/nautilus-reference.md`
- Language-specific: `python-style.md`, `typescript-style.md`, `database.md`, `frontend-design.md`
