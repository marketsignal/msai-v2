# CONTINUITY

## Goal

Reach Codex parity (Phase 2 — 5 P1 items).

## Workflow

| Field     | Value                                        |
| --------- | -------------------------------------------- |
| Command   | /fix-bug daily-scheduler-timezone            |
| Phase     | 5.1 — Code review loop                       |
| Next step | Await Codex iter 4; commit + PR autonomously |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Researched Codex's `daily_scheduler.py` design
- [x] TDD execution complete (24 new tests)
- [x] Code review loop (5 iters) — PASS. Iter 1 P2 broad except + Codex P1 target_date + P2 long-ingest re-fire. Iter 2 Codex 2×P2 (config validation, atomic state). Iter 3 Codex P1 Databento window semantics. Iter 4 PR toolkit docstring + Codex P2 overnight schedules (session_offset_days). Iter 5 Codex 2×P2 (Polygon end-inclusive + multi-exchange) — documented as out-of-scope/pre-existing follow-ups (parquet dedup mitigates Polygon, multi-exchange needs per-asset schedules). PR toolkit iter 5: "Terminal. Ship it." 45 tests, 987 backend total.
- [x] Verified (987 unit tests pass, ruff + mypy clean)
- [x] Simplified (helper extraction _valid_alerts, consolidated loop handling)
- [x] E2E — N/A: internal scheduler, no user-facing changes
- [x] Learning documented (`docs/solutions/scheduling/daily-ingest-timezone-awareness.md`)
- [x] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

## Done

- Phase 2 #1 QuantStats intraday → PR #9 (open)
- Phase 2 #2 Alerting API + history → PR #10 (open, 9-iter Codex review, 52 tests)
- Phase 1 #1/#2/#3 portfolio + Playwright + CLI → PRs #6/#7/#8 (open)

## Now

**Phase 2 #3 in progress**: Daily ingest scheduler tz-aware. Replaced hardcoded `06:00 UTC` arq cron with `run_nightly_ingest_if_due` wrapper that consults tz/hour/minute/enable settings + atomic JSON state file. Preserves Claude's DB-backed asset universe + fallback. 40 tests including London/Tokyo parametrize, atomic-write-on-fsync-failure, hour/minute range validation, Databento end-exclusive window semantics, eager-claim idempotency across restarts. 4 review iterations — each Codex finding was real and narrowing. PR toolkit iter 4 "not terminal" on stale docstring → fixed.

## Next

1. Finish Phase 2 #3 → PR → next item
2. Phase 2 #4 LiveStateController + snapshot builders (5s Redis snapshots)
3. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex)
