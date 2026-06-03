# PRD: Broker-Account Spawn-Wiring (deployment ↔ BrokerAccount linkage + control-plane validation gate)

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-06-02
**Last Updated:** 2026-06-02

---

## 1. Overview

PR 3 (#86) made `BrokerAccount` a first-class entity (table + CRUD via API/CLI/UI + Option B' KV credentials + the `BrokerAccountService.resolve_for_spawn` seam). But **nothing consumes it on the deploy path**: a live deployment still carries free-form `account_id` (String) and `ib_login_key` (String) with **no foreign key** to `broker_accounts`, and `resolve_for_spawn` is called nowhere. So an operator can add an account through PR 3's CRUD and it is **operationally decorative** — it cannot be selected for a live deployment, and nothing validates that the chosen account is active/usable before a real-money node spawns.

This feature makes a `BrokerAccount` **deployable by identity**: a nullable, additive `LiveDeployment.broker_account_id` FK; new deploy/start flows resolve exactly one **active** `BrokerAccount` and derive `account_id` / `ib_login_key` / gateway routing **from that row** (single source of truth); and a **control-plane validation gate** (using `resolve_for_spawn`) refuses to deploy against an archived / mis-configured / credential-unresolvable account **before** the supervisor spawns the node.

Scope is the council-ratified **narrowed Option A** (Engineering Council, 2026-06-02 — see `docs/decisions/multi-account-broker-fleet.md` addendum). It explicitly does **not** change where the IB Gateway gets its working credentials (still env/render) and does **not** introduce per-account gateway containers (deferred to the 2-VM split).

## 2. Goals & Success Metrics

### Goals

- Make a CRUD-created `BrokerAccount` selectable + deployable by identity (close the control-plane→deploy gap PR 3 left open).
- Establish **one source of truth** for a live deployment's account identity (the `BrokerAccount` row), eliminating the current dual mutable-string `account_id`/`ib_login_key`.
- **Fail closed** before a real-money node spawns when the selected account is archived, mode-inconsistent, not router-resolvable, or has unresolvable/​unpinned credentials.

### Success Metrics

| Metric                                     | Target                                                                                                                         | How Measured                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| CRUD-created account becomes deployable    | A new active `BrokerAccount` can be selected for a deployment and drives its `account_id`/`ib_login_key`                       | API/CLI E2E on paper LVP                                                  |
| Bad-account deploys refused pre-spawn      | Deploy against an archived/mis-configured account returns a clear API error + records a fail-closed status; **no node spawns** | API E2E (create→archive→deploy→refusal)                                   |
| Zero regression for legacy deployments     | Existing string-based deployments continue to start/warm-restart unchanged                                                     | existing supervisor integration tests green                               |
| No KV read on the supervisor critical path | Supervisor warm-restart path reads only the DB; validation/KV-resolution happens at deploy/start time                          | code review + test asserting spawn path makes no `resolve_for_spawn` call |

### Non-Goals (Explicitly Out of Scope)

- ❌ Changing where the IB Gateway gets its working TWS credentials (still env/rendered) — that is **Option B** (gateway-env render), deferred.
- ❌ Per-account IB Gateway containers — **Option C**, deferred to the 2-VM split (PR 2 council addendum 2026-05-31).
- ❌ Claiming/implying the gate proves the running gateway authenticated with the BrokerAccount's KV credentials — it does **not**, and naming/docs/tests must say so.
- ❌ Injecting TWS credentials into the TradingNode/`TradingNodePayload` (the node never consumes them).
- ❌ Making `broker_account_id` NOT NULL or removing/renaming the legacy `account_id`/`ib_login_key` columns in this PR.
- ❌ The LVP/HVP real-login graduation drill (market-hours-only, parked).

## 3. User Personas

### Fleet Operator

- **Role:** Runs the msai-v2 platform; adds/edits/archives IB broker accounts and deploys strategies to them (via API, CLI, or UI).
- **Permissions:** Full control-plane access (authenticated).
- **Goals:** Add an account once (PR 3 CRUD) and then deploy strategies to it by selecting it — without hand-editing env vars or risking a deploy against a stale/archived account.

## 4. User Stories

### US-001: Deploy a strategy to a selected broker account

**As a** fleet operator
**I want** to start a live deployment against a specific broker account I added earlier
**So that** the deployment uses that account's IB identity and gateway routing without me re-typing account strings.

**Scenario:**

```gherkin
Given an active BrokerAccount exists (added via PR 3 CRUD) with a paper IB account id and ib_login_key
When I start a portfolio deployment selecting that BrokerAccount
Then the deployment is created linked to that BrokerAccount (broker_account_id FK)
And its account_id and ib_login_key are derived from the BrokerAccount row
And the node spawns and connects to the routed gateway for that account
```

**Acceptance Criteria:**

- [ ] The start/deploy request can identify a `BrokerAccount` (by its UUID or by a free-form `account_id`+`ib_login_key` that resolves to exactly one active account).
- [ ] The created `LiveDeployment` has `broker_account_id` set to the resolved account's id.
- [ ] `account_id`, `ib_login_key`, and gateway routing are derived from the `BrokerAccount` row, not from independently-supplied strings.
- [ ] A follow-up GET of the deployment/status reflects the linked account and the derived identity.

**Edge Cases:**
| Condition | Expected Behavior |
| --- | --- |
| Free-form `account_id`+`ib_login_key` matches no active account | Fail closed: clear API error, no deployment created, no node spawned |
| Free-form pair matches >1 account | Fail closed: ambiguous-account error |
| Legacy existing deployment (no FK) | Warm-restart unchanged via legacy string fallback |

**Priority:** Must Have

---

### US-002: A deploy against a bad account is refused before any node spawns

**As a** fleet operator
**I want** the system to refuse a deployment when the chosen account is archived, mode-inconsistent, not router-resolvable, or has unresolvable/unpinned credentials
**So that** I never get a half-started real-money node against a misconfigured account.

**Scenario:**

```gherkin
Given a BrokerAccount that has been archived (or whose credentials cannot be resolved)
When I attempt to start a deployment selecting that account
Then the start request fails closed with a clear, specific error
And the deployment is recorded with a fail-closed status (not a silent stuck "starting")
And a SPAWN_FAILED-equivalent signal is emitted for alerting
And no TradingNode subprocess is spawned
```

**Acceptance Criteria:**

- [ ] Control-plane validation runs at **deploy/start time** (API path), not on the supervisor per-spawn/warm-restart path.
- [ ] Validation confirms: account active (not archived), trading-mode consistent with the account id prefix, router-resolvable (`GatewayRouter` + `accounts_for` binding when present), and credentials resolvable + version-pinned (for non-legacy backends).
- [ ] On failure: the API returns a specific error (422/409 as appropriate) AND the deployment row reflects a distinct fail-closed status; a `SPAWN_FAILED`-equivalent metric/alert fires.
- [ ] On success: a validation stamp/status (e.g. `credentials_validated_at` + version) is persisted before the supervisor spawn command is enqueued.
- [ ] The validation gate's docstring/messages state explicitly that it proves account identity + credential **resolvability**, NOT that the running gateway authenticated with those credentials.

**Edge Cases:**
| Condition | Expected Behavior |
| --- | --- |
| KV transiently unavailable at deploy time | Deploy fails closed with a retryable error; supervisor warm-restart of _existing_ deployments is unaffected (no KV on that path) |
| Legacy-env account (NULL version) | Validation treats legacy backend per its rules (no spurious version-pin failure) |

**Priority:** Must Have

---

## 5. Constraints & Policies

### Business / Compliance Constraints

- Real-money safety: wrong-account credential/identity resolution = wrong account trades. The deploy path must fail closed.

### Platform / Operational Constraints

- **No KV resolution on the supervisor per-spawn/warm-restart critical path** (a KV outage must not block fleet-wide restarts).
- Migration must be **additive + nullable** (legacy/default deployments must continue to start and survive rollback).
- Acceptance must be demonstrable **off-hours on the paper LVP account** (the real-login LVP/HVP drill is parked for market hours).
- Per-account gateway containers remain deferred (must not be pulled forward).

### Dependencies & Required Integrations

- **Requires:** PR 3's `broker_accounts` table + `BrokerAccountService.resolve_for_spawn` (merged, #86).
- **Named integrations (scope, not mechanism):** Interactive Brokers (via the existing IB Gateway + NautilusTrader IB adapter), Azure Key Vault / file store (existing credential backends — read-only here).

## 6. Security Outcomes Required

- **Who can access what:** only authenticated operators can start deployments; account selection cannot be used to trade an account the caller's routing bindings don't permit (`accounts_for` enforcement preserved).
- **What must never leak:** TWS credentials are never returned by any read and never enter the TradingNode payload, logs, or API responses (unchanged from PR 3; this PR adds no new credential exposure — the gate reads resolvability, never echoes secrets).
- **What must be auditable:** a deployment's bound `BrokerAccount` and its pre-spawn validation outcome (pass/fail + reason) must be traceable.

## 7. Open Questions

- [ ] Exact API shape for selecting the account: accept a `broker_account_id` UUID directly, OR keep the `account_id`+`ib_login_key` request fields and resolve-to-one-active-account server-side? (Resolve in design; council requires "resolve to exactly one active account or fail closed" either way.)
- [ ] Backfill/grandfather map for existing `LiveDeployment` rows (esp. legacy `"default"` sentinel) — link forward where an unambiguous matching active account exists; leave NULL otherwise.
- [ ] Where the validation stamp lives (new column on `LiveDeployment` vs reuse of an existing status field) — design decision.

## 8. References

- **Decision (council-ratified slice):** `docs/decisions/multi-account-broker-fleet.md` — 2026-06-02 addendum (narrowed Option A).
- **Seam:** `backend/src/msai/services/live/broker_account_service.py:556` (`resolve_for_spawn`).
- **Spawn path:** `backend/src/msai/live_supervisor/__main__.py:214-289`, `fleet_router.py`, `services/nautilus/live_node_config.py:475`.
- **Linkage gap:** `backend/src/msai/models/live_deployment.py:109-119`; `backend/src/msai/api/live.py:456`.

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                  |
| ------- | ---------- | -------------- | -------------------------------------------------------- |
| 1.0     | 2026-06-02 | Claude + Pablo | Initial PRD — scope = council-ratified narrowed Option A |

## Appendix B: Approval

- [ ] Product Owner approval
- [ ] Technical Lead approval
- [ ] Ready for technical design
