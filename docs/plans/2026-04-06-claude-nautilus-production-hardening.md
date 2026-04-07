# Claude — Nautilus Production Hardening (Revision 2)

**Status:** Plan v2 (incorporates Codex review + Nautilus natives audit)
**Branch:** `feat/claude-nautilus-production-hardening`
**Scope:** `claude-version/` ONLY. The `codex-version/` directory is not touched by this plan; Codex CLI is hardening that codebase independently in parallel.

## References

- `docs/plans/2026-04-06-architecture-review.md` — the architecture review that produced this plan
- `docs/nautilus-reference.md` — deep technical reference on NautilusTrader (60KB, 10 sections, 20 gotchas)
- `docs/nautilus-natives-audit.md` — what Nautilus already provides natively vs what we have to build
- `.claude/rules/nautilus.md` — auto-loaded short-form gotchas list
- `docs/plans/2026-04-06-claude-nautilus-production-hardening.md` (this file)

## What changed in revision 2

Codex reviewed revision 1 and flagged 1 P0 + 9 P1 + 3 P2 issues. A separate Nautilus natives audit found that ~30% of revision 1 reinvented things Nautilus already provides. Revision 2 fixes both.

**Architectural changes:**

1. **Live trading subprocess is hosted by the arq worker, not the FastAPI process.** The FastAPI API publishes start/stop commands to a Redis command stream. A long-running supervisor task inside the arq worker consumes that stream and spawns the actual `multiprocessing.Process` running the `TradingNode`. This resolves the Codex P0 — killing the FastAPI container has no effect on running trading subprocesses, because FastAPI never owned them.
2. **Custom `RiskEngine` subclass is DELETED.** The Nautilus kernel instantiates `LiveRiskEngine` directly from `LiveRiskEngineConfig` and cannot be subclassed. We use a strategy-side `RiskAwareStrategy` mixin with a `pre_submit_check()` method instead, plus the built-in `LiveRiskEngineConfig.max_notional_per_order` for native throttles.
3. **`PositionSnapshotCache` is DELETED.** Nautilus's `Cache` with `CacheConfig.database = redis` already persists positions. The FastAPI projection layer reads positions directly via Redis Streams events from the message bus.
4. **Cache rehydration smoke test is DELETED.** Rehydration happens automatically in `node.build()`. The Phase 4 restart-continuity test is the real verification.
5. **Crash recovery is dramatically simplified.** Reconciliation is automatic via `LiveExecEngineConfig.reconciliation = True`. State persistence is automatic via `NautilusKernelConfig.load_state = True` + `save_state = True`. We just enable the flags and implement `on_save`/`on_load` on strategies. The only manual work is orphaned-process detection.
6. **Reconciliation gating is replaced with a 2-line marker.** The trading subprocess writes `status="ready"` to `live_node_processes` after `kernel.start_async()` returns (the kernel internally completes reconciliation before that returns). No log scraping, no private internals.
7. **`buffer_interval_ms = 0` corrected to `None`** (the fields are `PositiveInt | None`, not `int`).
8. **Redis stream topic names corrected** to Nautilus's actual format: `events.order.{strategy_id}`, `events.position.{strategy_id}`, `events.account.{account_id}`.
9. **Phase 3 projection consumer uses Redis consumer groups** with persisted offsets so FastAPI downtime cannot lose events.
10. **Audit table gains a `client_order_id` correlation key** so a single audit row can be updated through the order lifecycle.
11. **Strategy code hash is computed from strategy file bytes at deploy time**, not via `git rev-parse HEAD` (the container only mounts `src/` and `strategies/`, not the repo root).
12. **Phase 1 E2E uses a deterministic smoke strategy** that submits one tiny market order on the first bar, so the test actually proves the order path end-to-end.
13. **Phase 2 `instrument_cache` table now stores `trading_hours` metadata** so the Phase 4 market-hours guard has something to read.
14. **`GET /api/v1/live/status/{deployment_id}` route is explicitly added** as a Phase 1 task (was missing).
15. **Phase 1 task ordering corrected:** the previously "parallelizable" Group D (1.7/1.8/1.10) all hot-edited the same files; revision 2 numbers them sequentially.

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

**6. Trading subprocesses are hosted by the arq worker, not by FastAPI.**

The control plane:

```
┌──────────────────┐                         ┌────────────────────┐
│  FastAPI         │                         │  arq worker        │
│  /api/v1/live/*  │                         │  process           │
│                  │  publish command        │                    │
│  POST /start ────┼──> Redis stream ────────┼─> supervisor task  │
│  POST /stop      │  msai:live:commands     │  (long-running)    │
│                  │                         │       │            │
│  GET /status     │                         │       │ spawn      │
│  reads from      │  Postgres               │       v            │
│  live_node_      │                         │  ┌──────────────┐  │
│  processes  ◄────┼──── status / heartbeat ─┼──┤ TradingNode  │  │
│  table           │                         │  │ subprocess   │  │
└──────────────────┘                         │  │ (mp.Process, │  │
                                             │  │ daemon=False)│  │
                                             │  └──────────────┘  │
                                             └────────────────────┘
```

- FastAPI publishes `{"action": "start", "deployment_id": ..., ...}` to `msai:live:commands` Redis stream
- The arq worker has a long-running supervisor task subscribed to that stream via consumer groups
- Supervisor calls `multiprocessing.get_context("spawn").Process(target=_trading_node_subprocess, daemon=False).start()`
- Trading subprocess runs `node.run()`, periodically updates `live_node_processes.last_heartbeat_at`
- API queries `live_node_processes` table for status (does not maintain in-memory state)
- Killing FastAPI: trading subprocess is unaffected (FastAPI never owned it)
- Killing arq worker: trading subprocess survives (it's not a daemon child of the worker either, since `daemon=False`; the spawned process is independent)

**7. Each phase ends with a docker-based E2E test** that exercises the actual subprocess lifecycle, IB Gateway, Postgres, Redis, and (where relevant) the frontend.

---

## Phase 1 — Live Node + Worker Supervisor + Audit

**Goal:** Claude can launch a real Nautilus `TradingNode` against IB Gateway paper, supervised by the arq worker, with deployment registry, structured logging, order audit, and a deterministic E2E that proves the order path.

**Phase 1 acceptance:**

- `POST /api/v1/live/start` publishes a command to the Redis stream
- The arq worker supervisor receives it and spawns a real subprocess
- Subprocess builds a `TradingNode`, connects to IB Gateway paper, completes reconciliation, transitions to `status="ready"`
- The deterministic smoke strategy submits a tiny AAPL market order on the first bar
- The order is recorded in `order_attempt_audits` with `client_order_id`, then updated through accepted/filled
- Killing the FastAPI container has zero effect on the trading subprocess
- After API restart, `GET /api/v1/live/status/{deployment_id}` finds the surviving subprocess via the registry
- `POST /api/v1/live/stop` publishes a stop command, the strategy cancels orders + closes positions in `on_stop`, the subprocess calls `node.stop_async()` and `dispose()`, exits cleanly

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
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)  # docker container hostname
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # values: starting | ready | running | stopping | stopped | failed
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
```

TDD: write a model integration test that creates a row and queries it back; then write the model + migration.

Acceptance: integration test green; `alembic upgrade head` succeeds on a fresh database.

Effort: S
Depends on: nothing
Gotchas: none

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

#### 1.6 — Redis command stream (start/stop messages from API to worker)

Files:

- `claude-version/backend/src/msai/services/live_command_bus.py` (new)
- `claude-version/backend/tests/integration/test_live_command_bus.py` (new)

```python
LIVE_COMMAND_STREAM = "msai:live:commands"
LIVE_COMMAND_GROUP = "live-supervisor"

class LiveCommandBus:
    """Thin wrapper over Redis Streams for the API ↔ worker control plane.

    Why a stream not pub/sub: pub/sub messages are lost if no consumer is
    listening. Streams + consumer groups give us durable delivery and
    let us survive worker restarts.
    """

    async def publish_start(self, deployment_id: UUID, payload: dict) -> str:
        """Publish a start command. Returns the Redis stream entry ID."""

    async def publish_stop(self, deployment_id: UUID) -> str:
        """Publish a stop command."""

    async def consume(self, consumer_id: str) -> AsyncIterator[LiveCommand]:
        """Consume from the stream as part of LIVE_COMMAND_GROUP. Used by
        the worker supervisor in 1.7. ACKs are explicit so a crashed
        supervisor can replay un-ACKed messages on restart.
        """
```

TDD:

1. Integration test against testcontainers Redis: publish 3 commands, consume them, ACK each, verify they're not redelivered
2. Test that an un-ACKed command IS redelivered to a new consumer in the same group
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: nothing
Gotchas: none (Codex finding #5 informs the consumer group choice)

---

#### 1.7 — Live-node supervisor task in the arq worker

Files:

- `claude-version/backend/src/msai/workers/live_supervisor.py` (new)
- `claude-version/backend/src/msai/workers/settings.py` (modify — add the supervisor as a startup task)
- `claude-version/backend/tests/integration/test_live_supervisor.py` (new)

The arq worker registers a startup task that:

1. Joins the `LIVE_COMMAND_GROUP` consumer group on `LIVE_COMMAND_STREAM`
2. Loops forever consuming commands
3. On `start` command: spawns `multiprocessing.get_context("spawn").Process(target=_trading_node_subprocess, args=(payload,), daemon=False).start()`. Inserts a `live_node_processes` row with `pid` from `process.pid`, `status="starting"`, then ACKs the command
4. On `stop` command: looks up the row, sends SIGTERM to the pid, waits for `status="stopped"` or timeout; ACKs the command

The supervisor never tracks Process objects in memory after spawn — all state lives in `live_node_processes`. If the worker restarts, the supervisor reconnects to the consumer group and resumes consuming; existing trading subprocesses continue running independently because `daemon=False`.

TDD:

1. Unit test the start handler with a mock subprocess + mock DB
2. Unit test the stop handler
3. Integration test: publish a start command, verify a row is inserted and a real subprocess starts (use a no-op stub TradingNode); verify killing the worker does NOT kill the subprocess
4. Implement

Acceptance: tests pass.

Effort: L
Depends on: 1.1, 1.5, 1.6
Gotchas: #18 (asyncio.run conflict — supervisor uses arq's existing event loop)

---

#### 1.8 — Trading subprocess entry point

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (full rewrite)
- `claude-version/backend/tests/unit/test_trading_node_subprocess.py` (new)

Top-level function (must be importable for `spawn` pickling):

```python
def _trading_node_subprocess(payload: _LiveNodePayload) -> None:
    """Entry point for the live trading subprocess.

    Runs in a fresh Python interpreter under the spawn context. Steps:

    1. Import nautilus_trader (this installs uvloop policy globally — gotcha #1)
    2. Reset asyncio event loop policy to default (already needed in
       workers/settings.py for the same reason; we replicate here because
       this is a fresh interpreter)
    3. Connect to Postgres, write LiveNodeProcess.status="building"
    4. Build the TradingNodeConfig via build_live_trading_node_config
    5. Construct TradingNode
    6. Register IB factories under key "IB"
    7. node.build()
    8. Start the heartbeat task (1.9)
    9. Start the audit hook (1.11)
    10. Write LiveNodeProcess.status="ready" — kernel.start_async() runs
        reconciliation BEFORE returning, so when run() begins we know
        reconciliation is complete (gotcha #5/#10 are handled by the kernel)
    11. node.run() — blocks until stop signal
    12. finally:
        - Heartbeat task cancelled
        - Strategy on_stop runs (cancels orders + closes positions per gotcha #13)
        - node.stop_async() — graceful Nautilus shutdown
        - node.dispose() — releases Rust logger and sockets (gotcha #20)
        - LiveNodeProcess.status="stopped", exit_code recorded
    """
```

The subprocess installs a SIGTERM handler that calls `node.stop_async()` then exits. The supervisor (1.7) sends SIGTERM on stop commands.

TDD:

1. Unit test the function with all `nautilus_trader` imports mocked: verify policy reset is called, verify the status state machine writes the right rows, verify dispose() is called in finally
2. Unit test that an exception inside `node.run()` still triggers the finally block
3. Implement

Acceptance: tests pass.

Effort: L
Depends on: 1.1, 1.5
Gotchas: #1 (uvloop), #5 (connection timeout — but kernel handles it), #10 (reconciliation — kernel handles it), #13 (stop doesn't close — strategy handles in on_stop), #18 (asyncio.run), #20 (dispose)

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

#### 1.10 — Stop sequence (Strategy `on_stop` flattens; node disposes)

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend with SIGTERM handler)
- `claude-version/strategies/example/ema_cross.py` (extend `on_stop`)
- `claude-version/backend/tests/integration/test_trading_node_stop.py` (new)

The supervisor sends SIGTERM to the subprocess pid on a stop command. The subprocess's signal handler:

1. Updates `live_node_processes.status="stopping"`
2. Calls `node.stop_async()` — Nautilus shuts down all engines gracefully

Inside `Strategy.on_stop()` (called by Nautilus during shutdown):

```python
def on_stop(self) -> None:
    self.cancel_all_orders(self.config.instrument_id)
    self.close_all_positions(self.config.instrument_id)
```

These are `Strategy` methods (not `TradingNode` methods — Codex finding #6). The strategy does the flattening; the node just orchestrates the shutdown.

If the subprocess does not exit within 30 seconds of SIGTERM, the supervisor escalates to SIGKILL and marks `status="failed"`, `error_message="hard kill on stop timeout"`.

TDD:

1. Integration test: spawn subprocess with stub strategy, send SIGTERM, verify `cancel_all_orders` + `close_all_positions` were called, verify exit_code=0 and status="stopped"
2. Integration test: spawn a subprocess that ignores SIGTERM, verify SIGKILL escalation and status="failed"
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.7, 1.8
Gotchas: #13 (stop doesn't close — fixed by Strategy.on_stop)

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

#### 1.14 — Wire `/api/v1/live/start` and `/stop` to the command bus

Files:

- `claude-version/backend/src/msai/api/live.py` (modify start/stop endpoints)
- `claude-version/backend/tests/integration/test_live_start_stop_endpoints.py` (new)

`POST /api/v1/live/start`:

1. Create the `live_deployments` row with `strategy_code_hash` from 1.12
2. Publish a start command to the Redis stream via `LiveCommandBus.publish_start`
3. Poll `live_node_processes` for `status="ready"` or `status="failed"` with timeout (30s default)
4. On `ready`: return 201 + deployment_id
5. On `failed`: return 503 + error message
6. On timeout: return 504, supervisor will eventually clean up

`POST /api/v1/live/stop`:

1. Publish a stop command via `LiveCommandBus.publish_stop`
2. Poll `live_node_processes` for `status in ("stopped", "failed")` with timeout (30s)
3. Return 200

TDD:

1. Integration test: start endpoint publishes to stream, mocked supervisor flips status to "ready", endpoint returns 201
2. Integration test: stop endpoint publishes, mocked supervisor flips status to "stopped"
3. Test timeouts return the right HTTP status codes
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.6, 1.13
Gotchas: none

---

#### 1.15 — Deterministic smoke strategy

Files:

- `claude-version/strategies/example/smoke_market_order.py` (new)
- `claude-version/backend/tests/unit/test_smoke_strategy.py` (new)

```python
class SmokeMarketOrderStrategy(AuditedStrategy):
    """Submits exactly ONE tiny market order on the first bar received,
    then sits idle. Used by the Phase 1 E2E to prove the order path
    end-to-end.

    Why: the EMA strategy may not cross during a short E2E window
    (Codex finding #8). The smoke strategy is deterministic.
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

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)
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

#### 2.11 — Parity validation harness (revised tolerance model)

Files:

- `claude-version/scripts/parity_check.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/normalizer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/comparator.py` (new)
- `claude-version/backend/tests/integration/test_parity_check.py` (new)

The harness takes a strategy file, config, instrument, time window. It runs:

1. **Backtest leg**: existing backtest runner, produces normalized `OrderIntent` records `(decision_timestamp_bucket, instrument_id, side, signed_intent_qty)`. The bucket is the bar close timestamp — we align decisions to bar boundaries because both legs make decisions on bar close.

2. **Historical paper replay leg**: spawns a `TradingNode` against IB Gateway paper, replays the same time window from the catalog (NOT live IB ticks — we want determinism). Captures the same normalized `OrderIntent` records.

The comparator (corrected per Codex finding #12):

- **Required exact match**:
  - same `(decision_timestamp_bucket, instrument_id, side, signed_intent_qty)` for each decision
  - same decision sequence by bucket ordering
  - same end-of-window position trajectory
  - **neither side has extra decisions** (Codex #12)
- **Required match within tolerance**:
  - aggregate filled qty per intent: exact
  - VWAP within `max(1 tick, configured slippage budget)`
- **NOT compared**:
  - exact fill timestamps
  - exact fill counts (paper-live can partial-fill)
  - commissions (compared separately after a fee model is configured in Phase 4)

The harness reports diffs as a structured table.

The plan documentation explicitly notes: **"the live leg of the parity test is historical paper replay, not true live shadow parity. Live shadow parity is impractical for a unit-test-style harness because it requires real-time IB ticks and the same wall-clock window in two processes."**

TDD:

1. Unit test the normalizer
2. Unit test the comparator with known diffs
3. Integration test on a 1-day AAPL window with the EMA strategy

Acceptance: parity check passes.

Effort: L
Depends on: 2.5, 2.6, 2.9, 1.8
Gotchas: #14 (handled via tolerance)

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

- A live deployment publishes events through Nautilus's `MessageBusConfig.database = redis` to Redis Streams
- A FastAPI projection consumer reads those streams via **consumer groups** (durable, no event loss on FastAPI restart)
- The consumer translates Nautilus events to a stable internal schema and pushes to the WebSocket
- The `/live` page shows real-time positions, fills, and PnL
- The `RiskAwareStrategy` mixin blocks an order that would breach a per-strategy max position, using REAL position data from the Nautilus Cache (which is persisted to Redis via `CacheConfig.database = redis`)
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
    encoding="msgpack",  # gotcha #17 — JSON fails on Decimal/datetime/Path
    stream_per_topic=True,
    use_trader_prefix=True,
    use_trader_id=True,
    streams_prefix="stream",
    buffer_interval_ms=None,  # write-through; Codex #3
)
```

This causes Nautilus to publish events to Redis streams named like:

- `trader-{trader_id}-stream-events.order.{strategy_id}`
- `trader-{trader_id}-stream-events.position.{strategy_id}`
- `trader-{trader_id}-stream-events.account.{account_id}`

These are the **actual** stream names per Codex #4 (not `events.order.filled` as the original plan said).

TDD: parallel to 3.1.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, #17, Codex #3, #4

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

#### 3.4 — Redis Streams consumer with **consumer groups + offset persistence**

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/consumer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/translator.py` (new)
- `claude-version/backend/tests/integration/test_projection_consumer.py` (new)

Background asyncio task in the FastAPI process that:

1. Joins a Redis consumer group `msai-projection` on each Nautilus stream
2. Consumes via `XREADGROUP` (durable: un-ACKed messages survive FastAPI restart)
3. Decodes Nautilus events using `MsgSpecSerializer` from `nautilus_trader.serialization.serializer`
4. Translates each Nautilus event to the internal schema (3.3) via `translator.py`
5. Pushes the internal event onto a per-deployment in-memory queue that the WebSocket broadcaster reads from
6. ACKs the Redis stream message via `XACK` only after the queue push succeeds
7. On FastAPI startup, the consumer reconnects to the group and processes any pending (un-ACKed) messages

The translator is a pure function `translate(nautilus_event) -> InternalEvent`. One mapper per Nautilus event type. Comprehensive switch.

**No TTL on positions** — Codex finding #5. Position snapshots live as long as the position is open. They're cleaned up on `PositionClosed` events, not on a timer.

TDD:

1. Unit test translator with each Nautilus event type
2. Integration test: publish a synthetic `OrderFilled` event to a Redis stream, verify the consumer receives it via the group, translates it, and ACKs
3. Integration test: kill the consumer mid-message, restart, verify the un-ACKed message is redelivered
4. Implement

Acceptance: tests pass.

Effort: L
Depends on: 3.2, 3.3
Gotchas: #17, Codex #5

---

#### 3.5 — Position state from Nautilus Cache (NOT a separate snapshot cache)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/position_reader.py` (new)
- `claude-version/backend/tests/integration/test_position_reader.py` (new)

The natives audit DELETED the planned `PositionSnapshotCache`. Instead, FastAPI reads positions from Nautilus's own Cache (which is backed by Redis from 3.1):

```python
class PositionReader:
    """Reads current positions from the Nautilus Redis-backed Cache.

    Why not a separate snapshot cache: Nautilus's Cache (via
    CacheConfig.database = redis) already persists positions and
    accounts. A separate snapshot would be a parallel state machine
    that drifts from Nautilus's source of truth.

    This service either:
    (a) reads from the same Redis keys Nautilus writes (preferred), or
    (b) builds a transient Nautilus Cache instance pointed at the same
        Redis backend and queries it via cache.positions_open()
    """

    async def get_open_positions(self, deployment_id: UUID) -> list[PositionSnapshot]: ...
    async def get_account(self, deployment_id: UUID) -> AccountStateUpdate: ...
```

TDD:

1. Integration test: spawn a stub TradingNode, submit a synthetic order, read positions back via PositionReader
2. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.1
Gotchas: none

---

#### 3.6 — WebSocket broadcaster wired to projection

Files:

- `claude-version/backend/src/msai/api/websocket.py` (full rewrite)
- `claude-version/backend/tests/integration/test_websocket_live_events.py` (new)

Replaces the heartbeat-only WebSocket. The handler:

1. Auths via first-message JWT/API-key
2. Optionally accepts a `deployment_id` filter
3. On connect, sends a snapshot: current positions and account state from `PositionReader`
4. Subscribes to the per-deployment in-memory queue from 3.4
5. Streams each translated internal event as JSON
6. Sends a heartbeat every 30s if idle

TDD:

1. Integration test connects, expects snapshot
2. Push an event to the queue, verify client receives it
3. Multi-deployment fan-out
4. Implement

Effort: M
Depends on: 3.4, 3.5

---

#### 3.7 — `RiskAwareStrategy` mixin (replaces custom RiskEngine subclass)

Files:

- `claude-version/backend/src/msai/services/nautilus/risk/risk_aware_strategy.py` (new)
- `claude-version/backend/tests/unit/test_risk_aware_strategy.py` (new)

Per the natives audit and Codex finding #2: the Nautilus `LiveRiskEngine` cannot be subclassed via config. We use a Strategy mixin instead.

```python
class RiskAwareStrategy(AuditedStrategy):
    """Strategy mixin that runs custom pre-submit risk checks BEFORE
    calling submit_order.

    Checks (in order):
    1. Sticky kill switch (Redis key msai:risk:halt) — if set, deny
    2. Per-strategy max position (read from RiskLimits on the deployment row)
    3. Daily loss limit (read PnL from Nautilus Cache aggregated)
    4. Max notional exposure (sum of all open positions × current marks)
    5. Market hours (read from instrument_cache.trading_hours from Phase 2)

    On any failure: log a structured warning, write a "denied" row to
    order_attempt_audits, do NOT submit. Strategies use this by calling
    self.submit_order_with_risk_check(order) instead of self.submit_order.

    Built-in Nautilus checks (precision, native max_notional_per_order,
    rate limits) still run because we configure LiveRiskEngineConfig
    in 3.8 — this mixin is in addition to those, not instead.
    """

    def submit_order_with_risk_check(self, order: Order) -> None:
        if self._is_halted():
            self._audit.write_denied(...)
            return
        if not self._within_position_limit(order):
            self._audit.write_denied(...)
            return
        if not self._within_daily_loss_limit():
            self._audit.write_denied(...)
            return
        if not self._within_market_hours(order):
            self._audit.write_denied(...)
            return
        self.submit_order_with_audit(order)
```

The mixin queries Nautilus Cache directly via `self.cache.positions_open(...)` and `self.cache.account(...)`.

TDD:

1. Test each check in isolation with a mock cache
2. Test that submitted orders pass through when within limits
3. Test that orders are denied when over limits, with audit row
4. Implement

Effort: L
Depends on: 1.11, 1.2 (audit table)
Gotchas: Codex #2 (don't subclass LiveRiskEngine)

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

#### 3.9 — Sticky kill switch in Redis

Files:

- `claude-version/backend/src/msai/services/risk_engine.py` (extend existing)
- `claude-version/backend/src/msai/api/live.py` (modify `/kill-all`, add `/resume`)
- `claude-version/backend/tests/integration/test_kill_switch.py` (new)

`POST /api/v1/live/kill-all`:

1. Sets Redis key `msai:risk:halt = true` with a long TTL
2. Publishes a "flatten all positions" command to the live command bus for every running deployment
3. Each subprocess's strategy receives the command via the Strategy event hook (or via the pre-existing `Strategy.on_event` mechanism), invokes `cancel_all_orders` + `close_all_positions`, then waits for new instructions
4. The `RiskAwareStrategy` mixin reads `msai:risk:halt` on every `on_bar` and refuses new orders
5. Returns count of deployments halted

`POST /api/v1/live/resume`: clears the halt flag (manual, requires explicit operator action).

TDD:

1. Integration test: start two subprocesses, set kill-all, verify both close positions, verify the halt flag prevents new starts
2. Implement

Effort: M
Depends on: 3.7
Gotchas: #13

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

**Phase 4 acceptance (revised per Codex):**

- `LiveExecEngineConfig.reconciliation = True` runs at startup; the subprocess writes `status="ready"` only after `kernel.start_async()` returns (which means reconciliation completed)
- `NautilusKernelConfig.load_state = True` and `save_state = True` are enabled; `EMACrossStrategy.on_save` and `on_load` are implemented and validated by a round-trip test
- A restart-continuity test verifies: after subprocess restart, the strategy resumes from validated state AND the next bar does NOT generate a duplicate decision
- Killing the FastAPI container does NOT interrupt trading (already true from Phase 1, re-tested here)
- Killing the trading subprocess is detected by the API (heartbeat stops) and the deployment is marked failed with an alert
- IB Gateway disconnect for >2 minutes halts the strategy; on reconnect the strategy stays paused until manual `/resume`
- Equity strategies auto-pause outside RTH (using `instrument_cache.trading_hours` from Phase 2)
- Prometheus metrics exposed at `/metrics`

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

The trading subprocess in 1.8 already writes `status="ready"` after `kernel.start_async()` returns. Since the kernel internally awaits reconciliation BEFORE returning (`live/node.py` lifecycle), the readiness signal IS the reconciliation completion signal. No log scraping, no private internals (Codex #11 fix).

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

#### 4.4 — Crash recovery: orphaned process detection

Files:

- `claude-version/backend/src/msai/main.py` (lifespan)
- `claude-version/backend/src/msai/services/nautilus/recovery.py` (new)
- `claude-version/backend/tests/integration/test_recovery_on_startup.py` (new)

In the FastAPI lifespan:

1. Query `live_node_processes` for rows with `status in ("ready", "running")`
2. For each, check if pid is alive via `os.kill(pid, 0)`
3. If alive: log discovery, leave alone (the subprocess keeps running, the API rejoins the consumer group for projection events)
4. If dead: mark `status="failed"`, `error_message="orphaned after API restart"`, alert via existing alerting service

This is the only manual recovery code. Cache rehydration, reconciliation, state persistence are all automatic via Nautilus config from 4.1.

TDD:

1. Insert a row with a known-dead pid → status flips to "failed"
2. Insert a row with the current process pid → left alone
3. Implement

Effort: M
Depends on: 1.1, 1.8
Gotchas: none

---

#### 4.5 — Strategy state persistence + restart-continuity test

Files:

- `claude-version/strategies/example/ema_cross.py` (modify)
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
```

The **restart-continuity test** (Codex #10):

1. Start a TradingNode subprocess with the EMA strategy
2. Feed bars until the EMAs cross and the strategy submits an order
3. SIGTERM the subprocess (graceful)
4. Restart the subprocess with the same `deployment_id`
5. Feed the next bar
6. Assert the strategy did NOT submit a duplicate order on the first bar after restart
7. Assert the EMA values are continuous (no jump back to initial state)

TDD: round-trip test PLUS the integration restart-continuity test.

Effort: M
Depends on: 4.1, 1.8
Gotchas: #16, Codex #10

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
6. Verify trading subprocess still running (heartbeat advancing)
7. Verify `GET /api/v1/live/status/{deployment_id}` discovers it
8. Verify the projection consumer rejoins the consumer group and resumes streaming events from where it left off

**Scenario B: Kill TradingNode subprocess**

1. Deploy strategy
2. SIGKILL the trading subprocess pid directly
3. Wait 30s (heartbeat stale)
4. Verify the API marks the deployment as failed via Phase 4 task 4.4
5. Verify an alert was emitted

**Scenario C: Disconnect IB Gateway**

1. Deploy strategy
2. `docker pause msai-claude-ib-gateway`
3. Wait 130 seconds (past `disconnect_grace_seconds`)
4. Verify the strategy halted (orders cancelled, positions closed)
5. `docker unpause msai-claude-ib-gateway`
6. Verify the strategy stays halted (manual resume required)
7. POST `/api/v1/live/resume`
8. Verify the strategy is restartable

**Scenario D: Restart with state persistence**

1. Deploy EMA strategy with `save_state=True`, `load_state=True` (Phase 4 config)
2. Feed bars until EMAs are populated
3. Stop deployment via `/api/v1/live/stop` (graceful)
4. Restart deployment with the same strategy
5. Verify EMA state is restored (not reset to zero)
6. Verify the strategy does NOT submit a duplicate order on the first bar after restart

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

**Plan version:** 2.0
**Last updated:** 2026-04-06
**Approved by:** [pending Codex re-review]

## Revision history

- **v1.0** (2026-04-06): initial 5-phase plan after architecture review
- **v2.0** (2026-04-06): incorporates Codex review (1 P0 + 9 P1 + 3 P2 fixed) and Nautilus natives audit (deletes 6 reinventing tasks, simplifies Phase 4 dramatically)
