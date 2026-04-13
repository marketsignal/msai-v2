# CONTINUITY

## Goal

Merge the best Codex features into the Claude version: research engine (Optuna), graduation pipeline, portfolio management, daily universe, job watchdog, compute slots, strategy templates, specialized workers. Claude is the foundation — Codex's JSON file persistence replaced with proper DB models.

## Workflow

| Field     | Value                         |
| --------- | ----------------------------- |
| Command   | /new-feature hybrid-merge     |
| Phase     | 4 — Execute                   |
| Next step | Task 8: Research worker + API |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified (brainstorming ran successfully in main session)
- [x] PRD created — docs/user-stories.md (38 stories, 26 must-haves)
- [x] Research done — 5 parallel agents explored both codebases, Codex second opinion reviewed comparison
- [x] Design guidance loaded — N/A for backend merge (frontend pages will use existing shadcn/ui)
- [x] Brainstorming complete — 3 approaches evaluated, feature-vertical recommended
- [x] Approach comparison filled — Bottom-up vs Feature-vertical vs Big-bang, feature-vertical chosen
- [x] Contrarian gate passed (skip) — Codex GPT-5.4 reviewed and confirmed hybrid/Claude-first approach
- [x] Plan written — docs/plans/2026-04-12-hybrid-merge-implementation.md
- [x] Plan review loop (2 iterations) — Iter 1: 2 P0 + 12 P1/P2 fixed (column sizes + 13 rules). Iter 2: Claude found 2 P1 (Redis parser, Jinja2); Codex found 4 P1 + 1 P2 (lifecycle population, uvloop reset, governance_status, describe_catalog, watchdog pattern). All fixed: rules expanded to 18. PASS.
- [x] TDD execution complete
- [x] Code review loop (1 iteration) — 1 P0 + 4 P1 + 2 P2 fixed
- [x] Simplified
- [x] Verified (tests/lint/types) — 868 unit tests pass
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

- Task 1 (892e6fc): Backtest lifecycle fields — 5 columns, migration, populate in API + worker, fixed Redis URL parser
- Task 2 (d81f5d5): 8 new DB models + migration + 61 tests
- Task 3 (604a60a): Pydantic schemas for research, graduation, portfolio, universe — 40 tests
- Task 4 (5aae72d): Compute slots (Redis semaphore) — 11 tests
- Task 5 (27f33e0): Job watchdog + arq cron wiring + lifecycle fields on ResearchJob — 13 tests
- Task 6 (e6a85d2): Asset universe service + 4 API endpoints + nightly ingest updated — 14 tests
- Task 7 (ac9a480): Research engine core (~800 LOC) — Optuna, parameter sweeps, walk-forward CV — 29 tests

## Now

Task 8: Research worker + API routes (wires engine to API layer)

## Next

Task 9: Graduation pipeline service + API
