# PRD: multi-account-broker-fleet (PR 1 — shared-login paper sub-account drill, control-plane proof)

**Version:** 1.1
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-28
**Last Updated:** 2026-05-29 (post-council)

**Authoritative decision doc:** [`docs/decisions/multi-account-broker-fleet.md`](../decisions/multi-account-broker-fleet.md) — council verdict 2026-05-27 + Databento-data/IB-exec addendum 2026-05-28 + **PR 1 topology addendum 2026-05-29 (Shape A, CONDITIONAL APPROVE)**. This PRD does not re-litigate architecture; it scopes the user-visible deliverable for the first PR.

**Scope-of-proof boundary (2026-05-29 council mandate).** PR 1 is a **shared-login paper sub-account drill** — ONE `ib-gateway` container under `marin1016test` + TWO TradingNodes routed by distinct `ibg_client_id`s + `account_id`. PR 1 proves the **control plane** (account namespacing, halt split, drain isolation at the supervisor/TradingNode layer, Databento+IB-exec wiring, symbology shim). PR 1 does NOT prove independent IB Gateway container failure domains — that property is bound to the **pre-LVP/HVP graduation gate** in the decision doc (Shape B with two distinct TWS logins must pass before any real-money deployment).

---

## 1. Overview

MSAI v2 today runs one IB account at a time. The product vision (memory `project_multi_account_broker_fleet_vision.md`) is N IB accounts simultaneously in prod, each in its own container, with portfolios deployable per account and the operator able to drain or halt accounts independently. The council ratified container-per-account with **per-account supervisors** as the production unit, plus a Databento-live-data + IB-exec-only split topology that eliminates per-username IB market-data subscriptions as a fleet prerequisite.

**PR 1 proves the control plane end-to-end on paper**, without yet doing the supervisor ownership refactor (PR 2) or the data-stale safety harness (PR 1b). Two `marin1016test` paper sub-accounts (DUP733214, DUP733215) play the roles of accounts A and B; both are reached through ONE IB Gateway container (single TWS session, per the IB constraint that only allows one session per username). The two accounts are distinguished at the Nautilus layer by distinct `ibg_client_id`s and at the IB layer by `InteractiveBrokersExecClientConfig.account_id`. The operator deploys a portfolio to each, watches both place paper orders, drains one without touching the other, and triggers an explicit fleet emergency halt. If that drill is green end-to-end, the control-plane plumbing is proven and PR 1b unlocks the LVP/HVP graduation work (where Shape B — two distinct logins, two containers — must independently prove gateway-as-blast-radius isolation per the 2026-05-29 council's Hawk minority report).

Out of scope, by design: real-money accounts, per-account hard caps, fleet-aggregate risk ledger, per-account supervisor ownership, data-stale auto-halt, BrokerAccount entity + CRUD, dashboard account selector. Those land in PRs 1b–6 per the council's slicing.

## 2. Goals & Success Metrics

### Goals

- **G1 — Prove the split topology on paper.** An operator can deploy a portfolio to two IB paper accounts (A and B) simultaneously through the same MSAI v2 stack, both can submit paper orders independently, neither is using IB for market data, and the operator can drain one account or halt the entire fleet on demand.
- **G2 — Account identifiers visible on every operator surface.** Status API, CLI output, logs, alerts, deploy output, health checks all carry account scope so the operator never has to guess which account a line refers to (council blocking objection #5).
- **G3 — Databento live data flows into the Nautilus TradingNodes.** Strategies receive bars from Databento (re-tagged to the canonical `.IBKR` venue suffix via the symbology shim) and submit orders to IB Gateway, which is configured exec-only.
- **G4 — Fleet halt and account-scoped drain are demonstrably distinct.** An account-scoped drain on A does not affect B; the explicit fleet emergency halt stops both. The two paths must be callable separately and observable separately (council blocking objection #4 reinforced by addendum).
- **G5 — Boot-time fail-closed validation on gateway/account routing.** Misconfigured `GATEWAY_CONFIG` or missing Databento config fails the stack at startup, not at first bar event (council blocking objection #6).

### Success Metrics

| Metric                                                                                                             | Target                                                      | How Measured                                                                                                                                                                              |
| ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Two paper accounts deploy concurrently and each submit a paper order independently                                 | 100% of drill attempts pass                                 | Drill: deploy portfolio P-A to DUP733214, P-B to DUP733215, observe paper fill in each account's IB statement within the drill window                                                     |
| Account-scoped drain of A does not affect B                                                                        | 100% of drill attempts pass                                 | Drill: with both deployments live, drain A; verify A's TradingNode exits cleanly and B's TradingNode continues to receive bars / accept orders                                            |
| Explicit fleet emergency halt stops both accounts                                                                  | 100% of drill attempts pass                                 | Drill: with both deployments live, fire fleet emergency halt; verify both TradingNodes exit, both Redis halt keys are set, both report `reason=fleet_emergency` (NOT `reason=data_stale`) |
| Pre-PR-1 verification spike: IB accepts a paper market order on DUP733213 (no-data sub-account)                    | 1 successful fill                                           | Operator runs the spike per the decision doc's "Verification spike" section; spike must clear before Phase 5 implementation begins (gates the architectural assumption)                   |
| Operator surfaces show per-account context                                                                         | 100% of surfaces tested in the drill carry account ID/login | Drill checklist verifies status API response, `msai live status` CLI output, logs from each TradingNode subprocess, and any alerts all show `ib_login_key` or `account_id`                |
| Stack fails fast at boot on misconfigured `GATEWAY_CONFIG` (e.g., undefined login, port collision, missing secret) | Stack exits non-zero within 10s; clear log line names cause | Negative drill scenario: corrupt `GATEWAY_CONFIG`, restart stack, confirm exit + log                                                                                                      |

### Non-Goals (Explicitly Out of Scope — see decision doc §"Effect on PR slicing" for the full slicing)

- ❌ **No real-money accounts (LVP, HVP, live fund).** Paper drill only — LVP/HVP graduation requires PR 1b (data-stale safety) and PRs 5/6 (caps + observability) to land first.
- ❌ **No per-account hard risk caps, no fleet-aggregate ledger.** Deferred to PR 5.
- ❌ **No per-account supervisor ownership refactor.** The shared `ProcessManager.handles` map remains — PR 1 only namespaces the command/halt surfaces enough to drill account-scoped drain. The supervisor SPOF fix is PR 2.
- ❌ **No data-stale auto-halt, no per-node freshness observability.** Deferred to PR 1b. PR 1 proves topology; PR 1b proves safety on shared-Databento-SPOF.
- ❌ **No BrokerAccount first-class entity, no CRUD API, no CLI sub-app, no UI Settings page, no per-account KV credential convention.** All deferred to PR 3. PR 1 hardcodes two paper accounts in env / compose config.
- ❌ **No dashboard account selector, no per-account read filtering.** Deferred to PR 4. PR 1 surfaces account context on existing endpoints but doesn't reorganize the UI around it.
- ❌ **No options trading, no OPRA data subscription.** Equities-first per the council's "stocks/FX paper-drill" framing; OPRA capacity spike deferred per Codex P2 #9.
- ❌ **No 2-VM split.** PR 1 stays on the single D4ds_v6; capacity proof is deferred per council blocking objection #8.
- ❌ **No migration from the existing `ib_login_key` / `gateway_session_key` schema.** PR 1 propagates `gateway_session_key` through `handle_command()` → `ProcessManager.spawn()` (the existing seam is finished, not replaced).

## 3. User Personas

### Operator (Pablo) — primary, only persona in PR 1

- **Role:** Founding operator running MSAI v2 on the dev laptop and the prod Azure VM.
- **Permissions:** Full access — SSH to VM, Entra ID admin, repo write, deploy-workflow dispatcher, Azure Key Vault writer.
- **Goals for PR 1:**
  - Run the 2-account paper drill end-to-end on the dev stack and observe both accounts trading independently.
  - Confirm that draining account A doesn't kill account B (the council's "rolling drain" mandate, smallest demonstration).
  - Confirm that the fleet emergency halt stops both accounts at once and is reported distinctly from a data-stale halt (even though data-stale doesn't fire in PR 1).
  - Confirm that the Databento-live + IB-exec topology actually carries bars from Databento into the strategy and orders from the strategy into IB Gateway.
  - Catch a misconfigured `GATEWAY_CONFIG` at boot, not at first paper order.

PR 1 has no other personas. Dashboard users, automated alerts, multi-tenant scenarios — all deferred.

## 4. User Stories

Each story maps to one or more E2E use cases that will be designed in Phase 3.2b of the workflow. Stories are written in the operator's voice.

### US-1 — Run the 2-account paper drill end-to-end

> _As the operator, I want to deploy a portfolio to two IB paper accounts simultaneously and watch each place a paper order, so I can prove the split topology works before committing real money._

- The operator brings up the dev stack with ONE IB Gateway container (paper, `marin1016test` login) and Databento live wired as the sole data adapter. Both paper sub-accounts (DUP733214, DUP733215) reach IB through that single container, distinguished by their `account_id` on each order.
- The operator deploys portfolio P-A to account A (DUP733214) and portfolio P-B to account B (DUP733215) via existing `POST /api/v1/live/start-portfolio`.
- Each portfolio contains a `smoke_market_order`-style strategy that triggers a one-share paper buy on AAPL within the drill window.
- The operator observes one paper fill in each account's IB statement, both within the same drill window, with no cross-account interference.

### US-2 — Drain account A without touching account B

> _As the operator, I want to drain a specific account so I can take it down for maintenance or rollback without affecting other accounts._

- With both portfolios live, the operator calls the existing-with-account-scope drain endpoint for account A.
- A's TradingNode exits cleanly: positions reported, deployment row marked drained, no new orders accepted on A.
- B's TradingNode continues running: bars still arriving, deployment row still active, strategy still able to submit orders.
- The operator confirms via API + CLI that A is drained and B is not.

### US-3 — Fire fleet emergency halt and recover

> _As the operator, in a panic scenario I want to halt every account at once and clearly distinguish that halt from an automatic data-stale halt (even though data-stale isn't wired yet in PR 1)._

- With both portfolios live, the operator calls the explicit fleet emergency halt (`POST /api/v1/live/kill-all` or its account-aware successor).
- Both TradingNodes exit. Both Redis halt keys are set. The recorded halt cause is `reason=fleet_emergency` (matches the operator's intent, distinguishable from the PR-1b `reason=data_stale`).
- After the halt, recovery requires explicit operator clear (no auto-resume).

### US-4 — Boot fails fast on misconfigured `GATEWAY_CONFIG`

> _As the operator, I want a misconfigured gateway/account routing to break the stack at boot, not at first bar event, so I never deploy a half-configured fleet to prod._

- The operator corrupts `GATEWAY_CONFIG` (drops a login, adds a port collision, removes a referenced KV secret).
- `docker compose up` exits non-zero within 10 seconds with a log line that names the misconfiguration.
- After the operator fixes the config, the stack boots normally.

### US-5 — Verification spike (gate for everything else)

> _As the operator, before any of the above, I want to prove that IB Gateway accepts an order on an account with no IB market-data subscription, so the split topology is not built on a false premise._

- The operator brings up a single IB Gateway container against `DUP733213` (the known no-data sub-account from `reference_ib_entitlements.md`).
- The operator places one paper market order on AAPL through the gateway.
- The order is accepted and fills (paper fill).
- If this spike fails, PR 1 is cancelled and the council is re-opened on data-adapter choice.

## 5. Acceptance Criteria (the drill checklist)

PR 1 is acceptance-gated by the drill below. The drill MUST be run on the dev stack from this worktree (per `feedback_compose_must_cycle_from_worktree.md`) before the PR is opened (per `feedback_e2e_before_pr_for_live_fixes.md`).

| #   | Step                                                                                                                                                                                                 | Pass criterion                                                                                                                                                                                                                                                                                           |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0   | **Verification spike: paper market order on DUP733213 with no IB market-data subscription**                                                                                                          | 1 paper fill observed in IB's UI for DUP733213. (Gates everything else.)                                                                                                                                                                                                                                 |
| 1   | Bring up the dev stack from the worktree with ONE `ib-gateway` container (paper, exec-only, login=`marin1016test`) + Databento `DATABENTO_API_KEY` set + valid `GATEWAY_CONFIG` with 1 session entry | All containers `healthy`. Backend `/health` 200. Boot log names the gateway session and the two sub-accounts (DUP733214, DUP733215) explicitly. Fail-closed: a `GATEWAY_CONFIG` with two entries sharing the same `ib_login_key` must exit non-zero at boot (council 2026-05-29 blocking objection #13). |
| 2   | Deploy portfolio P-A (AAPL strategy, 1-share market) to account A (DUP733214) via `POST /api/v1/live/start-portfolio`                                                                                | 201 Created. `GET /api/v1/live/status` shows deployment for account A with `ib_login_key="marin1016test"`, `account_id="DUP733214"`, and a distinct `ibg_client_id` visible.                                                                                                                             |
| 3   | Deploy portfolio P-B (AAPL strategy, 1-share market) to account B (DUP733215) — same call, different account                                                                                         | 201 Created. `/live/status` now shows both deployments, each scoped to its own `account_id` and `ibg_client_id`; both share the same `ib_login_key`.                                                                                                                                                     |
| 4   | Wait for each strategy to trigger its paper order                                                                                                                                                    | Each account's IB UI shows exactly 1 paper fill; trades reconciled into the DB; `GET /api/v1/live/trades` shows both fills with `account_id` scope.                                                                                                                                                      |
| 5   | Account-scoped drain on A                                                                                                                                                                            | A's deployment transitions to drained; A's TradingNode exits; **B's TradingNode remains connected to the shared gateway** (verifiable via supervisor logs + IB Gateway connection count) and continues to receive bars (council 2026-05-29 blocking objection #11).                                      |
| 6   | Explicit fleet emergency halt                                                                                                                                                                        | Both TradingNodes exit; halt keys set in Redis with `reason=fleet_emergency`; `/live/status` shows both deployments halted.                                                                                                                                                                              |
| 7   | Negative test: corrupt `GATEWAY_CONFIG` and restart                                                                                                                                                  | Stack exits non-zero within 10s; log line names the misconfiguration. Includes the same-`ib_login_key` collision check from row 1 (council blocking objection #13).                                                                                                                                      |

E2E use cases for the verify-e2e agent (Phase 3.2b authoring) are derived directly from US-1..US-5; the drill above is the acceptance gate, the UCs are the regression artifact.

## 6. Operator Prerequisites

- ✅ **Databento LIVE subscription** covers AAPL + SPY equity OHLCV-1m — **confirmed active 2026-05-28**.
- ✅ **Compose ports.** Existing `4004` (host) → `4002` (internal paper) is sufficient. Only ONE IB Gateway container in PR 1 per the 2026-05-29 council Shape A ruling; no second port needed. The `4006` port previously proposed is **NOT** allocated for PR 1.
- ✅ **No new KV secrets required for PR 1.** Both paper accounts ride under the existing `marin1016test` advisor login. The second KV TWS secret pair stays deferred to the LVP/HVP graduation drill per the addendum's "Operator prerequisites (revised)".

## 7. Non-Goals — explicit reminders of what does NOT ship in PR 1

(Mirrors §2 Non-Goals, restated here so a reader who jumps straight to the acceptance gate doesn't miss them.)

- No real money.
- No per-account hard caps.
- No fleet-aggregate fail-closed ledger.
- No per-account supervisor ownership refactor.
- No data-stale auto-halt; no per-node freshness signals.
- No `BrokerAccount` entity, CRUD API, CLI sub-app, or UI Settings page.
- No dashboard account selector or per-account read filtering.
- No OPRA/options.
- No 2-VM split.
- **NEW — No multi-container gateway topology proof.** PR 1 uses one shared IB Gateway container. The independent-gateway-failure-domain property is deferred to the **pre-LVP/HVP graduation drill** per the 2026-05-29 council addendum's Hawk minority report (institutionalized as a hard gate before any real-money deployment).

## 7a. Scope-of-proof boundary (2026-05-29 council mandate)

**What PR 1 proves:**

- Two TradingNodes can be deployed simultaneously to two `account_id`s under one `ib_login_key`, each with a distinct `ibg_client_id`.
- Account-scoped drain on TradingNode A leaves TradingNode B's IB Gateway connection intact.
- Explicit fleet emergency halt sets account-scoped halt keys with `reason=fleet_emergency` and stops both nodes.
- `gateway_session_key` propagation through command payload → `handle_command()` → `ProcessManager.spawn()` works end-to-end (council 2026-05-27 blocking objection #2 + 2026-05-29 blocking objection #12).
- `LiveCommandBus` namespacing carries account scope on every command.
- Databento live data flows through `DatabentoLiveDataClient` into each TradingNode; orders flow through IB Gateway (exec-only) using `account_id` routing on `placeOrder`.
- Bidirectional dataset-aware symbology shim translates strategy `.IBKR` ↔ Databento native venues.
- Fail-fast validation: a `GATEWAY_CONFIG` with two entries sharing the same `ib_login_key` exits the backend at boot (council 2026-05-29 blocking objection #13).
- Synthetic test exercises the multi-login `GatewayRouter` path even though PR 1 runs single-login (council 2026-05-29 blocking objection #14, to keep `_build_production_payload_factory()`'s multi-login route table covered).

**What PR 1 explicitly does NOT prove (deferred to the pre-LVP/HVP graduation gate):**

- Independent IB Gateway container failure isolation (one container OOM / crash / 2FA prompt taking down one account while the other survives).
- Two distinct TWS sessions running concurrently.
- Per-container volume / settings isolation (`tws_settings`).
- Per-container port + socat-proxy duplication under load.
- Multi-login boot validation against a real second login (PR 1 only tests this synthetically).
- Independent drain/restart at the container layer (PR 1 only proves it at the TradingNode layer).

## 8. References

- **Authoritative architecture:** [`docs/decisions/multi-account-broker-fleet.md`](../decisions/multi-account-broker-fleet.md)
- **Long-term vision:** memory `project_multi_account_broker_fleet_vision.md`
- **Verification-spike target:** memory `reference_ib_entitlements.md` (DUP733213, no-data)
- **Paper drill accounts:** memory `reference_ib_accounts.md` (DUP733214 + DUP733215 clean $1M each)
- **Nautilus split-adapter API:** memory `reference_nautilus_multi_strategy_api.md` (Nautilus 1.223.0 + issue #3176)
- **Drill discipline:** memory `feedback_e2e_before_pr_for_live_fixes.md`
- **Compose cycle from worktree:** memory `feedback_compose_must_cycle_from_worktree.md`
- **Phase 3.1 skip protocol:** memory `feedback_skip_phase3_brainstorm_when_council_predone.md`
