# Module Map

This document is the "where does this logic live?" reference.

## Backend Package Map

### API layer

Path:
[backend/src/msai/api](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api)

| Module | Purpose |
|---|---|
| [auth.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/auth.py) | auth endpoints and identity-facing API helpers |
| [strategies.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/strategies.py) | strategy listing and registry-facing endpoints |
| [backtests.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/backtests.py) | one-off backtest APIs |
| [research.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/research.py) | queued sweeps, walk-forward jobs, reports, promotions |
| [market_data.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/market_data.py) | historical ingest, daily universe, data-status APIs |
| [live.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/live.py) | live start/stop/status/orders/positions/risk APIs |
| [alerts.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/alerts.py) | persisted alert feed |
| [account.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/account.py) | broker/account oriented read APIs |
| [websocket.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api/websocket.py) | WebSocket auth and live snapshot/event fanout |

### Core layer

Path:
[backend/src/msai/core](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core)

| Module | Purpose |
|---|---|
| [config.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/config.py) | central settings and path conventions |
| [auth.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/auth.py) | `X-API-Key` and Entra auth validation |
| [database.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/database.py) | SQLAlchemy engine and sessions |
| [queue.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/queue.py) | Redis pool and ARQ queue helpers |
| [audit.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/audit.py) | request audit middleware |
| [logging.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/core/logging.py) | structured logging |

### Nautilus integration layer

Path:
[backend/src/msai/services/nautilus](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus)

| Module | Purpose |
|---|---|
| [instrument_service.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/instrument_service.py) | canonical instrument resolution for Databento research and IB live |
| [catalog_builder.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/catalog_builder.py) | builds Nautilus catalog data from raw inputs |
| [backtest_runner.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/backtest_runner.py) | executes Nautilus backtests |
| [strategy_loader.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/strategy_loader.py) | imports strategy modules and config classes |
| [strategy_config.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/strategy_config.py) | prepares strategy config for backtest/live modes |
| [trading_node.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/trading_node.py) | live TradingNode start/stop/reconcile/liquidate control |
| [live_state.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/live_state.py) | in-node controller publishing snapshots and persisting live events |
| [instruments.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus/instruments.py) | instrument payload encode/decode helpers |

### Data and research services

Path:
[backend/src/msai/services](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services)

| Module | Purpose |
|---|---|
| [data_ingestion.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/data_ingestion.py) | orchestrates historical ingest and status |
| [data_sources/databento_client.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/data_sources/databento_client.py) | Databento HTTP access and definition loading |
| [data_sources/polygon_client.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/data_sources/polygon_client.py) | Polygon fallback path |
| [parquet_store.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/parquet_store.py) | writes raw market-data Parquet files |
| [research_engine.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/research_engine.py) | parameter sweeps and walk-forward execution |
| [research_jobs.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/research_jobs.py) | file-backed queued job state |
| [research_artifacts.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/research_artifacts.py) | report listing, detail, compare, promotion artifacts |
| [daily_universe.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/daily_universe.py) | persisted scheduler universe |
| [alerting.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/alerting.py) | persistent alert feed |
| [live_runtime.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/live_runtime.py) | backend-side client for dedicated live-runtime worker |
| [live_updates.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/live_updates.py) | snapshot publishing and replay helpers |
| [live_state_view.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/live_state_view.py) | merges runtime snapshots into API payloads |
| [risk_engine.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/risk_engine.py) | app-level halt state and kill-all gate |
| [strategy_registry.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/strategy_registry.py) | discovers and syncs strategy metadata |

### Worker entrypoints

Path:
[backend/src/msai/workers](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers)

| Worker | Purpose |
|---|---|
| [settings.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/settings.py) | research queue worker settings |
| [research_job.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/research_job.py) | queued sweep and walk-forward execution |
| [backtest_job.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/backtest_job.py) | queued backtest execution |
| [live_settings.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/live_settings.py) | live-runtime queue worker settings |
| [live_runtime.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/live_runtime.py) | live start/stop/status/kill-all handlers |
| [daily_scheduler.py](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers/daily_scheduler.py) | polling daily ingest scheduler |

## Frontend Map

Path:
[frontend/src/app](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app)

| Page | Purpose |
|---|---|
| [dashboard/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/dashboard/page.tsx) | high-level operator view |
| [data/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/data/page.tsx) | historical ingest, daily universe, alert feed |
| [backtests/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/backtests/page.tsx) | one-off backtest execution |
| [research/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/research/page.tsx) | report browser, compare, promotion flow |
| [live/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/live/page.tsx) | live deployment and monitoring |
| [strategies/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/strategies/page.tsx) | strategy discovery |
| [settings/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/settings/page.tsx) | system settings UI |
| [login/page.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app/login/page.tsx) | browser auth entrypoint |

Shared shell and auth:

- [app-shell.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/components/layout/app-shell.tsx)
- [sidebar.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/components/layout/sidebar.tsx)
- [header.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/components/layout/header.tsx)
- [api.ts](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/lib/api.ts)
- [auth.tsx](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/lib/auth.tsx)
- [auth-mode.ts](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/lib/auth-mode.ts)

## Strategy Modules

Path:
[strategies](/Users/pablomarin/Code/msai-v2/codex-version/strategies)

Strategies are shared between research and live.

Examples:

- [mean_reversion.py](/Users/pablomarin/Code/msai-v2/codex-version/strategies/example/mean_reversion.py)
- [donchian_breakout.py](/Users/pablomarin/Code/msai-v2/codex-version/strategies/example/donchian_breakout.py)

That is one of the core architectural promises of the system:

- same strategy code
- different runtime mode
- different data/execution adapters

## Data Directory Map

Path:
[data](/Users/pablomarin/Code/msai-v2/codex-version/data)

| Path | Purpose |
|---|---|
| [data/parquet](/Users/pablomarin/Code/msai-v2/codex-version/data/parquet) | raw historical bars |
| [data/databento/definitions](/Users/pablomarin/Code/msai-v2/codex-version/data/databento/definitions) | Databento `DEFINITION` files |
| [data/nautilus](/Users/pablomarin/Code/msai-v2/codex-version/data/nautilus) | Nautilus catalog |
| [data/reports](/Users/pablomarin/Code/msai-v2/codex-version/data/reports) | report artifacts |
| [data/research](/Users/pablomarin/Code/msai-v2/codex-version/data/research) | research reports, jobs, promotions |
| [data/scheduler](/Users/pablomarin/Code/msai-v2/codex-version/data/scheduler) | daily universe and scheduler state |
| [data/alerts](/Users/pablomarin/Code/msai-v2/codex-version/data/alerts) | alert feed |

## If You Are Looking For A Bug

Use this sequence:

1. API route in [backend/src/msai/api](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/api)
2. service in [backend/src/msai/services](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services)
3. Nautilus integration in [backend/src/msai/services/nautilus](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/services/nautilus)
4. worker entrypoint in [backend/src/msai/workers](/Users/pablomarin/Code/msai-v2/codex-version/backend/src/msai/workers)
5. UI caller in [frontend/src/app](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/app) or [frontend/src/lib/api.ts](/Users/pablomarin/Code/msai-v2/codex-version/frontend/src/lib/api.ts)
