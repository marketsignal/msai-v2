# Platform Overview

## What Is MSAI v2?

MSAI v2 is a personal hedge fund platform for automated trading via
Interactive Brokers. It lets you:

- Define trading strategies as Python files in a `strategies/` directory
- Backtest them against historical minute-level OHLCV data stored as Parquet files
- Deploy them to paper or live trading through IB Gateway
- Monitor portfolio performance, open positions, and trade executions through a web dashboard
- Control the platform via REST API, WebSocket, or CLI

## The Nautilus/MSAI Split

NautilusTrader is the core trading engine. MSAI wraps around it:

**NautilusTrader owns:**

- Order lifecycle (submission, fill, cancel, modify)
- Position tracking and portfolio accounting
- IB Gateway data + execution adapter (via `InteractiveBrokersDataClientConfig` / `InteractiveBrokersExecClientConfig`)
- Backtest execution via `BacktestNode` + `BacktestEngine`
- Live execution via `TradingNode` + `TradingNodeConfig`
- Cache persistence to Redis (`CacheConfig.database`)
- Message bus event streaming to Redis (`MessageBusConfig.database`)
- Startup reconciliation against IB (`LiveExecEngineConfig.reconciliation=True`)
- Native risk engine rate limits (`LiveRiskEngineConfig.max_order_submit_rate`)
- Strategy state persistence (`load_state=True`, `save_state=True`)
- Flatten-on-stop (`manage_stop=True` in `ImportableStrategyConfig`)

**MSAI owns:**

- Web dashboard (Next.js frontend)
- REST API layer (FastAPI)
- Authentication (Azure Entra ID JWT validation via PyJWT)
- Strategy registry (file discovery + DB sync)
- Live supervisor process manager (spawn, reap, watchdog, heartbeat)
- Redis Streams command bus (`msai:live:commands` with PEL recovery + DLQ)
- Projection pipeline (translates Nautilus message bus events into dashboard-friendly JSON)
- WebSocket streaming (per-deployment pub/sub via Redis)
- Kill switch (four-layer halt: Redis flag, supervisor re-check, push-based stop, in-strategy check)
- IB disconnect handler (120s grace window, then auto-halt)
- Prometheus metrics endpoint (`/metrics`)
- Audit logging (every start/stop/kill-all persisted to `audit_logs` table)
- Data ingestion from Polygon.io and Databento
- Backtest job queue (arq + Redis)
- QuantStats HTML report generation
- Typer CLI for data ingestion and live trading control

## Current Capabilities (Phase 1)

Phase 1 is complete. The system can:

- Ingest OHLCV data from Polygon.io (stocks) and Databento (futures) into Parquet files
- Run backtests via NautilusTrader's BacktestNode with QuantStats HTML reports
- Deploy strategies to paper trading via IB Gateway inside Docker Compose
- Stream live position and order events to the frontend via WebSocket
- Emergency halt all strategies with a four-layer kill switch
- Auto-halt on extended IB disconnects (120s grace window)
- Reconcile against IB on subprocess restart (`reconciliation_lookback_mins=1440`)
- Persist cache and message bus state to Redis (write-through, `buffer_interval_ms=None`)
- Expose Prometheus counters for deployments started/stopped/failed, orders, kill switch activations, IB disconnects

## Known Stubs and Limitations

These endpoints or features are not yet fully wired to production data:

- **`/api/v1/live/trades`** returns data from `order_attempt_audits` (submitted price, not fill price from the broker)
- **`/api/v1/account/summary`** and **`/api/v1/account/portfolio`** return zero-valued data when IB Gateway is offline (they use `ib_async` to query IB directly)
- **Dashboard equity curve** on the main dashboard page still renders from mock data
- **`StrategyStatus` component** on the dashboard still renders from mock deployments
- **`AlertService`** requires SMTP configuration (`alerting.py`) to actually send emails; without it, alert calls are best-effort no-ops

## Phase 2 Scope (Not Yet Implemented)

- Split from single VM to 2-VM architecture (trading VM-A + compute VM-B)
- Real-money live trading (requires release sign-off checklist)
- Per-strategy max position and daily loss limits (custom `RiskAwareStrategy` mixin exists, not yet enforced in all strategies)
- Options chain support and multi-asset strategies
- Azure Key Vault integration for production secrets
- Frontend session management with refresh tokens

## Test Coverage

The backend has **814 test functions** across `backend/tests/unit/` and
`backend/tests/integration/`. Run with:

```bash
cd backend && uv run pytest tests/ -v
```
