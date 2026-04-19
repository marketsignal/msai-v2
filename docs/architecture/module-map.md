# Module Map

Directory-by-directory tour of the backend and frontend codebases.
Every file path and class name below was verified against the source.

## Backend: `backend/src/msai/`

### `main.py` -- FastAPI Application Entrypoint

Creates the FastAPI app, registers all routers, starts the projection
pipeline (StateApplier + ProjectionConsumer) as background tasks during
lifespan startup, and provides the `/health`, `/ready`, `/metrics`, and
WebSocket endpoints.

Key globals:

- `app: FastAPI` -- the application instance
- `_stream_registry` -- per-worker `StreamRegistry` singleton for the projection consumer
- `_projection_tasks` -- background asyncio tasks for StateApplier + ProjectionConsumer

### `cli.py` -- Typer CLI

Console script `msai` with commands:

- `ingest` -- download historical OHLCV data for given symbols/dates
- `ingest-daily` -- incremental daily update (yesterday's data)
- `data-status` -- storage stats summary
- `live-start` -- start a deployment via the API
- `live-stop` -- stop a deployment via the API
- `live-status` -- list all deployments
- `live-kill-all` -- emergency halt all strategies (with confirmation prompt)

### `core/` -- Foundation Layer

| File                | Purpose                                                                                                                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py`         | `Settings(BaseSettings)` -- pydantic-settings from env vars. Module-level `settings` singleton. Properties: `parquet_root`, `reports_root`, `nautilus_catalog_root`.                                    |
| `auth.py`           | `get_current_user` FastAPI dependency. Validates Azure Entra ID JWT via PyJWT against OIDC JWKS, or falls back to `MSAI_API_KEY` header for dev. `_API_KEY_CLAIMS` dict for the synthetic API-key user. |
| `database.py`       | `create_async_engine` + `async_session_factory`. `get_db` yields sessions for FastAPI Depends.                                                                                                          |
| `logging.py`        | structlog setup. `logging_middleware` injects `request_id` into every HTTP request.                                                                                                                     |
| `queue.py`          | `get_redis_pool`, `enqueue_backtest`, `enqueue_ingest` -- arq Redis pool management.                                                                                                                    |
| `audit.py`          | `log_audit` -- writes structured entries to the `audit_logs` table.                                                                                                                                     |
| `data_integrity.py` | Parquet file integrity checks.                                                                                                                                                                          |
| `secrets.py`        | Azure Key Vault integration (Phase 2).                                                                                                                                                                  |

### `api/` -- FastAPI Routers

| File             | Prefix                | Endpoints                                                                                                                                        |
| ---------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `auth.py`        | `/api/v1/auth`        | `GET /me`, `POST /logout`                                                                                                                        |
| `strategies.py`  | `/api/v1/strategies`  | `GET /`, `GET /{id}`, `PATCH /{id}`, `POST /{id}/validate`, `DELETE /{id}`                                                                       |
| `backtests.py`   | `/api/v1/backtests`   | `POST /run`, `GET /history`, `GET /{job_id}/status`, `GET /{job_id}/results`, `GET /{job_id}/report`                                             |
| `live.py`        | `/api/v1/live`        | `POST /start`, `POST /stop`, `POST /kill-all`, `POST /resume`, `GET /status`, `GET /status/{deployment_id}`, `GET /positions`, `GET /trades`     |
| `market_data.py` | `/api/v1/market-data` | `GET /bars/{symbol}`, `GET /symbols`, `GET /status`, `POST /ingest`                                                                              |
| `account.py`     | `/api/v1/account`     | `GET /summary`, `GET /portfolio`, `GET /health`                                                                                                  |
| `websocket.py`   | (registered on app)   | `WS /api/v1/live/stream/{deployment_id}`                                                                                                         |
| `live_deps.py`   | (internal)            | FastAPI dependency providers: `get_command_bus`, `get_idempotency_store`, `get_projection_state`, `get_position_reader`, `get_live_redis_binary` |

### `models/` -- SQLAlchemy 2.0 ORM Models

| File                     | Table                  | Key Columns                                                                                                                                                                  |
| ------------------------ | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base.py`                | --                     | Declarative base class                                                                                                                                                       |
| `user.py`                | `users`                | id, entra_id, email, display_name, role                                                                                                                                      |
| `strategy.py`            | `strategies`           | id, name, file_path, strategy_class, default_config                                                                                                                          |
| `backtest.py`            | `backtests`            | id, strategy_id, config, instruments, start/end_date, status, metrics, report_path                                                                                           |
| `trade.py`               | `trades`               | id, backtest_id, instrument, side, quantity, price, pnl                                                                                                                      |
| `live_deployment.py`     | `live_deployments`     | id, strategy_id, deployment_slug, identity_signature, trader_id, strategy_id_full, account_id, message_bus_stream, config_hash, instruments_signature, paper_trading, status |
| `live_node_process.py`   | `live_node_processes`  | id, deployment_id, pid, host, status, last_heartbeat_at, failure_kind, exit_code, error_message                                                                              |
| `audit_log.py`           | `audit_logs`           | id, user_id, action, resource_type, resource_id, details                                                                                                                     |
| `order_attempt_audit.py` | `order_attempt_audits` | id, deployment_id, instrument_id, side, quantity, price, order_type, status, client_order_id, is_live, ts_attempted                                                          |
| `strategy_daily_pnl.py`  | `strategy_daily_pnls`  | strategy_id, date, realized_pnl, unrealized_pnl                                                                                                                              |
| `instrument_cache.py`    | `instrument_cache`     | Cached instrument metadata                                                                                                                                                   |

### `schemas/` -- Pydantic Request/Response Schemas

Separate schemas for each API domain: `strategy.py`, `backtest.py`,
`live.py`, `market_data.py`, `common.py`. Each domain has distinct
Create/Update/Response models following the schema separation pattern.

### `services/` -- Business Logic

| File                   | Purpose                                                                                                                                                     |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `strategy_registry.py` | `discover_strategies` scans `strategies/` dir, `compute_file_hash` SHA256, `validate_strategy_file` checks for Nautilus Strategy subclass                   |
| `risk_engine.py`       | `RiskEngine` -- pre-deployment validation. `is_halted` property.                                                                                            |
| `parquet_store.py`     | `ParquetStore` -- atomic Parquet writes, symbol listing, storage stats                                                                                      |
| `market_data_query.py` | `MarketDataQuery` -- DuckDB reads on Parquet files for OHLCV bars                                                                                           |
| `data_ingestion.py`    | `DataIngestionService` -- orchestrates Polygon + Databento downloads                                                                                        |
| `report_generator.py`  | QuantStats HTML report generation                                                                                                                           |
| `ib_account.py`        | `IBAccountService` -- queries IB Gateway for account summary/portfolio via `ib_async`                                                                       |
| `ib_probe.py`          | `IBProbe` -- IB Gateway health check (TCP connect test)                                                                                                     |
| `alerting.py`          | `AlertService` -- email alerts for strategy errors, IB disconnects (requires SMTP config)                                                                   |
| `live_command_bus.py`  | `LiveCommandBus` -- Redis Streams command bus (`msai:live:commands`), PEL recovery via XAUTOCLAIM, DLQ at `msai:live:commands:dlq`, MAX_DELIVERY_ATTEMPTS=5 |

### `services/live/` -- Live Trading Support

| File                     | Purpose                                                                                                                                                          |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `deployment_identity.py` | `derive_deployment_identity`, `derive_trader_id`, `derive_strategy_id_full`, `derive_message_bus_stream`, `generate_deployment_slug`, `normalize_request_config` |
| `failure_kind.py`        | `FailureKind` enum: NONE, HALT_ACTIVE, SPAWN_FAILED_PERMANENT, SPAWN_FAILED_TRANSIENT, RECONCILIATION_FAILED, BUILD_TIMEOUT, UNKNOWN                             |
| `idempotency.py`         | `IdempotencyStore` -- Redis SETNX-based HTTP idempotency with 24h TTL. Result types: `Reserved`, `InFlight`, `CachedOutcome`, `BodyMismatchReservation`          |

### `services/nautilus/` -- NautilusTrader Integration

| File                           | Purpose                                                                                                                                            |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live_node_config.py`          | `build_live_trading_node_config` -- constructs `TradingNodeConfig` with IB data/exec clients, Redis cache/messagebus, risk engine, strategy config |
| `trading_node_subprocess.py`   | `TradingNodePayload` dataclass, `run_subprocess_async` lifecycle, `_HeartbeatThread`, `_trading_node_subprocess` entry point                       |
| `disconnect_handler.py`        | `IBDisconnectHandler` -- 120s grace window, sets `msai:risk:halt` on extended disconnect                                                           |
| `startup_health.py`            | `wait_until_ready` -- polls `node.kernel.trader.is_running`, raises `StartupHealthCheckFailed` on timeout                                          |
| `trading_node.py`              | `TradingNodeManager` -- in-memory tracking of active nodes                                                                                         |
| `backtest_runner.py`           | `BacktestRunner` -- wraps NautilusTrader `BacktestNode` execution                                                                                  |
| `catalog_builder.py`           | Builds NautilusTrader `ParquetDataCatalog` from raw Parquet via `BarDataWrangler`                                                                  |
| `instruments.py`               | `canonical_instrument_id` -- normalizes symbol shorthand to full form                                                                              |
| `live_instrument_bootstrap.py` | `build_ib_instrument_provider_config` -- creates IB instrument provider config                                                                     |
| `strategy_loader.py`           | `resolve_importable_strategy_paths` -- resolves file path to Nautilus importable strings                                                           |
| `strategy_hash.py`             | Strategy code hashing utilities                                                                                                                    |
| `audit_hook.py`                | Order attempt audit hook for persisting order events                                                                                               |
| `market_hours.py`              | Market hours checking service                                                                                                                      |

### `services/nautilus/projection/` -- Event Projection Pipeline

| File                  | Purpose                                                                                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `consumer.py`         | `ProjectionConsumer` -- reads Nautilus message bus Redis streams via XREADGROUP, translates events via `Translator`, publishes to dual pub/sub channels |
| `translator.py`       | `Translator` -- converts raw Nautilus msgpack events to `InternalEvent` JSON                                                                            |
| `events.py`           | `InternalEvent` schema definitions                                                                                                                      |
| `fanout.py`           | `DualPublisher` -- publishes to both `msai:live:state:{dep_id}` and `msai:live:events:{dep_id}` channels. `events_channel_for(deployment_id)` helper.   |
| `state_applier.py`    | `StateApplier` -- subscribes to `msai:live:state:*` pub/sub, feeds events into `ProjectionState`                                                        |
| `projection_state.py` | `ProjectionState` -- in-memory per-deployment state (positions, account)                                                                                |
| `position_reader.py`  | `PositionReader` -- fast path from `ProjectionState`, cold path from Redis cache                                                                        |
| `registry.py`         | `StreamRegistry` -- maps deployment_id to Nautilus message bus stream name                                                                              |

### `services/nautilus/risk/` -- Risk Controls

| File                     | Purpose                                                                                                |
| ------------------------ | ------------------------------------------------------------------------------------------------------ |
| `risk_aware_strategy.py` | `RiskAwareStrategy` mixin -- in-strategy halt-flag check, market hours check, per-strategy risk limits |

### `services/observability/` -- Metrics

| File                 | Purpose                                                                                                                                                                                                                    |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `__init__.py`        | `MetricsRegistry`, `Counter`, `Gauge`, `get_registry` -- lightweight Prometheus-compatible registry without `prometheus_client` dependency                                                                                 |
| `metrics.py`         | Registry implementation, text exposition format renderer                                                                                                                                                                   |
| `trading_metrics.py` | Pre-registered counters: `DEPLOYMENTS_STARTED`, `DEPLOYMENTS_STOPPED`, `DEPLOYMENTS_FAILED`, `KILL_SWITCH_ACTIVATED`, `ORDERS_SUBMITTED`, `ORDERS_FILLED`, `ORDERS_DENIED`, `IB_DISCONNECTS`, `ACTIVE_DEPLOYMENTS` (gauge) |

### `services/data_sources/` -- External Data Clients

- `polygon_client.py` -- Polygon.io REST client for stocks/options OHLCV
- `databento_client.py` -- Databento client for futures data

### `live_supervisor/` -- Live Trading Process Manager

| File                   | Purpose                                                                                                                                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `__main__.py`          | Entry point (`python -m msai.live_supervisor`). Wires ProcessManager with `_trading_node_subprocess` as spawn target and `_build_production_payload_factory` as payload factory. Installs SIGTERM/SIGINT handlers. |
| `main.py`              | `run_forever` -- command consumer loop + background tasks (reap, heartbeat monitor, startup watchdog). ACK-on-success-only semantics.                                                                              |
| `process_manager.py`   | `ProcessManager` -- INSERT-spawn-UPDATE pattern (3-phase), halt-flag re-check, reap loop, startup watchdog.                                                                                                        |
| `heartbeat_monitor.py` | `HeartbeatMonitor` -- stale-heartbeat sweep for post-startup rows (ready/running/stopping), 30s threshold.                                                                                                         |

### `workers/` -- arq Background Jobs

| File                 | Purpose                                                                                                                                                                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `settings.py`        | `WorkerSettings` -- registers `run_backtest` and `run_ingest` functions, cron jobs for `aggregate_daily_pnl` (20:30 UTC) and `run_nightly_ingest` (05:00 UTC). `asyncio.set_event_loop_policy(None)` after nautilus import to fix uvloop/arq conflict. |
| `backtest_job.py`    | `run_backtest_job` -- executes backtest via BacktestRunner                                                                                                                                                                                             |
| `nightly_ingest.py`  | `run_nightly_ingest` -- cron job for daily data update                                                                                                                                                                                                 |
| `pnl_aggregation.py` | `aggregate_daily_pnl` -- cron job for daily P&L rollup                                                                                                                                                                                                 |

## Frontend: `frontend/src/`

### `app/` -- Next.js 15 Pages

| Directory          | Route              | Purpose                                             |
| ------------------ | ------------------ | --------------------------------------------------- |
| `page.tsx`         | `/`                | Redirects to dashboard                              |
| `dashboard/`       | `/dashboard`       | Portfolio overview, equity curve, active strategies |
| `strategies/`      | `/strategies`      | Strategy list, detail, config editor                |
| `backtests/`       | `/backtests`       | Backtest history, results, QuantStats report viewer |
| `live-trading/`    | `/live-trading`    | Live deployment management, positions, trades       |
| `market-data/`     | `/market-data`     | OHLCV chart viewer, symbol browser                  |
| `data-management/` | `/data-management` | Data ingestion controls, storage stats              |
| `settings/`        | `/settings`        | Application settings                                |
| `login/`           | `/login`           | Azure Entra ID login page                           |
| `layout.tsx`       | --                 | Root layout with sidebar navigation                 |
| `globals.css`      | --                 | Tailwind CSS + shadcn/ui dark theme                 |

### `components/` -- React Components

| Directory       | Purpose                                                      |
| --------------- | ------------------------------------------------------------ |
| `layout/`       | Sidebar navigation, header, page layout                      |
| `charts/`       | TradingView Lightweight Charts wrappers, Recharts components |
| `dashboard/`    | Dashboard-specific widgets (equity curve, strategy status)   |
| `strategies/`   | Strategy list/detail/editor components                       |
| `backtests/`    | Backtest result display, report iframe                       |
| `live/`         | Live deployment controls, position table, trade log          |
| `data/`         | Data management components                                   |
| `ui/`           | shadcn/ui primitives (button, card, dialog, table, etc.)     |
| `providers.tsx` | React context providers (auth, theme)                        |

### `lib/` -- Frontend Utilities

Located in `frontend/src/lib/`:

- `auth.ts` -- MSAL configuration and token acquisition
- `api.ts` -- HTTP client wrapper for backend API calls
- `msal-config.ts` -- Azure Entra ID MSAL configuration
- `format.ts` -- Number/date formatting utilities
- `mock-data.ts` -- Mock data for development (dashboard equity curve, strategy status still use this)
