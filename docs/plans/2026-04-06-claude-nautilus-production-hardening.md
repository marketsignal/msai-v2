# Claude — Nautilus Production Hardening

**Status:** Plan (not yet executing)
**Branch:** `feat/claude-nautilus-production-hardening`
**Scope:** `claude-version/` ONLY. The `codex-version/` directory is not touched by this plan; Codex CLI can do its own equivalent work in parallel.
**References:**

- `docs/plans/2026-04-06-architecture-review.md` — the architecture review that produced this plan
- `docs/nautilus-reference.md` — deep technical reference on NautilusTrader (60KB, 10 sections, 20 gotchas)
- `.claude/rules/nautilus.md` — auto-loaded short-form gotchas list

---

## Goal

Production-harden the Claude implementation of MSAI v2 so that it can safely run a personal hedge fund:

- Real Nautilus `TradingNode` for live trading via Interactive Brokers (currently a stub)
- Real security master that handles stocks, futures, options, indexes, FX (currently fake `TestInstrumentProvider.equity(SIM)`)
- Backtest and live use the **same** strategy code, the **same** instrument IDs, the **same** event contract
- Real-time positions, fills, and PnL visible in the dashboard
- Risk engine integrated into the order path with real inputs (currently hardcoded zeros)
- Crash recovery and broker reconciliation on restart
- Order audit trail for every submission attempt
- 30-day paper soak as a release gate before real money

## Non-Goals

- The `codex-version/` codebase. This plan does not modify it. Codex CLI can implement an equivalent plan in parallel.
- Multi-user / multi-tenant support. This is a single-trader personal platform.
- Distributed deployment beyond a single Azure VM (deferred to a future Phase 6+).
- Crypto venues. This plan covers IB-supported asset classes only (stocks, ETFs, indexes, futures, options, FX). Crypto can be added later via a different exchange adapter.

## Approach

Five phases. Each phase ends with a demonstrable improvement and a docker-based E2E verification. Phases are strictly sequential: Phase 2 depends on Phase 1, Phase 3 depends on Phase 2, etc. **Tasks within a phase can be parallelized**, so multiple sessions (or me + Codex) can pick up tasks independently as long as they're in the same phase.

Every task uses TDD: a failing test is written first, then the implementation, then refactor.

---

## Pre-Phase Decisions (Locked Before Phase 1)

These choices are locked here so every phase can rely on them. Codex review explicitly called out that delaying these would block Phase 2.

**1. Canonical symbology: `IB_SIMPLIFIED`**
Live IB instruments use the form `<symbol>.<exchange>` — e.g. `AAPL.NASDAQ`, `EUR/USD.IDEALPRO`, `ESM5.XCME`. **Not** `AAPL.IBKR` or `AAPL.SIM`.

**2. Backtest instruments use the same canonical IDs as live**
A backtest of AAPL uses `AAPL.NASDAQ`. The current `*.SIM` rebinding in `claude-version/backend/src/msai/services/nautilus/instruments.py:45` is removed in Phase 2.

**3. Live IB venue suffixes are real exchanges**
Equities → `NASDAQ`, `NYSE`, `ARCA`. FX → `IDEALPRO`. Futures → `XCME`, `XCBT`, `GLOBEX`. Options → underlying exchange. Indexes → `CBOE`, `NASDAQ`, `XNAS`.

**4. The Nautilus IB client factory key stays `"IB"`**
This is not the venue. It's the registration key for `node.add_data_client_factory("IB", ...)` and `node.add_exec_client_factory("IB", ...)`. Confused with venue strings in earlier work.

**5. Audit + structured logging start in Phase 1**
Not Phase 4. We need them while debugging the live path.

**6. Trading subprocesses are independent of the FastAPI process**
A trading node lives as a subprocess of the arq worker (which itself is a separate Docker service). Killing the FastAPI container must NOT kill any running trading subprocess. The API discovers running subprocesses via a Postgres-backed `live_node_processes` table on startup.

**7. Each phase ends with a docker-based E2E test**
Not just unit tests. The E2E exercises the actual subprocess lifecycle, IB Gateway, Postgres, Redis, and (where relevant) the frontend.

---

## Phase 1 — Live Node + Bootstrap + Health/Audit

**Goal:** Claude can launch a real Nautilus `TradingNode` in a subprocess against a paper IB Gateway, with safe minimal real instruments, deployment registry, structured logging, and order-attempt audit.

**Phase 1 acceptance:**

- `POST /api/v1/live/start` spawns a real subprocess that boots a `TradingNode`
- IB connection succeeds, reconciliation completes, the strategy receives bars from IB
- Strategy submits market orders; the order audit table records every attempt
- Killing the FastAPI process with `docker kill` does NOT kill the trading subprocess
- After API restart, `GET /api/v1/live/status` discovers the surviving subprocess via the Postgres registry
- `POST /api/v1/live/stop` cancels orders, closes positions, and exits cleanly

### Phase 1 tasks

#### 1.1 — Add `live_node_processes` table + model

Files:

- `claude-version/backend/src/msai/models/live_node_process.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_live_node_processes.py` (new)
- `claude-version/backend/tests/integration/test_live_node_process_model.py` (new)

Schema:

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

TDD:

1. Write `test_live_node_process_model.py` that creates a row, queries it back, asserts fields
2. Write the model + migration
3. Run integration test against testcontainers Postgres

Acceptance: integration test green; `alembic upgrade head` succeeds on a fresh database.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 1.2 — Add `order_attempt_audit` table + model

Files:

- `claude-version/backend/src/msai/models/order_attempt_audit.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_order_attempt_audit.py` (new)
- `claude-version/backend/tests/integration/test_order_attempt_audit_model.py` (new)

Schema:

```python
class OrderAttemptAudit(Base, TimestampMixin):
    __tablename__ = "order_attempt_audits"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
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

TDD: same pattern as 1.1.

Acceptance: model integration test green, migration applies.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 1.3 — Enhance structured logging (deployment_id context var)

Files:

- `claude-version/backend/src/msai/core/logging.py` (modify)
- `claude-version/backend/tests/unit/test_logging.py` (extend)

Add a `deployment_id` context variable that gets injected into every log record by the existing structlog processors. Add a helper `bind_deployment(deployment_id)` that returns a context manager.

TDD:

1. Test that `with bind_deployment(uuid)` causes subsequent log calls inside it to include `deployment_id`
2. Implement
3. All existing logging tests still pass

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 1.4 — Minimal real instrument bootstrap (NOT `TestInstrumentProvider`)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py` (new)
- `claude-version/backend/tests/unit/test_live_instrument_bootstrap.py` (new)

Function:

```python
def build_ib_instrument_provider_config(
    symbols: list[str],
) -> InteractiveBrokersInstrumentProviderConfig:
    """Build an InstrumentProviderConfig that loads real IB contracts for the
    given paper-trading symbols.

    For Phase 1 we use a hand-curated mapping of symbol → IBContract for the
    1-2 paper symbols we'll test with. Phase 2 replaces this with the full
    SecurityMaster service.

    Why not TestInstrumentProvider: gotcha #9 — TestInstrumentProvider
    instruments use a fake venue suffix that the IB exec client cannot route
    to. Live trading needs real IB contract details loaded through the IB
    instrument provider path.
    """
```

The hand-curated mapping:

```python
_PHASE_1_PAPER_SYMBOLS = {
    "AAPL": IBContract(secType="STK", symbol="AAPL", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
    "MSFT": IBContract(secType="STK", symbol="MSFT", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
}
```

TDD:

1. Test that `build_ib_instrument_provider_config(["AAPL"])` returns a config whose `load_contracts` contains exactly the AAPL `IBContract`
2. Test that an unknown symbol raises `ValueError`
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #9 (instrument not pre-loaded fails on first bar event), #11 (don't call load_async at runtime)

---

#### 1.5 — Live `TradingNodeConfig` builder

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (new)
- `claude-version/backend/tests/unit/test_live_node_config.py` (new)

Function:

```python
def build_live_trading_node_config(
    deployment_id: UUID,
    strategy_path: str,
    strategy_config: dict,
    paper_symbols: list[str],
    ib_settings: IBSettings,
) -> TradingNodeConfig:
    """Build the TradingNodeConfig used by the live trading subprocess.

    - data_clients["IB"]: InteractiveBrokersDataClientConfig with instrument
      provider from build_ib_instrument_provider_config
    - exec_clients["IB"]: InteractiveBrokersExecClientConfig pointed at the
      configured paper port (4002) with the configured account_id
    - exec_engine: LiveExecEngineConfig(reconciliation=True,
      reconciliation_lookback_mins=1440, position_check_interval_secs=60)
    - data_engine: LiveDataEngineConfig() with default queue size
    - cache, message_bus: NOT yet configured (Phase 3 adds Redis backends)
    - strategies: ImportableStrategyConfig pointing at strategy_path

    Each call gets a unique ibg_client_id (offset by hash of deployment_id) so
    multiple concurrent deployments don't collide (gotcha #3).
    """
```

Validation that the function refuses to build a config if:

- `paper_symbols` is empty
- `ib_settings.port == 4001` (live port) and `ib_settings.account_id` starts with `DU` (paper account → mismatch, gotcha #6)
- `ib_settings.port == 4002` (paper port) and `ib_settings.account_id` does NOT start with `DU`

TDD:

1. Test happy path
2. Test each validation rejection
3. Test that two calls with different deployment IDs produce different `ibg_client_id`
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.4
Gotchas: #3 (client_id collision), #6 (port/account mismatch)

---

#### 1.6 — Replace `TradingNodeManager` stub with real subprocess

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (full rewrite)
- `claude-version/backend/tests/unit/test_trading_node_manager.py` (new)
- `claude-version/backend/tests/integration/test_trading_node_subprocess.py` (new)

Subprocess entry point (top-level function for pickling under `spawn`):

```python
def _trading_node_subprocess(payload: _LiveNodePayload) -> None:
    """Subprocess entry point for a live TradingNode.

    Order matters here — see gotcha #1 (uvloop policy) and gotcha #18
    (asyncio.run conflict). We:
    1. Import nautilus_trader (this installs uvloop policy globally)
    2. Reset the policy to default so any nested asyncio code doesn't fight
       arq's expectations (already needed in workers/settings.py for the same
       reason)
    3. Build the TradingNodeConfig
    4. Construct TradingNode
    5. Register the IB factories under the key "IB"
    6. node.build()
    7. Start the heartbeat task
    8. node.run() — blocks
    9. finally: cancel orders, close positions (gotcha #13), node.dispose()
       (gotcha #20), update LiveNodeProcess.status="stopped"
    """
```

`TradingNodeManager` (the parent-side service):

- `start(deployment_id, ...)`:
  1. Insert `LiveNodeProcess` row with status="starting", pid=-1
  2. Spawn subprocess via `multiprocessing.get_context("spawn").Process(target=_trading_node_subprocess, ...)` — **NOT** `daemon=True`
  3. Update row with the real pid
  4. Return immediately (readiness is checked by 1.7)
- `stop(deployment_id)`: send SIGTERM to the pid, wait, escalate to SIGKILL on timeout, update status
- `status(deployment_id)`: read from `LiveNodeProcess` table

Critical: when FastAPI starts up, the `TradingNodeManager` does NOT keep an in-memory dict of processes. All process state lives in the database. This is the difference vs both Claude's old stub and Codex's daemon-child approach.

TDD:

1. Unit test `_trading_node_subprocess` with all `nautilus_trader` imports mocked: verify the policy reset is called, verify cancel_all + close_all + dispose are called in finally
2. Unit test `TradingNodeManager.start()` patching `multiprocessing.Process`: verify a row is inserted, the process is started, the pid is updated
3. Integration test that actually spawns a subprocess against a stub `TradingNode` that just sleeps

Acceptance: tests pass; an integration test successfully spawns + stops a subprocess.

Effort: L
Depends on: 1.1, 1.5
Gotchas: #1 (uvloop), #13 (stop doesn't close), #18 (asyncio.run), #20 (dispose)

---

#### 1.7 — Heartbeat task in subprocess

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend)
- `claude-version/backend/tests/integration/test_trading_node_heartbeat.py` (new)

Background asyncio task running inside the subprocess that updates `LiveNodeProcess.last_heartbeat_at = now()` every 5 seconds. Started after `node.build()`, stopped in the `finally` block.

The heartbeat is the canary the parent process uses to detect dead subprocesses (Phase 4).

TDD:

1. Integration test that spawns a stub subprocess (no actual TradingNode), verifies the row's `last_heartbeat_at` advances by ≥1 second after waiting 6 seconds, then stops the subprocess and verifies the heartbeat stops.

Acceptance: integration test green.

Effort: S
Depends on: 1.1, 1.6
Gotchas: none

---

#### 1.8 — Startup readiness check

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend)
- `claude-version/backend/src/msai/api/live.py` (modify `/start` endpoint)
- `claude-version/backend/tests/unit/test_live_start_readiness.py` (new)

`TradingNodeManager.wait_until_ready(deployment_id, timeout_seconds)` polls `LiveNodeProcess.status` until it transitions from "starting" → "ready" or until the timeout fires. The subprocess writes `status="ready"` only after:

1. `node.build()` completed
2. The IB client connection probe succeeds (read `node.is_running == True` is **not** sufficient — gotcha #5)
3. Reconciliation reported complete (gotcha #10): we wait for the subprocess's local cache to contain at least the strategy's required instruments + the account state

If the timeout fires:

- Send SIGTERM to the subprocess
- Mark the deployment failed with a clear error message
- Return 503 from `/api/v1/live/start`

The `/api/v1/live/start` endpoint awaits `wait_until_ready` before returning success.

TDD:

1. Unit test the readiness state machine with patched DB queries
2. Integration test that simulates a slow-starting subprocess (writes "ready" after 3s) and verifies wait_until_ready returns success
3. Integration test that simulates a never-ready subprocess and verifies the timeout fires + SIGTERM is sent

Acceptance: tests pass.

Effort: M
Depends on: 1.6, 1.7
Gotchas: #5 (connection timeout dormant), #10 (reconciliation timeout)

---

#### 1.9 — Audit hook for order submissions inside the subprocess

Files:

- `claude-version/backend/src/msai/services/nautilus/audit_hook.py` (new)
- `claude-version/backend/tests/unit/test_audit_hook.py` (new)

A small Strategy mixin (or a custom RiskEngine wrapper) that intercepts `submit_order` calls and writes to `order_attempt_audits` BEFORE the broker sees the order. Then on `on_order_accepted`, `on_order_rejected`, `on_order_filled`, `on_order_cancelled`, it updates the same row's `status` and `broker_order_id`.

The audit row includes `strategy_code_hash` (already computed by the worker for backtests; reused here) and `strategy_git_sha` (read from `subprocess.run(['git', 'rev-parse', 'HEAD'])` at subprocess startup, cached).

TDD:

1. Mock submit_order, verify a row is written with status="submitted" and the right strategy_code_hash
2. Fire on_order_accepted, verify the row updates to status="accepted" and broker_order_id is set
3. Fire on_order_rejected, verify status="rejected" and reason populated
4. Fire on_order_filled, verify status="filled"
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.2
Gotchas: none (audit is the gotcha mitigation itself)

---

#### 1.10 — Stop endpoint cancels orders + closes positions

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend)
- `claude-version/backend/src/msai/api/live.py` (modify `/stop` endpoint)
- `claude-version/backend/tests/integration/test_trading_node_stop.py` (new)

On `/api/v1/live/stop`:

1. Send SIGTERM to the subprocess pid
2. The subprocess's signal handler calls (in order):
   - `node.cancel_all_orders(instrument_id)` for every instrument in cache
   - `node.close_all_positions(instrument_id)` for every instrument in cache
   - `node.stop()`
   - `node.dispose()`
3. Update `LiveNodeProcess.status="stopped"` and `exit_code=0`
4. If the subprocess does not exit within 30 seconds, the parent escalates to SIGKILL and marks `status="failed"`, `error_message="hard kill on stop timeout"`

TDD:

1. Integration test: spawn subprocess running a stub strategy that submits a fake order, call stop, verify cancel + close called, verify exit_code=0
2. Integration test: spawn a subprocess that ignores SIGTERM, verify SIGKILL + status="failed"

Acceptance: tests pass.

Effort: M
Depends on: 1.6
Gotchas: #13 (stop doesn't close)

---

#### 1.11 — Phase 1 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_live_trading_phase1.py` (new)
- `claude-version/scripts/e2e_phase1.sh` (helper script)

Docker-based E2E:

1. Bring up the full Claude stack: `docker compose -f docker-compose.dev.yml up -d`
2. IB Gateway container in paper mode with `TWS_USERID=msai-paper-test`, `TWS_PASSWORD=...`
3. POST to `/api/v1/live/start` with the EMA strategy and `instruments=["AAPL"]`
4. Poll `/api/v1/live/status/{deployment_id}` until `status="running"` (timeout 60s)
5. Verify the `live_node_processes` table has a heartbeat that advances over 10 seconds
6. Wait for at least one bar to arrive and the EMA strategy to evaluate `on_bar`
7. Check `order_attempt_audits` table for any submission attempts (may be empty if EMAs haven't crossed yet — that's OK, just proves the audit hook is wired)
8. **Kill the FastAPI container** with `docker kill msai-claude-backend`
9. Wait 5 seconds
10. Restart FastAPI with `docker compose up -d backend`
11. Verify the trading subprocess is still alive (heartbeat still advancing)
12. Verify `GET /api/v1/live/status` returns the running deployment from the Postgres registry
13. POST `/api/v1/live/stop`
14. Verify `LiveNodeProcess.status="stopped"`, `exit_code=0`
15. Verify no positions are still open in the IB account

The E2E test is gated behind an env var (`MSAI_E2E_IB_ENABLED=1`) so CI doesn't try to talk to a real IB Gateway. Locally we run it before declaring Phase 1 done.

Acceptance: harness passes locally against a real IB Gateway paper container.

Effort: L
Depends on: 1.1–1.10
Gotchas: covered by all upstream tasks

---

## Phase 2 — Security Master + Catalog Migration + Parity

**Goal:** Backtest and live use the same canonical instruments. The fake `TestInstrumentProvider.equity(SIM)` is gone everywhere. Multi-asset support (stocks, futures, options, FX) actually works.

**Phase 2 acceptance:**

- A backtest of `AAPL.NASDAQ` uses the real IB contract details, loaded from the SecurityMaster cache
- A live deployment of `AAPL` resolves to the **exact same** `AAPL.NASDAQ` `Instrument` object
- The parity validation harness runs the EMA strategy in both backtest and a paper-live shadow over the same window and asserts intent-level parity (see 2.11 below)
- Streaming catalog builder handles a 1 GB Parquet directory without OOM
- Existing `*.SIM` backtests are migrated to canonical IDs by a one-shot script

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
    venue: str  # NASDAQ, NYSE, IDEALPRO, XCME, CBOE, etc.
    currency: str = "USD"
    # Future-specific
    expiry: date | None = None
    # Option-specific
    strike: Decimal | None = None
    right: Literal["C", "P"] | None = None
    underlying: str | None = None
    multiplier: Decimal | None = None
```

`InstrumentSpec.canonical_id() -> str` returns the Nautilus instrument ID string per IB_SIMPLIFIED conventions:

- Equity: `AAPL.NASDAQ`
- Future: `ESM5.XCME` (front-month notation handled in 2.3)
- Option: `AAPL250620C00250000.NASDAQ` (OCC-style)
- Forex: `EUR/USD.IDEALPRO`
- Index: `SPX.CBOE`

TDD:

1. Test canonical_id for each asset class
2. Test that bad combinations (e.g., option without strike) raise ValueError
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #4 (venue suffix discipline)

---

#### 2.2 — Postgres `instruments` cache table + model

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
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

TDD: same pattern as 1.1.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 2.3 — IB qualification adapter

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (new)
- `claude-version/backend/tests/unit/test_ib_qualifier.py` (new)

```python
class IBQualifier:
    """Wraps ib_async to qualify InstrumentSpec → ib_async.Contract.

    Throttles to ≤50 msg/sec to respect IB API limits (gotcha #11
    direction). For continuous futures uses CONTFUT secType. For options
    uses reqSecDefOptParamsAsync (NOT reqContractDetails which gets
    throttled on ambiguous queries).
    """

    async def qualify(self, spec: InstrumentSpec) -> Contract: ...
    async def qualify_many(self, specs: list[InstrumentSpec]) -> list[Contract]: ...
    async def front_month_future(self, root_symbol: str, exchange: str) -> Contract: ...
    async def option_chain(self, underlying: str, exchange: str) -> list[Contract]: ...
```

TDD:

1. Mock `ib_async.IB` and verify the right contract type is constructed for each asset class
2. Test throttling (use a fake clock to verify ≥20ms between calls)
3. Test that an unqualified contract (returns None) raises a clear error
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.1
Gotchas: #11 (don't load on critical path), #12 (option chains are huge)

---

#### 2.4 — Nautilus instrument parser

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/parser.py` (new)
- `claude-version/backend/tests/unit/test_security_master_parser.py` (new)

Wraps Nautilus's existing `parse_instrument` from `nautilus_trader.adapters.interactive_brokers.parsing.instruments` (file ref: `nautilus_trader/adapters/interactive_brokers/parsing/instruments.py:301+`). Returns concrete `Equity` / `FuturesContract` / `OptionContract` / `CurrencyPair` objects.

TDD:

1. Test that an `Equity` `IBContractDetails` parses to `nautilus_trader.model.instruments.Equity` with the right precision/multiplier
2. Same for each asset class
3. Implement (mostly delegation)

Acceptance: tests pass.

Effort: S
Depends on: 2.1, 2.3
Gotchas: none

---

#### 2.5 — `SecurityMaster` service (top-level API)

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/security_master/service.py` (new)
- `claude-version/backend/tests/unit/test_security_master.py` (new)

```python
class SecurityMaster:
    def __init__(self, qualifier: IBQualifier, parser: NautilusInstrumentParser, db: AsyncSession): ...

    async def resolve(self, spec_or_symbol: InstrumentSpec | str) -> Instrument:
        """Resolve an InstrumentSpec or shorthand symbol to a Nautilus Instrument.

        Order:
        1. Read from the instrument_cache table by canonical_id
        2. If not present, qualify via IBQualifier, parse via NautilusInstrumentParser, write to cache, return
        3. If present but older than refresh_threshold, refresh in the background and return cached for now
        """

    async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]: ...

    async def refresh(self, canonical_id: str) -> Instrument: ...

    @classmethod
    def shorthand_to_spec(cls, symbol: str) -> InstrumentSpec:
        """Best-effort shorthand: 'AAPL' → equity AAPL.NASDAQ. 'ES' → future ESM5.XCME.

        Used by API/CLI for ergonomics. Production code should always pass a
        full InstrumentSpec.
        """
```

TDD:

1. Test resolve hits cache when present
2. Test resolve falls through to qualifier when missing, then writes to cache
3. Test bulk_resolve makes batched calls (no N+1)
4. Test shorthand_to_spec for each asset class
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.2, 2.3, 2.4
Gotchas: #11 (don't dynamically load on critical path — preload via bulk_resolve)

---

#### 2.6 — Replace `instruments.py` and remove `*.SIM` rebinding

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (rewrite)
- `claude-version/backend/tests/unit/test_instruments.py` (rewrite)

The current `instruments.py:45` strips any suffix and rebinds to `SIM`. Remove this. The function now delegates to `SecurityMaster.resolve()`.

Backwards compatibility: a temporary `legacy_resolve_sim(symbol)` helper that builds a synthetic `Equity` with venue `SIM` is kept _only_ for the existing backtest test fixtures. Marked deprecated with a warning. Removed in 2.10.

TDD:

1. Test that `resolve_instrument("AAPL")` returns an `Equity` with `instrument_id = "AAPL.NASDAQ"`
2. Test that the returned instrument is structurally identical (precision, multiplier, lot_size) to what the security master returns
3. Implement

Acceptance: tests pass; all upstream callers (catalog_builder, backtest_runner) compile.

Effort: M
Depends on: 2.5
Gotchas: #4 (venue suffix discipline)

---

#### 2.7 — Streaming catalog builder

Files:

- `claude-version/backend/src/msai/services/nautilus/catalog_builder.py` (modify)
- `claude-version/backend/tests/unit/test_catalog_builder_streaming.py` (new)

Replace the current full-partition pandas load (`catalog_builder.py:123` per the architecture review) with a chunked iterator using `pyarrow.parquet.ParquetFile.iter_batches(batch_size=100_000)`. Each batch is wrangled into Nautilus `Bar` objects via `BarDataWrangler` and appended to the catalog.

The function signature stays the same so callers don't change.

TDD:

1. Generate a synthetic Parquet file with 1M rows (~50 MB)
2. Run the new builder with `batch_size=100_000`
3. Assert peak memory usage is under 200 MB (use `tracemalloc`)
4. Assert the resulting catalog has 1M bars
5. Implement

Acceptance: streaming test passes; existing catalog_builder tests still pass.

Effort: M
Depends on: nothing
Gotchas: relates to gotcha-adjacent #15 (cache eviction) — large catalogs need to stream, not batch-load

---

#### 2.8 — Migration script: rebuild existing catalogs under canonical IDs

Files:

- `claude-version/backend/scripts/migrate_catalog_to_canonical.py` (new)
- `claude-version/backend/tests/integration/test_migrate_catalog.py` (new)

One-shot script that:

1. Walks `data/parquet/<asset_class>/<symbol>/` directories
2. For each `(asset_class, symbol)`, calls `SecurityMaster.shorthand_to_spec(symbol).canonical_id()` to get the new ID
3. Builds a Nautilus catalog under `data/nautilus/<canonical_id>/` using the streaming builder
4. Idempotent: skips entries already migrated (check by `data/nautilus/<canonical_id>/instruments.parquet` existence)
5. Reports a summary: migrated, skipped, failed

TDD:

1. Integration test with a synthetic `data/parquet/stocks/AAPL/2025/01.parquet`
2. Run migration
3. Assert `data/nautilus/AAPL.NASDAQ/` exists with bar data
4. Assert running the migration twice is a no-op

Acceptance: tests pass.

Effort: M
Depends on: 2.5, 2.7
Gotchas: none

---

#### 2.9 — Update backtest API + worker to use canonical IDs

Files:

- `claude-version/backend/src/msai/api/backtests.py` (modify)
- `claude-version/backend/src/msai/workers/backtest_job.py` (modify)
- `claude-version/backend/tests/unit/test_backtests_api.py` (modify)

`POST /api/v1/backtests/run` accepts either `instruments=["AAPL"]` (shorthand) or `instruments=["AAPL.NASDAQ"]` (canonical). The API resolves shorthand via `SecurityMaster.shorthand_to_spec` and persists the canonical IDs in the `backtests.instruments` column. The worker reads only canonical IDs from the row.

The backtest_runner's `BacktestVenueConfig.name` is set per-instrument-venue (multiple venue configs if instruments span venues), not hardcoded `SIM`.

TDD:

1. POST with shorthand → row has canonical ID
2. POST with canonical → row has canonical ID unchanged
3. Worker builds a `BacktestVenueConfig` per unique venue in the instruments list
4. Implement

Acceptance: tests pass; an existing backtest run end-to-end produces the same trades it did before, just under canonical IDs.

Effort: M
Depends on: 2.5, 2.6, 2.8
Gotchas: #4 (venue suffix), #2 (account_report needs `venue=`)

---

#### 2.10 — Remove the `legacy_resolve_sim` shim

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (delete shim)
- All test fixtures that depended on `*.SIM` are migrated to canonical IDs

TDD: full test suite passes without the shim.

Acceptance: `git grep -l "legacy_resolve_sim"` returns nothing; tests pass.

Effort: S
Depends on: 2.6, 2.9
Gotchas: none

---

#### 2.11 — Parity validation harness

Files:

- `claude-version/backend/scripts/parity_check.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/normalizer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/comparator.py` (new)
- `claude-version/backend/tests/integration/test_parity_check.py` (new)

The parity harness takes a strategy file, config, instrument, and time window. It runs:

1. **Backtest leg**: existing backtest runner, produces a list of `OrderAttempt` records normalized to `(timestamp, instrument_id, side, intent_qty)` (intent_qty = signed change in target position)
2. **Paper-live leg**: spins up a TradingNode against IB Gateway paper, replays the same time window via IB historical bars, captures the same `OrderAttempt` records

The comparator (per Codex Q7 answer):

- **Required exact match**: same instrument_id, same side, same signed intent_qty for each decision
- **Required exact match**: same decision sequence (by timestamp ordering)
- **Required exact match**: end-of-window position trajectory
- **Required match within tolerance**: aggregate filled qty per intent (exact); VWAP within `max(1 tick, configured slippage budget)`
- **NOT compared**: exact fill timestamps (live has latency), exact fill counts (paper-live can partial-fill), commissions (compared separately after a fee model is configured)

The harness reports diffs as a structured table.

TDD:

1. Unit test the normalizer: feed mock OrderAttempt rows, verify the normalized output
2. Unit test the comparator: feed two normalized lists with known diffs, verify the right exceptions/warnings
3. Integration test on a 1-day AAPL window with the EMA strategy

Acceptance: parity check passes for the EMA strategy on a 1-day window.

Effort: L
Depends on: 2.5, 2.6, 2.9, and Phase 1 live path (1.6)
Gotchas: #14 (backtest fills are optimistic) — handled via the tolerance model

---

#### 2.12 — Multi-asset support

Three sub-tasks (parallelizable):

**2.12a — Futures**

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py` (extend)
- `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (extend)
- `claude-version/backend/tests/unit/test_security_master_futures.py` (new)

Add `FuturesSpec` (subtype of `InstrumentSpec` with `asset_class="future"`). Front-month resolution via CONTFUT. Tests use a mocked IB.

**2.12b — Options**

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py` (extend)
- `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (extend)
- `claude-version/backend/tests/unit/test_security_master_options.py` (new)

Add `OptionSpec` with strike/right/expiry. Use `reqSecDefOptParamsAsync` (gotcha-related: don't blow out the chain — require explicit strike).

**2.12c — FX**

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py` (extend)
- `claude-version/backend/tests/unit/test_security_master_fx.py` (new)

Add `ForexSpec` with `base_currency` / `quote_currency`. IDEALPRO venue.

Each sub-task: TDD pattern, acceptance = unit tests pass.

Effort: M each
Depends on: 2.5
Gotchas: #12 (option chain explosion — explicit strike required)

---

#### 2.13 — Phase 2 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_security_master_phase2.py` (new)

E2E:

1. Start the stack including IB Gateway paper
2. Hit the SecurityMaster API to resolve `AAPL`, `ESM5.XCME`, `EUR/USD.IDEALPRO`
3. Run a backtest with `AAPL.NASDAQ` for a 1-day window
4. Run the parity harness for the same window against paper-live
5. Verify the parity check passes
6. Verify the streaming catalog builder handled the data without OOM (memory under 500 MB peak)

Effort: L
Depends on: 2.1–2.12
Gotchas: covered by upstream tasks

---

## Phase 3 — State Spine + PnL Projection + Risk in Order Path

**Goal:** The API can see what live strategies are doing in real-time. Risk runs on real position state, not hardcoded zeros. The kill switch actually closes positions.

**Phase 3 acceptance:**

- A live deployment publishes events through Nautilus's MessageBus into Redis Streams
- A FastAPI projection layer consumes those streams and broadcasts to the existing WebSocket
- The `/live` page shows real-time positions, fills, and PnL for the running deployment
- The custom RiskEngine blocks an order that would breach a per-strategy max position, using REAL position data from the cache
- `POST /api/v1/live/kill-all` sets a sticky halt flag that prevents any new orders, AND closes all open positions across all running deployments

This phase has three logical sub-phases (3A, 3B, 3C). They're labeled here so they can be tracked but they live under one numbered phase per Codex's recommendation (don't fragment too much).

### Phase 3A — State spine (cache + msgbus + projection)

#### 3.1 — Configure `CacheConfig.database = redis` for live (NOT backtest)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/src/msai/services/nautilus/backtest_runner.py` (verify NO database config)
- `claude-version/backend/tests/unit/test_live_node_config_cache.py` (new)

Add to live config:

```python
cache=CacheConfig(
    database=DatabaseConfig(
        type="redis",
        host=settings.redis_host,
        port=settings.redis_port,
    ),
    encoding="msgpack",
    buffer_interval_ms=0,  # gotcha #7: write-through
    persist_account_events=True,
)
```

Backtest config has NO `cache.database` set (gotcha #8 inverse: don't pollute backtest with live state).

TDD:

1. Test that live config has `cache.database.type == "redis"` and `buffer_interval_ms == 0`
2. Test that backtest config has `cache.database is None`
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.5
Gotchas: #7 (buffered cache loss), #8 (msgbus pollutes prod)

---

#### 3.2 — Configure `MessageBusConfig.database = redis` for live (NOT backtest)

Files: same as 3.1 plus tests

```python
message_bus=MessageBusConfig(
    database=DatabaseConfig(type="redis", host=..., port=...),
    encoding="msgpack",  # gotcha #17: avoid JSON for custom types
    stream_per_topic=True,
    buffer_interval_ms=0,
)
```

TDD: parallel to 3.1.

Acceptance: tests pass.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, #17

---

#### 3.3 — Cache rehydration smoke test

Files:

- `claude-version/backend/tests/integration/test_cache_rehydration.py` (new)

Integration test:

1. Start a TradingNode subprocess
2. Submit a synthetic order, get a fill
3. Verify the position is in the Redis-backed cache
4. SIGTERM the subprocess (gracefully)
5. Restart the subprocess with the same `deployment_id`
6. Verify the position is still present in the cache after restart

Acceptance: test passes.

Effort: M
Depends on: 3.1
Gotchas: #19 (reconciliation finds unexpected fills)

---

#### 3.4 — Internal event schema (projection layer types)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/events.py` (new)
- `claude-version/backend/tests/unit/test_projection_events.py` (new)

Pydantic models for the **internal MSAI event schema**, NOT Nautilus's bus schema (Codex projection-layer warning):

- `PositionSnapshot`
- `FillEvent`
- `OrderStatusChange`
- `AccountStateUpdate`
- `RiskHaltEvent`
- `DeploymentStatusEvent`

Each carries `deployment_id`, `timestamp`, and the relevant fields. They're stable contracts the WebSocket and frontend depend on. If Nautilus's bus schema changes, only the projection layer needs updating.

TDD:

1. Test serialization/deserialization for each model
2. Implement

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #17 (msgpack over JSON for custom types — but our internal schema is all primitives)

---

#### 3.5 — Redis Streams consumer in FastAPI

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/consumer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/translator.py` (new)
- `claude-version/backend/tests/integration/test_projection_consumer.py` (new)

Background asyncio task in the FastAPI process that:

1. Subscribes to `trader-*-stream-events.order.filled`, `trader-*-stream-events.position.*`, `trader-*-stream-events.account.state` etc.
2. Decodes the Nautilus event (msgpack) via Nautilus's deserialization helpers
3. Translates to the internal event schema (3.4)
4. Pushes the internal event onto a per-deployment in-memory queue that the WebSocket broadcaster reads from
5. Also writes a `PositionSnapshot` to Redis so the API can read current positions without subscribing

The translator is a pure function `translate(nautilus_event) -> InternalEvent`.

TDD:

1. Unit test the translator with each Nautilus event type
2. Integration test: publish a synthetic `OrderFilled` event to Redis Streams, verify the consumer receives it and the WebSocket queue gets the right `FillEvent`

Acceptance: tests pass.

Effort: L
Depends on: 3.2, 3.4
Gotchas: #17 (encoding)

---

#### 3.6 — Position snapshot cache (Redis)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/position_cache.py` (new)
- `claude-version/backend/tests/integration/test_position_cache.py` (new)

Service:

```python
class PositionSnapshotCache:
    async def get(self, deployment_id: UUID, instrument_id: str) -> PositionSnapshot | None: ...
    async def get_all(self, deployment_id: UUID) -> list[PositionSnapshot]: ...
    async def set(self, snapshot: PositionSnapshot) -> None: ...
    async def delete(self, deployment_id: UUID, instrument_id: str) -> None: ...
```

Backed by Redis hash. Updated by 3.5's consumer on every fill / position_changed event.

TDD:

1. Set + get round-trip
2. List by deployment
3. TTL eviction (positions older than 1h since last update)
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.4
Gotchas: none

---

#### 3.7 — WebSocket broadcaster wired to projection

Files:

- `claude-version/backend/src/msai/api/websocket.py` (full rewrite)
- `claude-version/backend/tests/integration/test_websocket_live_events.py` (new)

The existing `/api/v1/live/stream` WebSocket is currently heartbeat-only. Replace with:

1. Auth via first message (existing pattern)
2. Subscribe to the deployment's per-deployment in-memory queue from 3.5
3. Send each internal event as JSON over the socket
4. Heartbeat every 30s if no events

TDD:

1. Integration test: connect to WebSocket, push an event into the queue, receive it on the client
2. Test multi-deployment fan-out
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.5, 3.6
Gotchas: none

---

### Phase 3B — Risk in order path

#### 3.8 — Custom Nautilus `RiskEngine` subclass

Files:

- `claude-version/backend/src/msai/services/nautilus/risk/custom_risk_engine.py` (new)
- `claude-version/backend/tests/unit/test_custom_risk_engine.py` (new)

A Nautilus `RiskEngine` subclass (or a Strategy mixin if subclassing is too invasive — investigate during the task) that adds these checks BEFORE the broker call:

- Per-strategy max position (read from `RiskLimits` on the deployment row)
- Daily loss limit (read PnL from `PositionSnapshotCache` aggregated)
- Max notional exposure across all running strategies
- Sticky halt flag (Redis key `msai:risk:halt`)
- Kill-switch (overrides everything)

Real inputs, not hardcoded zeros (architecture review's biggest Codex callout for the risk engine).

TDD:

1. Test each check in isolation with a mock cache
2. Test that submitted orders pass through when within limits
3. Test that orders are denied when over limits, with clear `OrderDenied` events
4. Implement

Acceptance: tests pass.

Effort: L
Depends on: 3.6, existing `services/risk_engine.py` (Claude's well-designed RiskLimits)
Gotchas: none

---

#### 3.9 — Wire custom RiskEngine into TradingNodeConfig

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/tests/unit/test_live_node_config_risk.py` (new)

Add:

```python
risk_engine=LiveRiskEngineConfig(
    bypass=False,
    # custom subclass installed as a runtime override — see 3.8
)
```

TDD:

1. Test that the live config installs the custom risk engine
2. Test that backtest config does NOT install it (backtest uses Nautilus default)
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 3.8
Gotchas: none

---

#### 3.10 — Sticky kill switch

Files:

- `claude-version/backend/src/msai/services/risk_engine.py` (extend existing)
- `claude-version/backend/src/msai/api/live.py` (modify `/kill-all` and add `/resume`)
- `claude-version/backend/tests/integration/test_kill_switch.py` (new)

`POST /api/v1/live/kill-all`:

1. Sets Redis key `msai:risk:halt = true` with a long TTL
2. For each running deployment in `live_node_processes`, sends a "kill all positions" signal via Redis pub/sub (or via the deployment's command queue)
3. Each subprocess's custom risk engine sees the flag, calls `cancel_all_orders` + `close_all_positions` for every instrument
4. Returns count of deployments halted

`POST /api/v1/live/resume`: clears the halt flag (manual, requires explicit action).

TDD:

1. Integration test: start two subprocesses, set kill-all, verify both close positions, verify the halt flag prevents new starts
2. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.8
Gotchas: #13 (stop doesn't close positions — we explicitly do it here)

---

### Phase 3C — API/UI projection

#### 3.11 — Frontend live page wired to real WebSocket events

Files:

- `claude-version/frontend/src/app/live-trading/page.tsx` (modify)
- `claude-version/frontend/src/components/live/positions-table.tsx` (modify)
- `claude-version/frontend/src/components/live/strategy-status.tsx` (modify)
- `claude-version/frontend/src/lib/use-live-stream.ts` (new hook)

Replace mock data with a `useLiveStream(deploymentId)` hook that:

1. Opens the WebSocket
2. Subscribes to internal events
3. Maintains a normalized state for positions, fills, account
4. Returns reactive state to the page components

TDD:

1. Vitest unit test for the hook with a mock WebSocket
2. Visual test against a running deployment (manual)

Acceptance: page shows real positions when a strategy is running.

Effort: L
Depends on: 3.7
Gotchas: none

---

#### 3.12 — Phase 3 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_live_streaming_phase3.py` (new)

E2E:

1. Start the stack with paper IB Gateway
2. Deploy an EMA strategy via `/api/v1/live/start`
3. Connect to `/api/v1/live/stream` WebSocket
4. Trigger an order (e.g., manually push a synthetic bar that crosses the EMAs)
5. Verify the WebSocket receives `FillEvent` within 5 seconds
6. Verify `GET /api/v1/live/positions` returns the new position
7. POST `/api/v1/live/kill-all`
8. Verify both positions closed and the halt flag is set
9. POST `/api/v1/live/start` again — should fail with 503 due to halt flag
10. POST `/api/v1/live/resume`, then start succeeds

Effort: L
Depends on: 3.1–3.11
Gotchas: covered

---

## Phase 4 — Recovery + Reconnect + Market Hours + Metrics

**Goal:** Production-grade resilience. The platform survives crashes, broker disconnects, and market closures gracefully.

**Phase 4 acceptance (revised per Codex):**

- `LiveExecEngineConfig(reconciliation=True)` is verified complete before the trader starts submitting orders
- Killing the FastAPI container does NOT interrupt trading; the trading subprocess survives, and on API restart the deployment is rediscovered
- Killing the trading subprocess is detected by the API (heartbeat stops) and the deployment is marked failed with an alert
- IB Gateway disconnect for >2 minutes halts the strategy; on reconnect the strategy resumes only after reconciliation succeeds
- Equity strategies auto-pause outside RTH
- The platform exposes Prometheus-style metrics at `/metrics`
- Strategy state persistence (`on_save`/`on_load`) works with a validated round-trip
- "Strategy resumes" is **not** unconditionally claimed: strategies either resume from validated state OR remain paused until manually warmed (Codex's correction)

### Phase 4 tasks

#### 4.1 — Verified reconciliation gating

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (modify subprocess)
- `claude-version/backend/tests/integration/test_reconciliation_gate.py` (new)

The subprocess transitions to `status="ready"` only after:

1. `node.build()` returned
2. The local cache has the strategy's required instruments
3. The local cache has the account state
4. **Reconciliation completed** — verified by checking that `LiveExecEngine` has emitted its `_reconciliation_completed` log line OR by waiting for an explicit reconciliation report in the cache

Until then, status stays "starting".

TDD:

1. Integration test that simulates a slow reconciliation and verifies the readiness wait works
2. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.7, 1.8
Gotchas: #10 (reconciliation timeout)

---

#### 4.2 — IB Gateway disconnect/reconnect handler

Files:

- `claude-version/backend/src/msai/services/nautilus/disconnect_handler.py` (new)
- `claude-version/backend/tests/integration/test_disconnect_handler.py` (new)

Background task in the trading subprocess that:

1. Monitors the IB connection state via Nautilus's connection events
2. On disconnect, starts a timer
3. If reconnect happens within `disconnect_grace_seconds` (default 120s): no action
4. If grace expires: set local halt flag, cancel all orders, close all positions
5. If reconnect happens AFTER halt: do nothing automatic — require manual `/resume` (consistent with Phase 4 acceptance: "remain paused until warm")

TDD:

1. Mock IB connection events, simulate disconnect+quick-reconnect, verify no halt
2. Simulate disconnect+timeout, verify halt
3. Simulate halt + reconnect, verify no auto-resume
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.10 (sticky halt mechanism)
Gotchas: relates to gotcha #10 (reconciliation)

---

#### 4.3 — Market hours awareness

Files:

- `claude-version/backend/src/msai/services/nautilus/market_hours.py` (new)
- `claude-version/backend/tests/unit/test_market_hours.py` (new)

A helper that consults the cached instrument's `trading_hours` field (loaded from IB during qualification in Phase 2) and returns whether the instrument is currently in RTH. The custom RiskEngine consults this helper before allowing an equity order.

Configurable per strategy: `allow_eth: bool = False`. If False (default), orders are denied outside RTH for equities.

TDD:

1. Test in_rth() for AAPL at 10am ET (true), at 3am ET (false)
2. Test for a future at 10am ET (true — futures trade ETH)
3. Test allow_eth=True bypasses the check
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.5 (security master loads trading_hours)
Gotchas: none

---

#### 4.4 — Crash recovery: rediscover surviving subprocesses on API startup

Files:

- `claude-version/backend/src/msai/main.py` (lifespan)
- `claude-version/backend/src/msai/services/nautilus/recovery.py` (new)
- `claude-version/backend/tests/integration/test_recovery_on_startup.py` (new)

In the FastAPI lifespan:

1. Query `live_node_processes` for rows where `status in ("running", "ready")`
2. For each, check if the pid is alive (`os.kill(pid, 0)` or check via container hostname)
3. If alive: refresh the heartbeat probe, mark as discovered
4. If dead: mark `status="failed"`, `error_message="orphaned after API restart"`, alert via the existing alerting service

TDD:

1. Integration test: insert a row with a known-dead pid, run lifespan, verify status flipped to "failed"
2. Integration test: insert a row with the current process pid (cheating), verify it's left alone
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.1, 1.6
Gotchas: none

---

#### 4.5 — Strategy state persistence with validated round-trip

Files:

- `claude-version/strategies/example/ema_cross.py` (modify)
- `claude-version/backend/tests/unit/test_ema_cross_state_persistence.py` (new)

Implement `on_save` and `on_load` on `EMACrossStrategy`:

```python
def on_save(self) -> dict[str, bytes]:
    return {
        "fast_ema_value": self.fast_ema.value.to_bytes(...),
        "slow_ema_value": self.slow_ema.value.to_bytes(...),
        # Custom state, NO references to live IB connection objects
    }

def on_load(self, state: dict[str, bytes]) -> None:
    if not state:
        return
    # Validate keys, types, ranges. Reject silently-corrupt state.
```

TDD (gotcha #16):

1. Set EMA values, save, load on a fresh instance, verify values restored
2. Save with corrupt bytes, load, verify graceful rejection (start from zero state, log warning)
3. Save with empty dict, load, verify no-op
4. Implement

Acceptance: tests pass; round-trip explicitly verified.

Effort: S
Depends on: nothing
Gotchas: #16 (on_save round-trip)

---

#### 4.6 — Prometheus metrics

Files:

- `claude-version/backend/src/msai/services/observability/metrics.py` (new)
- `claude-version/backend/src/msai/main.py` (mount `/metrics`)
- `claude-version/backend/tests/integration/test_metrics_endpoint.py` (new)

Use `prometheus_client`. Register:

- Counters: `msai_orders_submitted_total`, `msai_orders_filled_total`, `msai_orders_rejected_total`, `msai_deployments_started_total`, `msai_deployments_failed_total`
- Gauges: `msai_active_deployments`, `msai_position_count{deployment_id}`, `msai_daily_pnl_usd{deployment_id}`
- Histograms: `msai_order_submit_to_fill_ms`, `msai_reconciliation_duration_seconds`

The trading subprocess increments counters via a Redis-backed counter (the projection consumer also reads them and exposes them in `/metrics`).

TDD:

1. Test that `/metrics` returns Prometheus format
2. Test that metrics are non-zero after a synthetic event
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.5
Gotchas: none

---

#### 4.7 — Phase 4 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_recovery_phase4.py` (new)

Three scenarios, each is its own test:

**Scenario A: Kill FastAPI mid-trade**

1. Deploy strategy
2. Wait for status="running"
3. `docker kill msai-claude-backend`
4. Sleep 5s
5. `docker compose up -d backend`
6. Verify trading subprocess is still running (heartbeat)
7. Verify `GET /api/v1/live/status` discovers it via the registry

**Scenario B: Kill TradingNode subprocess**

1. Deploy strategy
2. SIGKILL the trading subprocess pid directly
3. Wait 30s (heartbeat stale)
4. Verify the API marks the deployment as failed with an alert in the log

**Scenario C: Disconnect IB Gateway**

1. Deploy strategy
2. `docker pause msai-claude-ib-gateway`
3. Wait 130 seconds (past disconnect_grace_seconds)
4. Verify the strategy halted (orders cancelled, positions closed)
5. `docker unpause msai-claude-ib-gateway`
6. Verify the strategy stays halted (manual resume required)
7. POST `/api/v1/live/resume`
8. Verify the strategy is re-startable

Effort: L
Depends on: 4.1–4.6
Gotchas: #5, #10, #19

---

## Phase 5 — Paper Soak Release Gate (NOT implementation)

**This is a release gate, not an implementation phase.** It exists in the plan so "Phase 4 done" cannot be misread as "ready to trade real money".

### 5.1 — Paper soak procedure

Document at `claude-version/docs/paper-soak-procedure.md`:

1. **Duration:** 30 calendar days minimum
2. **Account:** IB paper account, separate from any real account
3. **Strategies:** start with one strategy, EMA Cross on AAPL+MSFT only; add one new instrument per week if no incidents
4. **Monitoring:**
   - Daily PnL report emailed to the operator
   - Prometheus alerts on: API down, trading subprocess down, IB disconnect >2 min, reconciliation failure, sticky halt set
   - Manual review of the order audit log every Friday
5. **Incidents:** any incident (unexpected halt, position outside expected range, API error >1% rate) restarts the 30-day clock
6. **Exit criteria:** 30 consecutive calendar days with zero P0/P1 incidents AND a manual sign-off from the operator AND a final review of the audit log

### 5.2 — Release sign-off

Document at `claude-version/docs/release-signoff-checklist.md`:

- [ ] 30-day paper soak completed without incident
- [ ] All Phase 1–4 E2E tests passing on the latest commit
- [ ] All unit + integration tests passing
- [ ] Architecture review re-run by both Claude and Codex against the latest code, no P0/P1/P2 findings
- [ ] Disaster recovery runbook tested (`docs/runbooks/disaster-recovery.md`)
- [ ] Operator confirms emergency contact is configured for IB account
- [ ] Initial real-money allocation: maximum $1,000, hard cap enforced in the risk engine config

**No code commits go in this phase. It's a checklist that gates real-money deployment.**

---

## Cross-Cutting Concerns

### Test Strategy

Every task uses TDD (red-green-refactor). The order is:

1. Write a failing test that captures the acceptance criterion
2. Run the test, see it fail
3. Write the minimum implementation to make it pass
4. Refactor for clarity, simplicity, and reuse
5. Run the test again, verify it still passes

Test pyramid:

- **Unit tests** for every function and class (target: every public method)
- **Integration tests** for anything that touches Postgres, Redis, or spawns a subprocess
- **E2E tests** at the end of each phase, gated by `MSAI_E2E_IB_ENABLED=1` env var so CI doesn't try to talk to real IB

### Logging and Observability

Structured logging is on from Phase 1 day 1. Every event boundary logs with:

- `deployment_id` (when relevant)
- `strategy_id`
- `event_type`
- structured fields, never f-string concatenation

### Configuration

All new settings go in `claude-version/backend/src/msai/core/config.py`:

- `ib_gateway_host`, `ib_gateway_port_paper`, `ib_gateway_port_live`
- `ib_account_id`, `ib_client_id_base`
- `redis_host`, `redis_port`, `redis_db`
- `disconnect_grace_seconds`
- `risk_halt_redis_key`

### Database Migrations

Each task that adds a table also adds an Alembic migration. Migration tests live in `tests/integration/`.

### Backwards Compatibility

The existing backtest pipeline must keep working at every phase boundary. Phase 2 includes a migration script for existing `*.SIM` catalogs. After Phase 2 the `SIM` venue is gone.

### Parallelization Notes for Multiple Sessions / Codex

Tasks within a phase that have no `Depends on:` overlap can run in parallel. For each phase:

- **Phase 1 parallelizable groups:**
  - Group A: 1.1, 1.2, 1.3, 1.4 (foundation, no inter-deps)
  - Group B: 1.5 (depends on 1.4)
  - Group C: 1.6 (depends on 1.1, 1.5)
  - Group D: 1.7, 1.8, 1.9, 1.10 (depend on 1.6, parallelizable among themselves)
  - Group E: 1.11 (depends on all)
- **Phase 2 parallelizable groups:**
  - Group A: 2.1, 2.2, 2.7 (foundation)
  - Group B: 2.3, 2.4 (depend on 2.1)
  - Group C: 2.5 (depends on 2.2, 2.3, 2.4)
  - Group D: 2.6, 2.8, 2.9 (depend on 2.5; some on 2.7)
  - Group E: 2.10, 2.11 (depend on 2.6, 2.9)
  - Group F: 2.12a, 2.12b, 2.12c (depend on 2.5, parallel)
  - Group G: 2.13 (depends on all)
- Similar for Phase 3 and 4.

---

## Open Questions

1. **IB account_id provisioning**: We need a paper IB account with credentials in a secrets file. Who owns this and where do the credentials live? (Probably Azure Key Vault — see existing `secrets.py` provider abstraction.)

2. **Redis cluster vs single instance**: Phase 3 introduces heavy Redis usage (cache backend, message bus, position cache). Single Redis container is fine for Phase 3 dev. Production may want a redundant setup. Defer to Phase 6+.

3. **Postgres connection pooling**: The trading subprocess uses async SQLAlchemy. Each subprocess opens its own pool. With multiple deployments, pool count grows. May need pgbouncer in production.

4. **Strategy state schema versioning**: When a strategy changes its `on_save` payload, old saved state is invalid. Add a `version` field to the saved dict and reject mismatches. Defer until we have a second strategy.

5. **Multi-currency PnL**: Phase 3's `daily_pnl` gauge assumes single currency. Multi-currency requires FX conversion. Defer until we have a non-USD-denominated strategy.

---

## Risks

1. **Nautilus version drift**: We're locking against Nautilus 1.x. A minor version bump could break private APIs we touch (e.g., the subprocess startup pattern). Mitigation: pin exact version in `pyproject.toml`, run upgrade tests in a separate branch.

2. **IB Gateway flakiness**: IB Gateway is notorious for spurious disconnects. Phase 4's disconnect handler must be tested extensively against real-world disconnect patterns, not just synthetic ones. Mitigation: paper soak in Phase 5.

3. **Subprocess orchestration complexity**: Spawning subprocesses across container boundaries (FastAPI in one container, trading subprocess inheriting from arq worker in another) is hairy. Mitigation: keep the contract narrow (DB rows + Redis), avoid IPC primitives.

4. **Catalog migration data loss**: 2.8's migration script rewrites Parquet files. If it has a bug, existing backtests are gone. Mitigation: idempotent + dry-run mode + test against a copy first.

5. **Phase boundaries shifting**: Some Phase 3 tasks may need to slip into Phase 4 (especially the projection layer). Mitigation: re-evaluate phase scope at the end of each phase before starting the next.

---

## How To Use This Plan

- **Future sessions of Claude Code**: pick up the next pending task in the lowest pending phase. Read the architecture review, the Nautilus reference, and the relevant gotchas before implementing. Do not skip TDD.

- **Codex CLI working in parallel** (if/when the user asks for parallel codex-version work): this plan is Claude-only. Codex can use this as a template but should write its own equivalent plan for the codex-version codebase.

- **The user**: each task is sized to fit in a single working session. Tell Claude which task to pick up next, or just say "next task in Phase X" and Claude reads the plan to find an unblocked one.

- **Phase boundaries are checkpoints**: don't start Phase N+1 until Phase N's E2E harness passes. Each E2E is the gate.

---

**Plan version:** 1.0
**Last updated:** 2026-04-06
**Approved by:** [pending Codex review]
