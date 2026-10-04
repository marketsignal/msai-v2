# MSAI v2 Master Map

Initial assessment: **October 3, 2026**, source `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. **Operational update: October 4, after the 06:29 UTC deployment failure**: PR102–106 are merged at main `e3ad0951fbeb9c9d3e85228eafc71bc4c8e56909`. Exact-main CI, auth and image build passed; automatic deployment failed during temporary SSH-rule creation with a confirmed Requests import-lock exception in Azure CLI 2.90.0/Python 3.14.6. VM staging and execution were skipped; independent cleanup and unrelated-policy preservation passed. The earlier `f4ede895` automatic deployment passed at 05:42 UTC. The last separate direct VM inspection remains `fa4c8f8`. Current startup repair, actual producer cancellation and scheduled recovery require separate acceptance. [Latest evidence and repair boundary](docs/audits/2026-10-04/azure-cli-startup.md).

**MSAI is a functioning platform with real research workflows, but it is not yet the dependable research-to-trading service you wanted.** The initial audit proved local research/portfolio simulations and Azure browser backtests, while reproducing inconsistent financial results. The first repairs are now merged and installed on Azure: a new equity reference backtest reconciles through independent API/CLI checks and a real-browser submission/report/reload. Broader data, validation, portfolio/account, release, supervisor and storage findings remain open. The priority is still trustworthy research and dependable operation.

This map separates inspected implementation, offline reproductions, observed local/Azure execution, historical claims, and unverified integrations. Three specialist agents audited research/data, live safety, and product/operations; the coordinator ran the platform and reconciled the evidence. The [initial runtime assessment](docs/audits/2026-10-03/runtime-assessment.md) corrected the limits of source-only assessment. The later [Azure installation and acceptance record](docs/audits/2026-10-03/azure-first-installation.md) establishes the bounded repairs below. Application services were updated with explicit approval; no trading order was placed. The companion [Master Plan](MASTER_PLAN.md) tracks the remaining work.

## Latest verified change: automatic Azure deployment and recovery tests

PR102, PR103 and PR104 are merged. Exact merged-main CI/auth/image builds passed, followed by automatic Deploy `37171071535` at `fa4c8f8`. The full installer, smoke checks, public probes and normal temporary-rule cleanup passed. Direct VM inspection confirms all six application services at that revision. Fresh API, installed CLI and real-browser reload/full-report checks preserve the reconciled 166-fill reference. The earlier first-installation CI reported 3,748 passed, 11 skipped and 17 xfailed; that is a historical count, not one inferred from the later green run.

Recovery evidence is mixed. Real active-owner preservation, cleanup-only rerun of an arranged original-attempt rule, and cleanup after deliberate SSH staging failure passed. Cancellation setup first failed with `CLI_RUNTIME_ERROR`; one unchanged retry created the rule, but ordinary cancellation did not stop the producer. Its `always()` job condition preserved execution into the unreachable test target until connection timeout. Cleanup then passed. Scheduled reaping has no fresh execution proof. Release recovery remains **PARTIAL**. [Detailed pipeline and recovery evidence](docs/audits/2026-10-03/azure-normal-pipeline.md).

**Integrated follow-up, October 4:** [PR105](https://github.com/marketsignal/msai-v2/pull/105) merged the reviewed cancellation/timeout/diagnostic repair at `d5e5197` into main `f4ede895`. Independent focused verification passed 52 tests plus workflow/lint/type checks, the real read-only Azure helper regression and both closure reviews. PR checks passed before the authorized merge. [Exact-main CI](https://github.com/marketsignal/msai-v2/actions/runs/37179681442), main image build/auth and [Deploy 37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) passed; deployment cleanup completed at 05:42 UTC. Operational acceptance remains PARTIAL: actual GitHub producer interruption and scheduled reaping remain unverified. M20 stays open. [Candidate and integration evidence](docs/audits/2026-10-03/release-cancellation-candidate.md).

**Worktree consolidation and tooling publication: complete.** All four merged feature worktrees and the merged PR106 publication worktree are archived recoverably; their ignored audit evidence is preserved outside worktrees, and their local and GitHub branches are deleted. Forge 6.4.3 and the assessment/context documents are in main through PR106. Seven guarded local research services use primary source mounts; a real-browser reload retained the existing 100-fill local reference. These tooling/documentation changes do not close application findings. The focused Azure CLI startup repair uses one new isolated worktree. [Cleanup, preservation and runtime evidence](docs/audits/2026-10-03/worktree-consolidation.md).

| Scope | Current evidence and remaining boundary |
| --- | --- |
| Reference equity accounting, M05 | Independently reconciled 166 fills, $1 million opening capital, −$1.21 final change and the first day's −$0.09. API, CLI and real-browser reports agree at −0.000121% return. This closes the reproduced reference-case error; broader portfolio/account and asset accounting stays open. |
| Result meaning and costs, M19 | Opening capital and realized-balance scope are explicit. Unknown legacy P&L/fees remain unavailable; recorded zero fees are distinguishable. Realistic IB costs, slippage and full marked-to-market NAV are not certified. |
| Release controls, M10/M20 | Earlier normal deployments and the named preservation/cleanup cases passed. Exact-revision CI/auth and fleet gates also passed in the latest e3ad095 attempt, which then failed with a confirmed Requests import lock; independent cleanup passed. Focused startup repair is under verification, while actual repaired-producer cancellation and fresh scheduled reaping remain open. |
| Operator workflow, M24 and browser path | The merged candidate repairs CLI settings/diagnostic handling. Installed CLI reads and real Azure browser submission/history/pagination/report/reload passed. Primary main and local source mounts are now reconciled; the local real-browser reference survived handoff. Broader CLI/browser acceptance remains scoped to the named journeys. |
| Broker/supervisor/storage, M21–M23 | Unchanged by this batch. Supervisor remains `65ae682`; account identity, disk placement, restore proof and live acceptance remain open. |

Full evidence, run identifiers, revision/digests and caveats: [Azure first installation](docs/audits/2026-10-03/azure-first-installation.md). Findings below retain the initial audit evidence unless explicitly superseded here; a reference pass is not a blanket finding closure.

## The goal this assessment measures

The operator clarified the product goal on October 3: **a professional-grade hedge fund research and portfolio operations platform, managing a portfolio of strategies in each brokerage account**. The platform must support strategy research and honest validation, portfolio construction and validation, deployment to different accounts, and fresh account/portfolio/strategy performance through API, CLI and UI. Interactive Brokers comes first; additional brokers are a future extension. Infrastructure should become dependable enough for the operator to focus on investment research and portfolio decisions.

The product model is **strategy versions and evidence → portfolio revision → account-specific deployment → reconciled live records**. A portfolio definition is separate from its deployment: different accounts may run different compositions, or reuse a validated revision with different capital and limits. The intended operating rule is one active portfolio per account, with multiple strategies inside that portfolio. Account observations follow the selected view scope. Account commands name and validate their execution target independently; fleet-wide controls retain explicit fleet scope. The account selector does not by itself provide customer authorization or a performance ledger.

This clarification changes the assessment standard. Multi-account routing and portfolio revisions serve the core goal; they should not be removed merely because the first reference test uses one equity and one account. Conversely, model names and a successful EMA backtest do not establish the complete product. The single-equity workflow in the plan is a diagnostic/acceptance starting point, not a reduction of the goal. The platform must assess alpha evidence without guaranteeing alpha or claiming that every possible bias has been eliminated.

The revised [agent context](docs/agent-context.md) retains the mandatory-read deployment procedures, API inventory, vendor coverage and Forge E2E configuration. Its new goal is durable; historical operational assertions are dated and known contradictions are explicit. The original "first real backtest" goal is retired as a completed initial milestone.

A further [goal-alignment assessment](docs/audits/2026-10-03/goal-alignment.md) inspected the account/portfolio paths against this goal. It confirms useful foundations and identifies five concrete gaps now tracked as M25–M29: validation tied to a single deployment, no account-wide active-portfolio invariant, incomplete account scoping, placeholder live performance aggregation, and inconsistent promotion/account semantics. These are source-confirmed findings; no new broker operation or two-account acceptance test was performed.

## Answers to your main questions

| Question | Assessment |
| --- | --- |
| What do we have? | A custom investment research and trading operations platform around NautilusTrader, with broad API/CLI/UI workflows and substantial account/supervisor infrastructure. |
| Does it work? | Yes for the exercised local simulations and the newly reconciled Azure equity API/CLI/browser reference journey. Broader data/validation, portfolio economics and research-to-live acceptance remain unverified. |
| Is it good? | Good component choices and useful safety design, combined with material defects at the boundaries between those components. Strong building blocks do not make every result trustworthy. |
| Is it overengineered? | **Selectively.** Halt, broker reconciliation, durable jobs and strategy isolation address real needs. Multiple authorities for live state, dormant manager code, large orchestration handlers, and sophisticated optimization ahead of reliable validation impose avoidable cost. |
| Is it current? | No. The locked trading engine and several libraries lag upstream. Next.js is behind published security patches; arq is maintenance-only. PostgreSQL 16 remains supported. |
| Is it efficient? | Overall efficiency is unmeasured. Queue separation and streaming are useful, but there are source-level memory/I/O bottlenecks and correctness problems under concurrent writes. No 100-symbol capacity proof exists from this audit. |
| Is it substantially better than alternatives? | Not demonstrated. Custom Azure/Entra/account workflows are a potential advantage. Better returns, reliability, research speed or total ownership cost have not been established against LEAN, thin Nautilus workflows, or VectorBT research. |
| Does it meet the clarified intent? | **Partially.** Research and account/portfolio structures exist, but portfolio reuse/account exclusivity, consistent account-scoped interfaces and reconciled live performance need explicit acceptance alongside data, validation and risk repairs. No reliable alpha strategy is established. |

## Scope and evidence legend

- **Tested:** the named focused checks passed on this revision; this applies only to their tested contracts.
- **Runtime-observed:** an actual service, job or browser path was exercised; its environment and scope are specified. A healthy service or completed job does not certify financial correctness.
- **Reproduced defect:** a deterministic offline probe demonstrated incorrect behavior. It is not automatically evidence of a past production incident.
- **Source-confirmed gap:** the reachable code/configuration lacks or contradicts the intended behavior; the real external journey was not run.
- **Unverified:** requires an environment, credentials, data, workload or broker journey not exercised here.
- **Historical:** prior documentation/conversation evidence, not present-day certification.

Severity expresses consequence: **High** affects research conclusions, trading authority, financial records, data integrity or release safety; **Medium** affects operability, consistency or maintainability. It is not a claim that every finding is currently being triggered.

At the initial audit, local containers were stopped. The follow-up started seven guarded local research services and verified existing data plus new jobs. Azure progressed from `71aa4a9` to the supervised `23db3b8` installation and then the automatic `fa4c8f8` deployment; the older supervisor remains `65ae682`. Account identity remains unresolved: its registry says LVP, the prior context assumed HVP, and account snapshots returned null. No trading order was placed. The latest installer invoked migration successfully with schema head unchanged at `f6a7b8c9d0e1`; existing dependency and broker containers were preserved. The [initial comparison](docs/audits/2026-10-03/runtime-assessment.md), [first installation](docs/audits/2026-10-03/azure-first-installation.md) and [normal pipeline](docs/audits/2026-10-03/azure-normal-pipeline.md) describe distinct snapshots.

## What is actually in this repository

The parallel-version comparison ended in April. The surviving former Claude implementation is now at the root; the older Codex implementation is archived under the `codex-final` tag. The pasted Codex test results and NQ slope-strategy work must not be assumed to describe this root. The active tracked strategies are examples/smokes; no slope strategy was found in the active backend/strategy inventory. See [history decision](docs/decisions/which-version-to-keep.md) and [research audit](docs/audits/2026-10-03/research-data.md).

| Area | Size or surface | Role |
| --- | --- | --- |
| `backend/src/msai/` | 224 Python files; 70,331 physical lines | APIs, CLI, jobs, trading integration, risk, data and operations |
| `frontend/src/` | 137 TS/TSX/CSS files; 24,147 lines; 24 pages | Operator dashboard and workflow controls |
| `strategies/` | 8 Python files; 723 lines | EMA example, trading smokes, shared config and failure demonstration |
| `backend/tests/` | 357 Python files; 106,073 lines | Unit, integration and opt-in E2E tests |
| Frontend tests | 7 Playwright spec files | Selected browser flows, chiefly bypass-auth coverage |
| Database | 48 migration revisions; one structural head | Strategy/research/account/deployment and audit records |
| Deployment | Compose, Azure IaC, image/deploy workflows, backups and watchdog | Single-host operation and recovery tooling |
| Existing docs | 206 Markdown files; 111,018 lines | Plans, decisions, runbooks and historical evidence, with drift |

Counts include comments/blank lines and are not quality scores. The [machine-readable inventory](docs/audits/2026-10-03/inventory.json) records the method and largest files. AST counting found 3,427 backend test functions; this is not a passing-test count or coverage percentage.

## How the system works

```mermaid
flowchart TD
    Operator[You and partners] --> UI[Next.js dashboard]
    Operator --> CLI[Typer CLI]
    UI --> API[FastAPI with Entra authentication]
    CLI --> API
    CLI -. Some operator commands run locally .-> Ingest[Data ingestion]
    API --> PG[(PostgreSQL metadata and audit)]
    API --> Queue[Redis and arq queues]
    Queue --> Ingest
    Vendor[Databento historical data] --> Ingest
    Ingest --> PQ[(Historical Parquet)]
    PQ --> Duck[DuckDB dashboard queries]
    Duck --> API
    PQ --> Catalog[Nautilus catalog conversion]
    Queue --> Research[Research and portfolio workers]
    Research --> Backtest[Backtest worker and Nautilus engine]
    Catalog --> Backtest
    Strategies[Git-managed Python strategies] --> Backtest
    Backtest --> Results[Metrics and QuantStats reports]
    Results --> PG
    Research --> Graduation[Candidate and portfolio graduation]
    Graduation --> PG
    API --> Commands[Live command streams]
    Commands --> Supervisor[Account supervisor and fleet routing]
    PG --> Supervisor
    Supervisor --> Node[Nautilus TradingNode subprocess]
    Strategies --> Node
    Feed[Databento live market data] --> Node
    Node --> IB[IB Gateway execution]
    Node --> Events[Engine events and persisted cache]
    Events --> Projection[Dashboard projection and audit hooks]
    Projection --> PG
    Projection --> WS[WebSocket and live REST views]
    WS --> UI
    Halt[Fleet and account halt state] --> Supervisor
    Halt --> Node
```

This diagram maps intended and inspected connections; it does **not** mark every arrow as working. The catalog conversion, research selection, risk wiring and projection boundaries contain the most consequential findings below.

There are four distinct state stores: PostgreSQL for application records, historical Parquet, a derived Nautilus catalog, and Redis for queues/commands/live cache. Their different purposes are reasonable. Their consistency contracts need work: a successful ingest is not proof of correct catalog instruments; a stored portfolio weight is not proof of live sizing; a submitted order is not proof of a correctly displayed fill.

## Subsystem map and health

| Subsystem and entry points | What exists | Current assessment | Direction |
| --- | --- | --- | --- |
| Strategy registry: `api/strategies.py`, `services/strategy_registry.py`, `strategies/` | Filesystem discovery, metadata, config loading and hash tracking; API/CLI/UI management | Implemented; executable Python is trusted operator code, not a sandbox for untrusted uploads. Example inventory does not establish alpha. | Keep git-managed source and a small approved strategy set. |
| Instrument registry: `services/nautilus/security_master/` | Provider aliases and instrument metadata; live lookup wiring | Substantial registry exists; backtest catalog reconstruction loses important asset semantics. | Make the registry authoritative through the entire backtest path. |
| Historical data: `services/data_ingestion.py`, `services/parquet_store.py`, `services/data_sources/` | Vendor fetches, validation, monthly atomic replacement, deduplication | Reproduced concurrent lost updates; dataset/bar-schema identity is absent from storage key. Atomic replacement alone is insufficient. | Repair storage identity and writer serialization. |
| Analytics queries: `services/market_data_query.py` | DuckDB over Parquet | Appropriate architecture; interval selection does not provide the requested aggregation in the reproduced probe. | Keep DuckDB; fix frequency/data contracts. |
| Backtesting: `workers/backtest_job.py`, `services/nautilus/backtest_runner.py`, `services/nautilus/catalog_builder.py` | Queued Nautilus execution, reports and results | Real engine integration, but futures/crypto identifiers can be converted into Equity instruments and bars into one-minute catalog data. Broad multi-asset correctness fails at this boundary. | Correct metadata and validate one instrument per promised class. |
| Research: `services/research_engine.py` | Parameter sweeps, Optuna and walk-forward windows | Reproduced holdout-based selection and ignored sweep acceptance filters; failed holdout can still produce a selected configuration. | Re-establish a clean train/validation/final-test contract. |
| Portfolio research: `services/portfolio/`, `services/portfolio_backtest/` | Candidate combinations, return aggregation, allocation and optimization | Out-of-sample allocation can be fitted on the same test returns being scored; combined independent curves are not full shared-account execution simulation. | Fix evaluation before adding optimization sophistication. |
| Graduation: `services/graduation.py`, `services/live/portfolio_service.py` | Candidate stages, live revisions and promotions | Empty evidence can advance; one candidate is consumed by one deployment; portfolio promotion requires a paper-format account but stores it only in the description. | Separate reusable validation, composition materialization and explicit account assignment; preserve suitability checks. |
| Live control: `api/live.py`, `live_supervisor/`, `services/nautilus/trading_node_subprocess.py` | Account routing, subprocess ownership, restart/backoff, commands and reconciliation | Real control architecture; focused halt/restart checks pass. Real broker behavior on this revision remains unverified. | Preserve safety controls; remove retired competing authorities. |
| Risk and flattening: `services/nautilus/risk/risk_aware_strategy.py`, `services/live/flatness_service.py` | Halt latch, node-side gate, data-stale/disconnect responses, reduce-only escape and drain protocol | Useful tested mechanisms; monetary/position/loss risk limits are not fully wired into the production node. Empty Redis state can clear a prior halt in a surviving node. | Complete limits and prove state-loss recovery. |
| Live records: `services/nautilus/projection/`, `services/nautilus/audit_hook.py` | Redis events, persistent fills, snapshots and WS hydration | Actual installed-engine topics are dropped; real position payloads can fail parsing; sign/member attribution defects reproduced. Separate audit persistence exists, so this is not a claim that all fills are lost. | Fix engine contracts and reconcile broker, DB, REST and UI. |
| Access and partners: `core/auth.py`, `api/auth.py`, `api/websocket.py` | Real Entra validation, shared API key, displayed roles | 45 focused auth/health/identity tests pass; role enforcement absent and REST/WS visibility disagrees. | Choose and enforce a small partner policy. |
| Dashboard and CLI: `frontend/src/`, `cli.py` | Broad research/backtest/account/live workflow surfaces | Account selection is partial; All positions can collapse to one streamed deployment. Daily live PnL aggregation writes placeholders, and some CLI operations run locally. | Repair scope/freshness and real account/portfolio/strategy accounting; make command destinations explicit. |
| Release and operations: `.github/workflows/`, `infra/`, `scripts/`, Compose | Image builds, Azure delivery, gates, rollback, backups and gateway watchdog | Azure online; latest two deploys blocked by stale NSG rule; supervisor/API incompatibility; data on root disk. Today's backup exists, restore untested. Same-revision CI and `stopping` gates also need repair. | Restore dependable deployment/storage and rehearse recovery. |

Paths in this table are relative to `backend/src/msai/` unless another root is shown. Detailed file/line evidence and reproduction procedures are in the three audit reports linked below.

## Fit against your requirements

| Your requirement | Fit today |
| --- | --- |
| Azure plus Interactive Brokers | Azure owner sign-in, browser backtest/report, container operation and backups were observed. Existing IB connection health responds, but account attribution is unresolved and current order execution remains unverified. |
| Python strategy authoring | Good fit. Git-only source is deliberate; UI/CLI manage registration/configuration, not arbitrary strategy-file uploads. |
| Minute data for stocks, indexes, futures, options and some crypto | Partial. Provider/registry concepts exist, but correct research representation is not established across these classes. Options chains, lifecycle/expiry and crypto paths need explicit acceptance evidence. |
| Trading every 5–10 minutes or daily | Possible as strategy logic on minute input, but the data/catalog/query interval contract is incomplete. Do not equate an interval parameter with verified resampling or daily execution. |
| Approximately 100 symbols and selected options | No representative load/capacity test run. Options cannot be sized from the old storage estimates. |
| Research and validate strategies, then construct and validate portfolios | These stages have code and interfaces, but validity, evidence binding, cost/sizing semantics and selection defects break assurance between stages. A one-member quick simulation is not full shared-capital portfolio validation. |
| Deploy a portfolio of strategies per account; reuse a revision across accounts | Revision/account schema and routing fit the model, but candidate evidence is consumed by one deployment and a second portfolio on the same account is not prohibited by the inspected guards. M25/M26 remain open; a two-account live journey is unverified. |
| Switch accounts consistently through API, CLI and UI | Selector/filter plumbing exists, but several API/CLI reads are global/gateway-bound and the All positions view can drop non-streamed deployments. Historical live PnL aggregation is a placeholder. M27/M28 require implementation and acceptance. |
| IB first, additional brokers later | IB is the current execution integration. Treat future broker support as an extension requirement; declarations or metadata are not functioning adapters. |
| Strong protection against overfitting | **Not met by the inspected selection paths.** Walk-forward terminology and many tests do not compensate for test-period selection/leakage. |
| Pyfolio-like tearsheets | QuantStats reports exist. Their trustworthiness depends on corrected returns, drawdown, fees and input data. |
| Access for you and a few partners via Entra | Authentication fits; read-only/operator permissions and individual CLI identity do not yet fit reliably. |
| Positions, trades, P&L and system health | Broad UI/API coverage; correctness and completeness gaps remain in financial attribution, daily P&L and multi-process metrics. |
| API first, CLI second, UI third | Largely reflected in interfaces; local-service CLI commands and missing complete journey tests remain exceptions. |
| AI/LLM features deferred | Sensible to retain. No reason to add them before research and accounting are trustworthy. |

## Findings that should drive the next phase

The identifiers below are stable references for the Master Plan. They retain the original findings; the latest verified change and explicit repaired-scope labels above supersede only their named acceptance cases. Broader unresolved boundaries remain open.

The historical [research-foundation verification](docs/audits/2026-10-03/research-foundation-verification.md) retains the repaired accounting/presentation/harness evidence and remaining limits. Reversed-date input still fails late as `ENGINE_CRASH` rather than being rejected before queuing; include that usability gap in the next date/input acceptance work. Intermittent browser history/trades transport failures also remain unresolved despite working Retry/reload and corrected error presentation.

| ID | Priority | Finding and consequence | Evidence |
| --- | --- | --- | --- |
| M01 | High | Research selects using holdout results; portfolio OOS allocation can use future test-window information. Reported OOS performance cannot serve as untouched confirmation. | [Research audit](docs/audits/2026-10-03/research-data.md); `services/research_engine.py:661`, `:1279`; `services/portfolio/orchestration.py:1808`; `services/portfolio_backtest/optimizer.py:275` |
| M02 | High | Graduation can advance with empty evidence; sweep acceptance filters/failure handling permit unsuitable selected configurations. | [Research audit](docs/audits/2026-10-03/research-data.md), including offline counterexamples |
| M03 | High | Backtest catalog recreates non-equities as Equity with multiplier 1 and hardcodes minute bars. Futures/options economics and frequency can be wrong. | [Research audit](docs/audits/2026-10-03/research-data.md); `services/nautilus/instruments.py:23`; `services/nautilus/catalog_builder.py:48` |
| M04 | High | Dataset/schema share a storage namespace; concurrent monthly writes lose rows. | [Research audit](docs/audits/2026-10-03/research-data.md); `services/parquet_store.py:264`; deterministic two-writer reproduction |
| M05 | High | Headline total-return units are 100× wrong for their consumer, and charts drop first-day PnL in actual local and Azure jobs. Additional drawdown/account-snapshot edge cases fail offline. | [Runtime accounting evidence](docs/audits/2026-10-03/runtime-assessment.md); [research audit](docs/audits/2026-10-03/research-data.md) |
| M06 | High | Position/exposure/daily-loss limit code is not connected to live execution; live revision weights are not a sizing policy. | [Live audit](docs/audits/2026-10-03/live-safety.md); `services/nautilus/risk/risk_aware_strategy.py:516`; `services/nautilus/trading_node_subprocess.py:2892` |
| M07 | High | Actual engine order/account topics are discarded by projection; actual position payloads can fail parsing, lose short sign or overwrite another member. | [Live audit](docs/audits/2026-10-03/live-safety.md); installed Nautilus 1.223.0 event probes |
| M08 | High | Redis recreation loses halt/cache/stream state. A still-running node can interpret missing halt keys as permission after reconnect. | [Live audit](docs/audits/2026-10-03/live-safety.md); `services/nautilus/trading_node_subprocess.py:302`; `docker-compose.prod.yml:80` |
| M09 | High for shared use | Viewer/operator labels are not enforced; REST/WS and shared-key identities disagree. | [Product audit PO-01 and PO-02](docs/audits/2026-10-03/product-operations.md) |
| M10 | Repaired in tested release scope | Initial same-revision CI and stopping-deployment gate defects were repaired in PR103. Automatic `fa4c8f8` deployment exercised same-SHA CI/auth and complete fleet readiness. This snapshot is not a maintenance lock or account-flatness proof. | [Original PO-04/PO-08](docs/audits/2026-10-03/product-operations.md), [runtime acceptance](docs/audits/2026-10-03/azure-normal-pipeline.md) |
| M11 | High at incompatible boundaries | Image rollback leaves an already-upgraded schema/data contract in place. Destructive historical migrations make arbitrary rollback unsafe. | [Product audit PO-06](docs/audits/2026-10-03/product-operations.md) |
| M12 | Maintenance priority | Next.js 15.5.12 predates published 15.5.27 security fixes. Reachability varies by advisory; exploitation was not demonstrated. | [Ecosystem audit](docs/audits/2026-10-03/ecosystem-and-baseline.md), [September release](https://nextjs.org/blog/september-2026-security-release) |
| M13 | Medium, with financial-record implications | Daily P&L label, multi-member hash attribution, partial-fill P&L update, cold member-position reads and legacy active-count authority are inconsistent. | [Product audit PO-03](docs/audits/2026-10-03/product-operations.md), [live audit](docs/audits/2026-10-03/live-safety.md) |
| M14 | Medium | Browser CI, multi-process observability and documentation do not provide current full-workflow assurance. | [Product audit PO-05 and PO-07](docs/audits/2026-10-03/product-operations.md) |
| M15 | High | An advertised inclusive backtest end date becomes midnight at the start of that date in installed Nautilus config, omitting normal final-day intraday bars. | [Research audit RD-06](docs/audits/2026-10-03/research-data.md); configuration decoding reproduced, full execution not run |
| M16 | High for reproducibility | Coverage can miss internal gaps; recorded source/data hashes and reused Optuna studies do not bind an immutable executed experiment. | [Research audit RD-09](docs/audits/2026-10-03/research-data.md); source-confirmed limitations |
| M17 | Medium | Research reserves multiple compute slots while trials run serially; cancellation is checked after the experiment. Blocking I/O and full-history reads add avoidable work. | [Research audit RD-10](docs/audits/2026-10-03/research-data.md); runtime-scale impact unmeasured |
| M18 | High for recovery decisions | `broker_flat` checks deployment positions in the local engine cache, not a fresh account-wide broker query. Position views exclude failed/stopping deployments that may retain exposure. | [Live audit operational boundaries](docs/audits/2026-10-03/live-safety.md); source-confirmed scope, actual residual exposure unverified |
| M19 | Economic-validation gap | Backtests hardcode one million USD per venue and do not expose/pin realistic commission, slippage, spread or latency assumptions in the inspected builder. Realistic net returns are unverified; this is not proof all costs are zero. | [Research audit module map and priorities](docs/audits/2026-10-03/research-data.md) |
| M20 | High operational, partially repaired | Stale orphan removed; normal deployment and several recovery cases passed at earlier revisions. PR105 integrates cancellation/timeout controls. The later e3ad095 deployment identified a Requests import deadlock; a focused startup repair is under verification. Earlier generic crashes remain unattributed, and actual repaired-runner interruption and fresh scheduled recovery remain unverified. | [Previous real failures](docs/audits/2026-10-03/azure-normal-pipeline.md), [cancellation repair](docs/audits/2026-10-03/release-cancellation-candidate.md), [latest startup failure](docs/audits/2026-10-04/azure-cli-startup.md) |
| M21 | High live-start compatibility | Running Azure supervisor cannot resolve canonical strategy paths stored by the newer API. Actual resolver raises FileNotFoundError; no live start attempted. | [Runtime assessment](docs/audits/2026-10-03/runtime-assessment.md) |
| M22 | High operational | Application/database volumes are on the 83%-used root filesystem; intended 128 GB data disk is attached but unmounted. | [Runtime assessment](docs/audits/2026-10-03/runtime-assessment.md) |
| M23 | High before trading | Historically documented HVP, registered LVP and null broker-reported account identity do not establish a safe current account binding. System health reports version/commit unknown. | [Runtime assessment](docs/audits/2026-10-03/runtime-assessment.md); documentation now qualified, operational identity still unresolved |
| M24 | High diagnostic confidentiality | Root-directory CLI startup fails on Compose-only environment fields and validation errors echo sensitive inputs. Backend-directory CLI invocation succeeds. | [Runtime assessment](docs/audits/2026-10-03/runtime-assessment.md); saved evidence excludes credential values |
| M25 | Medium workflow, required for multi-account acceptance | Graduation candidate has one deployment link and becomes ineligible after binding. Reusing the same approved strategy/composition across accounts requires new eligible candidate records, coupling validation to deployment lifecycle. | [Goal alignment GA-01](docs/audits/2026-10-03/goal-alignment.md#ga-01--p2-a-validation-candidate-is-consumed-by-one-deployment); `api/live.py:941`, `:2069`; source-confirmed |
| M26 | High for account assignment | One active portfolio per account is not enforced: uniqueness/collision guards protect a deployment, while gateway startup serialization allows a different deployment after the first is running. | [Goal alignment GA-02](docs/audits/2026-10-03/goal-alignment.md#ga-02--p1-one-active-portfolio-per-account-is-not-enforced); `models/live_node_process.py:169`; `live_supervisor/fleet_router.py:1875`; no conflicting live starts attempted |
| M27 | High for account visibility | Account selection is incomplete across API/CLI/UI. All positions can replace the fleet snapshot with one deployment's WebSocket list, hiding other accounts despite a successful REST response. | [Goal alignment GA-03](docs/audits/2026-10-03/goal-alignment.md#ga-03--p1-account-selection-is-only-partially-propagated-and-all-can-hide-other-deployments); `frontend/src/app/live-trading/page.tsx:203`, `:243`; source-confirmed |
| M28 | High financial-record gap | Daily live performance aggregation writes zero PnL/win/loss placeholders and only updates order-row counts. Gateway snapshots do not provide a persisted account/portfolio/strategy performance ledger. | [Goal alignment GA-04](docs/audits/2026-10-03/goal-alignment.md#ga-04--p1-the-live-performance-history-is-a-skeleton-not-an-accounting-ledger); `workers/pnl_aggregation.py:45`, `:83`; source-confirmed, no broker accounting reconciliation |
| M29 | Medium workflow consistency | Portfolio promotion requires a `DU` paper-format target although composition is reusable and the supplied account is stored only in its description. Current test-account availability/binding does not justify assuming this path is usable. | [Goal alignment GA-05](docs/audits/2026-10-03/goal-alignment.md#ga-05--p2-portfolio-promotion-has-an-obsolete-paper-only-target-contract); `api/portfolio.py:617`; `services/live/portfolio_service.py:340`; no restriction changed |

The subsystem reports contain narrower secondary findings and recommended fixes. They remain part of the assessment; this table prioritizes rather than erases them.

**Runtime strengthening of M05/M19:** 100 actual simulated fills form 50 round trips and -$0.53 gross PnL. The headline return has a 100× percentage/ratio mismatch, while the chart/portfolio omits the first day's -$0.04. Saved fill PnL and commissions are zero placeholders. This is now real worker/API evidence, not only an offline counterexample. [Reconciliation](docs/audits/2026-10-03/runtime-journey.md#observed-local-result-and-accounting-reconciliation).

## What to keep and what to simplify

**Keep the basic stack.** FastAPI, PostgreSQL, Parquet, DuckDB and Nautilus match the workload. Separate ingest/research/backtest queues prevent long work from blocking interactive requests. A live supervisor, node-side halt checks, restart reconciliation, immutable portfolio revisions and fill deduplication address concrete failure modes. Replacing these wholesale would discard useful work while leaving the financial-validity problems unresolved.

**Keep the account/portfolio model.** The clarified goal requires multiple account identities, reusable portfolio definitions, account-specific deployments, routing and attributed records. Evaluate whether each mechanism correctly serves that model before calling it overengineering. Remove conflicting authorities and unsupported generality, not the user's requested account management capability.

**Simplify conflicting implementations.** The old `TradingNodeManager` still supplies an active count even though the active portfolio path uses the fleet router. Live state is represented in several places without fully consistent identity and serialization. Large handlers (`fleet_router.py`, 4,906 lines; `api/live.py`, 4,087; `cli.py`, 3,529) combine too many decisions. Extract small stable boundaries while fixing named defects. Do not start a generic framework rewrite.

**Pause new sophistication until the fundamentals work.** Optimizer breadth, additional account topologies, more asset classes, new databases, Kubernetes, a visual strategy builder and AI features would increase the support surface. The platform already has far more machinery than demonstrated strategy value. A two-VM split may eventually protect trading from research load, but should follow measured contention and a clear operating requirement.

**Treat trusted Python execution honestly.** A subprocess isolates process failures; it is not a security sandbox. The current git-managed operator-authoring model is reasonable. Partner uploads or arbitrary generated code would change that trust boundary and require separate design.

## Is it advantageous compared with what exists elsewhere

| Alternative | What it already provides | MSAI judgment |
| --- | --- | --- |
| QuantConnect / LEAN | Python/C# research, backtests and IB live trading; local/cloud workflows | Strong comparison candidate. MSAI's custom ownership and account/UI integration may matter, but superiority is unproven. [LEAN](https://www.quantconnect.com/docs/v2/lean-engine/getting-started), [IB CLI](https://www.quantconnect.com/docs/v2/lean-cli/live-trading/brokerages/interactive-brokers) |
| Thin Nautilus plus notebooks/CLI | Same engine family for simulation and live execution | Likely lower platform maintenance for one researcher, by inference. Gives up much of the custom product surface. [Nautilus documentation](https://nautilustrader.io/docs/latest/) |
| VectorBT for research | Optimized portfolio simulations and performance analysis | Useful research-speed benchmark; does not by itself establish a replacement for MSAI's account/execution/recovery workflow. [Portfolio API](https://vectorbt.dev/api/portfolio/base/) |

The justified advantage to pursue is **a trustworthy workflow tailored to your team**, with control over data, research evidence and execution. There is no evidence that a custom dashboard or more code creates an investment edge. Compare one identical strategy, data interval, fees and decisions across systems before considering migration. Current pricing, licensing entitlements and total ownership costs were not evaluated.

## Currency and good practice

The source stack is recognizable and appropriate; the dependency lifecycle needs attention. Nautilus is locked to 1.223.0 while upstream has 1.231.0 and a v2 release-candidate transition. FastAPI is 0.133.1 versus upstream 0.142.2; Databento is 0.71.0 versus 0.87.0. Next.js needs security maintenance. arq's maintenance-only status warrants an ownership plan, not an immediate queue rewrite. PostgreSQL 16 is supported. DuckDB's official support pages conflict and need reconciliation before choosing its upgrade target. Sources and precise limits are in the [ecosystem audit](docs/audits/2026-10-03/ecosystem-and-baseline.md).

Good practices already present include lockfiles, typed schemas, explicit auth validation, asynchronous jobs, revision/hash metadata, atomic file replacement, non-root backend containers, managed-identity secret access, health endpoints and backups. Practices needing correction include validation leakage, economic identity loss, unenforced limits, inconsistent authorization, homegrown event decoding, release gates detached from tests, and image-only rollback across schema changes.

“Latest” is not the same as “best for this application.” Upgrade security fixes promptly, then upgrade engine/data dependencies behind compatibility and parity checks. Do not migrate to Nautilus v2 RC merely to get the newest version.

## What was verified

| Check | Result and interpretation |
| --- | --- |
| Research/data focused tests | **89 passed**, with seven warnings. Separate deterministic probes exposed defects outside those assertions. |
| Live safety/projection focused tests | **79 passed**. Real installed-engine event/position probes exposed contract defects despite green tests. |
| Auth/health/identity focused tests | **45 passed**. Token validation is tested; partner authorization is not thereby proved. |
| Backend lint | Passed. |
| Backend strict typing | Failed: four missing-stub/import errors in two files. This is a local check failure, not proof of an application crash. |
| Frontend lint/build | Could not execute successfully: `eslint` and `next` absent; `node_modules` empty. Dependency setup is incomplete, not a reproduced frontend code failure. |
| Migration graph | 48 revisions, one head, no missing parents. No migration/restore executed. |
| Local runtime | Seven services started and healthy; current source mounted; broker/vendor ingestion guarded off. Actual equity backtest, two research trials, discovery candidate, quick portfolio simulation and two full tearsheets succeeded. |
| CLI | Retrieved the actual completed backtest from `backend/`; root-directory settings failure recorded separately. |
| Azure runtime | Public health/readiness/UI 200; actual container versions, disk mounts, backup Blob metadata and stopped deployment records inspected. Current GitHub CI/auth/build all passed; latest two deploys failed before VM update. |
| Real browser access | Owner Entra login succeeded using `pablo@marketsignal.ai`. Actual Azure browser submission completed in 2.50 seconds, with 166 simulated fills, native results and a rendered full QuantStats report. Local dashboard partially works, but history/trades fetch failures remain unresolved in the available browser. |
| Complete acceptance | Full research→live journey, current order execution, new market-data ingestion, recovery/restore, partner permissions and scale remain unverified. See runtime report for the bounded Azure browser simulation result. |

Total focused cases: **213 passed** across the three disjoint selections. This is not the full suite. Exact commands, scope and counterexamples are preserved in the audit reports. No full regression, benchmark, penetration test or production certification was attempted.

## Documentation to trust and documentation to reconcile

Use this map as an assessment bound to the stated revision, then follow source evidence. `docs/agent-context.md` was revised on October 3 to express the full goal, preserve operational knowledge and Forge's testing configuration, correct obsolete inventory/deferred claims, and distinguish historical accounts/vendor checks from current evidence. `docs/architecture/platform-overview.md` still mixes older capability/stub/Phase 2 claims with newer implementation. The old design's storage/pricing estimates and historical “complete/no findings” language must not drive decisions.

Further documentation updates should follow verified capability and repair evidence. Preserve old plans as history with explicit status; keep project intent in agent context, assessment evidence here and its linked reports, and proposed work in the Master Plan. Do not mark a finding closed because its description was improved.

## Evidence and next phase

- [Research, data and analytics audit](docs/audits/2026-10-03/research-data.md)
- [Live trading and safety audit](docs/audits/2026-10-03/live-safety.md)
- [Product, access and operations audit](docs/audits/2026-10-03/product-operations.md)
- [Current ecosystem and runtime baseline](docs/audits/2026-10-03/ecosystem-and-baseline.md)
- [Measured source inventory](docs/audits/2026-10-03/inventory.json)
- [Actual runtime assessment and local/Azure comparison](docs/audits/2026-10-03/runtime-assessment.md)
- [Account/portfolio goal alignment and two-account acceptance](docs/audits/2026-10-03/goal-alignment.md)
- [Actual local job requests and results](docs/audits/2026-10-03/local-runtime-evidence.json)
- [Proposed Master Plan](MASTER_PLAN.md)

The first milestone should be **one small, correctly measured, reproducible research-to-trading workflow**. The plan orders repairs around that result. It does not assume that completing infrastructure work will produce a profitable strategy.
