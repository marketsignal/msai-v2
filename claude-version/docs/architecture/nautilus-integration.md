# Nautilus Integration

Where MSAI ends and NautilusTrader begins. This document covers the
config builder, instrument bootstrap, IB adapter wiring, cache and
message bus Redis persistence, and the projection consumer that bridges
Nautilus events into MSAI's dashboard.

## Config Builder

Source: `services/nautilus/live_node_config.py`

### `build_live_trading_node_config` Signature

```python
def build_live_trading_node_config(
    *,
    deployment_slug: str,
    strategy_path: str,
    strategy_config_path: str,
    strategy_config: dict[str, Any],
    paper_symbols: list[str],
    ib_settings: IBSettings,
    max_notional_per_order: dict[str, int] | None = None,
    max_order_submit_rate: str = "100/00:00:01",
    max_order_modify_rate: str = "100/00:00:01",
) -> TradingNodeConfig:
```

### TradingNodeConfig Values

The builder constructs a `TradingNodeConfig` with these exact settings
(verified against the source):

**Trader identity:**

- `trader_id`: `TraderId("MSAI-{deployment_slug}")` -- derived from 16-char hex slug

**State persistence:**

- `load_state`: `True` -- rehydrate strategy state on restart
- `save_state`: `True` -- persist strategy state on stop

**Data engine:**

- `LiveDataEngineConfig()` -- defaults

**Execution engine:**

- `LiveExecEngineConfig`:
  - `reconciliation`: `True` -- reconcile against IB on startup
  - `reconciliation_lookback_mins`: `1440` (24 hours)
  - `inflight_check_interval_ms`: `2000`
  - `inflight_check_threshold_ms`: `5000`
  - `position_check_interval_secs`: `60`

**Risk engine:**

- `LiveRiskEngineConfig`:
  - `bypass`: `False` -- every order goes through the engine
  - `max_order_submit_rate`: `"100/00:00:01"` (100 per second)
  - `max_order_modify_rate`: `"100/00:00:01"` (100 per second)
  - `max_notional_per_order`: caller-provided dict or `{}`

**Cache:**

- `CacheConfig`:
  - `database`: `DatabaseConfig(type="redis", ...)` parsed from `REDIS_URL`
  - `encoding`: `"msgpack"`
  - `buffer_interval_ms`: `None` (write-through, no buffering -- gotcha #7)
  - `persist_account_events`: `True`

**Message bus:**

- `MessageBusConfig`:
  - `database`: same `DatabaseConfig` as cache (shared Redis instance)
  - `encoding`: `"msgpack"` (gotcha #17 -- JSON fails on Decimal/datetime/Path)
  - `stream_per_topic`: `False` (one stream per trader, topics in message body)
  - `use_trader_prefix`: `True`
  - `use_trader_id`: `True`
  - `streams_prefix`: `"stream"`
  - `buffer_interval_ms`: `None` (write-through)

Resulting stream name: `stream/MSAI-{deployment_slug}`

**Strategy:**

- Single `ImportableStrategyConfig`:
  - `strategy_path`: e.g. `"strategies.example.ema_cross:EMACrossStrategy"`
  - `config_path`: e.g. `"strategies.example.config:EMACrossConfig"`
  - `config`: merged dict with `manage_stop=True` and `order_id_tag=deployment_slug`
    injected on top of the caller's config

### IB Client Wiring

Two separate IB clients per deployment (gotcha #3):

**Data client** (`InteractiveBrokersDataClientConfig`):

- `ibg_host`: from `IBSettings.host` (default `"127.0.0.1"`, `"ib-gateway"` in Docker)
- `ibg_port`: from `IBSettings.port` (default `4002`)
- `ibg_client_id`: `_derive_data_client_id(deployment_slug)` -- stable 31-bit hash
- `instrument_provider`: from `build_ib_instrument_provider_config(paper_symbols)`

**Exec client** (`InteractiveBrokersExecClientConfig`):

- `ibg_host`, `ibg_port`: same as data client
- `ibg_client_id`: `_derive_exec_client_id(deployment_slug)` -- different hash (salted with `"exec"`)
- `account_id`: normalized (stripped) account ID from deployment row
- `instrument_provider`: same as data client

Client ID derivation (`_derive_client_id`):

```python
digest = hashlib.sha256(deployment_slug.encode("ascii") + role.encode("ascii")).digest()
raw = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
return raw or 1  # zero mapped to 1 (IB reserves client_id=0)
```

Both clients are registered under `IB_VENUE.value` (the Nautilus
constant for the IB venue):

```python
data_clients={IB_VENUE.value: data_client}
exec_clients={IB_VENUE.value: exec_client}
```

### Port/Account Validation

Source: `live_node_config.py:_validate_port_account_consistency`

Called at config-build time. Rejects silent gotcha #6 misconfigurations:

| Port  | Account Prefix | Result                                 |
| ----- | -------------- | -------------------------------------- |
| 4002  | DU...          | OK (paper port + paper account)        |
| 4002  | U...           | ValueError (paper port + live account) |
| 4001  | U...           | OK (live port + live account)          |
| 4001  | DU...          | ValueError (live port + paper account) |
| Other | Any            | ValueError (unsupported port)          |

Blank/whitespace-only account IDs are also rejected.

### Redis DatabaseConfig Builder

Source: `live_node_config.py:build_redis_database_config`

Parses `settings.redis_url` to construct a Nautilus `DatabaseConfig`:

```python
DatabaseConfig(
    type="redis",
    host=parsed.hostname or "localhost",
    port=parsed.port or 6379,
    username=parsed.username,
    password=parsed.password,
    ssl=parsed.scheme == "rediss",  # rediss:// = TLS
)
```

This helper is shared by both the live `TradingNodeConfig` writers
(cache + message bus) and the `PositionReader` cold path, so both
sides always use the same Redis connection parameters.

## Instrument Bootstrap

Source: `services/nautilus/live_instrument_bootstrap.py`

`build_ib_instrument_provider_config(paper_symbols)` creates the
instrument provider config that tells Nautilus which IB contracts to
load at startup. The `paper_symbols` list contains bare symbols (e.g.
`["AAPL", "MSFT"]`) extracted from `deployment.instruments` by
stripping the venue suffix.

## Strategy Loading

Source: `services/nautilus/strategy_loader.py`

`resolve_importable_strategy_paths(strategy_file, strategy_class_name)`
converts a filesystem path like `strategies/example/ema_cross.py` and a
class name like `EMACrossStrategy` into the two Nautilus importable
path strings:

- `strategy_path`: e.g. `"strategies.example.ema_cross:EMACrossStrategy"`
- `config_path`: e.g. `"strategies.example.config:EMACrossConfig"`

The same helper is used by both the backtest runner and the live
payload factory -- single source of truth.

## Startup Health Check

Source: `services/nautilus/startup_health.py`

`wait_until_ready(node, timeout_s, shutdown_event)` polls
`node.kernel.trader.is_running` until it flips to `True` or the
timeout expires. This is the canonical FSM signal that the trader
actually started (decision #14).

Nautilus's engine methods silently early-return on failure, so a
"succeeded" return from `node.run_async()` does not prove the trader
is live. The health check catches the gap.

Raises `StartupHealthCheckFailed` on timeout. The subprocess maps
this to exit code 2 and `FailureKind.RECONCILIATION_FAILED`.

## Backtest Runner

Source: `services/nautilus/backtest_runner.py`

`BacktestRunner` wraps NautilusTrader's `BacktestNode`:

1. Reads the backtest row from PostgreSQL
2. Builds a `ParquetDataCatalog` from raw Parquet via `BarDataWrangler`
   (`catalog_builder.py`)
3. Configures `BacktestNode` with venue configs derived from canonical
   instrument IDs
4. Runs the backtest
5. Extracts metrics (from `BacktestNode.generate_*_report()` methods)
6. Generates a QuantStats HTML report
7. Persists results + trade rows to PostgreSQL

Note: `generate_account_report()` requires `venue=Venue("SIM")`
(gotcha #2). The other two reports (`generate_orders_report`,
`generate_positions_report`) do not need this parameter.

## Projection Pipeline

The projection pipeline bridges Nautilus's internal event system
(msgpack-encoded Redis streams) into MSAI's dashboard layer
(JSON pub/sub channels).

### How It Works

1. **TradingNode writes** to a single Redis stream per trader:
   `stream/MSAI-{deployment_slug}`. Events are msgpack-encoded.
   `stream_per_topic=False` means all event types go to one stream
   with the topic encoded in the message body.

2. **ProjectionConsumer** (`projection/consumer.py`) runs as a
   background task in the FastAPI process. It uses `XREADGROUP` with
   a consumer group to read from every active deployment's stream
   (discovered via `StreamRegistry`). For each message:
   - `Translator` converts msgpack bytes to `InternalEvent` JSON
   - `DualPublisher` publishes the JSON to two Redis pub/sub channels

3. **DualPublisher** (`projection/fanout.py`) writes to:
   - `msai:live:state:{deployment_id}` -- consumed by `StateApplier`
   - `msai:live:events:{deployment_id}` -- consumed by WebSocket handler

4. **StateApplier** (`projection/state_applier.py`) subscribes to
   `msai:live:state:*` and feeds events into `ProjectionState`.

5. **ProjectionState** (`projection/projection_state.py`) maintains
   in-memory per-deployment state (positions, account snapshots).

6. **PositionReader** (`projection/position_reader.py`) serves queries:
   - Fast path: reads from `ProjectionState` (in-memory, same worker)
   - Cold path: rebuilds from Redis cache (on worker restart or cache miss)

7. **StreamRegistry** (`projection/registry.py`) maps deployment IDs
   to stream names. Populated at startup from active DB rows and
   updated when `/api/v1/live/start` registers new deployments via
   `main.py:get_stream_registry().register(...)`.

### Startup Sequence

During FastAPI lifespan startup (`main.py:_start_projection_tasks`):

1. Create `ProjectionState` instance
2. Create text-mode Redis client for `StateApplier`
3. Create binary-mode Redis client for `ProjectionConsumer`
4. Initialize `StreamRegistry` and populate from DB (active deployments)
5. Start `StateApplier` as background asyncio task
6. Start `ProjectionConsumer` as background asyncio task

## Audit Hook

Source: `services/nautilus/audit_hook.py`

Persists every order attempt to the `order_attempt_audits` table with:

- `deployment_id`, `instrument_id`, `side`, `quantity`, `price`
- `order_type`, `status`, `client_order_id`
- `is_live` flag, `ts_attempted` timestamp

The `order_id_tag=deployment_slug` in the strategy config makes every
Nautilus `client_order_id` prefix-stable across restarts, enabling the
audit hook to correlate orders to deployments deterministically.

## RiskAwareStrategy Mixin

Source: `services/nautilus/risk/risk_aware_strategy.py`

A strategy mixin that adds pre-order checks:

- Kill switch halt-flag check (layer 4 of the four-layer kill switch)
- Market hours check
- Per-strategy position and loss limits (Phase 2)

Strategies that inherit from `RiskAwareStrategy` get these checks
applied before every order submission.

## Key Gotchas Applied

These NautilusTrader gotchas (from `.claude/rules/nautilus.md`) are
explicitly handled in the codebase:

| Gotcha                             | Where Applied                | How                                                         |
| ---------------------------------- | ---------------------------- | ----------------------------------------------------------- |
| #1 (uvloop policy)                 | `workers/settings.py`        | `asyncio.set_event_loop_policy(None)` after nautilus import |
| #2 (generate_account_report)       | `backtest_runner.py`         | Always pass `venue=Venue("SIM")`                            |
| #3 (duplicate ibg_client_id)       | `live_node_config.py`        | Unique data/exec client IDs per deployment via SHA256       |
| #6 (port/account mismatch)         | `live_node_config.py`        | `_validate_port_account_consistency` at build time          |
| #7 (buffered cache loss)           | `live_node_config.py`        | `buffer_interval_ms=None` (write-through)                   |
| #8 (backtest pollutes Redis)       | `live_node_config.py`        | Different `MessageBusConfig` per environment                |
| #13 (stop doesn't close positions) | `live_node_config.py`        | `manage_stop=True` in strategy config                       |
| #14 (is_running as FSM signal)     | `startup_health.py`          | `wait_until_ready` polls `trader.is_running`                |
| #17 (JSON encoding fails)          | `live_node_config.py`        | `encoding="msgpack"` for cache and message bus              |
| #18 (asyncio.run conflicts)        | `trading_node_subprocess.py` | `node.run_async()` as task, not `asyncio.run(node.run())`   |
| #20 (dispose leaks)                | `trading_node_subprocess.py` | try/finally with dispose after asyncio.run returns          |
