# Nautilus Natives Audit

**Date:** 2026-04-06
**Purpose:** For each Tier-1 production-hardening blocker, identify what NautilusTrader already provides natively vs what we actually need to build.
**Source:** Direct read of installed nautilus_trader source under
`/Users/pablomarin/Code/msai-v2/.worktrees/claude-nautilus-production-hardening/claude-version/backend/.venv/lib/python3.12/site-packages/nautilus_trader/`

The principle: **Nautilus is already 60-70% of a production hedge fund platform.** We must not reinvent the wheel. We only build the thin glue that Nautilus genuinely doesn't provide.

---

## A. Live Trading via TradingNode + IB

### Built-in

- `TradingNode` lifecycle (`live/node.py:39+`): `__init__` → `build()` → `run()` / `start_async()` → `stop()` → `dispose()`
- IB live data + exec client factories (`adapters/interactive_brokers/factories.py`): `InteractiveBrokersLiveDataClientFactory`, `InteractiveBrokersLiveExecClientFactory`
- Order submission flow: Strategy `submit_order()` → built-in RiskEngine → ExecEngine → ExecClient → IB Gateway → events back to Strategy callbacks
- Reconnection: `InteractiveBrokersClient._handle_reconnect()` (`client/connection.py:127`)
- Strategy event hooks: `on_order_submitted`, `on_order_accepted`, `on_order_rejected`, `on_order_filled`, `on_order_cancelled`, `on_position_opened`, `on_position_changed`, `on_position_closed`, `on_account_state` — all wired automatically

### We must build

- **Subprocess launcher** because TradingNode is single-process and we need to run it in a separate OS process from FastAPI so that killing the API doesn't kill trading. Use `multiprocessing.get_context("spawn")` (NOT `fork`).
- **Process registry** (`live_node_processes` table) — Nautilus has zero subprocess registry. We need this so the API can rediscover surviving subprocesses after a restart.
- **Heartbeat task** inside the subprocess — Nautilus has no native health ping that an external process can read. Background task writes `last_heartbeat_at` to the registry every 5s.
- **Stop sequence with position flatten** — `node.stop()` does NOT cancel orders or close positions. The strategy itself must `cancel_all_orders()` + `close_all_positions()` in its `on_stop` (these are `Strategy` methods, not `TradingNode` methods).

### We MUST NOT build

- A custom order submission layer
- A custom event distribution layer for in-process events (Nautilus's MessageBus already handles it)

---

## B. Real Security Master / Instrument Loading

### Built-in

- `InteractiveBrokersInstrumentProvider` (`adapters/interactive_brokers/providers.py:50+`) loads instruments from IB and parses them to Nautilus `Instrument` objects via `parse_instrument()` (`parsing/instruments.py:301+`)
- `InteractiveBrokersInstrumentProviderConfig` (`adapters/interactive_brokers/config.py:88+`) supports `load_ids`, `load_contracts`, `build_options_chain`, `build_futures_chain`, `min_expiry_days`, `max_expiry_days`, `cache_validity_days`, `symbology_method` (`IB_SIMPLIFIED` or `IB_RAW`)
- Instrument cache via `CacheConfig.database = redis` — instruments persist to Redis automatically
- Trading hours metadata is included in the parsed `Instrument` object

### We must build

- A thin **`SecurityMaster` service** as an ergonomic layer above the Nautilus provider:
  - `resolve(symbol_or_spec) -> Instrument` (cache lookup → IB qualification → parse → cache write)
  - `bulk_resolve(specs)` for batched preload at startup
  - `shorthand_to_spec("AAPL") -> InstrumentSpec` for ergonomic single-symbol API access
- `InstrumentSpec` dataclass — a serializable handle to pass through the API and persist in Postgres
- A Postgres `instrument_cache` table — backstops the Nautilus Redis cache so we have an authoritative store across full Redis flushes (and so the API can read instrument metadata without spinning up Nautilus)
- **Trading hours field** in `instrument_cache` so the strategy-side market-hours guard can read it without going through Nautilus

### We MUST NOT build

- Our own contract qualification — use `InteractiveBrokersInstrumentProvider`
- Our own parse_instrument — use Nautilus's
- Our own instrument cache layer — let `CacheConfig.database = redis` do it; our Postgres table is just a backstop, not a duplicate
- Symbology translation logic — set `symbology_method = IB_SIMPLIFIED`

---

## C. Backtest/Live Parity

### Built-in

- Same `Strategy` class runs in `BacktestNode` and `TradingNode` — same kernel, same engines, same message bus contract
- `ImportableStrategyConfig` lets backtest and live load the same strategy file with the same config
- `Bar` and `Tick` objects are identical across data sources
- Strategy event hooks (`on_bar`, `on_quote_tick`, etc.) are identical

### We must build

- **Canonical instrument unification** — currently backtest pins to `*.SIM`, live would use `*.NASDAQ`. We must rebuild backtest catalogs with the same canonical IDs the live IB adapter uses
- **Parity validation harness** — run the same strategy on the same window in both modes, compare normalized intent (same instrument, same side, same signed quantity, same decision sequence) within tolerance for fills/VWAP. Nautilus has no built-in parity tester
- A **historical-paper replay mode** for the live leg of the parity test (paper-port TradingNode against historical bars), since live streaming for parity is impractical

### We MUST NOT build

- A separate backtest risk engine (use the same `RiskEngineConfig` in both modes)
- A pseudo-live mode that fakes IB inside backtest (use real paper trading)
- Runtime venue remapping (load correct instruments upfront)

---

## D. Real-Time Position & PnL

### Built-in

- `Cache.position(id)` and `Cache.positions_open(...)` (`cache/base.pxd:200+`) return `Position` objects with: `quantity`, `avg_open_price`, `unrealized_pnl`, `realized_pnl`
- `Position` events: `PositionOpened`, `PositionChanged`, `PositionClosed` published on the message bus automatically
- `Portfolio.account(account_id)` returns `Account` with `balance`, `margin_used`, `margin_available` updated in real time from `AccountState` events
- `CacheConfig.database = redis` persists positions and accounts to Redis so external processes can read them

### We must build

- **FastAPI WebSocket subscriber** that consumes Nautilus's Redis stream (see section G) and rebroadcasts to browser clients
- **Mark-to-market PnL update loop** because `position.unrealized_pnl` uses the last trade price, not real-time bid/ask. We subscribe to QuoteTicks, compute mid, and re-publish updated PnL
- **Per-strategy aggregation** — Nautilus tracks per-position; we sum across positions where `position.strategy_id == target` for "strategy PnL" displays
- **Firm-wide PnL aggregator** — sum across strategies

### We MUST NOT build

- A separate `PositionSnapshotCache` — Nautilus's Cache with Redis backend already does this
- A custom Position state machine — use the events
- Our own margin tracking — read `account.margin_used`

---

## E. Risk Engine in Order Path

### Built-in

- `LiveRiskEngine` (`live/risk_engine.py:34+`) instantiated by the kernel from `LiveRiskEngineConfig` — **cannot be subclassed via config** because the kernel hardcodes the class (`system/kernel.py:400`)
- Built-in pre-trade checks: order ID precision, quantity precision, price precision, `max_notional_per_order` (per instrument dict), `max_order_submit_rate`, `max_order_modify_rate`, reduce-only enforcement
- `OrderDenied` event sent to the strategy on rejection — strategy receives `on_order_denied(event)`
- `bypass: bool` flag for emergency mode

### We must build

- **Strategy-side pre-submit guard** (mixin or method on a base class) for our custom checks: per-strategy max position, daily loss, kill switch, market hours. Strategies call this BEFORE `self.submit_order(...)`
- **Sticky kill switch in Redis** (key like `msai:risk:halt`) that the strategy guard reads on every `on_bar`. POST `/api/v1/live/kill-all` sets it; POST `/api/v1/live/resume` clears it
- **Built-in `LiveRiskEngineConfig` configuration** — populate `max_notional_per_order` from our deployment table at startup, set `max_order_submit_rate` per environment

### We MUST NOT build

- A custom `LiveRiskEngine` subclass — kernel won't use it
- Our own pre-trade precision/notional checks — Nautilus already does them

---

## F. Crash Recovery / Reconciliation

### Built-in

- **Automatic reconciliation** when `LiveExecEngineConfig.reconciliation = True` (default in `live/config.py:195`):
  - Queries broker for open orders → reconciles with cache
  - Queries broker for open positions → reconciles
  - Generates synthetic events for any discrepancies
  - Timeout: `timeout_reconciliation = 30s` (kernel config)
- **Cache rehydration** when `CacheConfig.database = redis`: instruments, orders, positions auto-load on `node.build()`
- **Strategy state persistence** via `NautilusKernelConfig.load_state` and `save_state` (BOTH default to **False** — must be enabled explicitly)
- Strategy hooks: `on_save() -> dict[str, bytes]` and `on_load(state)` — kernel calls these automatically when configured

### We must build

- **Reconciliation completion signal** — Nautilus reconciles but emits no specific "done" event. Our subprocess writes `LiveNodeProcess.status = "ready"` to the registry table after `kernel.start_async()` returns successfully. The parent waits for that status
- **Orphaned position detection on API restart** — query `live_node_processes` for `status="running"` rows whose pid is dead, alert + flag for manual review
- **Strategy `on_save`/`on_load` implementations** — Nautilus calls them but the strategies have to actually serialize/deserialize their indicator state. Validate the round-trip in unit tests (gotcha #16)
- **Restart continuity test** — verify after restart the strategy doesn't generate a duplicate decision on the first bar

### We MUST NOT build

- Our own reconciliation logic
- Our own broker query layer to detect missing orders (Nautilus does it)
- Our own cache persistence layer (set `CacheConfig.database`)
- A "rehydration manager" — `node.build()` already does this

---

## G. Real-Time Message Bus / Event Distribution

### Built-in

- `MessageBusConfig.database = redis` (`common/config.py:360+`) publishes ALL events to Redis Streams automatically
- `stream_per_topic = True` (default): one Redis stream per event topic
- `use_trader_prefix = True`, `use_trader_id = True`, `streams_prefix = "stream"` define the stream naming
- Default stream names look like `trader-T001-stream-events.order.{strategy_id}`, `trader-T001-stream-events.position.{strategy_id}`, `trader-T001-stream-events.account.{account_id}`
- Per-topic `events.order.{strategy_id}`, `events.position.{strategy_id}`, `events.account.{account_id}` are the actual published topics (per Nautilus source: `execution/algorithm.pyx:334`, `portfolio/portfolio.pyx:487`, `risk/engine.pyx:190-191`)
- `encoding = "msgpack"` (default, binary) or `"json"` — `msgpack` recommended (gotcha #17: JSON fails on `Decimal`/`datetime`/`Path`)
- `buffer_interval_ms: PositiveInt | None` — `None` = write-through. **`0` is INVALID** (Codex flagged this)
- `MsgSpecSerializer` (`serialization/serializer.py`) for deserializing events on the consumer side

### We must build

- **FastAPI Redis Streams consumer** with **consumer groups** for durable replay (so FastAPI downtime doesn't lose events)
- **Translator** that maps Nautilus event objects to our internal stable event schema (so the frontend depends on our schema, not Nautilus's, which can drift between versions)
- **WebSocket broadcaster** that pushes translated events to browser clients
- **Stream offset persistence** so the consumer resumes from the right position after restart

### We MUST NOT build

- A custom serialization format — use `MsgSpecSerializer`
- A custom message bus — Nautilus's is production-grade
- Our own Redis stream design — Nautilus chooses the stream names
- A heartbeat publisher — Nautilus has `heartbeat_interval_secs` in `MessageBusConfig` if we want it

---

## What gets DELETED from the original plan

| Original task                                                  | Reason                                                                                           | Replacement                                                                                                     |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Custom `RiskEngine` subclass (3.8, 3.9)                        | Kernel instantiates `LiveRiskEngine` directly; subclass won't be used                            | Strategy-side `RiskAwareStrategy` mixin + Redis halt flag                                                       |
| `PositionSnapshotCache` (3.6)                                  | Nautilus Cache with Redis backend already does this                                              | FastAPI Redis Streams consumer reads positions from the events directly                                         |
| Custom reconciliation gating logic (4.1)                       | Kernel reconciles automatically; reading private `_reconciliation_completed` log line is fragile | Subprocess writes `status="ready"` to `live_node_processes` table after `kernel.start_async()` returns          |
| Custom strategy state persistence infrastructure (most of 4.5) | Nautilus calls `on_save`/`on_load` automatically when `load_state=True` and `save_state=True`    | Just enable the kernel config flags + implement `on_save`/`on_load` on the strategy + validated round-trip test |
| Custom rehydration smoke test (3.3)                            | Kernel auto-rehydrates from `CacheConfig.database`                                               | Real restart test in Phase 4 verifies positions survive                                                         |
| TTL on `PositionSnapshotCache` (3.6)                           | Nautilus cache has no TTL; using one would silently evict open positions                         | Don't TTL, ever — let Nautilus manage lifecycle                                                                 |
| Custom crash recovery cache rebuild (4.4)                      | Cache rehydration is automatic                                                                   | Just orphaned-process detection (still needed)                                                                  |

## What gets KEPT but SIMPLIFIED

| Original task                  | Original size | Simplified size | Why                                                                                                                   |
| ------------------------------ | ------------- | --------------- | --------------------------------------------------------------------------------------------------------------------- |
| 1.10 Stop endpoint             | M             | S               | Just call `Strategy.cancel_all_orders + close_all_positions` in `on_stop`, then `node.stop_async()`, then `dispose()` |
| 4.4 Crash recovery             | L             | M               | Most is automatic; only orphaned-process detection + alert is custom                                                  |
| 4.5 Strategy state persistence | M             | S               | Just config flags + on_save/on_load + round-trip test                                                                 |
| 3.8 Custom risk engine         | L             | M               | Strategy mixin instead of kernel subclass; same effort but different shape                                            |

## What gets ADDED based on Codex findings + audit

| New task                                                         | Phase   | Why                                                                                                             |
| ---------------------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------- |
| Live-node supervisor inside arq worker                           | Phase 1 | Resolves the Codex P0 — trading subprocess is spawned by the arq worker, not by FastAPI                         |
| Live-node start/stop **command plane** via Redis                 | Phase 1 | API publishes "start deployment X" / "stop deployment Y" commands to Redis; the worker supervisor consumes them |
| Deterministic **smoke strategy** for E2E                         | Phase 1 | Submits one tiny market order on first bar so the E2E proves the audit + order path                             |
| `client_order_id` correlation key in audit table                 | Phase 1 | Codex finding #7 — needed to update the same audit row through the order lifecycle                              |
| Strategy code hash from file bytes (not git)                     | Phase 1 | Codex finding #7 — git is fragile in container                                                                  |
| Trading hours field in `instrument_cache`                        | Phase 2 | Codex finding #9 — Phase 4 market-hours awareness depends on this                                               |
| Consumer groups + offset persistence                             | Phase 3 | Codex finding #5 — durable replay                                                                               |
| **Translator layer** Nautilus events → internal schema           | Phase 3 | Codex finding (decoupling) — frontend depends on our schema, not Nautilus's                                     |
| `GET /api/v1/live/status/{deployment_id}` route                  | Phase 1 | Codex finding #13 — referenced but never added                                                                  |
| Phase 1 task ordering: 1.7/1.8/1.10 are sequential, not parallel | Phase 1 | Codex finding #13 — they hot-edit the same files                                                                |
| `load_state=True` and `save_state=True` in live node config      | Phase 4 | Codex finding #10 — defaults are False                                                                          |
| Restart-continuity test                                          | Phase 4 | Codex finding #10 — round-trip alone isn't enough                                                               |

---

## The bottom line

> **Nautilus is already 60-70% of what we need.**
> Our job is the 30-40% of glue: subprocess management, security master ergonomics, FastAPI projection, strategy-side custom risk, observability.
> We do NOT subclass Nautilus internals. We do NOT duplicate Nautilus persistence. We do NOT build a parallel event bus.

This audit governs the revised plan. Every task in the revised plan must be justified against the "what Nautilus does for free" tables above.
