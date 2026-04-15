# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field   | Value |
| ------- | ----- |
| Command | none  |

## Done

- Hybrid merge PR#3 merged (2026-04-13): 18 tasks, 99 files, ~15K lines
- Docker Compose parity PR#4 merged (2026-04-13): 12 gaps fixed, all 10 containers running
- IB Gateway connected: 6 paper sub-accounts verified (DFP733210 + DUP733211-215, ~$1M each)
- Databento API key configured
- Phase 2 parity backlog cleared 2026-04-15: PR #6 portfolio, #7 playwright e2e, #8 CLI sub-apps, #9 QuantStats intraday, #10 alerting API, #11 daily scheduler tz — all merged after local merge-main-into-branch conflict resolution (1147 tests on final branch)
- First real backtest 2026-04-15 14:01 UTC: AAPL.NASDAQ + SPY.ARCA Databento 2024 full year, 258k bars, 4,448 trades, QuantStats HTML report via `/api/v1/backtests/{id}/report`. Core goal from Project Overview met.
- Alembic migration collision fixed: PR #6 + PR #15 both authored revision `k9e0f1g2h3i4`; portfolio rechained to `l0f1g2h3i4j5` (commit 3139d75).
- Phase 2 #4 council (5 advisors + chairman): rejected verbatim Option A (867 LOC) and framed Option B (300 LOC); mandated paper-IB kill-all drill as go/no-go gate
- Phase 2 #4 drill executed (2026-04-15 04:00 UTC): exposed 3 P0 live-stack bugs blocking any `/live/start` (profile-gate, supervisor silent-fail, IB host/port drift)
- Phase 2 #4 — live trade persistence merged (PR #15): broker_trade_id column + partial unique dedup + ON CONFLICT DO NOTHING path from OrderFilled → trades; audit row mismatch now visible (Codex review P1+P2 both addressed)
- Live-stack kill-all drill PASSED 2026-04-15 05:37: EUR/USD.IDEALPRO paper BUY filled → /kill-all → SELL reduce_only filled → PositionClosed in 187 ms. Layer 3 (SIGTERM + manage_stop=True) verified.
- Live-stack sprint complete 2026-04-15 06:00 UTC — all 3 P0s fixed in separate branches ready for PR+merge:
  - P0-B `fix/live-supervisor-silent-spawn-fail` (f324f0c): LiveCommandBus.\_publish now calls ensure_group before xadd so commands don't vanish when consumer group is positioned at `$`; supervisor **main**.py configures stdlib logging.basicConfig so its logs are visible in docker logs
  - P0-C `fix/ib-gateway-env-var-drift` (6f02767): settings.ib_host/ib_port accept AliasChoices on IB_GATEWAY_HOST + IB_GATEWAY_PORT_PAPER env names
  - P0-A `fix/live-supervisor-default-profile` (08b34a9): /live/start returns 503 fast when no supervisor consumer is registered (vs silent 504 timeout)

## Now

**First real backtest achieved** (2026-04-15). Full year AAPL + SPY 2024 Databento data ingested (258,150 bars in 12 s), EMA Cross backtest ran in 11 s producing **4,448 trades**, QuantStats HTML report (365 KB) generated and fetchable via `/api/v1/backtests/{id}/report`. Core goal from Project Overview is met.

**During the session, two pre-existing bugs surfaced and were worked around, not yet fixed:**

1. **Stale catalog bug** — `ensure_catalog_data` returns `already_populated` when ANY bar parquet exists for the instrument, even if the on-disk raw data now covers a wider range than the catalog. Worked around by `rm -rf /app/data/nautilus/data/{bar,equity}` before the run. Fix: detect date-range delta and rebuild the subset, or make the check time-bucketed.
2. **Stale subprocess signature** — long-running worker containers cache the `_run_in_subprocess` import in memory. After PR #5 changed its signature from 2 args to 1, containers that predated the merge kept invoking the 2-arg form and every backtest failed with "takes 1 positional argument but 2 were given". Fixed by restarting `job-watchdog`. Fix: the docker-compose dev image should bust on source change, or entrypoints should force a `compileall` at start.

## Next

1. Apply migration (done — already at head `l0f1g2h3i4j5`).
2. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex).
3. File follow-ups for (a) stale catalog detection, (b) worker-container stale-import hygiene, (c) EMA backtest PnL / win-rate / sharpe all zero (trade extraction ran, PnL scoring didn't).
4. Other known follow-ups: `READ_ONLY_API=no` compose default; `/account/health` probe never-started bug; `/live/positions` gap; deployment row status stays `starting`; audit lifecycle race auto-heal.
