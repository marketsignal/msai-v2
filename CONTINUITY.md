# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                                                       |
| --------- | --------------------------------------------------------------------------- |
| Command   | /fix-bug alerting-api-history                                               |
| Phase     | 5.1 — Code review loop                                                      |
| Next step | Await Codex iter 3 verdict; PR toolkit already says READY. Then commit + PR |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified
- [x] Searched existing solutions (first port, no prior solution)
- [x] Researched Codex port (`AlertingService`, schemas, router, `alerts_path`)
- [x] Systematic debugging — capability gap + Codex iter 1 surfaced real cross-process race
- [x] TDD fix execution complete
- [x] Code review loop (9 iterations) — PASS. 997 tests, both reviewers converged clean on P3-only. PR toolkit iter 9 "Terminal. Ship it." Codex's cascade was narrow+narrowing throughout; final port strictly more robust than Codex reference on every axis.
- [x] Simplified (helper extraction `_valid_alerts` in iter 6; iter 9 consolidated executor pattern)
- [x] Verified (992 unit tests pass, ruff clean, mypy clean)
- [x] E2E — N/A: internal operational surface; router is purely read-only from file history already covered by unit tests
- [x] Learning documented (`docs/solutions/alerting/api-history-and-cross-process-race.md`)
- [x] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

## Done

- Phase 2 #1 QuantStats intraday (PR #9 OPEN, Codex + PR-toolkit clean, merged queued)
- Phase 1 #1 portfolio orchestration (PR #6 OPEN, 10-iter reviewed)
- Phase 1 #2 Playwright e2e harness (PR #7 OPEN, stacked on #6)
- Phase 1 #3 CLI sub-apps (PR #8 OPEN, 27 commands)

## Now

**Phase 2 #2 in progress**: Alerting API + history. 6 review iterations so far; convergent (iter 1 found P1s, iter 6 only P3 DRY). PR toolkit iter 6 READY TO MERGE. Awaiting Codex iter 6. `AlertingService` (Codex port) + `alerting_service` singleton + `GET /api/v1/alerts/` router + `alerts_path` config. SMTP `AlertService.send_alert` records history best-effort (storage failure no longer blocks email). Cross-process race fixed via `fcntl.flock` on a sidecar lockfile. Defensive `_read_payload` + `_coerce_alerts_list` self-heal from operator hand-edits of any shape (invalid JSON, wrong top-level type, non-list `alerts` field, non-dict rows). 38 alerting tests + 978 total pass (including multiprocess regression guard). PR toolkit iter 3 verdict: READY TO MERGE. Awaiting Codex iter 3.

## Next

1. Close Phase 2 #2 (this branch) → PR → merge
2. Phase 2 #3 Daily scheduler timezone-aware (configurable tz/hour/minute + state file)
3. Phase 2 #4 LiveStateController + snapshot builders (5s Redis snapshots)
4. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex)
