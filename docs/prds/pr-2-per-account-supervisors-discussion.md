# PRD Discussion: PR 2 — Per-Account Supervisor Ownership Boundary

**Status:** In Progress
**Started:** 2026-05-31
**Participants:** Pablo, Claude

## Original User Stories

From the council decision doc (`docs/decisions/multi-account-broker-fleet.md`), PR 2 is:

> **PR 2 — Per-account supervisor ownership boundary.** Replace the single
> `ProcessManager.handles`-owns-everything model so a supervisor restart cannot
> take down all accounts (per-account supervisor processes, or a thin router
> that does not parent node lifetimes).

Council ruling (coupled decision #1): "Per-account supervisors own TradingNode
lifetimes. A shared router is allowed ONLY if it does not parent/lifetime-manage
all nodes."

## Current architecture (grounding facts)

- Single `live-supervisor` compose service → `python -m msai.live_supervisor`.
- One `ProcessManager` instance with `self.handles: dict[UUID, mp.process.BaseProcess]`
  (keyed by `deployment_id`) owning ALL TradingNode subprocesses across all accounts
  (`process_manager.py:160`).
- `reap_loop` (line 851) polls `self.handles` every 1s; `kill_all` (843), `stop` (1125)
  all operate on the single map.
- `gateway_session_key` already threaded into `spawn()` (PR 1) — per-session startup
  serialization exists; PR 2 is about OWNERSHIP, not the session key plumbing.

## The blast-radius problem

One `live-supervisor` process owns every account's TradingNode via the single `handles`
map. If that process restarts (deploy, OOM, crash, code reload), every account's node is
affected at once — directly violating the council's "never stop all at once" /
container-per-account isolation ruling.

## Discussion Log

### Round 1 — three requirement-shaping decisions (2026-05-31)

**Q1 — Crash-recovery semantics for the affected account's own node.**
(Other accounts continuing untouched is settled — it's the entire point of PR 2.)
→ **Auto-restart + reconcile.** When an account's supervisor/owner crashes or
restarts, the owner automatically restarts that account's TradingNode and runs IB
reconciliation (`LiveExecEngineConfig(reconciliation=True)`) to recover open
orders/positions. Self-healing fleet intent. (Implication: PR 2 must verify
reconciliation actually completed before the restarted node accepts new orders —
Nautilus gotcha #10; and the restart must honor the account's existing halt latch,
i.e. a halted/drained account does NOT auto-restart.)

**Q2 — New operator-visible surface?**
→ **Add per-account supervisor health to `/live/status` + `msai live status` CLI.**
Each account row gains a supervisor/owner health field (alive, owner-id/pid,
last-heartbeat-age, last-restart-at). The operator can see "account A's supervisor
up; account B's restarted 2m ago." Makes the isolation property directly observable
AND gives PR 2 real user-journey E2E UCs (API + CLI surfaces). → PR 2 IS user-facing;
E2E is REQUIRED, not N/A.

**Q3 — Acceptance drill (scope-of-proof gate).**
→ **Kill A's owner, prove B untouched + A recovers.** Two accounts live (Shape B:
LVP + HVP). Kill/restart account A's supervisor-owner. Assert: (a) B keeps receiving
bars and stays drainable/haltable throughout the event; (b) A's TradingNode recovers
per the auto-restart-+-reconcile policy; (c) the PR-1 Shape B halt-isolation drill
still passes (account-scoped halt latches unaffected). This is operator-driven (live
IB), runs before merge per `feedback_all_continuity_gates_before_pr.md`.

---

## Refined Understanding

### Personas

- **Operator (Pablo)** — runs the fleet; needs to see per-account supervisor health
  and trust that one account's failure doesn't cascade. Restarts/deploys the stack
  and must know the blast radius is one account, not all.
- **The fleet itself (autonomous)** — the per-account owner that detects a dead node
  and auto-restarts + reconciles it without human intervention (within the halt-latch
  constraint).

### User Stories (Refined)

- **US-1** — As the operator, when I restart/redeploy the live-supervisor layer (or
  one account's supervisor crashes), the OTHER accounts' TradingNodes keep trading
  uninterrupted — I never lose the whole fleet at once.
- **US-2** — As the operator, when one account's TradingNode dies, its owner
  auto-restarts it and reconciles open IB positions/orders, UNLESS that account is
  halted/drained (in which case it stays down until I `/resume`).
- **US-3** — As the operator, I can see each account's supervisor/owner health
  (alive, owner-id, last-heartbeat-age, last-restart) on `/live/status` and
  `msai live status`, so I can tell at a glance which accounts are healthy and which
  recently recovered.
- **US-4 (drill, operator-run)** — With LVP + HVP both live, I kill account A's
  supervisor-owner and observe B trading throughout + A self-recovering; the PR-1
  Shape B halt-isolation drill still passes.

### Non-Goals (explicit — deferred to later PRs)

- BrokerAccount first-class entity / CRUD / UI (PR 3).
- Dashboard account selector + per-account read filtering (PR 4).
- Per-account risk caps + fleet-aggregate ledger (PR 5).
- Data-stale auto-halt (PR 1b).
- Any change to the credentials model (PR 3 / council Option B').

### Key Decisions

- Crash recovery = **auto-restart + reconcile**, gated by the account halt latch
  (halted account does NOT auto-restart).
- PR 2 **is user-facing** — adds per-account supervisor health to `/live/status` +
  CLI → E2E required (API + CLI UCs).
- Acceptance = **kill-A-owner drill** proving B-untouched + A-recovers + PR-1 Shape B
  halt-isolation still green.
- Must NOT regress any PR 1 behavior (halt latches, /drain, /resume, GatewayRouter
  binding enforcement, symbology shim, Shape B two-gateway topology).

### Open Questions (Remaining → resolved in Phase 3, not here)

- [ ] **Implementation shape (DESIGN, not requirement):** per-account supervisor
      PROCESSES (one OS process per account) vs. a thin control-plane router that does
      NOT parent node lifetimes (self-supervising nodes + ownership records). Council
      allows either; the Phase 3 brainstorming + approach-comparison + Contrarian gate
      picks one. Noted here as the load-bearing design fork.
- [ ] **Owner-health heartbeat mechanism (DESIGN):** how the owner-liveness signal is
      produced/stored (Redis heartbeat key, DB row, process-table probe) — design phase.

### Status: Complete — ready for `/prd:create pr-2-per-account-supervisors`
