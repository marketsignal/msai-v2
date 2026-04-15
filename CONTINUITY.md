# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                      |
| --------- | ------------------------------------------ |
| Command   | /fix-bug quantstats-intraday-normalization |
| Phase     | 6 — Finish                                 |
| Next step | Commit + push + PR                         |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified (Codex CLI available)
- [x] Searched existing solutions (none for QuantStats intraday)
- [x] Researched Codex's fix (`_normalize_report_returns` groupby day + compound)
- [x] Systematic debugging complete (failing test written — import error reproduced bug)
- [x] TDD fix execution complete (18 tests green, ported helper verbatim from Codex)
- [x] Code review loop (1 iteration) — PASS. Codex xhigh + PR toolkit both clean on P0/P1/P2. PR toolkit's single P2 was self-labeled "not worth changing, user impact nil"; 3 of 4 P3 test-coverage nits addressed
- [x] Simplified (helper ported verbatim from Codex; no accidental complexity added)
- [x] Verified (tests: 972 pass, ruff clean on new code, mypy +1 env-only error — pandas.api.types stubs, same class as existing pandas stub error)
- [x] E2E — N/A (internal metric normalization, output changes only)
- [x] Learning documented (`docs/solutions/backtesting/quantstats-intraday-sharpe-inflation.md`)
- [x] State files updated (CONTINUITY + CHANGELOG)
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

## Done

- Phase 1 #1 portfolio orchestration (PR #6 OPEN, 10-iter Codex-reviewed)
- Phase 1 #2 Playwright e2e harness (PR #7 OPEN, 16 specs green)
- Phase 1 #3 CLI sub-apps (PR #8 OPEN, 27 commands, Codex + e2e reviewed)
- Hybrid merge PR#3 merged (2026-04-13): 18 tasks, 99 files, ~15K lines
- Docker Compose parity PR#4 merged (2026-04-13): 12 gaps fixed, all 10 containers running
- Codex ingestion + backtest IPC PR#5 merged (2026-04-14)

## Now

**Phase 2 #1 in progress**: QuantStats intraday normalization. Helper `_normalize_report_returns` ported verbatim from Codex. 18 new tests covering compound, pass-through, tz-aware/naive, empty, non-numeric, midnight-cross. PR toolkit review PASS. Codex xhigh review running.

## Next

1. Finish Phase 2 #1 QuantStats intraday (this branch) → PR → merge
2. Phase 2 #2 Alerting API + history (file-backed log + GET /api/v1/alerts/ router)
3. Phase 2 #3 Daily scheduler timezone-aware (configurable tz/hour/minute + state file)
4. Phase 2 #4 LiveStateController + snapshot builders (5s Redis snapshots)
5. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex)
