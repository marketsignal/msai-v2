# MSAI v2 (MarketSignal AI) — Project Context

This is the shared, project-owned context for Claude Code and Codex. Read it completely before project action, as required by the root adapters. Keep the deployment procedures, API inventory, data-provider knowledge and E2E configuration here so every agent sees them. Canonical Forge workflow policy lives in [`.forge/instructions.md`](../.forge/instructions.md); this document supplies MSAI's goals, facts and operating constraints.

**Last reconciled: 2026-10-04.** The goal below is the intended product. Implementation descriptions are not proof of acceptance. Dated runtime observations are snapshots; verify the relevant environment before operating it. [Master Map](../MASTER_MAP.md) records capability evidence and open findings; [Master Plan](../MASTER_PLAN.md) owns delivery milestones, the monthly roadmap, status and acceptance gates.

## Project Overview

### Goal

Build a professional-grade hedge fund research and portfolio operations platform. The operator should be able to focus on researching strategies, assessing evidence and managing portfolios across brokerage accounts, with dependable infrastructure and clear financial records.

The complete workflow is **research → strategy backtesting and validation → portfolio construction and validation → deployment to an account → live monitoring, reconciliation and controlled revision**. The platform must support multiple accounts, each with a deployed portfolio composed of strategies, and consistent account switching through the API, CLI and UI. Interactive Brokers is the first supported broker; other brokers are a future extension of the same account/portfolio model.

Professional quality means reproducible experiments, controls against leakage and selection bias, explicit execution costs and market assumptions, correct portfolio/account accounting, verified account routing, effective risk controls, recoverable operation and traceable actions. The platform assesses evidence for alpha; neither a completed backtest nor the platform itself guarantees profitable strategies.

The earlier goal of completing an AAPL/SPY EMA backtest was an initial milestone. It is not the product boundary.

### Product model and scope

- **Strategy:** versioned Python code, configuration and evidence. Author and evaluate strategies through real backtests, independent validation and forward observation before assigning capital. Track failed experiments as well as selected results.
- **Portfolio:** a versioned combination of strategies with defined allocations, capital/risk assumptions and portfolio-level validation. Combining independently calculated return curves does not by itself prove shared-cash or margin behavior.
- **Account deployment:** bind an approved portfolio revision to an explicit brokerage account. The intended operating model is one active portfolio per account, containing multiple strategies. Different accounts may use different portfolios, or the same revision with account-specific capital and limits. Enforcing replacement, allocation and account ownership is part of acceptance, not an assumption about current code.
- **Account and fleet views:** show fresh positions, orders, fills, cash, exposure, performance and system health by account, portfolio and strategy. Observations follow the selected view scope. Account commands name and validate their execution target independently; fleet-wide controls retain explicit fleet scope. A UI filter alone is not an authorization boundary. Managing customer accounts does not automatically imply customer logins or a public multi-tenant SaaS product.
- **Interfaces:** API first, CLI second, UI third is the implementation and verification order. All three are product interfaces and must express the same account/portfolio identities and financial meanings.
- **Markets and cadence:** the target includes minute data for stocks, indexes, futures, selected options and some crypto, with strategies acting every 5–10 minutes or daily and a potential universe of about 100 underlyings. This is not high-frequency trading. Asset correctness, derived bar intervals and capacity must be demonstrated before those targets are advertised as supported.
- **Broker and access scope:** prove the full account lifecycle with Interactive Brokers first. Preserve a clear broker boundary without building speculative adapters. Access for the operator and partners uses Microsoft Entra ID; permission and account-access policies require explicit enforcement.
- **AI:** AI/LLM trading features remain deferred. AI-assisted strategy authoring and analysis must pass the same evidence and execution controls as human-authored work; generated explanations are not validation evidence.

### What Is This?

MSAI v2 is the current implementation toward that goal: Python strategies, Nautilus backtests and trading nodes, research/portfolio workflows, account routing, and a custom operations dashboard. Much of the workflow exists, but professional readiness has not been established. Use the evidence below and in the Master Map to distinguish implemented, tested, runtime-observed and unverified behavior.

### Current implementation focus

Read the [Master Plan delivery milestones and monthly roadmap](../MASTER_PLAN.md#delivery-milestones) before selecting implementation work. The delivery sequence is **reliable strategy research → reliable portfolio operations → dependable daily operation**, with operational repairs and usable interfaces throughout. The immediate priority is trustworthy strategy iteration: reconcile backtest economics and data, protect independent validation, and prove the same research workflow through API, CLI and real-browser computer use.

The Master Plan owns changing dates, progress, dependencies and next actions; do not duplicate its task tracker here. This context retains mandatory operating knowledge and the [UI/browser acceptance requirements](#ui-implementation-and-real-browser-acceptance). A research milestone does not confer portfolio-execution or live-capital readiness.

**Installed research baseline, October 4:** the last directly verified Azure application revision is `fa4c8f8`, after a successful automatic deployment. The bounded AAPL reference reconciles through API/CLI checks and real-browser reload/full report; the earlier browser submission is in the [first-installation evidence](audits/2026-10-03/azure-first-installation.md). Opening capital and realized-balance scope are explicit; old unknown economics remain unavailable. This does not certify broker costs, full NAV, unbiased selection, other assets or live trading. The [normal-pipeline record](audits/2026-10-03/azure-normal-pipeline.md) preserves passed installation/recovery cases and the prior cancellation/CLI failures.

The [cancellation follow-up](audits/2026-10-03/release-cancellation-candidate.md), reviewed head `d5e5197`, merged through [PR105](https://github.com/marketsignal/msai-v2/pull/105) at 05:22 UTC as `f4ede895`. PR checks, local/read-only Azure verification and closure reviews passed. [Exact-main CI](https://github.com/marketsignal/msai-v2/actions/runs/37179681442), main image build/auth and [automatic Deploy 37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) passed, including public probes and temporary SSH cleanup completed at 05:42 UTC. This workflow result does not replace a fresh direct VM inventory or actual repaired-runner cancellation acceptance. The Master Plan owns remaining operational acceptance; preserve operational E2E PARTIAL.

**Local consolidation and tooling publication, October 4:** all four merged feature worktrees are archived recoverably, and their local and GitHub branches are deleted. This package includes the operator's installed Forge 6.4.3 and the latest assessment/context updates, published from one temporary review worktree due for cleanup after merge. Seven guarded local research services use primary source mounts and the existing database/data; a real-browser reload retained the local 100-fill reference. Use the startup configuration below. This does not make local and Azure broker/environment configurations equivalent. [Preservation and runtime evidence](audits/2026-10-03/worktree-consolidation.md).

### History

This project was originally built in parallel by two AI implementations from the same PRD, and compared side-by-side through 2026-02 to 2026-04. The comparison concluded 2026-04-19 (council verdict in [the version decision](decisions/which-version-to-keep.md)); the older Codex implementation was archived at tag `codex-final` and removed. The surviving implementation was flattened to the repo root. **The root is the shipping implementation; there is no active "version" suffix.** Historical Codex NQ research and test results do not automatically describe this tree. The decision's postscript records why an attempted direct port of the archived browser tests was abandoned.

### Stack

- **Backend:** Python 3.12 + FastAPI + NautilusTrader + arq (Redis job queue)
- **Frontend:** Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Charts + Recharts
- **Database:** PostgreSQL 16 + Parquet files + DuckDB + Redis 7
- **Auth:** Azure Entra ID (MSAL frontend, PyJWT backend)
- **Deploy:** Docker Compose on Azure VM; the observed production VM is Standard_D4ds_v6. Splitting research and live compute across hosts is a future decision based on measured contention and recovery requirements, not an implemented or unconditional "Phase 2" promise.
- **Data sources:** Databento defaults to `EQUS.MINI` for equities and `GLBX.MDP3` for futures. Databento is the intended live market-data source; IB Gateway supplies execution/account connectivity. Historical data availability is recorded in the dated vendor table below. Provider coverage does not certify MSAI's asset/catalog handling.
- **Broker identity:** LVP `U4705114` and HVP `U4715997` are the named live test accounts from the historical operating plan. **Do not assume the old local-LVP / Azure-HVP mapping is currently active.** The October 3 inspection found an LVP production registry entry but null broker-reported account identity. Avoid competing sessions on the same IB login; establish the actual account/login/mode before starting another gateway. The real fund account remains outside the standing test authorization.

### Verified baseline and current limits

The [initial October 3 runtime assessment](audits/2026-10-03/runtime-assessment.md) and [later Azure installation record](audits/2026-10-03/azure-first-installation.md) record distinct execution snapshots:

- Local equity backtest, two research trials, discovery-candidate creation, a one-member quick portfolio simulation, CLI reads and QuantStats reports succeeded using existing data.
- Azure owner Entra sign-in, browser backtest submission, 166 simulated fills, native results and a full report succeeded. Full research-to-live acceptance and current broker order execution were not tested.
- Both initial environments produced incorrect headline return units and omitted first-day PnL. The merged repair now passes the Azure reference: 166 fills, −$1.21 on $1 million, first-day −$0.09 included; API, CLI and browser agree. Research holdout/selection, multi-asset modeling, risk wiring and broader financial attribution remain open. Completed jobs are not investment approval.
- Initial automatic deployments failed on stale temporary SSH access and later Azure CLI writes. The old orphan was removed; the automatic `fa4c8f8` installer and normal cleanup passed. Real active-owner preservation, original-attempt cleanup-only rerun and SSH-failure cleanup also passed. Prior cancellation acceptance failed: after an intermittent CLI runtime crash in setup, a repeat kept its producer running after ordinary cancellation until SSH timed out; cleanup then passed. PR105 integrates a cancellable producer and time bounds; actual repaired-runner behavior still needs acceptance. The scheduled reaper is active without fresh execution proof. Supervisor remains last observed at `65ae682`; root disk was 85% used with 9.3 GiB free at 02:28 UTC. Backup restoration and intended data-disk use remain unverified.
- Local research services retain disabled vendor ingestion and broker access through the assessment override. The merged candidate repairs CLI settings/diagnostics and browser history/error presentation; installed Azure CLI and the browser reference passed. Intermittent browser history/trades fetch failures recovered on Retry/reload, but their transport cause and repair remain unverified. Primary source and local mounts were subsequently reconciled during worktree consolidation; the local persistence handoff passed. Never publish raw sensitive diagnostics, and verify the actual runtime for the acceptance scope.

These are dated observations, not permanent environment facts. Consult the linked evidence before repeating a completed investigation; refresh the relevant checks when the code, environment or intended operation changes.

### Ports (dev)

| Service            | Host port | Container port |
| ------------------ | --------- | -------------- |
| Frontend (Next.js) | `3300`    | `3000`         |
| Backend (FastAPI)  | `8800`    | `8000`         |
| PostgreSQL         | `5433`    | `5432`         |
| Redis              | `6380`    | `6379`         |

### Running the stack

Run from the repository root. To resume the existing-data research assessment with its broker/vendor guards and existing images:

```bash
# Nonsecret placeholders satisfy disabled broker-profile interpolation.
IB_ACCOUNT_ID=disabled TWS_USERID=disabled TWS_PASSWORD=disabled COMPOSE_PROFILES='' \
docker compose --project-name msai-v2 --env-file /dev/null \
  -f docker-compose.dev.yml \
  -f docs/audits/2026-10-03/local-runtime.override.yml \
  -f docs/audits/2026-10-03/local-runtime-source.override.yml \
  up -d --no-build postgres redis backend frontend backtest-worker research-worker portfolio-worker

# Health checks
curl http://localhost:8800/health
curl http://localhost:8800/ready
open http://localhost:3300

# Logs
docker compose -f docker-compose.dev.yml logs -f
```

The stable source override is specific to this operator's checkout path; adjust paths deliberately for another host. The project name and empty env file preserve the consolidated local assessment configuration. The standard development startup remains `docker compose -f docker-compose.dev.yml up -d`; it does **not** preserve these overrides. Inventory existing services and select the intended mode first. The API starts account-connection tasks even when no broker container is launched, so omitting the broker profile alone is not a broker-disable switch. The override points those tasks at closed loopback port 65534, disables scheduled ingestion and automatic data healing, and clears the vendor key. Keep it for this bounded research mode; inspect any additional `IB_HOST` override because it has precedence over `IB_GATEWAY_HOST`.

IB Gateway and the live supervisor are behind the `broker` Compose profile. The historical broker-start form is:

```bash
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d
```

Use it only within an authorized broker task after verifying account/login/mode, routing and existing sessions. It can start/recover live processes; it is not a passive health check. `docker compose -f docker-compose.dev.yml down` stops the project; do not run it against active trading merely to reset development. Health/readiness do not certify worker progress or safe trading. `/ready` may also bootstrap the configured API-key user.

### Deploying to production

**Azure identity:** MSAI belongs to the **MarketSignal.ai LLC** Azure/Entra tenant. Use **`pablo@marketsignal.ai`** for the platform and tenant sign-in. This environment is separate from KSG; **`pablo@ksgai.com` is not the MSAI tenant account**. MarketSignal2 subscription is `68067b9b-943f-4461-8cb5-2bc97cbc462d`; tenant is `2237d332-fc65-4994-b676-61edad7be319`. Supply the subscription explicitly and verify the identity rather than relying on the CLI default. Preserve other projects' Azure default context; do not change the shared default subscription for this task. The existing production target is `msaiv2_rg/msai-vm`. Confirmed by the operator and fresh inspection on 2026-10-03.

**Push to main triggers automatic deployment toward the Azure VM** via a two-workflow chain. A trigger is not proof of success. Automatic Deploy 37171071535 successfully installed `fa4c8f8` on October 4, with normal runner rule creation/deletion and full installer/public checks. Cleanup-only recovery and deliberate SSH-failure cleanup passed. An intermittent `CLI_RUNTIME_ERROR` still lacks an identified exception. The producer's `always()` job condition also caused an accepted cancellation to continue into SSH staging until timeout; cleanup succeeded afterward. Do not treat ordinary Cancel as a proven stop of the installer. Scheduled reaping remains unverified despite an active workflow. See the [pipeline evidence](audits/2026-10-03/azure-normal-pipeline.md) and Master Plan for the narrow follow-up.

1. `.github/workflows/build-and-push.yml` (Slice 2) — OIDC → ACR → docker build & push tagged `<sha7>`.
2. `.github/workflows/deploy.yml` (Slice 3 + 4), as merged in PR103 — resolve exact application revision → require current main-push CI/auth evidence for that same SHA → require authenticated complete quiet-fleet readiness → OIDC + ownership-verified transient SSH rule → repeat readiness before VM execution → `scp` + `ssh sudo bash deploy-on-vm.sh` → Compose update and VM probes → runner public probes → separate ownership-verified cleanup. Helpers are pinned to the executing workflow revision independently of application rollback. VM probe failures trigger image rollback; runner-side TLS/public/frontend failures do not automatically roll back.

Manual rollback / re-deploy / rehearsal-RG dispatch use `gh workflow run deploy.yml -f git_sha=<sha>` (and full override matrix for rehearsal).

**Read [how_to_deploy.md](how_to_deploy.md) before deployment**, plus the [release-safety runbook](operations/release-safety.md), now present in primary main. Retain the architecture, rehearsal, repository-variable and recovery runbooks. Routine deployment excludes broker-profile services, but the stock installer also refreshes environment/vendor configuration and operates the existing watchdog; inspect those effects when defining maintenance scope. The first supervised installation avoided that installer; the subsequent normal automatic deployment exercised it. Same-revision CI and stopping/restart blockers are repaired; actual cancellation acceptance, intermittent CLI failure and scheduled recovery remain open. Image rollback does not reverse migrations. These procedures are references, not automatic authorization to execute them during an audit.

### File Structure

```
msai-v2/
├── backend/                 # FastAPI, CLI, workers, research and trading services
│   ├── src/msai/
│   │   ├── api/             # FastAPI routers (auth, strategies, backtests, live, portfolios, market-data, account, websocket, alerts)
│   │   ├── core/            # Config, auth, database, logging, queue, secrets, audit, data_integrity, metrics
│   │   ├── live_supervisor/ # Subprocess spawner for TradingNode — heartbeat monitor, process manager, command bus
│   │   ├── models/          # SQLAlchemy application and audit models
│   │   ├── schemas/         # Pydantic request/response
│   │   ├── services/        # Business logic (risk_engine, alerting, parquet_store, nautilus/*, security_master/*, live/*, data_sources/*)
│   │   ├── workers/         # arq background workers (backtest, research, portfolio, ingest, live_supervisor)
│   │   ├── main.py          # FastAPI app entrypoint
│   │   └── cli.py           # Typer CLI and local operator commands
│   ├── tests/{unit,integration,e2e}/
│   ├── alembic/             # Database migrations
│   ├── Dockerfile + Dockerfile.dev
│   └── pyproject.toml
├── frontend/                # Next.js + shadcn/ui + typed API client
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
├── .forge/                  # Canonical Forge policy, workflows and roles
├── .claude/ + .codex/ + .agents/ # Host adapters and discovery surfaces
├── docker-compose.dev.yml   # Ports: 3300, 8800, 5433, 6380
├── docker-compose.prod.yml
├── AGENTS.md + CLAUDE.md    # Thin pointers to this context and Forge policy
├── MASTER_MAP.md + MASTER_PLAN.md # Evidence-based assessment and proposed work
└── README.md
```

### Key Commands

```bash
# Backend development (run each command from the repository root)
cd backend && uv run pytest tests/unit/<owning_test_file>.py -v  # replace placeholder
cd backend && uv run ruff check src/
cd backend && uv run mypy src/ --strict
cd backend && uv run uvicorn msai.main:app --reload   # Dev server on :8000 (Docker maps to :8800)

# Frontend development
cd frontend && pnpm dev                               # Dev server on :3000 (Docker maps to :3300)
cd frontend && pnpm build
cd frontend && pnpm lint

# Docker startup/mode selection: use "Running the stack" above.
# Production changes: use the supported deployment procedure above.

# CLI examples; groups and target configuration are listed below.
cd backend && uv run msai ingest stocks AAPL,MSFT 2024-01-01 2025-01-01   # positional: asset symbols start end
cd backend && uv run msai data-status
cd backend && uv run msai live status
cd backend && uv run msai live kill-all
cd backend && uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers

# Database migrations (mutating operations, only when in task scope)
cd backend && uv run alembic upgrade head
cd backend && uv run alembic revision --autogenerate -m "description"

# Worker stale-import refresh (after merges touching src/msai/{services,workers,live_supervisor})
./scripts/restart-workers.sh
./scripts/restart-workers.sh --with-broker   # also restart live-supervisor + ib-gateway
```

Use focused owning checks during development; broad regression belongs in CI or an explicitly requested full run. Run each `cd ... && ...` example independently from the repository root. CLI groups are `strategy`, `backtest`, `research`, `live`, `graduation`, `portfolio`, `account`, `broker`, `system`, `instruments`, `alerts`, `auth`, `market-data` and `symbols`; top-level commands include `health`, `ingest`, `ingest-daily`, `data-status` and `whoami`.

API-backed CLI commands read `MSAI_API_URL` (default `http://localhost:8000`; use `http://localhost:8800` for the Docker host API) and `MSAI_API_KEY`. Top-level ingestion/data-status and some operator commands execute locally, so setting a remote API URL does not relocate every command. The initial October 3 host fallback from repo root was `PYTHONPATH=backend/src backend/.venv/bin/python -m msai.cli ...` because that host lacked the installed `msai` executable. The backend-directory form is `cd backend && PYTHONPATH=src .venv/bin/python -m msai.cli ...`; settings load `.env` relative to the working directory, so the forms can select different configuration. In an isolated worktree, use its intended source and an available compatible interpreter explicitly. The older checkout's root Compose `.env` caused settings validation errors containing sensitive inputs; the merged repair ignores Compose-only settings and redacts validation inputs. Installed Azure CLI reads passed, and primary source is now reconciled to main. Use the intended API configuration and never copy raw sensitive diagnostics. Restart commands affect running processes and require the same account/session awareness as startup.

### API Endpoints

Principal route inventory, checked against the mounted routers on 2026-10-03. Paths in the table have the `/api/v1` prefix. Exact schemas, query parameters and less common operations remain defined by the router/OpenAPI contract; this table does not imply every route has passed end-to-end acceptance.

| Family | Methods and paths | Purpose |
| --- | --- | --- |
| Authentication | `GET /auth/me`; `POST /auth/logout` | Authenticated user; logout route is an unauthenticated placeholder. |
| Strategies | `GET /strategies/`; `GET/PATCH/DELETE /strategies/{id}`; `POST /strategies/{id}/validate` | Registry, configuration and Python strategy validation. |
| Backtests | `POST /backtests/run`; `GET /backtests/history`; `GET /backtests/{id}/status`; `GET /backtests/{id}/results`; `GET /backtests/{id}/trades`; `POST /backtests/{id}/report-token`; `GET /backtests/{id}/report` | Queued simulation, metrics/series, paginated fills and QuantStats report delivery. |
| Research | `POST /research/sweeps`; `POST /research/walk-forward`; `GET /research/jobs`; `GET /research/jobs/{id}`; `POST /research/jobs/{id}/cancel`; `POST /research/promotions` | Experiment jobs and candidate promotion. |
| Graduation | `GET/POST /graduation/candidates`; `GET /graduation/candidates/{id}`; `POST /graduation/candidates/{id}/stage`; `GET /graduation/candidates/{id}/transitions` | Candidate evidence and stage history; current evidence gates have open findings. |
| Research portfolios | `GET/POST /portfolios`; `GET /portfolios/{id}`; `GET /portfolios/{id}/allocations`; `GET /portfolios/runs`; `POST /portfolios/{id}/runs`; `GET /portfolios/runs/{id}`; `GET /portfolios/runs/{id}/report`; `POST /portfolios/runs/{id}/cancel`; `POST /portfolios/runs/{id}/promote-to-live`; `POST /portfolios/smoke/runs` | Portfolio construction, simulation and promotion. |
| Live portfolio definitions | `GET/POST /live-portfolios`; `GET /live-portfolios/{id}`; `POST /live-portfolios/{id}/strategies`; `POST /live-portfolios/{id}/snapshot`; `GET /live-portfolios/{id}/members`; `GET /live-portfolio-revisions/{revision_id}/members` | Live composition, immutable revisions and member inspection; there is no live-portfolio PATCH route. |
| Live commands | `POST /live/start-portfolio`; `POST /live/stop`; `POST /live/kill-all`; `POST /live/drain/{account_id}`; `POST /live/resume`; `POST /live/resume/{account_id}` | Account-bound deployment and lifecycle controls. Old `POST /live/start` is an unauthenticated **410 deprecated** tombstone. |
| Live observations | `GET /live/status`; `GET /live/status/{deployment_id}`; `GET /live/positions`; `GET /live/trades`; `GET /live/data-health`; `GET /live/audits/{deployment_id}`; `WS /live/stream/{deployment_id}` | Status, positions, fills, feed health, audit trail and stream. Verify account/deployment scope and freshness. |
| Release readiness | `GET /live/release-readiness` (installed in `23db3b8`) | Authenticated fleet scope; contract 1, complete=true, ready=true and zero deployment/process/restart blockers are all required. Includes nonterminal/unknown states, orphan process state and latest restart eligibility. Database/auth/old-contract errors refuse. This is a point-in-time lifecycle snapshot, not a maintenance lock or broker-flatness proof. |
| Broker accounts | `GET/POST /broker-accounts`; `GET/PATCH /broker-accounts/{id}`; `POST /broker-accounts/{id}/rotate-credentials`; `POST /broker-accounts/{id}/archive` | Broker account registry and lifecycle. A registry row is not proof of the connected broker identity. |
| Market data | `GET /market-data/bars/{symbol}`; `GET /market-data/symbols`; `GET /market-data/status`; `POST /market-data/ingest` | DuckDB queries and stored-data/ingestion controls. |
| Symbol onboarding | `POST /symbols/onboard/dry-run`; `POST /symbols/onboard`; `GET /symbols/onboard/{run_id}/status`; `POST /symbols/onboard/{run_id}/repair`; `GET /symbols/readiness`; `GET /symbols/inventory`; `DELETE /symbols/{symbol}` | Data availability/onboarding and lifecycle; distinguish preview, download and deletion. |
| Instruments | `POST /instruments/bootstrap` | Instrument registry preparation. |
| Account snapshots | `GET /account/summary`; `GET /account/portfolio`; `GET /account/health` | Broker account data, positions and connection health; these are not a full historical performance ledger. |
| Operations | `GET /alerts/`; `GET /system/health` | Alerts and operational visibility. |

Unprefixed backend routes: `GET /health` (liveness), `GET /ready` (PostgreSQL readiness plus API-user bootstrap), `GET /metrics` (metrics). They are unauthenticated at the application layer; deployment/proxy exposure is a separate concern.

Protected REST routes accept an Entra access token in `Authorization: Bearer …` or the configured `MSAI_API_KEY` in `X-API-Key`. Entra validation checks tenant issuer, client audience, token version 2.0 and delegated `access_as_user` scope. Backtest report downloads also accept a short-lived signed `?token=` capability; portfolio reports require normal authenticated headers. Never retain that capability in shared evidence. Stored viewer/operator roles do not currently enforce REST permissions (M09).

WebSocket authentication sends the raw JWT or API key as the first text message within five seconds. API-key access covers all deployments; JWT access requires deployment ownership. REST/WS visibility therefore needs explicit reconciliation rather than assuming identical authorization.

---

## Architecture Notes

### Data Flow

- **Historical data**: Databento → Python ingestion → atomic Parquet writes → `{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`
- **Backtesting**: FastAPI → arq queue → backtest worker → NautilusTrader BacktestRunner → QuantStats report → results in PostgreSQL
- **Live trading**: FastAPI account/revision/start checks → `live_supervisor` spawns TradingNode subprocess → NautilusTrader → IB Gateway. Supervisor owns heartbeat monitor + command bus (Redis Streams + consumer groups + PEL recovery + DLQ). Quantitative risk validation is not fully wired into this active path; see M06.
- **Dashboard queries**: Frontend → FastAPI → DuckDB (in-memory, reads Parquet) → JSON response

### Databento data availability — recorded verification 2026-06-03

The previous investigation reported empirical verification on our **Standard** account using `metadata.list_datasets`, `get_dataset_range` and sample fetches, cross-checked against vendor documentation. Preserve this knowledge to avoid repeating settled research. The table is that dated evidence, **not a fresh vendor/account entitlement check on every context revision**. Recheck the relevant dataset when a requested symbol/window, changed offering, entitlement issue or contradictory result warrants it; do not restart the entire investigation without a reason.

| Asset class                             | Dataset(s)                                                                                                                   | History from   | Notes                                                                       |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------- | --------------------------------------------------------------------------- |
| **Futures** (ES, NQ, YM, EMD)           | `GLBX.MDP3`                                                                                                                  | **2010-06-06** | Consolidated exchange feed; RTY history was reported from 2017. |
| **Equities — per-VENUE feeds**          | `ARCX.PILLAR` (NYSE Arca), `XNAS.ITCH` (Nasdaq), `XNYS.PILLAR`, `XASE.PILLAR`, `BATS/BATY/EDGA/EDGX.PITCH`, `XBOS/XPSX.ITCH` | **2018-05-01** | One-minute and daily; single-venue coverage, not total-market volume. |
| **Equities — CONSOLIDATED**             | `EQUS.MINI`, `DBEQ.BASIC` (historically deprecated 2025-01)                                                                   | **2023-03-28** | Use the configured `EQUS.MINI` path; the former investigation recorded `DBEQ.BASIC` deprecation as 2025-01-13. |
| **Equities — consolidated EOD/summary** | `EQUS.SUMMARY`, `XNAS.BASIC`                                                                                                 | **2024-07-01** | EOD/summary coverage does not substitute for minute history. |

**Operational implications of that evidence:**

- **A 2023 equities cutoff can be a dataset-choice issue.** The recorded consolidated `EQUS.MINI` history begins in 2023, while selected single-venue feeds begin in 2018. The prior examples used `ARCX.PILLAR` for SPY/IWM/DIA/EEM/EFA/LQD and `XNAS.ITCH` for QQQ/AAPL/MSFT. Confirm the symbol/dataset pair rather than assuming every symbol has the dataset's full range.
- Single-venue volume is not consolidated volume. Earlier volume-based equity studies need an explicitly limited single-venue interpretation or a validated aggregation approach covering the required venues. Do not label that input total-market data without evidence.
- The recorded archive did not supply 15 years of equity history. ES futures via `GLBX.MDP3` offered deeper S&P-related history, but futures are a different instrument and MSAI's futures modeling has open defects. Do not silently substitute ES for SPY or treat vendor coverage as proof the backtest is economically correct.
- The previous Standard-account check did not justify upgrading to Plus solely to obtain the listed older history. Do not assume a higher tier creates pre-inception data. Check current entitlements, licensing and the requested schema before a purchase or download; old pricing/size estimates are not purchasing evidence.
- Before designing a new window, use recorded coverage plus `metadata.get_dataset_range(dataset=...)` and, when needed and within the authorized data-download scope, a small sample fetch. Distinguish vendor availability, data actually stored here, and completeness within the requested sessions.

### Key Design Decisions

- `DATA_ROOT` env var controls all Parquet/report paths (Docker: `/app/data`, local: `./data`)
- Strategies are Python files in `strategies/` dir (no UI uploads in Phase 1 — git-only)
- Strategy hashes and backtest lineage fields (`nautilus_version`, `python_version`, `data_snapshot`) support provenance. They do not yet bind every executed helper/config/data byte into an immutable experiment; see Master Map M16.
- arq/Redis provide background job queues, with application retry/cancellation/recovery logic. Live command streams have separate consumer-group/PEL/DLQ handling; do not infer every queue has identical delivery guarantees.
- PyJWT for backend JWT validation (NOT MSAL — MSAL is frontend only)
- Backend accepts `X-API-Key` header as alternative to Bearer JWT (dev/CLI/testing via `MSAI_API_KEY` env)
- Live safety mechanisms include fleet/account Redis halt latches, supervisor checks, push-stop and process-stop/flatten paths. `RiskAwareStrategy` gates opening submit/modify calls in live mode while allowing reduce-only/`MARKET_EXIT` exits. A failed or stale halt read is intended to fail closed; **a missing key after Redis state loss is a separate known gap** (M08). Position/exposure/daily-loss limits are not fully connected to live order execution (M06). Do not describe these mechanisms as complete risk enforcement or assume every strategy reaches the same gate.
- Trade dedup via partial unique index on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL` — idempotent reconciliation replay
- WebSocket reconnect hydrates orders/trades/status/risk_halt from persisted records; authentication and its current visibility differences are described in the API inventory above.

### Portfolio-per-account

The implementation has a `LivePortfolio → LivePortfolioRevision → LiveDeployment` chain, revision members and a broker-account routing layer. Frozen revisions and `TradingNodeConfig.strategies=[N ImportableStrategyConfigs]` support multi-strategy composition. `FailureIsolatedStrategy` wraps strategy event handlers to contain member exceptions; this is not complete process or resource isolation.

The target is explicit portfolio deployment per account, with account-specific capital, limits and reconciled performance. Stored revision weights do not currently guarantee live order sizing. The [goal-alignment assessment](audits/2026-10-03/goal-alignment.md) records these source-confirmed gaps:

- A graduation candidate is linked to one deployment and becomes ineligible for general reuse; the same approved evidence is not cleanly reusable across account deployments (M25).
- Active-process uniqueness is per deployment, not account; the inspected guards permit another portfolio after the first is running. The intended one-active-portfolio rule is not enforced (M26).
- Account scope is partial across API/CLI/UI, and All positions can collapse to a single streamed deployment. Live daily PnL aggregation writes placeholders instead of a reconciled performance history (M27/M28).
- Research-portfolio promotion requires a `DU` paper-format account, but stores that target only in the new composition's description. Actual account deployment is selected later; clarify this contract without assuming a provisioned paper account or bypassing the restriction (M29).

These gaps remain open after this documentation revision. A real two-account, multi-strategy acceptance journey is required before claiming the product model works end to end. Other brokers remain future work: the present broker registry, credentials and execution factory are IB-specific.

### Instrument Registry (2026-04-17 / PR #32 + #35)

Tables `instrument_definitions` + `instrument_aliases` hold control-plane metadata for instrument resolution. UUID-keyed with effective-date windowing on aliases for futures rolls. `SecurityMaster.resolve_for_backtest` honors `start` kwarg for historical alias windowing. `msai instruments refresh --provider interactive_brokers` CLI warms the registry via IB qualification.

**Current boundary:**

- Instrument/alias metadata exists, but the backtest catalog can reconstruct non-equities as Equity and hardcode one-minute bars (M03). Registry presence does not certify asset, multiplier, expiry or resampling correctness.
- Strategy config-schema/default extraction is already implemented in the registry; it is not a future task. Schema availability depends on the discovered config class and supported field types.
- Recheck old instrument-cache/live-resolution migration notes against current call sites before carrying them into a plan; the prior deferred list is not a current work inventory.

### Environment Variables

Backend settings reference (container-network examples and placeholders, **not a complete file to copy into every process**):

```dotenv
DATABASE_URL=postgresql+asyncpg://msai:password@postgres:5432/msai
REDIS_URL=redis://redis:6379
DATA_ROOT=/app/data
STRATEGIES_ROOT=/app/strategies
ENVIRONMENT=development|production
MSAI_API_KEY=msai-dev-key               # Alternative to Bearer JWT for dev/CLI/testing
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
CORS_ORIGINS=["http://localhost:3300"]
DATABENTO_API_KEY=your-key
DATABENTO_EQUITIES_DATASET=EQUS.MINI
DATABENTO_FUTURES_DATASET=GLBX.MDP3
DATABENTO_DEFAULT_SCHEMA=ohlcv-1m
IB_GATEWAY_HOST=ib-gateway
IB_GATEWAY_PORT_PAPER=4004              # client-side socat proxy port (gateway binds 4002 internally)
IB_ACCOUNT_ID=verified-account-id
REPORT_SIGNING_SECRET=replace-with-generated-secret
REPORT_TOKEN_TTL_SECONDS=60
BROKER_GATEWAY_SLOTS=ib-gateway
AZURE_KEYVAULT_URI=https://your-vault.vault.azure.net/
AZURE_KV_MI_CLIENT_ID=                  # optional managed-identity client id, distinct from JWT audience
```

`IB_HOST` takes precedence over `IB_GATEWAY_HOST`; `IB_PORT` takes precedence over `IB_GATEWAY_PORT_PAPER`. Live mode uses explicit `IB_PORT=4003` and a matching gateway internal `IB_API_PORT=4001`; paper uses 4004/4002. `IB_GATEWAY_PORT_LIVE` is documentation-only for Settings. Account ID, trading mode, gateway routing and both port layers must agree.

Keep these configuration domains separate:

| Domain | Variables and meaning |
| --- | --- |
| Backend JWT validation | Actual Settings fields are `AZURE_TENANT_ID` and `AZURE_CLIENT_ID`. Production Compose also sets `JWT_TENANT_ID`/`JWT_CLIENT_ID`, but they are not aliases consumed by Settings. |
| API/supervisor process environment | `GATEWAY_CONFIG` describes gateway/account routing. It is read from the process environment, not a Settings field. |
| Gateway container | `TRADING_MODE`, `IB_API_PORT` and credential inputs configure the broker process. Obtain credentials through the authorized secret store; never put their values in this document or reports. |
| CLI client | `MSAI_API_URL` and `MSAI_API_KEY` target API-backed commands; host Docker API is port 8800. |
| Frontend build | `NEXT_PUBLIC_AZURE_CLIENT_ID`, `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_API_URL`. These public values are embedded at build time. Local/E2E bypass configuration is not a production permission policy. |
| Assessment guards | Empty `DATABENTO_API_KEY`, `DAILY_INGEST_ENABLED=false`, `AUTO_HEAL_MAX_SYMBOLS=0` and closed-loopback broker routing are in the retained override. `IB_ALLOW_MOCK_FALLBACK` has no found production-code consumer and must not be treated as a broker-disable control. |

Production report signing requires a nondefault secret of at least 32 characters; token TTL defaults to 60 seconds and is bounded at 300. The Key Vault managed identity is distinct from the Entra API audience. Use the deployed secret-rendering flow and current Compose/settings definitions rather than pasting every listed variable into the root `.env`.

### Revival of archived implementation (if ever needed)

Everything in the archived parallel implementation at the time of deletion is preserved at git tag `codex-final`:

```bash
git checkout codex-final -- <path-inside-codex-version>
```

No active work relies on it.

---

## E2E Configuration

```yaml
interface_type: fullstack
surfaces: [API, CLI, UI]
```

Preserve these explicit fields: Forge's verifier uses them to determine interface coverage; `fullstack` alone defaults to UI + API and can omit the CLI. Its surface-coverage warning applies in feature mode. Verify API first, CLI second, UI third for the capability under test. An unexpected API failure blocks dependent CLI/UI acceptance until diagnosed; a deliberately disabled broker returning unavailable during non-trading research is a recorded scope limitation.

**Server URLs:**

| Surface    | URL                     |
| ---------- | ----------------------- |
| API base   | `http://localhost:8800` |
| UI base    | `http://localhost:3300` |
| PostgreSQL | `localhost:5433`        |
| Redis      | `localhost:6380`        |

All API routes are versioned under `/api/v1/`. Health: `GET /health`.

**Pre-flight (before any E2E run):**

1. Check `http://localhost:8800/health` and the required application dependencies. If unavailable, use the intended startup mode in "Running the stack"; do not replace guarded research mode with unrestricted default startup automatically.
2. Confirm the UI responds at `http://localhost:3300` (only if UI use cases are in scope).
3. For live-trading use cases: establish the exact authorized account, login, gateway route and mode before any start. Historically the live test routes used client port 4003 to internal 4001; paper used 4004 to 4002. A reachable gateway or `gateway_connected:true` is not account identity proof. The October 3 identity conflict must be resolved before relying on the historical environment mapping.

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

**Live-trading safety rails and standing authorization.** The June 5–8 operating plan designated **LVP (`U4705114`) locally pre-PR** and **HVP (`U4715997`) on Azure post-merge**, and chose live test accounts rather than paper for those drills. Retain the existing standing authorization for short deploy→verify→stop cycles on those named test accounts, with minimal exposure; it does **not** authorize the real fund account or unattended trading tests. This historical plan does not prove today's gateway binding or paper-account availability. Verify the actual identity/mode and resolve contradictory evidence before exercising the authorization; a null account ID is not confirmation.

Live use cases must name the account explicitly and must not run from an unattended regression cron. Stop and diagnose unexpected 5xx responses during a live flow before dependent UI testing. Promptly stop the test deployment and reconcile its orders/positions with fresh broker state. `broker_flat:true` currently describes the deployment's local engine-cache scope, not fresh account-wide flatness (M18). Account-wide reconciliation must account for unrelated holdings; do not liquidate them to make a test appear flat. Stopping a Nautilus process alone does not establish that positions were closed.

**Core use-case categories** (for inventory in `tests/e2e/use-cases/`):

- `strategies/` — create, edit, list, hash versioning
- `backtests/` — submit, poll status, fetch report, download artifacts
- `live/` — portfolio create, deploy, start/stop, positions, order events
- `data/` — instrument lookup, catalog browse, bar chart rendering
- `auth/` — login, token refresh, logout, RBAC

The use-case lifecycle is draft → execute → graduate. Use the installed Forge verifier's result vocabulary, including PASS / FAIL_BUG / FAIL_STALE / FAIL_INFRA and invalid-use-case handling; do not create a separate MSAI result schema.

### UI implementation and real-browser acceptance

**Every UI implementation or change must be easy to understand, intuitive to operate, and tested through computer use in a real browser with an end-to-end user journey.** This is a completion requirement, not optional polish. Apply it to the affected workflow at the final candidate; it does not require an unrelated exhaustive regression suite for every small change.

- Define the user's goal and successful outcome before implementation. Use plain labels, visible primary actions and understandable next steps. The operator should not need infrastructure knowledge to research a strategy, interpret results or manage a portfolio. Evaluate usability while performing the task, not just visual appearance.
- Verify the capability API first, CLI second, UI third, preserving Forge's explicit surface configuration above. Then use computer-use browser tooling to perform the actual user actions against the running app and real backend/data. Playwright can automate the journey; API calls alone, static screenshots and rendered-component checks cannot replace browser interaction and observation.
- Cover the affected path from entry and meaningful input/action through progress to its persisted result; reload or revisit where persistence is promised. Check a relevant failure/recovery path and applicable loading, empty, error and stale-data states. Scale the cases to the change while retaining the complete user outcome.
- Keep account/environment identity, units, dates, costs and data freshness understandable. Verify observations follow the selected account view, while account commands independently name and validate their execution target; fleet controls must state fleet scope. Test account switching when the change affects account data or controls.
- Use real API responses and real application state for final E2E acceptance. Mocked responses remain useful for isolated unit/component tests but do not establish that a journey works. A configured development auth bypass is not proof of Entra login or permission enforcement; test the real documented login for authentication acceptance.
- Record the journey, revision, environment, relevant data/account scope, result and evidence. Screenshots support usability evidence; observable actions/results and persisted state establish functionality. Preserve existing arrange/verify rules and live-trading authorization above. If services, identity or authorization prevent the journey, record the blocker/unverified scope and leave UI acceptance incomplete rather than calling it done.

### Playwright Framework

The Playwright framework lives inside `frontend/`, where the application package is located:

- `frontend/playwright.config.ts` — `baseURL` defaults to `http://localhost:3300` (host-exposed Docker port). Override per run with `PLAYWRIGHT_BASE_URL=<url>`.
- `frontend/tests/e2e/specs/` — existing browser specs; author additional cases with `getByTestId` / role-based selectors. Presence of specs or mocked/bypass-auth checks does not establish real Entra/broker acceptance.
- `frontend/tests/e2e/fixtures/` — auth fixture + helpers.
- `frontend/tests/e2e/.auth/` — gitignored storage state (credentials).

Verify-e2e agent artifacts live at the repo root (independent of the Playwright framework):

- `tests/e2e/use-cases/` — markdown use cases (draft before graduation, then checked in under `backtests/`, `strategies/`, `live/`, etc.).
- `tests/e2e/reports/` — verify-e2e agent output (markdown reports, HTML on failure).

Run specs locally:

```bash
cd frontend && pnpm exec playwright test
```

With no `PLAYWRIGHT_BASE_URL`, the current config starts/reuses `pnpm dev --port 3300` and checks `http://localhost:3300`. An explicit `PLAYWRIGHT_BASE_URL` sets browser navigation and disables the managed `webServer`; start the intended target yourself and configure its authentication. The corrected managed-server startup passed the historical [research-foundation harness control](audits/2026-10-03/research-foundation-verification.md); refresh it when changing the harness.

Playwright uses `TEST_API_KEY` for backend request headers and `NEXT_PUBLIC_E2E_AUTH_BYPASS=1` for its managed development server. For independent computer-use/dev-browser journeys, configure `NEXT_PUBLIC_MSAI_API_KEY` to match the backend key or use a real Entra token through the supported login. UI bypass alone does not authenticate backend requests. MSAL storage-state setup is commented out. Frontend CI currently runs lint/build, not these browser specs. See Master Map M14 for the verification boundary.

API-only use cases don't need Playwright — the `verify-e2e` agent hits the REST endpoints directly with curl/httpx.

### Research Enforcement

Follow the installed canonical Forge workflows rather than copying their phase numbers or tool requirements into this file. [New-feature research](../.forge/workflows/new-feature.md) precedes design and addresses current dependencies, official sources and verification implications. [Bug-fix research](../.forge/workflows/fix-bug.md) follows root-cause investigation and reuses relevant repository knowledge; dispatch `research-first` when current external behavior matters. Preserve useful project research under `docs/research/` with dates, sources and explicit unknowns.

---

### Visual Design Preferences

- Optimize for intuitive task completion and clear financial decisions; apply the mandatory [UI/browser acceptance requirements](#ui-implementation-and-real-browser-acceptance) to every UI change.
- Preserve the dark-mode-first Geist/shadcn visual system and accessible contrast.
- Prioritize readable financial tables/charts, explicit account and environment identity, data freshness, consistent units and clear empty/error states.
- Motion is optional and should help explain a state change; respect `prefers-reduced-motion`. Decorative animation or organic shapes are not required for financial controls or dashboards.

## No Bugs Left Behind Policy

The canonical policy is [Forge's No Bugs Left Behind](../.forge/instructions.md#no-bugs-left-behind): fix known reachable defects in the active supported scope before shipping. During an explicitly requested assessment, record findings and evidence without pretending they are repaired or silently expanding into production changes. A finding closes with a verified repair at the final revision, not a new document or a passing unrelated test.

## Ground Your Claims Policy

Apply [Forge's Ground Your Claims](../.forge/instructions.md#ground-your-claims) to MSAI's financial and operational assertions:

- Claims about code → cite the file you actually read (`file.py:42`)
- Claims about behavior → name the tested environment, revision and real workflow, or label the claim unverified
- Claims about alpha/performance → reconcile inputs, costs, return units, account scope and selection/validation boundaries; a rendered tearsheet is not proof of economic correctness
- Uncertain → say "I haven't checked X" instead of guessing fluently

Keep intended capability, inspected code, offline test evidence, actual runtime observations and historical reports distinct. Preserve the operational details in this document while correcting drift; links supplement the mandatory-read context rather than replacing essential instructions.
