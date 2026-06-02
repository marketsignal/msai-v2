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

- **PR 1 — shared-login paper sub-account drill (control-plane proof).** ONE `ib-gateway` container under `marin1016test` paper login + TWO TradingNodes with distinct `ibg_client_id`s, account-scoped via `InteractiveBrokersExecClientConfig.account_id="DUP733214"|"DUP733215"`. `gateway_session_key` propagation, `LiveCommandBus` namespacing, halt split (account-scoped + explicit fleet emergency), drain/restart, Databento data client wired, symbology shim, paper drill green end-to-end (deploy to A + B, paper order each, drain one, emergency-halt). See 2026-05-29 council addendum below for the topology shape ratification + minority report.
- **PR 1b — data-stale safety.** Per-node freshness observability + data-stale auto-halt + multi-failure-mode test harness (disconnect, stale-timestamp, partial-dataset stall, single-symbol stall, reconnect storm). Does NOT block PR 1 merge or the paper drill, but **blocks any real-money N-account graduation** (i.e., LVP/HVP) — blocking objection #9 binds at the graduation gate, not at PR 1 merge.

PRs 2–6 are unchanged.

### Operator prerequisites (revised)

- ~~Second KV TWS secret pair~~ — **still required for the LVP/HVP graduation drill** (PR 1's paper drill uses `marin1016test` sub-accounts under one TWS login, so two-secret provisioning is deferred to the LVP/HVP step).
- ~~Per-username IB market-data entitlement, CME funded-master gate~~ — **eliminated for data purposes.** IB market-data subscriptions are no longer fleet prerequisites. (Still relevant for execution-side margin & order acceptance.)
- ~~Second compose gateway service + volume + socat ports~~ — **NOT required for PR 1** (superseded by 2026-05-29 council addendum: PR 1 is a shared-login paper sub-account drill). Still required at LVP/HVP graduation — distinct TWS logins force distinct containers.
- **NEW:** Databento live subscription covering the symbols the drill trades (equities first per the council's "stocks/FX paper-drill" framing). Confirm subscription status before PR 1.
- **DEFERRED (Codex P2 #9):** OPRA real-time chain-load + throttle behavior is explicitly out of scope for PR 1 / 1b (equities-first). A separate OPRA capacity spike is required before options live trading.

### Provenance

Pablo + agent, 2026-05-28. Verification of Nautilus adapter availability done in-session against `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/`. Codex review pass complete (gpt-5.5, xhigh) — 0 P0, 5 P1 applied inline (per-node detection vs fleet-wide action; session-aware freshness vs raw 30s; halt-cause metadata + recovery gates; bidirectional dataset-aware symbology shim; expanded failure modes in objection 9), 4 P2 applied or resolved (audit-metadata preservation, labeled observability, PR 1 split into PR 1 + PR 1b, OPRA deferred to post-equities).

---

## Addendum 2026-05-29 — PR 1 topology under the one-TWS-login constraint

- **Status:** Accepted (Engineering Council verdict, 2026-05-29, CONDITIONAL APPROVE).
- **Council:** 5 advisors (Simplifier, Scalability Hawk, Pragmatist — Claude; Contrarian, Maintainer — Codex gpt-5.5 high) + Codex chairman (gpt-5.5, xhigh). Tally: 2 APPROVE, 2 CONDITIONAL, 1 OBJECT. Verdict: CONDITIONAL APPROVE Shape A with explicit scope-of-proof boundary and a hard pre-LVP/HVP graduation gate.
- **Trigger:** `research-first` brief at [`docs/research/2026-05-28-multi-account-broker-fleet.md`](../research/2026-05-28-multi-account-broker-fleet.md) Risk #1 surfaced that the PRD's "two IB Gateway containers" topology is mechanically incompatible with the addendum-2026-05-28's "one TWS login (`marin1016test`)" prerequisite — IB allows one session per username; two containers with the same login fight via `EXISTING_SESSION_DETECTED_ACTION=primary`.

### Decision

**Shape A — ONE `ib-gateway` container + TWO TradingNodes.** PR 1 uses one paper IB Gateway container under `marin1016test`, with two TradingNodes connected via distinct `ibg_client_id`s (Nautilus gotcha #3). Per-account routing happens via `InteractiveBrokersExecClientConfig.account_id="DUP733214"` vs `"DUP733215"` on `placeOrder`.

PR 1 is renamed in the slicing section to **"shared-login paper sub-account drill"** to make the scope-of-proof boundary explicit. PR 1 proves the control-plane: account-scoped TradingNode lifecycle, `gateway_session_key` propagation, `LiveCommandBus` namespacing, halt split, drain/restart, Databento+IB-exec wiring, bidirectional symbology shim. PR 1 does NOT prove independent IB Gateway container failure domains — that property is bound to the pre-LVP/HVP graduation gate below.

### Hard pre-LVP/HVP graduation gate (institutionalized minority report)

The Scalability Hawk objected: Shape A hides the highest-risk production failure mode — gateway-as-blast-radius (one IB Gateway OOM / crash / 2FA prompt / relogin issue / volume-misconfig affecting both accounts). The chairman overruled for PR 1 because Shape B is mechanically blocked, but **did not dismiss** the objection. It is preserved here as a load-bearing gate.

**Before any real-money N-account deployment** (LVP/HVP graduation drill), the following MUST be proven:

1. Two distinct TWS logins active concurrently (`marin1016test` + a second login — likely `mslvp000` for the live test path).
2. Two `ib-gateway` containers running side-by-side, each with its own `TWS_USERID`/`TWS_PASSWORD`, distinct `GATEWAY_CONFIG` entries, distinct volumes (`tws_settings`), distinct host ports + socat proxies.
3. Independent drain/restart per container — drain account A's gateway without touching account B's gateway.
4. Crash isolation: kill one container mid-flight; confirm the other continues to receive orders and reconcile correctly.
5. Per-container 2FA / relogin behavior verified.

Until those five conditions are demonstrated in a documented drill, **no real-money fleet deployment is authorized.** This is the operationalized form of the Hawk's OBJECT.

### Blocking objections from the 2026-05-29 council (must clear before PR 1 ships)

10. **PRD/addendum language amendment.** The PRD and operator-prereqs list must NOT claim "two IB Gateway containers" or port `4006` under one login for PR 1. (Maintainer + Contrarian, applied at the PRD level inline with this addendum.)
11. **PR 1 must explicitly assert in its acceptance gate** that two different `account_id`s under one `ib_login_key` use distinct `ibg_client_id`s, and that stopping TradingNode A does NOT disconnect TradingNode B from the shared gateway. (Contrarian.)
12. **`gateway_session_key` propagation through command payload → `handle_command()` → `ProcessManager.spawn()` is non-negotiable for PR 1.** Already objection #2 from the 2026-05-27 council; reinforced here. The per-session startup guard must not silently degrade back to global serialization. (Contrarian.)
13. **Fail-fast validation at config load time:** two configured gateway sessions (in `GATEWAY_CONFIG` or successor) cannot share the same `ib_login_key`/TWS login. The check fires at backend startup. (Maintainer.)
14. **Synthetic tests must exercise the multi-login `GatewayRouter` path even though PR 1 runs single-login.** `_build_production_payload_factory()` only uses `ib_login_key` routing when `gateway_router.is_multi_login`; without a synthetic multi-login test, PR 1 can pass while the multi-login route table is silently undertested. (Contrarian.)

### Risks accepted under Shape A (named, not dismissed)

- One IB Gateway crash kills both PR 1 paper accounts. Acceptable on paper; **NOT** acceptable at LVP/HVP — gated by the pre-graduation drill above.
- The supervisor's account-scoped drain is "proven" by draining one of two TradingNodes against a shared gateway; this is semantically different from draining one container against a separate container. Gap closed at the pre-LVP/HVP drill.
- `gateway_session_key` propagation lands at PR 1 but is exercised against a degenerate 1-key namespace; bugs that only manifest with ≥2 distinct keys may surface later. Mitigation: the synthetic multi-login test in blocking objection #14.
- Compose-level multi-gateway plumbing (ports, volumes, socat proxies, healthchecks for N gateways) is deferred; debugged later under PR 3 / LVP-HVP schedule pressure rather than PR 1's paper safety net.

### Missing evidence (gaps the council could not assess)

- Current implementation state of `gateway_session_key` propagation through `handle_command()` → `ProcessManager.spawn()`. (Phase 3 plan-writing must inspect `backend/src/msai/services/live_command_bus.py` and `live_supervisor/process_manager.py` and account for the actual current state.)
- Whether any existing synthetic test exercises multi-login `GatewayRouter` behavior. (Phase 3 plan-writing must inspect `backend/tests/` and either reuse or author the missing coverage.)
- Real operator timeline to procure a second paper TWS login. (Not relevant to PR 1 under Shape A, but informs LVP/HVP graduation sequencing.)

### Provenance

Engineering Council, 2026-05-29. Standalone `/council` invocation during `/forge-goal` autonomous run on `/new-feature multi-account-broker-fleet`. Raw advisor transcripts captured at session time. Engine diversity: 3 Claude advisors + 2 Codex advisors + Codex chairman (xhigh).

---

## Addendum 2026-05-30 — Forward plan after PR 1 ship + PR 3 credentials council

PR 1 shipped 2026-05-30 as **PR #84** (`feat/multi-account-broker-fleet`). Shape A control plane + Shape B compose foundation + 2026-05-30 18:55 CT operator drill: multi-container topology + halt-key isolation verified end-to-end on LVP+HVP. UC matrix: Negative UC PASS, UC4 PASS, UC5 FIXED, UC1/UC2/UC3 PASS (Shape B at API+Redis level — fill verification deferred to next market session since drill ran weekend after-hours).

### Sequencing ratified (Pablo + council)

1. **PR 1 — DONE** (per-account control plane, halt-key consolidation, symbology shim, GatewayRouter, account-scoped drain, Shape B compose foundation).
2. **PR 1b — data-stale auto-halt** (deferred from PR 1 per Hawk minority report; gates LVP/HVP graduation).
3. **PR 2 — per-account supervisor processes.** Replace `ProcessManager.handles` shared map with one supervisor process per account. The Hawk's PR 3 council vote was explicit: "letting an operator add/remove accounts at runtime is safer with isolated supervisors." PR 2 lands before PR 3.
4. **PR 3 — BrokerAccount first-class entity.** Operator-facing CRUD: add/edit/archive an IB account via UI/CLI/API, no env-var edits. Credentials handling council below.
5. **PR 4 — Dashboard account selector** (UI fleet view + filters).
6. **PR 5 — Per-account hard caps + fleet-aggregate ledger** (risk overlays land here).

### PR 3 credentials handling — council ruling (2026-05-30, standalone /council)

- **Council:** 5 advisors (Simplifier, Scalability Hawk, Pragmatist — Claude; Contrarian, Maintainer — Codex `gpt-5.5`) + Codex chairman (`gpt-5.5`, xhigh).
- **Tally:** 4 of 5 advisors verdict **CONDITIONAL APPROVE Option B'** (DB stores secret reference + pinned KV version; backend writes credentials to Azure Key Vault server-side from UI/CLI/API POST payload — operator never opens an Azure portal tab). 1 advisor (Pragmatist) dissented in favor of Option A (DB-encrypted-at-rest with `MASTER_ENCRYPTION_KEY`).

#### Ratified decision

**Option B'.** The single UI/CLI/API operator flow accepts credentials in the POST payload. Backend writes them to AKV server-side using managed identity in production, or to `.env` via `EnvSecretsProvider` in dev. The `broker_accounts` row stores **only** `credentials_secret_ref` + `credentials_secret_version` (pinned KV version GUID) + `credentials_updated_at` + `credentials_updated_by`. GET endpoints never return the cleartext. The IB Gateway container reads `TWS_USERID`/`TWS_PASSWORD` from its own process env, materialized at spawn time from a fresh KV fetch.

#### Blocking objections (must land in the same PR as the storage)

1. **Backend writes to KV on POST.** Operators never call `az keyvault secret set`. (Simplifier, Hawk.)
2. **Dev fallback via `EnvSecretsProvider`** reading `TWS_USERID_<suffix>` from `.env`. **No local KV emulator** — keeps dev paths drama-free. (Hawk, Contrarian.)
3. **Pin `credentials_secret_version`** on the row (KV version GUID, not just the name) — rotation becomes an explicit DB UPDATE; audit trail preserved across versions. (Hawk, Maintainer.)
4. **Dedicated `BrokerCredentialsStore` interface** (or `WritableSecretsProvider`) — do NOT bolt write semantics onto the read-only `SecretsProvider`. Explicit `put(account_id) / get(account_id, version) / rotate(account_id) / delete(account_id)` semantics. (Contrarian, Maintainer.)
5. **Audit metadata on the row:** `credentials_updated_at` + `credentials_updated_by`. (Maintainer.)
6. **Mandatory alerts shipped same PR:**
   - `broker_account_spawn_failed{account_id, reason}` counter — reason ∈ `kv_unauthorized | kv_not_found | kv_throttled | kv_unreachable | decrypt_failed`
   - `kv_secret_age_seconds{account_id}` gauge — alerts when >90d (rotation enforcement)
   - `credentials_last_accessed` timestamp on the row — feeds ghost-account detection
   - Startup boot-time KV reachability probe — fail-closed if KV unreachable AND any active deployment exists
   - (Hawk, Maintainer.)
7. **Fail-LOUD mapping:** credential resolution errors must surface as `SPAWN_FAILED_PERMANENT` with actionable error messages naming the missing/invalid account + secret reference. NOT generic provisioning failures. (Hawk, Maintainer, prior council 2026-05-27.)
8. **Account-deletion + rotation semantics specified before merge.** What happens to KV secret on `archive`? Hard delete? Soft delete with TTL? Define before shipping. (Contrarian.)

#### Minority Report (Pragmatist, Option A)

The Pragmatist favored Option A (DB-encrypted-at-rest with `MASTER_ENCRYPTION_KEY`) on velocity grounds (~2-3 days vs ~3-4 days) and threat-model symmetry (single-VM compromise = total compromise either way; KV's audit/rotation advantages don't apply to a solo dev today).

**Overruled because:** the Pragmatist did not re-evaluate after Option B' was named during deliberation — their primary objection (Option B's out-of-band operator KV write breaking Pablo's UX) is resolved by B'. The majority preserved B' because:

- KV-grade audit + rotation map to the eventual multi-user state; Option A would require re-architecting encryption later.
- Master-key loss is unrecoverable; KV-version pinning preserves prior-version recovery.
- Existing `SecretsProvider` abstraction in `backend/src/msai/core/secrets.py` already handles both backends — B' adds no new infrastructure surface in prod.

The speed concern (2-3 days vs 3-4 days) is deferred to implementation scope control, NOT used to change the storage decision.

#### Fourth option (Contrarian) — envelope encryption

Stored credentials_encrypted in DB, with the encryption key itself coming from the SecretsProvider (KV in prod, env in dev). Single POST flow, avoids per-account KV write lifecycle.

**Status:** Documented but not adopted. Coherent fallback if writable-KV integration spike (see Missing Evidence) finds a blocker. Re-evaluate before merge if blockers surface.

#### Static-pool provisioning (Pablo's choice)

The compose ships N predefined `ib-gateway-{1..N}` services. Provisioning a new account = allocate a free slot + restart that service with the new login env (read from KV via supervisor-side init). Caps N at the pool size (proposed: 10). Pool size raised by editing compose; not runtime-dynamic. **Rejected alternatives:** docker-py spawn (security surface — docker socket access), Kubernetes operator (overkill for single-VM).

#### Sub-PR split for PR 3

Per the Pragmatist's flattening recommendation:

- **PR 3a** — `broker_accounts` table + migration + CRUD API + `BrokerCredentialsStore` (with `EnvSecretsProvider` + `AzureKeyVaultProvider` adapters) + API-level E2E UC. (~1.5 days target.)
- **PR 3b** — CLI sub-app + UI wizard + 2 surface UCs (CLI, UI). (~1 day target.)

### Missing evidence (PR 3 council called out for pre-merge spike)

- Writable KV integration spike: prove `SecretClient.set_secret` + version pinning + read-back works cleanly with the existing managed-identity setup. Estimated effort: < 30 min.
- Concrete failure behavior for KV outage / missing version / deleted secret / bad permissions — each maps to specific `SPAWN_FAILED_PERMANENT` reasons.
- Final naming/schema confirmation for `credentials_secret_ref`, `credentials_secret_version`, audit columns.
- Rotation + deletion lifecycle tests.

### Pending operator tasks (post-PR-1, pre-LVP/HVP graduation)

- IB Account Management → Trading API permissions on **U4715997 (HVP)** — currently Read-Only API blocks order placement (drill 2026-05-30 surfaced this via IB error 321).
- IB Client Portal → terminate stuck `marin1016test` primary-override session — unlocks the original Shape A paper sub-account drill if desired in addition to Shape B.
- Re-run the Shape B drill with **fills** during market hours (Monday 8:30 AM CT or later) — adds the fill-round-trip evidence that this weekend's drill couldn't capture.

### Provenance

PR 3 credentials council: Engineering Council, 2026-05-30 (standalone `/council`). 4-1 majority for Option B' with overlapping CONDITIONAL constraints. Pragmatist minority report preserved per protocol. Codex advisors on first dispatch misread the question and answered the Shape A/B topology question; re-dispatched with tighter prompts and produced clean credentials-focused verdicts on retry. Chairman synthesis on first attempt also drifted to topology; retry with tight prompt produced the verdict captured here. Raw transcripts at `/tmp/council_{contrarian2,maintainer2,chairman2}_response.txt` (session-local).

---

## Addendum 2026-05-31 — PR 2 supervisor topology + binding deploy contract (council #2)

Two councils fired during PR 2 design. The first (Shape A vs B) chose "Shape B refined — one process, per-account supervisor actors, fenced authority." The **plan-review loop then surfaced a P0** that forced a second council: the chosen in-process design could not satisfy US-1's redeploy clause because of the container topology. Council #2 (2026-05-31, full 5-advisor) ruled **Opt 4 refined**, materially simplifying the design.

### Verified facts that drove council #2

- **F1-F3:** `live-supervisor` runs `command: python -m msai.live_supervisor` as the container's PID-1 process (`restart: unless-stopped`, no `init:true`); TradingNodes are `mp.Process` children spawned in-process (`process_manager.py:579`), in the SAME container. So supervisor/PID-1 death OR a container recreate kills ALL co-located nodes — `os.setsid`/`init:true` cannot save co-located nodes from a container recreate.
- **F4 (decisive):** `scripts/deploy-on-vm.sh` `pull` + `up -d --wait` operate ONLY on `DEFAULT_PROFILE_SERVICES` (line 44). `live-supervisor` is behind `profiles:["broker"]` and is EXCLUDED. A routine push-to-main deploy does NOT recreate the supervisor container.
- **F5 (decisive):** `.github/workflows/deploy.yml:399` "Refuse if active live_deployments" — the deploy workflow fails closed (before OIDC) if backend reports any active live deployment.
- **F6 (unanimous P0):** node-side halt is a CACHED boolean (`RiskAwareStrategy._halt_flag_cached`); `_refresh_halt_flag_fn` is declared but not wired to any poll loop; the order-submit path never re-reads Redis. A halt set while the supervisor is down does NOT reach a running node's order loop.

### Ruling — Opt 4 refined (1 APPROVE-equivalent + 4 CONDITIONAL, 1 OBJECT resolved)

**PR 2 ships:**

1. **Per-account ownership SEMANTICS in the existing single supervisor** — NOT in-process actor-fencing, NOT per-account containers. Per-account command streams (`command_stream_for_account`, pre-built PR 1) give failure containment (one account's poison command can't stall others). The reaper + a startup re-scan of `failed` rows drive halt-latch-gated auto-restart with a bounded crash-loop guard (DB-backed counter, backoff, max-concurrent-respawn=1).
2. **Node-side live halt re-check (F6 fix)** — the running node re-reads `fleet_halt_key` + `account_halt_key` on the order-submit path, fail-closed on Redis error. Real-money P0; corrects the 4-layer-kill-all "cached Layer-4" overstatement.
3. **Binding production deploy contract (documented + enforced):** routine deploys exclude the `broker` profile (F4); the deploy workflow refuses while any live deployment is active (F5); broker-profile image changes are deliberate, sequenceable maintenance only. Documented in PRD + this decision doc + the deploy runbook + the release checklist.
4. **Per-account supervisor/restart-authority health** on `/live/status` + `msai live status` (US-3), honest `FleetRouter`/`NodeHandleCache` naming (was `ProcessManager`/`handles`), account-scoped logging.

**PR 2 explicitly DEFERS (to the 2-VM split / per-account-container phase):**

- Per-account containers (**Opt 1** — the ratified long-term container-per-account vision, see the 2026-05-27 verdict). It is a launch-model rewrite (nodes become containers launched by the orchestrator, not `mp.Process` children of the router) with material 16GB-VM OOM risk.
- True supervisor-container redeploy isolation (only per-account containers or the 2-VM split provide it).

### Shape-A / per-account-container migration trigger

This deferral is NOT permanent — it is gated on the conditions below. Revisit (and migrate from the single-supervisor "Opt 4 refined" shape to per-account containers / **Opt 1**) when **ANY** of these fires:

1. **The 2-VM split** — when real-money multi-account production moves off the single `Standard_D4ds_v6` to the 2-VM topology (coupled decision #3 in the 2026-05-27 verdict), per-account containers land there because that's where memory headroom + per-container rollout semantics exist.
2. **Fleet growth beyond ~4-5 accounts on one VM** — the 16GB single-VM OOM risk that motivated the deferral becomes real; co-locating N≥~5 TradingNode `mp.Process` children in one container exhausts the budget.
3. **A deliberate-redeploy incident the deploy contract didn't cover** — if the binding deploy contract (routine deploys exclude the `broker` profile, F4; active-live deploy refusal, F5) is ever bypassed and a broker-profile redeploy recreates the supervisor container while nodes are live, the lack of true per-container redeploy isolation has bitten us — migrate.

Cross-reference: PR 2 implementation plan `docs/plans/2026-05-31-pr-2-per-account-supervisors.md` (`## Approach Comparison` → "Opt 1 — per-account containers", and US-1 / T7 / T11 honest-scope notes). PR 2 ships the single-supervisor shape; this trigger records WHEN the deferred per-account-container migration should be picked back up so the deferral isn't silently forgotten.

**REJECTED for PR 2:** the in-process `AccountSupervisor + SupervisorLease + RestartBudget + generation-token` actor-fencing architecture from the first PR-2 council. It adds machinery without delivering the deploy isolation it appeared to (the container is the real boundary), and the eventual per-account-container architecture would discard it. The fencing existed to prevent two-supervisor split-brain during deploy overlap — but F5's active-live deploy refusal already prevents that overlap.

### Minority Report (council #2)

- **Contrarian OBJECTed** — the design relied on undocumented deploy discipline + a stale node-side halt. RESOLVED: PR 2 makes F4/F5 a binding documented contract and fixes F6. His own proposed resolution ("default deploy never touches broker + active-live gate fails closed") is exactly the verified system behavior.
- **Scalability Hawk + Maintainer pushed Opt 1** (per-account containers — the honest Docker isolation boundary, matches the documented long-term vision). DEFERRED, not rejected: F4/F5 remove the routine-deploy blast radius that motivated it now; it lands at the 2-VM split where memory + rollout semantics support it.

### Provenance

Engineering Council #2, 2026-05-31, during `/forge-goal` autonomous `/goal` run on PR 2. 3 Claude advisors (Simplifier, Scalability Hawk, Pragmatist) + 2 Codex advisors (Contrarian, Maintainer) + Codex chairman (xhigh). Escalated from the Phase 3.1c gate after the plan-review loop surfaced the container-topology P0. Verified facts cross-checked against the live codebase before the council reasoned.
