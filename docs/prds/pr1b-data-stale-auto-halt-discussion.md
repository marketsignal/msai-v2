# PRD Discussion: PR 1b — Data-Stale Auto-Halt

**Status:** Complete
**Started:** 2026-06-03
**Participants:** Pablo, Claude

## Original User Stories

PR 1b of the multi-account-broker-fleet roadmap. Deferred from PR 1 per the Hawk minority report. **Blocks real-money LVP/HVP N-account graduation** (blocking objection #9), but does NOT block PR 1 merge or the paper drill.

The fleet runs N TradingNodes, each wiring its own `DatabentoDataClient` (IB is exec-only — no IB market data). Any one node / dataset / subscription can stall independently — accounts do NOT go stale together. When a required live feed goes stale, at least one account is flying blind, so the system must auto-halt the whole fleet and force explicit operator recovery.

## Council-Settled Requirements (from docs/decisions/multi-account-broker-fleet.md)

These are ratified — encoded as PRD requirements, not re-litigated:

- **Per-account / per-node / per-required-feed freshness detection**; **fleet-wide halt action** if any required live feed is stale (objection #5/#7 per-account observability preserved).
- **Freshness = event-timestamp + expected-interval + session-aware grace**, NOT a flat "last bar older than 30s" (a flat 30s false-halts 1-min bars by construction — every closing bar is naturally >30s old before the next exists). Default grace target: 30s _after the next expected data point_, with asset-class/session overrides.
- **On stale detection: set the SAME explicit-fleet-emergency halt latch** (reuse the PR 1 latch code path) carrying distinguishing metadata: `reason=data_stale`, source `account/node/dataset/symbol`, `detected_at`, `last_event_ts` — so an operator can tell a data-stale halt from a manual `/kill-all` panic and not clear one as the other.
- **Recovery is explicit operator action only** — no auto-resume on stale-clear. `/resume` requires **data-warm status verification AND reconciliation completion** before any account resumes order submission.
- **Multi-failure-mode test harness** covering ALL of: simulated Databento disconnect, stale-timestamp feed (connection up but bars frozen in the past), partial-dataset stall (one dataset stalls while another flows), single-symbol stall while others flow, reconnect-storm.
- **Observability (objection #7, modified):** drop IB market-data pacing codes 100/162/420 (irrelevant — IB is exec-only). Replace with Databento connection-health + per-node/per-dataset/per-symbol freshness signal + IB exec pacing/throttling + fleet halt-cause attribution. **Metrics MUST be labeled by account/node/dataset/symbol** — a single fleet "Databento healthy" light is insufficient (Codex P2 #8).
- **Boot-time fail-closed** treatment for the Databento data-client config (objection #6 extended).

## Open Scope Boundaries (this discussion resolves)

1. **Asset-class scope** — equities-first; OPRA/options explicitly deferred (Codex P2 #9). Are ES/futures (`GLBX.MDP3`) in scope for PR 1b freshness, or equities (`EQUS.MINI`) only?
2. **Session-aware grace-budget defaults** — concrete per-asset-class/session budgets (RTH vs pre/post-market vs illiquid).
3. **`/resume` data-warm verification** — automatic re-check the operator triggers vs operator attestation; reconciliation-complete also required.
4. **Operator read surface** — new freshness read endpoint/CLI (`msai live data-health`?) vs metrics + halt-cause attribution only.

## Discussion Log

- 2026-06-03: Surfaced the 4 open boundaries to Pablo. Resolved (below).

### Resolved Boundary Decisions (Pablo, 2026-06-03)

1. **Asset-class scope → Equities + ES futures.** Cover `EQUS.MINI` (consolidated equities, RTH + pre/post sessions) AND `GLBX.MDP3` (futures — ES etc., near-24h session with distinct grace). OPRA/options remain deferred (Codex P2 #9). → freshness + grace config must handle BOTH an equities RTH/extended-session calendar and a futures near-24h calendar.
2. **Grace budgets → config-driven per asset-class + session.** Budgets live in config keyed by (asset-class, session: RTH / pre-market / post-market / closed/maintenance), operator-tunable without code change. Claude proposes defaults in the PRD/plan (e.g. 1-min equities RTH ≈ 90s after next-expected; looser pre/post; futures per its session). Defaults reviewed at plan time.
3. **`/resume` data-warm verification → auto re-check (system-verified, fail-closed).** `/resume` actively re-probes every required feed's live freshness AND confirms reconciliation completed; it REFUSES to resume if any required feed is still stale or reconciliation is incomplete. Operator triggers it; the system is the gate. No operator-attestation shortcut.
4. **Operator read surface → new read endpoint + CLI.** Add a public read surface: `GET /api/v1/live/data-health` (per-account/node/dataset/symbol freshness + `last_event_ts` + session + computed grace + stale/warm verdict) and a mirroring `msai live data-health` CLI. Operators check warmth before `/resume` and during incidents. This adds API + CLI E2E surfaces (UI: N/A for PR 1b — operator/fleet-ops surface, not an end-user page; consistent with the API-first/CLI-second ordering and how PR 1/2/3 operator capabilities were surfaced).

## Refined Understanding

### Personas

- **Fleet operator (API/CLI)** — runs the live fleet; needs to (a) trust that a stalled feed auto-halts the fleet before blind trading, (b) tell a data-stale halt from a manual kill-all, (c) check per-feed warmth, and (d) resume safely only when data is verified warm + reconciliation complete.
- **On-call responder** — during an incident, reads halt-cause attribution + per-feed freshness to diagnose which node/dataset/symbol stalled.

### User Stories (Refined)

- **US-001 (auto-halt):** As a fleet operator, when any required live feed for any active node goes stale, the fleet auto-halts (existing emergency latch) with `reason=data_stale` + source `account/node/dataset/symbol` + `detected_at` + `last_event_ts`, so no account trades blind.
- **US-002 (distinguish cause):** As an on-call responder, I can tell a data-stale halt from a manual `/kill-all` via the halt-cause attribution, so I don't clear one as the other.
- **US-003 (check warmth):** As a fleet operator, I can query per-account/node/dataset/symbol freshness via `GET /api/v1/live/data-health` and `msai live data-health` and see each feed's warm/stale verdict + `last_event_ts` + session + grace.
- **US-004 (safe resume):** As a fleet operator, `/resume` refuses unless every required feed re-probes warm AND reconciliation completed — fail-closed.
- **US-005 (observability):** Per-account/node/dataset/symbol freshness + Databento connection-health + IB exec pacing metrics, labeled (no single fleet "healthy" light).

### Non-Goals

- OPRA/options freshness (separate capacity spike, Codex P2 #9).
- Auto-resume on stale-clear (explicit operator action only).
- IB market-data pacing codes 100/162/420 (IB is exec-only).
- UI page for data-health (operator API/CLI surface for PR 1b).
- The LVP/HVP real-money graduation drill itself (PR 1b unblocks it; the drill is a separate market-hours operator step).

### Key Decisions

- Freshness = event-ts + expected-interval + session-aware grace (config-driven, per asset-class+session); equities + ES futures calendars.
- Data-stale rides the SAME explicit-fleet-emergency latch, distinct cause attribution.
- `/resume` = system-verified auto re-check (fail-closed) + reconciliation-complete.
- New `data-health` read endpoint + CLI.
- Multi-failure-mode test harness: disconnect / stale-timestamp / partial-dataset stall / single-symbol stall / reconnect-storm.

### Open Questions (Remaining)

- [ ] Concrete grace-budget default values per (asset-class, session) — proposed in PRD, finalized at plan review.
- [ ] Exact "reconciliation complete" signal source for the resume gate (Nautilus `LiveExecEngineConfig(reconciliation=True)` completion vs a supervisor-tracked flag) — resolve during research/plan.
