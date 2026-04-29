# Implementation Plan — Developer-Journey How-Tos

**Goal:** Author 9 how-to documents in `docs/architecture/` covering the developer's path through MSAI v2 from blank repo to live P&L. Each doc carries a component diagram, parity table (API/CLI/UI), one shared internal sequence diagram, and Common-Failures / Idempotency / Rollback / Key-Files sections with file:line citations.

**Architecture:** Pure documentation. No code changes. Layout follows `mcpgateway/docs/architecture/how-*-works.md` style adapted to MSAI's three-surface (API/CLI/UI) parity. Updates `docs/architecture/README.md` reading order. Single PR.

**Tech Stack:** Markdown only (with optional ASCII + 1 trial Mermaid diagram in 00).

---

## Approach Comparison

### Chosen Default

Journey-ordered 9-doc set (00 narrative + 8 subsystem deep-dives) with parity tables and one post-entry sequence diagram per operation.

### Best Credible Alternative

Surface-first split: one doc per API/CLI/UI surface listing every operation, plus separate "what's behind it" docs for engines.

### Scoring (fixed axes)

| Axis                  | Default (journey)          | Alternative (surface-first)                    |
| --------------------- | -------------------------- | ---------------------------------------------- |
| Complexity            | M (9 docs, repeated shape) | M (3 surfaces × N operations, more cross-refs) |
| Blast Radius          | L (docs only)              | L (docs only)                                  |
| Reversibility         | H (just edit/delete)       | H (just edit/delete)                           |
| Time to Validate      | L (read-aloud test)        | M (cross-doc consistency harder)               |
| User/Correctness Risk | M (must reflect real code) | H (3-way parity errors compound)               |

### Cheapest Falsifying Test

Write `00-developer-journey.md` first; if a developer cold-reading it can't navigate to "where do I add a symbol?" within 30 seconds, the journey ordering is wrong. < 30 min spike.

## Contrarian Verdict

**VALIDATE** — Codex consult 2026-04-28 (gpt-5.4 xhigh, general mode) explicitly endorsed the journey ordering and added two real pivots Claude missed: (a) split walk-forward into its own `how-research-and-selection-works.md` doc; (b) split portfolio into backtest-portfolio (allocation) vs live-portfolio (deployment) — both adopted. Codex also rejected per-surface "how to view" docs and prescribed parity table + ONE shared sequence diagram per operation. All 5 questions (A–E) answered with strong opinions and integrated into the plan.

The Approach Comparison + Codex verdict together satisfy the Phase 3.1c contrarian gate per `feedback_skip_phase3_brainstorm_when_council_predone.md` ("council verdict IS what 3.1c delegates to") — Codex's general-mode output here functions as the contrarian validation layer.

---

## Files

### New (9 docs)

| Path                                                       | Approx. lines |
| ---------------------------------------------------------- | ------------- |
| `docs/architecture/00-developer-journey.md`                | 300           |
| `docs/architecture/how-symbols-work.md`                    | 700           |
| `docs/architecture/how-strategies-work.md`                 | 600           |
| `docs/architecture/how-backtesting-works.md`               | 700           |
| `docs/architecture/how-research-and-selection-works.md`    | 600           |
| `docs/architecture/how-graduation-works.md`                | 500           |
| `docs/architecture/how-backtest-portfolios-work.md`        | 700           |
| `docs/architecture/how-live-portfolios-and-ib-accounts.md` | 800           |
| `docs/architecture/how-real-time-monitoring-works.md`      | 700           |

Total ~5,600 lines across 9 files.

### Modified

| Path                          | Change                                                                  |
| ----------------------------- | ----------------------------------------------------------------------- |
| `docs/architecture/README.md` | Add `## Subsystem Deep Dives — Developer Journey` section linking all 9 |

---

## Per-doc outlines

### 00. `00-developer-journey.md` (~300 lines)

- TL;DR: "From a blank repo to live P&L in 8 steps"
- Component diagram (Nautilus-style, ASCII): SYMBOLS · STRATEGIES · BACKTESTS · RESEARCH · GRADUATION · PORTFOLIOS · LIVE · MONITORING with arrows showing GraduationCandidate flow + IB account binding
- 8-step narrative (one paragraph per step), each linking to its how-to
- "If you only read three documents, read these" subsection
- Trial Mermaid `flowchart LR` diagram (decide based on GitHub render quality)

### 01. `how-symbols-work.md` (~700 lines)

Citations to gather from: `backend/src/msai/api/symbol_onboarding.py`, `backend/src/msai/api/instruments.py`, `backend/src/msai/services/symbol_onboarding/`, `backend/src/msai/services/security_master/`, `backend/src/msai/workers/`, `backend/src/msai/cli.py` (symbols + instruments sub-apps), `frontend/src/app/data-management/page.tsx`, `alembic/versions/*registry*.py`.

- §1 Concepts: `SymbolOnboardingRun`, `instrument_definitions`, `instrument_aliases`, asset-class taxonomy, futures alias-rotation
- §2 Parity table:
  - Onboard new symbols (POST /api/v1/symbols/onboard | `msai symbols onboard` | `/data-management`)
  - Poll status (GET /symbols/onboard/{id}/status | `msai symbols status` | data-management UI)
  - Repair failed symbols (POST /symbols/onboard/{id}/repair | `msai symbols repair` | retry button)
  - View readiness (GET /symbols/readiness | `msai symbols readiness` | row badge)
  - Refresh IB qualification (POST /api/v1/instruments/refresh | `msai instruments refresh` | per-row action)
- §3 Sequence diagram: surface → router → `_enqueue_and_persist_run` → arq → `run_symbol_onboarding` → `_onboard_one_symbol` → bootstrap (Databento) → ingest → coverage check → optional IB qualification → status update
- §4 See/Verify: `/data-management` rows, `msai data-status`, log scan
- §5 Common failures: `BOOTSTRAP_AMBIGUOUS`, `BOOTSTRAP_UNAUTHORIZED`, `IB_TIMEOUT`, `INGEST_FAILED`, `COVERAGE_INCOMPLETE` (cite mapping in `symbol_onboarding.py:_suggest_next_action`)
- §6 Idempotency: digest-based job IDs (`compute_blake2b_digest_key`), parent-run preservation on repair
- §7 Rollback/Repair: `/repair` endpoint, manual alias close-out via runbook
- §8 Key Files

### 02. `how-strategies-work.md` (~600 lines)

Citations: `backend/src/msai/api/strategies.py`, `backend/src/msai/services/strategy_registry.py`, `backend/src/msai/models/strategy.py`, `strategies/*.py`, `frontend/src/app/strategies/`, `backend/src/msai/cli.py` (strategy sub-app).

- §1 Concepts: filesystem-as-source-of-truth (Phase 1), `code_hash`, `git_sha`, `ImportableStrategyConfig`, `FailureIsolatedStrategy`, `__init_subclass__` event-handler wrapping
- §2 Parity table:
  - List strategies (GET /strategies/ | `msai strategy list` | /strategies)
  - Show one (GET /strategies/{id} | `msai strategy show` | /strategies/[id])
  - Validate (POST /strategies/{id}/validate | `msai strategy validate` | validate button)
  - Update default config (PATCH /strategies/{id} | n/a (CLI) | edit form)
  - Delete (DELETE /strategies/{id} | n/a | delete button)
  - Authoring: drop file in `strategies/` + commit (no surface — explicit Phase 1 decision; ref decision-log)
- §3 Sequence: GET /strategies → `sync_strategies_to_db` → filesystem scan → upsert → return rows
- §4 See/Verify: `/strategies` page, `msai strategy list`, validation pass
- §5 Common failures: invalid Strategy class, missing file, schema-extraction failure
- §6 Idempotency: re-sync produces same rows; `code_hash` stable across calls
- §7 Rollback: revert the file, re-sync — DB row retained; backtests still reference old `code_hash`
- §8 Key Files

### 03. `how-backtesting-works.md` (~700 lines)

Citations: `backend/src/msai/api/backtests.py`, `backend/src/msai/workers/backtest_worker.py`, `backend/src/msai/services/nautilus/backtest_runner.py`, `backend/src/msai/services/security_master/`, `backend/src/msai/cli.py` (backtest sub-app), `frontend/src/app/backtests/`.

- §1 Concepts: arq job lifecycle, BacktestRunner config, venue pinning (SIM), data lineage stamping
- §2 Parity table:
  - Run backtest (POST /backtests/run | `msai backtest run` | "New backtest" form)
  - Poll status (GET /backtests/{id}/status | `msai backtest status` | results page polling)
  - Get results (GET /backtests/{id}/results | `msai backtest show` | results page)
  - Download report (GET /backtests/{id}/report | `msai backtest report --download` | iframe + download)
  - History (GET /backtests/history | `msai backtest history` | /backtests)
- §3 Sequence: API → arq enqueue → backtest worker → strategy resolve + `code_hash`/`git_sha` capture → `SecurityMaster.resolve_for_backtest(start=)` → Parquet load via DuckDB → BacktestRunner → results materialization → QuantStats HTML
- §4 See/Verify: results page series rendering, trade log pagination, QuantStats iframe
- §5 Common failures: instrument not pre-loaded (gotcha #9), venue mismatch (gotcha #4), strategy class errors
- §6 Idempotency: same `(strategy_id, params, date_range)` produces deterministic results given pinned `nautilus_version` + `data_snapshot`
- §7 Rollback: delete result row + Parquet artifacts; re-run produces fresh
- §8 Key Files

### 04. `how-research-and-selection-works.md` (~600 lines)

Citations: `backend/src/msai/api/research.py`, `backend/src/msai/services/research/`, `backend/src/msai/workers/research_worker.py`, `backend/src/msai/cli.py` (research sub-app), `frontend/src/app/research/`, `frontend/src/components/research/launch-form.tsx`.

- §1 Concepts: parameter sweep, walk-forward CV, OOS folds, `GraduationCandidate`
- §2 Parity table:
  - Launch research run (POST /research/launch | `msai research launch` | /research launch form)
  - List runs (GET /research/ | `msai research list` | /research list)
  - Show run (GET /research/{id} | `msai research show` | /research/[id])
  - Cancel (POST /research/{id}/cancel | `msai research cancel` | cancel button)
  - Mark candidates for graduation (POST /research/{id}/graduate | n/a | "Promote" button)
- §3 Sequence: launch → fan-out backtests across param grid → walk-forward across windows → OOS aggregation → ranking → selection → `GraduationCandidate` rows
- §4 See/Verify: research run dashboard, OOS plot, top-N table
- §5 Common failures: bad sweep config, walk-forward window too tight, no positive folds
- §6 Idempotency: same (strategy_id, sweep_config, window_config) → same digest
- §7 Rollback: cancel run, delete `GraduationCandidate` rows
- §8 Key Files

### 05. `how-graduation-works.md` (~500 lines)

Citations: `backend/src/msai/api/graduation.py`, `backend/src/msai/services/graduation/`, `backend/src/msai/models/graduation.py`, `frontend/src/app/graduation/`.

- §1 Concepts: graduation as a gate (not a step in research) — promoting `GraduationCandidate` → vetted strategy + symbol pair ready for portfolio inclusion. Risk overlay validation. Immutability stamping at graduation time.
- §2 Parity table:
  - List candidates (GET /graduation/ | `msai graduation list` | /graduation)
  - Show one (GET /graduation/{id} | `msai graduation show` | /graduation/[id])
  - Approve (POST /graduation/{id}/approve | n/a | approve button)
  - Reject (POST /graduation/{id}/reject | n/a | reject button)
- §3 Sequence: candidate → risk overlay checks → metadata freeze (code_hash, git_sha, nautilus_version, walk-forward fingerprint) → status → APPROVED → eligible for portfolio
- §4 See/Verify: /graduation queue, approval log
- §5 Common failures: risk-overlay reject (max position, daily-loss cap), missing OOS coverage
- §6 Idempotency: re-approving a candidate is a no-op
- §7 Rollback: revoke graduation (audit-preserved)
- §8 Key Files

### 06. `how-backtest-portfolios-work.md` (~700 lines)

Citations: `backend/src/msai/api/portfolio.py`, `backend/src/msai/services/portfolio_service.py`, `backend/src/msai/cli.py` (portfolio sub-app), `frontend/src/app/portfolio/`.

NOTE: This is the **backtest-portfolio** (allocation of GraduationCandidates), distinct from the **live-portfolio** (doc 07). Codex's split is binding — the file `portfolio_service.py:115` allocates GraduationCandidates; this is its lane.

- §1 Concepts: portfolio = mapping of (strategy, symbol) pairs with weight allocations, drawn from APPROVED graduation candidates. Portfolio backtest aggregates per-component results. Walk-forward at portfolio level uses per-fold component weights.
- §2 Parity table:
  - Create portfolio (POST /portfolio/ | `msai portfolio create` | /portfolio "+")
  - List portfolios (GET /portfolio/ | `msai portfolio list` | /portfolio)
  - Show portfolio (GET /portfolio/{id} | `msai portfolio show` | /portfolio/[id])
  - Run portfolio backtest (POST /portfolio/{id}/run | `msai portfolio run` | run button)
  - Show run (GET /portfolio/runs/{run_id} | `msai portfolio runs show` | run page)
- §3 Sequence: portfolio create → component validation (all candidates APPROVED) → backtest run dispatched → per-component fan-out → aggregation → contribution analysis → walk-forward
- §4 See/Verify: portfolio detail page (allocation chart + walk-forward plot), contribution table
- §5 Common failures: non-approved candidate included, weights don't sum to 1, conflicting symbols
- §6 Idempotency: same composition + dates → same result digest
- §7 Rollback: delete run row + artifacts; portfolio definition stays
- §8 Key Files

### 07. `how-live-portfolios-and-ib-accounts.md` (~800 lines)

Citations: `backend/src/msai/api/portfolios.py` (NB: plural — different from `portfolio.py`), `backend/src/msai/api/live.py`, `backend/src/msai/api/account.py`, `backend/src/msai/live_supervisor/`, `backend/src/msai/services/live/`, `backend/src/msai/cli.py` (live + account sub-apps), `frontend/src/app/live-trading/`, `frontend/src/app/settings/page.tsx`.

This is the **live-portfolio** (LivePortfolio → Revision → Deployment chain) — distinct from doc 06. Codex's split: `portfolios.py:184` + `live.py:245` are the live lanes.

- §1 Concepts: `LivePortfolio` (mutable container) → `LivePortfolioRevision` (immutable, frozen at deploy time) → `LiveDeployment` (running subprocess). IB account types (paper DU... vs live U...), port mapping (4002 paper, 4001 live), per-deployment account binding.
- §2 Parity table:
  - Create live portfolio (POST /live-portfolios/ | `msai portfolio live-create` | live-trading "+")
  - Edit revision (PATCH /live-portfolios/{id} | n/a | edit form)
  - Add IB account (POST /account/add | `msai account add` | settings page)
  - Deploy portfolio (POST /live/start-portfolio | `msai live start` | deploy button)
  - List deployments (GET /live/status | `msai live status` | live-trading list)
  - Stop deployment (POST /live/stop | `msai live stop` | stop button)
  - Kill all (POST /live/kill-all | `msai live kill-all` | KILL button)
- §3 Sequence: deploy → revision freeze → risk validation → live_supervisor spawn TradingNode subprocess → IB Gateway connect (port 4002/4001) → instrument bootstrap → strategy load → bar events → order submit → command bus heartbeat
- §4 See/Verify: /live-trading deployment list, `msai live status`, IB Gateway log
- §5 Common failures: account/port mismatch (gotcha #6), instrument-bootstrap timeout, IB ClientID collision (gotcha #3), reconciliation timeout (gotcha #10)
- §6 Idempotency: revisions are immutable; same revision_id can re-deploy after stop
- §7 Rollback: stop → flatten positions (4-layer kill-all); revert via redeploying prior revision
- §8 Key Files

### 08. `how-real-time-monitoring-works.md` (~700 lines)

Citations: `backend/src/msai/api/websocket.py`, `backend/src/msai/services/live/`, `backend/src/msai/api/account.py`, `frontend/src/app/dashboard/page.tsx`, `frontend/src/app/live-trading/page.tsx`.

- §1 Concepts: WebSocket stream `/api/v1/live/stream/{deployment_id}`, first-message JWT auth (5s timeout), reconnect hydration from DB, message types (order, trade, status, risk_halt)
- §2 Parity table:
  - Connect to live stream (WS /live/stream/{id} | n/a CLI (read-only) | dashboard auto-connect)
  - View account summary (GET /account/summary | `msai account summary` | dashboard)
  - View positions (GET /account/portfolio | `msai account positions` | positions panel)
  - View IB health (GET /account/health | `msai account health` | health badge)
  - Switch active account (n/a API — implicit from deployment | `msai account switch` | account dropdown)
- §3 Sequence: WS connect → JWT first-message → DB hydrate (orders + trades + status + risk_halt) → live event subscription → push to client → reconnect on disconnect
- §4 See/Verify: dashboard P&L curve, position list, alert toasts
- §5 Common failures: JWT timeout (5s), reconnect storm, halt-flag confusion
- §6 Idempotency: hydration is read-only and replay-safe
- §7 Rollback: kill-all from any surface
- §8 Key Files

### `docs/architecture/README.md` patch

Add new section after the existing reading-order list:

```markdown
## Subsystem Deep Dives — Developer Journey

How to use the system end-to-end across API/CLI/UI:

0. [Developer Journey](00-developer-journey.md) — start here
1. [How Symbols Work](how-symbols-work.md)
2. [How Strategies Work](how-strategies-work.md)
3. [How Backtesting Works](how-backtesting-works.md)
4. [How Research and Selection Work](how-research-and-selection-works.md)
5. [How Graduation Works](how-graduation-works.md)
6. [How Backtest Portfolios Work](how-backtest-portfolios-work.md)
7. [How Live Portfolios and IB Accounts Work](how-live-portfolios-and-ib-accounts.md)
8. [How Real-time Monitoring Works](how-real-time-monitoring-works.md)
```

---

## Tasks

| ID  | Description                                                              | Depends on  | Writes                                                   |
| --- | ------------------------------------------------------------------------ | ----------- | -------------------------------------------------------- |
| R1  | Research cluster A: symbols + instruments + onboarding worker            | —           | scratch/citations-cluster-a.md                           |
| R2  | Research cluster B: strategies + backtesting                             | —           | scratch/citations-cluster-b.md                           |
| R3  | Research cluster C: research + graduation + backtest-portfolios          | —           | scratch/citations-cluster-c.md                           |
| R4  | Research cluster D: live-portfolios + IB accounts + real-time monitoring | —           | scratch/citations-cluster-d.md                           |
| D0  | Write `00-developer-journey.md` (sets the voice for the rest)            | R1,R2,R3,R4 | docs/architecture/00-developer-journey.md                |
| D1  | Write `how-symbols-work.md`                                              | R1, D0      | docs/architecture/how-symbols-work.md                    |
| D2  | Write `how-strategies-work.md`                                           | R2, D0      | docs/architecture/how-strategies-work.md                 |
| D3  | Write `how-backtesting-works.md`                                         | R2, D0      | docs/architecture/how-backtesting-works.md               |
| D4  | Write `how-research-and-selection-works.md`                              | R3, D0      | docs/architecture/how-research-and-selection-works.md    |
| D5  | Write `how-graduation-works.md`                                          | R3, D0      | docs/architecture/how-graduation-works.md                |
| D6  | Write `how-backtest-portfolios-work.md`                                  | R3, D0      | docs/architecture/how-backtest-portfolios-work.md        |
| D7  | Write `how-live-portfolios-and-ib-accounts.md`                           | R4, D0      | docs/architecture/how-live-portfolios-and-ib-accounts.md |
| D8  | Write `how-real-time-monitoring-works.md`                                | R4, D0      | docs/architecture/how-real-time-monitoring-works.md      |
| F1  | Patch `docs/architecture/README.md` reading order                        | D0–D8       | docs/architecture/README.md                              |
| F2  | Cross-link audit (every doc links to neighbors + 00)                     | F1          | (edits to all 9)                                         |

## Dispatch Plan

- **Phase A (parallel research, 4 agents):** R1, R2, R3, R4 — file-disjoint, all read-only
- **Phase B (serial gate-doc):** D0 — sets voice + diagram convention; must be hand-written first to lock the style. ~300 lines
- **Phase C (parallel doc writing, file-disjoint):** D1, D2, D3, D4, D5, D6, D7, D8 — 8 subagents, each writes one doc. Concurrency cap 3 (default; the docs are large enough that 5 may overcommit)
- **Phase D (serial finalization):** F1 (README patch) → F2 (cross-link audit + voice consistency pass)

Sequential override: NO. Each doc writes to a unique file path. The voice-consistency risk is mitigated by D0 being written first and referenced by every D1–D8 dispatch as the style anchor.

## Plan-review loop

- Iteration 0: this file (current)
- After Codex pass + my own re-read, increment counter and re-evaluate

---

## E2E Use Cases

**N/A — docs-only PR, zero user-facing behavior change.** No API/CLI/UI surfaces touched. Justification per `rules/testing.md` "When E2E can be skipped (N/A)" — purely internal documentation change.

---

## Open questions

- Mermaid render quality on GitHub for our specific markdown viewer — decide after writing 00.
- Whether to inline a "first-time setup" snippet in 00 (docker compose up + `/health` curl) — leaning yes, it grounds the narrative.
