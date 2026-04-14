# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field   | Value |
| ------- | ----- |
| Command | none  |

## Done

- Phase 1 #1 — Portfolio orchestration port (PR #6): open, 10 review iterations clean
- Phase 1 #2 — Playwright e2e harness (PR #7): open, stacked on #6, 16 specs green
- Phase 1 #3 — CLI sub-app restructure (this branch): 27 commands in 8 sub-apps, e2e tested against live stack, 1 bug found+fixed (ReadTimeout)

## Now

CLI verified against real backend — all 27 commands work. ReadTimeout handling bug fixed + 2 new tests. 24 CLI tests, 947 total pass. Awaiting Codex review → push → PR.

## Next

1. Codex review → push → PR for CLI sub-apps
2. Phase 2 P1 items: daily scheduler, alerting API, QuantStats intraday, strategy registry, LiveStateController
3. Merge PR #6 → #7 → CLI PR in sequence
