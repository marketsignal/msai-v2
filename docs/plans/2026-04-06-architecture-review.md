# MSAI v2 Architecture Review — 2026-04-06

Two independent parallel reviews of the Claude and Codex implementations against
NautilusTrader's canonical system architecture diagram.

**Reference architecture (Nautilus):**

```
DATA CLIENTS (Databento, OKX, IB)         EXECUTION CLIENTS (IB, OKX, Bybit)
        ↓                                          ↓
DATA ENGINE                                EXEC ENGINE
(subscriptions, requests)                  (order commands)
        ↑                                          ↑
        └────────── MESSAGE BUS ──────────┘
              (pub/sub, req/res, data, commands, events)
                  ↑          ↑          ↑
                  │          │          │
              TRADER     PORTFOLIO   RISK ENGINE
           (strategies)  (positions, (pre/post-trade)
                          margin, pnl)
                            ↓
                          CACHE
                  (instruments, orders,
                   positions, custom)
                            ↓
                        DATABASE
                       (persistence)
```

Every component talks through the Message Bus. Cache ↔ Database for persistence.
Strategies submit orders via Bus → Risk Engine → Exec Engine → Exec Clients →
exchanges. Data flows the opposite direction.

**Target use case:** Personal hedge fund running multiple strategies 24/7 on
stocks/futures/options/indexes/crypto, with Interactive Brokers for execution,
real-time data, big data (TBs of historical minute bars), backtesting that must
match live execution exactly, and live trading in production with real money.

---

## Reviewers

Two independent reviews ran in parallel:

1. **Claude (Explore agent)** — file-by-file architectural analysis
2. **Codex CLI (gpt-5.4)** — independent assessment with web research on Nautilus APIs

Both reached the same conclusions, which is strong signal.

---

## Consensus Verdict

> **Neither version is close to a hedge-fund production platform.**
>
> `claude-version` is the cleaner backtest scaffold with real Nautilus
> `BacktestNode`, but its live trading is a literal stub.
>
> `codex-version` is the only one that attempts a real Nautilus live
> architecture with `TradingNode` + IB adapters, but risk/recovery/data-model
> gaps would make a CIO reject it immediately.

| Overall     | Claude                                         | Codex                                         |
| ----------- | ---------------------------------------------- | --------------------------------------------- |
| Score       | **4/10**                                       | **5/10**                                      |
| Posture     | Toy on the live side, well-engineered backtest | Rough prototype with real exec path, untested |
| Real money? | No (can't even deploy)                         | No (would deploy but blow up first day)       |

---

## Component Scores (averaged across both reviewers)

| Component           |   Claude   |   Codex    | Notes                                                                                                                                                      |
| ------------------- | :--------: | :--------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Data Clients        |    3/10    |   4-5/10   | Both have historical Polygon/Databento ingestion. Codex adds IB live data client config. Neither has real-time tick subscriptions.                         |
| Data Engine         |    4/10    |   4-5/10   | Real inside `BacktestNode` for both. Absent for live in Claude. Codex wires it but no security-master orchestration.                                       |
| Exec Clients        | **0-1/10** |   5-6/10   | Claude has none. Codex wires `InteractiveBrokersLiveExecClientFactory` with credentials but **untested E2E**.                                              |
| Exec Engine         | **1-2/10** |   5-7/10   | Claude's `TradingNodeManager.start()` literally stores `None` as a placeholder. Codex spawns a real `TradingNode` subprocess.                              |
| Trader + Strategies |   5-6/10   |    6/10    | Both have real Nautilus `Strategy` subclasses. Both hardcoded to single instrument. Codex's live manager **discards** the `instruments` argument.          |
| Portfolio           |   2-4/10   |   3-4/10   | Claude returns empty list. Codex queries IB via `ib_async`. Neither has real-time PnL per strategy or per-deployment.                                      |
| **Message Bus**     | **1-2/10** | **1-4/10** | **Neither uses one as a platform spine.** Both rely on subprocess isolation + DB commits. Codex has a Redis WebSocket subscriber but no publisher.         |
| Risk Engine         |   3-5/10   | **2-3/10** | Claude's design is better (halt flag, reset cycle, notional limits) but validates a stub. Codex's is stateless and fed **hardcoded PnL/exposure** numbers. |
| Cache               |    2/10    |   2-4/10   | Neither has an instrument/order/position cache layer. No persistence or restart rehydration.                                                               |
| Database            |   5-7/10   |   5-7/10   | Both use PostgreSQL + SQLAlchemy 2.0. Claude has proper UUID columns, Codex uses String(36). Neither has order-attempt audit trail.                        |

---

## Backtest/Live Parity — The Critical Question

**Does the same strategy code run identically in backtest and live?**

### Claude

**No — live trading is a stub.** `TradingNodeManager.start()` does not spawn
anything; it stores `None` in `self._processes[deployment_id]`. Zero parity by
default because there's nothing on the live side.

### Codex

**No — diverges at the data source layer.**

- Backtest venue: `SIM` (synthetic instruments via `TestInstrumentProvider`)
- Live venue: `IB` (real IB Gateway instruments)
- Backtest data: Parquet catalog (pre-computed bars, instant fills)
- Live data: `InteractiveBrokersLiveDataClientFactory` (real IB ticks, real
  slippage, real latency, real rejections)
- Codex's live manager **discards** the `instruments` argument from the start
  request, so multi-instrument deployments are not supported

A strategy that returned 18% in backtest will not behave the same on day 1 of
live trading. This is an architectural flaw in both versions.

---

## Critical Bugs Both Reviewers Independently Found

### Codex version

1. **FK user-ID bug** — `auth.py:27` creates `User.id` as a generated UUID, but
   `backtests.py:45`, `live.py:59`, and `audit.py:57` all write raw `oid/sub`
   claim strings into FK columns. Either broken persistence or silent audit
   loss.

2. **Risk engine fed hardcoded inputs** — `live_start` in `live.py:33` passes
   hardcoded PnL/exposure numbers to the risk engine instead of real ones. The
   risk gate is theatre.

3. **Live manager discards `instruments`** — `trading_node.py:64` ignores the
   `instruments` argument from the start request. Not a serious multi-instrument
   model.

4. **`kill_all()` is not sticky** — `risk_engine.py:56` does not persist halt
   state, so a new deployment can start immediately after a kill.

5. **Backtest committed before enqueue** — `api/backtests.py:57` commits the row
   before pushing to the Redis queue. Redis failure strands `pending` jobs.
   (Claude fixed this in an earlier review pass.)

### Both versions

6. **Catalog builders load entire Parquet partitions into pandas memory** —
   `catalog_builder.py:123` (Claude), `catalog_builder.py:69` (Codex). Will OOM
   on terabyte data sets.

7. **Multi-asset support is fictional** — Both use
   `TestInstrumentProvider.equity(venue="SIM")` for every symbol. Futures,
   options, crypto, and indexes are not modeled with real contracts.

8. **No real Message Bus** — Both rely on subprocess isolation and DB commits
   instead of a true event spine. The API process cannot stop, monitor, or
   communicate with a running TradingNode subprocess.

9. **Live nodes are daemon child processes of the API** — If the FastAPI process
   crashes, the trading subprocess dies with it. Open positions become orphaned.
   No restart reconciliation with IB.

10. **Test for the "real Nautilus" backtest doesn't actually run the engine** —
    Claude's `test_nautilus_backtest_runner.py:3` only builds the config, never
    invokes `BacktestNode`.

---

## Head-to-Head

| Dimension                              | Verdict                                                       |
| -------------------------------------- | ------------------------------------------------------------- |
| Further along **architecturally**      | **Codex** — only one with a real `TradingNode` + IB adapters  |
| Better **engineered around the edges** | **Claude** — cleaner backtest worker, API, storage, and tests |
| Backtest pipeline                      | **Tie** — both run real `BacktestNode` end-to-end             |
| Live trading                           | **Codex** — wired but untested vs. Claude's stub              |
| IB integration                         | **Codex** — real `ib_async` vs. Claude's mocked methods       |
| Risk engine design                     | **Claude** — better state model                               |
| Test coverage                          | **Claude** — 139 vs. 24 unit tests                            |
| Documentation                          | **Claude** — much higher docstring density                    |
| Production readiness                   | **Codex** (5/10 vs 3/10)                                      |
| Real-money verdict                     | **Neither**                                                   |

---

## Critical Gaps — Must Fix Before Real Money

### Tier 1 — Blockers

1. **Real Live Trading Path in Claude**
   - Replace `TradingNodeManager` stub with a real Nautilus `TradingNode`
     subprocess (port the working pattern from Codex)
   - Wire `InteractiveBrokersLiveDataClientFactory` and
     `InteractiveBrokersLiveExecClientFactory`

2. **Real Security Master / Instrument Layer**
   - Replace `TestInstrumentProvider.equity(venue="SIM")` with real IB contract
     details (`ib_async.IB.qualifyContractsAsync`) for stocks, futures, options,
     indexes, and crypto
   - Cache instruments in PostgreSQL keyed by canonical Nautilus ID
   - Backtest and live must use **identical** instrument definitions (same
     venue, same multiplier, same tick size)

3. **Backtest/Live Parity**
   - Same strategy code, same instrument IDs, same bar types in both modes
   - Same data source contract (Nautilus `Bar` objects from the message bus,
     regardless of source)
   - Validation harness that runs a strategy against the same window in both
     backtest and paper-live and asserts the trade log matches

4. **Real-Time Position & PnL Tracking**
   - Cache position snapshots in Redis, refreshed on every fill event
   - Stream updates to the API via Redis pub/sub
   - Per-strategy and firm-wide PnL attribution
   - Expose via WebSocket to the frontend

5. **Pre- and Post-Trade Risk Engine Integration**
   - Position limits, max drawdown, daily loss, max notional, kill switch
   - Hooked into the actual order path (not just `live_start`)
   - Real PnL/exposure inputs, not hardcoded
   - Sticky halt state persisted to Redis or PostgreSQL
   - Margin breach detection from IB

6. **Crash Recovery and Reconciliation**
   - On API restart: rebuild Nautilus cache from IB open orders, executions,
     and positions
   - Run live nodes as independent processes (not daemon children of API)
   - Detect orphaned positions and alert/auto-close

7. **Real Message Bus**
   - Redis pub/sub for: position updates, fills, order state, kill signals
   - Strategies publish events; API and risk engine subscribe
   - Replaces ad-hoc DB commits as the integration channel

### Tier 2 — Production Hardening

8. **Audit Trail of Order Attempts** — record every order submission attempt,
   not just executed fills. Include rejections, partial fills, modifications.

9. **Streaming Catalog Builder** — convert raw Parquet → Nautilus catalog
   incrementally instead of loading full partitions into pandas memory.

10. **Multi-Strategy Concurrency** — proper isolation, per-strategy resource
    limits, fair scheduling.

11. **Order Lifecycle Visibility** — pending, working, filled, partially
    filled, cancelled, rejected — all visible in the UI in real time.

12. **Observability** — structured logging at every event boundary, Prometheus
    metrics for order latency, queue depth, position update lag.

13. **Reconnection Logic** — IB Gateway disconnect handling: auto-reconnect,
    halt strategies if disconnected too long, replay missed events.

14. **Market Hours Awareness** — automatic pause outside RTH for equity
    strategies.

15. **Rate Limiting** — throttle order submissions to stay under IB's API
    limits.

16. **Commission and Slippage Modeling in Backtest** — use real IB commission
    schedules so backtests are not falsely optimistic.

17. **Fix Codex FK Bug** — migrate `oid/sub` claim writes to use the real
    `User.id` UUID.

---

## Recommended Path Forward

**Converge Claude on Codex's live trading skeleton + keep Claude's backtest
rigor.** Specifically:

1. Port Codex's `TradingNodeManager` + IB clients into Claude's structure
   (Claude's transactional patterns + tests + docstrings make the result better
   than either parent)
2. Build a **real security master** in Claude using `ib_async` contract
   qualification, persisted to PostgreSQL
3. Wire **Redis pub/sub** as the platform message bus
4. Implement **position snapshot caching** + WebSocket streaming
5. Integrate the **risk engine** into the actual order path with real inputs
6. Add **crash recovery** that reconciles with IB on startup
7. Write a **30-day paper trading harness** with monitoring and alerting
8. Only then: enable a small real-money allocation behind a hard cap

---

## Files Cited

### Claude version

- `services/nautilus/trading_node.py:8,76` — live trading stub
- `services/nautilus/instruments.py:54` — synthetic SIM equities
- `services/nautilus/backtest_runner.py:229` — real `BacktestNode`
- `services/nautilus/catalog_builder.py:123` — full-partition pandas load
- `workers/backtest_job.py:292` — instrument_id/bar_type injection
- `core/audit.py:48` — audit logging is TODO, no DB insert
- `services/ib_account.py:35` — IB account is mocked
- `api/live.py:228,246` — positions and trades return empty
- `api/websocket.py:59` — WebSocket is heartbeat-only
- `tests/unit/test_nautilus_backtest_runner.py:3` — config-only test, no engine

### Codex version

- `services/nautilus/trading_node.py:64,150` — discards instruments, real `TradingNode`
- `services/nautilus/trading_node.py:81` — daemon child process of API
- `services/nautilus/instruments.py:24` — synthetic SIM equities
- `services/nautilus/catalog_builder.py:69` — full-partition pandas load
- `services/risk_engine.py:12,56` — stateless, no sticky halt
- `services/ib_account.py:22-77` — real `ib_async` integration
- `api/backtests.py:45,57` — FK user-ID bug, commit-before-enqueue
- `api/live.py:33,59,115` — hardcoded risk inputs, FK bug, reads is_live=true
- `api/auth.py:27` — User.id generated UUID
- `core/audit.py:57` — FK user-ID bug
- `strategies/example/config.py:10` — explicit instrument_id required
- `workers/backtest_job.py:40` — config forwarded unchanged

---

## What the Reviewers Said in One Sentence Each

**Claude reviewer:**

> "Claude is a well-engineered backtest platform with honest phase-gating;
> Codex is a dual-mode prototype that actually tries to execute trades but is
> untested at scale. Both would lose money on day 1 due to lurking bugs."

**Codex reviewer:**

> "claude-version is the better-written backtest scaffold; codex-version is the
> only one with a plausible Nautilus live path; both are still far from
> something a hedge fund should trust with capital."
