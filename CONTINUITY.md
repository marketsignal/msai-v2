# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                   |
| --------- | --------------------------------------- |
| Command   | /new-feature portfolio-per-account-live |
| Phase     | Pre-Flight                              |
| Next step | Verify plugins                          |

### Checklist

- [x] Worktree created (`feat/portfolio-per-account-live`)
- [x] Project state read
- [ ] Plugins verified
- [ ] PRD created
- [ ] Research done
- [ ] Brainstorming complete
- [ ] Approach comparison filled (council already ran — use as input)
- [ ] Contrarian gate passed — **council already ran (standalone mode), verdict below**
- [x] Council verdict: **new immutable live-composition model** (LivePortfolio + LivePortfolioRevision + LivePortfolioRevisionStrategy + LiveDeploymentStrategy + gateway_session_key); per-account IB Gateway Compose services; per-gateway-session spawn guard; full-portfolio cold restart on any member change.
- [ ] Plan written
- [ ] Plan review loop (2 iterations) — iterate until no P0/P1/P2
- [ ] TDD execution complete
- [ ] Code review loop (0 iterations) — iterate until no P0/P1/P2
- [ ] Simplified
- [ ] Verified (tests/lint/types)
- [ ] E2E use cases designed (Phase 3.2b)
- [ ] E2E verified via verify-e2e agent (Phase 5.4)
- [ ] E2E regression passed (Phase 5.4b)
- [ ] E2E use cases graduated to tests/e2e/use-cases/ (Phase 6.2b)
- [ ] Learnings documented (if any)
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

### Feature scope

Evolve `LiveDeployment` from `(strategy_id, account_id)` to `(portfolio_revision_id, account_id)`. Introduce `LivePortfolio` (mutable, rebalanced over time by portfolio-manager), `LivePortfolioRevision` (immutable snapshot, the warm-restart identity boundary), `LivePortfolioRevisionStrategy` (M:N: one graduated strategy can appear in many portfolios), and `LiveDeploymentStrategy` (member strategy-instance rows for attribution/recovery). Enable same portfolio deployed to N different IB accounts in parallel — each deployment = isolated subprocess + per-account IB Gateway session. Static per-account Gateway services in Compose. Concurrent-spawn guard scoped to gateway session, not global. Deterministic IB `client_id` allocation rule. Full-portfolio cold restart on any member change (no per-strategy hot-swap in v1). Only graduated strategies may be added to a portfolio.

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

## Done (cont'd)

- ES futures canonicalization merged 2026-04-16 04:35 UTC (PR #23): fixes the drill's zero-bars failure mode at the MSAI layer. `canonical_instrument_id()` maps `ES.CME` → `ESM6.CME` so the strategy's bar subscription matches the concrete instrument Nautilus registers from `FUT ES 202606`. Spawn-scoped `today` threaded through supervisor + subprocess (via `TradingNodePayload.spawn_today_iso`) closes the midnight-on-roll-day race. Live-verified: subscription succeeds without `instrument not found`. Caught a `.XCME` vs `.CME` venue bug in live testing that unit tests missed. 28 new bootstrap tests (39 total). Codex addressed 4 rounds of findings + a 5th surfaced only by the live deploy. DUP733213's missing real-time CME data subscription confirmed as the remaining upstream blocker (IB error 354) — operator action at broker.ibkr.com, not code.
- 7-bug post-drill sprint complete 2026-04-16 02:31 UTC — every offline-fixable bug from the 2026-04-15 multi-asset drill aftermath shipped to main, no bugs left behind:
  - **Bug #1** PR #17 — backtest metrics now derive from positions when Nautilus stats return NaN (3-tier fallback: stats → account snapshot → positions). Verified: win_rate=0.17, sharpe=-45.7 on AAPL/SPY 2024.
  - **Bug #2** PR #18 — `/account/health` IB probe now starts as a FastAPI lifespan background task (30s interval). Verified: `gateway_connected=true` after first probe tick.
  - **Bug #3** commit 2084423 — `READ_ONLY_API` compose default flipped to `no` so paper-trading orders submit without per-session env override (was triggering IB error 321 in 2026-04-15 drill).
  - **Bug #4** PR #19 — `PositionClosed.realized_pnl` now propagates to `trades.pnl` via new `client_order_id` linkage; subscribed to `events.position.*` in subprocess.
  - **Bug #5** PR #20 — `graduation_candidates.deployment_id` auto-links on `/live/start` so the graduation → live audit chain stays connected.
  - **Bug #6** PR #21 — `trades.side` now persists as `BUY`/`SELL` strings via `OrderSide.name` (was leaking enum int 1/2 into the DB).
  - **Bug #7** PR #22 — `claude-version/scripts/restart-workers.sh` ships ~10s worker container restart for stale-import hygiene; documented in `claude-version/CLAUDE.md`.

## Now

PR #23 merged. Stack restarted on main-branch code. All code-side work for ES is complete. The remaining blocker to actual fills at market open is the IB market-data entitlement gap on `DUP733213` (confirmed via IB error 354) — operator action at `broker.ibkr.com → Market Data Subscription Manager`, no code involved.

## Next

1. **Enable IB real-time market data on `DUP733213`** (operator UI action at broker.ibkr.com):
   - `CME Real-Time (NP, L1)` — for ES/NQ/YM/RTY futures
   - `NASDAQ TotalView` or `NASDAQ Level I` — for AAPL/MSFT
   - `NYSE Real-Time` or the `US Securities Snapshot and Futures Value Bundle` (free) — for SPY
2. **Fix ES futures contract spec** — DONE via PR #23.
3. **Add options-chain bootstrap path** for one ticker (still pending, separate PR).
4. **At market open (2026-04-16 09:30 ET)**: rerun the graduation pipeline with entitlements in place. Verify bars flow on AAPL/MSFT/SPY/ES, the 7 earlier bug fixes hold, and paper orders fill end-to-end.
5. Phase 2 #5 Strategy registry + continuous futures (DB-backed InstrumentDefinition, `.Z.` regex).
6. Remaining follow-ups: `/live/positions` empty with open Nautilus position; deployment row status stays `starting`; audit lifecycle race auto-heal.
