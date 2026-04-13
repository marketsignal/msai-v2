# CONTINUITY

## Goal

Merge the best Codex features into the Claude version: research engine (Optuna), graduation pipeline, portfolio management, daily universe, job watchdog, compute slots, strategy templates, specialized workers. Claude is the foundation — Codex's JSON file persistence replaced with proper DB models.

## Workflow

| Field     | Value                           |
| --------- | ------------------------------- |
| Command   | /new-feature hybrid-merge       |
| Phase     | 4 — Execute                     |
| Next step | Task 13: Frontend Research page |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified
- [x] PRD created — docs/user-stories.md (38 stories, 26 must-haves)
- [x] Research done — 5 parallel agents explored both codebases, Codex second opinion
- [x] Design guidance loaded — N/A for backend merge
- [x] Brainstorming complete — 3 approaches, feature-vertical chosen
- [x] Approach comparison filled
- [x] Contrarian gate passed (skip) — Codex confirmed hybrid/Claude-first
- [x] Plan written — docs/plans/2026-04-12-hybrid-merge-implementation.md
- [x] Plan review loop (2 iterations) — 18 execution rules. PASS.
- [x] TDD execution complete (18/18 tasks done — 1133 tests pass)
- [x] Code review loop (4 iterations) — iter1: 1P0+7P1+5P2, iter2: 5P1+2P2, iter3: 4P1+3P2, iter4: 1P1+6P2. All fixed. Only accepted P2s remain (TOCTOU, serial exec, Optuna study key). PASS.
- [ ] Simplified
- [ ] Verified (tests/lint/types)
- [ ] E2E use cases tested (if user-facing)
- [ ] Learnings documented (if any)
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

## Context

Design doc: `docs/plans/2026-04-12-hybrid-merge-design.md`
Implementation plan: `docs/plans/2026-04-12-hybrid-merge-implementation.md`
User stories: `docs/user-stories.md` (38 stories, 26 must-haves)
Comparison: `docs/claude-vs-codex-comparison.md`

Key decisions:

- Claude version is the foundation (stronger Nautilus integration, production infra)
- Codex features ported with DB persistence (not JSON files)
- 18 tasks in dependency order
- Live trading engine, risk management, security master are untouched
- Single IB account for MVP, multi-account-ready architecture
- Feature-vertical approach: model → service → API → UI per feature

## Done

- Task 1 (892e6fc): Backtest lifecycle fields — 5 columns + migration + populate in API/worker + fixed Redis URL parser
- Task 2 (d81f5d5): 8 new DB models + migration + 61 tests
- Task 3 (604a60a): Pydantic schemas — 40 tests
- Task 4 (5aae72d): Compute slots Redis semaphore — 11 tests
- Task 5 (27f33e0): Job watchdog + arq cron + ResearchJob lifecycle fields — 13 tests
- Task 6 (e6a85d2): Asset universe service + 4 API endpoints + nightly ingest updated — 14 tests
- Task 7 (ac9a480): Research engine core (~800 LOC) — Optuna, parameter sweeps, walk-forward CV — 29 tests
- Task 8 (9ea1cf1): Research worker + 6 API endpoints + enqueue function — 13 tests
- Task 9 (9d05cd9): Graduation pipeline with enforced stage transitions + 5 API endpoints — 31 tests
- Task 10 (f501226): Portfolio management + 7 API endpoints + worker — 22 tests
- Task 11 (1364e0d): Strategy templates + 2 API endpoints — 22 tests
- Task 12 (4a73279): Docker Compose workers (research + portfolio) + ingest settings — 10 tests
- Fix rounds: 0ca7382, 1b6fdf5, 8c85e52, bf796f9, 084cb35, 1870be8, 64c53d8 (7 fix commits from 4 review iterations)
- Task 13 (af64d91, fc51649): Frontend Research page — list page with KPI cards + polling, launch form dialog, detail page with trials + promote button
- Task 14 (ca75dc3): Frontend Graduation page — kanban board with 9 stage columns, detail panel with stage advance + transition history
- Task 15 (8aa03e2): Frontend Portfolio page — portfolios table, create form with allocations, run backtest dialog, runs table with metrics
- Task 16 (247da53): Strategy governance — AST-based validation, blocked imports/dangerous calls, governance_status column — 11 tests
- Task 17 (aa1c3f7): Data lineage — nautilus_version, python_version, data_snapshot on backtests + describe_catalog() — 8 tests
- Task 18 (6f37616): Integration tests — research lifecycle + graduation stage machine — 23 tests

## Now

Phase 5: Quality gates — code review loop

## Next

Simplify → verify → commit → push → PR
