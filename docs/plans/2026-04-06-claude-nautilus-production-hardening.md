# Claude — Nautilus Production Hardening (Revision 4)

**Status:** Plan v4 (incorporates Codex v3 re-review — Redis Streams PEL semantics, idempotency, readiness gate, identity/schema model corrections)
**Branch:** `feat/claude-nautilus-production-hardening`
**Scope:** `claude-version/` ONLY. The `codex-version/` directory is not touched by this plan; Codex CLI is hardening that codebase independently in parallel.

## References

- `docs/plans/2026-04-06-architecture-review.md` — the architecture review that produced this plan
- `docs/nautilus-reference.md` — deep technical reference on NautilusTrader (60KB, 10 sections, 20 gotchas)
- `docs/nautilus-natives-audit.md` — what Nautilus already provides natively vs what we have to build
- `.claude/rules/nautilus.md` — auto-loaded short-form gotchas list
- `docs/plans/2026-04-06-claude-nautilus-production-hardening.md` (this file)

## What changed in revision 4

Codex re-reviewed v3 and rejected it with 3 new P0 + 5 P1 + 2 P2 findings. Container topology and ownership are now directionally correct, but v3 had errors in Redis Streams semantics, command idempotency, the readiness gate, and the identity/schema model. v4 corrects these.

**Architectural corrections from v3:**

1. **Stable `deployment_slug` decoupled from `live_deployments.id`.** v3 derived `trader_id`/`order_id_tag` from `deployment_id`, but the live_deployments model creates a fresh row on every restart, so the deterministic identity changed across restarts and Phase 4 state reload broke. v4 adds a stable `deployment_slug` (16 hex chars = 64 bits, ~no collisions until 4 billion deployments) on the `live_deployments` row that is computed once at first deployment and reused across restarts. The stable slug is also persisted on the strategy row for cross-restart logical identity. Per-restart per-process state lives only in `live_node_processes` (run records), not in `live_deployments`.
2. **Redis Streams PEL recovery via `XAUTOCLAIM`.** v3 assumed un-ACKed messages are auto-redelivered to a new consumer in the same group. They are not — they sit parked in the Pending Entries List (PEL) until a consumer explicitly claims them. v4 uses `XAUTOCLAIM` (Redis ≥ 6.2) on consumer startup to reclaim entries idle longer than `min_idle_time_ms`. Both `LiveCommandBus.consume` (1.6) and the projection consumer (3.4) implement explicit PEL recovery. Tests verify that an un-ACKed message IS picked up by a new consumer after the recovery sweep, NOT by automatic redelivery.
3. **Idempotency at every layer.** v3's `ProcessManager.spawn` had no "already active" guard and `run_forever()` ACKed in `finally` even on failure. v4 adds: (a) a database-level unique partial index on `live_node_processes(deployment_id) WHERE status IN ('starting','building','ready','running','stopping')` so duplicate spawns fail at the DB; (b) the supervisor's command handler ACKs **only on success**, leaving failed messages in the PEL for `XAUTOCLAIM` recovery; (c) the `/api/v1/live/start` endpoint accepts an `Idempotency-Key` HTTP header and short-circuits to return the existing deployment if a duplicate retry arrives within the idempotency TTL.
4. **Post-start health check before `status="ready"`.** v3 treated `kernel.start_async()` returning as proof that reconciliation completed and trading was ready. v4 adds an explicit health check: after `kernel.start_async()` returns, the subprocess polls `node.is_running and trader.is_running and data_engine.is_connected and exec_engine.is_connected and not exec_engine.reconciliation_active` for up to `startup_health_timeout_seconds` (default 60). Only if **all** conditions are true does it write `status="ready"`. On timeout, the subprocess writes `status="failed"`, error_message includes which condition failed, and exits.
5. **`live_node_processes.pid` is nullable; `building` added to status enum.** v3's schema was inconsistent with its workflow (row is inserted before `process.start()` but pid was non-nullable). v4 fixes the column nullability and adds `building` to the status enum which the subprocess writes during the `kernel.build()` phase.
6. **`live_deployments` schema additions in a dedicated migration.** v3 referenced new columns (`trader_id`, `account_id`, `message_bus_stream`, `strategy_id_full`, `deployment_slug`) without a task that creates them. v4 adds task 1.1b which migrates the existing `live_deployments` model to add these columns and makes the existing model docstring "A new deployment row is created each time a strategy is (re-)started" obsolete — the new model reuses rows across restarts when keyed by stable `deployment_slug`.
7. **PositionReader rebuild model corrected.** v3's `Cache(database=adapter)` then `cache_all()` once is wrong: the `Cache` does not subscribe to Redis updates and would drift after the first read. v4's PositionReader builds a fresh `CacheDatabaseAdapter` and `Cache` per request, calls `cache_all()`, reads, and disposes — so every read is fresh. The import path is corrected to `from nautilus_trader.cache.config import CacheConfig` (not `nautilus_trader.common.config`). For the WebSocket initial snapshot the cost is acceptable (one snapshot per connect); for high-frequency reads, the projection consumer maintains an in-memory `dict[deployment_id, PositionState]` populated from the message bus stream, which the WebSocket already uses for live updates anyway.
8. **`RiskAwareStrategy` uses correct portfolio API names.** `portfolio.total_pnl()` and `portfolio.net_exposure()` take `InstrumentId` (per-instrument), not `Venue`. The venue-level aggregate methods are `portfolio.total_pnls(venue)` and `portfolio.net_exposures(venue)` — plural — and return `dict[Currency, Money]`. v4 fixes the mixin to use the plural forms for venue-level checks and the singular forms for per-instrument checks.
9. **Restart-continuity test runs against testcontainers Redis.** v3 invented an "on-disk KV-store StateSerializer" that doesn't exist in this Nautilus install — `DatabaseConfig` here is Redis-only. v4's restart test brings up a testcontainers Redis, points two consecutive `BacktestNode` runs at it via `CacheConfig.database = redis`, and verifies state persists across runs. Phase 4 Scenario D (live-feed restart assertion) is dropped — it's non-deterministic and the BacktestNode-twice test already proves the save/load contract.
10. **Supervisor keeps in-memory `dict[deployment_id, mp.Process]`.** v3 threw away child handles and relied solely on heartbeat. v4 keeps the handle map (parent and child are in the same namespace, so `Process.is_alive()`/`exitcode` ARE meaningful and give instant exit detection). Heartbeat is used only as the recovery/discovery signal after a supervisor restart — when the live handles are gone but the rows survive.
11. **Kill switch is push-based, not bar-poll.** v3's "strategy reads halt flag on `on_bar`" allows up to one bar of lag (up to a minute). v4 makes `/kill-all` publish a `stop` command for every running deployment to the supervisor (immediate SIGTERM → `manage_stop=True` flatten), AND set the persistent halt flag (so a future `/start` is rejected until `/resume`), AND have the audit hook in the strategy mixin reject any new orders the moment the in-process halt flag is observed. Three layers, no bar-poll lag.

**v3 → v4 changes (still in effect from prior revisions):**

- Dedicated `live-supervisor` Docker container (not arq-hosted)
- `stream_per_topic = False` — one deterministic Redis stream per trader
- Redis pub/sub for WebSocket fan-out (multi-uvicorn-worker correctness)
- FastAPI imports `nautilus_trader` for the Cache Python API
- `StrategyConfig.manage_stop = True` for native flatten on stop
- Parity harness: determinism + config round-trip + intent contract
- Heartbeat-only liveness for **cross-container recovery** (not for in-process exit detection — see #10 above)
- Custom `RiskEngine` subclass deleted; replaced with `RiskAwareStrategy` mixin
- `PositionSnapshotCache` deleted (use Nautilus Cache + projection state)
- `buffer_interval_ms = None` (write-through)
- Audit `client_order_id` correlation key
- Strategy code hash from file bytes
- `instrument_cache.trading_hours` JSONB column
- Phase 1 tasks 1.7-1.11 sequential

## What changed in revision 3

Codex re-reviewed v2 and rejected it with 2 new P0 + 7 P1 findings — all in the area of container topology and process ownership. v3 corrects the architectural mistakes and uses Nautilus features v2 was still reinventing.

**Architectural corrections from v2:**

1. **Dedicated `live-supervisor` Docker service (Option A).** v2 tried to host the supervisor as an arq startup task, but arq awaits `on_startup` completion BEFORE entering its poll loop — a "loops forever" startup would block the worker. v3 adds a third backend container alongside `backend` and `backtest-worker`: `live-supervisor`. It runs `python -m msai.live_supervisor` as its own entrypoint and consumes the Redis command stream directly. Trading subprocesses are children of this container. When the supervisor restarts, its children die — we accept this as a full node restart with broker reconciliation (which is fast and automatic).
2. **Heartbeat is the authority for liveness, not PID probing.** v2 proposed `os.kill(pid, 0)` from FastAPI to detect orphaned subprocesses, but FastAPI is in a different container namespace from the trading subprocess — PIDs are meaningless across containers. v3 uses heartbeat freshness (`last_heartbeat_at < now - 30s` → orphaned) as the sole liveness check.
3. **Deterministic `trader_id` / `strategy_id` / `order_id_tag`** derived from `deployment_id`. v2 never set these; Nautilus defaults to `TRADER-001` (collisions) and strategy IDs become unstable. v3 locks them in: `trader_id = f"MSAI-{deployment_id.hex[:8]}"`, `order_id_tag = deployment_id.hex[:8]`. This is also why Phase 4 state reload now works — state is keyed by the deterministic `strategy_id`, not `deployment_id`.
4. **`stream_per_topic = False`** so Nautilus publishes to ONE stream per trader (`trader-MSAI-{id}-stream`) rather than N streams per (topic, strategy). `stream_per_topic=True` combined with strategy-scoped topics means FastAPI can't subscribe before the stream exists (wildcard `XREADGROUP` is not a thing). v3 uses one stream per trader, deterministic name, FastAPI registers on deployment start.
5. **Redis pub/sub for WebSocket fan-out**, not in-memory queues. The backend runs with `--workers 2`, so in-memory queues mean a WebSocket client only sees events from the uvicorn worker that consumed them. v3 uses a Redis pub/sub channel per deployment.
6. **Nautilus Cache Python API** instead of raw Redis key reads. v2 suggested reading Nautilus's Redis keys directly from FastAPI, but those names are internal implementation details. v3 imports `nautilus_trader` in FastAPI and uses a transient `Cache` backed by the same `CacheDatabaseAdapter`.
7. **`manage_stop = True` native flatten**, not custom `on_stop`. Nautilus has a built-in market-exit loop triggered by `StrategyConfig.manage_stop = True`. v2's custom `on_stop` was reinventing this.
8. **Parity harness redesigned.** v2 planned to "feed bars into a TradingNode against IB paper" — this doesn't exist in Nautilus. v3 replaces it with three simpler tests:
   - **Determinism test**: same strategy, same bars, run BacktestNode twice, assert identical trade lists
   - **Config round-trip test**: load strategy via `ImportableStrategyConfig` with the live config schema, assert instantiation succeeds
   - **Intent capture contract test**: backtest emits `(timestamp, instrument, side, qty)` tuples; paper soak (Phase 5) is what catches live divergence, not this harness
9. **Restart test via BacktestNode twice.** v2 planned to restart a live TradingNode subprocess, which requires a deterministic bar feeder we don't have. v3 uses BacktestNode for both legs: run 1 saves state after N bars, run 2 loads state and processes bar N+1, asserts no duplicate order.

**v1 → v2 changes (still in effect):**

- Custom `RiskEngine` subclass DELETED (kernel can't use it); replaced with strategy-side mixin
- `PositionSnapshotCache` DELETED (Nautilus Cache already does this)
- Cache rehydration smoke test DELETED (automatic)
- Crash recovery simplified to orphaned-process detection only
- Reconciliation gating replaced with `status="ready"` marker after `kernel.start_async()` returns
- `buffer_interval_ms = 0` → `None`
- Redis stream topic names corrected
- Consumer groups with persisted offsets
- Audit `client_order_id` correlation key
- Strategy code hash from file bytes (not git)
- Phase 1 E2E uses deterministic smoke strategy
- `instrument_cache.trading_hours` JSONB column
- `GET /api/v1/live/status/{deployment_id}` route added
- Phase 1 tasks 1.7-1.11 sequential (not parallel)

## Goal

Production-harden the Claude implementation of MSAI v2 so it can safely run a personal hedge fund:

- Real Nautilus `TradingNode` for live trading via Interactive Brokers (currently a stub)
- Real security master that handles stocks, futures, options, indexes, FX (currently fake `TestInstrumentProvider.equity(SIM)`)
- Backtest and live use the **same** strategy code, the **same** instrument IDs, the **same** event contract
- Real-time positions, fills, and PnL visible in the dashboard, streamed from Nautilus's own message bus
- Risk runs in the order path with real inputs (currently hardcoded zeros)
- Crash recovery and broker reconciliation on restart (mostly automatic via Nautilus, we wire only the orphan detection)
- Order audit trail for every submission attempt with `client_order_id` correlation
- 30-day paper soak as a release gate before any real money

## Non-Goals

- The `codex-version/` codebase. This plan does not modify it.
- Multi-user / multi-tenant support.
- Distributed deployment beyond a single Azure VM (deferred to Phase 6+).
- Crypto venues. IB-supported asset classes only.

## Approach

Five phases. Each phase ends with a demonstrable improvement and a docker-based E2E verification. Phases are strictly sequential. Tasks within a phase parallelize only if explicitly noted (revision 2 corrected several false-parallelization claims from revision 1).

Every task uses TDD: failing test first, then implementation, then refactor.

**Iron rule:** If Nautilus already does it, we do not build it. We only configure it. The natives audit (`docs/nautilus-natives-audit.md`) is the authoritative reference for "already provided vs we have to build" decisions.

---

## Pre-Phase Decisions (Locked Before Phase 1)

These choices are locked here so every phase can rely on them.

**1. Canonical symbology: `IB_SIMPLIFIED`**
Live IB instruments use the form `<symbol>.<exchange>` — `AAPL.NASDAQ`, `EUR/USD.IDEALPRO`, `ESM5.XCME`. Set `InteractiveBrokersInstrumentProviderConfig.symbology_method = SymbologyMethod.IB_SIMPLIFIED`.

**2. Backtest instruments use the same canonical IDs as live.**
A backtest of AAPL uses `AAPL.NASDAQ`. The current `*.SIM` rebinding in `claude-version/backend/src/msai/services/nautilus/instruments.py` is removed in Phase 2.

**3. Live IB venue suffixes are real exchanges.**
Equities → `NASDAQ`, `NYSE`, `ARCA`. FX → `IDEALPRO`. Futures → `XCME`, `XCBT`, `GLOBEX`. Options → underlying exchange. Indexes → `CBOE`, `XNAS`.

**4. Nautilus IB client factory key stays `"IB"`.**
This is the registration key for `node.add_data_client_factory("IB", ...)` and `node.add_exec_client_factory("IB", ...)`. Not the venue.

**5. Audit + structured logging start in Phase 1.**
We need them while debugging the live path.

**6. Trading subprocesses are hosted by a dedicated `live-supervisor` Docker service.**

Neither FastAPI nor the arq worker owns the trading subprocess. A third backend container runs a long-running supervisor whose only job is to consume the Redis command stream and spawn `TradingNode` subprocesses.

The control plane (Option A):

```
┌───────────────┐           ┌────────────────┐      ┌─────────────────────┐
│  FastAPI      │           │  backtest      │      │  live-supervisor    │
│  backend      │           │  worker (arq)  │      │  (standalone)       │
│               │           │                │      │                     │
│ POST /start ──┼──┐        │  handles       │      │ consumes Redis      │
│ POST /stop    │  │        │  backtest +    │      │ command stream      │
│ GET /status   │  │        │  ingest jobs   │      │ via consumer group  │
│    ▲          │  │        │                │      │          │          │
│    │          │  │        │                │      │          │ spawn    │
│    │ read     │  │        │                │      │          v          │
└────┼──────────┘  │        └────────────────┘      │   ┌─────────────┐   │
     │             │                                │   │ TradingNode │   │
     │         Redis stream msai:live:commands      │   │ subprocess  │   │
     │         ┌──────────────────────────┐         │   │ (mp.Process │   │
     │         │ {"action":"start",...}   │         │   │  spawn)     │   │
     │         │ {"action":"stop",...}    │         │   │             │   │
     │         └──────────────────────────┘         │   └──────┬──────┘   │
     │                                              │          │ heartbeat│
     │         Postgres live_node_processes         │          │          │
     └───────── pid, status, last_heartbeat_at  ◄───┼──────────┘          │
                                                    └─────────────────────┘
```

Service-level behavior:

- **FastAPI backend** publishes `{"action": "start", "deployment_id": ..., ...}` commands to the `msai:live:commands` Redis stream via `XADD`. It **never** spawns subprocesses. `GET /status/{deployment_id}` reads from the `live_node_processes` table.
- **backtest-worker (arq)** is unchanged from today — it only handles backtest and ingest jobs. It does NOT host the live supervisor (Codex v2 P0: arq awaits `on_startup` before its poll loop, so a forever-loop startup task would deadlock the worker).
- **live-supervisor** is a new Docker service. Its entrypoint is `python -m msai.live_supervisor` and it runs `live_supervisor.main.run_forever()`. The supervisor:
  1. Joins the `msai-live-supervisor` consumer group on `msai:live:commands`
  2. Consumes commands via `XREADGROUP`, blocking with a 5-second timeout
  3. On `start`: writes a `live_node_processes` row with `status="starting"`, then calls `multiprocessing.get_context("spawn").Process(target=_trading_node_subprocess, args=(payload,)).start()`, updates the row with the spawned pid, ACKs the stream message
  4. On `stop`: reads the pid from `live_node_processes`, sends `SIGTERM`, waits for `status="stopped"` or timeout (then `SIGKILL`), ACKs the stream message
  5. Periodically scans `live_node_processes` for rows whose `last_heartbeat_at` is older than 30 seconds and marks them `status="failed"` with `error_message="heartbeat timeout"`. This is the **orphaned-process detector** that runs on the supervisor side, not FastAPI (heartbeat, not PID probing — Codex v2 P0 fix).
- **Trading subprocesses** are children of the live-supervisor container. When the supervisor container restarts, its children die. **This is accepted**: a container restart is a full node restart. Nautilus's `LiveExecEngineConfig.reconciliation = True` + `CacheConfig.database = redis` + `NautilusKernelConfig.load_state = True` reconcile broker state and rehydrate the cache on the next start. Reconciliation is fast (seconds) and the operator can choose to halt all strategies before restarting the supervisor if they want zero open positions during the gap.
- **FastAPI is never killed by a supervisor restart** — they're separate containers. `GET /status/{deployment_id}` keeps working. If the supervisor is dead, `status` will show stale heartbeats and the `/start` and `/stop` endpoints will return 503 until the supervisor is back.
- **Killing FastAPI** does not touch the supervisor or its children. The trading subprocess keeps running. The projection consumer (Phase 3) reconnects to the Redis consumer group on FastAPI restart and resumes streaming events from where it left off.

**7. Deterministic identities derived from a stable `deployment_slug`, NOT from `deployment_id`.**

Nautilus defaults `trader_id` to `TRADER-001` (collisions between deployments) and leaves `order_id_tag` at `None` (unstable strategy IDs — Codex v2 P1). v3 derived these from `deployment_id`, but the existing `LiveDeployment` model creates a fresh row on every restart, so `deployment_id` (and therefore `trader_id`/`order_id_tag`) changed across restarts and Phase 4 state reload could never find prior state (Codex v3 P1).

v4 introduces a **stable** `deployment_slug` that survives restarts:

```python
import secrets

# At first deployment of (strategy_id, instruments_set) for a user:
deployment_slug = secrets.token_hex(8)  # 16 hex chars = 64 bits
                                        # birthday-collision threshold ≈ 4 billion
trader_id = f"MSAI-{deployment_slug}"   # e.g. "MSAI-a1b2c3d4e5f60718"
order_id_tag = deployment_slug          # e.g. "a1b2c3d4e5f60718"
# Nautilus Strategy.id is built from f"{class_name}-{order_id_tag}"
# -> e.g. "EMACrossStrategy-a1b2c3d4e5f60718"
```

The slug is persisted on the `live_deployments` row at FIRST creation. On subsequent restarts of the same logical deployment, the existing row is reused — `/api/v1/live/start` looks up `(strategy_id, instruments, paper_trading)` first, and if a stopped deployment exists, it reuses that row's `deployment_slug`. The model docstring "A new deployment row is created each time a strategy is (re-)started" is obsolete — see task 1.1b for the migration.

This is the key change that makes Phase 4 state reload work: `EMACrossStrategy.on_save` writes state keyed by `f"EMACrossStrategy-{deployment_slug}"`, and on restart `on_load` reads state keyed by the same string because the slug didn't change.

`live_node_processes` rows are still created per restart — they record the per-process lifecycle (pid, status, heartbeat). They reference `live_deployments.id` via FK. The split is: **`live_deployments` is the stable logical record; `live_node_processes` is the per-restart run record.**

**Why 16 hex / 64 bits, not 8 / 32:** Birthday-collision likelihood for 32 bits hits ~50% at ~65k deployments. For a personal hedge fund that's far enough away to be theoretically safe but the cost of upgrading to 64 bits is zero — `secrets.token_hex(8)` is the same one-line call.

**8. `stream_per_topic = False` — one Redis stream per trader.**

With `stream_per_topic = True`, Nautilus publishes to `events.order.{strategy_id}`, `events.position.{strategy_id}`, etc. — one stream per (topic, strategy). FastAPI can't subscribe before those streams exist (wildcard `XREADGROUP` is not a thing). v3 uses `stream_per_topic = False`, which produces one stream per trader: `trader-MSAI-{deployment_slug}-stream`. The stream name is deterministic and can be registered in `live_node_processes` at start time so FastAPI knows what to subscribe to.

**9. WebSocket fan-out via Redis pub/sub, not in-memory queues.**

FastAPI runs with `--workers 2`. In-memory queues live inside a single uvicorn worker, so a WebSocket client only sees events from the worker that processed them (Codex v2 P1). v3 uses a Redis pub/sub channel per deployment (`msai:live:events:{deployment_id}`). The projection consumer (one per uvicorn worker) publishes translated events to the channel; every uvicorn worker subscribes and broadcasts to its own WebSocket clients. No in-memory state shared across workers.

**10. FastAPI imports `nautilus_trader` to use the Cache Python API — but ephemerally per request, not as a long-lived Cache.**

Reading Nautilus's Redis keys directly is wrong — those names are internal implementation details. The right pattern is to build a transient `Cache` in FastAPI pointed at the same Redis backend, populate it via `cache_all()`, read from it, and dispose it. **Each request gets a fresh `Cache`.** v3 wrongly kept long-lived `Cache` instances; the `Cache` does not subscribe to Redis updates so its view drifts after `cache_all()` (Codex v3 P1). v4 corrects both the import path and the lifetime model:

```python
from nautilus_trader.cache.cache import Cache
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.cache.config import CacheConfig             # NOT common.config
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.model.identifiers import StrategyId, TraderId


def read_open_positions(redis_host: str, redis_port: int, trader_id: str, strategy_id: str) -> list[Position]:
    cfg = CacheConfig(database=DatabaseConfig(type="redis", host=redis_host, port=redis_port), encoding="msgpack")
    adapter = CacheDatabaseAdapter(trader_id=TraderId(trader_id), config=cfg)
    try:
        cache = Cache(database=adapter)
        cache.cache_all()  # one-shot batch load — NOT a live subscription
        return cache.positions_open(strategy_id=StrategyId(strategy_id))
    finally:
        adapter.close()
```

The cost (one Redis batch read per request) is acceptable because it only runs in two places: (a) the WebSocket initial-snapshot handler on connect, and (b) PositionReader's cold path when `ProjectionState` doesn't have a deployment yet (Phase 3 task 3.5). Steady-state UI reads come from `ProjectionState` (in-memory, populated by the projection consumer in 3.4), not from Cache rebuilds.

This requires `nautilus_trader` as a runtime dep of the FastAPI backend (it already is). No raw key access.

**11. Strategies use `manage_stop = True` for native flatten.**

`StrategyConfig.manage_stop = True` tells Nautilus to close all positions and cancel all orders automatically on strategy stop. v3 uses this instead of custom `on_stop` flatten code (Codex v2 P2 — we were still reinventing).

**12. Redis Streams PEL recovery via `XAUTOCLAIM`, not "automatic redelivery".**

v3 wrongly assumed un-ACKed messages on a Redis Stream would be redelivered to a new consumer in the same group (Codex v3 P0). They are not. They sit parked in the **Pending Entries List** (PEL) for the original consumer until they are explicitly claimed by another consumer via `XCLAIM` or `XAUTOCLAIM` (Redis ≥ 6.2).

v4 makes both consumer-group users (the supervisor command bus, 1.6, and the projection consumer, 3.4) explicitly recover stale pending entries:

```python
# On consumer startup, before first XREADGROUP:
async def _recover_pending(self) -> None:
    """Reclaim entries idle longer than min_idle_time_ms.

    Run as the FIRST step in consume(): if the previous consumer
    crashed mid-message, its un-ACKed entries are now ours to retry.
    """
    cursor = "0-0"
    while True:
        cursor, claimed, _ = await self._redis.xautoclaim(
            name=self._stream,
            groupname=self._group,
            consumername=self._consumer_id,
            min_idle_time=self._min_idle_ms,
            start_id=cursor,
            count=100,
        )
        for entry_id, fields in claimed:
            yield self._decode(entry_id, fields)
        if cursor == "0-0":
            break
```

After `_recover_pending()`, the consumer enters its normal `XREADGROUP BLOCK ...` loop. On every iteration the consumer also calls `_recover_pending()` periodically (e.g. every 30 seconds) to handle the case where a peer consumer crashes mid-flight in steady state.

**13. Idempotency at every layer.**

v3 had no idempotency. A retried `/start` after a 504 timeout, or a supervisor crash between `process.start()` and the Redis ACK, both produced duplicate trading subprocesses with the same deterministic identity (Codex v3 P0). v4 adds three layers:

- **Database layer:** A unique partial index on `live_node_processes(deployment_id)` `WHERE status IN ('starting','building','ready','running','stopping')`. Two concurrent spawns for the same `deployment_id` cannot both insert active rows.
- **Supervisor layer:** Before spawning, the supervisor runs `SELECT ... FOR UPDATE SKIP LOCKED` on the `live_deployments` row by `deployment_slug`, then checks `live_node_processes` for an active row. If one exists, the spawn is a no-op and the command is ACKed (idempotent success). If not, it inserts and spawns. The command is ACKed **only after** the spawn is observed to succeed (`live_node_processes.status` reached `building` or later) — NOT in `finally`.
- **API layer:** `/api/v1/live/start` accepts an `Idempotency-Key` HTTP header (RFC draft). The key is hashed to a Redis key `msai:idem:start:{hash}` with the result and a 24-hour TTL. A retry with the same key returns the cached response immediately and does NOT publish a new command. Without an idempotency key, the API still de-duplicates by checking if an active deployment exists for `(strategy_id, instruments_set)` and returns the existing one.

**14. Post-start health check before writing `status="ready"`.**

v3 treated `kernel.start_async()` returning as proof that reconciliation completed and trading was ready (Codex v3 P0). It is not. Nautilus can return early on:

- Failed engine connect (data engine, exec engine)
- Failed reconciliation (returns False instead of raising)
- Failed portfolio init
- Pending instruments not yet loaded

v4 adds an explicit post-start health check in the trading subprocess (1.8):

```python
async def _wait_until_ready(node: TradingNode, timeout_s: int = 60) -> None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        kernel = node.kernel
        ok = (
            node.is_running
            and kernel.trader.is_running
            and kernel.data_engine.is_connected
            and kernel.exec_engine.is_connected
            and not kernel.exec_engine.reconciliation_active
            and len(kernel.cache.instruments()) > 0  # at least one instrument loaded
        )
        if ok:
            return
        await asyncio.sleep(0.5)
    raise StartupHealthCheckFailed(_diagnose(node))
```

`_diagnose` builds a structured failure reason listing exactly which condition failed. The trading subprocess catches `StartupHealthCheckFailed` in its outer try/except, writes `live_node_processes.status="failed"` with the diagnosis as `error_message`, then exits cleanly via the `finally` block (which still runs `node.dispose()`).

Only when `_wait_until_ready` returns successfully does the subprocess write `status="ready"`. The `/start` API (1.14) polls for `ready` OR `failed` and returns the right HTTP status accordingly.

**15. Supervisor keeps an in-memory `dict[deployment_id, mp.Process]`.**

v3 threw away the `mp.Process` handles immediately after `process.start()` and relied solely on heartbeat staleness for liveness (Codex v3 P2). That works for cross-container recovery but loses **instant** in-process exit detection: the supervisor can't reap zombies, can't observe real exit codes, and can't detect a crash for up to 30 seconds.

The parent (supervisor) and child (trading subprocess) are in the **same** Linux namespace (same container), so `Process.is_alive()` and `Process.exitcode` ARE meaningful. v4 keeps the handle map:

```python
class ProcessManager:
    def __init__(self, ...) -> None:
        self._handles: dict[UUID, mp.Process] = {}  # in-memory, supervisor-local
        ...

    async def _reap_loop(self) -> None:
        """Background task: poll handles for exits, surface real exit codes."""
        while not self._stop_event.is_set():
            for deployment_id, proc in list(self._handles.items()):
                if not proc.is_alive():
                    await self._on_child_exit(deployment_id, proc.exitcode)
                    del self._handles[deployment_id]
            await asyncio.sleep(1)
```

Heartbeat freshness is still the **recovery/discovery** signal — when the supervisor itself restarts, the in-memory map is empty and we have to rediscover via the database. But for normal in-process operation, the handle map is faster, more accurate, and surfaces real exit codes.

**16. Kill switch is push-based, not bar-poll.**

v3's "strategy reads `msai:risk:halt` flag on every `on_bar`" allowed up to one bar of lag — for a 1-minute bar that's a 60-second lag on an emergency halt (Codex v3 P2). v4 makes `/api/v1/live/kill-all` a push:

1. Set `msai:risk:halt = true` in Redis with a long TTL (persistence — survives any restart)
2. For every `live_node_processes` row with `status IN ('ready','running')`, publish a `stop` command to the supervisor's command stream with `reason="kill_switch"` and a special flag that requests immediate flatten (the supervisor SIGTERMs the child, `manage_stop=True` flattens automatically)
3. The pre-submit risk check in `RiskAwareStrategy` is the **third layer of defense**: it reads the halt flag from the cached value updated by an async task on every `on_bar` (still bar-cadence). This is defense-in-depth, NOT the primary mechanism.

Latency from operator click to flatten: bounded by the `XADD` latency to the supervisor command stream + the supervisor's `XREADGROUP BLOCK 5000` window + SIGTERM delivery + Nautilus stop. Realistically < 5 seconds. Compared to v3's "up to one bar," this is a 12× improvement on a 1m bar and 720× on a 1h bar.

`POST /api/v1/live/resume` clears `msai:risk:halt` and is required before `/start` will accept new deployments. There is no auto-resume.

**17. Each phase ends with a docker-based E2E test** that exercises the actual subprocess lifecycle, IB Gateway, Postgres, Redis, and (where relevant) the frontend.

---

## Phase 1 — Live Node + Live Supervisor + Audit

**Goal:** Claude can launch a real Nautilus `TradingNode` against IB Gateway paper, supervised by a dedicated `live-supervisor` Docker service, with deployment registry, structured logging, order audit, and a deterministic E2E that proves the order path.

**Phase 1 acceptance:**

- `POST /api/v1/live/start` publishes a command to the Redis stream (`msai:live:commands`)
- The `live-supervisor` service (its own Docker container) consumes the command via `XREADGROUP` and spawns a real `TradingNode` subprocess as a child of its own container
- Subprocess builds a `TradingNode` with deterministic `trader_id=f"MSAI-{deployment_slug}"` and `order_id_tag=deployment_slug`, connects to IB Gateway paper, completes reconciliation inside `kernel.start_async()`, transitions to `status="ready"` immediately after
- The deterministic smoke strategy submits a tiny AAPL market order on the first bar
- The order is recorded in `order_attempt_audits` with `client_order_id`, then updated through accepted/filled
- Killing the FastAPI container has zero effect on the trading subprocess (the supervisor and its children are in a different container)
- After API restart, `GET /api/v1/live/status/{deployment_id}` finds the surviving subprocess via the `live_node_processes` table
- `POST /api/v1/live/stop` publishes a stop command, the supervisor sends `SIGTERM`, the subprocess's `manage_stop = True` native flatten cancels orders + closes positions automatically, `node.stop_async()` and `dispose()` run in the `finally` block, exits cleanly
- Heartbeat freshness (not cross-container PID probing) is the sole liveness signal: the supervisor's `HeartbeatMonitor` marks rows with stale `last_heartbeat_at` as `status="failed"`

### Phase 1 tasks

#### 1.1 — `live_node_processes` table + model

Files:

- `claude-version/backend/src/msai/models/live_node_process.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_live_node_processes.py` (new)
- `claude-version/backend/tests/integration/test_live_node_process_model.py` (new)

```python
class LiveNodeProcess(Base, TimestampMixin):
    __tablename__ = "live_node_processes"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("live_deployments.id"), index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULLABLE because the row is inserted with status="starting" BEFORE
    # process.start() returns a pid (Codex v3 P1 fix). The supervisor
    # updates pid to the real value after spawn.
    host: Mapped[str] = mapped_column(String(255), nullable=False)  # docker container hostname
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # values: starting | building | ready | running | stopping | stopped | failed
    # 'building' is written by the subprocess during kernel.build() — Codex v3 P1 added.
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Idempotency layer (decision #13): a deployment can have at most ONE
        # active process row at any time. Two concurrent spawns racing on the
        # same deployment_id will fail at the database with a uniqueness
        # violation, which the supervisor catches and treats as "already
        # active, ACK the command."
        Index(
            "uq_live_node_processes_active_deployment",
            "deployment_id",
            unique=True,
            postgresql_where=text(
                "status IN ('starting','building','ready','running','stopping')"
            ),
        ),
    )
```

TDD:

1. Model integration test: create a row, query it back
2. Insert two rows with the same `deployment_id` and `status='ready'`, assert the second insert raises `IntegrityError`
3. Insert two rows with the same `deployment_id` where one is `status='stopped'` and the other is `status='ready'`, assert both succeed
4. Insert a row with `pid=None`, assert success (column is nullable)
5. Insert a row with `status='building'`, assert success
6. Then write the model + migration

Acceptance: all six tests pass; `alembic upgrade head` succeeds on a fresh database.

Effort: S
Depends on: nothing
Gotchas: Codex v3 P1 (nullable pid, building status, partial unique index for idempotency)

---

#### 1.1b — `live_deployments` schema migration (stable identity)

Files:

- `claude-version/backend/src/msai/models/live_deployment.py` (modify)
- `claude-version/backend/alembic/versions/<rev>_live_deployments_stable_identity.py` (new)
- `claude-version/backend/tests/integration/test_live_deployment_stable_identity.py` (new)

The existing model docstring says "A new deployment row is created each time a strategy is (re-)started." That contradicts the v4 stable-identity model (decision #7) and breaks Phase 4 state reload (Codex v3 P1). v4 makes a `live_deployments` row a **logical, stable** record that survives restarts. Per-run state lives only in `live_node_processes`.

New columns added by this migration:

```python
class LiveDeployment(Base):
    """A live or paper-trading deployment of a strategy.

    A deployment is a STABLE logical record. It is created once for a
    given (strategy_id, instruments_set, paper_trading) tuple per user
    and reused across restarts. Per-restart state lives in
    live_node_processes.
    """

    # ... existing columns above ...

    deployment_slug: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    # 16 hex chars = 64 bits, derived from secrets.token_hex(8) at first
    # creation. Used to derive trader_id, order_id_tag, and the Nautilus
    # message bus stream name. Stable across restarts (decision #7).

    trader_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # f"MSAI-{deployment_slug}" — convenience denormalization for log queries.

    strategy_id_full: Mapped[str] = mapped_column(String(64), nullable=False)
    # f"{strategy_class_name}-{deployment_slug}" — the Nautilus StrategyId.value

    account_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # IB account id (e.g. "DU1234567" for paper, "U1234567" for live).
    # Stored here so PositionReader and the projection consumer can find it.

    message_bus_stream: Mapped[str] = mapped_column(String(96), nullable=False)
    # f"trader-MSAI-{deployment_slug}-stream" — the deterministic Redis
    # stream name where Nautilus publishes events for this trader
    # (Phase 3 task 3.2). Persisted here so the projection consumer
    # knows what to subscribe to without polling Redis for stream names.

    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Replace the old started_at/stopped_at — those tracked the FIRST
    # start, but a deployment can be (re-)started many times.

    __table_args__ = (
        # Logical-identity uniqueness: at most one stable deployment
        # per (user, strategy, instruments, paper_trading). The instruments
        # array is normalized to a sorted, comma-joined string in a
        # generated column to make this work as a constraint.
        Index(
            "uq_live_deployment_logical",
            "started_by",
            "strategy_id",
            "paper_trading",
            "instruments_signature",
            unique=True,
        ),
    )

    instruments_signature: Mapped[str] = mapped_column(String(512), nullable=False)
    # Sorted, comma-joined canonical instrument IDs. Computed at deployment
    # creation time. The unique index above uses this for logical-identity
    # de-duplication: starting the "same" deployment twice reuses the row.
```

The migration:

1. Adds the new columns with default placeholders for any pre-existing rows
2. Backfills `deployment_slug` for existing rows via `secrets.token_hex(8)` per-row
3. Backfills `trader_id`, `strategy_id_full`, `message_bus_stream` from the slug
4. Backfills `account_id` from the IBSettings env vars (stamped at migration time as a best-effort)
5. Backfills `instruments_signature` by sorting and joining `instruments`
6. Backfills `last_started_at` from `started_at`, `last_stopped_at` from `stopped_at`
7. Creates the partial unique index on `instruments_signature`
8. Drops the old `started_at` / `stopped_at` columns (they're now redundant)

A helper service is added to compute the `instruments_signature` from a list of canonical IDs (sorted, joined with `,`). Used by `/api/v1/live/start` to look up existing deployments.

```python
# claude-version/backend/src/msai/services/live/deployment_identity.py
def compute_instruments_signature(instruments: list[str]) -> str:
    """Stable, normalized signature for the instrument set.

    Used for logical-identity de-duplication: a 'restart' of the
    same strategy on the same instruments returns the same row.
    """
    return ",".join(sorted(instruments))


def generate_deployment_slug() -> str:
    """16 hex chars = 64 bits. Used as the stable, deterministic
    Nautilus identity (trader_id, order_id_tag, strategy_id)."""
    return secrets.token_hex(8)


def derive_trader_id(slug: str) -> str:
    return f"MSAI-{slug}"


def derive_message_bus_stream(slug: str) -> str:
    return f"trader-{derive_trader_id(slug)}-stream"
```

TDD:

1. Migration test: starting with the existing schema, run the upgrade, verify all new columns exist with correct types
2. Migration test: insert a pre-existing row before the migration, run the migration, verify the row has a backfilled `deployment_slug` and `instruments_signature`
3. Logical de-duplication test: insert two `LiveDeployment` rows with the same `(started_by, strategy_id, paper_trading, instruments_signature)`, assert the second raises `IntegrityError`
4. Logical de-duplication test: same tuple but different `paper_trading` → both succeed
5. Helper test: `compute_instruments_signature(["MSFT.NASDAQ", "AAPL.NASDAQ"]) == "AAPL.NASDAQ,MSFT.NASDAQ"` (sorted)
6. Helper test: `derive_message_bus_stream("a1b2c3d4e5f60718") == "trader-MSAI-a1b2c3d4e5f60718-stream"`
7. Implement

Acceptance: all seven tests pass; `alembic upgrade head` succeeds on a fresh database AND on a database with pre-existing rows.

Effort: M
Depends on: 1.1
Gotchas: Codex v3 P1 (schema dependencies were missing); decision #7 (stable slug)

---

#### 1.2 — `order_attempt_audits` table + model with `client_order_id`

Files:

- `claude-version/backend/src/msai/models/order_attempt_audit.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_order_attempt_audit.py` (new)
- `claude-version/backend/tests/integration/test_order_attempt_audit_model.py` (new)

```python
class OrderAttemptAudit(Base, TimestampMixin):
    __tablename__ = "order_attempt_audits"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, unique=True)
    deployment_id: Mapped[UUID | None] = mapped_column(ForeignKey("live_deployments.id"), index=True, nullable=True)
    backtest_id: Mapped[UUID | None] = mapped_column(ForeignKey("backtests.id"), index=True, nullable=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True, nullable=False)
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_attempted: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # values: submitted | accepted | rejected | filled | partially_filled | cancelled | denied
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        CheckConstraint("(deployment_id IS NOT NULL) OR (backtest_id IS NOT NULL)"),
    )
```

The `client_order_id` is the correlation key. The audit hook in 1.11 generates this UUID, writes the initial `submitted` row, and looks the row up by `client_order_id` to update through accepted → filled.

TDD: integration test creates a row, updates via `client_order_id`, asserts state machine.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex finding #7 — `client_order_id` is the stable correlation key

---

#### 1.3 — Structured logging with `deployment_id` context

Files:

- `claude-version/backend/src/msai/core/logging.py` (modify)
- `claude-version/backend/tests/unit/test_logging.py` (extend)

Add a `deployment_id` context variable injected into every structlog record. Add `bind_deployment(deployment_id)` context manager.

TDD: test that `with bind_deployment(uuid)` causes subsequent log calls to include `deployment_id`.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 1.4 — Minimal real instrument bootstrap (NOT `TestInstrumentProvider`)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py` (new)
- `claude-version/backend/tests/unit/test_live_instrument_bootstrap.py` (new)

Returns an `InteractiveBrokersInstrumentProviderConfig` with `load_contracts` populated for the Phase 1 paper symbols. Phase 2 replaces this with the full SecurityMaster.

```python
_PHASE_1_PAPER_SYMBOLS = {
    "AAPL": IBContract(secType="STK", symbol="AAPL", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
    "MSFT": IBContract(secType="STK", symbol="MSFT", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
}

def build_ib_instrument_provider_config(symbols: list[str]) -> InteractiveBrokersInstrumentProviderConfig:
    contracts = frozenset(_PHASE_1_PAPER_SYMBOLS[s] for s in symbols)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )
```

TDD: test that `build_ib_instrument_provider_config(["AAPL"])` returns a config with the right contract; unknown symbol raises `ValueError`.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #9 (instrument not pre-loaded), #11 (don't load on critical path)

---

#### 1.5 — `build_live_trading_node_config()` builder

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (new)
- `claude-version/backend/tests/unit/test_live_node_config.py` (new)

```python
def build_live_trading_node_config(
    deployment_id: UUID,
    strategy_path: str,
    strategy_config: dict,
    paper_symbols: list[str],
    ib_settings: IBSettings,
) -> TradingNodeConfig:
    """Build the TradingNodeConfig used by the live trading subprocess.

    Uses Nautilus natives for everything Nautilus already provides:
    - LiveDataEngineConfig — defaults
    - LiveExecEngineConfig — reconciliation=True (default), reconciliation_lookback_mins=1440
    - LiveRiskEngineConfig — bypass=False, max_notional_per_order populated from deployment
    - InteractiveBrokersDataClientConfig — instrument provider from build_ib_instrument_provider_config
    - InteractiveBrokersExecClientConfig — paper port (4002), account_id from settings
    - cache and message_bus left UNCONFIGURED in Phase 1 (Phase 3 adds Redis)
    - load_state and save_state left at default False in Phase 1 (Phase 4 enables them)
    - strategies = [ImportableStrategyConfig(strategy_path=...)]

    Each call gets a unique ibg_client_id per deployment so concurrent
    deployments don't collide (gotcha #3). Uses ib_data_client_id and
    ib_exec_client_id (separate IDs) to avoid the data/exec collision.

    Validation:
    - paper_symbols must be non-empty
    - port=4002 implies account_id starts with "DU" (paper); port=4001 implies it doesn't (live)
    """
```

TDD:

1. Happy path
2. Each validation rejection
3. Two calls with different deployment IDs produce different `ibg_client_id` values
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.4
Gotchas: #3 (client_id collision), #6 (port/account mismatch)

---

#### 1.6 — Redis command stream with explicit PEL recovery (XAUTOCLAIM)

Files:

- `claude-version/backend/src/msai/services/live_command_bus.py` (new)
- `claude-version/backend/tests/integration/test_live_command_bus.py` (new)

```python
LIVE_COMMAND_STREAM = "msai:live:commands"
LIVE_COMMAND_GROUP = "live-supervisor"

class LiveCommandBus:
    """Thin wrapper over Redis Streams for the API ↔ supervisor control plane.

    Why a stream not pub/sub: pub/sub messages are lost if no consumer is
    listening. Streams + consumer groups give us durable delivery — but
    NOT automatic redelivery. Un-ACKed entries sit in the per-consumer
    PEL (Pending Entries List) until explicitly claimed (Codex v3 P0).
    This class implements XAUTOCLAIM-based recovery so a crashed
    supervisor's un-ACKed messages are picked up by the next supervisor
    instance (or the next iteration of the same supervisor).

    Decision #12 in the pre-phase decisions documents the rationale.
    """

    def __init__(
        self,
        redis: Redis,
        stream: str = LIVE_COMMAND_STREAM,
        group: str = LIVE_COMMAND_GROUP,
        min_idle_ms: int = 30_000,  # entries idle longer than this are eligible for reclaim
        recovery_interval_s: int = 30,
    ) -> None: ...

    async def publish_start(self, deployment_id: UUID, payload: dict) -> str:
        """Publish a start command. Returns the Redis stream entry ID.

        Adds an `idempotency_key` field to the payload (the caller's
        Idempotency-Key header value, or a hash of the deployment_id).
        Used by the supervisor to deduplicate retries (decision #13).
        """

    async def publish_stop(self, deployment_id: UUID, reason: str = "user") -> str:
        """Publish a stop command."""

    async def ensure_group(self) -> None:
        """Idempotently create the consumer group via XGROUP CREATE MKSTREAM."""

    async def consume(self, consumer_id: str) -> AsyncIterator[LiveCommand]:
        """Consume from the stream as part of LIVE_COMMAND_GROUP.

        Lifecycle per call to consume():
        1. ensure_group() — idempotent XGROUP CREATE MKSTREAM
        2. _recover_pending() — XAUTOCLAIM stale entries from any
           crashed peer (or our own previous run); yield each one
        3. Enter the steady-state XREADGROUP BLOCK 5000 COUNT N loop
        4. Every recovery_interval_s, call _recover_pending() again to
           handle peers crashing in steady state
        5. Each yielded LiveCommand has an `entry_id` the caller MUST
           pass back to ack(entry_id) — explicit ack semantics, no
           auto-ack-on-yield. The caller decides when to ACK (decision
           #13: ACK only on success, never in finally).
        """

    async def ack(self, entry_id: str) -> None:
        """XACK the entry. Call only after the command has been
        successfully handled and observed in the database (e.g.,
        live_node_processes row reached 'building' or later)."""

    async def _recover_pending(self) -> AsyncIterator[LiveCommand]:
        """Reclaim entries from peers that have been idle longer than
        min_idle_ms. Yields the same LiveCommand stream as the steady
        state. Implementation:

        cursor = "0-0"
        while True:
            cursor, claimed, _ = await redis.xautoclaim(
                name=stream, groupname=group,
                consumername=consumer_id,
                min_idle_time=min_idle_ms,
                start_id=cursor,
                count=100,
            )
            for entry_id, fields in claimed:
                yield LiveCommand.from_redis(entry_id, fields)
            if cursor == "0-0":
                break
        """
```

TDD:

1. Integration test against testcontainers Redis: publish 3 commands, consume + ACK each, verify they don't reappear on next consume
2. Integration test: publish a command, consume it WITHOUT ACKing (simulating a crash), call consume again from the SAME consumer_id with `min_idle_ms=0` — verify the pending entry is yielded
3. Integration test: publish a command, consume from `consumer_a` without ACKing, then call consume from `consumer_b` (different consumer_id) with `min_idle_ms=0` — verify `consumer_b` reclaims and yields the pending entry via `_recover_pending`
4. Integration test: verify that without `_recover_pending`, the entry would NOT be auto-redelivered (sanity check that the recovery is necessary)
5. Test the idempotency_key field is preserved through publish → consume
6. Test that `ack` outside the yielded entry does not crash
7. Implement

Acceptance: tests pass; the documentation explicitly notes "un-ACKed entries are NOT auto-redelivered, see \_recover_pending".

Effort: M
Depends on: nothing
Gotchas: Codex v3 P0 (PEL semantics — explicit XAUTOCLAIM, not auto-redelivery)

---

#### 1.7 — Dedicated `live-supervisor` Docker service

Files:

- `claude-version/backend/src/msai/live_supervisor/__init__.py` (new)
- `claude-version/backend/src/msai/live_supervisor/__main__.py` (new)
- `claude-version/backend/src/msai/live_supervisor/main.py` (new — the supervisor loop)
- `claude-version/backend/src/msai/live_supervisor/process_manager.py` (new — mp.Process lifecycle)
- `claude-version/backend/src/msai/live_supervisor/heartbeat_monitor.py` (new — orphaned-process detector)
- `claude-version/docker-compose.dev.yml` (add service)
- `claude-version/docker-compose.prod.yml` (add service)
- `claude-version/backend/tests/integration/test_live_supervisor.py` (new)

The supervisor runs as a standalone Python service (`python -m msai.live_supervisor`) in its own Docker container. It does NOT run inside the arq worker because arq awaits `on_startup` completion before entering its poll loop (Codex v2 P0).

`live_supervisor/main.py`:

```python
async def run_forever() -> None:
    """Main supervisor loop.

    Consumes commands from msai:live:commands via a Redis consumer
    group with explicit XAUTOCLAIM-based PEL recovery (decision #12).
    Maintains an in-memory dict[deployment_id, mp.Process] handle map
    (decision #15) and a background reap loop that surfaces real exit
    codes the moment a child dies.

    Runs until SIGTERM. On shutdown:
    - stop consuming new commands
    - drain in-flight handlers
    - do NOT send SIGTERM to any running trading subprocesses —
      they're owned by this container's OS and will be reaped when
      the container exits. The next supervisor start re-discovers
      surviving subprocesses via heartbeat-fresh rows.
    """
    bus = LiveCommandBus(redis=get_redis())
    process_manager = ProcessManager(db=async_session_factory)
    heartbeat_monitor = HeartbeatMonitor(db=async_session_factory, stale_seconds=30)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    monitor_task = asyncio.create_task(heartbeat_monitor.run_forever(stop_event))
    reap_task = asyncio.create_task(process_manager.reap_loop(stop_event))

    try:
        async for command in bus.consume("supervisor-1", stop_event):
            ok = False
            try:
                if command.action == "start":
                    ok = await process_manager.spawn(
                        deployment_id=command.deployment_id,
                        deployment_slug=command.deployment_slug,
                        payload=command.payload,
                        idempotency_key=command.idempotency_key,
                    )
                elif command.action == "stop":
                    ok = await process_manager.stop(command.deployment_id, reason=command.reason)
                else:
                    logger.warning("unknown_command", action=command.action)
                    ok = True  # ACK so we don't loop forever on a malformed command
            except Exception as exc:
                logger.exception("command_failed", entry_id=command.entry_id, error=str(exc))
                ok = False  # leave it in the PEL for XAUTOCLAIM-based retry

            # Decision #13: ACK only on success. Failures stay in the PEL
            # so a future _recover_pending() sweep retries them.
            if ok:
                await bus.ack(command.entry_id)
    finally:
        monitor_task.cancel()
        reap_task.cancel()
```

`process_manager.py`:

```python
class ProcessManager:
    """Owns the trading subprocesses spawned by this supervisor instance.

    Idempotency model (decision #13):
    - The DB has a unique partial index on live_node_processes(deployment_id)
      WHERE status IN active states. Two concurrent spawns for the same
      deployment cannot both insert active rows.
    - spawn() pre-checks for an existing active row. If found, returns
      True (idempotent success — the command can be ACKed).
    - spawn() catches IntegrityError on the partial index as the second
      line of defense and treats it as idempotent success.

    Handle map (decision #15):
    - self._handles maps deployment_id → mp.Process while the supervisor
      is alive. Used by reap_loop for instant exit detection (parent and
      child are in the same Linux namespace, so Process.is_alive() works).
    - On supervisor restart, the map is empty. Rediscovery is via the
      heartbeat: stale rows are flipped to 'failed' by HeartbeatMonitor;
      fresh rows are still running (their pids may even still match,
      but we don't rely on that).
    """

    def __init__(self, db: async_sessionmaker[AsyncSession]) -> None:
        self._db = db
        self._handles: dict[UUID, mp.Process] = {}

    async def spawn(
        self,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict,
        idempotency_key: str,
    ) -> bool:
        """Spawn a new trading subprocess. Returns True on success or
        idempotent no-op, False on hard failure (caller should NOT ACK).

        Steps:
        1. SELECT FOR UPDATE on the live_deployments row by deployment_slug
        2. Check live_node_processes for an active row for this deployment_id
           - If found and status in (starting, building, ready, running):
             return True (idempotent — already running)
           - If found and status in (stopping): return False (busy, retry later)
        3. INSERT live_node_processes with status='starting', pid=NULL
           (catch IntegrityError on the partial unique index → return True)
        4. ctx = mp.get_context("spawn"); process = ctx.Process(...); start()
        5. UPDATE live_node_processes.pid with the real pid
        6. Stash process in self._handles[deployment_id]
        7. Return True
        """
        async with self._db() as session, session.begin():
            deployment = await session.execute(
                select(LiveDeployment)
                .where(LiveDeployment.deployment_slug == deployment_slug)
                .with_for_update()
            )
            deployment = deployment.scalar_one_or_none()
            if deployment is None:
                logger.error("spawn_no_deployment", deployment_slug=deployment_slug)
                return False  # Hard failure — leave in PEL for ops investigation

            existing = await session.execute(
                select(LiveNodeProcess).where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.status.in_(("starting", "building", "ready", "running")),
                )
            )
            if existing.scalar_one_or_none() is not None:
                logger.info("spawn_idempotent", deployment_id=str(deployment_id))
                return True  # Already active — idempotent success

            try:
                row = LiveNodeProcess(
                    deployment_id=deployment_id,
                    pid=None,  # filled after process.start()
                    host=socket.gethostname(),
                    started_at=utcnow(),
                    last_heartbeat_at=utcnow(),
                    status="starting",
                )
                session.add(row)
                await session.flush()
            except IntegrityError:
                # Partial unique index caught a race
                logger.info("spawn_race_idempotent", deployment_id=str(deployment_id))
                return True

            ctx = mp.get_context("spawn")
            process = ctx.Process(
                target=_trading_node_subprocess,
                args=(TradingNodePayload.from_dict(payload),),
            )
            process.start()
            row.pid = process.pid
            await session.flush()
            self._handles[deployment_id] = process

        return True

    async def stop(self, deployment_id: UUID, reason: str = "user") -> bool:
        """Send SIGTERM to the subprocess. Escalate to SIGKILL after 30s.

        Returns True on success (or idempotent no-op), False on hard failure.

        Uses self._handles[deployment_id] for in-process cases. For supervisor-
        restart-discovered subprocesses (no handle), reads pid from the row
        and signals it directly. Both paths are in the same container
        namespace so the pid is meaningful.
        """
        async with self._db() as session:
            row = await session.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.status.in_(("starting", "building", "ready", "running")),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
            row = row.scalar_one_or_none()
            if row is None:
                logger.info("stop_idempotent", deployment_id=str(deployment_id))
                return True  # Already stopped
            row.status = "stopping"
            await session.commit()

        process = self._handles.get(deployment_id)
        pid = process.pid if process is not None else row.pid
        if pid is None:
            logger.error("stop_no_pid", deployment_id=str(deployment_id))
            return False

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # Already gone — reap_loop will catch up
            return True

        # Wait up to 30s for the row to flip to stopped/failed
        async with self._db() as session:
            for _ in range(30):
                await asyncio.sleep(1)
                cur = await session.get(LiveNodeProcess, row.id)
                if cur.status in ("stopped", "failed"):
                    return True
            # Escalate
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            cur.status = "failed"
            cur.error_message = "hard kill on stop timeout"
            cur.exit_code = -9
            await session.commit()
        return True

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        """Background task: poll handles for exits, surface real exit codes.

        Decision #15: parent + child are in the same namespace, so
        Process.is_alive() and Process.exitcode are meaningful and
        give instant exit detection. Heartbeat is only the recovery
        signal across supervisor restarts.
        """
        while not stop_event.is_set():
            for deployment_id, proc in list(self._handles.items()):
                if not proc.is_alive():
                    proc.join(timeout=1)
                    await self._on_child_exit(deployment_id, proc.exitcode)
                    del self._handles[deployment_id]
            await asyncio.sleep(1)

    async def _on_child_exit(self, deployment_id: UUID, exit_code: int | None) -> None:
        """Called when self._handles[deployment_id] is no longer alive.

        Updates live_node_processes.status to 'stopped' (exit_code 0)
        or 'failed' (non-zero), records the real exit_code, and emits
        an alert if the exit was unexpected.
        """
        async with self._db() as session, session.begin():
            row = await session.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.status.in_(("starting", "building", "ready", "running", "stopping")),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
            row = row.scalar_one_or_none()
            if row is None:
                return
            if exit_code == 0:
                row.status = "stopped"
            else:
                row.status = "failed"
                row.error_message = f"child exited with code {exit_code}"
            row.exit_code = exit_code
```

`heartbeat_monitor.py`:

```python
class HeartbeatMonitor:
    """Scans live_node_processes for rows whose last_heartbeat_at is older
    than stale_seconds and marks them status='failed'. This is the ONLY
    orphaned-process detector — FastAPI never PID-probes across container
    namespaces (Codex v2 P0 fix).
    """
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._mark_stale_as_failed()
            await asyncio.sleep(10)
```

The docker-compose service:

```yaml
live-supervisor:
  build:
    context: ./backend
    dockerfile: Dockerfile.dev
  container_name: msai-claude-live-supervisor
  command: ["python", "-m", "msai.live_supervisor"]
  volumes:
    - ./backend/src:/app/src:ro
    - ./strategies:/app/strategies:ro
    - ./data:/app/data
  environment:
    DATABASE_URL: postgresql+asyncpg://msai:msai_dev_password@postgres:5432/msai
    REDIS_URL: redis://redis:6379
    MSAI_API_KEY: ${MSAI_API_KEY:-msai-dev-key}
  depends_on:
    postgres: { condition: service_healthy }
    redis: { condition: service_healthy }
    ib-gateway: { condition: service_started }
  restart: unless-stopped
```

TDD:

1. Unit test `ProcessManager.spawn` with a patched `multiprocessing`: verify a row is inserted with `status="starting"` and `pid=None`, then updated with the real pid after `start()`
2. **Idempotency unit test #1**: pre-seed an active row for `deployment_id`, call `spawn(deployment_id)`, verify it returns True without spawning a new process and without inserting a new row
3. **Idempotency unit test #2**: simulate a race — patch `INSERT` to raise `IntegrityError` (the partial unique index fired), assert `spawn` catches it and returns True
4. Unit test `ProcessManager.stop` with the handle map populated: verify SIGTERM, wait loop, SIGKILL escalation, real exit code recorded
5. Unit test `ProcessManager.stop` when the handle map is empty (rediscovered subprocess): verify pid is read from the row and signaled
6. **Reap loop unit test**: stash a fake `Process` whose `is_alive()` returns False and `exitcode == 1`, run one iteration of `reap_loop`, verify `live_node_processes` row is `status='failed'`, `exit_code=1`, error_message contains "child exited with code 1"
7. Unit test `HeartbeatMonitor._mark_stale_as_failed` with a mock DB
8. **ACK-on-success-only test**: invoke `run_forever`'s command handler with a mock that returns False (failure), assert `bus.ack` is NOT called; with True (success), assert ack IS called
9. Integration test against testcontainers Postgres + Redis: publish a start command via `LiveCommandBus`, verify the supervisor consumes it, inserts a row, calls `_trading_node_subprocess` (use a no-op stub)
10. Integration test: publish two `start` commands for the same deployment_id back-to-back, verify only one trading subprocess is spawned and the second command is also ACKed
11. Implement

Acceptance: tests pass; the service stands up in `docker compose up -d live-supervisor`.

Effort: L
Depends on: 1.1, 1.1b, 1.5, 1.6
Gotchas: Codex v3 P0 (idempotency at DB + supervisor + ACK-on-success), Codex v3 P2 (handle map for instant exit detection), #18 (asyncio.run conflict — the supervisor owns its own event loop via `asyncio.run(run_forever())`)

---

#### 1.8 — Trading subprocess entry point (with deterministic identities + post-start health check)

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (full rewrite)
- `claude-version/backend/src/msai/services/nautilus/startup_health.py` (new)
- `claude-version/backend/tests/unit/test_trading_node_subprocess.py` (new)
- `claude-version/backend/tests/unit/test_startup_health.py` (new)

Top-level function (must be importable for `spawn` pickling):

```python
def _trading_node_subprocess(payload: TradingNodePayload) -> None:
    """Entry point for the live trading subprocess.

    Runs in a fresh Python interpreter under the spawn context. Steps:

    1. Import nautilus_trader (this installs uvloop policy globally — gotcha #1)
    2. Reset asyncio event loop policy to default (gotcha #18)
    3. Connect to Postgres, write LiveNodeProcess.status="building"
    4. Install the SIGTERM handler: calls node.stop_async() via asyncio.run_coroutine_threadsafe
    5. Build the TradingNodeConfig via build_live_trading_node_config
       - trader_id = f"MSAI-{deployment_slug}"  (deterministic, from payload — STABLE across restarts, decision #7)
       - strategies[0].order_id_tag = deployment_slug
       - manage_stop = True  (native flatten on stop — no custom on_stop needed)
    6. Construct TradingNode
    7. Register IB factories under key "IB"
    8. node.build()
    9. Start the heartbeat thread (1.9)
    10. node.start_async() — connects to engines, runs reconciliation
    11. POST-START HEALTH CHECK (decision #14, NEW IN v4):
        - Wait until is_running AND data_engine.is_connected AND
          exec_engine.is_connected AND not exec_engine.reconciliation_active
          AND len(cache.instruments()) > 0
        - Raises StartupHealthCheckFailed on timeout (default 60s) with
          a structured diagnosis describing which condition failed
        - This is the v4 fix for Codex v3 P0: kernel.start_async() can
          return without raising even when reconciliation, engine connect,
          or portfolio init failed silently. Don't trust the return; verify.
    12. Only after the health check passes, write LiveNodeProcess.status="ready"
    13. node.run() — blocks until SIGTERM
    14. finally:
        - Heartbeat thread stopped
        - node.stop_async() — Nautilus closes positions and cancels orders
          automatically because manage_stop=True
        - node.dispose() — releases Rust logger and sockets (gotcha #20)
        - On clean exit: LiveNodeProcess.status="stopped", exit_code=0
        - On StartupHealthCheckFailed: LiveNodeProcess.status="failed",
          error_message=diagnosis, exit_code=2
        - On any other exception: status="failed", error_message=traceback,
          exit_code=1
"""
```

`startup_health.py`:

```python
@dataclass(slots=True, frozen=True)
class StartupHealthDiagnosis:
    is_running: bool
    data_engine_connected: bool
    exec_engine_connected: bool
    reconciliation_complete: bool
    instruments_loaded: int
    failure_reason: str  # human-readable

    def is_healthy(self) -> bool:
        return (
            self.is_running
            and self.data_engine_connected
            and self.exec_engine_connected
            and self.reconciliation_complete
            and self.instruments_loaded > 0
        )


class StartupHealthCheckFailed(Exception):
    def __init__(self, diagnosis: StartupHealthDiagnosis) -> None:
        self.diagnosis = diagnosis
        super().__init__(diagnosis.failure_reason)


def diagnose_node(node: "TradingNode") -> StartupHealthDiagnosis:
    """Inspect the running node and return the current health snapshot.

    Pure inspection — no side effects, safe to call repeatedly.
    """
    kernel = node.kernel
    is_running = bool(node.is_running and kernel.trader.is_running)
    data_connected = bool(kernel.data_engine.is_connected)
    exec_connected = bool(kernel.exec_engine.is_connected)
    reconciled = not bool(kernel.exec_engine.reconciliation_active)
    instruments = len(kernel.cache.instruments())

    if not is_running:
        reason = "trader not running"
    elif not data_connected:
        reason = "data engine not connected"
    elif not exec_connected:
        reason = "exec engine not connected"
    elif not reconciled:
        reason = "reconciliation still active"
    elif instruments == 0:
        reason = "no instruments loaded into cache"
    else:
        reason = "healthy"

    return StartupHealthDiagnosis(
        is_running=is_running,
        data_engine_connected=data_connected,
        exec_engine_connected=exec_connected,
        reconciliation_complete=reconciled,
        instruments_loaded=instruments,
        failure_reason=reason,
    )


async def wait_until_ready(node: "TradingNode", timeout_s: int = 60) -> StartupHealthDiagnosis:
    """Poll the node until all health conditions are true or timeout.

    Returns the healthy diagnosis on success.
    Raises StartupHealthCheckFailed with the LAST diagnosis on timeout.
    """
    deadline = monotonic() + timeout_s
    diagnosis = diagnose_node(node)
    while monotonic() < deadline:
        diagnosis = diagnose_node(node)
        if diagnosis.is_healthy():
            return diagnosis
        await asyncio.sleep(0.5)
    raise StartupHealthCheckFailed(diagnosis)
```

The deterministic identities from decision #7 are injected here. `payload.deployment_slug` comes from the supervisor, which reads it from the `live_deployments` row (now stable across restarts per task 1.1b). The `trader_id` and `order_id_tag` are stable across restarts so Nautilus's cache and state persistence can key against them consistently.

TDD:

1. Unit test `diagnose_node` with mock TradingNode in each failure state (data not connected, exec not connected, reconciliation active, no instruments) — verify the right `failure_reason`
2. Unit test `wait_until_ready` with a stub that flips to healthy on the third poll — verify it returns the healthy diagnosis
3. Unit test `wait_until_ready` with a stub that never becomes healthy — verify `StartupHealthCheckFailed` is raised after timeout with the last diagnosis attached
4. Unit test `_trading_node_subprocess` with all `nautilus_trader` imports mocked: verify policy reset is called, verify `status="starting"` → `building` → `ready` state machine, verify `dispose()` is called in finally, verify `trader_id` is `f"MSAI-{slug}"`, verify `manage_stop=True` is set
5. Unit test that `StartupHealthCheckFailed` causes the subprocess to write `status="failed"` with the diagnosis as `error_message`, exit_code=2, and still call `dispose()` in finally
6. Unit test that an exception inside `node.run()` still triggers the finally block with status="failed", exit_code=1
7. Unit test that SIGTERM triggers `node.stop_async`
8. Implement

Acceptance: tests pass.

Effort: L
Depends on: 1.1, 1.5
Gotchas: #1 (uvloop), Codex v3 P0 (post-start health check — kernel.start_async return is NOT proof of readiness), #13 (manage_stop handles flatten), #18 (asyncio.run), #20 (dispose)

---

#### 1.9 — Heartbeat task in subprocess

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend)
- `claude-version/backend/tests/integration/test_trading_node_heartbeat.py` (new)

A `threading.Thread` (NOT asyncio task — the trading node owns the event loop) that updates `live_node_processes.last_heartbeat_at = now()` every 5 seconds. Started after `node.build()`, stopped in the `finally` block.

Why a thread, not asyncio: writing to Postgres from inside Nautilus's event loop adds complexity (we'd need to share the loop). A short-lived sync DB write from a daemon thread is simpler and the heartbeat doesn't need low latency.

TDD:

1. Integration test with a stub subprocess (no actual TradingNode) that runs the heartbeat for 12 seconds, verifies `last_heartbeat_at` advances at least twice
2. Implement

Acceptance: integration test green.

Effort: S
Depends on: 1.1, 1.8
Gotchas: none

---

#### 1.10 — Stop sequence via native `manage_stop = True`

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (SIGTERM handler already in 1.8)
- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (set `manage_stop=True` on StrategyConfig)
- `claude-version/backend/tests/integration/test_trading_node_stop.py` (new)

v2 had a custom `Strategy.on_stop` that called `cancel_all_orders` + `close_all_positions`. v3 deletes that and uses Nautilus's native `manage_stop = True` instead (Codex v2 P2).

With `manage_stop=True`, Nautilus runs the built-in market-exit loop (`trading/strategy.pyx:1779`) on strategy stop: it cancels all open orders for the strategy's instrument and submits market orders to close any open positions. No custom code.

```python
# In build_live_trading_node_config (1.5):
strategies=[
    ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=strategy_config_path,
        config={
            **strategy_config,
            "manage_stop": True,  # native flatten
            "order_id_tag": deployment_slug,  # deterministic
        },
    ),
]
```

The stop sequence is now:

1. Supervisor sends SIGTERM to the subprocess pid
2. Subprocess's signal handler updates `live_node_processes.status="stopping"` and schedules `node.stop_async()` on the kernel's event loop
3. Nautilus stops the strategy; because `manage_stop=True`, the built-in exit loop flattens positions and cancels orders
4. Subprocess exits cleanly, `finally` block writes `status="stopped"`, `exit_code=0`
5. If the subprocess does not exit within 30 seconds, the supervisor escalates to SIGKILL (ProcessManager.stop in 1.7 already handles this)

TDD:

1. Integration test: spawn subprocess with a stub strategy holding an open position, send SIGTERM, verify Nautilus closes the position via `manage_stop` (mocked broker records the close order), verify exit_code=0 and status="stopped"
2. Integration test: spawn a subprocess that ignores SIGTERM (e.g. blocking in a tight loop), verify the supervisor's SIGKILL escalation fires and status="failed"
3. Implement

Acceptance: tests pass.

Effort: S (dramatically simpler than v2)
Depends on: 1.7, 1.8
Gotchas: #13 (fixed by `manage_stop=True`, no custom code)

---

#### 1.11 — Order audit hook with `client_order_id` correlation

Files:

- `claude-version/backend/src/msai/services/nautilus/audit_hook.py` (new)
- `claude-version/backend/tests/unit/test_audit_hook.py` (new)

A Strategy mixin that intercepts order submissions:

```python
class AuditedStrategy(Strategy):
    def submit_order_with_audit(self, order: Order) -> None:
        client_order_id = order.client_order_id.value
        # Insert "submitted" row keyed by client_order_id BEFORE broker
        self._audit.write_submitted(
            client_order_id=client_order_id,
            deployment_id=self._deployment_id,
            strategy_id=self._strategy_id,
            strategy_code_hash=self._strategy_code_hash,  # from 1.13
            instrument_id=str(order.instrument_id),
            side=str(order.side),
            quantity=Decimal(str(order.quantity)),
            price=Decimal(str(order.price)) if hasattr(order, "price") else None,
            order_type=str(order.order_type),
            ts_attempted=now_utc(),
            status="submitted",
        )
        self.submit_order(order)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="accepted",
            broker_order_id=str(event.venue_order_id) if event.venue_order_id else None,
        )

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="rejected",
            reason=event.reason,
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="filled",
        )

    def on_order_denied(self, event: OrderDenied) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="denied",
            reason=event.reason,
        )
```

TDD:

1. Mock Strategy + DB; call `submit_order_with_audit`; verify "submitted" row written with `client_order_id`
2. Fire each event; verify the row is updated through the lifecycle by `client_order_id`
3. Test that `on_order_denied` records `denied` status
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.2, 1.13
Gotchas: Codex #7 (client_order_id correlation)

---

#### 1.12 — Strategy code hash from file bytes (NOT git)

Files:

- `claude-version/backend/src/msai/services/nautilus/strategy_hash.py` (new)
- `claude-version/backend/tests/unit/test_strategy_hash.py` (new)

```python
def compute_strategy_code_hash(strategy_path: Path) -> str:
    """SHA256 of the strategy file bytes. Used for reproducibility on
    every backtest and live deployment.

    Why not git rev-parse: Codex finding #7. The container only mounts
    src/ and strategies/, not the repo root. git is not available in
    the container at all.
    """
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def get_git_sha_from_env() -> str | None:
    """Read MSAI_GIT_SHA from env. Set by docker compose at build time
    via build args. Optional — used for traceability but not required.
    """
    return os.environ.get("MSAI_GIT_SHA")
```

The strategy hash is computed once at deploy time (in the API endpoint) and persisted on the `live_deployments` row. The audit hook (1.11) reads it from the row, doesn't recompute.

TDD: hash a known file, verify result matches OpenSSL.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex #7

---

#### 1.13 — `GET /api/v1/live/status/{deployment_id}` route

Files:

- `claude-version/backend/src/msai/api/live.py` (modify — add the parameterized route)
- `claude-version/backend/tests/unit/test_live_status_endpoint.py` (extend)

```python
@router.get("/status/{deployment_id}", response_model=LiveDeploymentStatusResponse)
async def get_live_deployment_status(
    deployment_id: UUID,
    db: AsyncSession = Depends(get_db),
    _claims: dict = Depends(get_current_user),
) -> LiveDeploymentStatusResponse:
    """Return the current status of a single live deployment.

    Reads from the `live_node_processes` table — does NOT maintain
    in-memory state. The supervisor + subprocess write to the table;
    this endpoint just reads.
    """
```

The existing `GET /api/v1/live/status` (no path param) returns all running deployments — keep it.

TDD:

1. Test the endpoint returns 404 for unknown deployment_id
2. Test it returns the row data for a known deployment
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.1
Gotchas: Codex #13 (route was missing)

---

#### 1.14 — Wire `/api/v1/live/start` and `/stop` to the command bus (with idempotency)

Files:

- `claude-version/backend/src/msai/api/live.py` (modify start/stop endpoints)
- `claude-version/backend/src/msai/services/live/idempotency.py` (new — Idempotency-Key store)
- `claude-version/backend/tests/integration/test_live_start_stop_endpoints.py` (new)
- `claude-version/backend/tests/integration/test_live_start_idempotency.py` (new)

`POST /api/v1/live/start`:

```python
@router.post("/start", status_code=201, response_model=LiveDeploymentResponse)
async def start_live_deployment(
    body: LiveDeploymentStartRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    user: User = Depends(get_current_user),
) -> LiveDeploymentResponse:
    """Start (or re-start) a live deployment.

    Idempotency layers (decision #13):

    1. HTTP Idempotency-Key: if the header is set, the (key, hash(body))
       pair is looked up in Redis. A retry within the TTL returns the
       cached response unchanged. Mismatch (same key, different body)
       returns 422.

    2. Logical de-duplication by (started_by, strategy_id, paper_trading,
       instruments_signature): if a STABLE live_deployments row already
       exists for this user+strategy+instrument-set, reuse it. The
       deployment_slug, trader_id, etc. all stay the same.

    3. Active-process de-duplication: if the existing deployment already
       has a live_node_processes row in (starting,building,ready,running),
       return 200 with the existing deployment_id WITHOUT publishing a
       new command.

    Workflow:
    A. If Idempotency-Key set, check the cache; on hit, return cached response
    B. Look up existing live_deployments by (user, strategy_id, instruments_signature, paper_trading)
       - Found: reuse row, keep deployment_slug
       - Not found: INSERT new row with secrets.token_hex(8) as deployment_slug
    C. Look up active live_node_processes for that deployment
       - Active: return 200 + existing deployment_id (no new command)
       - Not active: continue
    D. Publish start command via LiveCommandBus.publish_start (carries
       deployment_id, deployment_slug, idempotency_key, trader_id, etc.)
    E. Poll live_node_processes for status='ready' or status='failed' with
       a configurable timeout (default 60s, matches the subprocess
       startup_health_timeout_seconds from decision #14)
    F. On 'ready': return 201 + LiveDeploymentResponse
    G. On 'failed': return 503 + error_message from the row
    H. On timeout: return 504 (the supervisor will eventually catch up;
       caller can retry with the SAME Idempotency-Key)
    I. Cache the response in the Idempotency-Key store with 24h TTL
    """
```

`idempotency.py` — Idempotency-Key store:

```python
class IdempotencyStore:
    """Redis-backed Idempotency-Key cache for /start.

    Key: msai:idem:start:{sha256(idempotency_key)}
    Value: msgpack-encoded {body_hash, status_code, response_json}
    TTL: 24 hours

    Behavior:
    - get(key, body_hash): None if no entry, the cached response if hit
                           with matching body_hash, raises BodyMismatch
                           if same key but different body
    - put(key, body_hash, response, status_code): write with TTL
    """

    async def get(self, key: str, body_hash: str) -> CachedResponse | None: ...
    async def put(self, key: str, body_hash: str, response: dict, status_code: int) -> None: ...
```

`POST /api/v1/live/stop`:

```python
@router.post("/stop")
async def stop_live_deployment(
    body: LiveDeploymentStopRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    _user: User = Depends(get_current_user),
):
    """Stop a running deployment.

    Idempotent: if no active live_node_processes row exists, return 200
    immediately (already stopped). Otherwise publish a stop command and
    poll for status in (stopped, failed) with a 60s timeout.

    Idempotency-Key handling identical to /start.
    """
```

TDD:

1. Integration test: `/start` with no `Idempotency-Key` header for a fresh strategy → publishes, mocked supervisor flips to ready, returns 201
2. Integration test: `/start` twice with the SAME `Idempotency-Key` and identical body within the TTL → second call returns the cached 201 without re-publishing
3. Integration test: `/start` twice with the SAME `Idempotency-Key` but different body → second returns 422
4. Integration test: `/start` twice without `Idempotency-Key` for the same `(strategy_id, instruments_signature)` while the first is still running → second returns 200 (logical de-dup) with the existing deployment_id and does NOT publish a new command
5. Integration test: `/start` for a previously stopped deployment of the same `(strategy_id, instruments_signature)` → reuses the existing live_deployments row (verify same `deployment_slug`)
6. Integration test: stop endpoint publishes, mocked supervisor flips status to stopped
7. Integration test: stop endpoint when no active row exists → returns 200 immediately
8. Test timeouts return 504
9. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.1b, 1.6, 1.13
Gotchas: Codex v3 P0 (idempotency layer #3 — HTTP Idempotency-Key + logical + DB)

---

#### 1.15 — Deterministic smoke strategy

Files:

- `claude-version/strategies/example/smoke_market_order.py` (new)
- `claude-version/backend/tests/unit/test_smoke_strategy.py` (new)

```python
class SmokeMarketOrderConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    manage_stop: bool = True  # v3 decision #11: native flatten on stop
    order_id_tag: str = ""    # v3 decision #7: injected from deployment_slug


class SmokeMarketOrderStrategy(AuditedStrategy):
    """Submits exactly ONE tiny market order on the first bar received,
    then sits idle. Used by the Phase 1 E2E to prove the order path
    end-to-end.

    Why: the EMA strategy may not cross during a short E2E window
    (Codex finding #8). The smoke strategy is deterministic.

    No custom on_stop — `manage_stop=True` on the config tells Nautilus
    to cancel all open orders and flatten positions automatically when
    the strategy is stopped (v3 decision #11).
    """

    def __init__(self, config: SmokeMarketOrderConfig) -> None:
        super().__init__(config=config)
        self._order_submitted = False

    def on_bar(self, bar: Bar) -> None:
        if self._order_submitted:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("1"),
        )
        self.submit_order_with_audit(order)
        self._order_submitted = True
```

TDD:

1. Unit test: feed a synthetic bar, verify exactly one order is submitted
2. Feed a second bar, verify NO additional order is submitted
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.11
Gotchas: Codex #8

---

#### 1.16 — Phase 1 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_live_trading_phase1.py` (new)
- `claude-version/scripts/e2e_phase1.sh` (helper)

Docker-based E2E:

1. `docker compose -f docker-compose.dev.yml up -d`
2. IB Gateway paper container with credentials from env
3. POST `/api/v1/live/start` with the smoke strategy and `instruments=["AAPL"]`
4. Assert response is 201, get deployment_id
5. Verify `live_node_processes` heartbeat advances by ≥2 over 12 seconds
6. Wait for at least one bar to arrive (poll for an audit row)
7. Verify the audit table has exactly 1 row with `status` in `(submitted, accepted, filled)`
8. Verify the row has `client_order_id`, `strategy_code_hash`, `instrument_id="AAPL.NASDAQ"` (or whatever the IB provider returns), `side="BUY"`, `quantity=1`
9. **Kill the FastAPI container**: `docker kill msai-claude-backend`
10. Sleep 5s
11. `docker compose up -d backend`
12. Verify the trading subprocess is still alive (heartbeat still advancing)
13. `GET /api/v1/live/status/{deployment_id}` returns the running deployment from the registry
14. POST `/api/v1/live/stop`
15. Verify `live_node_processes.status="stopped"`, `exit_code=0`
16. Verify the IB account has zero open positions for the instrument

Gated by `MSAI_E2E_IB_ENABLED=1`.

Acceptance: harness passes locally against a real IB Gateway paper container.

Effort: L
Depends on: 1.1–1.15
Gotchas: covered

---

### Phase 1 task ordering

These tasks must run sequentially because later ones edit files earlier ones create:

```
1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6 → 1.7 → 1.8 → 1.9 → 1.10 → 1.11 → 1.12 → 1.13 → 1.14 → 1.15 → 1.16
```

There is no parallelization in Phase 1. Codex finding #13 was correct: 1.7/1.8/1.9/1.10/1.11 all hot-edit `trading_node.py` and `audit_hook.py`. The earlier "Group D parallelizable" claim was wrong.

---

## Phase 2 — Security Master + Catalog Migration + Parity

**Goal:** Backtest and live use the same canonical instruments. The fake `TestInstrumentProvider.equity(SIM)` is gone. Multi-asset support actually works.

**Phase 2 acceptance:**

- A backtest of `AAPL.NASDAQ` uses real IB contract details from the SecurityMaster cache
- A live deployment of `AAPL` resolves to the **exact same** `AAPL.NASDAQ` `Instrument` object
- The parity validation harness runs the EMA strategy in both backtest and historical-paper-replay over the same window and asserts intent-level parity (see 2.11)
- The streaming catalog builder handles a 1 GB Parquet directory without OOM
- Existing `*.SIM` backtests are migrated to canonical IDs by a one-shot script
- The `instrument_cache` table stores `trading_hours` metadata so Phase 4's market-hours guard has something to read

### Phase 2 tasks

#### 2.1 — `InstrumentSpec` dataclass

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py` (new)
- `claude-version/backend/tests/unit/test_instrument_spec.py` (new)

```python
@dataclass(slots=True, frozen=True)
class InstrumentSpec:
    asset_class: Literal["equity", "future", "option", "forex", "index"]
    symbol: str
    venue: str
    currency: str = "USD"
    expiry: date | None = None
    strike: Decimal | None = None
    right: Literal["C", "P"] | None = None
    underlying: str | None = None
    multiplier: Decimal | None = None

    def canonical_id(self) -> str:
        """Return the IB_SIMPLIFIED canonical Nautilus instrument ID string."""
```

TDD: per-asset-class canonical_id tests; bad combinations raise ValueError.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #4 (venue suffix discipline)

---

#### 2.2 — Postgres `instrument_cache` table with `trading_hours`

Files:

- `claude-version/backend/src/msai/models/instrument_cache.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_instrument_cache.py` (new)
- `claude-version/backend/tests/integration/test_instrument_cache_model.py` (new)

```python
class InstrumentCache(Base, TimestampMixin):
    __tablename__ = "instrument_cache"
    canonical_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ib_contract_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    nautilus_instrument_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trading_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Schema: {"timezone": "America/New_York", "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}, ...], "eth": [...]}
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`trading_hours` is populated by 2.4 from the IB contract details. Phase 4 task 4.3 reads it for the market-hours guard. Codex finding #9 — the dependency is now explicit.

TDD: integration test pattern.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex #9

---

#### 2.3 — IB qualification adapter

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (new)
- `claude-version/backend/tests/unit/test_ib_qualifier.py` (new)

```python
class IBQualifier:
    """Wraps Nautilus's InteractiveBrokersInstrumentProvider to qualify
    InstrumentSpec → IBContract via the running TradingNode's IB connection.

    For the SecurityMaster service, we don't open our own IB connection —
    we delegate to a temporary InteractiveBrokersInstrumentProvider built
    on top of an isolated InteractiveBrokersClient. Throttles to ≤50 msg/sec
    to respect IB API limits.

    For continuous futures, uses CONTFUT secType. For options, uses
    reqSecDefOptParamsAsync (NOT reqContractDetails) to avoid throttling
    on chain queries.
    """

    async def qualify(self, spec: InstrumentSpec) -> Contract: ...
    async def qualify_many(self, specs: list[InstrumentSpec]) -> list[Contract]: ...
    async def front_month_future(self, root_symbol: str, exchange: str) -> Contract: ...
    async def option_chain(self, underlying: str, exchange: str, max_strikes: int) -> list[Contract]: ...
```

TDD:

1. Mock `ib_async.IB`, verify the right contract type per asset class
2. Test throttling with a fake clock
3. Test that an unqualified contract raises a clear error
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.1
Gotchas: #11 (don't load on critical path), #12 (option chains)

---

#### 2.4 — Nautilus instrument parser + trading hours extractor

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/parser.py` (new)
- `claude-version/backend/tests/unit/test_security_master_parser.py` (new)

Wraps Nautilus's `parse_instrument` (`adapters/interactive_brokers/parsing/instruments.py`) to return `Equity` / `FuturesContract` / `OptionContract` / `CurrencyPair`. Also extracts `trading_hours` from the IB `ContractDetails.tradingHours` and `liquidHours` strings into the JSONB schema documented in 2.2.

TDD:

1. Test that an `Equity` `IBContractDetails` parses to `Equity` with the right precision
2. Test trading_hours extraction for AAPL (NYSE hours) and ESM5 (CME hours, near-24h)
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.1, 2.3
Gotchas: none

---

#### 2.5 — `SecurityMaster` service

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/security_master/service.py` (new)
- `claude-version/backend/tests/unit/test_security_master.py` (new)

```python
class SecurityMaster:
    def __init__(self, qualifier: IBQualifier, parser: NautilusInstrumentParser, db: AsyncSession): ...

    async def resolve(self, spec_or_symbol: InstrumentSpec | str) -> Instrument:
        """Cache-first resolve. Order:
        1. Read from instrument_cache by canonical_id
        2. Miss: qualify via IBQualifier, parse via NautilusInstrumentParser,
           extract trading_hours, write to cache, return
        3. Stale: refresh in background, return cached for now
        """

    async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]: ...
    async def refresh(self, canonical_id: str) -> Instrument: ...

    @classmethod
    def shorthand_to_spec(cls, symbol: str) -> InstrumentSpec:
        """Best-effort shorthand: 'AAPL' → equity AAPL.NASDAQ."""
```

TDD:

1. Cache hit
2. Cache miss → qualify + parse + write + return
3. Bulk resolve uses batched calls
4. Shorthand for each asset class
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.2, 2.3, 2.4
Gotchas: #11

---

#### 2.6 — Replace `instruments.py` with SecurityMaster delegation

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (rewrite)
- `claude-version/backend/tests/unit/test_instruments.py` (rewrite)

Remove the `*.SIM` rebinding (`instruments.py:45` per architecture review). Delegate to `SecurityMaster.resolve()`.

A temporary `legacy_resolve_sim(symbol)` shim is kept for existing backtest test fixtures, marked deprecated, removed in 2.10.

TDD:

1. `resolve_instrument("AAPL")` returns an `Equity` with `instrument_id = "AAPL.NASDAQ"`
2. The instrument is structurally identical to what SecurityMaster returns
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.5
Gotchas: #4

---

#### 2.7 — Streaming catalog builder

Files:

- `claude-version/backend/src/msai/services/nautilus/catalog_builder.py` (modify)
- `claude-version/backend/tests/unit/test_catalog_builder_streaming.py` (new)

Replace the full-partition pandas load with `pyarrow.parquet.ParquetFile.iter_batches(batch_size=100_000)`. Each batch is wrangled via `BarDataWrangler` and appended to the catalog.

TDD:

1. Synthetic 1M-row Parquet file
2. Run new builder with `batch_size=100_000`
3. Assert peak memory ≤ 200 MB via `tracemalloc`
4. Assert resulting catalog has 1M bars
5. Existing tests still pass

Acceptance: tests pass.

Effort: M
Depends on: nothing
Gotchas: #15-adjacent (large catalogs need streaming, not batch)

---

#### 2.8 — Migration script: rebuild existing catalogs under canonical IDs

Files:

- `claude-version/scripts/migrate_catalog_to_canonical.py` (new — note: under `claude-version/scripts/`, not `backend/scripts/` per Codex finding #13)
- `claude-version/backend/tests/integration/test_migrate_catalog.py` (new)

Walks `data/parquet/<asset_class>/<symbol>/`, resolves each via `SecurityMaster.shorthand_to_spec(symbol).canonical_id()`, builds Nautilus catalog under `data/nautilus/<canonical_id>/`. Idempotent.

TDD:

1. Synthetic input
2. Run migration
3. Assert output exists
4. Re-run is no-op
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.5, 2.7
Gotchas: Codex #13 (script location)

---

#### 2.9 — Update backtest API + worker for canonical IDs

Files:

- `claude-version/backend/src/msai/api/backtests.py` (modify)
- `claude-version/backend/src/msai/workers/backtest_job.py` (modify)
- `claude-version/backend/tests/unit/test_backtests_api.py` (modify)

`POST /api/v1/backtests/run` accepts shorthand or canonical; resolves shorthand via `SecurityMaster.shorthand_to_spec`; persists canonical IDs in `backtests.instruments`. The worker reads canonical only.

The backtest_runner builds a `BacktestVenueConfig` per unique venue in the instruments list (multiple venue configs if instruments span venues).

TDD:

1. POST with shorthand → row has canonical
2. POST with canonical → unchanged
3. Worker builds the right venue configs
4. Implement

Acceptance: tests pass; existing backtests run end-to-end producing the same trades under canonical IDs.

Effort: M
Depends on: 2.5, 2.6, 2.8
Gotchas: #4, #2

---

#### 2.10 — Remove `legacy_resolve_sim` shim

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (delete shim)
- All `*.SIM`-dependent fixtures migrated

TDD: full test suite passes without the shim.

Acceptance: `git grep -l "legacy_resolve_sim"` returns nothing.

Effort: S
Depends on: 2.6, 2.9
Gotchas: none

---

#### 2.11 — Parity validation harness (redesigned for v3)

Files:

- `claude-version/scripts/parity_check.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/normalizer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/comparator.py` (new)
- `claude-version/backend/tests/integration/test_parity_determinism.py` (new)
- `claude-version/backend/tests/integration/test_parity_config_roundtrip.py` (new)

v2 planned "feed bars into a TradingNode against IB paper" — this doesn't exist in Nautilus (Codex v2 P1). There is no Nautilus mode that runs `TradingNode` with IB paper exec + local catalog data replay. v3 replaces the harness with three tractable tests.

**Test A — Determinism test (backtest twice, same bars, same trades):**

The real risk the parity harness catches is strategy non-determinism — a strategy that depends on wall-clock time, random seeds without a fixed seed, or dict iteration order can drift between backtest runs. v3's determinism test runs the same strategy on the same Parquet catalog twice via `BacktestNode` and asserts the resulting trade list is byte-identical.

```python
def test_backtest_is_deterministic() -> None:
    result_a = run_backtest(strategy_path=..., catalog_path=..., window=...)
    result_b = run_backtest(strategy_path=..., catalog_path=..., window=...)
    assert normalize(result_a.trades) == normalize(result_b.trades)
```

**Test B — Config round-trip test (catches type errors between backtest and live configs):**

`ImportableStrategyConfig` is the Nautilus abstraction that loads a strategy class + config in both backtest and live. If the backtest strategy config and the live strategy config diverge in schema (e.g., an optional field added on one side), live deployments fail at instantiation. The round-trip test loads the strategy via `ImportableStrategyConfig` with the **live** config schema and asserts instantiation succeeds, even when run from a backtest context.

```python
def test_live_config_instantiates_in_backtest_context() -> None:
    live_config = build_live_strategy_config(...)
    importable = ImportableStrategyConfig(
        strategy_path=..., config_path=..., config=live_config
    )
    # Nautilus resolves and instantiates it via the BacktestNode path
    node = BacktestNode(configs=[build_backtest_run_config(...importable...)])
    node.build()  # Must not raise
    node.dispose()
```

**Test C — Intent capture contract (documentation, not a test):**

The real contract between backtest and live is that the strategy emits the same `(timestamp, instrument_id, side, signed_qty)` tuples given the same bars. The plan documents the `OrderIntent` dataclass:

```python
@dataclass(slots=True, frozen=True)
class OrderIntent:
    decision_timestamp: datetime  # bar-close timestamp
    instrument_id: str            # canonical Nautilus ID
    side: Literal["BUY", "SELL"]
    signed_qty: Decimal           # positive for buys, negative for sells
```

The `normalizer.py` module extracts `OrderIntent` tuples from a backtest `BacktestResult` (the list of submitted orders with timestamps). The `comparator.py` module compares two `list[OrderIntent]` sequences for exact ordered equality.

Both the backtest runner and the live audit hook (1.11) write `OrderIntent` records to disk (via the `order_attempt_audits` table). This lets us do **backtest-vs-production comparison after the fact**:

- Phase 5 paper soak produces a log of live `OrderIntent` tuples
- Operator can re-run the same strategy + same config against the same Parquet window in backtest
- Compare the two intent sequences for drift

**Why this is better than v2:**

- It's actually achievable with existing Nautilus APIs
- Determinism is the real risk the harness catches — wall-clock drift, RNG, dict order
- Config round-trip catches schema drift between backtest and live configs before deployment
- The intent contract is a stable artifact that lives across backtest and paper soak
- The paper soak in Phase 5 is what catches live-only divergence (latency, slippage) — the harness doesn't pretend to catch it

**Non-goals for v3 parity harness:**

- Compare against paper IB live fills (not achievable with stock Nautilus without a custom data feeder — deferred to a future phase if needed)
- Catch runtime divergence from latency/slippage (that's the paper soak's job)

TDD:

1. Unit test the normalizer: convert a `BacktestResult.orders_df` to `list[OrderIntent]`, verify round-trip
2. Unit test the comparator: feed two lists with known diffs (extra/missing/reordered decisions), verify the right errors
3. Integration test A (determinism): run the EMA strategy twice on a 1-day AAPL window, assert identical trades
4. Integration test B (config round-trip): load the live EMA config via `ImportableStrategyConfig` in a `BacktestNode`, assert instantiation succeeds

Acceptance: all four tests pass.

Effort: M (smaller than v2 because we dropped the IB paper leg)
Depends on: 2.5, 2.6, 2.9
Gotchas: #14 (divergence from fills — acknowledged and deferred to paper soak)

---

#### 2.12 — Multi-asset support

Three sub-tasks (parallelizable):

**2.12a — Futures**: extend specs/qualifier/parser. Front-month resolution via CONTFUT.
**2.12b — Options**: extend specs/qualifier/parser. Use `reqSecDefOptParamsAsync`. Require explicit strike (gotcha #12).
**2.12c — FX**: extend specs/qualifier. IDEALPRO venue.

Each: TDD pattern; tests cover one happy path + one edge case.

Effort: M each
Depends on: 2.5
Gotchas: #12

---

#### 2.13 — Phase 2 E2E

Files: `claude-version/backend/tests/e2e/test_security_master_phase2.py` (new)

E2E: start stack with paper IB Gateway; resolve `AAPL`, `ESM5.XCME`, `EUR/USD.IDEALPRO` via SecurityMaster API; run a backtest with `AAPL.NASDAQ` for a 1-day window; run parity harness; assert parity passes; verify streaming catalog builder peak memory ≤ 500 MB.

Effort: L
Depends on: 2.1–2.12

---

### Phase 2 task ordering / parallelization

```
2.1, 2.2, 2.7  (parallel — no inter-deps)
  ↓
2.3, 2.4 (parallel, both depend on 2.1)
  ↓
2.5 (depends on 2.2, 2.3, 2.4)
  ↓
2.6, 2.8, 2.9 (parallel, depend on 2.5; 2.8 also on 2.7)
  ↓
2.10, 2.11 (parallel, depend on 2.6, 2.9)
  ↓
2.12a, 2.12b, 2.12c (parallel, depend on 2.5)
  ↓
2.13 (depends on all)
```

---

## Phase 3 — Redis State Spine + Projection Layer + Risk in Order Path

**Goal:** The API can see what live strategies are doing in real-time, via Nautilus's own message bus published to Redis Streams. Risk runs on real position state. The kill switch actually closes positions.

**Phase 3 acceptance:**

- A live deployment publishes events through Nautilus's `MessageBusConfig.database = redis` to a **single** Redis Stream per trader, with a deterministic stream name `trader-MSAI-{deployment_slug}-stream` (v3 decision #8)
- A FastAPI projection consumer reads that stream via **consumer groups** (durable, no event loss on FastAPI restart)
- The consumer translates Nautilus events to a stable internal schema and publishes them to a **Redis pub/sub channel** per deployment (`msai:live:events:{deployment_id}`) — v3 decision #9, so multi-worker uvicorn still fans out correctly
- Every uvicorn worker subscribes to that pub/sub channel and pushes events to its own WebSocket clients
- The `/live` page shows real-time positions, fills, and PnL
- The `RiskAwareStrategy` mixin blocks an order that would breach a per-strategy max position, using the Nautilus `Portfolio` API inside the Strategy (`self.portfolio.account()`, `self.portfolio.net_exposure()`, `self.portfolio.total_pnl()`), which is populated automatically via `CacheConfig.database = redis`
- FastAPI reads position snapshots for the UI via the Nautilus **Cache Python API** (a transient `Cache` pointed at the same Redis backend — v3 decision #10), NOT by parsing raw Nautilus Redis keys
- `POST /api/v1/live/kill-all` sets a sticky halt flag in Redis that the strategy mixin reads on every `on_bar`

### Phase 3 tasks

#### 3.1 — Configure `CacheConfig.database = redis` for live (NOT backtest)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/src/msai/services/nautilus/backtest_runner.py` (verify NO database config)
- `claude-version/backend/tests/unit/test_live_node_config_cache.py` (new)

```python
cache=CacheConfig(
    database=DatabaseConfig(
        type="redis",
        host=settings.redis_host,
        port=settings.redis_port,
    ),
    encoding="msgpack",
    buffer_interval_ms=None,  # write-through; gotcha #7. Codex #3 — must be None, not 0
    persist_account_events=True,
)
```

Backtest config has NO `cache.database` set (gotcha #8 inverse).

TDD:

1. Live config has `cache.database.type == "redis"` and `buffer_interval_ms is None`
2. Backtest config has `cache.database is None`
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, Codex #3

---

#### 3.2 — Configure `MessageBusConfig.database = redis` for live (NOT backtest)

Files: same as 3.1 plus tests

```python
message_bus=MessageBusConfig(
    database=DatabaseConfig(type="redis", host=..., port=...),
    encoding="msgpack",          # gotcha #17 — JSON fails on Decimal/datetime/Path
    stream_per_topic=False,      # v3 decision #8 — ONE stream per trader
    use_trader_prefix=True,
    use_trader_id=True,
    streams_prefix="stream",
    buffer_interval_ms=None,     # write-through; Codex #3
)
```

With `stream_per_topic = False`, Nautilus publishes **all** message bus events for a given `trader_id` to a **single** Redis Stream:

```
trader-MSAI-{deployment_slug}-stream
```

Each entry on the stream carries the original topic (`events.order.filled`, `events.position.opened`, `events.account.state`, etc.) as a field inside the message so the projection consumer (3.4) can route by topic after `XREADGROUP`.

**Why not `stream_per_topic = True`:** That mode produces one stream per (topic, strategy) — e.g. `trader-{id}-stream-events.order.{strategy_id}`. The stream names are only known after the strategy is loaded, which means FastAPI can't subscribe at deployment start time. Redis has no wildcard `XREADGROUP`, so the consumer would have to poll for new stream names — a worse contract than knowing the single stream name up front. v3 chooses the single-stream mode and has the translator dispatch on the in-message topic field.

**Stream name is registered at deployment start:** Task 1.14 (`/api/v1/live/start`) computes `stream_name = f"trader-MSAI-{deployment_slug}-stream"` from the deterministic identities and writes it to the `live_deployments` row (new column `message_bus_stream`). The projection consumer (3.4) reads this column when it joins the consumer group — no guessing, no polling.

TDD: parallel to 3.1. Add a test that asserts `stream_per_topic is False` on the live config and `message_bus_stream` on a fresh deployment row matches `f"trader-MSAI-{slug}-stream"`.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, #17, Codex #3, #4, Codex v2 P1 (stream discoverability)

---

#### 3.3 — Internal event schema (stable frontend contract)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/events.py` (new)
- `claude-version/backend/tests/unit/test_projection_events.py` (new)

Pydantic models for the internal MSAI schema (stable, decoupled from Nautilus):

- `PositionSnapshot { deployment_id, instrument_id, qty, avg_price, unrealized_pnl, realized_pnl, ts }`
- `FillEvent { deployment_id, client_order_id, instrument_id, side, qty, price, commission, ts }`
- `OrderStatusChange { deployment_id, client_order_id, status, reason, ts }`
- `AccountStateUpdate { deployment_id, account_id, balance, margin_used, margin_available, ts }`
- `RiskHaltEvent { deployment_id, reason, set_at }`
- `DeploymentStatusEvent { deployment_id, status, ts }`

TDD: serialization round-trip per model.

Effort: S
Depends on: nothing
Gotchas: Codex projection-layer recommendation

---

#### 3.4 — Redis Streams consumer + Redis pub/sub fan-out

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/consumer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/translator.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/fanout.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/registry.py` (new)
- `claude-version/backend/tests/integration/test_projection_consumer.py` (new)
- `claude-version/backend/tests/integration/test_projection_fanout.py` (new)

Background asyncio task in **each** FastAPI uvicorn worker that:

1. On startup, queries `live_deployments` for all rows with an active `live_node_processes` row (status in ready/running) and pulls their `message_bus_stream` name (from 3.2)
2. Joins the Redis consumer group `msai-projection` on each active stream via `XGROUP CREATE MKSTREAM` (idempotent)
3. **Recovers any pending entries from the PEL** via `XAUTOCLAIM` for entries idle longer than `min_idle_ms` (default 30s) — un-ACKed messages from a previously crashed projection consumer (Codex v3 P0). This is the v4 fix for the false assumption that messages are auto-redelivered.
4. Consumes via `XREADGROUP BLOCK 5000 COUNT 100`
5. Decodes Nautilus events using `MsgSpecSerializer` from `nautilus_trader.serialization.serializer`
6. Routes by the in-message `topic` field — the translator is a `dict[topic_prefix, translator_fn]` lookup
7. Translates each Nautilus event to the internal schema (3.3) via `translator.py`
8. **Publishes** the translated internal event to the per-deployment Redis pub/sub channel `msai:live:events:{deployment_id}` via `PUBLISH` (JSON-encoded)
9. **Updates the in-memory projection state** (`projection_state.py`, see below) so PositionReader can serve fast snapshot reads without rebuilding a Cache
10. `XACK`s the Redis stream message ONLY after both `PUBLISH` and the in-memory state update succeed (at-least-once delivery, ACK only on success — never in `finally`, mirrors decision #13)
11. Every `recovery_interval_s` (default 30s), re-runs `XAUTOCLAIM` to catch entries from peers that crashed mid-flight after the initial recovery sweep
12. On deployment start, a new stream is registered via the `StreamRegistry` — the consumer picks it up on the next iteration of its loop
13. On deployment stop, the stream is deregistered and the consumer closes it after draining

**`projection_state.py`** — in-memory live state populated by the consumer (added in v4 to make `PositionReader` work without long-lived Caches):

```python
class ProjectionState:
    """In-memory rolling state of every active deployment, populated by the
    projection consumer from translated MessageBus events. Used by
    PositionReader (3.5) for fast UI reads — no Nautilus Cache rebuild
    on every request.

    State is per-uvicorn-worker. The pub/sub fan-out (step 8 above) is
    what keeps WebSocket clients consistent across workers. The internal
    state is for snapshot reads only.
    """

    def __init__(self) -> None:
        self._positions: dict[UUID, dict[str, PositionSnapshot]] = {}
        self._accounts: dict[UUID, AccountStateUpdate] = {}

    def apply(self, deployment_id: UUID, event: InternalEvent) -> None:
        """Single dispatcher: routes the translated event into the right
        in-memory bucket. Called by the projection consumer after a
        successful translate but BEFORE the XACK."""
        match event:
            case PositionSnapshot(): self._upsert_position(deployment_id, event)
            case OrderStatusChange(): pass  # not state-relevant
            case FillEvent(): pass  # the resulting PositionSnapshot covers it
            case AccountStateUpdate(): self._accounts[deployment_id] = event
            case PositionClosedEvent(): self._remove_position(deployment_id, event.instrument_id)

    def positions(self, deployment_id: UUID) -> list[PositionSnapshot]:
        return list(self._positions.get(deployment_id, {}).values())

    def account(self, deployment_id: UUID) -> AccountStateUpdate | None:
        return self._accounts.get(deployment_id)
```

`registry.py` — `StreamRegistry`:

```python
class StreamRegistry:
    """Tracks which streams the consumer should be reading.

    Every worker maintains its own view. On change, the consumer
    re-reads live_deployments and updates the set of active streams.
    Uses Redis pub/sub channel "msai:live:stream-registry-changed"
    as a change notifier (every uvicorn worker subscribes).
    """
    async def active_streams(self) -> dict[UUID, str]: ...
    async def notify_change(self) -> None: ...
```

`fanout.py` — thin pub/sub publisher:

```python
async def publish_event(
    redis: Redis,
    deployment_id: UUID,
    event: InternalEvent,  # Pydantic model from 3.3
) -> None:
    """Publish a translated internal event to the per-deployment
    pub/sub channel. Every uvicorn worker subscribes to this channel
    and forwards to its own WebSocket clients.

    Channel name: msai:live:events:{deployment_id}
    Payload: event.model_dump_json().encode()
    """
    await redis.publish(f"msai:live:events:{deployment_id}", event.model_dump_json())
```

**Why Redis pub/sub not in-memory queues:** FastAPI runs with `--workers 2`. An in-memory queue lives inside a single uvicorn worker, so a WebSocket client connected to worker A only sees events from worker A's consumer (Codex v2 P1). Redis pub/sub broadcasts to all subscribers, so every worker's WebSocket clients see every event exactly once (the consumer-group ensures the stream is consumed exactly once; the pub/sub ensures fan-out to all workers).

**Pub/sub is non-durable — this is fine:** The Redis stream + consumer group provides durability for events crossing uvicorn-worker restart. Pub/sub is only used for the fan-out step (stream → N websocket-broadcasting workers). If a worker is down when a pub/sub message arrives, its WebSocket clients briefly miss the event — but the next snapshot they request on reconnect (from the Cache, via 3.5) is authoritative.

**The translator is a pure function** `translate(nautilus_event_payload, topic: str) -> InternalEvent`. One mapper per Nautilus event type. Comprehensive switch keyed by topic prefix (`events.order.*`, `events.position.*`, `events.account.*`).

**No TTL on positions** — Codex finding #5. Position snapshots live as long as the position is open. They're cleaned up on `PositionClosed` events, not on a timer.

TDD:

1. Unit test translator with each Nautilus event type (order filled, order rejected, position opened, position closed, account state)
2. Integration test: publish a synthetic `OrderFilled` payload to the Redis stream, verify the consumer receives it via the group, translates it, publishes to pub/sub, updates ProjectionState, and ACKs
3. Integration test: two pub/sub subscribers (simulating two uvicorn workers), publish one event via the consumer, verify both receive it
4. **PEL recovery integration test**: publish a message, consume it WITHOUT ACKing (simulate consumer crash before publish), restart the consumer with `min_idle_ms=0` — verify `XAUTOCLAIM` reclaims the entry, the new consumer publishes to pub/sub, updates state, and ACKs
5. **PEL recovery negative test**: publish a message, consume + ACK normally, restart the consumer — verify the entry is NOT redelivered (idempotency proof)
6. **ACK-on-success-only test**: stub `publish_event` to raise, verify the message is NOT ACKed and is reclaimed on the next recovery sweep
7. Integration test: stream registry change — add a new deployment mid-loop, verify the consumer picks up the new stream within one iteration
8. Implement

Acceptance: tests pass.

Effort: L
Depends on: 3.2, 3.3
Gotchas: Codex v3 P0 (PEL semantics — explicit XAUTOCLAIM, ACK only on success)

---

#### 3.5 — `PositionReader` from in-memory `ProjectionState` (primary) + ephemeral `Cache` (fallback)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/position_reader.py` (new)
- `claude-version/backend/tests/integration/test_position_reader.py` (new)

v3's `PositionReader` kept long-lived `Cache` instances and called `cache_all()` once. That is wrong: a Nautilus `Cache` does NOT subscribe to Redis updates — `cache_all()` is a one-shot batch load. Snapshot reads would drift after the first call (Codex v3 P1). The import path was also wrong (`nautilus_trader.common.config` should be `nautilus_trader.cache.config`).

v4 has two reading paths:

**Primary: In-memory `ProjectionState` populated by the projection consumer (3.4).**

The projection consumer is already translating every `PositionOpened`, `PositionChanged`, `PositionClosed`, and `AccountState` event from the Nautilus message bus stream. The same translation step updates a per-uvicorn-worker `ProjectionState` (defined in 3.4), so by the time a UI request arrives the state is already up-to-date in memory. PositionReader serves snapshot reads from this state directly — no Cache, no Redis round-trip.

**Fallback: Ephemeral `Cache` rebuilt per request, used only for the initial snapshot before any events have been observed.**

If `ProjectionState` has nothing for a `deployment_id` yet (e.g. a freshly-restarted FastAPI worker that hasn't processed any events), PositionReader builds a fresh `Cache`, calls `cache_all()`, reads, and discards the `Cache`. This is intentionally per-request — the Cache is a one-shot loader, not a live view, and treating it that way avoids drift. The cost (one Redis batch read per cold-start request) is acceptable: it only happens once per deployment per worker per restart.

```python
from nautilus_trader.cache.cache import Cache
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.cache.config import CacheConfig  # NOT common.config — Codex v3 P1 fix
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.model.identifiers import AccountId, StrategyId, TraderId


class PositionReader:
    """Snapshot reads of positions/accounts for the live UI.

    Read order:
    1. ProjectionState (in-memory, updated by the projection consumer
       in step 9 of 3.4) — fast path
    2. If absent (cold worker), build an ephemeral Cache, cache_all(),
       read, dispose — slow but correct path

    NEVER keeps a long-lived Cache. The Cache is a one-shot loader, not
    a live view (Codex v3 P1).
    """

    def __init__(
        self,
        projection_state: ProjectionState,
        redis_host: str,
        redis_port: int,
    ) -> None:
        self._state = projection_state
        self._cache_config = CacheConfig(
            database=DatabaseConfig(type="redis", host=redis_host, port=redis_port),
            encoding="msgpack",
        )

    async def get_open_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        # Fast path
        positions = self._state.positions(deployment_id)
        if positions:
            return positions
        # Cold path
        return await self._read_via_ephemeral_cache_positions(
            deployment_id, trader_id, strategy_id_full
        )

    async def get_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        # Fast path
        account = self._state.account(deployment_id)
        if account is not None:
            return account
        # Cold path
        return await self._read_via_ephemeral_cache_account(
            deployment_id, trader_id, account_id
        )

    async def _read_via_ephemeral_cache_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        """Build a fresh Cache, load, read, dispose. Per-request."""
        adapter = CacheDatabaseAdapter(
            trader_id=TraderId(trader_id),
            config=self._cache_config,
        )
        try:
            cache = Cache(database=adapter)
            cache.cache_all()
            raw_positions = cache.positions_open(strategy_id=StrategyId(strategy_id_full))
            return [self._to_snapshot(p, deployment_id) for p in raw_positions]
        finally:
            adapter.close()  # release the Redis connection promptly

    async def _read_via_ephemeral_cache_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        adapter = CacheDatabaseAdapter(
            trader_id=TraderId(trader_id),
            config=self._cache_config,
        )
        try:
            cache = Cache(database=adapter)
            cache.cache_all()
            account = cache.account(AccountId(account_id))
            return self._to_account_update(account, deployment_id) if account else None
        finally:
            adapter.close()
```

**Note on the actual `CacheDatabaseAdapter` constructor signature:** the exact parameters can vary between Nautilus releases. The integration test in this task pins the package version and verifies the constructor call works. If a future Nautilus upgrade changes the signature, the test fails loudly and the wrapper is updated in one place.

TDD:

1. Unit test fast-path: pre-populate `ProjectionState` with one position, call `get_open_positions`, verify the position is returned without touching Redis (mock the Redis client and assert no calls)
2. Unit test cold-path: empty `ProjectionState`, mock the ephemeral Cache to return one position, verify the position is returned and the adapter is closed
3. Integration test: start a minimal live subprocess writing to a testcontainers Redis with `CacheConfig.database = redis`, submit a synthetic order that opens a position, call `get_open_positions` from a fresh PositionReader (empty ProjectionState), verify the position appears via the cold path
4. Integration test: feed an event through the projection consumer, assert ProjectionState is updated, then call `get_open_positions` and assert the fast path serves it
5. Integration test: two deployments with distinct `trader_id`s — assert PositionReader correctly isolates them via the trader_id parameter
6. **Drift test (regression for Codex v3 P1)**: build a long-lived `Cache`, `cache_all()`, write a new position to Redis from a different process, re-read from the same long-lived `Cache` — assert it does NOT see the new position (proves the v3 design was wrong and the v4 ephemeral pattern is necessary)
7. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.1, 3.4 (ProjectionState definition lives there)
Gotchas: Codex v3 P1 (correct import path, ephemeral Cache, no long-lived Cache drift)

---

#### 3.6 — WebSocket broadcaster via Redis pub/sub

Files:

- `claude-version/backend/src/msai/api/websocket.py` (full rewrite)
- `claude-version/backend/tests/integration/test_websocket_live_events.py` (new)

Replaces the heartbeat-only WebSocket. The handler:

1. Auths via first-message JWT/API-key (existing contract)
2. Requires a `deployment_id` path or query parameter
3. On connect, sends a snapshot: current positions and account state from `PositionReader` (3.5) using the `trader_id` and `account_id` looked up from the `live_deployments` row
4. Subscribes to the Redis pub/sub channel `msai:live:events:{deployment_id}` via `aioredis.client.PubSub.subscribe`
5. Forwards each received JSON message to the WebSocket verbatim (the projection consumer already produced the stable internal-schema JSON in 3.4)
6. Sends an application-level heartbeat every 30s if idle
7. On disconnect, unsubscribes from the pub/sub channel

```python
@router.websocket("/api/v1/live/stream/{deployment_id}")
async def live_stream(
    websocket: WebSocket,
    deployment_id: UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    position_reader: PositionReader = Depends(get_position_reader),
) -> None:
    await websocket.accept()
    # First message must be bearer/API-key — existing contract
    try:
        await _authenticate(websocket)
    except AuthError:
        await websocket.close(code=4401)
        return

    deployment = await db.get(LiveDeployment, deployment_id)
    if deployment is None:
        await websocket.close(code=4404)
        return

    # Send initial snapshot
    positions = await position_reader.get_open_positions(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        strategy_id=deployment.strategy_id_full,
    )
    account = await position_reader.get_account(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        account_id=deployment.account_id,
    )
    await websocket.send_json({"type": "snapshot", "positions": [p.model_dump() for p in positions], "account": account.model_dump() if account else None})

    # Subscribe to pub/sub fan-out
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"msai:live:events:{deployment_id}")
    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            await websocket.send_text(message["data"].decode())
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        await pubsub.unsubscribe(f"msai:live:events:{deployment_id}")
        await pubsub.close()
```

**Multi-worker correctness:** Every uvicorn worker can serve this WebSocket because every worker subscribes to the same pub/sub channel. When the projection consumer (3.4) publishes an event, Redis delivers it to every subscribed worker, each of which forwards to its own connected clients. No in-memory state is shared across workers.

**Heartbeat is an application-level JSON `{"type": "heartbeat", "ts": ...}`**, not a TCP keepalive. Clients use it to detect dead sockets.

TDD:

1. Integration test: connect, expect snapshot with empty positions
2. Publish an event via `fanout.publish_event`, verify the WebSocket client receives it
3. Integration test: two WebSocket clients connected (simulate two uvicorn workers), publish one event, verify **both** receive it exactly once
4. Integration test: disconnect, verify pub/sub subscription is cleaned up
5. Implement

Effort: M
Depends on: 3.4, 3.5
Gotchas: Codex v2 P1 — pub/sub fan-out replaces in-memory queues

---

#### 3.7 — `RiskAwareStrategy` mixin (replaces custom RiskEngine subclass)

Files:

- `claude-version/backend/src/msai/services/nautilus/risk/risk_aware_strategy.py` (new)
- `claude-version/backend/tests/unit/test_risk_aware_strategy.py` (new)

Per the natives audit and Codex finding #2: the Nautilus `LiveRiskEngine` cannot be subclassed via config. We use a Strategy mixin instead.

**Portfolio API — correct method names per Codex v3 P1:**

The Nautilus `Portfolio` API has both per-instrument and per-venue accessors with **different names**:

| Scope                                  | PnL                                      | Exposure                                    | Returns                              |
| -------------------------------------- | ---------------------------------------- | ------------------------------------------- | ------------------------------------ |
| Per-instrument                         | `total_pnl(instrument_id)`               | `net_exposure(instrument_id)`               | `Money \| None`                      |
| Per-venue (aggregated, multi-currency) | `total_pnls(venue)` (plural)             | `net_exposures(venue)` (plural)             | `dict[Currency, Money]`              |
| Per-venue, per-currency                | `total_pnls(venue, target_currency=USD)` | `net_exposures(venue, target_currency=USD)` | `dict[Currency, Money]` (single key) |

v3 wrongly called `portfolio.total_pnl(venue)` (singular form, expects InstrumentId, not Venue). v4 uses the correct plurals for venue-level aggregates.

**Halt flag is no longer the primary kill switch (decision #16):**

v3 had the strategy poll `msai:risk:halt` on every `on_bar` and refuse new orders if set. That allowed up to one bar of lag on an emergency halt. v4 makes the kill switch push-based (decision #16 / task 3.9): the supervisor SIGTERMs every running deployment immediately on `/kill-all`, and Nautilus's `manage_stop=True` flattens automatically. The halt flag in the strategy mixin remains as **defense in depth** — a third layer to refuse new orders if for some reason the supervisor's stop didn't reach the subprocess (e.g., a network blip in the command bus). The lag is acceptable because it's the third layer, not the primary.

```python
from decimal import Decimal
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order


class RiskAwareStrategy(AuditedStrategy):
    """Strategy mixin that runs custom pre-submit risk checks BEFORE
    calling submit_order.

    Checks (in order):
    1. Sticky kill switch (Redis key msai:risk:halt, cached) — defense
       in depth; primary kill switch is push-based (3.9, decision #16)
    2. Per-strategy max position (per-instrument net position vs limit)
    3. Daily loss limit (per-venue total PnL via portfolio.total_pnls)
    4. Max notional exposure (per-venue net exposure via portfolio.net_exposures)
    5. Market hours (via MarketHoursService reading instrument_cache.trading_hours from Phase 2)

    On any failure: log a structured warning, write a "denied" row to
    order_attempt_audits, do NOT submit. Strategies use this by calling
    self.submit_order_with_risk_check(order) instead of self.submit_order.

    Built-in Nautilus checks (precision, native max_notional_per_order,
    rate limits) still run because we configure LiveRiskEngineConfig
    in 3.8 — this mixin is in addition to those, not instead.

    Uses the Portfolio API (self.portfolio.*), not direct Cache reads,
    because Portfolio is the stable Strategy-side abstraction for
    PnL and exposure aggregation.
    """

    def submit_order_with_risk_check(self, order: Order) -> None:
        venue = Venue(order.instrument_id.venue.value)
        instrument_id = order.instrument_id

        # 1. Kill switch (defense in depth — primary is 3.9)
        if self._halt_flag_cached:
            self._audit.write_denied(order, reason="risk:halt")
            return

        # 2. Per-strategy max position (per-instrument)
        position_for_instrument = self.portfolio.net_position(instrument_id)
        if not self._within_position_limit(order, position_for_instrument):
            self._audit.write_denied(order, reason="risk:position_limit")
            return

        # 3. Daily loss limit via portfolio.total_pnls(venue) — PLURAL
        # Returns dict[Currency, Money]; we sum across currencies after
        # converting to USD via target_currency.
        venue_pnls = self.portfolio.total_pnls(venue, target_currency=USD)
        if venue_pnls and not self._within_daily_loss_limit(venue_pnls):
            self._audit.write_denied(order, reason="risk:daily_loss")
            return

        # 4. Max notional exposure via portfolio.net_exposures(venue) — PLURAL
        venue_exposures = self.portfolio.net_exposures(venue, target_currency=USD)
        if venue_exposures and not self._within_exposure_limit(venue_exposures, order):
            self._audit.write_denied(order, reason="risk:exposure")
            return

        # 5. Market hours (Phase 4 task 4.3 provides MarketHoursService)
        if not self._within_market_hours(order):
            self._audit.write_denied(order, reason="risk:market_hours")
            return

        self.submit_order_with_audit(order)

    def _within_daily_loss_limit(self, pnls: dict[Currency, Money]) -> bool:
        """Sum the per-currency PnLs (already converted to USD by Nautilus
        because we passed target_currency=USD). Compare against the
        configured daily loss limit on the deployment.
        """
        total = sum(money.as_double() for money in pnls.values())
        return total > -float(self._risk_limits.daily_loss_limit_usd)

    def _within_exposure_limit(self, exposures: dict[Currency, Money], order: Order) -> bool:
        """Sum the venue-level net exposures (USD-converted) and add the
        order's notional. Reject if the projected total exceeds the limit.
        """
        current_total = sum(money.as_double() for money in exposures.values())
        order_notional = float(order.quantity) * float(order.price or 0)
        projected = current_total + order_notional
        return projected <= float(self._risk_limits.max_notional_exposure_usd)

    async def _refresh_halt_flag(self) -> None:
        """Called from on_bar via async task. Reads msai:risk:halt.

        Defense in depth — the primary kill switch is the supervisor
        SIGTERM in 3.9. This cached read just refuses any new orders the
        strategy might emit between the SIGTERM being sent and the
        subprocess actually exiting.
        """
        self._halt_flag_cached = bool(await self._redis.get("msai:risk:halt"))
```

**Why per-venue plurals not singulars:** `Portfolio.total_pnl(instrument_id)` is a per-instrument query. `Portfolio.total_pnls(venue)` (plural) returns the venue-level aggregate as `dict[Currency, Money]`. The same naming applies to `net_exposure(instrument_id)` vs `net_exposures(venue)`. v3 mixed them up (Codex v3 P1).

TDD:

1. Unit test each check in isolation with a mock `self.portfolio`
2. **Per-venue API test (regression for Codex v3 P1)**: assert the mixin calls `portfolio.total_pnls(venue, target_currency=USD)` (plural) NOT `portfolio.total_pnl(venue)` (singular)
3. **Per-instrument API test**: assert `net_position(instrument_id)` (not `net_position(venue)`)
4. **Multi-currency aggregation test**: feed a `dict[Currency, Money]` with USD and EUR, verify `_within_daily_loss_limit` sums them via `as_double()`
5. Test that orders pass through when within limits
6. Test that orders are denied when over limits, with the right `reason` on the audit row
7. Test that halt-flag refresh is called from `on_bar` before the risk check
8. Implement

Effort: L
Depends on: 1.11, 1.2 (audit table)
Gotchas: Codex v3 P1 (Portfolio API plurals vs singulars), decision #16 (halt flag is defense in depth, not primary)

---

#### 3.8 — Configure built-in `LiveRiskEngineConfig` with real limits

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/tests/unit/test_live_node_config_risk.py` (new)

Populate Nautilus's built-in risk engine with native throttles:

```python
risk_engine=LiveRiskEngineConfig(
    bypass=False,
    max_order_submit_rate="100/00:00:01",  # 100 per second
    max_order_modify_rate="100/00:00:01",
    max_notional_per_order={
        # Populated from RiskLimits on the deployment row
        "AAPL.NASDAQ": Decimal("100000"),
        # ...
    },
    debug=False,
)
```

The custom checks (per-strategy max position, daily loss, kill switch, market hours) are NOT here — they're in the `RiskAwareStrategy` mixin from 3.7. Nautilus's built-in handles only what it natively supports.

TDD:

1. Test that the live config installs the right native limits
2. Test that backtest config does NOT install live limits (uses defaults)
3. Implement

Effort: S
Depends on: 1.5
Gotchas: Codex #2

---

#### 3.9 — Push-based kill switch (supervisor SIGTERM + persistent halt flag)

Files:

- `claude-version/backend/src/msai/services/risk_engine.py` (extend existing)
- `claude-version/backend/src/msai/api/live.py` (modify `/kill-all`, add `/resume`)
- `claude-version/backend/tests/integration/test_kill_switch.py` (new)

v3 had the strategy poll the halt flag on every `on_bar` — up to one bar of lag (Codex v3 P2). v4 makes the kill switch a push, with three layers of defense (decision #16):

**Layer 1 — Persistent halt flag (prevents NEW starts):**

`POST /api/v1/live/kill-all` first sets `msai:risk:halt = true` in Redis with a long TTL (24h). The `/api/v1/live/start` endpoint reads this flag at the very top and returns 503 immediately if set. This blocks any new deployment from being launched while the kill is active.

**Layer 2 — Supervisor push (immediate flatten of running deployments):**

For every `live_node_processes` row with `status IN ('starting','building','ready','running')`, the kill-all endpoint publishes a `stop` command via `LiveCommandBus.publish_stop(deployment_id, reason="kill_switch")`. The supervisor processes these commands the same way it processes a normal `/stop`: SIGTERM the subprocess, escalate to SIGKILL after 30s. Because the strategy is built with `manage_stop = True` (decision #11), Nautilus's native exit loop cancels orders and flattens positions automatically when the strategy receives the stop signal.

Latency from operator click to flatten: roughly the `XADD` round-trip + the supervisor's `XREADGROUP BLOCK 5000` window + SIGTERM delivery + Nautilus stop. Realistically < 5 seconds. This is the **primary** mechanism.

**Layer 3 — Strategy in-process halt (defense in depth):**

The `RiskAwareStrategy` mixin (3.7) caches the halt flag and refuses new orders if set. This is the third layer — it catches any orders the strategy might emit between the supervisor sending SIGTERM and Nautilus actually processing it. The lag is acceptable because it's the third layer, not the primary.

```python
@router.post("/kill-all", status_code=200)
async def kill_all(
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
) -> KillAllResponse:
    # Layer 1: persistent halt flag
    await redis.set("msai:risk:halt", "true", ex=86400)
    await redis.set("msai:risk:halt:set_by", user.id.hex, ex=86400)
    await redis.set("msai:risk:halt:set_at", utcnow().isoformat(), ex=86400)

    # Layer 2: push stop commands to every active deployment
    rows = await db.execute(
        select(LiveNodeProcess).where(
            LiveNodeProcess.status.in_(("starting", "building", "ready", "running"))
        )
    )
    halted = 0
    for row in rows.scalars():
        await bus.publish_stop(row.deployment_id, reason="kill_switch")
        halted += 1

    logger.warning("kill_switch_triggered", deployments=halted, set_by=str(user.id))
    return KillAllResponse(deployments_halted=halted)


@router.post("/resume", status_code=200)
async def resume(
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
) -> ResumeResponse:
    """Clear the halt flag. Required before /start will accept new deployments.
    No auto-resume — operator must explicitly unblock."""
    await redis.delete("msai:risk:halt")
    logger.warning("kill_switch_resumed", resumed_by=str(user.id))
    return ResumeResponse(resumed=True)
```

`/api/v1/live/start` is updated (small change to 1.14) to read the halt flag at the very top:

```python
@router.post("/start", ...)
async def start_live_deployment(...):
    if await redis.exists("msai:risk:halt"):
        raise HTTPException(status_code=503, detail="Kill switch is active. POST /api/v1/live/resume to clear.")
    # ... rest of /start ...
```

TDD:

1. Integration test: start two stub subprocesses, call `/kill-all`, verify two stop commands published to the bus, verify halt flag set
2. Integration test: with halt flag set, call `/start`, verify 503 returned with the right detail
3. Integration test: call `/resume`, verify halt flag cleared and `/start` accepts new deployments
4. Integration test (real subprocess): start one trading subprocess via the supervisor with the smoke strategy, call `/kill-all`, verify the subprocess exits with status="stopped", verify position count is zero (manage_stop flatten worked)
5. **Latency test**: from `/kill-all` POST to subprocess `status="stopping"` row update, assert < 5 seconds
6. **Defense-in-depth test**: bypass the supervisor (don't actually publish stop commands), instead set the halt flag directly and call `RiskAwareStrategy.submit_order_with_risk_check` from a unit test — verify the order is denied with reason="risk:halt"
7. Implement

Effort: M
Depends on: 3.7, 1.6
Gotchas: decision #16 (push not poll), #13 (manage_stop handles flatten)

---

#### 3.10 — Frontend live page wired to real WebSocket events

Files:

- `claude-version/frontend/src/app/live-trading/page.tsx` (modify)
- `claude-version/frontend/src/components/live/positions-table.tsx` (modify)
- `claude-version/frontend/src/components/live/strategy-status.tsx` (modify)
- `claude-version/frontend/src/lib/use-live-stream.ts` (new hook)

Replace mock data with `useLiveStream(deploymentId)`. Vitest unit test for the hook with a mock WebSocket. Visual test against a running deployment (manual).

Effort: L
Depends on: 3.6

---

#### 3.11 — Phase 3 E2E

Files: `claude-version/backend/tests/e2e/test_live_streaming_phase3.py` (new)

1. Start the stack with paper IB Gateway
2. Deploy a strategy
3. Connect to `/api/v1/live/stream` WebSocket
4. Receive snapshot
5. Trigger a fill (via the smoke strategy from 1.15)
6. Verify the WebSocket receives the translated `FillEvent` within 5 seconds
7. Verify `PositionReader` returns the new position
8. POST `/api/v1/live/kill-all`
9. Verify both positions closed and the halt flag is set
10. POST `/api/v1/live/start` again — should fail due to halt
11. POST `/api/v1/live/resume`, then start succeeds

Effort: L
Depends on: 3.1–3.10

---

### Phase 3 task ordering

```
3.1, 3.2, 3.3 (parallel — config + schema only, no code-level conflicts)
  ↓
3.4 (depends on 3.2, 3.3)
  ↓
3.5 (depends on 3.1)
  ↓
3.6 (depends on 3.4, 3.5)
  ↓
3.7 (depends on 1.11, can start any time after Phase 1)
  ↓
3.8 (depends on 1.5, can start any time after Phase 1)
  ↓
3.9 (depends on 3.7)
  ↓
3.10 (depends on 3.6)
  ↓
3.11 (depends on all)
```

---

## Phase 4 — Recovery + Reconnect + Market Hours + Metrics

**Goal:** Production-grade resilience. Mostly enabling Nautilus's built-in features and testing them.

**Phase 4 acceptance (revised per Codex v3):**

- `LiveExecEngineConfig.reconciliation = True` runs at startup; the subprocess writes `status="ready"` only after `wait_until_ready()` (decision #14) confirms `is_running AND data_engine.is_connected AND exec_engine.is_connected AND not exec_engine.reconciliation_active AND len(cache.instruments()) > 0`
- `NautilusKernelConfig.load_state = True` and `save_state = True` are enabled; `EMACrossStrategy.on_save` and `on_load` are implemented and validated by a unit round-trip test
- A two-leg `BacktestNode` restart-continuity test against testcontainers Redis verifies: leg 2 loads state written by leg 1, the EMA values are continuous, the next bar does NOT generate a duplicate decision (decision #7 stable trader_id is what makes the cache keys collide across legs)
- Killing the FastAPI container does NOT interrupt trading (already true from Phase 1, re-tested in scenario A; the projection consumer reconnects and reclaims via XAUTOCLAIM)
- Killing the trading subprocess is detected by the supervisor's `reap_loop` within seconds (decision #15) and the row is flipped to `failed` with the real exit_code; if the supervisor is also dead, the heartbeat monitor flips it on the next supervisor restart
- IB Gateway disconnect for >2 minutes halts the strategy; on reconnect the strategy stays paused until manual `/resume`
- Equity strategies auto-pause outside RTH (using `instrument_cache.trading_hours` from Phase 2)
- Prometheus metrics exposed at `/metrics`
- **Scenario D dropped:** the live-feed restart assertion was non-deterministic. The deterministic equivalent is the two-leg `BacktestNode` integration test in 4.5, which exercises the exact same Nautilus save_state/load_state path with a Redis backend identical to production.

**The acceptance is NOT "strategy resumes" unconditionally** (Codex #10 correction): strategies either resume from validated state OR remain paused until operator manually warms them.

### Phase 4 tasks

#### 4.1 — Enable reconciliation + state persistence in live node config

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/tests/unit/test_live_node_config_recovery.py` (new)

```python
exec_engine=LiveExecEngineConfig(
    reconciliation=True,
    reconciliation_lookback_mins=1440,
    inflight_check_interval_ms=2000,
    inflight_check_threshold_ms=5000,
    position_check_interval_secs=60,
)

# In TradingNodeConfig:
load_state=True,  # gotcha — defaults to False; Codex #10
save_state=True,  # same
```

The trading subprocess in 1.8 writes `status="ready"` only after `wait_until_ready(node)` (decision #14) confirms is_running, both engines connected, reconciliation complete, and at least one instrument loaded. v3 wrongly assumed `kernel.start_async()` returning was equivalent to "reconciliation completed" — it isn't (Codex v3 P0). v4 verifies explicitly via the post-start health check.

TDD:

1. Live config has `load_state=True`, `save_state=True`, `reconciliation=True`
2. Backtest config has all three at default False
3. Implement

Effort: S
Depends on: 1.5
Gotchas: Codex #10, #11

---

#### 4.2 — IB disconnect handler with halt-on-extended-disconnect

Files:

- `claude-version/backend/src/msai/services/nautilus/disconnect_handler.py` (new)
- `claude-version/backend/tests/integration/test_disconnect_handler.py` (new)

Background task in the trading subprocess:

1. Subscribes to Nautilus's connection state events
2. On disconnect: starts a timer
3. If reconnect within `disconnect_grace_seconds` (default 120s): no action, log only
4. If grace expires: set local halt flag (via Redis kill switch from 3.9 with `reason="ib_disconnect"`), trigger flatten via `Strategy.on_stop`'s logic
5. Stays halted until manual `/resume` (consistent with Codex's "remain paused until warm" wording)

TDD:

1. Mock IB connection events: simulate disconnect+quick-reconnect, verify no halt
2. Simulate disconnect+timeout, verify halt
3. Simulate halt + reconnect, verify no auto-resume
4. Implement

Effort: M
Depends on: 3.9
Gotchas: relates to #10

---

#### 4.3 — Market hours awareness via `instrument_cache.trading_hours`

Files:

- `claude-version/backend/src/msai/services/nautilus/market_hours.py` (new)
- `claude-version/backend/tests/unit/test_market_hours.py` (new)

```python
class MarketHoursService:
    """Reads trading_hours from instrument_cache (Phase 2 task 2.2 + 2.4)
    and exposes is_in_rth(canonical_id, ts) -> bool.

    Used by RiskAwareStrategy._within_market_hours.
    """

    async def is_in_rth(self, canonical_id: str, ts: datetime) -> bool: ...
    async def is_in_eth(self, canonical_id: str, ts: datetime) -> bool: ...
```

Per-strategy `allow_eth: bool = False` config. If False (default), orders outside RTH are denied.

TDD:

1. AAPL at 10am ET (in RTH, true), at 3am ET (out, false)
2. ESM5 at 10am ET (in, futures trade ETH)
3. allow_eth=True bypasses
4. Implement

Effort: M
Depends on: 2.2, 2.4 (Phase 2 must populate trading_hours)
Gotchas: Codex #9

---

#### 4.4 — Orphaned-process detection (supervisor-side, heartbeat-based)

Files:

- `claude-version/backend/src/msai/live_supervisor/heartbeat_monitor.py` (extend — already introduced in 1.7)
- `claude-version/backend/src/msai/main.py` (lifespan — recovery discovery, NO PID probing)
- `claude-version/backend/src/msai/services/nautilus/recovery.py` (new — recovery discovery helper)
- `claude-version/backend/tests/integration/test_heartbeat_orphan_detection.py` (new)
- `claude-version/backend/tests/integration/test_recovery_on_startup.py` (new)

v2 proposed `os.kill(pid, 0)` from FastAPI to detect orphaned subprocesses. That doesn't work — FastAPI and the trading subprocess live in different container namespaces, so their PIDs are meaningless to each other (Codex v2 P0). v3 makes **heartbeat freshness** the sole liveness signal:

**Supervisor side (extend HeartbeatMonitor from 1.7):**

```python
class HeartbeatMonitor:
    """Runs inside the live-supervisor container.

    Every 10 seconds:
    1. Selects live_node_processes rows with status in ('starting','ready','running')
    2. For each row where last_heartbeat_at < now() - stale_seconds (default 30s):
       - Updates row: status='failed', error_message='heartbeat timeout'
       - Fires the AlertService with deployment_id, last_heartbeat_at, duration_stale
    3. Sleeps 10 seconds
    """
    async def _mark_stale_as_failed(self) -> None: ...
```

This is the **authoritative** orphan detector. It runs in the same container as the subprocess's parent (the supervisor spawned it via `mp.get_context("spawn").Process`), so even if the subprocess OS-died, the row's heartbeat will stop advancing and the monitor will flip it to `failed` within 30–40 seconds.

**FastAPI side (recovery discovery only):**

On FastAPI lifespan startup, FastAPI does NOT probe PIDs. It only:

1. Queries `live_node_processes` for rows with `status in ("ready", "running")` and `last_heartbeat_at > now() - stale_seconds`
2. For each, **registers** the deployment with the projection consumer so the consumer re-joins the Redis consumer group for that deployment's stream (3.4)
3. Logs "discovered N surviving deployments after API restart"

That's it. If a row is stale, the supervisor's heartbeat monitor will have already flipped it to `failed` — FastAPI trusts the row state.

```python
# claude-version/backend/src/msai/services/nautilus/recovery.py
async def discover_surviving_deployments(
    db: AsyncSession,
    stale_seconds: int = 30,
) -> list[LiveDeployment]:
    """Return live_deployments that are likely still running.

    Heartbeat-based only — never PID-probes across container namespaces.
    The supervisor is the sole authority on process liveness.
    """
    stmt = (
        select(LiveDeployment)
        .join(LiveNodeProcess, LiveNodeProcess.deployment_id == LiveDeployment.id)
        .where(
            LiveNodeProcess.status.in_(("ready", "running")),
            LiveNodeProcess.last_heartbeat_at > utcnow() - timedelta(seconds=stale_seconds),
        )
    )
    return (await db.execute(stmt)).scalars().all()
```

**Cache rehydration, reconciliation, and state persistence are all automatic via Nautilus config from 4.1.** The only recovery code this task adds is the heartbeat monitor (already scaffolded in 1.7) and the FastAPI-side "re-register the projection consumer" helper.

TDD:

1. Unit test `HeartbeatMonitor._mark_stale_as_failed` with a mocked clock — verify rows older than `stale_seconds` flip to `failed`, fresher rows do not
2. Integration test: insert a `live_node_processes` row with `last_heartbeat_at = now() - 60s`, run the monitor iteration once, verify the row is `status="failed"`
3. Integration test: insert a row with `last_heartbeat_at = now() - 5s`, verify the monitor leaves it alone
4. Integration test: start FastAPI with one running row (fresh heartbeat), verify `discover_surviving_deployments` returns it and the projection consumer re-registers
5. Integration test: start FastAPI with one stale row, verify `discover_surviving_deployments` does NOT return it (the supervisor owns the flip-to-failed)
6. Verify FastAPI never calls `os.kill` in recovery code (grep test in CI)
7. Implement

Effort: M
Depends on: 1.1, 1.7, 1.8
Gotchas: Codex v2 P0 (no PID probing across container namespaces)

---

#### 4.5 — Strategy state persistence + restart-continuity test (via BacktestNode twice)

Files:

- `claude-version/strategies/example/ema_cross.py` (modify)
- `claude-version/backend/tests/integration/test_ema_cross_save_load_roundtrip.py` (new)
- `claude-version/backend/tests/integration/test_ema_cross_restart_continuity.py` (new)

Implement `on_save` and `on_load` on `EMACrossStrategy`:

```python
def on_save(self) -> dict[str, bytes]:
    """Persist EMA indicator state. Called by Nautilus kernel on shutdown
    when save_state=True.
    """
    return {
        "fast_ema_value": str(self.fast_ema.value).encode(),
        "slow_ema_value": str(self.slow_ema.value).encode(),
        "last_position_state": str(self._last_position_state).encode(),
        "last_decision_bar_ts": str(self._last_decision_bar_ts_ns or 0).encode(),
        "version": b"1",
    }

def on_load(self, state: dict[str, bytes]) -> None:
    """Restore EMA indicator state. Called by Nautilus kernel on startup
    when load_state=True.
    """
    if not state or state.get("version") != b"1":
        return  # Cold start
    self.fast_ema.update_raw(float(state["fast_ema_value"].decode()))
    self.slow_ema.update_raw(float(state["slow_ema_value"].decode()))
    self._last_position_state = state["last_position_state"].decode()
    self._last_decision_bar_ts_ns = int(state["last_decision_bar_ts"].decode()) or None
```

**Idempotency key (`last_decision_bar_ts`):** Nautilus replays any un-processed bars from its cache on restart. To prevent a duplicate decision on the first bar after restart, the strategy records the `ts_event` of the last bar that produced a trade decision. On restart, `on_bar` checks `bar.ts_event > self._last_decision_bar_ts_ns` before acting. This is the pattern that makes restart-continuity achievable without operator intervention.

**Why BacktestNode twice, not a live subprocess restart:** v2 proposed to restart a live TradingNode subprocess and feed it the next bar. That requires a deterministic bar feeder we don't have — IB Gateway's live feed is not reproducible. `BacktestNode` gives us deterministic, reproducible bar feeding AND full Nautilus kernel lifecycle (including `on_save`/`on_load`). It's the correct test vehicle.

**Why testcontainers Redis (NOT an "on-disk KV store"):** v3 invented an on-disk KV-store `StateSerializer` that does not exist in this Nautilus install — `nautilus_trader.common.config.DatabaseConfig` only supports `type="redis"` here (Codex v3 P1). v4 brings up a testcontainers Redis and points both BacktestNode runs at it via `CacheConfig.database = redis`. Nautilus writes the strategy state via its native cache-backed save_state path, the same path the live system uses.

The **restart-continuity test**:

```python
@pytest.fixture(scope="module")
def redis_container() -> Iterator[Redis]:
    """Spin up an isolated Redis for this test only."""
    from testcontainers.redis import RedisContainer
    with RedisContainer("redis:7-alpine") as container:
        yield container


def test_ema_cross_restart_continuity(redis_container) -> None:
    """Two-leg test against testcontainers Redis:

    Leg 1: Run BacktestNode on a N-bar catalog that triggers an EMA cross.
           save_state=True writes strategy state to the Redis-backed cache
           via Nautilus's native CacheDatabaseAdapter.

    Leg 2: New BacktestNode with the SAME trader_id and the SAME
           CacheConfig pointing at the SAME Redis. load_state=True reads
           the prior state on startup. Feed bar (N+1) only. Assert:
           (a) EMA fast/slow at the start of leg 2 == end of leg 1 (continuity)
           (b) No duplicate order on bar N+1 (idempotency via last_decision_bar_ts)
           (c) A subsequent "signal" bar still emits a new decision (not frozen)
    """
    redis_host = redis_container.get_container_host_ip()
    redis_port = redis_container.get_exposed_port(6379)
    cache_config = CacheConfig(
        database=DatabaseConfig(type="redis", host=redis_host, port=int(redis_port)),
        encoding="msgpack",
    )

    catalog = build_deterministic_catalog(n_bars=120, ema_cross_at_bar=60)
    common_kwargs = dict(
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        catalog_path=catalog,
        cache_config=cache_config,
        trader_id="MSAI-restart-test-0001",  # stable across both legs (decision #7)
    )

    # Leg 1: bars 0..99, crossing at bar 60, expect ≥1 decision; save_state=True
    result_a = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=False,
        save_state=True,
        bars_range=(0, 100),
    )
    assert len(result_a.orders) >= 1, "expected at least one order in leg 1"

    # Verify state was actually written to Redis (sanity)
    redis_client = Redis(host=redis_host, port=int(redis_port))
    state_keys = redis_client.keys(f"trader-MSAI-restart-test-0001:*")
    assert len(state_keys) > 0, "leg 1 should have written state to Redis"

    # Leg 2: bar 100 ONLY, load_state=True, expect NO duplicate
    result_b = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=True,
        save_state=True,
        bars_range=(100, 101),
    )
    assert len(result_b.orders) == 0, "leg 2 bar should not emit a duplicate"

    # Continuity check: leg 2's pre-bar EMA state matches leg 1's final state
    assert result_b.initial_fast_ema == pytest.approx(result_a.final_fast_ema, rel=1e-9)
    assert result_b.initial_slow_ema == pytest.approx(result_a.final_slow_ema, rel=1e-9)

    # Freshness check: a bar that WOULD trigger a new decision still does
    result_c = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=True,
        save_state=True,
        catalog_path=build_deterministic_catalog(n_bars=121, ema_cross_at_bar=120),
        bars_range=(100, 121),
    )
    assert len(result_c.orders) >= 1
```

`run_backtest` is a test helper that wraps `BacktestNode` with the kernel's `load_state`/`save_state` flags and the Redis `CacheConfig`. The same trader_id across both legs is what makes the cache keys collide and the state reload — enforced by decision #7 (stable deployment_slug).

**Why this is a test of the production path, not a test fixture artifact:** the live system also uses `CacheConfig.database = redis` (3.1) and `load_state=True`/`save_state=True` (4.1) with the same trader_id pattern. The integration test exercises the exact same code path Nautilus runs in production — only the data feed differs (deterministic bars instead of IB live).

**Separate round-trip test** (simpler, faster):

```python
def test_ema_cross_on_save_on_load_roundtrip() -> None:
    """Pure unit test: construct an EMA strategy, populate it, call
    on_save, construct a fresh instance, call on_load, assert state
    is restored."""
    strat = EMACrossStrategy(config=...)
    strat.fast_ema.update_raw(100.5)
    strat.slow_ema.update_raw(99.2)
    strat._last_position_state = "LONG"
    state = strat.on_save()

    fresh = EMACrossStrategy(config=...)
    fresh.on_load(state)
    assert fresh.fast_ema.value == pytest.approx(100.5)
    assert fresh.slow_ema.value == pytest.approx(99.2)
    assert fresh._last_position_state == "LONG"
```

TDD:

1. Round-trip unit test (above)
2. Two-leg BacktestNode restart-continuity integration test against testcontainers Redis (above)
3. Implement

Effort: M
Depends on: 3.1 (Redis CacheConfig), 4.1, 1.8
Gotchas: #16, Codex v2 P1 (BacktestNode twice, not live subprocess restart), Codex v3 P1 (testcontainers Redis, not on-disk KV-store), decision #7 (stable trader_id across legs)

---

#### 4.6 — Prometheus metrics

Files:

- `claude-version/backend/src/msai/services/observability/metrics.py` (new)
- `claude-version/backend/src/msai/main.py` (mount `/metrics`)
- `claude-version/backend/tests/integration/test_metrics_endpoint.py` (new)

`prometheus_client`-based registry:

- Counters: `msai_orders_submitted_total`, `msai_orders_filled_total`, `msai_orders_rejected_total`, `msai_orders_denied_total`, `msai_deployments_started_total`, `msai_deployments_failed_total`, `msai_kill_switch_triggered_total`
- Gauges: `msai_active_deployments`, `msai_position_count{deployment_id}`, `msai_daily_pnl_usd{deployment_id}`, `msai_unrealized_pnl_usd{deployment_id}`, `msai_ib_connected{deployment_id}`
- Histograms: `msai_order_submit_to_fill_ms`, `msai_reconciliation_duration_seconds`

The trading subprocess writes counter increments to a Redis key pattern; the FastAPI projection consumer reads them and exposes them via `/metrics`. Pure Nautilus events (no custom subprocess metric exporter required).

TDD:

1. `/metrics` returns Prometheus format
2. Metrics non-zero after a synthetic event
3. Implement

Effort: M
Depends on: 3.4

---

#### 4.7 — Phase 4 E2E (three scenarios)

Files: `claude-version/backend/tests/e2e/test_recovery_phase4.py` (new)

**Scenario A: Kill FastAPI mid-trade**

1. Deploy strategy
2. Wait for `status="running"`
3. `docker kill msai-claude-backend`
4. Sleep 5s
5. `docker compose up -d backend`
6. Verify trading subprocess still running (heartbeat advancing — supervisor's reap loop and heartbeat monitor are both unaffected by the FastAPI restart)
7. Verify `GET /api/v1/live/status/{deployment_id}` discovers it from the database
8. Verify the projection consumer joins the consumer group, runs `XAUTOCLAIM` to recover any pending entries from the previous instance's PEL, and resumes publishing to pub/sub

**Scenario B: Kill TradingNode subprocess**

1. Deploy strategy
2. SIGKILL the trading subprocess pid directly
3. Within 1–2 seconds: verify the supervisor's `reap_loop` (decision #15) flips the row to `status="failed"` with the real exit_code (-9 for SIGKILL)
4. Verify an alert was emitted
5. **(Recovery path)** SIGKILL the supervisor as well, then `docker compose up -d live-supervisor` — verify the supervisor's heartbeat monitor flips any other lingering rows to `failed` based on stale heartbeat (decision #15 fallback path)

**Scenario C: Disconnect IB Gateway**

1. Deploy strategy
2. `docker pause msai-claude-ib-gateway`
3. Wait 130 seconds (past `disconnect_grace_seconds`)
4. Verify the strategy halted (orders cancelled, positions closed via `manage_stop=True`)
5. `docker unpause msai-claude-ib-gateway`
6. Verify the strategy stays halted (manual resume required)
7. POST `/api/v1/live/resume`
8. Verify the strategy is restartable

**Scenario D — DROPPED in v4.** Codex v3 P1 flagged that the original Scenario D (restart a live deployment, feed it the next live bar, assert no duplicate) is non-deterministic against IB Gateway: the next live bar's contents and timing are uncontrollable, and flake risk is high. The same contract is now proven by the deterministic two-leg `BacktestNode` integration test in 4.5, which exercises the exact same Nautilus save_state/load_state path with a Redis backend identical to production (testcontainers Redis). The E2E loses no production-relevant coverage.

Effort: L
Depends on: 4.1–4.6

---

## Phase 5 — Paper Soak Release Gate (NOT implementation)

**Documentation only.** Exists in the plan so "Phase 4 done" cannot be misread as "ready for real money."

### 5.1 — Paper soak procedure

Document at `claude-version/docs/paper-soak-procedure.md`:

- **Duration:** 30 calendar days minimum
- **Account:** IB paper account, separate from real
- **Strategies:** start with one (EMA Cross on AAPL+MSFT), add one new instrument per week if no incidents
- **Monitoring:** daily PnL email, Prometheus alerts on API down, subprocess down, IB disconnect >2 min, reconciliation failure, halt set, manual review of audit log every Friday
- **Incidents:** any P0/P1 incident restarts the 30-day clock
- **Exit:** 30 consecutive days zero P0/P1 incidents AND manual sign-off AND audit log review

### 5.2 — Release sign-off checklist

Document at `claude-version/docs/release-signoff-checklist.md`:

- [ ] 30-day paper soak completed without incident
- [ ] All Phase 1–4 E2E tests passing on the latest commit
- [ ] All unit + integration tests passing
- [ ] Architecture review re-run by Claude + Codex against the latest code, no P0/P1/P2 findings
- [ ] Disaster recovery runbook tested
- [ ] Operator confirms emergency contact for IB account
- [ ] Initial real-money allocation: max $1,000, hard cap in `LiveRiskEngineConfig.max_notional_per_order`

**No code commits in this phase.**

---

## Cross-Cutting Concerns

### Test Strategy

TDD per task. Test pyramid: unit (every function/class) + integration (DB, Redis, subprocess) + E2E (full stack at end of each phase, gated by `MSAI_E2E_IB_ENABLED=1`).

### Logging and Observability

Structured logging with `deployment_id`, `strategy_id`, `client_order_id` context starting in Phase 1.

### Database Migrations

Each task adds an Alembic migration. Migration tests in `tests/integration/`.

### Backwards Compatibility

Existing backtest pipeline keeps working at every phase boundary. Phase 2 includes a migration script for existing `*.SIM` catalogs.

### Parallelization Notes

- **Phase 1** is fully sequential (1.1 → 1.16) — Codex #13 was correct that the original "Group D parallelizable" claim was wrong
- **Phase 2** has the parallelization map under section 2 above
- **Phase 3** has the map under section 3 above
- **Phase 4** is mostly sequential

---

## Open Questions

1. **IB account credentials in Key Vault** — who provisions the paper IB account and where do credentials live?
2. **Redis cluster vs single instance** — single is fine for Phase 3 dev; production may want redundancy. Defer to Phase 6.
3. **Postgres connection pooling under multi-deployment load** — may need pgbouncer in production. Defer.
4. **Strategy state schema versioning** — `on_save` payload changes require version handling. Defer until we have a second strategy.
5. **Multi-currency PnL** — defer until we have a non-USD strategy.

## Risks

1. **Nautilus version drift** — pin exact version; run upgrade tests in a separate branch.
2. **IB Gateway flakiness** — paper soak in Phase 5 is the mitigation.
3. **Subprocess orchestration complexity** — narrow contract (DB rows + Redis), no IPC primitives.
4. **Catalog migration data loss** — idempotent + dry-run mode + test against a copy.
5. **Phase boundary slippage** — re-evaluate scope at each phase boundary.

---

## How To Use This Plan

- **Future Claude Code sessions**: pick the next pending task in the lowest pending phase. Read the architecture review, the Nautilus reference, the natives audit, and the relevant gotchas before implementing. Do not skip TDD.
- **Codex CLI working in parallel on `codex-version/`**: this plan is Claude-only. Codex CLI can use this as a template for codex-version's own plan.
- **The user**: each task is sized to fit a single working session.
- **Phase boundaries are checkpoints**: don't start Phase N+1 until Phase N's E2E passes.

---

**Plan version:** 4.0
**Last updated:** 2026-04-07
**Approved by:** [pending Codex v4 re-review]

## Revision history

- **v1.0** (2026-04-06): initial 5-phase plan after architecture review
- **v2.0** (2026-04-06): incorporates Codex review of v1 (1 P0 + 9 P1 + 3 P2 fixed) and Nautilus natives audit (deletes 6 reinventing tasks, simplifies Phase 4 dramatically)
- **v3.0** (2026-04-06): incorporates Codex re-review of v2 (2 P0 + 7 P1 fixed) covering container topology and process ownership.
  - Dedicated `live-supervisor` Docker service replaces arq-hosted supervision (arq on_startup deadlock)
  - Heartbeat-only liveness detection replaces cross-container PID probing
  - Deterministic `trader_id`/`order_id_tag` from `deployment_slug`; deployments are now stably identifiable across restarts
  - `stream_per_topic = False` — one deterministic Redis Stream per trader so FastAPI can subscribe at deployment start
  - Redis pub/sub per deployment for WebSocket fan-out (multi-uvicorn-worker correctness)
  - FastAPI uses Nautilus `Cache` Python API instead of raw Redis key reads
  - `StrategyConfig.manage_stop = True` replaces custom `on_stop` flatten code
  - Parity harness redesigned: determinism test + config round-trip + intent contract (no more "TradingNode against IB paper")
  - Restart-continuity test uses `BacktestNode` run twice (deterministic bar feed) instead of live subprocess restart
  - `RiskAwareStrategy` uses `self.portfolio.account()/total_pnl()/net_exposure()` (stable Strategy-side API) instead of raw cache reads
- **v4.0** (2026-04-07): incorporates Codex re-review of v3 (3 P0 + 5 P1 + 2 P2 fixed) covering Redis Streams semantics, idempotency, the readiness gate, and the identity/schema model.
  - **Stable `deployment_slug`** decoupled from `live_deployments.id`. The slug is created once and reused across restarts so Phase 4 state reload actually works. 16 hex chars (64 bits) instead of 8 (32 bits).
  - **Redis Streams PEL recovery** via explicit `XAUTOCLAIM`. v3 wrongly assumed un-ACKed messages are auto-redelivered. They aren't — they sit in the per-consumer PEL until claimed. Both `LiveCommandBus` and the projection consumer recover stale pending entries explicitly.
  - **Idempotency at three layers**: DB-level partial unique index on `live_node_processes(deployment_id) WHERE status IN active`, supervisor-level pre-spawn check + ACK-only-on-success, HTTP-level `Idempotency-Key` header with Redis cache.
  - **Post-start health check** before writing `status="ready"`. `kernel.start_async()` returning is NOT proof of readiness. v4 polls `is_running and data_engine.is_connected and exec_engine.is_connected and not reconciliation_active and instruments_loaded > 0` until timeout.
  - **`live_deployments` schema migration** (task 1.1b) adds `deployment_slug`, `trader_id`, `strategy_id_full`, `account_id`, `message_bus_stream`, `instruments_signature`, `last_started_at`, `last_stopped_at`. Logical de-duplication via unique index on `(started_by, strategy_id, paper_trading, instruments_signature)`.
  - **`live_node_processes.pid` is nullable**, `building` added to status enum (v3 inconsistency fixed).
  - **`PositionReader` rebuilt**: in-memory `ProjectionState` populated by the projection consumer is the fast path; ephemeral per-request `Cache` is the cold-start fallback. Long-lived `Cache` instances are gone (they don't subscribe to Redis updates and would drift). Import path corrected.
  - **`RiskAwareStrategy` portfolio API names corrected**: `total_pnls(venue)` and `net_exposures(venue)` (plural — venue-level aggregates returning `dict[Currency, Money]`), not `total_pnl(venue)` / `net_exposure(venue)` (singular — those expect `InstrumentId`).
  - **Restart-continuity test against testcontainers Redis** instead of an invented "on-disk KV-store StateSerializer" (which doesn't exist in this Nautilus install). Two `BacktestNode` runs share the same Redis-backed cache via the same trader_id.
  - **Phase 4 Scenario D dropped**: live-feed restart was non-deterministic. The deterministic two-leg `BacktestNode` integration test in 4.5 covers the same contract with no production-relevant coverage loss.
  - **Supervisor keeps `dict[deployment_id, mp.Process]` handle map** for instant in-process exit detection via a `reap_loop` background task. Heartbeat is now only the cross-restart recovery signal.
  - **Kill switch is push-based**, not bar-poll. `/kill-all` sets the persistent halt flag AND publishes stop commands to the supervisor for every running deployment AND keeps the strategy-side mixin halt check as defense in depth. Latency from operator click to flatten < 5 seconds.
