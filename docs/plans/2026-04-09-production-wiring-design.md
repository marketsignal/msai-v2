# Production Wiring Design

**Date:** 2026-04-09
**Status:** Approved
**Scope:** Wire remaining production gaps in claude-version so the system is fully functional for backtesting, paper trading, and live trading via Interactive Brokers. Then regenerate documentation from the now-complete code.

---

## Problem

The doc audit + gap analysis identified that the claude-version backend infrastructure is complete (ProcessManager, TradingNode subprocess, IB adapter factories, ProjectionConsumer, PositionReader, audit hook, alerting service, metrics registry, MarketHoursService) but several endpoints return stubs, several background services are never started, and the frontend still imports mock data. The system cannot be put into production until these wires are connected.

## Scope — 11 Tasks

| #   | Task                                                        | Type            | Files                                                                         |
| --- | ----------------------------------------------------------- | --------------- | ----------------------------------------------------------------------------- |
| 1   | Wire `/api/v1/live/positions` to `PositionReader`           | stub → real     | `api/live.py`                                                                 |
| 2   | Wire `/api/v1/live/trades` to `order_attempt_audits` query  | stub → real     | `api/live.py`                                                                 |
| 3   | Start ProjectionConsumer + StateApplier in FastAPI lifespan | wiring          | `main.py`, `api/live_deps.py`                                                 |
| 4   | Wire real `ib_async` account queries                        | stub → real     | `services/ib_account.py`                                                      |
| 5   | Add Prometheus counters at trading lifecycle points         | instrumentation | `process_manager.py`, `audit_hook.py`, `api/live.py`, `disconnect_handler.py` |
| 6   | Wire alert triggers to disconnect/loss/errors               | instrumentation | `disconnect_handler.py`, `risk_aware_strategy.py`, `process_manager.py`       |
| 7   | Wire MarketHoursService into RiskAwareStrategy              | wiring          | `live_supervisor/__main__.py`                                                 |
| 8   | Daily PnL aggregation arq cron job                          | new job         | `workers/pnl_aggregation.py`, `workers/settings.py`                           |
| 9   | Scheduled nightly data ingestion arq cron                   | new job         | `workers/settings.py`                                                         |
| 10  | Wire dashboard + live-trading pages to real APIs            | frontend        | `dashboard/page.tsx`, `live-trading/page.tsx`                                 |
| 11  | Regenerate all `docs/architecture/` from code               | docs            | 9 doc files                                                                   |

## Design Decisions

### Task 3 — ProjectionConsumer startup

Start in the FastAPI lifespan as a background `asyncio.Task`. One consumer per uvicorn worker. The `StateApplier` subscribes via `PSUBSCRIBE msai:live:state:*` so every worker gets every update. No new containers.

### Task 4 — ib_async account

Use `ib_async.IB()` with `connect(host, port, clientId=0)` inside an async context manager. Reuse the existing `settings.ib_host` + `settings.ib_port`. Fall back gracefully to empty response if IB is unreachable (the `live` profile may be off).

### Task 8 — Daily PnL

An arq cron function that runs at market close (4:30 PM ET). Queries `order_attempt_audits` for the day's fills per deployment, aggregates realized PnL, writes to the existing `strategy_daily_pnl` table.

### Task 9 — Nightly ingest

An arq cron function that runs at 1:00 AM ET. Calls `DataIngestionService.ingest_daily()` which already exists.

### Task 10 — Frontend

Replace `mock-data/` imports with real `apiGet()` calls. Keep mock data as fallback for when the backend is unreachable.

### Task 11 — Docs

Full rewrite of all 9 architecture docs. Comes last because it describes the final state.

## E2E Use Cases

- UC1: Deploy strategy → `/api/v1/live/positions` returns real position data
- UC2: View dashboard → equity curve shows real PnL data
- UC3: Check `/api/v1/account/summary` → returns real IB account data (or graceful error if IB offline)

## Not In Scope

- New trading strategies (that's alpha research, not infrastructure)
- Grafana/Prometheus containers (separate feature)
- Log aggregation (separate feature)
- Automated Azure deployment (separate feature)
