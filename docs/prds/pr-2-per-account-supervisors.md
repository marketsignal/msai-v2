# PRD: PR 2 — Per-Account Supervisor Ownership Boundary

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-31
**Last Updated:** 2026-05-31

---

## 1. Overview

The live-trading layer runs a single `live-supervisor` process whose one in-memory
`ProcessManager.handles` map owns **every** account's TradingNode subprocess. Today a
single account's node crash is already survivable (the supervisor outlives a child),
but two real gaps remain: (1) a halt does NOT reliably stop a running node's order
submission (the order path reads a stale cached boolean — the F6 real-money P0), and
(2) a crashed account does not self-heal. PR 2 **hardens this single-supervisor model
into account-scoped ownership semantics** (per-account command streams for
failure containment, halt-gated bounded auto-restart + startup re-scan, and a
live-Redis node-side halt re-check), so a single account's node fault is isolated and
self-heals, and a halt is a real kill-switch. **Honest scope (council #2):** a
supervisor-PROCESS crash or a deliberate container redeploy still recreates the
container and restarts all co-located nodes (the supervisor is PID 1; nodes are its
`mp.Process` children) — that is recovered via the startup re-scan and gated by the
binding deploy contract, but true supervisor/redeploy isolation is DEFERRED to the
2-VM / per-account-container phase. The operator gains per-account restart-authority
visibility, and a crashed account self-heals via auto-restart + IB reconciliation
(unless that account is deliberately halted).

This is PR 2 of the multi-account-broker-fleet rollout (PR 1 shipped + merged as #84).
Provenance: `docs/decisions/multi-account-broker-fleet.md` — Supervisor ruling, coupled
decision #1, and the PR 2 forward-plan entry.

## 2. Goals & Success Metrics

### Goals

> Scope corrected after council #2 (single-VM honest boundary — see US-1).

- **Primary:** A single account's **TradingNode crash** cannot affect any other account's
  node, and the crashed account self-heals (halt-gated, bounded auto-restart + IB
  reconciliation). (Supervisor-process-crash / deploy = container recreate = all-restart,
  recovered via the startup re-scan; true supervisor/deploy isolation is deferred to the
  2-VM / per-account-container phase per council #2.)
- **Primary (real-money P0):** A halt (`/kill-all`, account `/drain`) stops a **running**
  node's order submission even when the supervisor is down — the node re-checks the live
  Redis halt latch on the order path, fail-closed (US-1b / F6).
- **Secondary:** The operator can observe per-account supervisor/restart-authority health
  through the existing `/live/status` API and `msai live status` CLI.
- **Operational:** The binding production deploy contract (routine deploys exclude the
  broker profile; deploys refused while live deployments are active) is documented +
  enforced.

### Success Metrics

| Metric                                  | Target                                                                                               | How Measured                                                                                                                                    |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Blast radius of one **node** crash      | Exactly 1 account affected (the one whose TradingNode crashed)                                       | Kill-A-**node** drill: account B keeps receiving bars + stays drainable/haltable throughout A's node crash                                      |
| Crashed-account recovery                | A's TradingNode auto-restarts + reconciles, OR stays down if A halted                                | Drill observation: A resumes trading after the node crash (reconciliation completed before new orders accepted)                                 |
| Node-side halt (P0)                     | A halt stops a running node's NEW orders with the supervisor down; flatten/reduce-only still allowed | Outage harness (T12) + drill: opening order blocked, flatten order allowed, with supervisor stopped                                             |
| PR 1 regression                         | Zero regressions                                                                                     | PR-1 Shape B halt-isolation drill (LVP+HVP) re-run still green; full unit+integration suite green                                               |
| Operator visibility of supervisor state | Per-account restart-authority health field present                                                   | `/live/status` + `msai live status` show per-account `auto_restart_paused`/heartbeat-age/`last_restart_at` + top-level `router_heartbeat_age_s` |

### Non-Goals (Explicitly Out of Scope — deferred to later PRs)

- ❌ BrokerAccount first-class entity / CRUD / UI (PR 3)
- ❌ Dashboard account selector + per-account read filtering (PR 4)
- ❌ Per-account risk caps + fleet-aggregate ledger (PR 5)
- ❌ Data-stale auto-halt (PR 1b)
- ❌ Any change to the credentials model (PR 3 / council Option B')
- ❌ Per-account containers / true supervisor-redeploy isolation — council #2 deferred this
  (Shape A / "Opt 1") to the 2-VM split. PR 2 is the single-supervisor "Opt 4 refined"
  shape. (The implementation-shape fork is RESOLVED — see §7.)
- ❌ In-process lease / generation-token fencing — council #2 rejected it as
  over-engineered for N=2 (the F5 active-live deploy gate prevents two-supervisor overlap)

## 3. User Personas

### Operator (Pablo)

- **Role:** Runs the fleet; deploys/restarts the stack; owns incident response.
- **Permissions:** Full — deploy, restart, drain, halt, resume any account.
- **Goals:** Trust that one account's failure (or a routine redeploy) does not cascade
  to the whole fleet; see at a glance which accounts are healthy vs. recently recovered.

### The Fleet (autonomous owner)

- **Role:** The per-account ownership mechanism that detects a dead TradingNode and
  auto-restarts + reconciles it without human intervention.
- **Permissions:** Restart + reconcile a node it owns — but MUST respect the account's
  halt latch (a halted/drained account is never auto-restarted).
- **Goals:** Keep each healthy account trading; self-heal a crashed node; never resurrect
  an account the operator deliberately took down.

## 4. User Stories

### US-1: One account's crash does not cascade to the fleet (single-VM contract)

> **Amended 2026-05-31 after council #2 (container-topology ruling).** The original US-1
> said "restart/redeploy the live-supervisor layer." Verified facts F4/F5 (see the
> decision-doc council #2 addendum) showed that on the single 16GB VM: (a) the routine
> push-to-main deploy EXCLUDES the `live-supervisor` / `broker` profile, and (b) the deploy
> workflow REFUSES while any live deployment is active. So routine deploys do not recreate
> the supervisor container. The remaining "deliberate operator supervisor-image rebuild"
> case is recreate-all (nodes are `mp.Process` children of the supervisor's PID-1 process,
> so a container recreate kills them all). True supervisor-container redeploy isolation is
> a **2-VM-split / per-account-container** capability, explicitly DEFERRED. US-1 below is the
> single-VM contract PR 2 actually delivers.

**As an** operator
**I want** one account's TradingNode crash to NOT take down the other accounts' nodes, and
any container-recreate event (supervisor crash or deliberate redeploy) to recover every
account safely via staggered auto-restart + reconcile
**So that** a single node fault never costs me the fleet, and a container event recovers
without me hand-restarting each account.

**Scenario:**

```gherkin
Given two accounts (A and B) are live with running TradingNodes
When account A's TradingNode crashes
Then account B's TradingNode keeps receiving bars and processing without interruption
And account B remains drainable and haltable throughout the event
And account A's node auto-restarts per US-2 (unless A is halted)
```

**Acceptance Criteria:**

- [ ] A single account's TradingNode crash leaves every other account's node running and
      unaffected (no bar-stream gap, no forced stop) — verified by the kill-A-node drill.
      This is the real isolation win: the supervisor survives a child-`mp.Process` crash
      and only the crashed account's node is affected.
- [ ] On a container-recreate event (supervisor-process crash → PID 1 dies → Docker
      recreates the container; OR a deliberate broker-profile redeploy), the restarted
      supervisor's **startup re-scan** detects each account's now-dead deployment and
      auto-restarts + reconciles it (staggered, `max-concurrent-respawn=1`, halt-gated).
- [ ] Account B's `/drain/{B}`, `/resume/{B}`, and fleet `/kill-all` behave identically
      during and after a node crash.
- [ ] No shared in-memory structure exists whose loss forces more than one account's node
      to stop **for a node-level crash** (the reaper/cache is per-deployment; one account's
      reap failure can't stall another's).

**Explicitly NOT delivered by PR 2 (single-VM honest boundary — deferred to the 2-VM split
/ per-account-container phase per council #2):**

- **Supervisor-process-crash isolation.** The supervisor IS the container's PID-1 process;
  its crash recreates the container, so ALL co-located nodes restart together (then
  auto-recover via the startup re-scan). PR 2 does NOT keep other accounts' nodes alive
  through a supervisor-process crash — that needs per-account containers (Opt 1).
- **Deploy-time isolation.** A deliberate `COMPOSE_PROFILES=broker up -d` with a changed
  supervisor image recreates the container. Mitigated by the binding deploy contract:
  routine deploys EXCLUDE the broker profile (F4) and the deploy workflow REFUSES while any
  live deployment is active (F5). Documented in decision doc + deploy runbook + release
  checklist.

**Edge Cases:**

| Condition                                 | Expected Behavior                                                                                                          |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Node died while supervisor was down       | On supervisor startup, the re-scan detects the `failed`/stale active deployment and auto-restarts it per US-2 (halt-gated) |
| Two accounts' nodes crash simultaneously  | Each auto-restarts independently; staggered (`max-concurrent-respawn=1`) to avoid a reconnect storm into a shared gateway  |
| Account B mid-drain when A's node crashes | B's drain completes normally; A's crash does not interfere                                                                 |

**Priority:** Must Have

---

### US-1b: A halt stops a running node even when the supervisor is down (node-side live halt)

> **New, council #2 unanimous P0 (F6).** Today `RiskAwareStrategy` checks a CACHED halt
> boolean refreshed by the supervisor; the order-submit path never re-reads Redis. If the
> supervisor is wedged/down when a halt fires, a running node keeps submitting orders
> against a stale `halt=false`. This is a real-money safety hole independent of topology.

**As an** operator
**I want** a `/kill-all` or account `/drain` to stop a running node's order submission
even if the supervisor process is wedged or down
**So that** "halt" is a real kill-switch, not a router-mediated best-effort.

**Acceptance Criteria:**

- [ ] The running node gates the order-submit path on a **live-backed halt state
      continuously refreshed from Redis** (fleet `fleet_halt_key` + `account_halt_key`),
      NOT a boolean cached once at startup. Because `on_bar` is a synchronous hot path, the
      gate reads a value refreshed by a background task at least every ~1s; the gate
      treats a `None`/stale value (age > `HALT_CACHE_MAX_AGE_S` = 2s) as halt and
      fails closed (plan T2 sync-safe staleness sentinel).
- [ ] **Reduce-only / flatten / cancel orders are ALLOWED under halt** — a halt blocks
      OPENING/increasing-exposure orders but must NOT block the `market_exit()` /
      `close_all_positions()` / `cancel_all_orders()` flatten path that `/kill-all`,
      `/drain`, `/stop` drive AFTER setting the latch. (Blocking those would freeze
      positions open — the opposite of a kill-switch.)
- [ ] If Redis is unreachable at the halt check, the node FAILS CLOSED for opening orders
      (blocks new exposure) while still allowing flatten/cancel.
- [ ] A halt set while the supervisor is down is honored by the running node **within the
      halt-cache refresh window** — typically ≤~1s, worst case ≤2s (`HALT_CACHE_MAX_AGE_S`),
      after which a not-yet-refreshed cache is treated as stale → fail-closed — with NO
      supervisor involvement. (Honest bound, not zero-latency: this in-node gate is the FAST
      layer of the multi-layer kill-all; when the supervisor IS up, the push-stop + SIGTERM
      layers backstop it. A ≤2s in-node window is the necessary trade-off for not blocking
      the synchronous `on_bar` hot path with an inline Redis GET.)
- [ ] **Bounded guarantee:** the gate covers strategies using the supported public submit
      API (`self.submit_order*` / `self.modify_order` via the mandatory `RiskAwareStrategy`
      base, enforced at subprocess startup, fail-closed). A strategy bypassing it via
      Nautilus internals (`_msgbus`, `_manager`, hand-built commands) is out of the
      guaranteed surface — rejected by a static lint where detectable; documented, not
      overclaimed.
- [ ] The 4-layer kill-all docs are corrected: the in-node order gate is the fast layer,
      now live-backed + reduce-only-aware (no "Layer-4 cached" overstatement).

**Edge Cases:**

| Condition                           | Expected Behavior                                                                              |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| Redis unreachable at halt check     | OPENING order blocked (fail-closed); flatten/cancel allowed; logged w/ account                 |
| Halt true + reduce-only/MARKET_EXIT | ALLOWED (kill-all/drain flatten proceeds under halt)                                           |
| Halt set, then cleared, mid-session | Node converges to the latest live state within the refresh/staleness bound (≤~1s typical, ≤2s) |

**Priority:** Must Have (real-money P0)

---

### US-2: A crashed account self-heals (within the halt latch)

**As an** operator
**I want** a dead account's TradingNode to auto-restart and reconcile its open IB
positions/orders — UNLESS that account is halted/drained
**So that** a transient crash doesn't silently leave an account flat through market
moves, but a deliberate halt is never overridden.

**Scenario:**

```gherkin
Given account A's TradingNode has died (crash, OOM, or a container-recreate event)
And account A is NOT under an active halt/drain latch
When the Fleet (the single supervisor's per-account restart authority) detects the dead node
Then it restarts account A's TradingNode
And runs IB reconciliation to recover open orders/positions
And the restarted node does NOT accept new orders until reconciliation completed
```

**Acceptance Criteria:**

- [ ] A dead, non-halted account's TradingNode is auto-restarted by the Fleet (the single
      supervisor's per-account restart authority).
- [ ] IB reconciliation (`LiveExecEngineConfig(reconciliation=True)`) completes before
      the restarted node submits any new order (Nautilus gotcha #10 — verify completion,
      don't trust absence of exception).
- [ ] An account under an active halt — its own `account_halt_key`/drain OR a
      **fleet-wide `/kill-all` (`fleet_halt_key`)** — is NOT auto-restarted; it stays down
      until the operator `/resume`s it. (The restart gate checks fleet OR account; see plan
      T6 — `/kill-all` sets only the fleet latch, `/drain` only the account latch.)
- [ ] Auto-restart is bounded (no infinite crash-loop): a max-restart-attempts ceiling
      with a clear terminal `SPAWN_FAILED_*` state surfaced to the operator.

**Edge Cases:**

| Condition                                           | Expected Behavior                                                                        |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Node crashes repeatedly (bad config / dead gateway) | Bounded retries, then terminal failure state visible on `/live/status`; no infinite loop |
| Account halted while a restart is in flight         | The restart aborts / the freshly-started node is stopped; halt wins                      |
| Reconciliation times out on restart                 | Node does NOT accept orders; surfaced as a degraded/failed health state                  |

**Priority:** Must Have

---

### US-3: Per-account restart-authority health is observable

> Scope note (council #2): there is ONE supervisor process (not per-account owner
> processes). "Health" here is the per-account restart-authority state (auto-restart
> paused?, recent restarts, node heartbeat age) + a single top-level supervisor liveness
> signal — NOT a per-account owner-process liveness.

**As an** operator
**I want** to see each account's restart-authority health (auto-restart state, recent
restarts, node heartbeat age) plus the supervisor's own liveness on `/live/status` and
`msai live status`
**So that** I can tell at a glance which accounts are healthy, which recently
auto-restarted, and whether the supervisor itself is alive — without reading logs.

**Scenario:**

```gherkin
Given accounts A and B are live and A's TradingNode auto-restarted 2 minutes ago
When I GET /api/v1/live/status (or run `msai live status`)
Then each account row shows its restart-authority health
And A's row reflects the recent restart (last_restart_at / heartbeat age)
And the response shows a small top-level router_heartbeat_age_s (supervisor alive)
```

**Acceptance Criteria:**

- [ ] `/api/v1/live/status` carries per-account `auto_restart_paused`(+reason),
      `consecutive_respawn_failures`, `last_restart_at`, node heartbeat age, halt-latch
      state, PLUS a top-level `router_heartbeat_age_s` (single supervisor liveness).
- [ ] `msai live status` shows the same per-account health + the router-health line.
- [ ] Account-scoped (consistent with PR 1's `account_id` / `ib_login_key` /
      `ibg_client_id` columns).
- [ ] A follow-up read after a restart reflects the updated health (persists; not a
      one-shot in-memory flag).

**Edge Cases:**

| Condition                          | Expected Behavior                                                         |
| ---------------------------------- | ------------------------------------------------------------------------- |
| An account has no running node yet | Health shows a clear "not started / no live node" state, not an error     |
| Node heartbeat is stale            | Health reflects staleness (age beyond threshold), so the operator sees it |

**Priority:** Must Have

---

### US-4: Kill-A-node + node-side-halt acceptance drill (operator-run, live)

> Scope corrected (council #2): the drill kills account A's **TradingNode** (the isolated
> unit), not "the supervisor-owner" — a supervisor kill recreates the container (all
> accounts restart) and is the deferred case, not the isolation proof. It also verifies
> the node-side live halt (US-1b/F6) with the supervisor stopped.

**As an** operator
**I want** to deliberately kill account A's TradingNode (LVP) with LVP + HVP both live,
watch HVP trade through it while LVP self-recovers, and verify a halt stops a running node
even with the supervisor down
**So that** I have direct, end-to-end proof of node-crash isolation + node-side halt
before merge.

**Scenario:**

```gherkin
Given LVP and HVP are both live with running TradingNodes (Shape B topology)
When I kill account LVP's TradingNode process
Then HVP keeps receiving bars and stays drainable/haltable for the whole event
And LVP's TradingNode auto-restarts and reconciles (per US-2), unless LVP is halted
And with the supervisor stopped, a halt set on a running account blocks its next order
And re-running the PR-1 Shape B halt-isolation drill still passes
```

**Acceptance Criteria:**

- [ ] B (HVP) shows continuous operation across A's (LVP) **node** kill — no bar gap, no
      forced stop, drain/halt still work on B.
- [ ] A (LVP) recovers per US-2 (auto-restart + reconcile), or stays down if halted.
- [ ] With the supervisor process stopped, setting an account halt blocks a running node's
      next order submission (US-1b/F6 node-side live halt).
- [ ] The PR-1 Shape B account-scoped halt-latch drill (`/drain` one account leaves the
      other's latch untouched; `/kill-all` halts both with `reason=fleet_emergency`)
      re-runs green after PR 2's changes.
- [ ] Operator-run before merge per `feedback_all_continuity_gates_before_pr.md`.

**Priority:** Must Have (acceptance gate)

---

## 5. Constraints & Policies

### Business / Compliance Constraints

- Real-money platform — reversibility and blast-radius dominate. Any autonomous
  recovery action (US-2 auto-restart) must be bounded and must respect an operator's
  deliberate halt.

### Platform / Operational Constraints

- Single Azure VM (Standard_D4ds_v6, 4 vCPU / 16 GB) in prod today. Per-account
  ownership must fit the existing host resource envelope. This 16GB ceiling is the
  reason council #2 chose the single-supervisor "Opt 4 refined" shape over per-account
  containers (deferred to the 2-VM split). PR 2 adds no new long-lived process — it
  hardens the one existing supervisor.
- Must run under the existing Docker Compose deployment + the broker / broker-hvp
  compose profiles introduced in PR 1.

### Dependencies & Required Integrations

- **Requires:** PR 1 (#84, merged) — `halt_keys`, `GatewayRouter`, account-scoped
  `/drain` + `/resume`, `gateway_session_key` threading, Shape B compose foundation.
- **Named integrations (scope, not mechanism):** Interactive Brokers (exec) +
  NautilusTrader `LiveExecEngineConfig` reconciliation; Redis (existing command bus +
  halt latches); PostgreSQL (`live_node_processes` / `live_deployments` rows).

## 6. Security Outcomes Required

- **Who can access what:** Supervisor-health fields on `/live/status` are visible to
  the same authenticated operator surface as the rest of live status (Azure Entra ID
  JWT / `X-API-Key`); no new privilege tier.
- **What must never leak:** No credentials, TWS passwords, or KV references appear in
  the new supervisor-health field or in logs emitted by the ownership boundary.
- **What must be auditable:** Every auto-restart action (US-2) is logged with
  account_id + cause + reconciliation outcome, traceable to the owning account.
- **Halt-latch authority:** The autonomous owner can NEVER override an operator's halt
  latch — a halted account stays down until an explicit `/resume`. This is a
  safety-critical outcome, not a nicety.

## 7. Open Questions

> **RESOLVED by council #2 (2026-05-31) — see decision-doc addendum.**

- [x] **Implementation shape — RESOLVED: Opt 4 refined.** NOT in-process per-account
      actor-fencing (rejected as over-engineered for N=2), NOT per-account containers (the
      ratified long-term vision, but DEFERRED to the 2-VM split — launch-model rewrite +
      16GB OOM risk). PR 2 ships per-account ownership _semantics_ in the existing single
      supervisor: per-account command streams (failure containment), halt-latch-gated
      auto-restart with a bounded crash-loop guard, node-side live halt re-check (the F6
      P0), per-account health, honest `FleetRouter`/`NodeHandleCache` naming, and the
      binding production deploy contract documented (F4 broker-excluded + F5 active-live
      refusal). Per-account-container deploy isolation lands at the 2-VM split.
- [x] **Owner-liveness signal — RESOLVED:** Redis reaper/router heartbeat + the existing
      `HeartbeatMonitor` DB rows; `/live/status` reads `last_heartbeat_at` (DB) +
      `router_heartbeat_age_s` (Redis).
- [x] **Auto-restart ceiling values — RESOLVED in plan T3:** `MAX_RESTART_ATTEMPTS = 5`
      consecutive failures within a 30-min rolling window → `auto_restart_paused`; backoff
      10s→20s→40s→80s→160s→300s (cap), ±25% jitter; `record_success` (node running +
      reconciled) resets. Module constants in `restart_policy.py`, env-overridable for the
      drill. (Conservative — a real-money node must not hammer a recovering shared IB
      gateway; the 10s floor sits above `reconciliation_startup_delay_secs=10`.)

## 8. References

- **Discussion Log:** `docs/prds/pr-2-per-account-supervisors-discussion.md`
- **Decision doc (authoritative):** `docs/decisions/multi-account-broker-fleet.md`
  (Supervisor ruling, coupled decision #1, PR 2 forward-plan entry)
- **Related PRDs:** `docs/prds/multi-account-broker-fleet.md` (PR 1)
- **PR 1:** merged as #84 (`873b8e6`)

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes     |
| ------- | ---------- | -------------- | ----------- |
| 1.0     | 2026-05-31 | Claude + Pablo | Initial PRD |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval
- [ ] Ready for technical design
