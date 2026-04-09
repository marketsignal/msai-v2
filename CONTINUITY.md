# CONTINUITY

## Goal

Wire the remaining production gaps in claude-version so the system is fully functional for backtesting, paper trading, and live trading via Interactive Brokers. Then regenerate documentation from the now-complete code.

## Workflow

| Field     | Value                                      |
| --------- | ------------------------------------------ |
| Command   | /new-feature msai-production-wiring        |
| Phase     | 4 — Execute                                |
| Next step | Task 11 (doc regeneration) then quality gates |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified
- [x] PRD created
- [x] Research done (gap analysis from doc audit served as research)
- [x] Design guidance loaded — N/A (backend wiring, no new UI design)
- [x] Brainstorming complete
- [x] Plan written
- [x] Plan review loop (2 iterations) — Claude found 3 P1s (fixed), Codex found 7 P1s (documented as corrections). No P0s. Snippets marked directional.
- [ ] TDD execution complete (Tasks 1-10 done, Task 11 remaining)
- [ ] Code review loop (0 iterations) — iterate until no P0/P1/P2
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

From the doc audit + gap analysis, 8 items need wiring:

1. Wire `/api/v1/live/positions` to PositionReader (~10 lines)
2. Wire `/api/v1/live/trades` to order_attempt_audits query (~15 lines)
3. Start ProjectionConsumer + StateApplier in FastAPI lifespan (~20 lines)
4. Wire real ib_async connection for account endpoints (~50 lines)
5. Add metrics counters at key trading lifecycle points (~30 lines)
6. Wire alert triggers to disconnect + daily loss + errors (~30 lines)
7. Wire MarketHoursService into RiskAwareStrategy (~5 lines)
8. Regenerate all docs/architecture/ from the now-complete code
