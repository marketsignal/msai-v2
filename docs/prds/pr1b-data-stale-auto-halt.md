# PRD: PR 1b — Data-Stale Auto-Halt

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-06-03
**Last Updated:** 2026-06-03

---

## 1. Overview

The live fleet runs N NautilusTrader TradingNodes, each wiring its own Databento data client (IB Gateway is execution-only — it carries no market data). Any single node, dataset, or subscription can stall independently, leaving at least one account trading on frozen prices without the others noticing. **PR 1b adds per-account/per-node/per-required-feed freshness detection that auto-halts the entire fleet when any required live feed goes stale**, tags the halt with a distinct `data_stale` cause so operators don't confuse it with a manual kill-all, exposes per-feed warmth to operators, and forces a fail-closed, system-verified `/resume`. It is the last engineering gate before real-money LVP/HVP N-account graduation (blocking objection #9); it does **not** block PR 1 merge or the paper drill.

## 2. Goals & Success Metrics

### Goals

- **Primary:** No account ever submits an opening order while its required live feed is stale — a stalled feed auto-halts the fleet before blind trading.
- **Primary:** Operators can distinguish a data-stale halt from a manual `/kill-all` and from a per-account drain, by cause attribution carrying source + timestamps.
- **Secondary:** Operators can read per-account/node/dataset/symbol freshness on demand (warm/stale verdict + `last_event_ts` + session + grace).
- **Secondary:** `/resume` is fail-closed — it resumes order submission only after every required feed re-probes warm AND reconciliation has completed.
- **Secondary:** Freshness/connection-health observability is labeled per account/node/dataset/symbol (no single fleet "healthy" light).

### Success Metrics

| Metric                           | Target                                                                                                                                    | How Measured                                                                                                       |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Stale-feed → fleet halt latency  | Halt latch set within the configured session grace budget of the missed expected data point (no fixed wall-clock; grace is session-aware) | Multi-failure-mode test harness: inject each failure mode, assert latch set with `reason=data_stale` within budget |
| False-halt rate on healthy feeds | 0 false halts across a full RTH + extended-session + futures-session pass                                                                 | Harness replays normal-cadence feeds (incl. natural closing-bar gaps); assert no halt fires                        |
| Failure-mode coverage            | 5/5 modes covered (disconnect, stale-timestamp, partial-dataset stall, single-symbol stall, reconnect-storm)                              | Each mode has a passing harness test asserting halt + correct source attribution                                   |
| Resume safety                    | 0 resumes that proceed while any required feed is stale OR reconciliation incomplete                                                      | `/resume` fail-closed tests assert refusal on each unsafe precondition                                             |
| Observability labeling           | Every freshness/health metric carries account + node + dataset + symbol labels                                                            | Metrics-shape test asserts label set per series                                                                    |

### Non-Goals (Explicitly Out of Scope)

- ❌ OPRA / options freshness — deferred to a separate OPRA capacity spike (Codex P2 #9).
- ❌ Auto-resume on stale-clear — recovery is explicit operator action only.
- ❌ IB market-data pacing codes 100/162/420 — irrelevant, IB is execution-only.
- ❌ A UI page for data-health — PR 1b is an operator API + CLI surface (UI fleet view is PR 4).
- ❌ The LVP/HVP real-money graduation drill itself — PR 1b unblocks it; the market-hours drill is a separate operator step.
- ❌ Changing the existing halt-latch mechanism or the per-account drain semantics — PR 1b reuses the PR 1 explicit-fleet-emergency latch code path; it does not redesign it.

## 3. User Personas

### Fleet Operator (API / CLI)

- **Role:** Runs the live multi-account fleet via the HTTP API and `msai` CLI.
- **Permissions:** Deploy/stop/halt/resume the fleet; read fleet + per-account status and data-health.
- **Goals:** Trust that a stalled feed halts the fleet before blind trading; check per-feed warmth before resuming; resume only when safe.

### On-Call Responder

- **Role:** Diagnoses live incidents.
- **Permissions:** Read halt-cause attribution, per-feed freshness, connection-health metrics.
- **Goals:** Quickly identify which account/node/dataset/symbol stalled and why the fleet halted, without confusing a data-stale halt with a manual panic.

## 4. User Stories

### US-001: Stale required feed auto-halts the fleet

**As a** fleet operator
**I want** the fleet to auto-halt the instant any active node's required live feed goes stale
**So that** no account keeps trading on frozen prices.

**Scenario:**

```gherkin
Given a live fleet with accounts A and B, each on its own TradingNode + Databento feed
And both feeds are warm
When account A's required feed stops producing events past its session-aware grace budget
Then the explicit-fleet-emergency halt latch is set
And the halt carries reason=data_stale with source account/node/dataset/symbol, detected_at, and last_event_ts
And no account (A or B) can submit a new opening order while the latch is set
```

**Acceptance Criteria:**

- [ ] Freshness is evaluated per active account/node and per required dataset/subscription.
- [ ] Staleness is computed from event-timestamp + expected-interval + session-aware grace — NOT a flat wall-clock threshold.
- [ ] On stale detection the existing explicit-fleet-emergency halt latch is set (reused code path), fleet-wide.
- [ ] The halt record carries `reason=data_stale`, source `account/node/dataset/symbol`, `detected_at`, `last_event_ts`.
- [ ] The node-side live-halt order gate (RiskAwareStrategy, PR 2/F6) blocks new opening orders under the data-stale latch; reduce-only / flatten orders remain allowed.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Natural closing-bar gap (1-min bar naturally >grace old before next bar exists) | NO halt — grace is measured after the next _expected_ data point, session-aware |
| Market session closed / maintenance window | NO halt — closed-session grace (feed legitimately idle) |
| Multiple feeds stale at once | Single fleet halt; attribution records the first/all detected source(s) |

**Priority:** Must Have

---

### US-002: Distinguish a data-stale halt from a manual kill-all

**As an** on-call responder
**I want** the halt cause to clearly say "data_stale" with its source
**So that** I don't clear a data-stale halt as if it were a manual `/kill-all` panic (or vice-versa).

**Scenario:**

```gherkin
Given the fleet is halted
When I inspect the halt cause via the operator surface
Then a data-stale halt shows reason=data_stale + source account/node/dataset/symbol + detected_at + last_event_ts
And a manual kill-all shows its distinct manual cause
And the two are never collapsed into the same undifferentiated "halted" state
```

**Acceptance Criteria:**

- [ ] Halt-cause attribution is queryable and distinguishes `data_stale` from manual fleet halt and from per-account drain.
- [ ] Cause metadata survives a supervisor/process restart (persisted, not memory-only) so a responder sees it after reconnecting.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Manual kill-all fires while a data-stale halt is already latched | Cause attribution does not silently overwrite the data-stale source in a way that loses the original reason |

**Priority:** Must Have

---

### US-003: Check per-feed warmth on demand

**As a** fleet operator
**I want** to query per-account/node/dataset/symbol freshness through the API and CLI
**So that** I can see which feeds are warm before I resume and during an incident.

**Scenario:**

```gherkin
Given a live fleet
When I GET /api/v1/live/data-health (or run `msai live data-health`)
Then I receive, per account/node/dataset/symbol: last_event_ts, current session, computed grace budget, and a warm/stale verdict
And re-requesting after a feed stalls reflects the changed verdict
```

**Acceptance Criteria:**

- [ ] `GET /api/v1/live/data-health` returns per-account/node/dataset/symbol freshness rows (last_event_ts, session, grace, verdict).
- [ ] `msai live data-health` renders the same data as a human-readable table and exits 0 on success.
- [ ] Both surfaces require the standard auth (JWT / `X-API-Key`).

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| No live deployments active | Returns an empty/"no active feeds" result, not an error |
| A node is mid-reconnect | Verdict reflects stale/unknown for that feed, not a crash |

**Priority:** Must Have

---

### US-004: Fail-closed resume

**As a** fleet operator
**I want** `/resume` to refuse unless every required feed is verified warm AND reconciliation has completed
**So that** I cannot accidentally resume the fleet into blind or unreconciled trading.

**Scenario:**

```gherkin
Given the fleet is halted with reason=data_stale
When I call /resume while a required feed is still stale
Then resume is REFUSED and tells me which feed is still stale
When the feed recovers but reconciliation has not completed
Then resume is REFUSED and tells me reconciliation is incomplete
When all required feeds re-probe warm AND reconciliation has completed
Then resume succeeds and accounts may submit opening orders again
```

**Acceptance Criteria:**

- [ ] `/resume` actively re-probes every required feed's live freshness at call time (not a cached verdict).
- [ ] `/resume` confirms reconciliation completion before clearing the latch.
- [ ] `/resume` is fail-closed: any still-stale feed OR incomplete reconciliation → refusal with a specific reason; no partial resume.
- [ ] No code path auto-resumes the fleet on stale-clear.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Feed warms then re-stales between probe and clear | Resume aborts/refuses; latch stays set (fail-closed) |
| Reconciliation never completes (IB timeout) | Resume refuses with reconciliation-incomplete reason; operator retries |

**Priority:** Must Have

---

### US-005: Labeled freshness + health observability

**As an** on-call responder
**I want** freshness, Databento connection-health, and IB exec pacing metrics labeled per account/node/dataset/symbol
**So that** I can pinpoint the failing feed instead of seeing one undifferentiated fleet light.

**Scenario:**

```gherkin
Given the fleet is running
When I read the metrics
Then per-account/node/dataset/symbol freshness + connection-health series are present and labeled
And IB execution pacing/throttling errors are surfaced (not IB market-data codes 100/162/420)
```

**Acceptance Criteria:**

- [ ] Freshness + connection-health metrics carry account + node + dataset + symbol labels.
- [ ] IB exec pacing/throttling is surfaced; IB market-data pacing codes are NOT used.
- [ ] A single fleet-level "healthy" boolean is NOT the only signal.

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| High-cardinality symbol set | Labeling stays per-required-feed (active subscriptions), not the full universe |

**Priority:** Should Have

---

### US-006: Multi-failure-mode coverage (graduation gate)

**As a** fleet operator preparing real-money graduation
**I want** the data-stale auto-halt proven against every known failure mode
**So that** blocking objection #9 is satisfied before LVP/HVP real money.

**Scenario:**

```gherkin
Given the test harness can inject each failure mode against a live-like node
When I run the harness
Then disconnect, stale-timestamp, partial-dataset stall, single-symbol stall, and reconnect-storm
   each trigger the fleet halt with correct reason=data_stale source attribution
And healthy-cadence feeds (incl. natural bar gaps) trigger NO halt
```

**Acceptance Criteria:**

- [ ] Harness covers all five failure modes, each asserting halt + correct source attribution.
- [ ] Harness includes a healthy-cadence negative case asserting no false halt.
- [ ] Harness is repeatable in CI (no real Databento/IB dependency — feeds are simulated/injected).

**Priority:** Must Have

---

## 5. Constraints & Policies

> Outcome-level only. HOW we satisfy them is design.

### Business / Compliance Constraints

- **Real-money safety gate:** PR 1b must satisfy blocking objection #9 before any real-money N-account (LVP/HVP) deployment. It does not gate PR 1 merge or the paper drill.
- **No blind trading:** under any required-feed stall, the fleet must reach a state where no account can open new positions, before the next decision an affected strategy would make.

### Platform / Operational Constraints

- **IB is execution-only** in the fleet topology — no IB market data; all market data is Databento per node.
- **Session-aware:** freshness must respect equities RTH / pre-market / post-market / closed sessions AND the near-24h futures session calendar. A flat wall-clock threshold is prohibited (false-halts 1-min bars by construction).
- **Reuse, don't redesign:** data-stale halt rides the existing PR 1 explicit-fleet-emergency latch code path; the node-side order gate is the PR 2/F6 RiskAwareStrategy gate.

### Dependencies & Required Integrations

- **Requires:** PR 1 (#84, per-account control plane + halt split), PR 2 (#85, per-account supervisor), PR 3 (#86) + spawn-wiring (#88) — all merged to main.
- **Named integrations (scope, not mechanism):** Databento live feeds for `EQUS.MINI` (equities) and `GLBX.MDP3` (ES/futures); IB Gateway (execution only).
- **Blocked by:** none (foundations merged).

## 6. Security Outcomes Required

- **Who can access what:** only authenticated fleet operators (JWT / `X-API-Key`) can read `data-health` or call `/resume`; the same authz as existing live endpoints.
- **What must never leak:** no Databento API key, IB credentials, or secret material in `data-health` responses, halt-cause metadata, logs, or metrics. Freshness records carry only feed identifiers + timestamps.
- **What must be auditable:** every fleet halt must be attributable to a cause (`data_stale` + source, or manual). Every `/resume` must record who resumed and the verified preconditions (feeds warm + reconciliation complete).
- **Fail-closed everywhere:** unknown/missing freshness state is treated as STALE (halt), never as warm. `/resume` refuses on any unverified precondition.

## 7. Open Questions

- [ ] Concrete grace-budget default values per (asset-class, session) — Claude proposes in the plan; finalized at plan review.
- [ ] Exact "reconciliation complete" signal source for the `/resume` gate (Nautilus `LiveExecEngineConfig(reconciliation=True)` completion event vs a supervisor-tracked per-node flag) — resolve during Phase 2 research / Phase 3 plan.
- [ ] Where freshness is evaluated (in-node monitor publishing to the supervisor, vs supervisor polling node-reported `last_event_ts`) — design decision for brainstorming/plan, constrained by "survives a supervisor outage" (mirrors the PR 2/F6 node-side gate philosophy).

## 8. References

- **Discussion Log:** `docs/prds/pr1b-data-stale-auto-halt-discussion.md`
- **Decision doc:** `docs/decisions/multi-account-broker-fleet.md` (§"Data-stale auto-halt — per-node detection, fleet-wide action", blocking objection #9, Addendum 2026-05-30 sequencing item 2, Verification spike, §7 observability-modification row)
- **Related PRs:** PR 1 (#84), PR 2 (#85), PR 3 (#86), spawn-wiring (#88)

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                               |
| ------- | ---------- | -------------- | ------------------------------------- |
| 1.0     | 2026-06-03 | Claude + Pablo | Initial PRD from completed discussion |

## Appendix B: Approval

- [ ] Product Owner approval
- [ ] Technical Lead approval
- [ ] Ready for technical design
