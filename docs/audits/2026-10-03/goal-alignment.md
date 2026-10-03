# Alignment with the account and portfolio operating model

Assessment date: 2026-10-03. Source revision: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. Documentation only; no application changes, service access, credentials, broker actions, or new tests in this pass.

The clarified goal is a professional investment research and operations platform: create Python strategies, validate alpha without leakage or bias, construct and validate combinations of strategies, deploy a portfolio to each brokerage account, and monitor results consistently by account, portfolio, and strategy through API, CLI, and UI. Interactive Brokers is the first execution integration; other brokers are a later extension. The scope is the operator and a few partners, not an inferred multi-tenant SaaS product.

**The existing entities are a useful foundation, but the complete operating model is not implemented or accepted end to end.** Account-independent portfolio revisions and account-bound deployments are appropriate. The missing contracts are reusable validation evidence, one active portfolio per account, consistent account selection, effective portfolio allocation, and reconciled live performance history. These are core requirements under the clarified goal. Basic two-account operation should not be grouped with speculative additional account topologies to defer indefinitely.

## Evidence boundaries and relation to the master map

Read `docs/agent-context.md` and `.forge/instructions.md` completely. Inspected the relevant models, start and promotion handlers, supervisor dispatch and collision guards, account snapshots, performance aggregation, CLI commands, and UI/API query propagation. Backend references that omit `backend/src/msai/` are relative to that directory; other file references are repository-relative. Line numbers identify the inspected revision.

- **Source-confirmed:** the stated model, branch, filter, or calculation is present in source. This does not establish that the corresponding operational failure has occurred.
- **Previously tested offline:** only the specific checks documented in [live-safety.md](live-safety.md) and [research-data.md](research-data.md). No new test result is claimed here.
- **Previously runtime-observed:** the guarded local research/backtest/portfolio jobs and Azure browser/backtest/report journey recorded in [runtime-assessment.md](runtime-assessment.md) and [runtime-journey.md](runtime-journey.md). These were simulations; they do not validate two-account live deployment or live accounting.
- **Unverified:** the proposed two-account acceptance scenario below, fresh broker identity, actual portfolio-sized orders, broker-confirmed reconciliation, and live equity history.

This report supplements [MASTER_MAP.md](../../../MASTER_MAP.md). It does not reopen the established leakage/evaluation findings M01–M05, unwired risk/allocation M06, event projections M07, identity/flatness M18/M23, or the release/runtime findings. GA-01, GA-02, GA-03, and GA-05 add specific operating-model gaps; GA-04 makes the already noted incomplete live performance materially concrete; GA-06 bounds future broker extensibility.

## Capability matrix

| Required capability | What exists | Assessment and evidence |
| --- | --- | --- |
| Python strategy research and backtests | Nautilus strategy loading, research jobs, portfolio jobs, reports; local and Azure simulated workflows exercised | **Partial, runtime-observed.** Retain the stack. Leakage, allocation evaluation, return accounting, and economic evidence still require the repairs already described in M01–M06/M19. Successful jobs do not establish alpha. |
| Portfolio as a combination of strategies | Account-independent `LivePortfolio`; immutable revision and member configuration/instrument/weight snapshots | **Source-confirmed foundation.** `models/live_portfolio.py:30`; `services/live/revision_service.py:87`, `:114`, `:127`, `:142`; `schemas/live_portfolio.py:19`. Preserve this distinction between composition and deployment. |
| One deployment contains several strategies | Member configurations are assembled before constructing the account's Nautilus node | **Source-confirmed.** `services/nautilus/trading_node_subprocess.py:2865`, `:2892`. Effective capital sizing and member-level monitoring remain broken/incomplete under M06/M07. |
| Different portfolios on different accounts | Deployment binds account and frozen revision; uniqueness is revision plus account | **Source-confirmed model, live journey unverified.** `models/live_deployment.py:54`, `:110`, `:158`. The model can represent the target arrangement. |
| Same validated portfolio/strategy on several accounts | Revision/account identity supports separate deployments, but a graduation candidate has one deployment link | **Workflow gap, source-confirmed.** GA-01. Reuse is not a general supported path from one approval to many account deployments. |
| One active portfolio per account | Active process uniqueness is per deployment; startup serialization is per gateway only while initializing | **Missing invariant in the inspected path, source-confirmed.** GA-02. No two-account or conflicting-account live test was run. |
| Consistent account selection in API, CLI, and UI | UI account view selector; explicit account target on start; selected live views filtered in the browser | **Partial, source-confirmed.** GA-03. Several account/trade queries remain global or gateway-bound, and the All positions view can collapse to one deployment. |
| Live results by account, portfolio, and strategy over time | Deployment/member identities, order audit, position projections, daily PnL table skeleton | **Incomplete, source-confirmed.** GA-04 plus M07/M13/M18. The daily aggregator writes zero PnL; account snapshots are a singleton gateway view, not an account equity ledger. |
| Stop, halt, and recover an account portfolio | Account routing, halt state, stable deployment identity, subprocess lifecycle, native Nautilus reconciliation | **Substantial implementation and bounded offline tests.** Keep; see [live-safety.md](live-safety.md). Cache flatness is not fresh account-wide broker flatness. |
| Interactive Brokers first, other brokers later | Concrete IB execution factory/configuration; broker registry and credentials are IB-specific | **IB implementation; extension work required.** GA-06. Security-master provider metadata does not constitute a second execution adapter. |

## Additional findings

### GA-01 — P2: a validation candidate is consumed by one deployment

**Source-confirmed; high confidence.** The data model supports a reusable revision across accounts, but the approval/deployment relationship does not.

`models/graduation_candidate.py:35` associates strategy/research/configuration/metrics with a single nullable `deployment_id`. On a first start, `api/live.py:941` searches for exactly one candidate for each member strategy with `stage == live_candidate` and `deployment_id IS NULL`. Zero eligible candidates produces `BINDING_NOT_GRADUATED`; multiple candidates produce `BINDING_AMBIGUOUS` before selecting by configuration (`:957`, `:972`).

The final locked check explicitly addresses concurrent starts of the same revision on two accounts: if the candidate is already linked to a different deployment, the second start is rejected (`api/live.py:2069`). The successful start stores the singular link and moves the candidate to `live_running` (`:2096`). This is a sound protection against overwriting audit links, but it also exposes the product mismatch.

Consequently, a portfolio revision can have deployments on account A and account B in the schema (`models/live_deployment.py:54`), yet the same approved candidate cannot simply support both. A fresh eligible candidate can permit another deployment; the claim is not that all multi-account deployment is impossible. The problem is that reusable research approval and per-account operating state are coupled. It also affects two different portfolios that share an approved strategy.

**Recommendation: repair the relationship, keep the immutable evidence.** Represent the approved strategy/configuration/evidence as reusable validation, with separate deployment/member associations and lifecycle state. A second account should retain the exact evidence reference without silently changing the first account's status or requiring duplicate research. Preserve independent account suitability/allocation checks; approval reuse must not bypass them. This is additional to M02's empty-evidence problem.

### GA-02 — P1: one active portfolio per account is not enforced

**Source-confirmed; high confidence for the inspected API/supervisor path. No conflicting live starts were performed.**

The deployment database uniqueness key is `(portfolio_revision_id, account_id)` (`models/live_deployment.py:54`). The active process partial unique index protects `deployment_id`, including `stopping`, rather than `account_id` (`models/live_node_process.py:169`). These prevent duplicate activity for the same deployment; they do not prohibit two different portfolio revisions/deployments on the same account.

The API locks the account for revalidation and checks revision/account identity collisions (`api/live.py:1655`, `:1712`), but the inspected start path does not check for a different active revision on that account. The supervisor checks existing activity by deployment (`live_supervisor/fleet_router.py:1690`). Its other-deployment guard is expressly a gateway startup guard: only `starting`, `building`, and `ready` block another start; a `running` deployment permits the next one (`:1856`, `:1875`). Final START dispatch calls that spawn path; the per-account consumer supplies command serialization, not an active-portfolio invariant (`live_supervisor/main.py:188`, `:238`).

With independently eligible members/candidates, starting portfolio Q after portfolio P is running on the same account is therefore not prevented by these visible guards. This is not proof that two such deployments currently exist. It also does not mean distinct accounts should block one another.

**Recommendation: repair the account assignment contract.** For the stated product, enforce one active portfolio assignment per account atomically across API and supervisor paths, with explicit replacement semantics. Define how `stopping`, failed processes with residual exposure, and restart/recovery retain that assignment. Link this to M18's broker-confirmed exposure boundary rather than assuming a terminal process means the account is empty. Multiple member strategies belong inside the portfolio; intentionally concurrent portfolios on one account would require a separately specified allocation model.

### GA-03 — P1: account selection is only partially propagated, and All can hide other deployments

**Source-confirmed; high confidence. The two-account browser scenario has not been executed.**

The selector is explicitly a persisted **view scope**, not an execution target (`frontend/src/lib/account-scope.tsx:6`, `:17`). The registry-aware selector distinguishes active, unknown/retired, and unassigned accounts (`frontend/src/components/layout/account-selector.tsx:27`). Explicit account targeting when starting is useful and should remain visible: CLI start accepts `broker_account_id` separately from legacy account settings (`backend/src/msai/cli.py:963`, `:1038`). A view selection should not silently authorize trading.

The view contract is nevertheless incomplete:

- Live status accepts `active_only`, not an account query, and returns at most 50 all-status rows or 1,000 active rows (`backend/src/msai/api/live.py:3675`, `:3710`). Browser filtering cannot recover history omitted by the server limit.
- Live positions has no account/deployment selection parameter (`api/live.py:3906`). Trades accepts deployment selection only; returned rows omit strategy/account/portfolio identity (`:3990`, `:4029`).
- CLI status/positions and account summary/positions/health expose no account selection, while CLI trades accepts a deployment filter (`backend/src/msai/cli.py:686`, `:1065`, `:1248`).
- The dashboard deliberately keeps its gateway-bound summary unscoped, while filtering deployment-derived cards (`frontend/src/app/dashboard/page.tsx:45`, `:70`, `:82`). Recent trades fetches unfiltered trades and displays no account column (`frontend/src/components/dashboard/recent-trades.tsx:25`, `:75`). Existing labels can explain the boundary, but they do not implement a consistent selected-account workspace.

There is also an independent multi-deployment aggregation defect in the live page. It selects the first running deployment for one WebSocket (`frontend/src/app/live-trading/page.tsx:194`). When that socket opens, `positionsForTable` becomes only that deployment's stream positions, replacing the full REST list (`:203`). The `scope === all` branch returns that list directly (`:243`). The specific-account branch instead merges other REST positions with the streamed deployment (`:245`). Therefore a successful REST response containing A and B can become an All view containing A alone. The unavailable warning requires a failed REST response (`:279`), so this successful-REST case need not display that warning. Totals reduce the shortened list (`:287`). Non-streamed REST data is fetched at mount/token change, not continuously refreshed (`:160`, `:190`).

**Recommendation: repair one explicit scope contract.** APIs should accept stable account and deployment/portfolio/strategy identifiers where meaningful; CLI and UI should send the same selection and display the same scope/freshness. An All response must aggregate all covered accounts and clearly report missing/stale coverage. Merge the one deployment's stream into the broader snapshot rather than substituting it for the fleet. Keep gateway health visibly separate when it cannot provide selected-account data. This is distinct from M07's backend event-decoding defect and M09's access-control findings.

### GA-04 — P1: the live performance history is a skeleton, not an accounting ledger

**Source-confirmed; high confidence. Live accounting has not been reconciled against a broker in this audit.**

`workers/pnl_aggregation.py:45` counts filled/partially filled `OrderAttemptAudit` rows by strategy, deployment, and attempted date. For new `StrategyDailyPnl` rows it writes `pnl=0`, `cumulative_pnl=0`, `win_count=0`, and `loss_count=0` (`:83`). On existing rows it updates only the order-row count (`:80`). These are explicitly placeholders; the count is not a completed-trade or fill-quantity calculation.

The table's strategy/deployment/date identity is useful (`models/strategy_daily_pnl.py:29`), but production references found in this pass were its model and aggregator, not an API that supplies a completed performance history. The inspected account service supplies current gateway snapshots rather than persisted per-account equity/returns. Its account identity is populated only when the gateway exposes exactly one managed account (`services/ib_account_snapshot.py:492`). Summary requests do not select an account and reduce repeated tag values into one dictionary (`:518`); portfolio requests likewise do not filter by account and omit account/currency from each output row (`:535`). The service singleton uses global IB connection settings (`:567`). It deliberately returns unknown identity for a multi-account login instead of guessing, which is preferable but still incomplete for account switching.

The live page's All daily PnL is realized PnL from its single connected deployment, or zero without that stream (`frontend/src/app/live-trading/page.tsx:301`). It is not historical daily performance across the portfolio/account. Correcting that label or the event translator alone cannot supply the missing ledger.

**Recommendation: implement the minimum trustworthy performance contract.** Persist and reconcile fills, fees, positions, cash/equity marks, and cash movements with account → deployment/frozen portfolio → member strategy attribution. Define gross/net PnL, return denominator, currency, day boundary, mark timestamp, and missing-data behavior. Preserve portfolio/version history across redeployments instead of treating a process restart as a new track record. Account totals must include unexplained/manual exposure explicitly rather than silently attributing it to an arbitrary member. Derive strategy/portfolio results from that ledger and reconcile them back to account equity. These definitions are necessary to interpret the requested performance; advanced analytics can wait.

This extends M13's incomplete historical-performance statement and depends on M07/M18 and the partial-fill audit defects in [live-safety.md](live-safety.md). Existing local/Azure QuantStats reports are simulated research results, not evidence that this live ledger exists.

### GA-05 — P2: portfolio promotion has an obsolete paper-only target contract

**Source-confirmed; high confidence. No account availability or successful broker connection is assumed.**

Portfolio promotion requires an `account_id`, then rejects identifiers without the `DU` prefix with `PAPER_ONLY_ENFORCED` (`api/portfolio.py:454`, `:617`). The CLI advertises that same paper-account restriction (`cli.py:3383`). Materialization places the supplied account only in the new portfolio's description (`services/live/portfolio_service.py:340`); the composition itself has no account relation. Actual deployment later chooses and validates the account separately.

This creates two ambiguities for the desired workflow: promotion appears account-targeted although it creates reusable composition, and the existing route requires a paper-form identifier even though current project context does not establish a provisioned paper account. It is not evidence that the start path bypasses account checks, and no recommendation here authorizes relaxing controls on a live account.

**Recommendation: simplify promotion semantics.** Make validated portfolio materialization and explicit account assignment clear separate operations, or persist an intentional target assignment if that is the chosen product behavior. Reconcile the paper-only restriction with the actual approved test-account plan before end-to-end acceptance. Do not fabricate a `DU` identifier or change records to work around it.

### GA-06 — P2 for future scope: brokerage metadata is not an execution adapter boundary

**Source-confirmed; high confidence. Retain IB first; defer a second broker implementation.**

The current broker registry is specifically an IB account registry: `ib_account_id`, `ib_login_key`, gateway slot, trading mode, and TWS credential requirements (`models/broker_account.py:41`; `schemas/broker_account.py:22`, `:62`). Deployment identity also contains IB account/login inputs (`models/live_deployment.py:110`; `services/live/deployment_identity.py:236`). The actual node imports and registers `InteractiveBrokersLiveExecClientFactory`, with concrete IB execution configuration (`services/nautilus/trading_node_subprocess.py:2802`, `:2892`, `:2911`; `services/nautilus/live_node_config.py:1001`, `:1135`).

The security master's `Provider` choices (`interactive_brokers`, `databento`) describe instrument-resolution sources (`services/nautilus/security_master/types.py:22`). They do not make Databento a brokerage, and asset-class/provider metadata is not proof of a pluggable execution broker in this application.

**Recommendation: keep the working IB integration and make its boundary explicit.** A later broker will require provider-neutral account identity plus provider-specific connection/credential settings, instrument/order capability mapping, execution registration, reconciliation, and acceptance tests. Reuse an appropriate Nautilus adapter where available, but verify its installed-version contract. Avoid a speculative generic broker framework before the IB portfolio/account workflow is accepted. The durable domain model should not assume that every account identifier or connection is an IB gateway.

## Minimum two-account, multi-strategy acceptance scenario

This is a future acceptance specification, **not an executed test or authorization to trade**. Begin with deterministic offline/fake-boundary tests and read-only fixtures. Any later broker exercise must use freshly verified, explicitly authorized test accounts; a paper account is not assumed to exist. Existing M23 account identity and M21 supervisor compatibility must be resolved first.

1. **Reusable evidence and composition.** Establish approved immutable evidence for strategies S1, S2, and S3 after the leakage/accounting repairs. Freeze P = {S1, S2} and Q = {S1, S3}, with explicit configuration, instruments, weights, capital budget, costs, and code/data references. Editing a draft must not change either revision or its evidence.
2. **Same portfolio, different accounts.** Assign the same P revision to A and B, using each account's verified identity and explicit budget. Both deployments retain the same validation references and distinct account/deployment identities. Starting B must not relink or alter A's candidate/evidence or running state. Evidence reuse must not imply equal capital or order quantity.
3. **Different portfolios per account.** Replace B's P with Q through the intended stop/reconcile/replace path. A remains on P. Attempting a second active portfolio on A must fail atomically before a command/order, including concurrent requests and an existing `stopping` assignment. Demonstrate recovery from a failed process with residual exposure without treating it as a free account slot.
4. **Effective allocation and isolation.** In controlled execution fixtures, verify quantities against each member's weight, account budget, symbol rules, and risk controls. Include two member strategies holding the same instrument with opposite directions. Preserve signed strategy positions, their portfolio aggregate, and the account's broker-net position as distinct views. An account A halt/stop must leave B's intended operation unaffected; fleet halt must cover both.
5. **Identical API/CLI/UI scope.** Select A, B, then All through each surface. Compare the same account/deployment/revision/member IDs, positions, trades, and coverage timestamps. Open a WebSocket for A while B changes: All must retain both and B must refresh or show its staleness. Include more history than the default status-page limit and a failed/stopping deployment with residual exposure.
6. **Account-to-strategy performance reconciliation.** Use a known fill/cashflow sequence including partial fills, fees, a close, and an external cash movement. Verify daily and cumulative net PnL, equity, returns, and drawdown for each account, portfolio revision, and member; do not count deposits as investment return. Account changes and process restarts must preserve history and attribution. Explicitly report any broker/manual exposure outside the deployed portfolio.
7. **Restart and reconciliation.** Exercise replay, reconnect, and restart through the installed adapter contract. No duplicate order or duplicate fill accounting; no silent candidate reassignment; no false flatness from an empty/stale cache. Reconcile account-wide broker exposure before claiming the account is clear for replacement.

The acceptance output should be a small reproducible evidence bundle: frozen identities/configurations, expected and observed accounting, API/CLI/UI scope comparisons, and lifecycle outcomes. A healthy process, successful job, or attractive chart alone does not satisfy these checks.

## Bounded recommendation and validation record

**Keep** account-independent compositions, immutable revisions, explicit account deployment, Nautilus execution/reconciliation, account halt controls, and the API-first structure. **Repair** evidence cardinality, account exclusivity, allocation/risk wiring, projection identity, query scope/freshness, and performance accounting. **Simplify** promotion and the overlapping account-snapshot/read-model meanings. **Defer** a second broker adapter, more optimizers, new infrastructure, and advanced reporting until the IB two-account scenario is accepted.

This pass used bounded read-only `rg`, `nl`, and `sed` inspections of the paths cited above, plus the existing audit reports and `MASTER_MAP.md`; `git status --short` confirmed concurrent documentation edits that were left intact. No API/CLI execution, database/Redis query, container action, external connection, secret retrieval, or new tests was performed. The prior live report's 79 passing offline tests and actual Nautilus 1.223.0 event probes remain scoped to that report; they do not validate the additional two-account contracts identified here. The missing acceptance evidence is stated explicitly rather than inferred from existing simulations.
