# PRD — Developer-Journey How-Tos

**Date:** 2026-04-28
**Author:** Pablo (driver), Claude (writer)
**Status:** Approved → Implementation
**Type:** Documentation (no code changes)

---

## Problem

The MSAI v2 codebase has solid macro-level architecture documentation (`platform-overview`, `system-topology`, `module-map`, `data-flows`, `live-trading-subsystem`, `nautilus-integration`, `decision-log`) but no journey-oriented walkthroughs that teach a developer **how to actually use the system end-to-end**. A new contributor cannot answer:

- How do I add a symbol I want to trade?
- How do I author a strategy and validate it?
- How do I backtest it, then iterate on parameters?
- How do I compose strategies into a portfolio and walk-forward it?
- How do I graduate a portfolio to live and wire it to an IB account?
- Where do I see real-time P&L and how do I kill a runaway deployment?

Each of these is exposed via three surfaces (API, CLI, UI) and the parity is non-trivial. Without a unified developer-journey reference, every new contributor (human or AI) spends hours reverse-engineering the surfaces.

## Goal

Author **9 how-to documents** in `docs/architecture/` covering the developer's actual path through the system, modeled on `mcpgateway/docs/architecture/how-*-works.md`. Each doc:

- Carries a Nautilus-style component diagram
- Tabulates API/CLI/UI parity per operation
- Includes ONE post-entry sequence diagram (not three per-surface ones)
- Closes with `Common failures`, `Idempotency / Retry`, `Rollback / Repair`, and `Key Files` sections
- Cites every claim with `path/file.py:LINE`

## Out of scope

- New code, new endpoints, new CLI commands, new UI pages
- Tutorials walking through writing a _specific_ strategy (we document _how_ the system loads strategies, not how to write a moving-average crossover)
- API reference / OpenAPI generation (that comes from FastAPI auto-docs)
- Operator runbooks (those live in `docs/runbooks/`)

## The 9 documents (Codex-validated 2026-04-28)

| #   | File                                     | Coverage                                                                                                                                                                                                    |
| --- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 00  | `00-developer-journey.md`                | Front-of-house narrative tying all 8 docs. Component diagram (Nautilus-style). The path from blank repo → first live deployment. ~300 lines, mostly links forward                                           |
| 01  | `how-symbols-work.md`                    | Symbol onboarding (API/CLI/UI), `symbol_onboarding` worker + `_onboard_one_symbol` seam, `instrument_definitions` + `instrument_aliases` registry, daily refresh, viewing the catalog                       |
| 02  | `how-strategies-work.md`                 | Authoring Python strategies in `strategies/` (git-only Phase 1), `code_hash`/`git_sha` capture, `ImportableStrategyConfig`, `FailureIsolatedStrategy`, validation endpoint, list/edit across all 3 surfaces |
| 03  | `how-backtesting-works.md`               | Single-strategy single-symbol backtest: submission → arq → BacktestRunner → results, QuantStats report, paginated trade log, results page                                                                   |
| 04  | `how-research-and-selection-works.md`    | Research sub-app: parameter sweeps, walk-forward CV, OOS validation, selection criteria, `GraduationCandidate` marking                                                                                      |
| 05  | `how-graduation-works.md`                | Graduation gate: `GraduationCandidate` → vetted strategy ready for portfolio inclusion, risk validation, immutability stamping                                                                              |
| 06  | `how-backtest-portfolios-work.md`        | Backtest portfolio = allocation of `GraduationCandidate`s (multi-strategy × multi-symbol); portfolio backtest, contribution analysis, portfolio-level walk-forward                                          |
| 07  | `how-live-portfolios-and-ib-accounts.md` | `LivePortfolio → Revision → Deployment` chain, IB account wiring (paper 4002 / live 4001), portfolio→account binding, deployment lifecycle, account switching                                               |
| 08  | `how-real-time-monitoring-works.md`      | WebSocket stream + reconnect hydration, dashboard P&L, account-scoped views, kill-switch, halt-flag flow                                                                                                    |

## Per-doc structure (validated by Codex)

```
# How <X> Works

[ASCII component diagram — Nautilus home-page style]

TL;DR — one paragraph + the 3 surfaces it lives on

## Table of Contents

## 1. Concepts & Data Model
## 2. The Three Surfaces (parity table)

| Intent | API | CLI | UI | Observe / Verify |
|--------|-----|-----|-----|-------------------|
| ...    | ... | ... | ... | ...               |

## 3. Internal Sequence (one shared diagram)
[ASCII sequence: surface → router → service → worker → storage]

## 4. See / Verify / Troubleshoot
## 5. Common Failures
## 6. Idempotency / Retry Behavior
## 7. Rollback / Repair
## 8. Key Files
| File | Role |
| ---- | ---- |
| `backend/src/msai/...` | ... |
```

## Diagram convention

ASCII for portability. **One** Mermaid diagram trial in `00-developer-journey.md`; if GitHub renders it cleanly, expand selectively. No PNG assets — they rot.

## Reading-order update

`docs/architecture/README.md` gets a new section "## Subsystem Deep Dives — Developer Journey" linking all 9 docs in order.

## Audience

- Primary: developers (human + AI assistants like Claude) onboarding to the codebase
- Secondary: future-Pablo who has forgotten the wiring details

## Success criteria

1. A developer cold-reading `00-developer-journey.md` can navigate to any of the 8 sub-docs and understand the surface they need.
2. Every claim in every doc cites `path/file.py:LINE`.
3. Each parity table covers all three surfaces (or notes "not exposed on this surface" with reason).
4. Each doc closes with the failure / retry / rollback triumvirate.
5. Total length: 4,500–7,500 lines across 9 files (avg 500-800 per file, with 00 ~300).

## Risks

- **Drift.** Docs that cite line numbers go stale fast. Mitigation: cite stable identifiers (function names, table names, route prefixes) alongside line numbers; treat line numbers as informational.
- **Over-coverage.** Tempting to document every code path. Mitigation: each doc answers the journey question, not "everything about this module."
- **Inconsistent voice.** 9 docs in one PR — risk of tonal drift. Mitigation: write 00 first, fix the voice, then write 01–08 as parallel subagents with the locked voice as a reference.

## Plan-of-attack

See `docs/plans/2026-04-28-how-tos-developer-journey.md` for task breakdown + dispatch plan.
