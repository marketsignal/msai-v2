# MSAI v2 Master Plan

**Implementation roadmap reconciled after the October 4 research release and subsequent cleanup.** Derived from the [Master Map](MASTER_MAP.md), initially assessed at `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. PR102–108 are merged; research repair PR108 is installed as `b48d0ad88c4ebcff498d2c14dc6fb834bea9c32a`. Exact-main CI/auth/build, normal Deploy 37238509145/cleanup and actual Azure API→CLI→real-browser research/Discovery journeys passed. [Released scope and retained evidence](MASTER_MAP.md#released-research-selection-and-discovery). Earlier actual pre-staging cancellation and genuine scheduled recovery passed at c1dd1c1; **M20 remains PASS for that bounded scope**, without implying a new cancellation test at b48d0ad. [Recovery evidence and limits](docs/audits/2026-10-04/release-acceptance.md). This plan records priorities and evidence, not blanket authorization to trade/deploy.

The objective is a professional-grade research and portfolio operations platform: validate strategies and portfolios, deploy a portfolio of strategies into each account, and monitor multiple accounts consistently through API, CLI and UI. Prove this first with Interactive Brokers; preserve a clear boundary for future brokers. The single-equity reference path below is a first acceptance case, not the product goal. Keep the current stack unless measured evidence establishes that maintaining it costs more than a suitable alternative. **Runtime follow-up:** local backtest → research → candidate → portfolio/report now executes; Azure owner sign-in → browser backtest → fills/results → full report also works. The [runtime assessment](docs/audits/2026-10-03/runtime-assessment.md) adds concrete operational faults and financial reconciliation failures in both environments. Restore dependable operation before treating infrastructure as finished.

## Confirmed product direction

- Separate validated strategy versions, portfolio revisions and account-specific deployments. Different accounts may run different portfolios or the same validated composition with different capital/limits.
- Make one active portfolio per account the operating rule, with multiple strategies inside it. Define controlled replacement and residual-position handling before enforcing that rule in code.
- Provide consistent account switching and financial meanings across API, CLI and UI, including account/portfolio/strategy attribution and freshness.
- Make leakage/bias controls, independent validation, explicit costs and reproducibility acceptance requirements. Successful execution and attractive reported returns do not establish alpha.
- Keep account management within the operator/partner product. Customer self-service, separate customer tenants and non-IB adapters are not implied implementation commitments.

The context revision preserves deployment procedures, endpoint inventory, dated vendor knowledge, explicit Forge E2E surfaces and existing bounded live-test authorization. It changes documentation only; no application finding is closed by it.

## Delivery milestones

The first usable release should let the operator iterate on strategies with trustworthy results and little infrastructure work. Deliver the following milestones in order, while preparing independent work in parallel. Each includes API, CLI and intuitive UI acceptance; browser work is part of each milestone, not a final cosmetic phase.

| Milestone | Operator outcome | Required proof | Current status |
| --- | --- | --- | --- |
| **1. Reliable strategy research** | Author/version a Python strategy, run backtests and independent validation, compare experiments, inspect trades and reports, and reproduce the result. | Reconciled capital, fills, costs, P&L and returns; correct data/date/asset semantics; immutable experiment inputs; selection independent of final holdout; consistent API/CLI/browser results and useful failure/retry behavior. Prove equities first, then actual futures before claiming futures support. | **In progress.** Reference accounting is verified; PR108 training-only selection and exploratory Discovery passed final local and actual Azure API/CLI/browser acceptance at b48d0ad. Data/date/session correctness, realistic costs, immutable inputs and independent final-validation/graduation gates remain open. |
| **2. Reliable portfolio operations** | Construct and validate portfolios, assign one active portfolio to each IB account, and monitor multiple accounts independently. | Two accounts and multiple strategies; reusable validation evidence; account-specific sizing/limits; atomic assignment/replacement; correct view scope and explicit execution targets; reconciled account/portfolio/strategy performance. Offline integration precedes the bounded authorized broker journey. | Planned; depends on research evidence plus live/account repairs. |
| **3. Dependable daily operation** | Operate the supported workload with clear health, alerts, permissions and recovery, without routine infrastructure intervention. | Measured capacity, supported release/rollback, tested backup restoration, restart/disconnect/state-loss recovery, partner permissions and an observation period whose duration, workload and acceptable intervention are defined before it starts. Record incidents and outcomes; a short smoke test cannot establish this milestone. | Planned; operational repairs begin in week 1 and continue across milestones. |

Milestone 1 enables research iteration for its explicitly verified markets and workflows. Portfolio execution and live-capital readiness retain their own gates. None of these milestones certifies strategy alpha.

### Current handoff after automatic deployment and recovery testing

**Earlier verified baseline:** PR102/103/104 merged; exact-main checks and Deploy `37171071535` passed at `fa4c8f8`. Real preservation, cleanup-only rerun and SSH-failure cleanup passed, while cancellation did not interrupt its producer. Preserve that [baseline evidence and limits](docs/audits/2026-10-03/azure-normal-pipeline.md). Current installed revision and successful actual cancellation are recorded in [the new acceptance record](docs/audits/2026-10-04/release-acceptance.md); the [first installation](docs/audits/2026-10-03/azure-first-installation.md) retains the real browser submission.

**Bounded release acceptance — Verified at c1dd1c1.** PR107 startup repair passed exact-main CI/auth/build, normal Linux deployment, installed API/CLI/browser preservation and actual ordinary pre-staging cancellation/cleanup. Genuine scheduled run37218539817 succeeded at16:55:34UTC; bounded real logs and independent Azure inventory confirm four unrelated policies preserved across fourteen fields. M20 is closed for this scope. Preserve original failed/PARTIAL records, exact-candidate certificates and the separate already-running-installer recovery limitation. [Current evidence](docs/audits/2026-10-04/release-acceptance.md).

**Earlier repair integration:** [PR105](https://github.com/marketsignal/msai-v2/pull/105) integrated reviewed cancellation controls as `f4ede895`; its focused checks and [normal deployment](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) passed. That predates the later `e3ad095` import failure and PR107 startup repair. [Historical candidate/integration evidence](docs/audits/2026-10-03/release-cancellation-candidate.md) remains separate from the current acceptance record above.

**Merged-checkout cleanup: complete before this documentation task.** All PR107/108 commits and the unused Claude checkout were contained in main and clean. Three extra local branches/checkouts and both remaining GitHub branches were removed, leaving one main checkout, only main locally/remotely and zero open PRs at b48d0ad. Ignored evidence/settings were hash-verified in private archives. Seven guarded local services use primary sources; API/CLI/real-browser reload retained the identical 100-fill reference and PostgreSQL/Redis/data were preserved. A new bounded task branch is expected during active work. [Release and retention details](MASTER_MAP.md#released-research-selection-and-discovery); [earlier consolidation history](docs/audits/2026-10-03/worktree-consolidation.md).

**Completed research selection batch:** PR108 now selects eligible training evidence before diagnostics in grid/halving/Optuna, persists the latest training window and labels automatic/manual exploratory Discovery. Final reviews, independent verification and real local/Azure API→CLI→browser acceptance passed. [Released scope and remaining boundaries](MASTER_MAP.md#released-research-selection-and-discovery). The [earlier candidate record](docs/audits/2026-10-04/research-selection-acceptance.md) retains initial failures; its pending-certification wording is historical. Only the research sweep/walk-forward and exploratory Discovery portion of M01/M02 is repaired; portfolio OOS allocation and final-validation/graduation remain open.

**Next research dependency:** the existing Milestone 1 sequence now proceeds to the smallest independently reconciled equity data/window/cost case: explicit supported input assumptions, correct requested-session bounds and missing-input refusal, then immutable source/helper/config/data/engine identity and independent final validation. Owners remain research/data and research validation, with matching API/CLI/intuitive browser flows. Correct actual futures metadata/economics before extending the supported market boundary. Reuse existing records, storage and workers; this does not authorize a new orchestration platform or a broad storage migration.

**Active first slice: inclusive backtest dates/M15 — In progress.** Independent reproduction confirmed final-day omission at b48d0ad; the [minimal repair plan](docs/plans/equity-backtest-window.md) passed review after two material acceptance gaps were corrected. Backend and interface producers implemented the full UTC date bounds at both cutoffs, reversed-date refusal, readable validation and optional native bar counts. Focused checks and frontend lint/build passed; real local API/CLI and sweep/walk-forward/Discovery journeys match the independent reference counts and preserve saved history. Preliminary native computer-use acceptance passed the actual form error/correction, 502-bar/32-fill result, full report, reload/history reopening and legacy missing-count journey. The original locked-Mac PARTIAL record is retained. [Current repair evidence and limits](docs/solutions/backtesting/inclusive-calendar-date-window.md).

**Candidate-stage next actions, coordinator/interface verification:** preserve the [graduated real journeys](tests/e2e/use-cases/backtests/inclusive-date-window.md) and run their observed browser regression; complete simplification and fresh final reviews/application/E2E gates on one frozen candidate. Publication, integration, applicable exact-main checks and real Azure acceptance must follow before closing M15. Azure remains on the previously verified b48d0ad release; this local candidate has not been integrated or deployed. Cost models, explicit completeness/input assumptions, immutable experiments and independent final validation follow as separate Milestone 1 slices. Forge's local receipts track final certification without rewriting this candidate-stage snapshot.

Broker identity, supervisor compatibility, account isolation and performance reconciliation remain prerequisites for portfolio/live work. No broker order or portfolio deployment was included in this installation.

## Next month: October 3–November 2, 2026

These are delivery targets, not a promise to close all 29 findings in a month. The primary target is **Milestone 1**. Portfolio and operational acceptance advance as their dependencies pass; missed gates move dependent work rather than weakening acceptance. Rebaseline dates after the first repair batch using observed effort and blockers. The numbered implementation phases below remain the detailed dependency and completion criteria.

| Target window | Concrete work and ownership | Reviewable result / gate |
| --- | --- | --- |
| **Week 1 · Oct 3–9** | Main agent defines reference cases and settles decisions needed for the first batch. Operations repairs release/verification failures, verifies environment identity and starts restore/storage planning. Research/accounting reproduces and repairs recorded return, first-day P&L and fill/cost failures. Product/interface work fixes CLI diagnostics and browser history/trades/startup failures. | A repeatable guarded research start; a hand-reconciled equity case; functioning API→CLI→real-browser backtest/report path; named remaining blockers. Deployment/storage mutations follow their applicable authorization and backup procedures. Detailed phases 0–1 begin here. |
| **Week 2 · Oct 10–16** | Research/data owns data identity, completeness, dates, intervals and experiment provenance; research validation owns selection/holdout boundaries. Product/interface work completes experiment progress, comparison, report and failure/recovery journeys alongside those contracts. Operations continues release/restore checks. | Execute the Milestone 1 research journey below for the supported equity scope. Close it only when relevant phases 0–3 criteria pass; otherwise carry the named blockers forward. Document unsupported markets/intervals plainly. |
| **Week 3 · Oct 17–23** | Prove the actual futures reference case, including multiplier, expiry, bars and costs. Portfolio/live work implements validated composition reuse, capital allocation, account assignment/replacement and financial records. Product/interface work makes account views, targets and attribution clear. | Extend research acceptance to futures only with economic proof. Produce a controlled two-account/multi-strategy integration case, including conflicting assignment rejection and account-isolated stop behavior. Broker testing waits for the relevant phase 4 risk/identity gates. |
| **Week 4 · Oct 24–30** | Integrate portfolio operation across API/CLI/browser. Access/operations verifies permissions, recovery, release/restore behavior and measured capacity. Run the smallest broker acceptance within verified standing authorization when its prerequisites pass; begin sustained observation of the supported candidate. | Target Milestone 2 acceptance and gather Milestone 3 evidence. Record exactly which environment, accounts, assets and workload passed, and which external checks remain blocked/unverified. Full journey criteria are in phases 4–6. |
| **Closeout · Oct 31–Nov 2** | Main agent reconciles final-revision evidence, remaining findings, operator friction and observed effort. Repair direct regressions; move unfinished dependent work into the next window. | Updated milestone status, evidence links, supported-use boundary and next priorities. Mark Milestone 3 achieved only if its observation/recovery/capacity evidence is sufficient; do not infer it from the calendar. |

### First acceptance journey: strategy research

1. Register/edit a versioned Python strategy and select a known dataset, period, capital and cost assumptions through supported interfaces.
2. Submit a backtest, follow its progress and inspect fills, net P&L, return series and the report. Reconcile them against an independent expected result, including first-day activity and the last requested session.
3. Run selection/validation with fixed boundaries, then compare strategy versions with visible evidence and limitations. Demonstrate that final holdout results cannot influence selection and that failed/missing evidence prevents advancement.
4. Reproduce the recorded experiment through API and CLI; complete the user-facing journey through computer use in the real browser against the actual backend/data. Confirm identifiers and financial meanings agree, and results persist after reload.
5. Exercise a meaningful failure and recovery, such as unavailable data or a failed job, and verify the user can understand what happened and take the supported next action. Preserve diagnostics without exposing secrets.

Keep this acceptance narrow enough to diagnose failures, then repeat it for correctly modeled futures. Portfolio/account journeys extend it in Milestone 2. Follow the [mandatory UI and browser acceptance requirements](docs/agent-context.md#ui-implementation-and-real-browser-acceptance) for every UI change throughout implementation.

### Progress tracking

This file owns the schedule, priorities and milestone status. The [Master Map](MASTER_MAP.md) owns the assessment and finding evidence; [agent context](docs/agent-context.md#current-implementation-focus) owns durable product/operating rules and the pointer to current work. Keep deployment procedures, endpoint inventory and vendor coverage in the mandatory-read context.

At each implementation handoff, update the affected milestone here with **Planned / In progress / Blocked / Verified**, its owner, next action/dependency and evidence links. These are delivery statuses, not replacements for Forge's test-result vocabulary. A verified result names its revision, environment, tested scope and date. Link detailed task plans/checks when they exist; do not mark a whole milestone complete because one repair passed. Keep unexecuted checks and unresolved findings visible, and revise the Master Map only when new evidence changes a finding.

## Decisions to settle before implementation

The recommended defaults are starting proposals, not changes silently made to product scope.

| Decision | Recommended starting point | Why it matters |
| --- | --- | --- |
| Initial supported research market | One liquid equity at one-minute resolution; add one actual futures contract immediately after the reference path works | Minimizes moving parts while testing the asset semantics that are currently broken. NQ/ES research must use correct futures metadata. |
| Strategy cadence | Make 1m source data and 5m/10m/daily derived bars explicit contracts | Prevents interval labels from implying nonexistent aggregation. |
| Partner permissions | Owner/operator plus read-only partner, enforced consistently in API, CLI and WS | Avoids building multi-tenant infrastructure while providing real access boundaries. If all partners are full operators, explicitly choose that instead. |
| Capital allocation | Define how validated portfolio allocations become account-specific sizing/rebalancing, cash/margin use and limits; label any advisory-only mode explicitly | The intended deployed portfolio must have meaningful capital semantics. Stored percentages must not imply orders are automatically sized to them. |
| Portfolio replacement and account ownership | Define replacement/drain behavior, treatment of unrelated holdings and overlap between strategies in the same symbol | One active portfolio per account must remain true through pending/running/stopping/recovery states without liquidating unrelated holdings. |
| Account performance | Specify realized/unrealized P&L, fees, cash flows, currency, valuation times and freshness; distinguish account totals from strategy attribution | A live balance snapshot or a selected account label is insufficient for historical performance or reconciliation. |
| Research evidence | Train/validation for selection; immutable final holdout used once, followed by forward observation | Prevents the word OOS from masking optimization on test data. |
| Broker validation environment | Verify available broker accounts and operational state afresh before any order-bearing test | Historical paper/live account descriptions conflict. Do not reuse an old account assumption. |

## Work sequence and completion criteria

### 0 Restore an operational baseline before broad implementation

**Findings:** M20–M24 plus runtime strengthening of M05/M10/M19. **Owner scope:** operations, runtime identity, source/deployment compatibility, accounting and user workflow.

- Preserve the completed bounded release-recovery evidence at c1dd1c1: normal deployment, actual pre-staging cancellation/cleanup and genuine scheduled inventory/preservation all passed. Retain prior failed/PARTIAL records and the separate already-running-installer recovery boundary. Future candidates need their own applicable CI/deployment evidence; this does not certify backups, account identity or live trading.
- Reconcile the actual broker account/login/mode with the registry and documentation. Current production registry says LVP while account API identity is null; do not assume the documented local-LVP/prod-HVP split is active.
- Sequence a compatible supervisor update only after current broker state and maintenance scope are established. The old running resolver demonstrably fails the API's canonical strategy paths. Verify the actual candidate loader before any order-bearing smoke.
- Preserve and restore-test backups, then plan the data-volume migration to the intended attached disk. Confirm actual mounts and adequate free space afterward; do not merely change a path in documentation.
- Preserve the verified reference return-unit and first-day repair; finish broader persisted fill/cost semantics using independently reconciled fills. Backtest/portfolio/report figures must reconcile before strategy decisions rely on them.
- Preserve the verified CLI settings/secret-redaction repair; reproduce unresolved local browser history/trades failures when they recur. Retain successful Azure owner SSO evidence; partner authorization is a separate check.
- Leave a documented, simple research start path and honest system status showing versions, account identity, data freshness, queue progress and backup freshness. The guarded local stack is available now; it deliberately cannot trade or ingest new data.

**Exit:** one agreed environment runs the bounded equity reference workflow with reconciled metrics, a functioning deployment/recovery process, understood account binding, and a real browser journey. This reduces infrastructure friction; dependable broader strategy research still requires the data/asset and selection/holdout gates below. It does not certify live-capital readiness or close every source-audit finding.

The continued [goal-alignment assessment](docs/audits/2026-10-03/goal-alignment.md) adds M25–M29 to the work below. Basic two-account, multi-strategy operation is a core milestone; it must not be deferred as speculative account topology.

### 1 Restore reproducible local verification

**Findings:** M12, M14. **Owner scope:** dependency locks, test tooling, CI, documentation.

- Preserve the exact-main passing lint/strict typing/build evidence, and verify from locks when dependency or installation scope changes. The four initial typing failures were repaired; never suppress checks to obtain a green result.
- Apply applicable security maintenance, starting with the Next.js supported patch line. Review transitive advisories and record why each applies or does not.
- Preserve the repaired Playwright startup and actual bounded API→CLI→real-browser non-trading journeys; refresh the affected journey at each changed candidate.
- Record revision, environment, dependency versions, fixtures and commands. Restore the existing owning CI gates; no engine major upgrade in this step.

**Exit:** clean lint/typing/build and named non-trading contracts on a fresh install. Missing credentials or services appear as explicit unverified checks, not successes.

### 2 Make market data and backtests economically correct

**Findings:** M03, M04, M05, M15, M16, M19. **Owner scope:** storage, registry, catalog, backtest worker, metrics.

- Include provider/dataset/schema/frequency in data identity, or explicitly reject unsupported combinations. Define safe migration and preservation of existing data before changing layout.
- Serialize writers or adopt immutable batches with a tested catalog/compaction rule. Keep atomic replacement but prove concurrent writers cannot silently lose records.
- Carry canonical instrument definitions through catalog construction: asset class, exchange, currency, tick, multiplier, expiry and effective date. Eliminate generic Equity reconstruction for non-equities.
- Define bar timestamp/session/resampling semantics and validate 1m→5m/10m/daily behavior. Translate inclusive requested dates into correct execution bounds and prove the last requested session is consumed.
- Validate missing sessions/bars directly rather than inferring complete coverage from partition endpoints; publish catalog generations so readers cannot observe purge/rebuild in progress.
- Correct first-period return/drawdown and multi-account snapshot aggregation; include cash flows, fees, mark-to-market timing and partial fills in the reference accounting specification.
- Persist explicit starting capital, account/currency model and commission/slippage/spread/latency assumptions, including which approximations are unsupported. Replace the hardcoded capital assumption where the supported workflow requires a different account size.

**Exit:** tiny hand-calculated equity and futures examples match expected fills and net-of-cost P&L; first-day gains/losses and staggered account observations calculate correctly; concurrent-write and mixed-dataset cases preserve or explicitly reject data. A requested unsupported asset/frequency/cost model fails clearly.

### 3 Make selection and graduation trustworthy

**Findings:** M01, M02, M16, M17, M25, M29. **Depends on:** step 2. **Owner scope:** research engine, optimizer, graduation, evidence and composition models.

- Preserve PR108 training-only research selection and separate diagnostics; finish portfolio allocation selection using only information available before each evaluation window, with independent final validation.
- Preserve eligible-training selection and exploratory Discovery refusal. Enforce independent final-evidence requirements before validated graduation; missing/failed final evidence must not advance a candidate.
- Bind candidate evidence to immutable strategy source and imported helpers, configuration, data content identity, engine version, costs and split boundaries. Validate the executed bundle and isolate Optuna studies per immutable experiment. Editing these inputs invalidates the relevant certification.
- Separate reusable validation evidence from account-specific deployment/member lifecycle. Starting the same validated composition on account B must preserve account A's binding and status without rerunning research or duplicating candidate evidence merely to bypass a single-use link. Retain account-specific capital/risk suitability checks.
- Clarify portfolio materialization versus account assignment. Reconcile the promotion route's `DU`-only input and description-only target with the actual test-account plan; do not fabricate a paper account ID or silently remove a safety restriction to make the workflow pass.
- Match reserved compute slots to actual parallelism, propagate cancellation between trials, and move blocking operations off worker event loops before scaling sweeps.
- Distinguish combined return-series analysis from simulation of shared cash, margin, orders and transaction costs.
- Define sample-size, turnover/cost, parameter-stability and forward-observation requirements before viewing final results. A failed strategy stays failed rather than triggering unbounded parameter search.

**Exit:** changing final holdout values cannot change trained weights or selected configuration; missing/failed evidence cannot advance a candidate; one reproducible report identifies all inputs and limitations; one approved immutable composition can support separately checked account assignments without corrupting prior evidence or lifecycle state. No profitability guarantee is part of this gate.

### 4 Repair live risk and financial visibility

**Findings:** M06, M07, M08, M13, M18, M26, M27, M28. **Can begin alongside:** steps 2–3, with isolated ownership. **Owner scope:** account assignment, live node, risk, projections, financial records, interface scoping and supervisor.

- Wire per-order, position/exposure and daily-loss limits into the real node path; define the risk scope for a whole account versus one strategy. Verify allowed flattening remains possible under a halt.
- Implement or clearly reject the selected portfolio sizing contract; prove research weights and live quantities agree where promised.
- Enforce one active portfolio assignment per account atomically through API, command delivery and supervisor recovery. A distinct revision must not start merely because another deployment has reached running. Define replacement/drain behavior and retain the assignment while stopping or while a failed deployment has unresolved exposure.
- Use actual installed Nautilus events/serialization as fixtures. Preserve signed quantity, member identity, position identity, actual fill prices, strategy hashes and partial-fill P&L.
- Replace zero-PnL placeholders with a minimal reconciled account/portfolio/strategy accounting record: fills, fees, positions, cash/equity marks and cash movements, with defined currency, day boundary, return denominator and freshness. Preserve history across portfolio revisions/restarts and distinguish unrelated/manual exposure.
- Make account scope explicit across API, CLI and UI observations. Merge a deployment's stream into the full snapshot without dropping other accounts, handle pagination beyond the global status limit, and expose stale/missing account coverage. View selection and execution target must remain explicit separate controls.
- Derive status/counts from the active supervisor authority and remove retired manager plumbing only after replacing its consumers.
- Keep residual positions visible for failed/stopping deployments. Label deployment-cache flatness separately from fresh broker-confirmed flatness, including scope, freshness, open orders and reconciliation status.
- Persist safety-critical Redis state and define startup after empty/replaced Redis. A surviving node must remain blocked until explicit safe recovery; a connection failure and state loss are different test cases.

**Exit:** deterministic order→partial fills→positions→P&L→close scenario agrees across event source, durable records, API, CLI and UI; two strategies in the same symbol remain correctly attributed; two accounts remain visible while one streams; conflicting portfolio assignment is refused atomically; failed/stopping residual exposure remains visible; real daily/cumulative performance reconciles including fees and external cash flows; risk limits block openings but allow exits; Redis recreation/supervisor interruption cannot silently reopen trading authority. Offline proof precedes broker testing.

### 5 Close partner and release boundaries

**Findings:** M09, M10, M11, M14. **Can begin alongside:** steps 2–4. **Owner scope:** auth, CLI, WS, deployment, backups and monitoring.

- Enforce the agreed partner policy and use one identity vocabulary. Test operator and reader over REST, CLI and WS; distinguish local operator-only CLI commands.
- Gate deployment on successful required checks for the exact revision. Include `stopping` and other position-bearing transitional states in release refusal; fail closed if state cannot be established.
- Define schema/image compatibility windows. Rehearse rollback and restore in a disposable environment, including a deliberately incompatible boundary.
- Make telemetry attributable across processes; check worker progress, data freshness and broker state rather than relying on `/health` alone.
- Correct the Daily P&L label and specify which account/time period every financial card represents.

**Exit:** reader cannot mutate broker/trading state; partner streams match REST visibility; failed CI or stopping deployments prevent promotion; restore yields usable application records/data; operator can detect a stale feed and a stuck job from supported interfaces.

### 6 Prove the whole workflow and measure its value

**Depends on:** steps 1–5. **Owner scope:** coordinated API→CLI→UI verification, then explicitly controlled external validation.

1. Ingest a small licensed dataset, register the reference strategy, submit a backtest, inspect/report its results, run valid selection and create an evidence-bound portfolio revision.
2. Complete the same workflow through API first, CLI second and browser third. No mocked responses in the final acceptance journey; keep mocks for isolated component tests.
3. Prove the multi-account product: account A runs a portfolio with at least two strategies; account B runs a different portfolio. Demonstrate reuse of the same validated revision on B in a separate case, with different account capital/limits and unmodified source evidence. Verify supported API/CLI/UI observations follow their selected view scope, while account commands independently name and validate their execution target and fleet controls retain explicit fleet scope. Reject a conflicting active portfolio on A, and prove stopping/halting A leaves B's authority and positions intact. Reconcile account totals with strategy/portfolio attribution and unrelated holdings. Start with offline/controlled integration evidence, then the smallest authorized broker proof.
4. After verifying account identity and current authorization, perform broker validation including fill reconciliation, stop/flatten and fresh broker-confirmed flatness for the agreed account/deployment scope. Reconcile broker positions and open orders; a local deployment-cache result alone cannot satisfy this gate. A historical drill does not substitute for this candidate's evidence. The standing test-account authorization remains bounded by verified identity, short minimal-exposure cycles and its original account limits.
5. Exercise controlled restart/disconnect/data-stale/state-loss scenarios; preserve clear stop conditions and operator recovery evidence, including account isolation during recovery.
6. Measure a realistic 100-symbol research workload and simultaneous dashboard/live-control responsiveness. Record rows, duration, peak memory, queue delays and data/storage costs from actual measurements.
7. Compare one identical strategy/data/cost scenario with a thin Nautilus baseline and, if switching remains attractive, LEAN or a research engine. Compare operator time, correctness and ownership cost; avoid feature-count scoring.

**Exit:** a replayable complete two-account, multi-strategy journey at the final revision, correct account-scoped controls/performance, honest supported-asset boundaries and measured capacity. Live-capital readiness and strategy investment approval remain separate decisions; successful plumbing is not evidence of alpha.

## Simplification after correctness

During each repair, consolidate the directly involved duplicate policy/state boundary and remove only demonstrably dead code. After the complete journey passes, consider reducing optimizer modes, worker topology or UI surfaces based on actual use. Reconcile `docs/agent-context.md`, architecture docs and runbooks with verified behavior. Keep this map's finding IDs as closure references, with test evidence and revision for each.

Preserve the account-independent portfolio and explicit deployment model: it serves the clarified goal. The current registry/credentials/node factory are IB-specific; provider metadata is not a second broker integration. A later adapter must address account identity, credentials, instruments/orders and reconciliation, but no generic broker framework is required to close the present IB acceptance gaps.

Do not add a new database, Kubernetes, a generic strategy DSL, multi-tenant SaaS scaffolding, broad AI features or a broker rewrite to solve these findings. A separate live/compute host is justified if resource measurements or the chosen recovery objective require it.

## How to coordinate the implementation

Use three bounded workstreams: **data/research**, **live/risk/accounting**, and **access/operations**. Product/interface work belongs to the relevant capability throughout all three streams; assign explicit frontend/CLI ownership when parallel work touches shared files. Agree on shared instrument, evidence and identity contracts before parallel edits. Give each worker explicit files and acceptance cases; integrate at the dependency boundaries above. The main agent maintains this plan and reconciles cross-system results.

Each finding closes only with the failing case reproduced, a narrow repair, relevant regression checks, and evidence from the final revision. Upgrade risk, environment failures and unresolved external checks remain visible. The monthly windows are planning targets; refine effort and cost estimates from the first verified batch and actual workload measurements.
