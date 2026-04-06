# MSAI v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a personal hedge fund platform with NautilusTrader backtesting/live trading, FastAPI backend, Next.js dashboard, and IB Gateway integration.

**Architecture:** Docker Compose with 6 containers on a single Azure VM (postgres, redis, backend, backtest-worker, frontend, ib-gateway). TradingNode runs as a subprocess spawned by backend. Data ingestion runs as arq jobs in the backtest-worker. FastAPI orchestrates via Redis + arq. Parquet for market data, PostgreSQL for app state, DuckDB for dashboard queries. Next.js frontend with TradingView charts.

**Tech Stack:** Python 3.12, FastAPI, NautilusTrader, PostgreSQL 16, Redis, arq, DuckDB, Next.js 15, React, shadcn/ui, Tailwind CSS, Docker Compose, Azure Entra ID, Polygon.io, Databento.

**Design Doc:** `docs/plans/2026-02-25-msai-v2-design.md`

**Review History:**

- 2026-02-25: Codex pass 1 identified 4 critical + 6 high issues. All addressed in revision 2.
- 2026-02-25: Codex pass 2 confirmed 9/14 fixed, found 4 new issues (CI install, CI ordering, Azure deps, retry inconsistency). Fixed in revision 3.
- 2026-02-25: External reviewer found 7 issues (versioning, attribution, schema). Fixed in design doc revision 4.
- 2026-02-25: Codex pass 3 found 4 final issues (DDL order, daily_pnl keying, impl drift, auth contract). All fixed.
- 2026-02-25: External reviewer pass 2 found 6 issues (DDL confirmed, data math, worker model, service count, CI ordering). All fixed.
- 2026-02-25: Codex pass 4 (final gate) found 1 blocker: DATA_ROOT path inconsistency. Fixed with canonical `DATA_ROOT` env var.
- 2026-02-25: External reviewer pass 3 found 9 issues (placeholder tasks, dep pinning, CI ordering, testcontainers vs services, schema drift, logging, WebSocket timeout). All fixed in revision 6 — all placeholder tasks expanded.
- 2026-02-25: Codex pass 5 found 5 blockers: Dockerfile --no-install-project, kill-all/ingest/logout endpoints, CLI entrypoint. Fixed.
- 2026-02-25: External reviewer pass 4 found 4 issues: Task ordering, commit conflicts, latest refs, subprocess guidance. Fixed.
- 2026-02-25: Codex pass 6 found 5 blockers: IB port exposure, Docker socket privilege, Redis hardcoded host, missing ingestion worker function, DuckDB read_only invalid. All fixed. **LGTM — ready to build.**

---

## Milestones Overview

| #   | Milestone             | Tasks | What You Get                                                                                                                       |
| --- | --------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------- |
| 0   | Foundation & Safety   | 1-7   | Dep pins + lockfile, CI workflow definition, structured logging, secrets abstraction, job queue, JWT auth, data integrity contract |
| 1   | Project Scaffold      | 8-12  | Monorepo, Docker Compose (with Redis), dev environment, FastAPI skeleton, CI pipeline                                              |
| 2   | Backend Core          | 13-19 | DB models, migrations, auth middleware, strategy registry, backtest API stubs                                                      |
| 3   | Data Layer            | 20-25 | Atomic Parquet store, DuckDB queries, Polygon/Databento clients, ingestion CLI                                                     |
| 4   | Backtesting Engine    | 26-31 | NautilusTrader integration, arq backtest worker, QuantStats reports, E2E test                                                      |
| 5   | Frontend Dashboard    | 32-39 | Next.js app, all pages, charts, auth flow                                                                                          |
| 6   | Operational Readiness | 40-42 | Runbooks, DR drills, deployment scripts, monitoring alerts                                                                         |
| 7   | Live Trading          | 43-49 | Risk engine, IB Gateway, TradingNode, live API, WebSocket, health monitor                                                          |

---

## Milestone 0: Foundation & Safety

> Per Codex review: "Not yet solid enough for 24/7 real money. Add foundation tasks before feature tasks."

### Task 1: Pin dependency versions + generate lockfile

**Files:**

- Create: `backend/pyproject.toml`
- Create: `backend/uv.lock` (auto-generated)

**Step 1: Create pyproject.toml with minimum version bounds (exact pins are in uv.lock)**

```toml
[project]
name = "msai"
version = "0.1.0"
description = "MSAI v2 - Personal Hedge Fund Platform"
requires-python = ">=3.12,<3.14"
dependencies = [
    # Web framework
    "fastapi>=0.133.0",
    "uvicorn[standard]>=0.32.0",
    # Database
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    # Validation & config
    "pydantic>=2.10.0",
    "pydantic-settings>=2.7.0",
    # Market data storage & queries
    "duckdb>=1.4.0",
    "pyarrow>=22.0.0",
    "pandas>=2.2.0",
    # Auth (JWT validation, NOT MSAL — MSAL is frontend only)
    "PyJWT[crypto]>=2.9.0",
    "cryptography>=43.0.0",
    # HTTP client
    "httpx>=0.28.0",
    # Job queue
    "arq>=0.26.0",
    "redis>=5.2.0",
    # Trading engine
    "nautilus_trader>=1.222.0",
    # Backtesting reports
    "quantstats>=0.0.81",
    # IB connectivity (escape hatch)
    "ib_async>=1.0.0",
    # Data sources
    "databento>=0.43.0",
    # Logging
    "structlog>=24.4.0",
    # CLI
    "typer>=0.15.0",
]

[project.scripts]
msai = "msai.cli:app"

[project.optional-dependencies]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=6.0.0",
    "testcontainers[postgres,redis]>=4.9.0",
    "ruff>=0.8.0",
    "mypy>=1.13.0",
]
azure = [
    "azure-identity>=1.19.0",
    "azure-keyvault-secrets>=4.9.0",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM", "TCH"]

[tool.mypy]
python_version = "3.12"
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Generate lockfile**

```bash
cd backend && uv lock
```

**Step 3: Verify all deps install cleanly on Python 3.12**

```bash
cd backend && uv sync && uv run python -c "
import fastapi, duckdb, pyarrow, structlog, arq
print('All core deps OK')
"
```

**Step 4: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock
git commit -m "chore: pin exact dependency versions with uv lockfile"
```

---

### Task 2: Define CI workflow file (written here, committed in Task 9)

**Files:**

- Create: `.github/workflows/ci.yml` (committed in Task 9 alongside frontend, so both lockfiles exist)

**Codex pass 2 fixes applied:**

- `uv sync --frozen --extra dev` to install dev deps only (ruff, mypy, pytest) — NOT `--all-extras` which would pull Azure SDK
- Frontend job conditional on lockfile existing

**Step 1: Create CI workflow**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  backend:
    runs-on: ubuntu-24.04
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: msai_test
          POSTGRES_USER: msai
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-timeout 3s
          --health-retries 5
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4.2.2
      - uses: astral-sh/setup-uv@v4.3.0
        with:
          version: "0.6.0"
      - run: cd backend && uv sync --frozen --extra dev
      - run: cd backend && uv run ruff check src/
      - run: cd backend && uv run mypy src/ --strict
      - run: cd backend && uv run pytest tests/ -v --cov=msai
        env:
          DATABASE_URL: postgresql+asyncpg://msai:test@localhost:5432/msai_test
          REDIS_URL: redis://localhost:6379
          ENVIRONMENT: test

  frontend:
    runs-on: ubuntu-24.04
    if: hashFiles('frontend/pnpm-lock.yaml') != ''
    steps:
      - uses: actions/checkout@v4.2.2
      - uses: pnpm/action-setup@v4.1.0
        with:
          version: "9.15.0"
      - uses: actions/setup-node@v4.2.0
        with:
          node-version: 22
          cache: pnpm
          cache-dependency-path: frontend/pnpm-lock.yaml
      - run: cd frontend && pnpm install --frozen-lockfile
      - run: cd frontend && pnpm lint
      - run: cd frontend && pnpm build
```

**Step 2:** Do NOT commit yet. The `.github/workflows/ci.yml` file is committed in Task 9 alongside the frontend (both lockfiles must exist for CI to work).

---

### Task 3: Create structured logging foundation

**Files:**

- Create: `backend/src/msai/core/logging.py`
- Test: `backend/tests/unit/test_logging.py`

**Why:** Codex flagged missing structured logging. For a 24/7 trading system, every action must be traceable.

**Implementation:**

- Use `structlog` with JSON output in production, pretty console in dev
- `get_logger(name)` → bound logger with request_id, user_id context
- FastAPI middleware that injects `request_id` into every request's log context
- Log levels: DEBUG (dev), INFO (prod), ERROR (alerts)

```python
# backend/src/msai/core/logging.py
import structlog

def setup_logging(environment: str) -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if environment == "development":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    log_level = 10 if environment == "development" else 20  # DEBUG=10, INFO=20
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
```

---

### Task 4: Create secrets provider abstraction

**Files:**

- Create: `backend/src/msai/core/secrets.py`
- Test: `backend/tests/unit/test_secrets.py`

**Why:** Codex flagged design says Key Vault but plan hardcodes secrets. Abstract from day 1.

**Implementation:**

- `SecretsProvider` protocol with `get(key) -> str`
- `EnvSecretsProvider` — reads from environment variables (dev)
- `AzureKeyVaultProvider` — reads from Azure Key Vault (production)
- `Settings` class uses the provider, not direct env vars for secrets

```python
# backend/src/msai/core/secrets.py
from typing import Protocol

class SecretsProvider(Protocol):
    def get(self, key: str) -> str: ...

class EnvSecretsProvider:
    """Dev: reads from environment variables."""
    def get(self, key: str) -> str:
        import os
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Secret '{key}' not found in environment")
        return value

class AzureKeyVaultProvider:
    """Prod: reads from Azure Key Vault."""
    def __init__(self, vault_url: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        self._client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())

    def get(self, key: str) -> str:
        return self._client.get_secret(key).value
```

Config uses it:

```python
# In Settings
database_url: str  # populated from secrets_provider.get("DATABASE_URL")
```

---

### Task 5: Create JWT validation middleware (NOT MSAL)

**Files:**

- Create: `backend/src/msai/core/auth.py`
- Test: `backend/tests/unit/test_jwt_validation.py`

**Why:** Codex CRITICAL #3 — MSAL is for token acquisition (frontend), not validation (backend). Backend must validate JWT signature against Entra OIDC metadata.

**Implementation:**

- Fetch OIDC metadata from `https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration`
- Cache JWKS (JSON Web Key Set) from the `jwks_uri`
- Validate JWT: signature, issuer, audience, expiry, required claims
- Use `PyJWT` with `cryptography` backend (NOT `python-jose`, NOT `msal`)
- `get_current_user()` FastAPI dependency

```python
# backend/src/msai/core/auth.py
import jwt
from jwt import PyJWKClient

class EntraIDValidator:
    def __init__(self, tenant_id: str, client_id: str) -> None:
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._audience = client_id
        self._jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        )

    def validate_token(self, token: str) -> dict:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
```

Test with mocked JWKS endpoint returning test RSA keys.

---

### Task 6: Set up Redis + arq job queue

**Files:**

- Create: `backend/src/msai/core/queue.py`
- Test: `backend/tests/unit/test_queue.py`

**Why:** Codex CRITICAL #1 — `multiprocessing.Process` risks orphaned jobs, no retry, no dead-letter. Use proper job queue.

**Implementation:**

- `arq` (lightweight async job queue backed by Redis)
- `enqueue_backtest(job_params)` — enqueue backtest job
- Worker settings: max retries=1 (backtests are expensive), job timeout configurable, dead-letter logging
- Redis also serves as WebSocket pub/sub transport (fixes Codex HIGH #9)

```python
# backend/src/msai/core/queue.py
from arq import create_pool
from arq.connections import RedisSettings
from msai.core.config import settings

def _parse_redis_url(url: str) -> RedisSettings:
    """Parse REDIS_URL env var into arq RedisSettings. Supports redis://host:port format."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return RedisSettings(host=parsed.hostname or "localhost", port=parsed.port or 6379)

async def get_redis_pool():
    return await create_pool(_parse_redis_url(settings.redis_url))

async def enqueue_backtest(pool, backtest_id: str, strategy_path: str, config: dict):
    await pool.enqueue_job(
        "run_backtest",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
        config=config,
    )
```

---

### Task 7: Define data integrity contract for Parquet writes

**Files:**

- Create: `backend/src/msai/core/data_integrity.py`
- Test: `backend/tests/unit/test_data_integrity.py`

**Why:** Codex HIGH #8 — append without atomicity/idempotency risks corruption.

**Implementation:**

- Atomic writes: write to temp file, then `os.rename()` (atomic on same filesystem)
- Dedup keys: each bar row has `(symbol, timestamp)` as natural key; dedup on write
- Gap detection: after ingestion, check for missing minutes in expected trading hours
- Checksum: store SHA256 of each Parquet file in a manifest for backup verification

```python
# backend/src/msai/core/data_integrity.py
import hashlib
import os
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

def atomic_write_parquet(table, target_path: Path) -> str:
    """Write Parquet atomically. Returns SHA256 checksum."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".parquet.tmp")
    try:
        os.close(fd)
        pq.write_table(table, tmp_path, compression="zstd")
        checksum = hashlib.sha256(Path(tmp_path).read_bytes()).hexdigest()
        os.rename(tmp_path, target_path)  # Atomic on same filesystem
        return checksum
    except BaseException:
        os.unlink(tmp_path)
        raise

def dedup_bars(df, key_columns=("symbol", "timestamp")):
    """Remove duplicate bars by natural key, keeping last."""
    return df.drop_duplicates(subset=list(key_columns), keep="last")
```

---

## Milestone 1: Project Scaffold

### Task 8: Initialize monorepo structure

**Files:**

- Create: directory structure, `.gitignore`, `.python-version`

**Step 1: Create directories**

```bash
mkdir -p backend/src/msai/{api,core,models,services,workers}
mkdir -p backend/tests/{unit,integration}
mkdir -p frontend
mkdir -p strategies/example
mkdir -p data/parquet data/reports
mkdir -p scripts
```

**Step 2: Create .python-version**

```
3.12
```

**Step 3: Create .gitignore** (Python, Node, Data, IDE, Docker, OS entries — same as before)

**Step 4: Install deps and verify**

```bash
cd backend && uv sync && uv run python -c "import fastapi; print('OK')"
```

**Step 5: Commit**

```bash
git add backend/ strategies/ data/.gitkeep scripts/ .gitignore .python-version
git commit -m "chore: initialize monorepo structure"
```

---

### Task 9: Initialize Next.js frontend

**Files:**

- Create: `frontend/` (via create-next-app)

**Step 1: Create Next.js app (note: --src-dir puts code in frontend/src/)**

```bash
cd frontend && pnpm create next-app@15 . --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --use-pnpm
```

**Step 2: Install shadcn/ui and chart deps**

```bash
cd frontend && pnpm dlx shadcn@2 init -d
cd frontend && pnpm add lightweight-charts recharts @azure/msal-browser @azure/msal-react
```

Note: `@azure/msal-browser` + `@azure/msal-react` for frontend Entra ID login. Backend does NOT use MSAL.

**Step 3: Verify and commit**

```bash
cd frontend && pnpm dev  # Should start on localhost:3000
git add frontend/ .github/
git commit -m "chore: initialize Next.js 15 frontend with shadcn/ui, charts, MSAL, and CI pipeline"
```

---

### Task 10: Create Docker Compose dev environment

**Files:**

- Create: `docker-compose.dev.yml`
- Create: `backend/Dockerfile.dev`
- Create: `frontend/Dockerfile.dev`

**Key fixes from Codex review:**

- Add Redis service (for arq job queue + WebSocket pub/sub)
- Fix frontend volume mounts for `--src-dir` (code lives in `frontend/src/`)
- Fix backend uvicorn command to find `msai` package correctly

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: msai
      POSTGRES_USER: msai
      POSTGRES_PASSWORD: msai_dev_password
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U msai"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile.dev
    ports:
      - "8000:8000"
    volumes:
      - ./backend/src:/app/src:ro
      - ./strategies:/app/strategies:ro
      - ./data:/app/data
    environment:
      DATABASE_URL: postgresql+asyncpg://msai:msai_dev_password@postgres:5432/msai
      REDIS_URL: redis://redis:6379
      DATA_ROOT: /app/data
      ENVIRONMENT: development
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  backtest-worker:
    build:
      context: ./backend
      dockerfile: Dockerfile.dev
    command: ["uv", "run", "arq", "msai.workers.settings.WorkerSettings"]
    volumes:
      - ./backend/src:/app/src:ro
      - ./strategies:/app/strategies:ro
      - ./data:/app/data
    environment:
      DATABASE_URL: postgresql+asyncpg://msai:msai_dev_password@postgres:5432/msai
      REDIS_URL: redis://redis:6379
      DATA_ROOT: /app/data
      ENVIRONMENT: development
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile.dev
    ports:
      - "3000:3000"
    volumes:
      - ./frontend/src:/app/src:ro
      - ./frontend/public:/app/public:ro
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000

volumes:
  postgres_data:
```

Backend Dockerfile.dev — fix package discovery:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.6.0 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# --no-install-project: install deps only, not the project itself (source mounted later as volume)
RUN uv sync --frozen --no-install-project
# src/ mounted as volume at runtime; PYTHONPATH ensures msai package is importable
ENV PYTHONPATH=/app/src
CMD ["uv", "run", "uvicorn", "msai.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]
```

---

### Task 11: Create FastAPI app skeleton with health + readiness probes

**Files:**

- Create: `backend/src/msai/main.py`
- Create: `backend/src/msai/core/config.py`
- Test: `backend/tests/unit/test_health.py`

Beyond basic `/health`, add `/ready` that checks PostgreSQL + Redis connectivity:

```python
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "environment": settings.environment}

@app.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    await db.execute(text("SELECT 1"))
    redis = await get_redis_pool()
    await redis.ping()
    return {"status": "ready"}
```

---

### Task 12: Set up Alembic for database migrations

**Files:**

- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/src/msai/models/base.py`

Initialize Alembic with async PostgreSQL:

1. `uv run alembic init alembic`
2. Create `Base(DeclarativeBase)` and `TimestampMixin` (created_at, updated_at) in `models/base.py`
3. Configure `alembic/env.py` for async: use `run_async_migrations()` pattern from Alembic docs, import `Base.metadata`, read `DATABASE_URL` from settings
4. Verify: `uv run alembic check` succeeds (no migration generated yet — models are created in Task 13)

Note: Do NOT run `alembic revision --autogenerate` here. That happens in Task 13 after all models are defined, producing a single migration for all tables.

---

## Milestone 2: Backend Core

### Task 13: Create all database models + migration

**Files:**

- Create: `backend/src/msai/models/user.py`
- Create: `backend/src/msai/models/strategy.py`
- Create: `backend/src/msai/models/backtest.py`
- Create: `backend/src/msai/models/trade.py`
- Create: `backend/src/msai/models/live_deployment.py`
- Create: `backend/src/msai/models/audit_log.py`
- Test: `backend/tests/integration/test_models.py`

Create all tables per design doc Section 9 in a single migration.

**Test infrastructure strategy:**

- **Local dev (`pytest`):** Uses `testcontainers` to spin up ephemeral PostgreSQL + Redis containers per test session. No external deps needed.
- **CI (GitHub Actions):** Uses pre-provisioned service containers (postgres/redis in workflow). Tests connect via `DATABASE_URL`/`REDIS_URL` env vars.
- **Both use real PostgreSQL** (not SQLite — Codex HIGH #10). The `conftest.py` detects which mode via env vars.

**Important schema notes (from Codex pass 3):**

- `live_deployments` must be created BEFORE `trades` (FK dependency)
- `trades` has CHECK constraint: exactly one of `backtest_id` or `deployment_id` must be set
- `strategy_code_hash` (SHA256) on `backtests`, `trades`, and `live_deployments` — compute hash of strategy `.py` file at execution/deploy time
- `strategy_git_sha` on `backtests` and `live_deployments` only (NOT on `trades` — trades inherit version from their parent backtest/deployment)
- `strategy_daily_pnl` table is for live deployments only (backtest attribution uses `backtests.metrics` JSONB)

```python
# backend/tests/conftest.py
import pytest
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def postgres_url():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")
```

---

### Task 14: Create database session + dependency injection

**Files:**

- Create: `backend/src/msai/core/database.py`
- Test: `backend/tests/integration/test_database.py`

Implement:

- `create_async_engine()` from `settings.database_url`
- `async_session_factory` using `async_sessionmaker`
- `get_db()` FastAPI dependency that yields `AsyncSession` and auto-closes
- Test: verify session can execute `SELECT 1` against real PostgreSQL (testcontainers in local, CI service in GitHub Actions)

---

### Task 15: Create audit logging middleware

**Files:**

- Create: `backend/src/msai/core/audit.py`
- Test: `backend/tests/unit/test_audit.py`

**Why:** Codex flagged missing audit middleware. Every mutation must be logged.

FastAPI middleware/dependency that logs to `audit_log` table: user_id, action, resource_type, resource_id, details. Applied to all POST/PATCH/DELETE endpoints.

---

### Task 16: Wire auth middleware into FastAPI

**Files:**

- Create: `backend/src/msai/api/auth.py`
- Test: `backend/tests/unit/test_auth_api.py`

Wire the `EntraIDValidator` from Task 5 into FastAPI:

- `GET /api/v1/auth/me` — decode JWT, return user info
- `POST /api/v1/auth/logout` — Invalidate session (clear server-side state if any; frontend clears MSAL cache)
- `get_current_user()` dependency for protected routes
- On first login: auto-create user record from JWT claims (entra_id, email, display_name)

---

### Task 17: Create Strategy registry (local files, NOT uploads)

**Files:**

- Create: `backend/src/msai/api/strategies.py`
- Create: `backend/src/msai/schemas/strategy.py`
- Create: `backend/src/msai/services/strategy_registry.py`
- Test: `backend/tests/unit/test_strategies_api.py`

**Key fix from Codex CRITICAL #2:** Phase 1 does NOT support file uploads. Strategies live in the local `strategies/` directory (checked into git or deployed alongside code). The API **registers** strategies from this directory, not uploads arbitrary Python files.

- `GET /api/v1/strategies/` — List all registered strategies (scanned from `strategies/` dir)
- `GET /api/v1/strategies/{id}` — Get strategy detail + config schema
- `PATCH /api/v1/strategies/{id}` — Update default config
- `POST /api/v1/strategies/{id}/validate` — Validate strategy loads + config parses
- `DELETE /api/v1/strategies/{id}` — Unregister (doesn't delete file)

No `POST /` upload endpoint in Phase 1. Strategy files are added via git commit.

---

### Task 18: Create Backtest API (stubs, wired to arq queue)

**Files:**

- Create: `backend/src/msai/api/backtests.py`
- Create: `backend/src/msai/schemas/backtest.py`
- Test: `backend/tests/unit/test_backtests_api.py`

- `POST /api/v1/backtests/run` — Create job in PostgreSQL + enqueue to arq. Returns job_id.
- `GET /api/v1/backtests/{job_id}/status` — Job status from DB
- `GET /api/v1/backtests/{job_id}/results` — Metrics + trade log
- `GET /api/v1/backtests/{job_id}/report` — Download QuantStats HTML (fixes Codex MEDIUM #11)
- `GET /api/v1/backtests/history` — List past backtests

---

### Task 19: Create arq worker settings

**Files:**

- Create: `backend/src/msai/workers/settings.py`
- Test: `backend/tests/unit/test_worker_settings.py`

Define arq `WorkerSettings`:

- Functions: `[run_backtest]` initially. Add `run_ingest` in Task 25 when `DataIngestionService` is implemented. (Register only what exists to avoid import errors.)
- Max jobs: 2 (prevent resource contention with live trading)
- Job timeout: configurable (default 30 min for backtests, 60 min for ingestion)
- Retry: 1 retry on transient failure (consistent with Task 6)
- On job complete/fail: update PostgreSQL status

**`run_ingest` worker function** (defined alongside `DataIngestionService` in Task 25):

```python
async def run_ingest(ctx, asset_class: str, symbols: list[str], start: str, end: str) -> None:
    """arq worker function for data ingestion jobs."""
    service = DataIngestionService(ParquetStore(settings.data_root))
    await service.ingest_historical(asset_class, symbols, start, end)
```

---

## Milestone 3: Data Layer

### Task 20: Create atomic Parquet store

**Files:**

- Create: `backend/src/msai/services/parquet_store.py`
- Test: `backend/tests/unit/test_parquet_store.py`

Uses `data_integrity.py` from Task 7:

- `write_bars(asset_class, symbol, df)` — Dedup, atomic write, checksum manifest update
- `read_bars(asset_class, symbol, start, end)` → DataFrame
- `list_symbols(asset_class)` → list of available symbols
- Partitioned: `asset_class/symbol/YYYY/MM.parquet`

---

### Task 21: Create DuckDB query service

**Files:**

- Create: `backend/src/msai/services/market_data_query.py`
- Test: `backend/tests/unit/test_market_data_query.py`

Implement `MarketDataQuery` class with read-only DuckDB connection:

- `get_bars(symbol, start, end, interval)` → dict (JSON-serializable for API response)
- `get_symbols()` → list of available symbols (scan Parquet directory structure)
- `get_storage_stats()` → dict with file count and size per asset class
- Uses `duckdb.connect(":memory:")` (in-memory connection — read_only flag is only for file-backed DBs). All operations are SELECT-only by convention; Parquet files are read via `read_parquet()` which does not modify them.
- All paths constructed from `settings.data_root`

---

### Task 22: Create Market Data API endpoints

**Files:**

- Create: `backend/src/msai/api/market_data.py`
- Test: `backend/tests/unit/test_market_data_api.py`

Implement:

- `GET /api/v1/market-data/bars/{symbol}` — Query params: `start`, `end`, `interval` (1m, 5m, 1h, 1d). Returns OHLCV array via `MarketDataQuery.get_bars()`
- `GET /api/v1/market-data/symbols` — List available symbols grouped by asset class
- `GET /api/v1/market-data/status` — Ingestion health: last run timestamp, storage stats, any gaps detected
- `POST /api/v1/market-data/ingest` — Trigger manual data ingestion. Body: `{asset_class, symbols, start, end}`. Enqueues ingestion job to arq. Returns job_id. (Used by "Trigger Download" button in Data Management page.)

---

### Task 23: Create Polygon.io data ingestion client

**Files:**

- Create: `backend/src/msai/services/data_sources/polygon_client.py`
- Test: `backend/tests/unit/test_polygon_client.py`

Implement `PolygonClient`:

- `fetch_bars(symbol, start, end, timespan="minute")` → DataFrame with columns: timestamp, open, high, low, close, volume
- `fetch_options_chain(underlying, start, end)` → DataFrame with OHLCV + strike, expiry, option_type
- Uses `httpx` async client with rate limiting (5 req/sec for free tier, configurable)
- API key from `settings.polygon_api_key` (via secrets provider)
- Test: mock Polygon REST API responses, verify DataFrame normalization

---

### Task 24: Create Databento data ingestion client

**Files:**

- Create: `backend/src/msai/services/data_sources/databento_client.py`
- Test: `backend/tests/unit/test_databento_client.py`

**Fix:** Renamed from `DabentoClient` to `DatabentoClient` (Codex LOW #14).

---

### Task 25: Create data ingestion orchestrator + CLI

**Files:**

- Create: `backend/src/msai/services/data_ingestion.py`
- Create: `backend/src/msai/cli.py`
- Test: `backend/tests/unit/test_data_ingestion.py`

Implement `DataIngestionService`:

- `ingest_historical(asset_class, symbols, start, end)` — Bulk download from Polygon/Databento → ParquetStore
- `ingest_daily(asset_class, symbols)` — Incremental daily update (fetch yesterday's data, append)
- Orchestrates PolygonClient (stocks/indexes/options/crypto) and DatabentoClient (futures) → ParquetStore

CLI via `typer`:

- `uv run python -m msai.cli ingest --asset stocks --symbols AAPL,MSFT --start 2024-01-01 --end 2025-01-01`
- `uv run python -m msai.cli ingest-daily --asset stocks`

**Also in this task:** Add `run_ingest` to arq `WorkerSettings.functions` (from Task 19) now that the implementation exists. Update the worker import.

- `uv run python -m msai.cli data-status` — Show storage stats and last ingestion timestamp

---

## Milestone 4: Backtesting Engine

### Task 26: Create NautilusTrader Parquet catalog integration

**Files:**

- Create: `backend/src/msai/services/nautilus/catalog.py`
- Test: `backend/tests/unit/test_nautilus_catalog.py`

Implement `NautilusCatalog`:

- `get_catalog(data_path)` → `ParquetDataCatalog` instance pointing to `{DATA_ROOT}/parquet/`
- `get_instruments(catalog)` → list of NautilusTrader `Instrument` objects from catalog
- Handle format conversion if MSAI Parquet schema differs from NautilusTrader's expected schema (timestamp format, column names)
- Test: create sample Parquet file, verify catalog reads it and returns instruments

---

### Task 27: Create example EMA Cross strategy

**Files:**

- Create: `strategies/example/ema_cross.py`
- Create: `strategies/example/config.py`
- Test: `backend/tests/unit/test_ema_cross_strategy.py`

Implement `EMACrossStrategy(Strategy)` following NautilusTrader pattern:

- `EMACrossConfig(StrategyConfig, frozen=True)`: `fast_ema_period` (int), `slow_ema_period` (int), `trade_size` (Decimal), `instrument_id` (InstrumentId), `bar_type` (BarType)
- `on_start()` — Register EMA indicators for bar_type, subscribe to bars
- `on_bar()` — If fast EMA > slow EMA and flat: buy. If fast EMA < slow EMA and long: close.
- `on_stop()` — Cancel all orders, close all positions
- Test: verify strategy instantiates with valid config, verify indicator registration

---

### Task 28: Create backtest runner service

**Files:**

- Create: `backend/src/msai/services/nautilus/backtest_runner.py`
- Test: `backend/tests/unit/test_backtest_runner.py`

Implement `BacktestRunner`:

- `run(strategy_class, config, instruments, start_date, end_date, data_path)` → `BacktestResult`
- `BacktestResult` dataclass: `orders_df` (DataFrame), `positions_df` (DataFrame), `account_df` (DataFrame), `metrics` (dict: sharpe, sortino, max_drawdown, total_return, win_rate, num_trades)
- Internally: create `BacktestEngine`, add `SimulatedExchange` venue, add instruments from catalog, add historical data, add strategy, run engine, extract reports via `generate_*_report()` methods
- Must run in a subprocess via `subprocess.Popen` or `multiprocessing.Process` (NautilusTrader constraint: one engine per process). Do NOT use `asyncio.to_thread()` — that still runs in the same process.
- Test: verify `BacktestResult` structure with mock engine outputs

---

### Task 29: Create QuantStats report generator

**Files:**

- Create: `backend/src/msai/services/report_generator.py`
- Test: `backend/tests/unit/test_report_generator.py`

Implement `ReportGenerator`:

- `generate_tearsheet(returns_series, benchmark=None)` → HTML string. Uses `quantstats.reports.html()` with `output=None` (returns string, doesn't write file)
- `save_report(html, backtest_id)` — Save to `{DATA_ROOT}/reports/{backtest_id}.html`
- `get_report_path(backtest_id)` → Path (for download endpoint)
- Test: generate tearsheet from sample returns Series, verify HTML contains expected sections (equity curve, drawdown, monthly returns)

---

### Task 30: Create arq backtest worker function

**Files:**

- Create: `backend/src/msai/workers/backtest_job.py`
- Test: `backend/tests/integration/test_backtest_worker.py`

**Key fix from Codex CRITICAL #1:** This is an arq job function, NOT a `multiprocessing.Process`:

```python
async def run_backtest(ctx, backtest_id: str, strategy_path: str, config: dict) -> None:
    """arq worker function. Runs in separate worker process managed by arq."""
    # 1. Update status to "running" in PostgreSQL
    # 2. Load strategy from strategy_path
    # 3. Run BacktestRunner in subprocess (NautilusTrader: one engine per process)
    # 4. Generate QuantStats report
    # 5. Save results + update status to "completed"
    # On exception: update status to "failed", log error
```

arq handles: retry, timeout, dead-letter, concurrency limits, graceful shutdown.

---

### Task 31: Wire backtest API to arq worker (end-to-end)

**Files:**

- Modify: `backend/src/msai/api/backtests.py`
- Test: `backend/tests/integration/test_backtest_e2e.py`

End-to-end integration test:

1. Seed sample Parquet data (AAPL 1-min bars for 1 month) into `{DATA_ROOT}/parquet/stocks/AAPL/`
2. Register EMA Cross strategy via API
3. `POST /api/v1/backtests/run` with strategy + config + date range
4. Poll `GET /api/v1/backtests/{job_id}/status` until `completed` (max 60s timeout)
5. Verify `GET /api/v1/backtests/{job_id}/results` returns metrics (Sharpe, return, drawdown, num_trades > 0)
6. Verify `GET /api/v1/backtests/{job_id}/report` returns HTML containing "QuantStats" header
7. Verify `strategy_code_hash` is populated in the backtest record

Requires: real PostgreSQL, real Redis, real Parquet data. Use testcontainers locally, CI services in GitHub Actions.

---

## Milestone 5: Frontend Dashboard

### Task 32: Set up Next.js layout, MSAL auth flow, and navigation

**Files:**

- Create: `frontend/src/app/layout.tsx`
- Create: `frontend/src/app/login/page.tsx`
- Create: `frontend/src/components/layout/sidebar.tsx`
- Create: `frontend/src/components/layout/header.tsx`
- Create: `frontend/src/lib/auth.ts`
- Create: `frontend/src/lib/msal-config.ts`

Implement:

- MSAL config: `clientId`, `authority` (`https://login.microsoftonline.com/{tenantId}`), redirect URI
- `MsalProvider` wrapping the app in `layout.tsx`
- Login page: redirect to Entra ID, receive token, store in MSAL cache
- Authenticated layout with sidebar: Dashboard, Strategies, Backtest, Live Trading, Market Data, Data Management, Settings
- Dark mode by default via Tailwind `dark` class on html element
- `useAuth()` hook that provides `user`, `token`, `isAuthenticated`, `login()`, `logout()`
- API client helper that attaches Bearer token to all requests

---

### Task 33: Dashboard overview page

**Files:**

- Create: `frontend/src/app/dashboard/page.tsx`
- Create: `frontend/src/components/dashboard/portfolio-summary.tsx`
- Create: `frontend/src/components/dashboard/active-strategies.tsx`
- Create: `frontend/src/components/dashboard/recent-trades.tsx`
- Create: `frontend/src/components/dashboard/equity-chart.tsx`

Implement:

- Portfolio summary cards: total value, daily P&L (green/red), total return %, active strategy count
- Active strategies list with status badges (running, stopped, error)
- Recent trades table: last 20 trades with timestamp, instrument, side, quantity, price, P&L
- Equity curve chart (Recharts `AreaChart`) showing portfolio value over time
- Fetches data from `GET /api/v1/account/summary`, `GET /api/v1/live/status`, `GET /api/v1/live/trades`

---

### Task 34: Strategies management page

**Files:**

- Create: `frontend/src/app/strategies/page.tsx`
- Create: `frontend/src/app/strategies/[id]/page.tsx`
- Create: `frontend/src/components/strategies/strategy-card.tsx`
- Create: `frontend/src/components/strategies/config-editor.tsx`

Implement:

- Grid of strategy cards showing: name, description, last backtest Sharpe ratio, status
- Strategy detail page with: description, config schema as editable JSON form, backtest history table
- "Run Backtest" button (navigates to backtest page with strategy pre-selected)
- "Validate" button → calls `POST /api/v1/strategies/{id}/validate`
- No file upload dialog — strategies registered from local directory
- Fetches from `GET /api/v1/strategies/`, `GET /api/v1/strategies/{id}`

---

### Task 35: Backtest run and results page

**Files:**

- Create: `frontend/src/app/backtests/page.tsx`
- Create: `frontend/src/app/backtests/[id]/page.tsx`
- Create: `frontend/src/components/backtests/run-form.tsx`
- Create: `frontend/src/components/backtests/results-charts.tsx`
- Create: `frontend/src/components/backtests/trade-log.tsx`

Implement:

- Run form: strategy dropdown, instruments multi-select, date range picker, config JSON editor, "Run Backtest" button
- Progress indicator (polls `GET /api/v1/backtests/{id}/status` every 2s while running)
- Results page: key metrics cards (Sharpe, Sortino, max drawdown, total return, win rate, num trades)
- Equity curve (Recharts `LineChart`), drawdown chart (Recharts `AreaChart`, inverted)
- Monthly returns heatmap (12 columns x N years, color-coded green/red)
- Trade log table with columns: timestamp, instrument, side, qty, price, P&L — sortable and filterable
- "Download Report" button → fetches `GET /api/v1/backtests/{id}/report` and opens QuantStats HTML in new tab

---

### Task 36: Market Data page with TradingView charts

**Files:**

- Create: `frontend/src/app/market-data/page.tsx`
- Create: `frontend/src/components/charts/candlestick-chart.tsx`
- Create: `frontend/src/components/charts/symbol-selector.tsx`

Implement:

- Symbol selector: searchable dropdown grouped by asset class (stocks, indexes, futures, options, crypto)
- Date range picker: preset buttons (1D, 1W, 1M, 3M, 1Y, ALL) + custom range
- TradingView Lightweight Charts candlestick chart with volume bars
- Chart fetches data from `GET /api/v1/market-data/bars/{symbol}?start=...&end=...&interval=1m`
- Auto-resize chart to container width

---

### Task 37: Live Trading monitoring page

**Files:**

- Create: `frontend/src/app/live/page.tsx`
- Create: `frontend/src/components/live/strategy-status.tsx`
- Create: `frontend/src/components/live/positions-table.tsx`
- Create: `frontend/src/components/live/kill-switch.tsx`

Implement:

- Active strategies table: name, instruments, status (running/stopped/error), start time, daily P&L
- Start/stop buttons per strategy (calls `POST /api/v1/live/start`, `POST /api/v1/live/stop`)
- Positions table: instrument, quantity, avg price, current price, unrealized P&L, market value
- Kill switch button: red "STOP ALL" button with confirmation dialog → calls kill_all endpoint
- WebSocket connection (`WS /api/v1/live/stream`) for real-time updates: position changes, trade executions, P&L updates
- Connection status indicator (connected/disconnected/reconnecting)

---

### Task 38: Data Management page

**Files:**

- Create: `frontend/src/app/data/page.tsx`
- Create: `frontend/src/components/data/storage-chart.tsx`
- Create: `frontend/src/components/data/ingestion-status.tsx`

Implement:

- Storage usage bar chart (Recharts) per asset class: stocks, indexes, futures, options, crypto
- Data ingestion status: last run timestamp, next scheduled, success/failure indicator
- "Trigger Download" button for manual data ingestion (calls backend ingestion endpoint)
- Available symbols table: symbol, asset class, first date, last date, row count — sortable
- Fetches from `GET /api/v1/market-data/status`, `GET /api/v1/market-data/symbols`

---

### Task 39: Settings page

**Files:**

- Create: `frontend/src/app/settings/page.tsx`

Implement:

- User profile section: display name, email, role (from Entra ID via `GET /api/v1/auth/me`)
- Notification preferences: email address for alerts (stored in user settings)
- System info: app version, environment, uptime, disk usage, database connection status (from `GET /health` and `GET /ready`)
- Danger zone: "Clear all data" (with confirmation), "Reset settings"

---

## Milestone 6: Operational Readiness

> Per Codex: "Runbooks and DR must come BEFORE live trading, not after."

### Task 40: Write deployment scripts

**Files:**

- Create: `scripts/deploy-azure.sh`
- Create: `docker-compose.prod.yml`
- Create: `scripts/backup-to-blob.sh`

Production Docker Compose with proper resource limits, restart policies, and no dev overrides.

---

### Task 41: Write runbooks

**Files:**

- Create: `docs/runbooks/vm-setup.md`
- Create: `docs/runbooks/disaster-recovery.md`
- Create: `docs/runbooks/ib-gateway-troubleshooting.md`

Document and TEST: VM provisioning, DR restore from Azure Blob, IB Gateway credential rotation, monitoring alert setup.

---

### Task 42: Set up monitoring alerts

**Files:**

- Create: `backend/src/msai/services/alerting.py`

Email/SMS alerts on: strategy error, daily loss threshold, system down, IB disconnect, disk usage > 80%.

---

## Milestone 7: Live Trading

> Risk controls BEFORE live endpoints (Codex CRITICAL #4).

### Task 43: Add IB Gateway to Docker Compose

**Files:**

- Modify: `docker-compose.dev.yml`
- Create: `scripts/ib-gateway-config/`

Add IB Gateway container to Docker Compose:

- Image: `ghcr.io/gnzsnz/ib-gateway:10.30.1t` (pinned version — check for latest stable tag at build time)
- Ports: NOT exposed to host (internal Docker network only). Other containers connect via `ib-gateway:4002`. Design requires IB Gateway on localhost only — never bind to `0.0.0.0`.
- Environment: `TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE=paper` (default paper)
- Credentials via `SecretsProvider` (Task 4): dev reads from env vars set directly in `docker-compose.dev.yml` (committed — contains only dev-safe placeholder values like `msai_dev_password`). Prod reads from Azure Key Vault. Real IB credentials go in `docker-compose.override.yml` (gitignored).
- Health check: `nc -z localhost 4002` (TCP connection test)
- Volume: `./scripts/ib-gateway-config:/root/ibc` for IBC config files
- Depends on: nothing (standalone)

---

### Task 44: Create risk engine MVP

**Files:**

- Create: `backend/src/msai/services/risk_engine.py`
- Test: `backend/tests/unit/test_risk_engine.py`

**Why:** Codex CRITICAL #4 — risk controls must exist BEFORE live trading endpoints.

Implement:

- `check_position_limit(strategy, instrument, quantity)` → allow/reject
- `check_daily_loss(current_pnl, threshold)` → allow/halt
- `check_notional_exposure(portfolio_value, max_exposure)` → allow/reject
- `kill_all()` → emergency stop all strategies
- NautilusTrader `RiskEngine` integration for broker-side enforcement

---

### Task 45: Create IB account API endpoints

**Files:**

- Create: `backend/src/msai/services/ib_account.py`
- Create: `backend/src/msai/api/account.py`
- Test: `backend/tests/unit/test_account_api.py`

Implement:

- `GET /api/v1/account/summary` — IB account summary: net liquidation value, buying power, margin used, available funds, unrealized P&L
- `GET /api/v1/account/portfolio` — Current IB portfolio: list of positions with instrument, quantity, avg cost, market value, unrealized P&L
- Uses `ib_async` to connect to IB Gateway (port 4002 for paper, 4001 for live)
- Connection management: connect on first request, auto-reconnect on disconnect
- Test: mock `ib_async.IB` client, verify response schemas

---

### Task 46: Create NautilusTrader TradingNode service

**Files:**

- Create: `backend/src/msai/services/nautilus/trading_node.py`
- Test: `backend/tests/unit/test_trading_node.py`

Implement `TradingNodeManager`:

- `start(strategy_id, config, instruments)` — Validate via `risk_engine.validate()` first, then start TradingNode process with IB adapter. Record deployment in `live_deployments` table with `strategy_code_hash` and `strategy_git_sha`.
- `stop(deployment_id)` — Gracefully stop TradingNode, update `live_deployments.status` to `stopped`
- `status()` → list of running deployments with status from DB + process health
- NautilusTrader config: `InteractiveBrokersDataClientConfig(host="ib-gateway", port=4002)`, `InteractiveBrokersExecClientConfig`, `TradingNodeConfig`
- Runs as separate process (one TradingNode per process). Communicates state via PostgreSQL.
- Test: verify start/stop lifecycle with mocked NautilusTrader + mocked risk engine

---

### Task 47: Create Live Trading API endpoints

**Files:**

- Create: `backend/src/msai/api/live.py`
- Create: `backend/src/msai/schemas/live.py`
- Test: `backend/tests/unit/test_live_api.py`

Implement:

- `POST /api/v1/live/start` — Body: `{strategy_id, config, instruments, paper_trading}`. Calls `risk_engine.validate()` BEFORE launching. Returns deployment_id.
- `POST /api/v1/live/stop` — Body: `{deployment_id}`. Gracefully stops strategy.
- `POST /api/v1/live/kill-all` — Emergency stop ALL running strategies. Calls `risk_engine.kill_all()`. Returns count of stopped deployments.
- `GET /api/v1/live/status` — All deployments with current status, instruments, start time
- `GET /api/v1/live/positions` — Current open positions from active TradingNodes
- `GET /api/v1/live/trades` — Recent trade executions (last 100, from `trades` table where `is_live=true`)
- All endpoints require auth + audit logging
- Test: verify risk engine is called before start, verify stop updates DB status, verify kill-all stops all deployments

---

### Task 48: Create WebSocket for live updates

**Files:**

- Create: `backend/src/msai/api/websocket.py`
- Test: `backend/tests/unit/test_websocket.py`

**Key fix from Codex HIGH #9:**

- Auth via **first message** (client sends JWT as first WebSocket message), NOT query string
- Transport: **Redis pub/sub** (Redis already in Docker Compose from Task 10)

```python
@app.websocket("/api/v1/live/stream")
async def live_stream(websocket: WebSocket):
    await websocket.accept()
    # First message must be JWT token — 5 second timeout
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
        user = validate_token(token)
    except (asyncio.TimeoutError, jwt.InvalidTokenError):
        await websocket.close(code=4001, reason="Authentication failed or timed out")
        return
    # Authenticated — subscribe to Redis pub/sub channel
    async for message in redis_subscriber:
        await websocket.send_json(message)
```

---

### Task 49: Create IB Gateway health monitor (IBProbe)

**Files:**

- Create: `backend/src/msai/services/ib_probe.py`
- Test: `backend/tests/unit/test_ib_probe.py`

Implement `IBProbe` (inspired by v1's IBProbe CronJob):

- Periodic health check (every 60s via `asyncio` background task): connect to IB Gateway via `ib_async`, verify connection, check account balance is non-zero
- On single failure: log warning via structlog, increment failure counter
- On 3 consecutive failures: log error, send alert (via alerting service from Task 42). Do NOT restart containers from app code (requires Docker socket mount = privilege escalation risk). Instead, rely on Docker Compose `restart: unless-stopped` policy for auto-recovery.
- On recovery: log info, reset failure counter, send recovery alert
- Expose health status via `GET /api/v1/account/health` (for frontend connection indicator)
- Test: mock ib_async connection, verify failure counting and alert triggers

---

## Post-Implementation

### Task 50: Create CLI tool for strategy management

**Files:**

- Modify: `backend/src/msai/cli.py`

Extend the `typer` CLI (created in Task 25) with strategy and live trading commands:

- `msai strategy list` — List registered strategies (calls same logic as API)
- `msai strategy validate --name ema_cross` — Validate a strategy loads correctly
- `msai backtest run --strategy ema_cross --instruments AAPL,MSFT --start 2024-01-01 --end 2025-01-01` — Run backtest, print progress, print summary metrics on completion
- `msai live start --strategy ema_cross --instruments AAPL --paper` — Start paper trading
- `msai live stop --deployment-id <uuid>` — Stop a running deployment
- `msai live status` — Show all active deployments with status
- `msai live kill-all` — Emergency stop all strategies
- All commands use the same service layer as the API (no duplicate logic)

---

## Build Order Summary

```
Milestone 0 (Foundation)   ██░░░░░░░░░░░░░░░░░░  Tasks 1-7    (MUST DO FIRST)
Milestone 1 (Scaffold)     ████░░░░░░░░░░░░░░░░  Tasks 8-12
Milestone 2 (Backend)      ████████░░░░░░░░░░░░  Tasks 13-19
Milestone 3 (Data)         ██████████░░░░░░░░░░  Tasks 20-25
Milestone 4 (Backtest)     ██████████████░░░░░░  Tasks 26-31
Milestone 5 (Frontend)     ████████████████░░░░  Tasks 32-39
Milestone 6 (Ops Ready)    ██████████████████░░  Tasks 40-42  (BEFORE live trading)
Milestone 7 (Live Trading) ████████████████████  Tasks 43-49  (risk engine FIRST)
Post-Impl                  ████████████████████  Task 50
```

**Critical path:** M0 → M1 → M2 → M3 → M4. Frontend (M5) can start in parallel with M4.

**Non-negotiable ordering:**

- M0 (Foundation) before everything
- M6 (Ops Readiness) before M7 (Live Trading)
- Task 44 (Risk Engine) before Task 47 (Live API endpoints)
