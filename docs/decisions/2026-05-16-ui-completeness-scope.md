# UI Completeness Scope — Engineering Council Verdict

**Date:** 2026-05-16
**Branch:** `feat/ui-completeness`
**Mode:** Standalone `/council` (5 advisors + Codex chairman)
**Decision status:** Council recommended staging (Stage 1 + Stage 2). **User-overridden 2026-05-16 to single-PR scope** with binding technical constraints adopted in-PR rather than deferred. See §13.

## Background

After PR #67 (live workflow UI catch-up) and PR #68 (CLI 100% REST parity), six UI gaps remained from PR #67's "OUT OF SCOPE" list:

1. **Alerts list** — `GET /api/v1/alerts/` exists; no UI list view
2. **Strategy edit/delete** — read-only UI; `PATCH`/`DELETE /api/v1/strategies/{id}` endpoints exist
3. **Strategy templates scaffolder** — UI for adding new strategy Python files (Phase 1 decided git-only)
4. **Market-data browser** — `/market-data/bars/{symbol}` + `/symbols` + `/status` + `/ingest` exist
5. **Account health / broker portfolio** — `/account/summary` + `/portfolio` + `/health` exist
6. **Settings page cleanup** — hardcoded Admin badge, fake notification save, broken `/api/v1/admin/clear-data` button

The user invoked `/new-feature ui-completeness` picking "Audit + ALL six gap areas in ONE branch" (mirror PR #68's mega-PR shape) and explicitly asked the council to validate.

## Council Verdict

**Reject Approach C (all-six mega-PR).** Stage the work into two branches.

### Stage 1 (this branch)

**In scope:**

- Gap 6: Settings page cleanup
- Gap 1: Alerts list page
- Gap 2: Strategy **edit** only (PATCH)

**Conditional:**

- Gap 2 (strategy **delete**): blocked pending backend soft-delete semantics (see `backend/src/msai/api/strategies.py:197` TODO). UI delete ships only after backend resolves hard-delete vs soft-delete authority. Today's UI is unaffected.

**Out of scope (defer to Stage 2):**

- Gap 3: Strategy templates scaffolder
- Gap 4: Market-data browser
- Gap 5: Account/broker portfolio page

### Stage 2 prerequisites (per gap)

Each Stage 2 gap is blocked behind its own prerequisite, not on a unified timeline:

| Gap                            | Prerequisite before UI work                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gap 3 (templates scaffolder)   | Decision doc amending Phase 1 "git-only strategies" policy. The current scaffolder service (`services/strategy_templates.py`) silently contradicts CLAUDE.md "no UI uploads in Phase 1 — git-only". UI surface either honors the policy (cut feature) or the policy is explicitly amended in a ratified council/decision doc. Do not let UI implicitly reverse architecture.                                                                                                  |
| Gap 4 (market-data browser)    | Audit existing `frontend/src/app/market-data/page.tsx` (already 240 lines per advisor report). Determine: extension vs greenfield. Scope Stage 2 accordingly.                                                                                                                                                                                                                                                                                                                 |
| Gap 5 (account/portfolio page) | Fix `backend/src/msai/services/ib_account.py:62-89` per-request IB reconnect: pick one of (a) 15-30s TTL cache, (b) serve from existing `IBProbe` periodic state, (c) explicit Refresh-button gating. Cap unbounded `itertools.count(start=900)` client_id counter on line 29 with recycling window. Without this fix, the account UI competes with live TradingNode for IB client_id slots — Nautilus gotchas #3, #6 explicitly warn against unmanaged client_id allocation. |

### Universal verification constraint

All Stage 1 + Stage 2 UI work requires **Playwright MCP-driven verification by the implementer**, not operator handoff. Per memory rule `feedback_use_playwright_mcp_for_ui_e2e`.

## Consensus Points

- PR #68 (CLI completeness) is a **weak precedent** for this work. CLI parity was isolated wrappers over already-tested services; UI gaps involve rendered states, forms, persistence, browser flows, and rollback risk.
- The strategy templates scaffolder is **not simple UI completeness**. It reverses (or weakens) the ratified Phase 1 git-only strategy-authoring decision.
- Settings cleanup is real, visible, and should not be held hostage by larger feature risk. Bundling it with new-build features creates a **rollback hazard** — reverting to fix a market-data bug would also re-introduce the known-broken settings page.
- Approach C has poor review and rollback properties for solo-team UI work with no peer reviewer.

## Blocking Objections (binding for Stage 2 planning)

- Strategy templates scaffolder contradicts Phase 1 architecture.
- Account summary/portfolio endpoints hammer IB Gateway with per-request reconnects + unbounded client_id counter.
- Strategy DELETE currently hard-deletes despite backend TODO indicating soft-delete is needed.
- A mega-PR would make settings cleanup rollback-hostile.
- No CI-graduated Playwright coverage exists yet for these UI flows.
- A single combined verification report is insufficient for six unrelated surfaces.

## Minority Report

**The Simplifier** objected to C and B, arguing for settings-only cleanup and permanent removal of gap 3.

- **Overruled in part:** alerts and safe strategy edit are legitimate UI parity gaps worth including in Stage 1.
- **Deferred in part:** gap 3 is blocked unless the strategy-authoring policy changes.

**The Pragmatist** objected to C and proposed Stage 1 + Stage 2.

- **Adopted with modification:** strategy delete is not automatically included until backend semantics are fixed.

**The Maintainer** objected to C and preferred audit-only + follow-up branches.

- **Deferred:** a freestanding audit is useful, but settings/alerts/edit are small enough to ship with the first scoped branch.

**The Contrarian** objected to C citing rollback, delete semantics, templates policy, and missing UI verification.

- **Adopted as binding constraints.**

**The Scalability Hawk** conditionally accepted C only if account endpoint safety was fixed.

- **Deferred:** that condition becomes mandatory before gap 5, not a reason to approve C.

## Missing Evidence

- Exact completeness of existing `frontend/src/app/market-data/page.tsx` (preliminary advisor evidence: 240 lines, 5 hooks, 3 drawers — material existing surface).
- Whether the untracked draft files in main (am-home.md, settings-page.md, etc.) contain usable UI sketches or stale notes.
- Current frontend Playwright `tests/e2e/specs/` content (advisor evidence: directory does not exist yet).
- Desired backend semantics for strategy DELETE (soft vs hard — needs product call).
- Whether account portfolio data can be served from cached `IBProbe` state rather than fresh connect.

## Phase 3.1/3.1b/3.1c — PRE-DONE

Per memory rule `feedback_skip_phase3_brainstorm_when_council_predone`, brainstorming and approach comparison phases are skipped — this council verdict supersedes them. Phase 3.2 (writing-plans) and Phase 3.3 (plan-review loop) still run fresh.

## §13 User Override — Single PR, All Gaps, Binding Constraints Adopted In-Branch

**2026-05-16, post-council**, the user (Pablo) overrode the council's Stage 1 / Stage 2 staging recommendation.

**User directive (verbatim):** "I want all the UI completeness to happen on this PR, also for ui I want a great looking UI using the ui-design skill."

**Resolved scope:** All gaps the audit surfaces ship in this single branch. The council's binding **technical** constraints (account caching, strategy soft-delete, Phase 1 templates policy, Playwright spec graduation, universal Playwright MCP verification) move IN-SCOPE for this PR rather than being deferred to Stage 2. The chairman's recommendation of "do not pace by wall-clock; finish the staged scope rigorously" carries over to "finish the full scope rigorously."

**Why the override is legitimate:**

- Pablo is the user with directional authority. The council was invited to validate, not gate.
- Memory rule `feedback_dont_optimize_for_cost`: this is a hedge fund; rigor + reversibility are the constraints, not velocity or PR-size cleanliness.
- Memory rule `feedback_dont_propose_time_based_stops`: don't pace by sessions; finish what's started.
- Memory rule `feedback_use_playwright_mcp_for_ui_e2e`: implementer drives UI verification; not a blocker to single-PR.

**What this means in practice:**

1. **Templates scaffolder UI** — STILL blocked. The Phase 1 git-only policy must be resolved (amend OR cut backend service) before any templates UI ships. This is non-negotiable per the Maintainer + Contrarian + Simplifier + Pragmatist consensus. The decision-doc amendment is a deliverable of THIS PR; the UI follows the decision.
2. **Strategy DELETE** — STILL gated. Backend hard-delete is shipping today; the UI delete CTA waits for the backend soft-delete refactor IN this PR. Both ship together or neither ships.
3. **Account page (gap 5)** — Backend caching fix (`services/ib_account.py`) ships IN this PR, then UI follows.
4. **Playwright specs** — Authored for every shipped UI in this PR. Not deferred.

**Single-PR risk we accept:**

- Rollback re-introduces all six gap closures together. Mitigation: aggressive per-area E2E + Playwright spec gates before merge.
- Visual review burden on Pablo. Mitigation: Playwright MCP autonomous verification + screenshots persisted to `tests/e2e/reports/`.
- Codex convergence may take more iterations than PR #68. Acceptable; not paced by time.

## Next Step

Codex audit pass merging into `docs/audits/2026-05-16-ui-surface-audit.md` (Codex section appended below Claude's). Then PRD covering full scope (P0 + P1 + P2 + verification + backend safety + Phase 1 policy decision). Then Phase 2 research (Next.js 15 patterns, shadcn primitives for the new surfaces, IB caching patterns).
