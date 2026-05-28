# Decision: IBKR Multi-Account Broker Fleet — Architecture

- **Status:** Accepted (Engineering Council verdict, 2026-05-27)
- **Decision owner:** Pablo
- **Council:** 5 advisors (Simplifier, Scalability Hawk, Pragmatist — Claude; Contrarian, Maintainer — Codex `gpt-5.5`) + Codex chairman (`gpt-5.5`, xhigh). All 5 returned CONDITIONAL.
- **Supersedes/extends:** the single-`IB_ACCOUNT_ID` env-var pattern in `CLAUDE.md`; memory `project_multi_account_broker_fleet_vision.md`.

## Context

MSAI v2 must manage **N IB accounts simultaneously in production** — any portfolio deployable to any account, with a UI/API/CLI account selector and per-account real-time views. This is a real-money hedge-fund platform: **reversibility and blast-radius dominate cloud cost.**

**Primary fork:** how to run N simultaneous IB accounts?

- **Option A — container-per-account:** N `ib-gateway` containers, each holding exactly one TWS session, each fronting its own account-owned TradingNode execution unit.
- **Option B — Nautilus multi-venue in one process:** a single TradingNode with N `LiveExecClient` connections (distinct `client_id`s + venue aliases).

**Ground truth that drove the decision:**

- **Nautilus 1.223.0** (verified from venv source): **no failure isolation between venues in one node** and **no per-strategy cache namespace** (issue #3176, unfixed). `FailureIsolatedStrategy` wraps event handlers but does not isolate a whole venue/exec-client failure.
- IB: **one login per TWS session** — a session cannot be shared across accounts.
- Market-data subscriptions are **per-username**, not per-account.
- Prod VM today: a single **Standard_D4ds_v6** (4 vCPU / 16 GB). Compose resource limits already sum to >16 GB before any broker fleet.
- **The codebase already has partial Option-A seams** (discovered during the council): `GatewayRouter` (`backend/src/msai/services/live/gateway_router.py`, parses `GATEWAY_CONFIG=login:host:port,...`), `LiveDeployment.ib_login_key` (indexed) + `account_id` + `gateway_session_key`, per-login routing in `live_supervisor/__main__.py`. **But** `ProcessManager.handles` is one in-memory map owning **all** child processes; `handle_command()` does not pass `gateway_session_key` into `ProcessManager.spawn()` (startup serialization is effectively global); `LiveCommandBus` is global (`msai:live:commands`); `_HALT_KEY = "msai:risk:halt"` is a single global key (kill-all is fleet-wide).

## Decision

**Option A — container-per-account.** One IB Gateway container + one account-owned TradingNode execution unit per IB login. **Reject Option B for real money** until Nautilus provides real per-venue failure isolation and cache namespaces (issue #3176).

**Supervisor ruling (the crux):** **do NOT use one shared supervisor that parents all TradingNode subprocesses.** The production unit must be account-scoped. A shared fleet process may exist only as a **thin control-plane router/API facade** — it must not own all child process lifetimes. Each account gets its own supervisor ownership boundary, command namespace, logs, health, halt, drain, and restart path.

### The four coupled decisions

| #   | Question          | Ruling                                                                                                                                                                                                                                                                    |
| --- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Supervisor model  | **Per-account supervisors** own TradingNode lifetimes. A shared router is allowed ONLY if it does not parent/lifetime-manage all nodes.                                                                                                                                   |
| 2   | Deploy/boot order | **Rolling account drain is mandatory.** Account-scoped start/stop/restart/drain/rollback/health. Fleet-wide "stop all" is an explicit emergency panic only — never the normal path.                                                                                       |
| 3   | 2-VM split timing | **Accelerate before real-money multi-account production.** A single D4ds*v6 is acceptable for a 2-account \_paper* drill, not as the N-real-money target without measured capacity + an admission gate.                                                                   |
| 4   | Risk limits       | Per-account hard caps are natural under Option A. **Fleet aggregation must be a separate fail-closed ledger/service** — never implicit coupling through one Nautilus process/cache. Aggregate check must deny new risk-increasing orders if any account's state is stale. |

## Blocking objections — must clear before any real-money N-account deploy

1. Constrain/replace the shared `ProcessManager.handles` ownership so one supervisor restart cannot affect every account.
2. Pass `gateway_session_key` through `handle_command()` → `ProcessManager.spawn()`; startup serialization must be per account/session, not global.
3. Namespace `LiveCommandBus` — `msai:live:commands` cannot remain a global undifferentiated command surface.
4. Split halt semantics: account-scoped halt/drain **plus** explicit fleet emergency halt. `_HALT_KEY` is too blunt as the only mechanism.
5. Add account identifiers to **every** operator surface: status API, CLI, logs, metrics, DLQ, flatness reports, deploy output, health checks.
6. Validate gateway/account routing **declaratively at boot** and **fail closed** on missing/ambiguous `ib_login_key`/gateway config (the static `GATEWAY_CONFIG` string is too easy to misconfigure for real money).
7. Per-account observability: gateway-up probe, TradingNode heartbeat age, IB pacing-violation counter (IB errors 100/162/420), reconciliation-completed/freshness flag, open-position-while-node-down alert. Ship in the same PR as the fleet, not after.
8. Prove capacity: measured RSS/CPU for each gateway + TradingNode under live market-data load before committing real money to a given N on a given VM.

## Minority Report

**The Simplifier** (and partly **The Pragmatist**) argued to keep the existing shared, namespaced supervisor — the code already leans that way, and the smallest useful slice is a 2-account paper fleet, not a supervisor redesign.

**Ruling:** valid for _sequencing_, **overruled for production architecture.** The shared supervisor is not merely a router today — it owns child process handles, making it a fleet-wide failure domain that directly conflicts with "never stop all at once." The phasing advice (paper drill first) is **accepted** for the first PR; the shared-supervisor model is **not** accepted for real-money N-account operation.

## Missing evidence (resolve with spikes/measurement before real money)

- Actual memory + CPU for one `ib-gateway` container per account, and one TradingNode per account under representative strategy + market-data load.
- N=2 and N=3 concurrent paper sessions on the current D4ds_v6 (where does it break first — almost certainly RAM).
- Supervisor restart behavior while child TradingNodes hold positions.
- IB pacing + entitlement behavior across the intended usernames.
- Fleet-risk staleness behavior when one account's node is down or delayed.

## Skeleton plan (PR-slicing — refined in `/new-feature` Phase 3)

> Re-sequenced per the Pragmatist's accepted phasing: **prove the fleet end-to-end first, polish the control plane after.**

- **PR 1 — Two-account paper fleet drill (the proof).** Second `ib-gateway` container + `GATEWAY_CONFIG` + second KV TWS secret; fix `gateway_session_key` propagation (`handle_command` → `spawn`); namespace `LiveCommandBus` + halt state per account; account-scoped drain/restart primitive + explicit fleet emergency-halt. **Acceptance gate (drill):** deploy a portfolio to account A and to account B, both place a paper order, account-scoped drain/restart one without touching the other, then explicit emergency fleet halt — end to end on paper.
- **PR 2 — Per-account supervisor ownership boundary.** Replace the single `ProcessManager.handles`-owns-everything model so a supervisor restart cannot take down all accounts (per-account supervisor processes, or a thin router that does not parent node lifetimes).
- **PR 3 — `BrokerAccount` as a first-class entity.** Table + CRUD API + CLI sub-app + Settings UI + per-account KV credential convention (`TWS-USERID-<suffix>`/`TWS-PASSWORD-<suffix>`). (Deferred below the drill on purpose — CRUD proves nothing about the fleet.)
- **PR 4 — Dashboard account selector + per-account read filtering** across all read endpoints (status, positions, trades, P&L).
- **PR 5 — Risk: per-account hard caps + separate fail-closed fleet-aggregate ledger.**
- **PR 6 — Per-account observability + alerting** (may fold into PR 1's drill scope for the safety-critical signals).
- **Capacity spike + 2-VM split** scheduled before any real-money N-account cutover (blocking objection #8 + coupled decision #3).

**External/operator prerequisites (not code-gated):** second KV TWS secret pair provisioned; per-username IB market-data entitlement (CME futures/options need a funded master — stocks/FX flow free, so paper-drill on stocks/FX first); the second compose gateway service + volume + socat ports.

## Provenance

Engineering Council, 2026-05-27. Standalone `/council` invocation. Raw advisor transcripts captured at session time (`/tmp/council_{simplifier,scalability_hawk,pragmatist,contrarian,maintainer}*`); chairman verdict reproduced verbatim above. Engine diversity: 3 Claude advisors + 2 Codex advisors + Codex chairman.

---

## Addendum 2026-05-28 — Databento live data + IB exec-only

- **Status:** Accepted (Pablo + agent, 2026-05-28). Codex review pass complete: 0 P0, 5 P1 (all applied inline below), 4 P2 (all resolved: audit-metadata preservation, labeled observability, PR 1 split into PR 1 + PR 1b, OPRA deferred to post-equities).
- **Scope:** Narrows the data-adapter choice the council left implicit. Does NOT alter the supervisor crux ruling, the rolling-drain mandate, the per-account hard caps + fleet-aggregate ledger requirement, or PR sequencing.

### Decision

**Split data and execution adapters:** Nautilus `TradingNodeConfig(data_clients={"DATABENTO": DatabentoDataClientConfig(...)}, exec_clients={"IBKR": InteractiveBrokersExecClientConfig(...)})`. Databento provides live market data for all N accounts; IB Gateway containers carry **only** order flow per account.

Verified in venv (`nautilus_trader` 1.223.0):

- `nautilus_trader/adapters/databento/data.py:88` — `class DatabentoDataClient(LiveMarketDataClient)`
- `nautilus_trader/adapters/databento/factories.py:114` — `class DatabentoLiveDataClientFactory(LiveDataClientFactory)`
- `nautilus_trader/adapters/databento/data.py:166` — uses `nautilus_pyo3.DatabentoLiveClient` (Rust-side live streaming)

This is a first-class Nautilus topology, not a workaround.

### Symbology — strategy-visible InstrumentId

**Strategies subscribe to `BarType("<SYMBOL>.IBKR-...")` unchanged.** A bidirectional symbology shim sits between strategies and the Nautilus Databento adapter. Concretely:

- **Outbound (strategy → Databento subscription).** Canonical strategy `InstrumentId` (e.g., `AAPL.IBKR`, `ES.IBKR`, `AAPL 240119C00190000.IBKR`) resolves to the correct native Databento subscription — `(dataset, native_symbol, native_venue)`, e.g., `(DBEQ.BASIC, AAPL, XNAS)`, `(GLBX.MDP3, ESH4, GLBX)`, `(OPRA.PILLAR, AAPL 240119C00190000, OPRA)`. The `IBKR` venue suffix alone cannot select between equity / futures / options datasets — the shim consults the **instrument registry** (`InstrumentDefinition` + aliases, PRs #32/#35) plus asset-class routing rules to pick the dataset.
- **Inbound (Databento native → canonical published).** Native bars/ticks arrive with their native `instrument_id.venue` (`XNAS`/`GLBX`/`OPRA`); the shim re-publishes them on the message bus tagged with the canonical `IBKR` venue suffix so strategies see what they subscribed to.
- **Audit preservation.** Provider (`databento`), dataset (`DBEQ.BASIC` / `GLBX.MDP3` / `OPRA.PILLAR`), and native venue MUST be retained in event metadata and any persisted catalog/audit records — never collapsed into the canonical `IBKR` tag alone. Backtest reproducibility and order-routing audit depend on the cross-venue truth.

This is the **compatibility-shim** shape of decision **(a)** in the design conversation. The mapping truth itself belongs in the registry/resolver layer, not the shim — the shim is the boundary translator, not the source of truth (per Codex P2 #7). Decision **(b)** — strategy-visible Databento-native IDs with registry mapping at order time — remains deferred. Migrate to (b) only if a forcing function emerges (e.g., dual-venue execution where the same symbol prices differently across venues we route to).

### Data-stale auto-halt — per-node detection, fleet-wide action

Each TradingNode wires its own `DatabentoDataClient` instance, so one node / dataset / subscription can stall independently — accounts do **not** necessarily go stale together. Detection must therefore be per-account/per-node/per-required-feed (preserving the original verdict's per-account observability requirement, blocking objection #5/#7); the halt action stays fleet-wide because the failure of any one node's required live feed leaves at least one account flying blind.

Mandatory in PR 1 (or PR 1b — see open question below):

- **Freshness is evaluated per active account/node and per required dataset/subscription**, with fleet-wide action if any required live feed is stale.
- **Freshness uses event timestamps plus expected interval and session-aware grace**, not a raw "last bar older than 30s". The default grace target is 30s _after the next expected data point_, subject to asset-class/session overrides (1-minute equities during RTH, lower-cadence pre-market, illiquid options, etc. all need distinct grace budgets). A flat 30s applied to 1-minute bars produces false halts by construction — every closing bar is naturally >30s old before the next bar exists.
- **On stale detection: set the same fleet emergency halt latch**, but carry distinguishing metadata: `reason=data_stale`, source account/node/dataset/symbol, `detected_at`, and `last_event_ts`. This is reuse of the latch code path (per the council's coupled decision #2 + blocking objection #4), not reuse of the cause attribution — operators must be able to tell a data-stale halt apart from a manual `/kill-all` panic so they don't clear one as if it were the other.
- **Recovery is explicit operator action only.** `/resume` must require **data-warm status verification AND reconciliation completion** before any account resumes order submission. No auto-resume on stale-clears.

### Effect on the original 8 blocking objections

| #   | Original blocking objection                                   | Status under split topology                                                                                                                                                                                                                                                                                                                                                                                                                   |
| --- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Constrain shared `ProcessManager.handles`                     | **Unchanged.** Still required.                                                                                                                                                                                                                                                                                                                                                                                                                |
| 2   | Pass `gateway_session_key` through `handle_command()`/`spawn` | **Unchanged.** Still required.                                                                                                                                                                                                                                                                                                                                                                                                                |
| 3   | Namespace `LiveCommandBus`                                    | **Unchanged.** Still required.                                                                                                                                                                                                                                                                                                                                                                                                                |
| 4   | Split halt: account-scoped + explicit fleet emergency         | **Reinforced.** The new data-stale auto-halt rides the explicit-fleet-emergency code path, so this objection's deliverable is now load-bearing for two callers, not one.                                                                                                                                                                                                                                                                      |
| 5   | Account identifiers on every operator surface                 | **Unchanged.** Still required.                                                                                                                                                                                                                                                                                                                                                                                                                |
| 6   | Declarative gateway/account routing validation at boot        | **Unchanged.** Still required — and the Databento data-client config gets the same boot-time fail-closed treatment.                                                                                                                                                                                                                                                                                                                           |
| 7   | Per-account observability incl. IB pacing errors 100/162/420  | **Modified, NOT relaxed.** IB error codes 100/162/420 are _market-data_ pacing — irrelevant when IB is exec-only. Replace with: Databento connection-health + per-node/per-dataset/per-symbol freshness signal (data-stale auto-halt input), IB exec pacing/throttling errors, fleet halt-cause attribution. Metrics MUST be labeled by account/node/dataset/symbol — a single fleet "Databento healthy" light is insufficient (Codex P2 #8). |
| 8   | Measured capacity proof before real-money N                   | **Unchanged but headroom likely grows.** IB Gateways carry no data subscriptions → lighter per-account RAM. Capacity spike still required; expect more favorable numbers.                                                                                                                                                                                                                                                                     |

### New blocking objection (must clear before any real-money N-account deploy)

9. **Data-stale fleet auto-halt** must exist, preserve halt-cause metadata (`reason=data_stale` + source account/node/dataset/symbol + `detected_at` + `last_event_ts`), fire under all of: simulated Databento disconnect, stale-timestamp feed (connection up but bars frozen in the past), partial-dataset stall (one dataset stalls while another flows), single-symbol stall while others flow, and reconnect-storm scenarios, and require explicit operator resume only after data-warm verification AND reconciliation completion. Ships in PR 1 (or PR 1b — see open question below). (Adds to the original 8.)

### Verification spike (cheap, must clear before PR 1 implementation begins)

**IB accepts order submission on an account that has no IB market-data subscription.** Strong prior: yes (TWS allows order flow on unsubscribed accounts; only quotes are gated). Cheap to falsify: place one paper market order on `DUP733213` (the known no-data sub-account from `reference_ib_entitlements.md`) via IB Gateway, observe acceptance + fill. If this fails, the entire split topology is invalid and we fall back to the council's original IB-data + IB-exec model — record the result either way.

### Effect on PR slicing (refines the council's PR 1 scope)

PR 1's deliverable is unchanged at the operator-visible level (2-account paper drill, deploy + paper order + drain + emergency-halt), but the _internal_ topology of PR 1 changes:

- Each TradingNode wires `DatabentoDataClientConfig` as its sole data client; no IB data client.
- Each IB Gateway container exposes only `InteractiveBrokersExecClientConfig` (no `InteractiveBrokersDataClientConfig`).
- One Databento `DATABENTO_API_KEY` lives in Key Vault, shared across accounts.
- Symbology shim (bidirectional, dataset-aware, audit-preserving — see "Symbology" section above) is in scope for whichever PR introduces the split topology.

**Decided: split PR 1 into PR 1 + PR 1b** (Pablo, 2026-05-28, accepting Codex P2 #6). The council's original PR 1 already had gateway topology + routing propagation + `LiveCommandBus` namespacing + halt split + drain/restart + paper drill; adding Databento wiring + symbology shim + per-node data-stale halt + multi-failure-mode test harness materially raises review risk, so split:

- **PR 1 — split topology proof.** 2nd IB Gateway exec-only, `gateway_session_key` propagation, `LiveCommandBus` namespacing, halt split (account-scoped + explicit fleet emergency), drain/restart, Databento data client wired, symbology shim, paper drill green end-to-end (deploy to A + B, paper order each, drain one, emergency-halt).
- **PR 1b — data-stale safety.** Per-node freshness observability + data-stale auto-halt + multi-failure-mode test harness (disconnect, stale-timestamp, partial-dataset stall, single-symbol stall, reconnect storm). Does NOT block PR 1 merge or the paper drill, but **blocks any real-money N-account graduation** (i.e., LVP/HVP) — blocking objection #9 binds at the graduation gate, not at PR 1 merge.

PRs 2–6 are unchanged.

PRs 2–6 are unchanged.

### Operator prerequisites (revised)

- ~~Second KV TWS secret pair~~ — **still required for the LVP/HVP graduation drill** (PR 1's paper drill uses `marin1016test` sub-accounts under one TWS login, so two-secret provisioning is deferred to the LVP/HVP step).
- ~~Per-username IB market-data entitlement, CME funded-master gate~~ — **eliminated for data purposes.** IB market-data subscriptions are no longer fleet prerequisites. (Still relevant for execution-side margin & order acceptance.)
- Second compose gateway service + volume + socat ports — **still required** for PR 1 (two IB Gateway containers, both exec-only).
- **NEW:** Databento live subscription covering the symbols the drill trades (equities first per the council's "stocks/FX paper-drill" framing). Confirm subscription status before PR 1.
- **DEFERRED (Codex P2 #9):** OPRA real-time chain-load + throttle behavior is explicitly out of scope for PR 1 / 1b (equities-first). A separate OPRA capacity spike is required before options live trading.

### Provenance

Pablo + agent, 2026-05-28. Verification of Nautilus adapter availability done in-session against `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/`. Codex review pass complete (gpt-5.5, xhigh) — 0 P0, 5 P1 applied inline (per-node detection vs fleet-wide action; session-aware freshness vs raw 30s; halt-cause metadata + recovery gates; bidirectional dataset-aware symbology shim; expanded failure modes in objection 9), 4 P2 applied or resolved (audit-metadata preservation, labeled observability, PR 1 split into PR 1 + PR 1b, OPRA deferred to post-equities).
