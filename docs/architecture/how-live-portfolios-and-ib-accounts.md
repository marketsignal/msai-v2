<!-- forge:doc how-live-portfolios-and-ib-accounts -->

> **Naming alert — read this first.**
>
> This document is about the **live portfolio** (the running production thing).
>
> - Live portfolio: file `backend/src/msai/api/portfolios.py` (**plural**), URL prefix `/api/v1/live-portfolios`, UI at `/live-trading`. Drives `LivePortfolio → LivePortfolioRevision → LiveDeployment` and ultimately a TradingNode subprocess connected to IB Gateway.
> - Backtest portfolio: file `backend/src/msai/api/portfolio.py` (**singular**), URL prefix `/api/v1/portfolios`, UI at `/portfolio`. Drives `Portfolio → PortfolioRun` and is documented in [How Backtest Portfolios Work](how-backtest-portfolios-work.md).
>
> Two domains, two files, two URLs. The names look almost identical and the codebase keeps them strictly separate on purpose. If you find yourself editing one of the files and the other directory's symbols start showing up in the diff, stop and re-check the file path. Codex flagged this split during planning; we maintain it deliberately.

# How Live Portfolios and IB Accounts Work

This is the operationally riskiest doc in the set. It covers the surface where MSAI v2 sends real orders to a real broker against real money. Everything that moves between "we ran a backtest and liked it" and "we have an open position in an IB account" is described here.

```
                       ┌─ LIVE PORTFOLIO ─────────────────────────────────────┐
                       │  POST /api/v1/live-portfolios                        │
                       │  ┌──────────────────────────────────────────────┐    │
                       │  │ LivePortfolio (mutable container)            │    │
                       │  │   id · name (UNIQUE) · description           │    │
                       │  │   creator                                    │    │
                       │  └──────────────────┬───────────────────────────┘    │
                       │                     │ has many revisions             │
                       │                     ▼                                │
                       │  ┌──────────────────────────────────────────────┐    │
                       │  │ LivePortfolioRevision  (immutable, frozen)   │    │
                       │  │   revision_number · composition_hash         │    │
                       │  │   is_frozen=true                             │    │
                       │  │   ONE draft per portfolio                    │    │
                       │  │   (partial unique idx WHERE is_frozen=false) │    │
                       │  └──────────────────┬───────────────────────────┘    │
                       │                     │ has N strategies               │
                       │                     ▼                                │
                       │  ┌──────────────────────────────────────────────┐    │
                       │  │ LivePortfolioRevisionStrategy                │    │
                       │  │   strategy_id · config (JSONB)               │    │
                       │  │   instruments[] · weight ∈ (0, 1]            │    │
                       │  │   order_index                                │    │
                       │  └──────────────────────────────────────────────┘    │
                       └────────────────────────────┬─────────────────────────┘
                                                    │
                                                    │  POST /api/v1/live/start-portfolio
                                                    │   { portfolio_revision_id, account_id, paper_trading }
                                                    ▼
              ┌────────────────────────────────────────────────────────────────────┐
              │ FastAPI ── api/live.py:245                                         │
              │  1. verify revision is frozen                                      │
              │  2. load member strategies + compute code_hashes                   │
              │  3. aggregate instruments + configs                                │
              │  4. 3-LAYER IDEMPOTENCY                                            │
              │     L1 HTTP Idempotency-Key (Redis SETNX)                          │
              │     L2 halt flag check (msai:risk:halt)                            │
              │     L3 identity_signature ON CONFLICT upsert on LiveDeployment     │
              │  5. publish START to msai:live:commands                            │
              │  6. poll live_node_processes (60s, 0.25s tick)                     │
              └────────────────────────────┬───────────────────────────────────────┘
                                           │  XADD msai:live:commands
                                           ▼
              ┌─────────────────────────────────────────────────────────────────┐
              │ live_supervisor (separate process)                              │
              │   ├─ LiveCommandBus.consume     (XREADGROUP, PEL recovery)      │
              │   ├─ ProcessManager.spawn       (re-checks halt; forks subproc) │
              │   ├─ ProcessManager.reap_loop   (surfaces exit codes)           │
              │   ├─ ProcessManager.watchdog_loop (SIGKILLs wedged starts)      │
              │   └─ HeartbeatMonitor           (flips stale rows → failed)     │
              └────────────────────────────┬────────────────────────────────────┘
                                           │  fork()
                                           ▼
              ┌─────────────────────────────────────────────────────────────────┐
              │ TradingNode subprocess (one per LiveDeployment)                 │
              │   ├─ Connect to IB Gateway                                      │
              │   ├─ Bootstrap instruments (no dynamic loading on hot path)     │
              │   ├─ Load strategies wrapped by FailureIsolatedStrategy         │
              │   ├─ Reconciliation against IB                                  │
              │   └─ Live event loop (bars → on_bar → orders)                   │
              └────────────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
              ┌─────────────────────────────────────────────────────────────────┐
              │ IB Gateway   (Compose profile: broker)                          │
              │   paper ports {4002, 4004} (DU…/DF…)                            │
              │   live  ports {4001, 4003} (U…)                                 │
              │   dev compose injects 4004 (paper) / 4003 (live) — socat proxy  │
              └────────────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼ executions
              ┌─────────────────────────────────────────────────────────────────┐
              │ Postgres                                                        │
              │   trades · orders · order_attempt_audits · live_node_processes  │
              │   (+ Trade dedup via partial unique idx on broker_trade_id)     │
              └─────────────────────────────────────────────────────────────────┘
```

The arrows are real flow, not metaphor. Every label maps to a file you can `grep` for.

---

## TL;DR

A **live portfolio** is a named, mutable container that owns a chain of immutable, hash-pinned **revisions**. Each revision lists the strategies, configs, instruments, and weights for one production deployment. To go live, you **freeze** a draft revision (it gets a `composition_hash` and `is_frozen=true`) and call `POST /api/v1/live/start-portfolio` with the revision id, an IB account id, and a paper/live flag. The API runs three idempotency layers, persists a `LiveDeployment` row, publishes a START command on a Redis Stream, and the **`live_supervisor`** — a separate process — picks up the command, spawns a NautilusTrader **TradingNode subprocess**, connects it to **IB Gateway** on the paper port (`{4002, 4004}` accept paper accounts) or the live port (`{4001, 4003}` accept live accounts), bootstraps instruments, loads strategies, and starts trading. A 4-layer **kill-all** flips a 24-hour halt flag, prevents new spawns, sends STOP commands, and refuses orders inside the strategies — but it does **not** auto-flatten positions: stopping a node leaves whatever was open at IB still open (NautilusTrader gotcha #13). That's a deliberate choice, and you have to know it.

This is the production-money doc. Read every section.

**Three surfaces.**

| Surface | Entry point                                                                                                                                                |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API     | `/api/v1/live-portfolios` (CRUD + revision lifecycle) and `/api/v1/live` (deploy/stop/kill/status/positions/trades) and `/api/v1/account` (IB-side truth). |
| CLI     | `uv run msai live …` (start, stop, status, kill-all) and `uv run msai account …` (summary, positions, health).                                             |
| UI      | `/live-trading` page (deployment list, kill switch, positions, WS stream) and `/settings` (IB account configuration display).                              |

---

## Table of Contents

1. [Concepts and the data model](#1-concepts-and-the-data-model)
2. [The three surfaces — parity table](#2-the-three-surfaces--parity-table)
3. [Internal sequence — `POST /live/start-portfolio` and the supervisor](#3-internal-sequence--post-livestart-portfolio-and-the-supervisor)
4. [See, Verify, Troubleshoot](#4-see-verify-troubleshoot)
5. [Common failures](#5-common-failures)
6. [Idempotency / Retry behavior](#6-idempotency--retry-behavior)
7. [Rollback / Repair](#7-rollback--repair)
8. [Key files](#8-key-files)

---

## 1. Concepts and the data model

The live-portfolio domain is six tables and one Redis stream. Learn the chain in this order: container → revision → revision-strategy → deployment → process. The IB account model is environment-driven (no DB row per account), but it interacts at the deployment level.

### 1.1 The chain

**`LivePortfolio` — the mutable container.** A named bucket (`name` is `UNIQUE`) that owns a sequence of revisions. The portfolio itself has no strategy composition — it just carries `name`, `description`, and `created_by`. Defined in `backend/src/msai/models/live_portfolio.py`. Renaming a portfolio is allowed; deleting one cascades to its revisions.

**`LivePortfolioRevision` — the immutable composition.** This is the unit you actually deploy. Two states:

- **Draft** (`is_frozen=false`) — at most one per portfolio, enforced by a **partial unique index** `uq_one_draft_per_portfolio` on `(portfolio_id) WHERE is_frozen = false`. You add and remove strategies freely while it's a draft.
- **Frozen** (`is_frozen=true`) — immutable. It carries a `composition_hash` (SHA256 of the member set) and a sequential `revision_number`. Two unique constraints kick in once frozen: `(portfolio_id, revision_number)` and `(portfolio_id, composition_hash)` — the second one is what makes `snapshot` idempotent (snapshotting a draft whose composition matches an existing frozen revision returns that revision instead of creating a new one).

Defined in `backend/src/msai/models/live_portfolio_revision.py`. Immutability is enforced at two layers: the service boundary (`RevisionService.enforce_immutability`) and the database (the partial unique index plus the `is_frozen` flag). You cannot mutate a frozen revision's membership through any documented API; if you need a different mix, freeze a new revision.

**`LivePortfolioRevisionStrategy` — one row per member.** Each row holds a `strategy_id` (FK with `RESTRICT`, so you can't delete a strategy that's in a revision), a `config` (JSONB) for the per-strategy parameters, an **`instruments`** array (the symbols this member will subscribe to — keyed under the column literally named `instruments`, sometimes called `member_instruments` colloquially in code review), a `weight` ∈ `(0, 1]` enforced by a CHECK constraint, and an `order_index` for stable ordering. Two unique constraints: `(revision_id, order_index)` and `(revision_id, strategy_id)` — you can't list the same strategy twice in the same revision.

Defined in `backend/src/msai/models/live_portfolio_revision_strategy.py`.

**`LiveDeployment` — the running thing.** When you call `/live/start-portfolio`, the API upserts a `LiveDeployment` row keyed by `identity_signature` (SHA256 of `{user_id, portfolio_revision_id, account_id, paper_trading}`). The same identity always re-uses the same row across restarts — that's how the system knows "this is a warm restart of the same deployment, not a brand-new one." Key columns:

- `deployment_slug` — 16 hex characters from `secrets.token_hex(8)`. Stable across restarts. The slug is what shows up in user-facing urls and logs.
- `trader_id` — `f"MSAI-{deployment_slug}"`. NautilusTrader's `TraderId` for this deployment.
- `strategy_id_full` — derived per-member Nautilus strategy identifier.
- `account_id` — the IB account this deployment is bound to (e.g., `DU0000000` or `DF…` / `DFP…` for paper, `U…` for live; see `IB_PAPER_PREFIXES` in `ib_port_validator.py`).
- `paper_trading` — boolean; selects which IB Gateway port the subprocess will connect to.
- `message_bus_stream` — Redis Streams name for this deployment's events; downstream pieces (WebSocket projection, dashboard) subscribe here.
- `portfolio_revision_id` — FK to the frozen revision this deployment is running.
- `last_started_at`, `last_stopped_at` — denormalized timestamps for the most recent transitions.
- `status` — one of `starting`, `building`, `ready`, `running`, `stopped`, `failed` (the engine state).

Unique constraint `uq_live_deployments_revision_account` on `(portfolio_revision_id, account_id)`: one deployment per (revision, account) pair. You cannot run the same revision twice on the same account.

Defined in `backend/src/msai/models/live_deployment.py`.

**`LiveNodeProcess` — per-restart, per-process state.** The `LiveDeployment` row is stable across restarts; the `LiveNodeProcess` row is per-spawn. It tracks the subprocess PID, status transitions (`starting → building → ready → running` with `failed` as a sink at any point), and `last_heartbeat_at` for the heartbeat monitor. When you stop and restart a deployment, you get a new `LiveNodeProcess` row for the new subprocess; the `LiveDeployment` row stays put.

Defined in `backend/src/msai/models/live_node_process.py` (referenced from `api/live.py`).

### 1.2 The IB account model

There is **no `accounts` table**. IB accounts are environment-configured at the FastAPI process level via env vars and bound to deployments through the `account_id` field on `LiveDeployment`.

**Paper vs live, by prefix.** The classification table lives at `backend/src/msai/services/nautilus/ib_port_validator.py` and is the single source of truth used by the runtime guard:

```python
IB_PAPER_PORTS:    tuple[int, ...] = (4002, 4004)
IB_LIVE_PORTS:     tuple[int, ...] = (4001, 4003)
IB_PAPER_PREFIXES: tuple[str, ...] = ("DU", "DF")
```

- **Paper accounts** start with **`DU`** (individual paper) or **`DF`** / **`DFP`** (Financial-Advisor paper sub-accounts). They live on a **paper port** — either `4002` (raw) or `4004` (the socat-proxied port the dev compose stack actually exposes; see below).
- **Live accounts** start with `U` (no `DU`/`DF` prefix; e.g., `U1234567`). They live on a **live port** — either `4001` (raw) or `4003` (socat-proxied dev port).

The convention is enforced as a runtime check in `build_live_trading_node_config` (which calls `validate_port_account_consistency` from `ib_port_validator`): a `(port, account_id_prefix)` mismatch crashes the subprocess on startup rather than failing silently in the data feed. This is the explicit answer to NautilusTrader gotcha #6 — see [`.claude/rules/nautilus.md`](../../.claude/rules/nautilus.md). Silent cross-wiring on the wrong port is the most expensive failure mode in this domain; we take the crash.

**Environment variables (FastAPI / supervisor side; see `backend/src/msai/core/config.py:62-128`):**

```
IB_ACCOUNT_ID=DU0000000           # paper default; live starts with U
IB_HOST=ib-gateway                # alias-accepted: IB_HOST or IB_GATEWAY_HOST
IB_PORT=4004                      # alias-accepted: IB_PORT or IB_GATEWAY_PORT_PAPER.
                                  # Defaults to 4004 — the gnzsnz socat proxy port for
                                  # paper. IB Gateway itself binds to 127.0.0.1:4002
                                  # internally and refuses non-loopback API connections;
                                  # cross-container clients MUST go through the proxy.
                                  # For LIVE runs, set IB_PORT=4003 (socat proxy for
                                  # live; IB Gateway binds 4001 internally) — there is
                                  # NO IB_GATEWAY_PORT_LIVE settings field; the
                                  # supervisor's paper/live mismatch guard catches the
                                  # cross-wire.
IB_CONNECT_TIMEOUT_SECONDS=5      # TCP + client-ready probe budget
IB_REQUEST_TIMEOUT_SECONDS=30     # per-request qualification budget
IB_INSTRUMENT_CLIENT_ID=999       # default for one-shot CLI connections (see config.py:125-128)
```

> **Important — what `core/config.py` actually reads.** The `Settings` model on the FastAPI/supervisor process exposes only `ib_host` and `ib_port`. Both accept aliases: `ib_port` is populated from `IB_PORT` **or** `IB_GATEWAY_PORT_PAPER` (whichever is set first), but **`IB_GATEWAY_PORT_LIVE` is NOT a `Settings` field** — it is set on the `live-supervisor` Compose service for downstream tooling (e.g., the gnzsnz IB Gateway image's socat proxy), not consumed by `Settings`. To run live, override `IB_PORT=4003` (socat proxy) — `IB_GATEWAY_PORT_LIVE` in CLAUDE.md is documentation-only and not read by Pydantic Settings. Treat `core/config.py` as ground truth.

Live subprocesses derive their `client_id` from a 31-bit hash of the deployment slug (`live_node_config.py::_derive_client_id`) rather than reusing the default `IB_INSTRUMENT_CLIENT_ID=999`, to avoid silent disconnects when two TradingNodes share a `client_id` (NautilusTrader gotcha #3).

**Dev-compose port reality (`docker-compose.dev.yml:200-282`).** The `live-supervisor` service is configured with `IB_GATEWAY_PORT_PAPER=4004`, `IB_GATEWAY_PORT_LIVE=4003`, and `IB_PORT=${IB_PORT:-4004}`. The `ib-gateway` container (gnzsnz/ib-gateway:stable) terminates at the raw IB ports (4001/4002) inside the container; socat proxies expose them on 4003/4004 to the supervisor. So in practice, `nc localhost 4004` from the supervisor reaches paper, and `nc localhost 4003` reaches live. The validator accepts both raw and proxied — paper = `{4002, 4004}`, live = `{4001, 4003}` — so either set is correct as long as it pairs with the right account prefix.

**Starting IB Gateway.** It's behind a Compose profile so it doesn't run in the default dev stack:

```bash
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d
```

Without this, every deployment fails at the connect step.

### 1.2a Multi-IB-login fabric (`ib_login_key`)

A single MSAI deployment can route to **multiple IB Gateway containers**, one per IB user login. The mechanism:

- `LiveDeployment.ib_login_key` (`String(64)`, indexed, NOT NULL) is part of the `PortfolioStartRequest` payload (see `backend/src/msai/schemas/live.py:32`). It's the IB TWS userid this deployment will execute under.
- The supervisor reads `GATEWAY_CONFIG` env var on startup (`docker-compose.dev.yml:222`). Format: `login1:host1:port1,login2:host2:port2` — e.g. `marin1016test:ib-gateway-paper:4004,mslvp000:ib-gateway-lvp:4003`.
- `GatewayRouter.resolve(ib_login_key)` (`backend/src/msai/services/live/gateway_router.py:37`) returns the `(host, port)` for that login. If no entry exists, it raises `ValueError`.
- When `gateway_router.is_multi_login` is True, the supervisor's payload factory (`live_supervisor/__main__.py:169-170`) overrides the default `IB_HOST`/`IB_PORT` with the per-login endpoint before constructing the `TradingNodeConfig`.
- Crucially, deployments that share an `ib_login_key` are **multiplexed onto a single Nautilus subprocess** via Nautilus's multi-account `exec_clients` feature (PR #3194, Nautilus 1.225+). This is what the model docstring at `live_deployment.py:115-119` describes.

If you only have one IB login, leave `GATEWAY_CONFIG` empty and the legacy single-gateway path applies (`process_manager.py:525-527` falls through to `"default"`). The `--ib-login-key` CLI flag (`cli.py:441`) is the operator-facing override; the API accepts it as an optional field on `PortfolioStartRequest`.

### 1.3 Identity-based warm restart

The signature `SHA256({user_id, portfolio_revision_id, account_id, paper_trading})` is the identity. If you stop a deployment and start it again with all four values the same, you get the **same** `LiveDeployment` row, the **same** `deployment_slug`, the **same** `trader_id`, the **same** `message_bus_stream`. NautilusTrader's cache rehydrates against the same `trader_id`, so reconciliation against IB picks up where it left off. Change any of the four fields and you get a new deployment with cold state.

> **Naming note.** This doc says `user_id` colloquially, but the actual field on `PortfolioDeploymentIdentity` is named `started_by` and is canonicalized from the JWT user (`canonicalize_user_id` in `deployment_identity.py`). If you're grepping for `user_id=...` on the dataclass you won't find it — search for `started_by`. The semantic content is identical.

The signature is computed in `backend/src/msai/services/live/deployment_identity.py`:

- `derive_portfolio_deployment_identity(user_id, portfolio_revision_id, account_id, paper_trading, user_sub)` — builds the dataclass.
- `PortfolioDeploymentIdentity.signature()` — returns the SHA256 hex of the canonical-JSON tuple.
- `derive_deployment_slug()` — generates the 16-hex-char slug.
- `derive_trader_id(slug)` — returns `f"MSAI-{slug}"`.
- `derive_strategy_id_full(strategy_class, slug, order_index)` — derives the per-member Nautilus strategy id.
- `derive_message_bus_stream(slug)` — names the per-deployment event stream.

### 1.4 Trade deduplication

NautilusTrader gotcha #19 applies here: on restart, IB reports historical fills via reconciliation. Without deduplication, those fills produce duplicate `Trade` rows. The fix is a **partial unique index** `ix_trades_broker_trade_id_deployment` on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL`. Live trades have a non-null `broker_trade_id` (assigned by IB) and collide on the index; backtest trades have null `broker_trade_id` and don't collide with anything. Reconciliation replay is therefore idempotent.

See `backend/src/msai/models/trade.py:30-91`.

### 1.5 Why revisions are immutable

This is the single architectural decision that pays back the most when something goes wrong. A revision is the answer to "what was running at 09:34:17 on a given Tuesday?" — and the only way to get that answer to stay the same as long as the row exists is to make the row immutable.

Concretely, an immutable revision means:

- The `composition_hash` is permanent. Anyone reproducing a result years later can re-derive it from the same hash.
- Rollback to a prior strategy mix is just "deploy the prior revision id" — no rebuild, no re-test, no race against partial state.
- Audit becomes mechanical: every `LiveDeployment` row carries `portfolio_revision_id`; every order audit row carries `(strategy_code_hash, git_sha)`; the chain from "an order went to IB" → "this strategy with this code" → "this revision with this composition" is traceable without interpretation.
- The "edit-while-running" failure mode is impossible by construction. If you want a different mix, you snapshot a new revision and deploy that — the old one is preserved unchanged.

The cost: an extra step in the workflow ("freeze before deploy"). The benefit: no class of bug where the running system mutates out from under the audit log. We pay the cost.

### 1.6 The supervisor and the command bus

The **live supervisor** is a separate process — not a child of FastAPI, not an arq worker. It runs as its own service (`live_supervisor` in Compose). It owns four background tasks:

1. **Command consumer** — `LiveCommandBus.consume` reads START/STOP commands from `msai:live:commands` via `XREADGROUP` (consumer group `live-supervisor`).
2. **Reap loop** — `ProcessManager.reap_loop` surfaces subprocess exit codes.
3. **Heartbeat monitor** — `HeartbeatMonitor.run_forever` flips stale rows to `failed`. Defaults (`heartbeat_monitor.py:77-78`): `stale_seconds=30`, `sleep_interval_s=10.0`. Worst-case detection latency is `stale_seconds + sleep_interval_s ≈ 40s`, not minutes.
4. **Startup watchdog** — `ProcessManager.watchdog_loop` (the wrapper) calls `watchdog_once` on each pass and SIGKILLs subprocesses wedged in `starting` past `startup_hard_timeout_s`.

The supervisor talks to FastAPI exclusively through Redis (commands in, status via DB rows the API polls). API crash does not kill live trading. Supervisor crash does not crash the API. Subprocess crash does not crash the supervisor — it's reaped and the row flips to `failed`.

**Wire contract for the command bus** (`backend/src/msai/services/live_command_bus.py`):

| Constant                  | Value                    | Purpose                                    |
| ------------------------- | ------------------------ | ------------------------------------------ |
| `LIVE_COMMAND_STREAM`     | `msai:live:commands`     | Primary stream for START/STOP              |
| `LIVE_COMMAND_GROUP`      | `live-supervisor`        | Consumer group (one pod per entry)         |
| `LIVE_COMMAND_DLQ_STREAM` | `msai:live:commands:dlq` | Dead-letter queue for poison messages      |
| `MAX_DELIVERY_ATTEMPTS`   | `5`                      | Deliveries before an entry is moved to DLQ |

PEL recovery is via `XAUTOCLAIM` — Redis Streams don't auto-redeliver unacked entries (Kafka does; Redis doesn't). The supervisor reclaims un-ACKed entries on startup and at `recovery_interval_s` steady state. ACK is **explicit** and only happens when the handler returned `True` and didn't raise — never in a `finally`. Decision #13 in the plan was to ACK only on real success; everything else stays in PEL for retry.

**Supervisor shutdown is intentionally hands-off.** The supervisor does **not** SIGTERM its child subprocesses on shutdown. Children are owned by the OS container (Compose / systemd). When the supervisor restarts, it re-discovers surviving children via heartbeat-fresh `LiveNodeProcess` rows. This decouples supervisor lifecycle from subprocess lifecycle — restarting the supervisor never disturbs running strategies, which is exactly what you want during a deploy.

**Why the supervisor isn't a FastAPI background task.** Three reasons, in order of operational importance:

1. **API crash must not kill live trading.** If the supervisor were inside the FastAPI process, a `503` on the API would cascade to a SIGTERM of every strategy. Splitting them means the API is a thin command-publication layer; the durability of trading is independent.
2. **Hot-reload would tear strategies down.** Uvicorn `--reload` watches the source tree and respawns the API on changes. A supervisor running inside it would respawn too, killing every subprocess. Strategies need stability that's not yoked to dev ergonomics.
3. **The reap/heartbeat/watchdog loops want their own event loop.** Mixing them with the API event loop has caused starvation issues in past versions; isolating them is cleaner.

---

## 2. The three surfaces — parity table

Same operations across API, CLI, and UI. This is the production-money doc, so this table is long on purpose.

| Intent                                  | API                                                         | CLI                                                  | UI                                                                | Observe / Verify                                                                                               |
| --------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Create live portfolio                   | `POST /api/v1/live-portfolios` `LivePortfolioCreateRequest` | n/a (CLI scaffold not exposed)                       | `/live-trading` → "+ New portfolio"                               | `GET /api/v1/live-portfolios` returns the new row.                                                             |
| List live portfolios                    | `GET /api/v1/live-portfolios`                               | n/a                                                  | `/live-trading` left rail                                         | Sorted newest-first; rows include the active frozen revision.                                                  |
| Get one live portfolio                  | `GET /api/v1/live-portfolios/{id}`                          | n/a                                                  | `/live-trading` → portfolio detail                                | Carries portfolio + active revision (latest frozen).                                                           |
| Add strategy to draft revision          | `POST /api/v1/live-portfolios/{id}/strategies`              | n/a                                                  | `/live-trading` portfolio detail → "Add strategy"                 | Lazy-creates the draft if none exists; `GET /members` lists current members.                                   |
| List draft revision members             | `GET /api/v1/live-portfolios/{id}/members`                  | n/a                                                  | `/live-trading` portfolio detail tab                              | Returns `LivePortfolioMemberResponse[]`.                                                                       |
| Snapshot (freeze) draft revision        | `POST /api/v1/live-portfolios/{id}/snapshot`                | n/a                                                  | `/live-trading` → "Freeze revision"                               | Returns frozen revision with `composition_hash`. Idempotent: same membership returns existing row.             |
| Deploy frozen revision                  | `POST /api/v1/live/start-portfolio` `PortfolioStartRequest` | `uv run msai live start`                             | `/live-trading` portfolio detail → "Deploy"                       | `LiveDeployment` row appears; `LiveNodeProcess` row transitions `starting → building → ready → running`.       |
| List active deployments (all)           | `GET /api/v1/live/status`                                   | `uv run msai live status`                            | `/live-trading` deployment list                                   | One row per active `LiveDeployment` with current `status` and process info.                                    |
| Get one deployment status               | `GET /api/v1/live/status/{deployment_id}`                   | n/a                                                  | `/live-trading` → deployment detail                               | Carries `LiveDeployment` + most-recent `LiveNodeProcess` + heartbeat freshness.                                |
| List open positions across deployments  | `GET /api/v1/live/positions`                                | n/a (use `msai account positions` for IB-side truth) | `/live-trading` Positions panel (REST fallback while WS connects) | Aggregates open positions from active deployments.                                                             |
| List recent executions                  | `GET /api/v1/live/trades` (paginated, last 50)              | n/a                                                  | `/live-trading` Trades panel                                      | Trade rows newest-first; partial unique index on `(deployment_id, broker_trade_id)` keeps reconciliation safe. |
| Audit trail per deployment              | `GET /api/v1/live/audits/{deployment_id}`                   | n/a                                                  | `/live-trading` deployment → "Audit" tab                          | Order attempt audit rows in submission order; durable across restarts.                                         |
| Stop a deployment                       | `POST /api/v1/live/stop` `LiveStopRequest`                  | `uv run msai live stop`                              | `/live-trading` deployment → "Stop"                               | 200 (idempotent). `LiveNodeProcess` flips to `stopped`. **Positions are not flattened** (gotcha #13).          |
| Emergency halt all deployments          | `POST /api/v1/live/kill-all`                                | `uv run msai live kill-all`                          | `/live-trading` red **KILL** button (confirmed)                   | Sets `msai:risk:halt` (24h TTL). Pushes STOP to every active row. New deploys return 503 until cleared.        |
| Resume after halt                       | `POST /api/v1/live/resume`                                  | n/a                                                  | n/a (operator action via API or CLI)                              | Clears `msai:risk:halt`. Subsequent `/start-portfolio` calls proceed.                                          |
| Account summary (IB-side)               | `GET /api/v1/account/summary`                               | `uv run msai account summary`                        | `/live-trading` account widget · `/dashboard` · `/settings`       | NetLiq, available funds, buying power, maintenance margin from IB.                                             |
| Account positions (IB-side)             | `GET /api/v1/account/portfolio`                             | `uv run msai account positions`                      | `/live-trading` Positions tab · `/dashboard`                      | IB's truth — independent of MSAI's audit trail. Reconcile against `/live/positions` if they diverge.           |
| IB Gateway connection health            | `GET /api/v1/account/health`                                | `uv run msai account health`                         | `/settings` health badge · `/live-trading` header                 | `gateway_connected: bool`, `consecutive_failures: int` (probe runs every 30s).                                 |
| Recent alerts (operator-visible events) | `GET /api/v1/alerts/`                                       | n/a                                                  | `/live-trading` alert toasts                                      | File-backed (opportunistic), newest-first, capped at 200 per request.                                          |
| Real-time stream for a deployment       | `WS /api/v1/live/stream/{deployment_id}`                    | n/a (read-only)                                      | `/live-trading` deployment detail (auto-connect)                  | Covered in [How Real-Time Monitoring Works](how-real-time-monitoring-works.md).                                |

A few notes on why the table is uneven:

- **Live-portfolio CRUD has no CLI yet.** Strategies are git-only and revisions are a control-plane concept; the CLI was prioritized for ops actions (`live start`, `live stop`, `live kill-all`, `account *`). If you need scripted portfolio composition, hit the API directly with `curl` + `MSAI_API_KEY`.
- **There is no `account add` endpoint.** IB accounts are environment-configured (`IB_ACCOUNT_ID` env var). Adding a new account = redeploy the FastAPI service with a new env var. The plan reflected an aspirational `POST /account/add` and `msai account add` — those don't exist, on purpose. The account is a config concern, not a runtime resource.
- **`/live/start` (singular) is deprecated** — it returns 410 Gone. Use `/live/start-portfolio` always. The single-strategy deploy path was removed when the portfolio-per-account model became canonical.

---

## 3. Internal sequence — `POST /live/start-portfolio` and the supervisor

This is the load-bearing flow. One ASCII diagram, then the kill-all flow underneath.

### 3.1 Deploy

```
client                       FastAPI                           Postgres                Redis              live_supervisor              IB Gateway
  │                             │                                  │                      │                      │                          │
  │  POST /live/start-portfolio │                                  │                      │                      │                          │
  ├────────────────────────────►│                                  │                      │                      │                          │
  │                             │  ┌─ load LivePortfolioRevision ─┐│                      │                      │                          │
  │                             │  │  WHERE id = revision_id      ││                      │                      │                          │
  │                             │  │  REQUIRE is_frozen = true    ├┼─────────────────────►│                      │                          │
  │                             │  └──────────────────────────────┘│                      │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  load member strategies          │                      │                      │                          │
  │                             │  + compute code_hashes           │                      │                      │                          │
  │                             │  aggregate instruments + configs │                      │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  ─── L1 HTTP Idempotency-Key ──► │                      │                      │                          │
  │                             │       SETNX msai:idem:{key}      │                      │                      │                          │
  │                             │       Reserved | in_flight | mismatch                   │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  ─── L2 halt flag check ────────►│                      │                      │                          │
  │                             │       GET msai:risk:halt         │                      │                      │                          │
  │                             │       set ⇒ 503 halt_active      │                      │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  ─── L3 identity upsert ────────────────────────────────►│                     │                          │
  │                             │       compute identity_signature │                      │                      │                          │
  │                             │       ON CONFLICT(identity_signature)                   │                      │                          │
  │                             │       UPSERT LiveDeployment      │                      │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  upsert LiveDeploymentStrategy[] │                      │                      │                          │
  │                             │                                  │                      │                      │                          │
  │                             │  XADD msai:live:commands * START {deployment_id, slug, payload}                │                          │
  │                             ├──────────────────────────────────────────────────────────►                     │                          │
  │                             │                                                                                │                          │
  │                             │  register message bus stream (per-deployment events)    │                      │                          │
  │                             │                                                          │                      │                          │
  │                             │  ┌─ poll every 0.25s for 60s ─────────────────────────────────────────────────────────────┐                │
  │                             │  │ SELECT status FROM live_node_processes WHERE deployment_id = ?               │                          │
  │                             │  │   ready (cold start)         ⇒ EndpointOutcome.ready             ⇒ 201      │                          │
  │                             │  │   already_active (warm)      ⇒ EndpointOutcome.already_active    ⇒ 200      │                          │
  │                             │  │   failed (registry class)    ⇒ EndpointOutcome.registry_permanent⇒ 422      │                          │
  │                             │  │   failed (other permanent)   ⇒ EndpointOutcome.permanent_failure ⇒ 503      │                          │
  │                             │  │   transient supervisor error ⇒ EndpointOutcome.spawn_failed_…    ⇒ 503      │                          │
  │                             │  │   poll budget exhausted      ⇒ EndpointOutcome.api_poll_timeout ⇒ 504      │                          │
  │                             │  └─────────────────────────────────────────────────────────────────────────────┘                          │
  │  201 / 200 / 422 / 425 /    │                                                                                │                          │
  │  503 / 504                  │                                                                                │                          │
  │◄────────────────────────────│                                                                                │                          │
  │                             │                                                                                │                          │
  │                                                                                                              │  XREADGROUP live-supervisor│
  │                                                                                                              │  pop START                 │
  │                                                                                                              │                            │
  │                                                                                                              │  ProcessManager.spawn(…)   │
  │                                                                                                              │  ┌─ re-check halt ────────┐│
  │                                                                                                              │  │ GET msai:risk:halt     ││
  │                                                                                                              │  │ set ⇒ NACK (do not run)││
  │                                                                                                              │  └────────────────────────┘│
  │                                                                                                              │                            │
  │                                                                                                              │  INSERT LiveNodeProcess    │
  │                                                                                                              │   status=starting          │
  │                                                                                                              │                            │
  │                                                                                                              │  os.fork() / subprocess.spawn
  │                                                                                                              │   target = TradingNode.run_async
  │                                                                                                              │                            │
  │                                                                                                              │   ┌─ subprocess ─────────┐ │
  │                                                                                                              │   │ build TradingNodeCfg │ │
  │                                                                                                              │   │ port ∈ {4002,4004} pa│ │
  │                                                                                                              │   │ port ∈ {4001,4003} li│ │
  │                                                                                                              │   │ client_id = hash(slug)│ │
  │                                                                                                              │   │                      │ │
  │                                                                                                              │   │ connect IB Gateway   ├─┼─►
  │                                                                                                              │   │ status=building       │ │
  │                                                                                                              │   │                      │ │
  │                                                                                                              │   │ pre-load instruments │ │
  │                                                                                                              │   │ (no dynamic loading) │ │
  │                                                                                                              │   │                      │ │
  │                                                                                                              │   │ load strategies      │ │
  │                                                                                                              │   │ wrapped by           │ │
  │                                                                                                              │   │ FailureIsolated-     │ │
  │                                                                                                              │   │ Strategy             │ │
  │                                                                                                              │   │                      │ │
  │                                                                                                              │   │ reconciliation       │ │
  │                                                                                                              │   │ status=ready         │ │
  │                                                                                                              │   │                      │ │
  │                                                                                                              │   │ live event loop      │ │
  │                                                                                                              │   │ status=running       │ │
  │                                                                                                              │   │ heartbeat every Ns   │ │
  │                                                                                                              │   └──────────────────────┘ │
  │                                                                                                              │                            │
  │                                                                                                              │  ACK command (only on True)│
```

A few mechanics worth calling out separately because they're easy to miss in the timeline:

- **Why the API polls.** The API doesn't fork the subprocess directly — that's the supervisor's job. After publishing the START command, the API watches `live_node_processes.status` for the row to settle. If it doesn't reach `ready` in 60s (`START_POLL_TIMEOUT_S`), the API returns **`504` via `EndpointOutcome.api_poll_timeout`** (not cacheable — a retry can re-attempt). The deployment may still come up; the operator gets a 504 with a "did not reach 'ready' within the poll timeout" detail rather than a hang.
- **Why the supervisor re-checks halt.** Layer 2 of the kill-all defense lives in `ProcessManager.spawn`. There's a TOCTOU window between the API checking the halt flag and the supervisor reading the START command. The re-check closes that window: even if the API let the START through, the supervisor will refuse to spawn if halt is set.
- **Why `client_id` is derived from the slug.** Two TradingNodes connecting to the same IB Gateway with the same `client_id` silently disconnect each other (NautilusTrader gotcha #3). Hashing the slug guarantees uniqueness across deployments running concurrently.
- **Why instruments are pre-loaded.** Dynamic instrument loading on the live event loop is one round-trip per instrument and blocks the event loop (gotcha #11). The subprocess pre-loads everything before flipping to `running`.
- **Why `FailureIsolatedStrategy`.** When you run multiple strategies in one TradingNode (`TradingNodeConfig.strategies=[…]`), an unhandled exception in one strategy's event handler will tear down the node by default. `FailureIsolatedStrategy` wraps the event handlers via `__init_subclass__` so a strategy crash flips that strategy to a halt state without taking down the others. See the architecture note in `CLAUDE.md` (§ Portfolio-per-account).

### 3.2 Kill-all (the 4-layer defense)

```
client                  FastAPI                       Redis                       Postgres                   live_supervisor
  │                        │                            │                            │                            │
  │ POST /live/kill-all    │                            │                            │                            │
  ├───────────────────────►│                            │                            │                            │
  │                        │                            │                            │                            │
  │                        │ ── L1 ─ SETEX 86400 ──────►│                            │                            │
  │                        │       msai:risk:halt = 1   │                            │                            │
  │                        │                            │                            │                            │
  │                        │ ── L2 ─ supervisor next spawn re-checks halt           │                            │
  │                        │       (already-running supervisors will refuse new spawns)                          │
  │                        │                                                         │                            │
  │                        │ ── L3 ─ for each active LiveNodeProcess row:           │                            │
  │                        │       XADD msai:live:commands * STOP {deployment_id} ─►│                            │
  │                        │                                                         │                            │
  │                        │                                                         │   XREADGROUP STOP commands  │
  │                        │                                                         │   ProcessManager.stop      │
  │                        │                                                         │   SIGTERM subprocess       │
  │                        │                                                         │                            │
  │                        │ ── L4 ─ subprocess receives SIGTERM                    │                            │
  │                        │       RiskAwareStrategy mixin halt-check:              │                            │
  │                        │       refuses any new orders                           │                            │
  │                        │                                                         │                            │
  │ 200 OK                 │                                                         │                            │
  │◄───────────────────────│                                                         │                            │
```

Stopping is push-based (the API publishes STOP), and the halt flag is the persistent guard. After a kill-all, the halt flag stays set for 24 hours — `/live/start-portfolio` will return 503 until you call `/live/resume` or the TTL expires. **Stopping does not flatten positions.** Whatever was open at kill time is still open at IB; you handle that separately (manually flatten through IB, or restart with the same revision and let the strategy continue from where it stopped). NautilusTrader gotcha #13 is the controlling fact.

> **Heads-up on a stale docstring.** The `live_kill_all` docstring at `backend/src/msai/api/live.py:761-801` describes Layer 3 as "the supervisor SIGTERMs the subprocess and Nautilus's `manage_stop=True` flatten loop closes positions automatically. Latency from `/kill-all` to flatten is < 5 seconds." That claim is wrong. There is no auto-flatten path — `live_kill_all` only sets the halt flag and publishes STOP commands, and Nautilus's `stop()` does not close positions. Trust the §3.2 ASCII (and gotcha #13), not the docstring. (Tracked for cleanup; behavior reaches the right outcome.)

---

## 4. See, Verify, Troubleshoot

Three layers of observability — frontend, CLI, and direct stack inspection.

### 4.1 The `/live-trading` page

Route: `frontend/src/app/live-trading/page.tsx`. Sections:

- **Deployment list** — fetched via `getLiveStatus(token)` from `GET /api/v1/live/status`. One card per active deployment with status, account, paper/live indicator.
- **WebSocket stream** — `useLiveStream(activeRealDeployment?.id, {token})` connects to `/api/v1/live/stream/{deployment_id}` and renders snapshot + live events. REST fallback via `getLivePositions(token)` runs in parallel until the WS is hot.
- **`<KillSwitch>`** — confirmed-action button that POSTs to `/api/v1/live/kill-all`. Confirmation modal is intentional friction.
- **`<StrategyStatus>`** — per-member status from the WS snapshot (running/halted/failed).
- **`<PositionsTable>`** — merged positions from the WS stream and the REST fallback.
- **Account widgets** — `/api/v1/account/summary`, `/api/v1/account/portfolio`, `/api/v1/account/health` for IB-side truth.

### 4.2 CLI

```bash
uv run msai live start \
    --portfolio-revision-id <UUID> \
    --account-id DU0000000 \
    --paper \
    --ib-login-key marin1016test  # POST /live/start-portfolio (cli.py:436)
uv run msai live status          # tabulated active deployments
uv run msai live stop            # stop one deployment by id
uv run msai live kill-all        # POST /live/kill-all (with confirmation prompt)
uv run msai account summary      # GET /account/summary
uv run msai account positions    # GET /account/portfolio
uv run msai account health       # GET /account/health
```

The `live` and `account` sub-apps are both wrappers over the API (see `backend/src/msai/cli.py` lines 91–620). They take `MSAI_API_KEY` from the environment as the default auth — you can override with a Bearer JWT.

**Operator drill — typical session.** A pre-market check on a Monday looks like this:

```bash
# 1. Confirm the stack is up.
curl -sf http://localhost:8800/health
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml ps

# 2. Confirm IB Gateway is connected and the probe task is alive.
uv run msai account health
# → {"status": "healthy", "gateway_connected": true, "consecutive_failures": "0"}

# 3. Confirm what's deployed.
uv run msai live status

# 4. Confirm IB-side truth matches.
uv run msai account summary
uv run msai account positions

# 5. If anything is off, kill before market open and investigate.
uv run msai live kill-all
# (then call /live/resume once you're confident before re-deploying)
```

This sequence is the minimum due diligence before letting strategies trade for the day. If steps 2–4 disagree, do not let the day open — kill, investigate, then resume.

### 4.3 Direct stack inspection

When something is wrong and the surfaces above aren't telling you enough.

**IB Gateway logs** — `docker compose -f docker-compose.dev.yml logs -f ib-gateway` (after the broker profile is up). The connect / qualify / order-route messages live there. Reconciliation is verbose — expect a wall of "ack" lines on startup.

**Supervisor liveness** — the API uses `XINFO CONSUMERS msai:live:commands live-supervisor` to check that a consumer exists with `idle < 15000ms`. You can run the same check yourself:

```bash
docker compose -f docker-compose.dev.yml exec redis \
  redis-cli XINFO CONSUMERS msai:live:commands live-supervisor
```

If the only consumer's `idle` is climbing past 15000, the supervisor is wedged. `docker compose ... restart live-supervisor` is the operator action.

**Audit trail** — `GET /api/v1/live/audits/{deployment_id}` returns the order-attempt audit rows. Every order submission persists `(strategy_code_hash, git_sha)` before being sent to IB; the row updates with broker IDs once the OrderFilled event lands.

**Heartbeat freshness** — `LiveNodeProcess.last_heartbeat_at` is updated by the subprocess; the heartbeat monitor flips stale rows to `failed` after `stale_seconds=30` (default in `heartbeat_monitor.py:77`) and sweeps every `sleep_interval_s=10`. Worst-case detection: ~40 seconds (one stale window plus one sweep interval). If a row is stuck in `running` but the subprocess is dead, the monitor will catch it inside that window.

---

## 5. Common failures

Each one has been seen at least once in the last drill cycle. Read this section first when you're triaging.

### 5.1 Account / port mismatch (the silent killer)

NautilusTrader gotcha #6. Connecting a paper-prefix account (`DU…` or `DF…`) to a live port (`4001`/`4003`), or a `U…` live account to a paper port (`4002`/`4004`), does **not** raise on the IB side — IB Gateway accepts the TCP connection and quietly returns no data. The strategy starts, never receives a bar, never trades.

**The fix lives in code**: `validate_port_account_consistency` in `backend/src/msai/services/nautilus/ib_port_validator.py` checks `(port ∈ IB_PAPER_PORTS, account.startswith(IB_PAPER_PREFIXES))` and refuses to construct the `TradingNodeConfig` on mismatch. The subprocess crashes at config-build time. Don't disable that check.

**To verify manually:** if a deployment is `running` for a paper account, the configured port must be in `{4002, 4004}`. If it's running for a live account, the configured port must be in `{4001, 4003}`. Mismatch → the subprocess crashes immediately and the row flips to `failed` within seconds.

### 5.2 Instrument-bootstrap timeout

If the registry is missing a symbol the revision needs, `lookup_for_live` raises `RegistryMissError` — `backend/src/msai/services/nautilus/security_master/live_resolver.py`. The resolution actually happens **inside the supervisor's payload factory** (`live_supervisor/__main__.py:302`), not at the API boundary; the API observes the failure by polling `live_node_processes.status='failed'` and mapping `failure_kind` through `EndpointOutcome.registry_permanent_failure` to a 422. Operationally that means: when you're triaging a 422 with a `REGISTRY_MISS`/`REGISTRY_INCOMPLETE`/`UNSUPPORTED_ASSET_CLASS`/`AMBIGUOUS_REGISTRY` failure_kind, look in the **supervisor logs**, not the API logs.

**Why this happens:** the live path is a pure-read of `instrument_definitions` + `instrument_aliases`. There is no on-the-fly IB qualification. Cold misses are an **operator action**:

```bash
uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers
```

This warms the registry from IB. Re-deploy after.

Other related exceptions surfaced by the resolver: `RegistryIncompleteError` (matched but missing required fields), `UnsupportedAssetClassError` (live trading not enabled for asset class), `AmbiguousRegistryError` (a bare symbol matched multiple definitions — disambiguate with venue suffix).

### 5.3 IB ClientID collision

NautilusTrader gotcha #3. Two TradingNodes connecting to the same IB Gateway with the same `client_id` silently disconnect each other. Symptom: a deployment "comes up", then its connection drops within seconds, then comes back when the other one drops, and so on, forever.

**Why we don't hit this normally:** subprocess `client_id` is derived from a hash of the deployment slug, so two concurrent deployments get different ids. Where it does happen: when an external script (`msai instruments refresh`, an ad-hoc IB qualifier) connects with the default `IB_CLIENT_ID=999` while a live deployment that for some reason ended up with `client_id=999` is running. The mitigation is to keep slug-derived ids strictly distinct from the default.

### 5.4 Reconciliation timeout

NautilusTrader gotcha #10. `LiveExecEngineConfig(reconciliation=True)` runs an async reconcile against IB on startup. If it times out, the node still flips to `running` — but with stale state. New orders submitted during the window can fight the reconciled state.

**Mitigation in code:** the subprocess does an explicit reconciliation completion check before allowing the trader to submit orders. If it didn't complete, the row stays in `building` past `startup_hard_timeout_s` and the watchdog SIGKILLs it. The deployment ends up `failed` rather than silently broken.

### 5.5 Halt flag set blocks new deployments

This is by design but easy to forget. After `/kill-all`, `msai:risk:halt` is set with a 24h TTL. Any subsequent `/live/start-portfolio` returns `503 halt_active` until you call `POST /api/v1/live/resume` (or wait out the TTL). The `/live-trading` page shows a halt banner when the flag is set.

**Operator action:** `curl -XPOST -H "X-API-Key: $MSAI_API_KEY" http://localhost:8800/api/v1/live/resume` clears the flag.

### 5.6 Supervisor not running

The API checks `XINFO CONSUMERS` on every `/live/start-portfolio` call. If no consumer exists in the `live-supervisor` group, or the only consumer's idle exceeds `_SUPERVISOR_MAX_IDLE_MS = 15000`, the API returns `503 supervisor_unavailable` and the start does not proceed. START commands published while the supervisor is down sit in the stream and are picked up via PEL recovery when the supervisor returns — provided they haven't exceeded `MAX_DELIVERY_ATTEMPTS = 5`.

**Operator action:** `docker compose -f docker-compose.dev.yml restart live-supervisor`. If the supervisor is crashing on startup, check `docker compose -f docker-compose.dev.yml logs live-supervisor` — the most common cause is an unreachable Redis or Postgres at startup.

### 5.7 IB probe task not started

The `_ib_probe` task in `backend/src/msai/api/account.py` is a module-level singleton spawned by FastAPI's lifespan. If the lifespan didn't run (e.g., during certain test harnesses) or `start_ib_probe_task()` was bypassed, `/account/health` always returns `gateway_connected=false` regardless of actual state. This bit us on Drill 2026-04-15.

**Sanity check:** if all three are simultaneously true — IB Gateway is up, deployments are running and producing fills, `/account/health` says `gateway_connected=false` — the probe task isn't running. Restarting the FastAPI service fixes it.

### 5.8 Cold cache on DLQ-bounce

If a START command exceeds `MAX_DELIVERY_ATTEMPTS = 5` it goes to `msai:live:commands:dlq` with diagnostic metadata and the deployment never starts. The 5-attempt ceiling exists so a poison message can't loop forever; the trade-off is that a transient failure that recurs five times will move the command to the DLQ and you'll need to re-call `/live/start-portfolio` to re-publish it.

**To inspect the DLQ:**

```bash
docker compose -f docker-compose.dev.yml exec redis \
  redis-cli XRANGE msai:live:commands:dlq - + COUNT 20
```

Each DLQ entry carries the original payload plus diagnostic fields: `failure_reason`, `delivery_count`, `first_seen_at`, `last_attempt_at`. If the same command keeps DLQ-bouncing, the cause is in the supervisor logs — pull `docker compose ... logs live-supervisor` and grep for the `deployment_id`.

### 5.9 Drift between MSAI's audit and IB's truth

`/api/v1/live/positions` is MSAI's reconstruction. `/api/v1/account/portfolio` is IB's truth. When they diverge, IB wins. Most divergences come from one of three causes:

1. **Manual order placed at IB outside MSAI** — e.g., someone hit "flatten" in TWS. MSAI has no row for it; reconciliation will pick it up at the next subprocess restart, but until then the dashboard is wrong.
2. **A trade landed during a window where the message bus stream was down** — the projection consumer drops the event from its in-flight store; reconciliation on next restart catches up.
3. **Reconciliation timeout (gotcha #10)** — the subprocess flipped to `running` with stale state. Stop and restart the deployment; the warm-restart path reuses the same `trader_id` and reconciles cleanly.

The operator action when in doubt: trust IB, stop the deployment, restart with the same identity. Warm restart will reconcile.

---

## 6. Idempotency / Retry behavior

Three layers, each with a different concern.

### 6.1 Layer 1 — HTTP `Idempotency-Key`

Every `/live/start-portfolio` accepts an optional `Idempotency-Key` header. The API does a Redis `SETNX` on `msai:idem:{key}` with three outcomes (factory definitions in `backend/src/msai/services/live/idempotency.py:69-287`):

- **`Reserved`** — key didn't exist; we hold the reservation and proceed with the request. The cached body of the eventual response is associated with the key for replay.
- **`in_flight`** — the key exists and the request is still processing; the API returns **`HTTP 425 Too Early`** with the in-flight signature so the client knows it was the same request. Not cacheable. (`idempotency.py:136-146`)
- **`body_mismatch`** — the key exists but the request body's hash doesn't match what was stored on first call. Returns **`HTTP 422`** (not 409 — a body-mismatch caller does not own the reservation slot, so caching this 422 would overwrite the original correct cached response at the same key). Not cacheable. (`idempotency.py:184-197`)

Same key + same body + completed first call → cached response replayed. The key has a TTL (sized to the start budget); after the TTL expires the same key acts as fresh.

**Full status-code map for `POST /live/start-portfolio`** (from `EndpointOutcome` factories):

| Outcome                                          | HTTP code | Cacheable | When                                                                                                                                            |
| ------------------------------------------------ | --------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready` (cold start, subprocess reached `ready`) | **201**   | yes       | First spawn for this identity, subprocess transitioned to `ready` within 60s poll.                                                              |
| `already_active` (warm idempotent retry)         | **200**   | yes       | Most recent `live_node_processes` row is in an active status; no new process spawn.                                                             |
| `body_mismatch`                                  | **422**   | no        | Same `Idempotency-Key` reused with a different request body.                                                                                    |
| `registry_permanent_failure`                     | **422**   | no        | Resolver raised `RegistryMissError` / `RegistryIncompleteError` / etc. in supervisor; operator-correctable.                                     |
| `in_flight`                                      | **425**   | no        | Another request with the same `Idempotency-Key` is currently holding the reservation.                                                           |
| `halt_active`                                    | **503**   | no        | `msai:risk:halt` flag set in Redis. Cleared by `/resume`.                                                                                       |
| `spawn_failed_transient`                         | **503**   | no        | Supervisor payload factory raised a transient error; command stays in PEL for retry.                                                            |
| `permanent_failure`                              | **503**   | yes       | DB row carries a permanent `failure_kind` (`SPAWN_FAILED_PERMANENT`, `RECONCILIATION_FAILED`, `BUILD_TIMEOUT`, `HEARTBEAT_TIMEOUT`, `UNKNOWN`). |
| `api_poll_timeout`                               | **504**   | no        | API waited the full `START_POLL_TIMEOUT_S` (60s); subprocess did not reach `ready`/`failed`.                                                    |

The "cacheable" column matters because the idempotency store only caches outcomes that are deterministic for the (key, body) pair. Transient 503/504 are not cached so the next retry can re-attempt; permanent 503 is cached so a forgotten retry doesn't re-burn the supervisor for an error that won't change.

### 6.2 Layer 2 — halt flag

`GET msai:risk:halt` is a hard short-circuit. If set, the API returns `503 halt_active` immediately, and the supervisor will refuse to spawn even if a START command somehow makes it through. `/kill-all` sets it; `/resume` clears it; the TTL is 24 hours.

### 6.3 Layer 3 — identity-based warm restart

This is the conceptual core. The signature `SHA256({user_id, portfolio_revision_id, account_id, paper_trading})` is computed in the API; the upsert against `LiveDeployment` uses `ON CONFLICT (identity_signature) DO UPDATE` with a stable slug. Three cases:

| Case                                                            | Signature | Outcome                                                                                                                                                                                                        |
| --------------------------------------------------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| First-ever deployment for this (user, revision, account, paper) | New       | INSERT new `LiveDeployment` with new slug. Cold start.                                                                                                                                                         |
| Re-deploy after stop, all four fields the same                  | Identical | Existing `LiveDeployment` row reused. Same slug, same `trader_id`, same `message_bus_stream`. **Warm restart**: cache rehydrates against same trader id; reconciliation picks up open orders from IB.          |
| Re-deploy with any of the four fields different                 | Different | New `LiveDeployment` with new slug. Cold state. (Unique constraint `uq_live_deployments_revision_account` may force you to choose a different account if you're trying to re-run the same revision elsewhere.) |

Trade dedup also lives at this layer: the partial unique index on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL` makes reconciliation replay safe. A restart that re-reports a fill IB already reported will hit the index and skip the duplicate.

### 6.4 Stop and resume idempotency

- `POST /live/stop` is idempotent at 200. Calling it on an already-stopped deployment is a no-op.
- `POST /live/kill-all` is idempotent — multiple calls re-publish STOP commands but don't change state.
- `POST /live/resume` clears the flag if set; calling it when the flag isn't set is a no-op.

---

## 7. Rollback / Repair

### 7.1 Stopping a deployment

`POST /api/v1/live/stop` (or `uv run msai live stop`) sends a STOP command. The supervisor SIGTERMs the subprocess; the subprocess flips through `stopped`. **Stopping does not flatten positions.** Anything open at IB stays open. This is gotcha #13: NautilusTrader's `stop()` means "stop receiving data and stop accepting new orders" — it does not close positions.

If the strategy needs to flatten on stop, do it inside `on_stop()` in the strategy itself (the controlled path), or kill-all and then manually flatten through IB (the panic path).

### 7.2 Kill-all

`POST /api/v1/live/kill-all` is the panic button. Sets `msai:risk:halt` (24h TTL), pushes STOP to every active deployment, and sets the in-strategy refusal flag. Same caveat as 7.1 — positions stay open. Call this when:

- Risk engine reports a breach you don't trust the strategies to handle.
- IB Gateway is misbehaving and you want to stop adding to the position book.
- You're about to redeploy the entire stack and want a clean slate.

After the dust settles, manually flatten what's open at IB, then call `/live/resume` to clear the halt flag.

### 7.3 Rollback to a prior strategy mix

Revisions are immutable on purpose — that's the rollback story. If a freshly-deployed revision is misbehaving:

1. Stop the bad deployment via `/live/stop`.
2. Find the prior frozen revision: `GET /api/v1/live-portfolios/{id}` returns the active (latest) revision; query the database for the full revision list if you need older ones.
3. Deploy the prior revision via `/live/start-portfolio` with the same account.
4. Because the four-tuple (user, revision, account, paper) is different (revision changed), you get a new `LiveDeployment` with a new slug — cold state, but with whatever positions from step 1 still open at IB. Reconciliation will discover them.

You can also snapshot a brand-new revision with the prior membership and deploy that — same effect, fresh `revision_number`.

### 7.4 Account-level rollback

If an account is misbehaving and you want the same revision on a different account:

1. Stop the bad deployment.
2. Call `/live/start-portfolio` with the same `portfolio_revision_id` but a different `account_id`. New identity signature → new deployment. Open positions at the bad account stay there until you flatten manually.

The unique constraint `(portfolio_revision_id, account_id)` keeps you from accidentally double-running the same revision on the same account.

### 7.5 Repair: stuck `LiveNodeProcess` rows

If a process row is stuck in `starting` or `building` past the watchdog interval, the watchdog should SIGKILL the subprocess and flip the row to `failed`. If for some reason that didn't happen (e.g., the supervisor itself crashed during the spawn):

- Restart the supervisor (`docker compose ... restart live-supervisor`). On restart, the supervisor reclaims un-ACKed entries via `XAUTOCLAIM` and the heartbeat monitor will catch the orphan row within ~40s (`stale_seconds=30` + `sleep_interval_s=10`).
- If the heartbeat monitor still doesn't flip it, the row is genuinely orphaned — direct DB intervention is the operator action of last resort. We don't expose an endpoint for that on purpose.

---

## 8. Key files

Citation table — `path:line` (lines may rot; the function and constant names are stable).

| Concern                             | File                                                                  | Line / Symbol                                                                                                                                                                                                                                                                   |
| ----------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Live-portfolio CRUD endpoints       | `backend/src/msai/api/portfolios.py`                                  | `1–262` (POST/GET /live-portfolios; /strategies; /snapshot; /members)                                                                                                                                                                                                           |
| Deploy / stop / kill / status       | `backend/src/msai/api/live.py`                                        | `start_portfolio` 245–663 · `stop` 665–758 · `kill_all` 761–893 · `resume` 894–933 · `status` 935–987 · `status/{id}` 988–1047 · `positions` 1049–1117 · `trades` 1119–1175 · `audits/{id}` 1177–1224                                                                           |
| Supervisor liveness check           | `backend/src/msai/api/live.py`                                        | `126–150` (XINFO CONSUMERS, `_SUPERVISOR_MAX_IDLE_MS`)                                                                                                                                                                                                                          |
| Account API (IB-side truth)         | `backend/src/msai/api/account.py`                                     | `summary` · `portfolio` · `health` · probe task 1–116                                                                                                                                                                                                                           |
| WebSocket stream                    | `backend/src/msai/api/websocket.py`                                   | First-message JWT auth; covered in [How Real-Time Monitoring Works](how-real-time-monitoring-works.md).                                                                                                                                                                         |
| Live portfolio model                | `backend/src/msai/models/live_portfolio.py`                           | `LivePortfolio` (name UNIQUE)                                                                                                                                                                                                                                                   |
| Revision model                      | `backend/src/msai/models/live_portfolio_revision.py`                  | `is_frozen`, `composition_hash`, partial unique idx `uq_one_draft_per_portfolio`                                                                                                                                                                                                |
| Revision member model               | `backend/src/msai/models/live_portfolio_revision_strategy.py`         | `instruments[]`, `weight ∈ (0,1]` CHECK, ordering                                                                                                                                                                                                                               |
| Deployment model                    | `backend/src/msai/models/live_deployment.py`                          | `1–160+` — `deployment_slug`, `identity_signature`, `trader_id`, `message_bus_stream`                                                                                                                                                                                           |
| Process model                       | `backend/src/msai/models/live_node_process.py`                        | per-restart status + heartbeat                                                                                                                                                                                                                                                  |
| Trade dedup                         | `backend/src/msai/models/trade.py`                                    | `30–91` — partial unique idx `ix_trades_broker_trade_id_deployment`                                                                                                                                                                                                             |
| Portfolio service                   | `backend/src/msai/services/live/portfolio_service.py`                 | `create_portfolio`, `add_strategy`, `list_draft_members`                                                                                                                                                                                                                        |
| Revision service                    | `backend/src/msai/services/live/revision_service.py`                  | `snapshot`, `enforce_immutability`, `get_active_revision`                                                                                                                                                                                                                       |
| Deployment identity                 | `backend/src/msai/services/live/deployment_identity.py`               | `derive_portfolio_deployment_identity`, `signature()`, `derive_deployment_slug`, `derive_trader_id`, `derive_strategy_id_full`, `derive_message_bus_stream`                                                                                                                     |
| Live resolver (registry lookup)     | `backend/src/msai/services/nautilus/security_master/live_resolver.py` | `lookup_for_live` (read-only registry)                                                                                                                                                                                                                                          |
| Command bus                         | `backend/src/msai/services/live_command_bus.py`                       | `LIVE_COMMAND_STREAM = "msai:live:commands"`, `MAX_DELIVERY_ATTEMPTS = 5`, PEL recovery via XAUTOCLAIM                                                                                                                                                                          |
| Supervisor main loop                | `backend/src/msai/live_supervisor/main.py`                            | `handle_command(START/STOP)`                                                                                                                                                                                                                                                    |
| ProcessManager                      | `backend/src/msai/live_supervisor/process_manager.py`                 | `spawn`, `stop`, `reap_loop`, `watchdog_loop`                                                                                                                                                                                                                                   |
| HeartbeatMonitor                    | `backend/src/msai/live_supervisor/heartbeat_monitor.py`               | `run_forever` — flips stale rows                                                                                                                                                                                                                                                |
| Schemas (live portfolio)            | `backend/src/msai/schemas/live_portfolio.py`                          | `LivePortfolioCreateRequest`, `LivePortfolioAddStrategyRequest`, `LivePortfolioResponse`, `LivePortfolioMemberResponse`, `LivePortfolioRevisionResponse`                                                                                                                        |
| Schemas (live deployment)           | `backend/src/msai/schemas/live.py`                                    | `PortfolioStartRequest`, `LiveStopRequest`, `LiveStatusResponse`, `LiveDeploymentInfo`, `LiveDeploymentStatusResponse`, `LivePositionsResponse`, `LiveTradesResponse`, `LiveKillAllResponse`                                                                                    |
| IB / app config                     | `backend/src/msai/core/config.py`                                     | `62–128` — `ib_account_id`, `ib_host` (alias `IB_HOST`/`IB_GATEWAY_HOST`), `ib_port` (alias `IB_PORT`/`IB_GATEWAY_PORT_PAPER`), `ib_connect_timeout_seconds`, `ib_request_timeout_seconds`, `ib_instrument_client_id`. Note: there is NO `IB_GATEWAY_PORT_LIVE` settings field. |
| IB port / account-prefix validator  | `backend/src/msai/services/nautilus/ib_port_validator.py`             | `IB_PAPER_PORTS = (4002, 4004)` · `IB_LIVE_PORTS = (4001, 4003)` · `IB_PAPER_PREFIXES = ("DU", "DF")` · `validate_port_account_consistency`                                                                                                                                     |
| Multi-IB-login router               | `backend/src/msai/services/live/gateway_router.py`                    | `GatewayRouter.resolve(ib_login_key)`, `GATEWAY_CONFIG` env-var format, `is_multi_login`                                                                                                                                                                                        |
| CLI sub-apps                        | `backend/src/msai/cli.py`                                             | `live` and `account` sub-apps, lines 91–620                                                                                                                                                                                                                                     |
| Live-trading frontend page          | `frontend/src/app/live-trading/page.tsx`                              | Token fetch · WS hook · REST fallback · KillSwitch · PositionsTable                                                                                                                                                                                                             |
| Settings frontend page              | `frontend/src/app/settings/page.tsx`                                  | IB account display · health badge                                                                                                                                                                                                                                               |
| NautilusTrader gotchas (read this!) | `.claude/rules/nautilus.md`                                           | Top-20 list — gotchas #3, #6, #10, #11, #13, #19 most relevant here                                                                                                                                                                                                             |
| Full Nautilus reference             | `docs/nautilus-reference.md`                                          | All 60KB                                                                                                                                                                                                                                                                        |

---

## Cross-references

- **Previous in journey:** [How Backtest Portfolios Work](how-backtest-portfolios-work.md) — the upstream composition step where `GraduationCandidate` rows are allocated and tested as a basket. Once a portfolio vets out there, you bring the strategy mix into a live portfolio here.
- **Next in journey:** [How Real-Time Monitoring Works](how-real-time-monitoring-works.md) — the WebSocket stream, dashboard P&L, position lists, alerts. The deployment you started here is what the dashboard subscribes to there.
- **Up:** [Developer Journey overview](00-developer-journey.md).
- **Adjacent:** [How Symbols Work](how-symbols-work.md) (the registry that backs `lookup_for_live`); [How Strategies Work](how-strategies-work.md) (where `code_hash` and `git_sha` come from); [`.claude/rules/nautilus.md`](../../.claude/rules/nautilus.md) (read this before any Nautilus code change).

---

**Date verified against codebase:** 2026-04-28
**Previous doc:** [← How Backtest Portfolios Work](how-backtest-portfolios-work.md)
**Next doc:** [How Real-Time Monitoring Works →](how-real-time-monitoring-works.md)
