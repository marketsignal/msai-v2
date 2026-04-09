# Decision Log

Every architectural choice with rationale and code reference. Decisions
are numbered for cross-referencing from code comments (e.g. "decision
#7" in `api/live.py`).

## Decision #1: NautilusTrader as the Core Engine

**Choice:** Use NautilusTrader for both backtest and live execution.

**Rationale:** One strategy class runs unchanged in backtest and live.
NautilusTrader provides IB adapter, order lifecycle, position tracking,
cache persistence, message bus, reconciliation, and risk engine as
native primitives. MSAI's job is to wrap Nautilus (UI, dashboards,
audit trail, risk overlays) -- not reimplement it.

**Code:** Every `TradingNodeConfig` and `BacktestNode` config goes
through Nautilus-native config classes imported from
`nautilus_trader.live.config` and `nautilus_trader.backtest.engine`.

## Decision #2: arq Over multiprocessing for Job Queue

**Choice:** Use arq (Redis-backed async job queue) for backtests and
data ingestion, NOT `multiprocessing.Process`.

**Rationale:** Codex review caught that `multiprocessing.Process` does
not handle retry, timeout, dead-letter, or distributed execution. arq
provides all four plus a clean async interface.

**Code:** `workers/settings.py:WorkerSettings` registers `run_backtest`
and `run_ingest` as arq functions. `max_tries=2` (one retry).

## Decision #3: Separate Supervisor Container for Live Trading

**Choice:** The live supervisor runs as its own Docker container, NOT
as a background task inside the FastAPI process.

**Rationale:** An API crash must not kill live trading subprocesses. arq's
`on_startup` blocks the poll loop (Codex v2 P0), making it unsuitable
for long-running process management. The supervisor needs its own event
loop for the command consumer, reap loop, heartbeat monitor, and
startup watchdog.

**Code:** `docker-compose.dev.yml` defines `live-supervisor` as a
separate service with its own container. Entry point:
`python -m msai.live_supervisor`.

## Decision #4: Redis Streams for Command Bus

**Choice:** Use Redis Streams (not Pub/Sub, not a database table) for
the API-to-supervisor control plane.

**Rationale:** Redis Streams provide consumer groups with at-least-once
delivery, PEL (Pending Entries List) for crash recovery, and XAUTOCLAIM
for automatic redelivery. Pub/Sub would lose commands if the supervisor
is down. A database table would require polling.

**Code:** `services/live_command_bus.py` -- stream
`msai:live:commands`, group `live-supervisor`, DLQ at
`msai:live:commands:dlq`.

## Decision #5: PyJWT for Backend Auth (Not MSAL)

**Choice:** Backend validates Azure Entra ID JWTs using PyJWT against
the OIDC JWKS endpoint. MSAL is frontend-only.

**Rationale:** MSAL is a token acquisition library, not a validation
library. The backend only needs to verify incoming JWTs -- it never
acquires tokens. PyJWT with explicit algorithm whitelisting
(`algorithms=["RS256"]`) is the correct tool.

**Code:** `core/auth.py:get_current_user` -- FastAPI dependency that
validates JWT or falls back to `MSAI_API_KEY` for development.

## Decision #6: Parquet + DuckDB for Market Data

**Choice:** Store OHLCV data as Parquet files, query via DuckDB for
dashboard endpoints.

**Rationale:** Parquet is columnar, compressed, and NautilusTrader reads
it natively via DataFusion. DuckDB provides SQL-on-Parquet without
running a separate OLAP server. PostgreSQL is reserved for app state
(strategies, backtests, deployments, users).

**Code:** `services/parquet_store.py` for writes,
`services/market_data_query.py` for DuckDB reads. Path structure:
`{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`.

## Decision #7: Stable Deployment Identity via Signature

**Choice:** Each deployment has an `identity_signature` computed from
`(user_id, strategy_id, strategy_code_hash, config_hash, account_id,
paper_trading, instruments)`. The same signature reuses the existing
DB row (warm restart); a different signature creates a new row (cold
start).

**Rationale:** Editing a strategy file, changing config, or switching
accounts must produce a new deployment with isolated state. Restarting
the same strategy with the same config should reconnect to existing
state (warm restart).

**Code:** `services/live/deployment_identity.py:derive_deployment_identity`.
The `deployment_slug` (16-char hex) drives every derived identifier:
`trader_id`, `strategy_id_full`, `message_bus_stream`, both
`ibg_client_id` values.

## Decision #8: One Redis Stream Per Trader (stream_per_topic=False)

**Choice:** Nautilus MessageBus writes all event types to a single Redis
stream per trader: `stream/MSAI-{deployment_slug}`.

**Rationale:** Redis does not support wildcard XREADGROUP across
multiple streams. With `stream_per_topic=True`, each Nautilus topic
would produce a separate stream, and the projection consumer would
need to discover and subscribe to all of them. A single stream per
trader is trivially discoverable from the deployment slug.

**Code:** `live_node_config.py` sets `stream_per_topic=False`,
`use_trader_prefix=True`, `use_trader_id=True`,
`streams_prefix="stream"`.

## Decision #9: Write-Through Cache (No Buffering)

**Choice:** Both `CacheConfig.buffer_interval_ms` and
`MessageBusConfig.buffer_interval_ms` are set to `None` (write-through).

**Rationale:** Gotcha #7 -- buffered writes lose up to
`buffer_interval_ms` of state on a crash. For live trading with real
money, every state change must be durable the moment it happens.

**Code:** `live_node_config.py` -- `buffer_interval_ms=None` on both
configs. Note: `0` is rejected by Nautilus with a positive-int
validation error; `None` is the correct way to disable buffering.

## Decision #10: manage_stop=True for Flatten-on-Stop

**Choice:** Every strategy's `ImportableStrategyConfig` includes
`manage_stop=True`.

**Rationale:** Gotcha #13 -- `TradingNode.stop()` does NOT close
positions. `manage_stop=True` enables Nautilus's built-in market-exit
loop: cancel open orders, submit market orders to flatten positions
(`trading/strategy.pyx:1779`).

**Code:** `live_node_config.py` injects `"manage_stop": True` into
the config dict.

## Decision #11: Heartbeat Thread Starts Before node.build()

**Choice:** The `_HeartbeatThread` starts BEFORE `node.build()`, not
after.

**Rationale:** A hanging build (IB contract loading stuck) must age out
via the supervisor's heartbeat/watchdog mechanism. If the heartbeat
only started after build, a wedged build would never write a heartbeat
and the watchdog could not distinguish "stuck" from "not yet started".

**Code:** `trading_node_subprocess.py:run_subprocess_async` -- heartbeat
starts at step 2, before `node = node_factory(payload)` at step 4.

## Decision #12: PEL Recovery via Explicit XAUTOCLAIM

**Choice:** The command bus explicitly runs XAUTOCLAIM on startup and at
regular intervals, rather than relying on auto-redelivery.

**Rationale:** Redis Streams do NOT auto-redeliver unACKed entries the
way Kafka does. Without explicit XAUTOCLAIM, a command consumed by a
crashed supervisor would be permanently stuck in the PEL.

**Code:** `services/live_command_bus.py` -- XAUTOCLAIM runs on
`consume()` startup and at `recovery_interval_s` intervals.

## Decision #13: ACK-on-Success-Only Semantics

**Choice:** The supervisor only calls `bus.ack(entry_id)` when the
handler returns `True` AND did not raise. Never ACK in a finally block.

**Rationale:** The PEL recovery path exists precisely so un-ACKed
entries can be retried. Skipping the ACK is the retry signal. ACKing
in a finally block would acknowledge failed commands and lose them.

**Code:** `live_supervisor/main.py:run_forever` -- `if ok:
await bus.ack(command.entry_id)`.

## Decision #14: trader.is_running as the Canonical Start Signal

**Choice:** The subprocess polls `node.kernel.trader.is_running` to
confirm the trader actually started, rather than trusting the return
from `node.run_async()`.

**Rationale:** Nautilus's engine methods silently early-return on
failure. A "succeeded" return from `run_async()` does not prove the
data connections are up or the strategy is receiving bars.

**Code:** `services/nautilus/startup_health.py:wait_until_ready`.

## Decision #15: Reap Loop via Process.is_alive()

**Choice:** The supervisor polls `Process.is_alive()` every 1 second
to detect child exits, rather than waiting for heartbeat staleness.

**Rationale:** Parent and child live in the same container namespace,
so `Process.is_alive()` and `Process.exitcode` give instant exit
detection. Heartbeat is the recovery signal across supervisor restarts
only (when the handle map is empty).

**Code:** `process_manager.py:reap_loop` -- 1-second poll interval.

## Decision #16: Three-Layer Idempotency for /start

**Choice:** The `/start` endpoint has three idempotency layers:

1. HTTP Idempotency-Key (Redis SETNX, user-scoped)
2. Halt flag check
3. Identity-based warm restart (upsert on identity_signature)

**Rationale:** Network retries, concurrent requests, and the kill
switch all need to be handled atomically. The three layers ensure:

- Duplicate HTTP requests see the same response
- Kill switch blocks new starts at multiple points
- Same-identity restarts reuse existing state

**Code:** `api/live.py:live_start` -- three clearly labeled sections.

## Decision #17: Self-Write PID Before Nautilus Imports

**Choice:** The trading subprocess writes its own PID and flips
status to `building` BEFORE constructing or building the
TradingNode.

**Rationale:** The supervisor's phase C PID write is best-effort. If
it fails, `/stop` after a supervisor restart would not know the PID.
The subprocess self-write ensures PID is always populated. It also
ensures the heartbeat monitor can see the row even if the supervisor's
write path failed.

**Code:** `trading_node_subprocess.py:_self_write_pid` -- runs as the
first statement inside the try block of `run_subprocess_async`.

## Decision #18: Separate Liveness Authorities

**Choice:** The startup watchdog owns `starting`/`building` rows. The
heartbeat monitor owns `ready`/`running`/`stopping` rows. They never
overlap.

**Rationale:** v6 had both authorities include startup statuses, which
raced the watchdog's wall-clock deadline and allowed retries to spawn
duplicate children. The clean split eliminates the race.

**Code:** `heartbeat_monitor.py:_POST_STARTUP_STATUSES = ("ready",
"running", "stopping")`. `process_manager.py:watchdog_once` queries
`status.in_(("starting", "building"))`.

## Decision #19: Cross-Host PID Kill Guard

**Choice:** `ProcessManager.stop()` checks `row.host` against
`socket.gethostname()` before sending SIGTERM. If they don't match,
the command is NOT ACKed and stays in the PEL for XAUTOCLAIM
redelivery to the correct supervisor.

**Rationale:** In Phase 2's 2-VM architecture, a STOP command on the
shared Redis stream can be consumed by either supervisor. A PID from
another host's namespace is meaningless (at best ProcessLookupError,
at worst kills an unrelated process).

**Code:** `process_manager.py:stop` -- hostname comparison before
`os.kill`.

## Decision #20: Dispose After asyncio.run Returns

**Choice:** Production callers pass `skip_dispose=True` to
`run_subprocess_async` and call `node.dispose()` AFTER `asyncio.run`
returns.

**Rationale:** Nautilus 1.223.0 `TradingNode.dispose()` calls
`loop.stop()` if the kernel's loop is running. This is the loop
`asyncio.run` is blocked on, causing `RuntimeError: Event loop stopped
before Future completed`.

**Code:** `trading_node_subprocess.py` -- `on_node_constructed`
callback captures the node reference; dispose runs after `asyncio.run`
in the production wrapper.

## Decision #21: Dual Pub/Sub Channels for Projection

**Choice:** The projection pipeline publishes each event to TWO Redis
pub/sub channels: `msai:live:state:{dep_id}` for the in-process state
applier, and `msai:live:events:{dep_id}` for WebSocket streaming.

**Rationale:** The state channel feeds `ProjectionState` (in-memory,
server-side). The events channel feeds the browser via WebSocket.
Separating them means a slow WebSocket client cannot back-pressure the
state update path.

**Code:** `projection/fanout.py:DualPublisher`.

## Decision #22: uvloop Policy Reset for arq Compatibility

**Choice:** `asyncio.set_event_loop_policy(None)` is called AFTER
importing `nautilus_trader` in `workers/settings.py`.

**Rationale:** Gotcha #1 -- NautilusTrader installs uvloop's
EventLoopPolicy on import. On Python 3.12+, arq's `Worker.__init__`
calls the deprecated `asyncio.get_event_loop()`, which raises
`RuntimeError` under uvloop's policy. Resetting to the default policy
after the import restores stock asyncio semantics.

**Code:** `workers/settings.py` line 38 -- must stay BELOW the import
block.

## Decision #23: IB Disconnect Handler with 120s Grace

**Choice:** The `IBDisconnectHandler` waits 120 seconds before
triggering the kill switch on an IB disconnect.

**Rationale:** IB Gateway routinely emits brief disconnects during the
daily reset window (~23:45 ET) that auto-recover within 30 seconds. A
120s grace window avoids false alarms while still catching real outages.
The same 120s value is used by the LiveCommandBus PEL recovery
threshold.

**Code:** `disconnect_handler.py:DEFAULT_GRACE_SECONDS = 120.0`.

## Decision #24: No Auto-Resume After Kill Switch or Disconnect

**Choice:** After `/kill-all` or an IB disconnect halt, the platform
stays halted until the operator explicitly calls `/resume`. There is
no auto-resume, not even after a clean IB reconnect.

**Rationale:** A long IB outage may have left the broker side in an
inconsistent state. The operator must review before re-deploying.
Individual deployments must be re-started via `/start` after resume.

**Code:** `api/live.py:live_resume` -- only clears the halt flag.
Does NOT restart previously-running deployments.

## Decision #25: Prometheus Metrics Without prometheus_client

**Choice:** A lightweight in-process metrics registry
(`services/observability/metrics.py`) renders Prometheus exposition
format without the `prometheus_client` library.

**Rationale:** The project only needs counters and gauges (~10 metrics).
A 200-line in-process registry keeps the dependency surface small.

**Code:** `services/observability/__init__.py:MetricsRegistry`.
Exposed at `GET /metrics` (unauthenticated, operator network only).

## Decision #26: Strategy Config Derivation in Payload Factory

**Choice:** The payload factory derives `instrument_id` and `bar_type`
into the strategy config if the caller did not set them.

**Rationale:** Every bundled live strategy requires a Nautilus
`InstrumentId` and `BarType` string in its config. The `/start`
endpoint accepts bare instrument names (`["AAPL"]`) but the strategy
needs `instrument_id: "AAPL.NASDAQ"` and
`bar_type: "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"`. Without derivation,
a `config: {}` request crashes the subprocess during `node.build()`.

**Code:** `live_supervisor/__main__.py:_factory` -- `setdefault` on
`instrument_id` and `bar_type` from the first instrument.
