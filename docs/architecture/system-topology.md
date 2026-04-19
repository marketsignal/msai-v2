# System Topology

## Docker Compose Services

The platform runs as a set of Docker containers orchestrated by Docker
Compose. There are two compose files:

- `docker-compose.dev.yml` -- development with hot reload via volume mounts
- `docker-compose.prod.yml` -- production with resource limits, no volume mounts

### Dev Compose Services (docker-compose.dev.yml)

| Service         | Container Name              | Image / Build                    | Internal Port | Host Port                      | Profile |
| --------------- | --------------------------- | -------------------------------- | ------------- | ------------------------------ | ------- |
| postgres        | msai-claude-postgres        | postgres:16-alpine               | 5432          | 5433                           | default |
| redis           | msai-claude-redis           | redis:7-alpine                   | 6379          | 6380                           | default |
| backend         | msai-claude-backend         | ./backend/Dockerfile.dev         | 8000          | 8800                           | default |
| backtest-worker | msai-claude-worker          | ./backend/Dockerfile.dev         | --            | --                             | default |
| frontend        | msai-claude-frontend        | ./frontend/Dockerfile.dev        | 3000          | 3300                           | default |
| live-supervisor | msai-claude-live-supervisor | ./backend/Dockerfile.dev         | --            | --                             | live    |
| ib-gateway      | msai-claude-ib-gateway      | ghcr.io/gnzsnz/ib-gateway:stable | 4002/5900     | 127.0.0.1:4002, 127.0.0.1:5900 | live    |

### Prod Compose Services (docker-compose.prod.yml)

| Service         | Image / Build                    | Host Port                            | CPU Limit | Memory Limit |
| --------------- | -------------------------------- | ------------------------------------ | --------- | ------------ |
| postgres        | postgres:16-alpine               | --                                   | 2.0       | 4G           |
| redis           | redis:7-alpine                   | --                                   | 0.5       | 512M         |
| backend         | ./backend/Dockerfile             | 8000                                 | 2.0       | 4G           |
| backtest-worker | ./backend/Dockerfile             | --                                   | 2.0       | 4G           |
| live-supervisor | ./backend/Dockerfile             | --                                   | 2.0       | 4G           |
| frontend        | ./frontend/Dockerfile            | 3000                                 | 1.0       | 1G           |
| ib-gateway      | ghcr.io/gnzsnz/ib-gateway:stable | 127.0.0.1:${IB_PORT}, 127.0.0.1:5900 | 1.0       | 2G           |

Note: In prod compose, `live-supervisor` and `ib-gateway` are NOT behind
a profile -- they run unconditionally. The prod compose requires
`TWS_USERID`, `TWS_PASSWORD`, `IB_ACCOUNT_ID`, and `POSTGRES_PASSWORD`
in `.env` (enforced via `${VAR:?msg}` syntax).

## The `live` Profile Boundary

In dev, the live-supervisor and ib-gateway services are gated behind the
`live` Compose profile. This means:

```bash
# Start WITHOUT live trading (frontend, backend, DB, Redis, worker only):
docker compose -f docker-compose.dev.yml up -d

# Start WITH live trading (adds ib-gateway + live-supervisor):
COMPOSE_PROFILES=live docker compose -f docker-compose.dev.yml up -d

# Or set in .env:
# COMPOSE_PROFILES=live
```

The profile gate exists so frontend-only development does not require IB
credentials. The `live-supervisor` service uses `${VAR:?msg}` guards for
`TWS_USERID`, `TWS_PASSWORD`, and `IB_ACCOUNT_ID` -- compose fails
instantly if any are missing.

## Network Topology

All services join a single Docker Compose default bridge network. Inter-
container communication uses Docker DNS hostnames:

```
+-------------------------------------------------------------------+
|  Docker Compose Network (bridge)                                  |
|                                                                   |
|  +----------+    +-------+    +---------+    +------------------+ |
|  | postgres |    | redis |    | backend |    |    frontend      | |
|  | :5432    |    | :6379 |    | :8000   |    |    :3000         | |
|  +----+-----+    +---+---+    +----+----+    +--------+---------+ |
|       |              |             |                   |           |
|       |   +----------+----------+  |                   |           |
|       |   |                     |  |                   |           |
|  +----+---+---+    +--------+--+--+---+               |           |
|  | backtest-  |    | live-supervisor  |               |           |
|  | worker     |    | (profile: live)  |               |           |
|  +------------+    +--------+---------+               |           |
|                             |                         |           |
|                    +--------+---------+               |           |
|                    |   ib-gateway     |               |           |
|                    |   :4002 (paper)  |               |           |
|                    |   :4001 (live)   |               |           |
|                    +------------------+               |           |
+-------------------------------------------------------------------+
        |                                                   |
   Host :5433                                          Host :3300
   Host :6380                                          Host :8800
   Host :4002 (127.0.0.1 only)
   Host :5900 (127.0.0.1 only, VNC)
```

### Inter-Container Communication Paths

| From            | To         | Protocol    | Address         | Purpose                           |
| --------------- | ---------- | ----------- | --------------- | --------------------------------- |
| backend         | postgres   | TCP/asyncpg | postgres:5432   | App state (SQLAlchemy async)      |
| backend         | redis      | TCP/Redis   | redis:6379      | Job queue, pub/sub, halt flag     |
| backtest-worker | postgres   | TCP/asyncpg | postgres:5432   | Read/write backtest + trade rows  |
| backtest-worker | redis      | TCP/Redis   | redis:6379      | arq job dequeue                   |
| live-supervisor | postgres   | TCP/asyncpg | postgres:5432   | live_node_processes rows          |
| live-supervisor | redis      | TCP/Redis   | redis:6379      | Command stream, halt flag         |
| live-supervisor | ib-gateway | TCP/TWS API | ib-gateway:4002 | Trading subprocess connects to IB |
| frontend        | backend    | HTTP        | localhost:8800  | Via browser (host network)        |
| frontend        | backend    | WebSocket   | localhost:8800  | Live event stream                 |

### Health Checks

Every service has a health check in compose:

| Service    | Probe                                                                | Interval | Start Period |
| ---------- | -------------------------------------------------------------------- | -------- | ------------ |
| postgres   | `pg_isready -U msai`                                                 | 5s       | --           |
| redis      | `redis-cli ping`                                                     | 5s       | --           |
| backend    | `python -c "urllib.request.urlopen('http://localhost:8000/health')"` | 5s       | 30s          |
| ib-gateway | `bash -c 'exec 3<>/dev/tcp/localhost/4002'`                          | 15s      | 180s         |

The `live-supervisor` depends on `ib-gateway: service_healthy` (not
`service_started`), so the supervisor only boots once IBC has logged in
and the TWS API port is accepting connections (typically 60-120s).

## Volumes

| Volume Name         | Mount Point              | Purpose                             |
| ------------------- | ------------------------ | ----------------------------------- |
| postgres_data       | /var/lib/postgresql/data | PostgreSQL data persistence         |
| ib_gateway_settings | /home/ibgateway/Jts      | TWS settings across restarts        |
| app_data (prod)     | /app/data                | Parquet files + reports (prod only) |

Dev compose mounts source directories as read-only bind mounts for hot
reload:

- Backend: `./backend/src:/app/src:ro`
- Strategies: `./strategies:/app/strategies:ro`
- Data: `./data:/app/data` (read-write for ingestion)
- Frontend: `./frontend/src:/app/src:ro`, `./frontend/public:/app/public:ro`, etc.

## Environment Variables

Key environment variables consumed by the backend
(`core/config.py:Settings`):

| Variable                 | Default                   | Description                             |
| ------------------------ | ------------------------- | --------------------------------------- |
| DATABASE_URL             | postgresql+asyncpg://...  | Async SQLAlchemy database URL           |
| REDIS_URL                | redis://localhost:6379    | Redis connection URL                    |
| DATA_ROOT                | (project)/data            | Root for Parquet, reports, catalogs     |
| STRATEGIES_ROOT          | (project)/strategies      | Strategy Python files directory         |
| ENVIRONMENT              | development               | `development` or `production`           |
| AZURE_TENANT_ID          | (empty)                   | Entra ID tenant for JWT validation      |
| AZURE_CLIENT_ID          | (empty)                   | Entra ID app client ID                  |
| MSAI_API_KEY             | (empty)                   | Dev API key bypass for auth             |
| CORS_ORIGINS             | ["http://localhost:3000"] | Allowed CORS origins                    |
| POLYGON_API_KEY          | (empty)                   | Polygon.io API key                      |
| DATABENTO_API_KEY        | (empty)                   | Databento API key                       |
| IB_ACCOUNT_ID            | DU0000000                 | IB paper/live account ID                |
| IB_HOST                  | 127.0.0.1                 | IB Gateway hostname                     |
| IB_PORT                  | 4002                      | IB Gateway port (4002=paper, 4001=live) |
| STARTUP_HEALTH_TIMEOUT_S | 60.0                      | Max wait for trader.is_running          |
| BACKTEST_TIMEOUT_SECONDS | 1800                      | arq job timeout for backtests           |
