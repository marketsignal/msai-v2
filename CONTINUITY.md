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
- Bug A FIXED (PR #16, 2026-04-15 19:27 UTC): catalog rebuild detects raw parquet delta via per-instrument source-hash marker; legacy markerless catalogs purged + rebuilt; basename collisions across years + footer-only rewrites both bump the hash; sibling bar specs survive purge. 5 regression tests + 2 Codex review iterations (P1 + 3×P2 all addressed).
- Live drill on EUR/USD.IDEALPRO 2026-04-15 19:30 UTC verified PR #15 trade persistence end-to-end: BUY @ 1.18015 + SELL (kill-all flatten) @ 1.18005 both wrote rows to `trades` with correct broker_trade_id, is_live=true, commission. ~376 ms kill-to-flat. Two minor follow-ups noted: side persists as enum int (1/2) not string (BUY/SELL); realized_pnl from PositionClosed not extracted into trades.
- Multi-asset live drill 2026-04-15 19:36-19:45 UTC FAILED to produce live fills on AAPL/MSFT/SPY/ES — see Now section. Demonstrated only EUR/USD reliably produces fills with current paper account/config.
- Phase 2 #4 council (5 advisors + chairman): rejected verbatim Option A (867 LOC) and framed Option B (300 LOC); mandated paper-IB kill-all drill as go/no-go gate
- Phase 2 #4 drill executed (2026-04-15 04:00 UTC): exposed 3 P0 live-stack bugs blocking any `/live/start` (profile-gate, supervisor silent-fail, IB host/port drift)
- Phase 2 #4 — live trade persistence merged (PR #15): broker_trade_id column + partial unique dedup + ON CONFLICT DO NOTHING path from OrderFilled → trades; audit row mismatch now visible (Codex review P1+P2 both addressed)
- Live-stack kill-all drill PASSED 2026-04-15 05:37: EUR/USD.IDEALPRO paper BUY filled → /kill-all → SELL reduce_only filled → PositionClosed in 187 ms. Layer 3 (SIGTERM + manage_stop=True) verified.
- Live-stack sprint complete 2026-04-15 06:00 UTC — all 3 P0s fixed in separate branches ready for PR+merge:
  - P0-B `fix/live-supervisor-silent-spawn-fail` (f324f0c): LiveCommandBus.\_publish now calls ensure_group before xadd so commands don't vanish when consumer group is positioned at `$`; supervisor **main**.py configures stdlib logging.basicConfig so its logs are visible in docker logs
  - P0-C `fix/ib-gateway-env-var-drift` (6f02767): settings.ib_host/ib_port accept AliasChoices on IB_GATEWAY_HOST + IB_GATEWAY_PORT_PAPER env names
  - P0-A `fix/live-supervisor-default-profile` (08b34a9): /live/start returns 503 fast when no supervisor consumer is registered (vs silent 504 timeout)

## Now

Multi-asset live drill ATTEMPTED 2026-04-15 19:36-19:45 UTC, **failed to produce a single live fill** outside the EUR/USD path that already works. Bugs surfaced (none yet fixed):

1. **AAPL/MSFT/SPY** — deployments reach RUNNING state and subscribe to bars, but no `on_bar` events fire. Set `IB_MARKET_DATA_TYPE=DELAYED` to dodge IB error 162 from earlier; DELAYED on US equities apparently doesn't push bar events to the strategy. Strategy is alive, EMAs never update, no signals. The dev account `DUP733213` may not have any market data subscription for US equities — needs check.
2. **ES futures** — `Unable to resolve contract details for IBContract(secType='FUT', exchange='CME', symbol='ES', currency='USD')`. Wrong contract spec; ES needs `exchange='GLOBEX'` + a `lastTradeDateOrContractMonth` for the front-month resolution. Bootstrap whitelist entry I added is wrong.
3. **Options** — never attempted; needs separate IB options-chain bootstrap path.
4. **Graduation flow** — never invoked the `/api/v1/graduation/...` API. The right user-facing path is backtest → graduate → live deploy, not hand-tuned EMA config straight to /live/start. This was the user's explicit "no cheating" requirement.
5. **Heartbeat-stale** flips on multi-deployment supervisor → `failed`. Symptomatic of the bar-feed gap.

## Next

1. **Tomorrow at market open**: do this properly. Pre-open prep — verify paper account `DUP733213` market-data entitlements (IB account → Settings → Market Data Subscriptions); fix ES futures contract spec (`GLOBEX` + front-month); add SPY-style entry for an index ETF if needed; add options-chain bootstrap for one ticker. After open: run the full graduation pipeline as the user-facing flow (backtest a fast EMA → `POST /api/v1/graduation/...` → graduated strategy goes live across all 5 asset classes → watch fills).
2. Bug B (worker stale-import hygiene) — lightweight ops fix.
3. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex).
4. Follow-ups: EMA backtest zero PnL/win-rate/sharpe (extraction gap); `trades.side` persists as Nautilus enum int (1/2) instead of "BUY"/"SELL"; PositionClosed `realized_pnl` not propagated; `READ_ONLY_API=no` compose default; `/account/health` probe never-started; `/live/positions` empty with open Nautilus position; deployment row status stays `starting`; audit lifecycle race auto-heal.
