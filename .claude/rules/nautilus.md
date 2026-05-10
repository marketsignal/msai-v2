# NautilusTrader — Critical Gotchas (read every session)

NautilusTrader is the central engine for backtest **and** live trading.
Both must use the same Strategy code, same Instruments, same data contract.

**Full reference:** `docs/nautilus-reference.md` (10 sections, 60KB).
This file is the short list — the things that have already cost time or
that **will** lose money if ignored.

---

## Top 20 Gotchas

### 1. uvloop policy is installed at import time
`nautilus_trader/system/kernel.py:97-98` calls `asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())` on import. This breaks `arq` on Python 3.12+ because arq's `Worker.__init__` calls the deprecated `asyncio.get_event_loop()`. **Fix:** call `asyncio.set_event_loop_policy(None)` *after* the imports that pull in `nautilus_trader`, in `workers/settings.py`. Already done in both versions — **do not move it back above the imports**.

### 2. `generate_account_report()` requires `venue=` or `account_id=`
Calling it bare raises `ValueError: At least one of 'venue' or 'account_id' must be provided`. Always pass `venue=Venue("SIM")` (backtest) or `venue=Venue("IBKR")` (live). The other two reports (`generate_orders_report`, `generate_positions_report`) **don't** need this.

### 3. Two TradingNodes with the same `ibg_client_id` silently disconnect each other
IB Gateway will silently drop the older connection when a new one arrives with the same `client_id`. Each `TradingNode` (data + exec) needs **unique** `ibg_client_id` values. Use `ib_client_id`, `ib_data_client_id`, `ib_exec_client_id` separately in config.

### 4. Backtest venue name must exactly match the instrument venue suffix
`BacktestVenueConfig(name="SIM")` only works with instruments like `AAPL.SIM`. If you use `TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS")` you get `AAPL.XNAS` and Nautilus raises `Venue 'XNAS' for AAPL.XNAS does not have a BacktestVenueConfig`. **Fix:** pin a single venue name (`SIM` for backtest, `IBKR` for live) and resolve all instruments through it.

### 5. Connection timeout on IB Gateway startup leaves TradingNode dormant
`await node.start_async()` returns successfully even if the IB connection failed. **Fix:** explicitly verify `node.is_running == True` *and* probe IB after startup. Don't trust the absence of an exception.

### 6. IB Gateway port 4002 (paper) with a live `account_id` fails silently
No data will flow. **Fix:** validate `(port, account_id_prefix)` consistency at startup. Paper accounts start with `DU`; live accounts don't.

### 7. Buffered cache database loses events on crash
`CacheConfig(database=DatabaseConfig(..., buffer_interval_ms=100))` batches writes. On crash, the last 100ms of state is lost. **Fix:** for production use `buffer_interval_ms=0` (write-through) or wait for flush in shutdown handlers.

### 8. Backtest `MessageBusConfig.database` will pollute production Redis
If you forget to disable the message-bus DB backend in backtests, every backtest dumps events into the same Redis instance the live node is using. **Fix:** different `MessageBusConfig` per environment, or no DB backend in backtest.

### 9. Instrument not pre-loaded fails at runtime, not startup
If a strategy subscribes to an instrument that wasn't in `instrument_provider.load_ids` or `load_contracts`, the failure happens on the first bar event — long after startup. **Fix:** validate every strategy's required instruments are in the provider config before `node.run()`.

### 10. Reconciliation timeout on startup makes the node "look" alive
`LiveExecEngineConfig(reconciliation=True)` runs an async reconcile against IB. If it times out, the node still starts but has stale state. **Fix:** explicit reconciliation completion check before allowing the trader to submit orders.

### 11. Dynamic instrument loading is synchronous and slow
`instrument_provider.load_async([id])` at runtime blocks for one IB round-trip per instrument. **Fix:** pre-load everything at startup. Never load instruments on the critical path.

### 12. Options chain loading explodes
`build_options_chain=True` with `min_expiry_days=0, max_expiry_days=365` for a liquid name (SPY, AAPL) loads **thousands** of strikes. Always set tight expiry windows and a strike filter (delta range or absolute price band).

### 13. `TradingNode.stop()` doesn't close positions
Stop = stop receiving data and stop accepting new orders. Open orders stay open, open positions stay open. **Fix:** explicit `cancel_all_orders()` + `close_all_positions()` in your `on_stop()` and in any kill-switch path.

### 14. Backtest fills are optimistic vs live
Backtest defaults assume immediate fills at the bar's price. Real markets give partial fills, slippage, rejections. **Fix:** use a `FillModel` in backtest with realistic slippage and probability of fill, especially for limit orders.

### 15. Cache eviction silently drops oldest ticks
`CacheConfig(tick_capacity=10_000)` is the default. When you exceed it, oldest ticks are dropped silently. Don't rely on the cache for historical lookbacks — use the Parquet catalog.

### 16. `on_save()` / `on_load()` return value isn't validated
Strategy state persistence is JSON-serialized. Custom Python types fail silently. **Fix:** test the round-trip explicitly in unit tests.

### 17. `MessageBus` JSON encoding fails on custom types
If you publish custom events, `Decimal`, `datetime`, `pathlib.Path`, etc. will not survive `json.dumps`. Use `msgpack` encoding or convert to primitives before publishing.

### 18. `asyncio.run(node.run())` causes event loop policy conflicts
**Wrong:** `asyncio.run(node.run())`. **Right:** `node.run()` (it manages its own loop) **or** `await node.run_async()` from inside an existing loop.

### 19. Reconciliation can discover fills that weren't in your DB
On restart, `LiveExecEngineConfig(reconciliation=True)` queries IB for orders/fills since last shutdown. New fills that landed after your last save will appear as "unexpected positions". **Fix:** subscribe to `AccountState` events and persist on every change, not just at shutdown.

### 20. `dispose()` not called → Rust logger + sockets leak
Always wrap `node.run()` in a try/finally and call `node.dispose()` in the finally. Otherwise the Rust-side logger and IB sockets leak across runs.

---

## Architectural Rules

1. **One Strategy class, one config schema, run unchanged in backtest and live.** Any divergence is a bug — file an issue and fix.
2. **Pre-load every instrument at startup.** No dynamic loading on the trading critical path.
3. **Pin venue names per environment** (`SIM` for backtest, `IBKR` for live). Use a single helper to construct `InstrumentId` strings so they can never drift.
4. **Always run TradingNode in a dedicated process** (subprocess of arq worker, NOT a daemon child of FastAPI). API crash must not kill live trading.
5. **Use `MessageBusConfig.database = redis` only in live environments.** Backtests must NOT publish to the shared Redis.
6. **Wrap `node.run()` in try/finally with `dispose()`** every single time.
7. **Use `cache.database = redis` (or postgres) in production** so cache rehydration on restart actually works. Pair with `LiveExecEngineConfig(reconciliation=True)` and verify it completed.
8. **Backtest and live must read from compatible catalogs.** Convert raw OHLCV → Nautilus catalog via `BarDataWrangler` once, dual-mode strategies read from the same catalog format.
9. **Custom risk checks belong in a custom RiskEngine subclass or as a `Strategy.on_start()` precheck.** Built-in `RiskEngine` only does precision/notional/rate-limit checks — it does not enforce per-strategy max position or daily loss.
10. **Every order submission must be auditable.** Persist the order attempt (with strategy_code_hash + git_sha) before sending to the broker. Persist the OrderFilled event when it returns.

---

## When in doubt

Open `docs/nautilus-reference.md` and search the section. Every claim there is cited with `nautilus_trader/...:line`.
