# MSAI v2 — System Design Document

**Date:** 2026-02-25
**Status:** Approved
**Author:** Pablo Marin + Claude + Codex

---

## 1. Vision

MSAI v2 is a personal hedge fund platform for automated trading. It enables:

- Defining trading strategies as Python files
- Backtesting strategies against historical minute-level data
- Deploying strategies to live trading via Interactive Brokers
- Monitoring portfolio performance, positions, and P&L through a web dashboard
- Managing everything from both a web UI and CLI

It replaces MSAI v1, which was a Jupyter notebook-driven system with a mixed Python/C# stack on Azure Kubernetes Service.

---

## 2. Scope

### In Scope (v1 release)

- Strategy authoring (Python files, NautilusTrader Strategy subclass)
- Backtesting engine with performance reports
- Live paper trading via IB Gateway
- Web dashboard: portfolio monitoring, backtest results, price charts
- Data ingestion: minute bars for stocks, indexes, futures, constrained options
- Azure Entra ID authentication
- Docker Compose deployment on Azure VM

### Out of Scope (future)

- AI/LLM-powered analysis and signal generation
- Portfolio manager service (capital allocation, risk-parity weighting, strategy rebalancing)
- Strategy file uploads via UI (Phase 1 uses git-only; UI uploads require sandbox approval workflow)
- Multi-tenant SaaS capabilities
- High-frequency trading (sub-second)
- Full options universe (all strikes, all expirations)
- Mobile app

### Phased Options Scope

- **Phase 1:** Options on top 30 underlyings only (SPY, QQQ, AAPL, MSFT, etc.), 0-60 DTE near-term expirations
- **Phase 2:** Expand to options on all 100 tracked underlyings, wider DTE range

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Azure VM (D4s_v5 or D8s_v5)                         │
│                                                                         │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────────────┐ │
│  │  Next.js 15   │  │  FastAPI       │  │  PostgreSQL 16               │ │
│  │  Frontend     │──│  Backend       │──│  (users, strategies,         │ │
│  │  :3000        │  │  :8000         │  │   backtest results,          │ │
│  │  React +      │  │  Python 3.12+  │  │   trade logs, audit trail)   │ │
│  │  shadcn/ui +  │  │  DuckDB inside │  │                              │ │
│  │  Tailwind     │  │  (read-only)   │  └──────────────────────────────┘ │
│  └──────────────┘  └───────┬───────┘                                    │
│                            │                                             │
│          ┌─────────────────┼─────────────────┐                          │
│          │                 │                  │                          │
│  ┌───────▼───────┐ ┌──────▼───────┐ ┌───────▼────────┐                │
│  │ arq Backtest   │ │ Nautilus      │ │ Data            │                │
│  │ Worker         │ │ TradingNode   │ │ Ingestion       │                │
│  │ (always-on     │ │ (always-on)   │ │ Service          │                │
│  │  worker pool,  │ │               │ │ (cron-scheduled) │                │
│  │  via Redis)    │ │               │ │                  │                │
│  └───────┬───────┘ └──────┬───────┘ └───────┬────────┘                │
│  └───────┬───────┘ └──────┬───────┘          │                          │
│          │                │                   │                          │
│          │         ┌──────▼───────┐           │                          │
│          │         │ IB Gateway    │           │                          │
│          │         │ (Docker)      │    Polygon.io WS                    │
│          │         │ :4001/:4002   │    Databento API                    │
│          │         └──────────────┘           │                          │
│          │                                    │                          │
│  ┌───────▼────────────────────────────────────▼────────┐                │
│  │              {DATA_ROOT}/parquet/ (local SSD)               │                │
│  │   stocks/  indexes/  futures/  options/  crypto/      │                │
│  │   Partitioned: asset_class/symbol/YYYY/MM/*.parquet   │                │
│  └────────────────────────┬────────────────────────────┘                │
│                           │ nightly sync                                 │
└───────────────────────────┼──────────────────────────────────────────────┘
                            ▼
                     Azure Blob Storage (backup + durability)
```

### Docker Compose Services (6 containers)

| Service           | Image                | Port     | Role                                                    | Always on? |
| ----------------- | -------------------- | -------- | ------------------------------------------------------- | ---------- |
| `frontend`        | Next.js 15           | 3000     | Dashboard UI                                            | Yes        |
| `backend`         | FastAPI + DuckDB     | 8000     | REST API + WebSocket. Spawns TradingNode as subprocess. | Yes        |
| `postgres`        | PostgreSQL 16        | 5432     | App state                                               | Yes        |
| `redis`           | Redis 7              | 6379     | Job queue (arq) + WebSocket pub/sub                     | Yes        |
| `ib-gateway`      | IB Gateway Docker    | internal | Broker connectivity (not exposed to host)               | Yes        |
| `backtest-worker` | arq + NautilusTrader | —        | Backtest + data ingestion jobs (arq worker pool)        | Yes        |

Note: `trading-node` is a subprocess spawned by the backend (not a separate container) — NautilusTrader requires one engine per process. `data-ingestion` runs as arq jobs inside the `backtest-worker` container.

---

## 4. Technology Decisions

| Component                   | Choice                                            | Rationale                                                                                                             |
| --------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Backtesting + Execution** | NautilusTrader                                    | Same strategy code for backtest and live. Rust core performance. Native IB adapter. Native Parquet/DataFusion reader. |
| **Broker**                  | Interactive Brokers via IB Gateway                | Supports all asset classes. Dockerized gateway available.                                                             |
| **IB Python client**        | `ib_async` (escape hatch)                         | Maintained successor to `ib_insync`. Used only for IB-specific edge cases outside NautilusTrader.                     |
| **Frontend**                | Next.js 15 + React + shadcn/ui + Tailwind CSS     | Modern, fast, great DX. shadcn/ui for polished components.                                                            |
| **Backend**                 | Python 3.12 + FastAPI + async/await               | Fast async API. Same language as NautilusTrader and data science ecosystem.                                           |
| **Dashboard charts**        | Lightweight Charts (TradingView) + Recharts       | Native React charts for dashboard. No Matplotlib PNGs.                                                                |
| **Backtest reports**        | QuantStats (downloadable HTML)                    | Actively maintained replacement for pyfolio. Generate full tearsheet as downloadable report.                          |
| **App database**            | PostgreSQL 16                                     | Users, strategies, backtest metadata, trade logs, audit trail.                                                        |
| **Market data storage**     | Parquet files (local SSD + Azure Blob)            | NautilusTrader reads natively via DataFusion. ~103 GB for 10yr.                                                       |
| **Dashboard data queries**  | DuckDB (embedded in FastAPI)                      | Read-only queries on Parquet for price charts and analytics. Zero infrastructure.                                     |
| **Authentication**          | Azure Entra ID                                    | App registration with client secret, tenant ID, client ID. Small group of users.                                      |
| **Secrets**                 | Azure Key Vault + managed identity                | No secrets in code or env files. Rotate via Key Vault.                                                                |
| **Deployment**              | Docker Compose on Azure VM                        | Single VM for Phase 1. Split to 2 VMs for Phase 2 (real money).                                                       |
| **Data sources**            | Polygon.io (stocks/options) + Databento (futures) | IB for execution and real-time quotes only. Not for historical data.                                                  |
| **Python version**          | 3.12 or 3.13                                      | NautilusTrader IB extras not available on 3.14. Pin this.                                                             |

---

## 5. Data Architecture

**`DATA_ROOT` canonical path:** All code uses `settings.data_root` (from `DATA_ROOT` env var). Defaults:

- **Docker:** `/app/data` (volume-mounted from `./data` on host)
- **Local dev:** `./data` (relative to project root)

This ensures Parquet files always land on the mounted volume inside Docker and never on an ephemeral container path.

### 5.1 Data Scope

| Asset Class | Symbols                                | Frequency  | Source                      |
| ----------- | -------------------------------------- | ---------- | --------------------------- |
| US Stocks   | Top 100                                | 1-min bars | Polygon.io                  |
| Indexes     | Top 100                                | 1-min bars | Polygon.io                  |
| Futures     | Top 100                                | 1-min bars | Databento                   |
| Options     | Chains on top 30 underlyings, 0-60 DTE | 1-min bars | Polygon.io / Databento OPRA |
| Crypto      | 3 coins (BTC, ETH, SOL)                | 1-min bars | Polygon.io                  |

### 5.2 Data Volumes

| Asset Class                           | Rows/day | Raw/day         | Raw/year       | 10yr compressed (10-15x) |
| ------------------------------------- | -------- | --------------- | -------------- | ------------------------ |
| Stocks                                | 39,000   | 2 MB            | 504 MB         | ~500 MB                  |
| Indexes                               | 39,000   | 2 MB            | 504 MB         | ~500 MB                  |
| Futures                               | 138,000  | 7 MB            | 1.8 GB         | ~1.7 GB                  |
| Options (30 underlyings, constrained) | ~5M      | 600 MB          | 151 GB         | **~100 GB**              |
| Crypto                                | 4,320    | 0.2 MB          | 50 MB          | ~50 MB                   |
| **Total**                             |          | **~611 MB/day** | **~154 GB/yr** | **~103 GB for 10yr**     |

Note: Options dominate at 97% of storage. With Parquet ZSTD compression (10-15x for repetitive OHLCV data), 10 years fits in ~103 GB — comfortably on a single 256 GB SSD.

### 5.3 Parquet Partitioning

```
{DATA_ROOT}/parquet/
├── stocks/
│   ├── AAPL/
│   │   ├── 2024/
│   │   │   ├── 01.parquet
│   │   │   ├── 02.parquet
│   │   │   └── ...
│   │   └── 2025/
│   └── MSFT/
├── indexes/
│   └── SPX/
├── futures/
│   └── ES/
├── options/
│   └── AAPL/          # Options on AAPL
│       ├── 2024/
│       │   ├── 01.parquet   # All AAPL option contracts for Jan 2024
│       │   └── ...
│       └── 2025/
└── crypto/
    └── BTC/
```

Each Parquet file contains all minute bars for one symbol for one month. Files are append-only and immutable after the month closes.

### 5.4 Data Flow

```
HISTORICAL BACKFILL                     ONGOING INGESTION
─────────────────                       ─────────────────

Polygon flat files    ──► Python        Polygon WebSocket  ──► Data Ingestion
Databento bulk download   ingestion     Databento live feed     Service (cron)
                          script                                    │
                            │                                       │
                            ▼                                       ▼
                     {DATA_ROOT}/parquet/ (local SSD)              Micro-batch
                            │                              to Parquet (every 5 min)
                            │                                       │
                            ├───────────────────────────────────────┘
                            │
                  ┌─────────┼─────────┐
                  │         │         │
                  ▼         ▼         ▼
           NautilusTrader  DuckDB   Azure Blob
           BacktestEngine  (dashboard) (backup)
```

### 5.5 Why NOT IB for Historical Data

| Limitation                          | Impact                                    |
| ----------------------------------- | ----------------------------------------- |
| Max 60 requests / 10 min (pacing)   | 5,000 stocks = ~83 hours to download      |
| ~2 years of minute data for futures | Insufficient for backtesting              |
| Expired options data deleted        | Impossible to backtest historical options |
| IB says "we are not a data vendor"  | Unreliable for systematic data collection |

IB is used for: execution, real-time quotes during live trading, and small gap-fills only.

---

## 6. Strategy System

### 6.1 Strategy as Python Files

Strategies are NautilusTrader `Strategy` subclasses stored in a strategies directory:

```
/strategies/
├── momentum/
│   ├── __init__.py
│   ├── ema_cross.py         # EMACrossStrategy(Strategy)
│   └── config.py            # EMACrossConfig(StrategyConfig)
├── mean_reversion/
│   ├── __init__.py
│   ├── pairs_trade.py
│   └── config.py
└── options/
    ├── __init__.py
    ├── covered_call.py
    └── config.py
```

### 6.2 Strategy Lifecycle

```
  AUTHOR          BACKTEST          REVIEW           DEPLOY          MONITOR
  ──────          ────────          ──────           ──────          ───────

Write .py    →  Run backtest   →  Review         →  Start live   →  Dashboard
file            via UI/CLI        tearsheet         paper trading    shows P&L,
                                  + metrics         via UI/CLI       positions,
                                                                     trades
                                                 →  Start live
                                                    real money
                                                    (Phase 2)
```

### 6.3 Strategy Execution Safety

Per Codex review — untrusted strategy code can crash workers:

- Strategies run in **isolated processes** (not in the FastAPI process)
- **Timeouts**: Backtest jobs killed after configurable max duration
- **Resource limits**: Docker cgroup limits on CPU and memory
- **Typed config**: All strategy parameters validated via Pydantic before execution
- **Audit trail**: Every strategy start/stop/modify logged to PostgreSQL

---

## 7. Frontend Design

### 7.1 Pages

| Page                 | Description                                                                                                                 |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Login**            | Azure Entra ID redirect                                                                                                     |
| **Dashboard**        | Portfolio overview: total value, daily P&L, active strategies, recent trades                                                |
| **Strategies**       | List registered strategies (from local `strategies/` dir). Edit configs. View backtest history. No file uploads in Phase 1. |
| **Backtest**         | Run backtest: select strategy + parameters + date range + instruments. View results.                                        |
| **Backtest Results** | Interactive charts: equity curve, drawdown, monthly returns heatmap. Download QuantStats tearsheet. Trade log table.        |
| **Live Trading**     | Active strategies with status. Start/stop controls. Real-time P&L and positions.                                            |
| **Market Data**      | Price charts (candlestick) for tracked instruments. Powered by TradingView Lightweight Charts.                              |
| **Data Management**  | Data ingestion status. Storage usage. Trigger manual data downloads.                                                        |
| **Settings**         | Account settings, API keys, notification preferences.                                                                       |

### 7.2 Chart Libraries

| Use Case                          | Library                        | Why                                              |
| --------------------------------- | ------------------------------ | ------------------------------------------------ |
| Candlestick/price charts          | TradingView Lightweight Charts | Industry standard, fast, small bundle, free      |
| Equity curves, drawdown, heatmaps | Recharts                       | React-native, composable, good for dashboards    |
| Downloadable full reports         | QuantStats HTML                | Generate server-side, serve as downloadable file |

### 7.3 Real-time Updates

- **WebSocket** from FastAPI to Next.js for live trading updates (positions, P&L, trade executions)
- **Polling** (30s interval) for backtest job status
- No SSE needed for the current frequency (5-10 min trading)

---

## 8. Backend API Design

### 8.1 API Structure

```
/api/v1/
├── auth/
│   ├── GET    /me             # Current user from JWT (frontend handles MSAL login)
│   └── POST   /logout         # Invalidate session
│
├── strategies/
│   ├── GET    /               # List registered strategies (from local dir)
│   ├── GET    /{id}           # Get strategy detail + config schema
│   ├── PATCH  /{id}           # Update strategy config
│   ├── DELETE /{id}           # Unregister strategy
│   └── POST   /{id}/validate # Validate strategy loads + config parses (syntax + type check)
│
├── backtests/
│   ├── POST   /run            # Start backtest (returns job_id)
│   ├── GET    /{job_id}/status  # Poll job status
│   ├── GET    /{job_id}/results # Get results (metrics + trade log)
│   ├── GET    /{job_id}/report  # Download QuantStats HTML tearsheet
│   └── GET    /history        # List past backtests
│
├── live/
│   ├── POST   /start          # Deploy strategy to live/paper trading
│   ├── POST   /stop           # Stop a running strategy
│   ├── POST   /kill-all       # Emergency stop ALL running strategies
│   ├── GET    /status         # All running strategies + status
│   ├── GET    /positions      # Current open positions
│   ├── GET    /trades         # Recent trade executions
│   └── WS    /stream         # WebSocket: real-time updates (first-message auth)
│
├── market-data/
│   ├── GET    /bars/{symbol}  # Historical bars (DuckDB → Parquet)
│   ├── GET    /symbols        # Available symbols
│   ├── GET    /status         # Data ingestion health
│   └── POST   /ingest        # Trigger manual data ingestion (enqueues arq job)
│
└── account/
    ├── GET    /summary        # IB account summary (balance, margin, etc.)
    ├── GET    /portfolio      # IB portfolio positions
    └── GET    /health         # IB Gateway connection health status
```

### 8.2 Backtest Job Flow

```
Client POST /api/v1/backtests/run
    │
    ▼
FastAPI creates job record in PostgreSQL (status: "pending")
    │
    ▼
Enqueues job to Redis via arq (always-on worker pool picks it up)
    │
    ▼
Worker: NautilusTrader BacktestEngine
    ├── Reads Parquet from {DATA_ROOT}/parquet/
    ├── Runs strategy
    ├── Updates PostgreSQL (status: "running", progress %)
    │
    ▼
Worker complete:
    ├── Saves results to PostgreSQL (metrics, trade log)
    ├── Generates QuantStats HTML report → saves to {DATA_ROOT}/reports/
    ├── Updates PostgreSQL (status: "completed")
    │
    ▼
Client polls GET /api/v1/backtests/{job_id}/status
Client fetches GET /api/v1/backtests/{job_id}/results
```

---

## 9. Database Schema (PostgreSQL)

### Core Tables

```sql
-- Users (synced from Entra ID)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entra_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'viewer',  -- 'admin', 'trader', 'viewer'
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Strategy definitions
CREATE TABLE strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    file_path VARCHAR(500) NOT NULL,        -- path to .py file
    strategy_class VARCHAR(255) NOT NULL,   -- e.g. "EMACrossStrategy"
    config_schema JSONB,                     -- Pydantic schema as JSON
    default_config JSONB,                    -- default parameter values
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Backtest jobs (with strategy version for reproducibility)
CREATE TABLE backtests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id),
    strategy_code_hash VARCHAR(64) NOT NULL, -- SHA256 of strategy .py file at run time
    strategy_git_sha VARCHAR(40),            -- git commit SHA (if available)
    config JSONB NOT NULL,                   -- parameters used
    instruments TEXT[] NOT NULL,              -- e.g. {'AAPL', 'MSFT'}
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',    -- pending, running, completed, failed
    progress SMALLINT DEFAULT 0,             -- 0-100
    metrics JSONB,                           -- Sharpe, drawdown, return, etc.
    report_path VARCHAR(500),                -- path to QuantStats HTML
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Live strategy deployments (MUST be created before trades — FK dependency)
CREATE TABLE live_deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID REFERENCES strategies(id),
    strategy_code_hash VARCHAR(64) NOT NULL, -- SHA256 of strategy .py file at deploy time
    strategy_git_sha VARCHAR(40),            -- git commit SHA (if available)
    config JSONB NOT NULL,
    instruments TEXT[] NOT NULL,
    status VARCHAR(50) DEFAULT 'stopped',   -- stopped, starting, running, error
    paper_trading BOOLEAN DEFAULT TRUE,
    started_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    started_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Trade log (both backtest and live)
CREATE TABLE trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    backtest_id UUID REFERENCES backtests(id),       -- NULL for live trades
    deployment_id UUID REFERENCES live_deployments(id), -- NULL for backtest trades
    strategy_id UUID REFERENCES strategies(id) NOT NULL, -- strong FK, not text name
    strategy_code_hash VARCHAR(64) NOT NULL,          -- version traceability
    instrument VARCHAR(100) NOT NULL,
    side VARCHAR(10) NOT NULL,              -- BUY, SELL
    quantity DECIMAL(18,8) NOT NULL,
    price DECIMAL(18,8) NOT NULL,
    commission DECIMAL(18,8),
    pnl DECIMAL(18,8),
    is_live BOOLEAN DEFAULT FALSE,
    executed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    -- Exactly one of backtest_id or deployment_id must be set
    CONSTRAINT chk_trades_source CHECK (
        (backtest_id IS NOT NULL AND deployment_id IS NULL)
        OR (backtest_id IS NULL AND deployment_id IS NOT NULL)
    )
);
CREATE INDEX idx_trades_backtest ON trades(backtest_id);
CREATE INDEX idx_trades_deployment ON trades(deployment_id);
CREATE INDEX idx_trades_strategy ON trades(strategy_id);
CREATE INDEX idx_trades_executed ON trades(executed_at);
CREATE INDEX idx_trades_instrument ON trades(instrument);

-- Daily strategy performance snapshots (live deployments only)
-- Backtest attribution uses backtests.metrics JSONB instead.
CREATE TABLE strategy_daily_pnl (
    id BIGSERIAL PRIMARY KEY,
    strategy_id UUID REFERENCES strategies(id) NOT NULL,
    deployment_id UUID REFERENCES live_deployments(id) NOT NULL, -- live only
    date DATE NOT NULL,
    pnl DECIMAL(18,2) NOT NULL,             -- daily P&L in account currency
    cumulative_pnl DECIMAL(18,2) NOT NULL,  -- running total
    capital_used DECIMAL(18,2),             -- capital allocated to this strategy
    num_trades INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    max_drawdown DECIMAL(18,8),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(strategy_id, deployment_id, date)
);
CREATE INDEX idx_daily_pnl_strategy ON strategy_daily_pnl(strategy_id, date);

-- Audit log
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    action VARCHAR(100) NOT NULL,           -- e.g. 'strategy.start', 'backtest.run'
    resource_type VARCHAR(50),
    resource_id UUID,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_audit_created ON audit_log(created_at);
```

---

## 10. Deployment

### 10.1 Phase 1 — Development + Paper Trading

Single Azure VM (D4s_v5: 4 vCPU, 16 GB RAM, ~$140/mo) or D8s_v5 (8 vCPU, 32 GB RAM, ~$280/mo).

All services in Docker Compose. Acceptable for paper trading — no real money at risk.

### 10.2 Phase 2 — Real Money Trading

Split into 2 VMs across Azure Availability Zones:

| VM                 | Services                                                           | Purpose                                                           | Criticality                     |
| ------------------ | ------------------------------------------------------------------ | ----------------------------------------------------------------- | ------------------------------- |
| **VM-A (Trading)** | IB Gateway, NautilusTrader TradingNode, FastAPI, PostgreSQL, Redis | Critical trading path + state. Must be always on.                 | HIGH — downtime = blind trading |
| **VM-B (Compute)** | Backtest workers, data ingestion, Next.js frontend                 | Non-critical compute. Can go down without affecting live trading. | MEDIUM                          |

Note: PostgreSQL and Redis are on VM-A because risk checks, audit logging, and operational visibility require database access during trading. If VM-B goes down, you lose the dashboard and can't run backtests, but live trading continues safely.

Active/passive failover: VM-A monitored with Azure alerts. Recovery runbook documented and tested.

### 10.3 Cost Estimate

| Item                        | Phase 1          | Phase 2          |
| --------------------------- | ---------------- | ---------------- |
| Azure VM(s)                 | $140-280/mo      | $280-560/mo      |
| Azure Blob Storage (50 GB)  | $1/mo            | $1/mo            |
| Azure Key Vault             | $5/mo            | $5/mo            |
| Polygon.io (stocks/options) | $29-79/mo        | $29-79/mo        |
| Databento (futures)         | $179/mo          | $179/mo          |
| Domain + SSL                | $15/mo           | $15/mo           |
| **Total**                   | **~$370-560/mo** | **~$510-840/mo** |

---

## 11. Security

- **Auth**: Azure Entra ID with PKCE flow. Frontend acquires tokens via MSAL.js. Backend validates JWT signature against Entra OIDC JWKS using PyJWT (no MSAL on backend).
- **Secrets**: Azure Key Vault. No secrets in code, Docker images, or environment files.
- **IB Gateway**: Runs on localhost only (not exposed to internet). API access via FastAPI only.
- **HTTPS**: All external traffic over TLS. Let's Encrypt or Azure-managed cert.
- **Strategy isolation**: Strategies run in isolated Docker processes with resource limits.
- **Audit trail**: All actions logged to `audit_log` table.
- **Database**: PostgreSQL with PITR backups. Trade/event tables partitioned by month.

---

## 12. Risk Controls

Even at 5-10 minute frequency, trading real money needs guardrails:

| Control                   | Implementation                                                        |
| ------------------------- | --------------------------------------------------------------------- |
| **Max position size**     | Configurable per strategy. Enforced in NautilusTrader RiskEngine.     |
| **Max daily loss**        | Hard stop: if daily P&L < threshold, stop all strategies.             |
| **Max notional exposure** | Total portfolio notional limit.                                       |
| **Broker-side stops**     | OCO/stop orders placed at broker level (survive system failure).      |
| **Kill switch**           | One-click "stop all" in dashboard + CLI.                              |
| **Alerting**              | Email/SMS on: strategy error, large loss, system down, IB disconnect. |

---

## 13. Key Risks and Mitigations

| Risk                                   | Likelihood | Impact | Mitigation                                                         |
| -------------------------------------- | ---------- | ------ | ------------------------------------------------------------------ |
| Single VM failure during live trading  | Medium     | High   | Phase 2: 2-VM split. Broker-side stops survive.                    |
| NautilusTrader breaking changes        | Medium     | Medium | Pin version. Test upgrades on paper before live.                   |
| IB Gateway disconnection               | High       | Medium | Auto-reconnect logic. IBProbe health monitor (like v1). Alerts.    |
| Parquet data corruption                | Low        | High   | Append-only writes. Checksums. Azure Blob backup. Restore runbook. |
| Strategy bug causing large loss        | Medium     | High   | Paper trading first. Position limits. Max daily loss. Kill switch. |
| Data source outage (Polygon/Databento) | Low        | Medium | IB as fallback for real-time. Parquet has historical.              |

---

## 14. Non-Functional Requirements

| Requirement         | Target                                                                 |
| ------------------- | ---------------------------------------------------------------------- |
| Backtest speed      | 1 year of minute data for 1 symbol < 30 seconds                        |
| Dashboard load time | < 2 seconds for any page                                               |
| Live data latency   | < 5 seconds from market to dashboard (acceptable for 5-min strategies) |
| Uptime (Phase 2)    | 99.9% for trading VM                                                   |
| Data integrity      | Zero data loss. Append-only Parquet + Azure Blob backup.               |
| Recovery time       | < 15 minutes to restore from VM failure (documented runbook)           |

---

## 15. Decisions Log

| #   | Decision            | Choice                             | Alternatives Considered                         | Rationale                                                                                        |
| --- | ------------------- | ---------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 1   | Backtest engine     | NautilusTrader                     | VectorBT, Backtrader, PyBroker, custom ib_async | Only option with backtest/live parity + native IB adapter. Codex confirmed.                      |
| 2   | Market data storage | Parquet + DuckDB                   | TimescaleDB, QuestDB, ClickHouse, ArcticDB      | NautilusTrader reads Parquet natively. ~103 GB compressed for 10yr — fits on single SSD.         |
| 3   | Frontend charts     | TradingView Lightweight + Recharts | Matplotlib PNGs, D3.js                          | Native React. No server-side image generation.                                                   |
| 4   | Backtest reports    | QuantStats HTML                    | pyfolio, custom                                 | pyfolio dead (last release 2019). QuantStats actively maintained.                                |
| 5   | Deployment          | Docker Compose → 2-VM split        | AKS, Container Apps                             | AKS is overhead for single-user. Compose now, split for real money.                              |
| 6   | Auth                | Azure Entra ID                     | JWT email/password, magic link                  | Already have Azure tenant. SSO for small group.                                                  |
| 7   | Data sources        | Polygon + Databento                | IB only, FirstRate Data                         | IB too slow/limited for historical. Polygon best for stocks/options. Databento best for futures. |
| 8   | Real-time DB        | None (Phase 1)                     | QuestDB, ClickHouse                             | ~103 GB compressed. Parquet + DuckDB sufficient. Add later if needed.                            |

---

## 16. Open Questions

- [ ] Databento vs Polygon.io for options data — run 30-day cost/quality bakeoff before committing
- [ ] ThetaData as alternative options data source — evaluate pricing
- [ ] Exact NautilusTrader version to pin (latest stable as of build start)
- [ ] IB Gateway Docker image choice (waytrade vs unusualalpha — v1 used both)
- [ ] QuantStats compatibility with NautilusTrader output format

---

## 17. References

- [NautilusTrader Docs](https://nautilustrader.io/docs/latest/)
- [NautilusTrader IB Integration](https://nautilustrader.io/docs/latest/integrations/ib/)
- [NautilusTrader Parquet Catalog](https://nautilustrader.io/docs/nightly/concepts/data/)
- [ib_async GitHub](https://github.com/ib-api-reloaded/ib_async)
- [Polygon.io Pricing](https://polygon.io/pricing)
- [Databento Pricing](https://databento.com/pricing)
- [QuantStats GitHub](https://github.com/ranaroussi/quantstats)
- [TradingView Lightweight Charts](https://github.com/nicenathapong/lightweight-charts)
- [MSAI v1 Repo](https://github.com/marketsignal/msai) (private)
- [Research Links](../research/trading-research-links.md)
