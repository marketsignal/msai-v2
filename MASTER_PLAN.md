# MSAI v2 Master Plan

**Implementation roadmap reconciled October 6, 2026, after PR111 Azure acceptance and cleanup.** Derived from the [Master Map](MASTER_MAP.md), initially assessed at `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. PR102–111 are merged; the latest accepted Azure application revision is `fb75795317a7568f276040b5482fae3ec55c9c30`, retaining PR108 research selection and PR109 inclusive-date behavior. Exact-main CI/auth/build, [Deploy 37420900926 and independent cleanup](https://github.com/marketsignal/msai-v2/actions/runs/37420900926), actual data-path smoke and installed API/CLI/owner-browser preservation passed. [October 6 release scope and evidence](MASTER_MAP.md#released-assessment-integration-october-6). Earlier actual pre-staging cancellation and genuine scheduled recovery passed at c1dd1c1; **M20 remains PASS for that bounded scope**, without implying a new cancellation test at fb757953. [Recovery evidence and limits](docs/audits/2026-10-04/release-acceptance.md). This plan records priorities and evidence, not blanket authorization to trade/deploy.

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
| **1. Reliable strategy research** | Author/version a Python strategy, run backtests and independent validation, compare experiments, inspect trades and reports, and reproduce the result. | Reconciled capital, fills, costs, P&L and returns; correct data/date/asset semantics; immutable experiment inputs; selection independent of final holdout; consistent API/CLI/browser results and useful failure/retry behavior. Prove equities first, then actual futures before claiming futures support. | **In progress.** Bounded reference accounting, PR108 training-only selection/Discovery and PR109 inclusive UTC date bounds are verified locally and on Azure through API/CLI/real browser. Dataset/session completeness, realistic costs, immutable inputs and independent final-validation/graduation gates remain open. |
| **2. Reliable portfolio operations** | Construct and validate portfolios, assign one active portfolio to each IB account, and monitor multiple accounts independently. | Two accounts and multiple strategies; reusable validation evidence; account-specific sizing/limits; atomic assignment/replacement; correct view scope and explicit execution targets; reconciled account/portfolio/strategy performance. Offline integration precedes the bounded authorized broker journey. | Planned; depends on research evidence plus live/account repairs. |
| **3. Dependable daily operation** | Operate the supported workload with clear health, alerts, permissions and recovery, without routine infrastructure intervention. | Measured capacity, supported release/rollback, tested backup restoration, restart/disconnect/state-loss recovery, partner permissions and an observation period whose duration, workload and acceptable intervention are defined before it starts. Record incidents and outcomes; a short smoke test cannot establish this milestone. | Planned; operational repairs begin in week 1 and continue across milestones. |

Milestone 1 enables research iteration for its explicitly verified markets and workflows. Portfolio execution and live-capital readiness retain their own gates. None of these milestones certifies strategy alpha.

### Current handoff after automatic deployment and recovery testing

**Earlier verified baseline:** PR102/103/104 merged; exact-main checks and Deploy `37171071535` passed at `fa4c8f8`. Real preservation, cleanup-only rerun and SSH-failure cleanup passed, while cancellation did not interrupt its producer. Preserve that [baseline evidence and limits](docs/audits/2026-10-03/azure-normal-pipeline.md). Current installed revision and successful actual cancellation are recorded in [the new acceptance record](docs/audits/2026-10-04/release-acceptance.md); the [first installation](docs/audits/2026-10-03/azure-first-installation.md) retains the real browser submission.

**Bounded release acceptance — Verified at c1dd1c1.** PR107 startup repair passed exact-main CI/auth/build, normal Linux deployment, installed API/CLI/browser preservation and actual ordinary pre-staging cancellation/cleanup. Genuine scheduled run37218539817 succeeded at16:55:34UTC; bounded real logs and independent Azure inventory confirm four unrelated policies preserved across fourteen fields. M20 is closed for this scope. Preserve original failed/PARTIAL records, exact-candidate certificates and the separate already-running-installer recovery limitation. [Current evidence](docs/audits/2026-10-04/release-acceptance.md).

**Earlier repair integration:** [PR105](https://github.com/marketsignal/msai-v2/pull/105) integrated reviewed cancellation controls as `f4ede895`; its focused checks and [normal deployment](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) passed. That predates the later `e3ad095` import failure and PR107 startup repair. [Historical candidate/integration evidence](docs/audits/2026-10-03/release-cancellation-candidate.md) remains separate from the current acceptance record above.

**Completed assessment cleanup, October 6:** PR109 cleanup and guarded main-source handoff preserved PostgreSQL/Redis/data, saved results and original drafts. After PR111 release acceptance, both continuity folds passed; 266 private files and the ignored plan were hash-preserved, the merged remote/local assessment branch and detached checkout were removed, and the final release inventory was one clean checkout with only `main` locally/remotely at fb757953. The subsequent user-requested pull confirmed local `main` and `origin/main` match that revision. New bounded task branches do not invalidate this dated cleanup result. [Release and retention evidence](MASTER_MAP.md#released-assessment-integration-october-6).

**Completed main-first integration, October 6:** Forge 6.4.5/PR110 was merged as `54dc407`; PR111 then merged the seven-document assessment as `fb757953` after final candidate reviews/checks and ten exact-head checks. Exact-main CI/auth/build, automatic Azure deployment, data-path smoke, independent installed-image/broker/policy readback and API/CLI/owner-browser preservation passed. The accepted baseline contains the completed bounded V2 trial with partial passes and explicit failures; Nautilus V2 has not been installed. [Current release evidence](MASTER_MAP.md#released-assessment-integration-october-6).

**Historical failed rollout, October 5:** automatic Deploy 37356543395 at `54dc407` failed at AAPL/Databento smoke bootstrap with a client `ReadTimeout`. The installer reported last-good rollback and independent cleanup passed; exact restored images and broker identities were not independently established at that time. Its cause remains unattributed. The later PR111 deployment at fb757953 passed the actual data-path smoke and independent identity checks; that supersedes the pending release gate without recertifying the failed October 5 rollout. [Historical observation](MASTER_MAP.md#forge-integration-and-automatic-rollout-october-5); [accepted October 6 release](MASTER_MAP.md#released-assessment-integration-october-6).

**Assessment publication outcome:** the previously approved assessment merge triggered the existing automatic Build/Deploy chain. Deploy 37420900926, actual data-path smoke and independent cleanup passed; retained API/CLI payloads and owner-browser result/report reload were unchanged. No new cancellation drill, broker order or V2 installation occurred. This documentation reconciliation authorizes no manual production repair or rollout.

**Completed research selection batch:** PR108 now selects eligible training evidence before diagnostics in grid/halving/Optuna, persists the latest training window and labels automatic/manual exploratory Discovery. Final reviews, independent verification and real local/Azure API→CLI→browser acceptance passed. [Released scope and remaining boundaries](MASTER_MAP.md#released-research-selection-and-discovery). The [earlier candidate record](docs/audits/2026-10-04/research-selection-acceptance.md) retains initial failures; its pending-certification wording is historical. Only the research sweep/walk-forward and exploratory Discovery portion of M01/M02 is repaired; portfolio OOS allocation and final-validation/graduation remain open.

**Completed inclusive-date slice, M15 — Verified at 84707a4.** [PR109](https://github.com/marketsignal/msai-v2/pull/109) passed final candidate reviews/application/E2E gates, publication, merge, exact-main checks and actual Azure acceptance. December 3–3 processed **502 bars and 32 fills** through API, installed CLI and a new real-browser submission. Signed fill cash flows reconciled to **+$1.13 on $1 million**, matching +0.000113%; per-fill P&L remained explicitly unavailable. Reversed dates created no job; report, history and reload persisted. Sweep/walk-forward diagnostics and exploratory Discovery retained the expected training/window evidence. Six application images were installed; both broker container/image/start identities and four network policies across fourteen fields were preserved. [Released evidence and limits](MASTER_MAP.md#released-inclusive-backtest-dates). This closes the date-boundary defect, not all of M15 or Milestone 1.

### Completed batch: Nautilus native reuse and version decision

**Decision checkpoint completed October 5; compatibility is partial, with reproduced gaps. Owner: main agent, with research/data and live/risk integration owners.** The [executed trial](docs/research/2026-10-05-nautilus-runtime-trial.md) extends the [initial source/package assessment](docs/research/2026-10-05-nautilus-native-upgrade.md). The [Nautilus-first rule](docs/agent-context.md#nautilus-first-engineering) applies to every technical decision. No application finding is closed by this trial.

**Decision: target V2 directly in development; retain the current 1.223 platform until the replacement is accepted. Do not schedule a 1.231 bridge without a concrete demonstrated benefit.** RC6 is the tested candidate, not an approved deployment. It passed the tiny equity/futures economic examples, supports a native persistence path and improves the tested staggered-cash/per-order-limit controls. The 1.231 bridge did not resolve those V1 risk/funding cases and introduces an IB Async downgrade. Refresh the exact upstream release before implementation and promotion; upstream's RC warning for production/live capital remains relevant.

**Demonstrated:** 21 native scenario/version executions with retained failures, reconciled $28 equity/$48 futures examples, inclusive fixture bounds, native live simulated events, hosted/cache incompatibility, native-only strategy/custom-cache state reload and complete backend dependency resolution. Account cache keys persisted, but independent account restoration remains unproved. [The trial report](docs/research/2026-10-05-nautilus-runtime-trial.md) records exact packaging results and separates native proof from application proof.

**Unmet gates remain explicit:** simultaneous shared-cash funding fails; aggregate margin affordability fails in all three versions including RC6; a second same-bar sell differs by one tick; unchanged event projections/cold readers and hosted cache lifecycle are incompatible; real strategy discovery, application journeys, open-order/position recovery, crash/state-loss recovery and Databento-to-IB identity still require acceptance. Native equity/report semantics must be reconciled, including differences between documentation and observed return-series behavior. Do not treat this checkpoint as a complete compatibility pass, live readiness or a speed benchmark.

Reconsider a bridge if a specific V1 fix enables the next milestone sooner, or if a required V2 contract demands substantial custom infrastructure/upstream work. The prior three-working-day budget was a decision checkpoint, not permission to skip failed checks or a migration estimate. No dependency, running application or broker was upgraded by the trial.

### Next implementation batch: one real research journey on V2

**Status: Planned, not started. Owner: research/data integration, with live/control-plane and API/CLI/frontend owners; main agent verifies the combined result.** Deliver one operator-visible strategy iteration path, not a broad engine rewrite or a permanent dual-version abstraction.

**Main-first prerequisite — completed October 6:** final assessment reviews/checks, PR111 merge after exact-head CI, approved automatic pipeline acceptance and merged branch/worktree cleanup passed at fb757953. Local `main` is synchronized with GitHub; no further sync or compatibility-trial rerun is the next batch. Begin the narrow V2 research integration from this consolidated baseline, preserving the completed trial's partial passes and explicit failures. V2 remains uninstalled; the application/API/CLI/browser journey and release gates below remain required. [Accepted baseline](MASTER_MAP.md#released-assessment-integration-october-6).

1. **Pin the candidate and declare application dependencies.** Refresh official release documentation, retain directly used packages such as Msgspec, and establish the application startup boundary. Every service importing the changed integration must start or fail explicitly; a research-only candidate must not silently break live routes. Preserve the running release and separate candidate storage.
2. **Port the narrow research path through native modules.** Real Python strategy/config discovery and validation → native instrument/catalog → native backtest → persisted fills/account results → existing API, CLI and intuitive browser report/reload. Retain explicit capital/leverage/fee/fill assumptions, inclusive dates and training-only selection. Prove both the independent tiny example and a representative existing strategy. Compare callback counts, indicator values, signal/first-trade timestamps, orders and fills, accounting for documented warm-up changes and expected differences. Final P&L alone is insufficient. Unsupported configuration should produce a useful refusal.
3. **Keep the unresolved engine cases visible.** Reproduce simultaneous funding and the one-tick execution difference on the selected release. Find a supported native resolution or retain an explicit unsupported portfolio case; do not silently certify shared-capital simulation or implement a shadow account ledger. Reconcile actual account returns before replacing custom analytics.
4. **Accept the complete slice.** Focused regression checks, Linux application startup, existing saved-result preservation, and real API → CLI → computer-use browser journeys including submission, failure/correction and reload. No mocked browser result counts as acceptance. Record exact engine/data/assumption provenance and a compatible rollback path.

**Then, before any rollout affecting live services:** port the supported native node lifecycle/stop handle, event decoding, cold reads and safety wrappers. Native-only `run()` plus Redis cache is the proven feasibility path; `run_async()` plus cache is not. Prove order/position recovery, deduplication/attribution, limits/halt/state loss and data-to-broker identity before a broker journey against an explicitly authorized, verified account and trading mode. Check strategy-scoped cancellation preserves siblings and intentional account-wide cancellation reaches every intended execution client; V2's default is strategy-only. This trial establishes no paper-account availability or new trading authorization. Coordinate compatible backend, worker and supervisor versions with versioned state. Do not use a general application rollout to mix incompatible engine contracts. Remove old adapters only after replacements pass. Real-capital readiness retains its separate gate.

### Following implementation batch: equity data quality and cost assumptions

**Status: Planned; follows the accepted V2 research slice above. Owner: research/data and accounting, with CLI/frontend ownership and coordinator verification. Findings: remaining M15/M19 and M16 input-coverage boundary; detailed phase 2 below.** The operator must be able to understand which data and economic assumptions produced a result before trusting or comparing strategies. Configure the selected engine's native models and reuse existing storage, job records and public interfaces; do not create a second simulator, fee engine or interval scanner.

1. Define the supported equity provider/dataset, one-minute schema, bar timestamps and session/calendar policy. Reuse the existing native interval check and ingestion integrity checks; add only the missing session/content policy. Distinguish missing input from a legitimate empty period and provide an actionable refusal under the chosen policy. A 502-bar observation alone is not session-completeness proof.
2. Persist and display the chosen opening capital, currency, leverage and fee/slippage assumptions, passing them to native venue/model configuration. Keep an explicitly labelled recorded/zero-cost baseline where applicable; validate the selected supported cost model and refuse unsupported assumptions clearly.
3. Prove a tiny independently calculated equity example: fills, recorded fees, net P&L and account returns must agree through API, CLI and intuitive real-browser results/report/reload. Include a meaningful missing-data or unsupported-cost failure and its supported correction. Preserve the released inclusive-date and research-selection regressions.

**Acceptance:** the same saved experiment explains its inputs/costs and reconciles to the independent expected result across all three interfaces; missing/unsupported inputs cannot silently produce a trusted result. Select concrete session and cost assumptions before coding. This batch is not started by this documentation update.

**Following batches:** bind source/imported helpers, configuration, data content, engine, costs and splits to a reproducible experiment (M16); enforce independent final-validation/graduation evidence (remaining M01/M02); prove actual futures metadata/economics (M03) before extending supported research. Milestone 2 then requires portfolio validation, reusable evidence, account-specific sizing, one active portfolio per account and real two-account acceptance. The monthly windows below remain targets, with unmet gates carried forward.

Broker identity, supervisor compatibility, account isolation and performance reconciliation remain prerequisites for portfolio/live work. No broker order or portfolio deployment was included in this installation.

## Next month: October 3–November 2, 2026

These are delivery targets, not a promise to close all 29 findings in a month. The primary target is **Milestone 1**. Portfolio and operational acceptance advance as their dependencies pass; missed gates move dependent work rather than weakening acceptance. **October 5 revision:** the native trial selects direct V2 development, with compatibility still partial. Week 1 now begins the narrow research integration; weeks 2–4 remain conditional on its acceptance and live/state work. No migration duration has been measured. Move later dates rather than compressing validation to preserve the calendar. The numbered implementation phases below remain the detailed dependency and completion criteria.

| Target window | Concrete work and ownership | Reviewable result / gate |
| --- | --- | --- |
| **Week 1 · Oct 3–9** | Preserve released accounting/selection/date and bounded release-recovery results. Native trial decision completed October 5; research/data begins the narrow V2 research integration, live/risk owns lifecycle/event/cache planning and unresolved native reproductions. Operations prepares restore/storage work independently. | Retained baseline plus a reviewable V2 strategy/catalog/results slice; trial passes and failures stay separate. Deployment/storage mutations follow their applicable authorization. |
| **Week 2 · Oct 10–16** | Implement the selected minimum engine integration when justified, then native equity data/cost exposure and experiment provenance. Research validation owns independent holdout boundaries; interfaces follow the same saved evidence. If migration exceeds the window, carry dependent work forward. | Accepted selected-engine research journey and explicitly remaining live/state gates; progress toward Milestone 1 rather than an assumed full migration. Close Milestone 1 only when relevant phases 0–3 pass. Document unsupported markets/intervals plainly. |
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

At each implementation handoff, update the affected milestone here with **Planned / In progress / Blocked / Verified**, its owner, next action/dependency and evidence links. Reconcile this plan and the affected Master Map findings before reporting the batch complete; update agent context when runtime/startup guidance changes. These are delivery statuses, not replacements for Forge's test-result vocabulary. A verified result names its revision, environment, tested scope and date. Link detailed task plans/checks when they exist; do not mark a whole milestone complete because one repair passed. Keep unexecuted checks and unresolved findings visible, and revise the Master Map only when new evidence changes a finding.

## Decisions to settle before implementation

The recommended defaults are starting proposals, not changes silently made to product scope.

| Decision | Recommended starting point | Why it matters |
| --- | --- | --- |
| Engine version and native reuse | Direct V2 development selected from the bounded trial; refresh the exact release, retain 1.223 until accepted, consider 1.231 only for a demonstrated bridge need | Avoid porting new custom code twice or duplicating native functions. Native fixture passes are not application or broker compatibility. |
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

### 0a Select native capabilities and the engine version

The [native version decision](#completed-batch-nautilus-native-reuse-and-version-decision) is recorded; complete the [V2 research slice](#next-implementation-batch-one-real-research-journey-on-v2) before expanding engine-related research work. Live/state and shared-funding gates still precede the relevant phase 4 work. Native models, catalog operations, reports and joint simulation are the default starting point; retained custom behavior must have a demonstrated product or integration need. Independent operational and security work can proceed. The investigation is complete for its bounded scope; application compatibility and the upgrade are not.

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
- Carry the existing native Databento/instrument definitions through catalog construction: asset class, exchange, currency, tick, multiplier, expiry and effective date. Eliminate generic Equity reconstruction for non-equities without building another instrument parser.
- Define bar timestamp/session/resampling semantics and validate 1m→5m/10m/daily behavior. Translate inclusive requested dates into correct execution bounds and prove the last requested session is consumed.
- Reuse native interval discovery and existing integrity checks; validate missing sessions/content under the declared data policy rather than inferring completeness from partition endpoints. Evaluate native catalog maintenance and the smallest safe publication boundary before adding custom generation machinery.
- Correct first-period return/drawdown and multi-account snapshot aggregation; include cash flows, fees, mark-to-market timing and partial fills in the reference accounting specification.
- Configure native capital, account/currency, leverage, commission/fill/latency models and persist their assumptions, including unsupported approximations. MSAI exposes policy and evidence; Nautilus calculates fills and account mechanics. Compare native account statistics with the required return semantics before extending custom accounting/report adapters.

**Exit:** tiny hand-calculated equity and futures examples match expected fills and net-of-cost P&L; first-day gains/losses and staggered account observations calculate correctly; concurrent-write and mixed-dataset cases preserve or explicitly reject data. A requested unsupported asset/frequency/cost model fails clearly.

### 3 Make selection and graduation trustworthy

**Findings:** M01, M02, M16, M17, M25, M29. **Depends on:** step 2. **Owner scope:** research engine, optimizer, graduation, evidence and composition models.

- Preserve PR108 training-only research selection and separate diagnostics; finish portfolio allocation selection using only information available before each evaluation window, with independent final validation.
- Preserve eligible-training selection and exploratory Discovery refusal. Enforce independent final-evidence requirements before validated graduation; missing/failed final evidence must not advance a candidate.
- Bind candidate evidence to immutable strategy source and imported helpers, configuration, data content identity, engine version, costs and split boundaries. Validate the executed bundle and isolate Optuna studies per immutable experiment. Editing these inputs invalidates the relevant certification.
- Separate reusable validation evidence from account-specific deployment/member lifecycle. Starting the same validated composition on account B must preserve account A's binding and status without rerunning research or duplicating candidate evidence merely to bypass a single-use link. Retain account-specific capital/risk suitability checks.
- Clarify portfolio materialization versus account assignment. Reconcile the promotion route's `DU`-only input and description-only target with the actual test-account plan; do not fabricate a paper account ID or silently remove a safety restriction to make the workflow pass.
- Match reserved compute slots to actual parallelism, propagate cancellation between trials, and move blocking operations off worker event loops before scaling sweeps.
- Distinguish combined return-series analysis from native multi-strategy simulation of shared cash, margin, orders and transaction costs. Use the latter for joint-execution acceptance; retain MSAI's allocation and independent-validation policy.
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
7. Extend the thin-native comparison started in the version trial to the complete workflow. If switching platforms remains attractive, compare with LEAN or a research engine using identical strategy/data/cost assumptions. Compare operator time, correctness and ownership cost; avoid feature-count scoring.

**Exit:** a replayable complete two-account, multi-strategy journey at the final revision, correct account-scoped controls/performance, honest supported-asset boundaries and measured capacity. Live-capital readiness and strategy investment approval remain separate decisions; successful plumbing is not evidence of alpha.

## Simplification after correctness

During each repair, consolidate the directly involved duplicate policy/state boundary and remove only demonstrably dead code. After the complete journey passes, consider reducing optimizer modes, worker topology or UI surfaces based on actual use. Reconcile `docs/agent-context.md`, architecture docs and runbooks with verified behavior. Keep this map's finding IDs as closure references, with test evidence and revision for each.

Preserve the account-independent portfolio and explicit deployment model: it serves the clarified goal. The current registry/credentials/node factory are IB-specific; provider metadata is not a second broker integration. A later adapter must address account identity, credentials, instruments/orders and reconciliation, but no generic broker framework is required to close the present IB acceptance gaps.

Do not add a new database, Kubernetes, a generic strategy DSL, multi-tenant SaaS scaffolding, broad AI features or a broker rewrite to solve these findings. A separate live/compute host is justified if resource measurements or the chosen recovery objective require it.

## How to coordinate the implementation

Use three bounded workstreams: **data/research**, **live/risk/accounting**, and **access/operations**. Product/interface work belongs to the relevant capability throughout all three streams; assign explicit frontend/CLI ownership when parallel work touches shared files. Agree on shared instrument, evidence and identity contracts before parallel edits. Give each worker explicit files and acceptance cases; integrate at the dependency boundaries above. The main agent maintains this plan and reconciles cross-system results.

Each finding closes only with the failing case reproduced, a narrow repair, relevant regression checks, and evidence from the final revision. Upgrade risk, environment failures and unresolved external checks remain visible. The monthly windows are planning targets; refine effort and cost estimates from the first verified batch and actual workload measurements.
