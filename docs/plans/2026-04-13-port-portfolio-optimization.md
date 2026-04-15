# Port Portfolio Optimization Orchestration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Claude's placeholder `portfolio_job.py` (marks runs "completed" without executing) with a real orchestration layer adapted from Codex — actually runs per-allocation backtests in parallel, combines weighted returns with leverage, computes portfolio metrics, generates QuantStats report, and persists everything to the DB.

**Architecture:** Keep Claude's PostgreSQL DB-backed model (superior to Codex's JSON files). Adapt Codex's orchestration logic (`run_portfolio_backtest`, `_execute_candidate_backtests`, `_heuristic_weight`, `_effective_leverage`, `_load_benchmark_returns`) onto Claude's existing infrastructure (GraduationService, BacktestRunner, MarketDataQuery, compute_slots, catalog_builder, analytics_math — all already in place).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, arq, Alembic, pandas, NautilusTrader.

---

## Approach Comparison

### Chosen Default

Scoped port: adapt Codex's orchestration methods onto Claude's DB models. Use existing `GraduationService.get_candidate()` to resolve `strategy → file_path + config`, call `BacktestRunner.run()` per allocation, combine with `analytics_math` helpers.

### Best Credible Alternative

Full Codex port: replace Claude's DB-backed model with Codex's JSON file persistence. Rejected — regresses from a superior architecture to a worse one; defeats the "Claude is the foundation" principle.

### Scoring (fixed axes)

| Axis                  | Default | Alternative |
| --------------------- | ------- | ----------- |
| Complexity            | M       | H           |
| Blast Radius          | M       | H           |
| Reversibility         | H       | L           |
| Time to Validate      | M       | L           |
| User/Correctness Risk | M       | H           |

### Contrarian Gate

**SKIP** — Alternative regresses architecture. Chosen approach is straightforward adaptation of already-working Codex logic onto Claude's existing better-designed DB layer. No spike needed.

### Cheapest Falsifying Test

Build a 2-candidate portfolio, run orchestration with fake BacktestRunner returning canned returns, assert combined metrics match hand-computed expectation. < 15 min to implement.

---

## Scope

### In scope

1. DB schema additions (alembic migration)
2. Model updates (Portfolio, PortfolioRun)
3. Schema/API response additions (non-breaking — new optional fields)
4. `PortfolioService.run_portfolio_backtest()` orchestration
5. Worker replacement (`portfolio_job.py`)
6. Unit tests for pure helpers
7. Integration test for end-to-end orchestration

### Deferred (separate P1 ports)

- **Continuous futures** — depends on strategy registry port (Phase 2)
- **Multi-provider instrument canonicalization** — depends on strategy registry port
- **Alerting on failure** — depends on alerting API port
- **Compute slot integration** — use existing `compute_slots` service as-is (no extension)

### Out of scope

- Frontend changes — backend-only port; UI consumes existing response shape + new optional fields

---

## Files To Touch

### Create

- `claude-version/backend/alembic/versions/NNNN_portfolio_orchestration_columns.py` — new migration
- `claude-version/backend/tests/unit/services/test_portfolio_orchestration.py` — pure-function tests
- `claude-version/backend/tests/integration/test_portfolio_job_orchestration.py` — end-to-end test

### Modify

- `claude-version/backend/src/msai/models/portfolio.py` — add `downside_target`
- `claude-version/backend/src/msai/models/portfolio_run.py` — add `max_parallelism`, `series`, `allocations`, `heartbeat_at`, `error_message`, `updated_at`
- `claude-version/backend/src/msai/schemas/portfolio.py` — add `downside_target` to `PortfolioCreate`, extend `PortfolioRunResponse`
- `claude-version/backend/src/msai/services/portfolio_service.py` — add orchestration methods
- `claude-version/backend/src/msai/workers/portfolio_job.py` — replace placeholder

---

## Task Breakdown (TDD)

### Task 1: DB migration for new columns

**Files:**

- Create: `backend/alembic/versions/NNNN_portfolio_orchestration_columns.py`

**Step 1:** Generate migration skeleton via `uv run alembic revision --autogenerate -m "portfolio_orchestration_columns"` (or manually if autogenerate flakes).

**Step 2:** Edit migration to add:

- `portfolios.downside_target` (`Numeric(8,4)`, nullable)
- `portfolio_runs.max_parallelism` (`Integer`, nullable)
- `portfolio_runs.series` (`JSONB`, nullable)
- `portfolio_runs.allocations` (`JSONB`, nullable)
- `portfolio_runs.heartbeat_at` (`DateTime(timezone=True)`, nullable)
- `portfolio_runs.error_message` (`Text`, nullable)
- `portfolio_runs.updated_at` (`DateTime(timezone=True)`, server_default=`func.now()`, onupdate=`func.now()`)

**Step 3:** Run `uv run alembic upgrade head` against dev Postgres — expect clean upgrade + downgrade round-trip.

**Step 4:** Commit.

---

### Task 2: Model updates

**Files:**

- Modify: `backend/src/msai/models/portfolio.py` — add `downside_target: Mapped[float | None]`
- Modify: `backend/src/msai/models/portfolio_run.py` — add new columns matching migration

**Step 1 (Red):** Write `tests/unit/models/test_portfolio_models.py::test_portfolio_has_downside_target` asserting the new column is on the model.

**Step 2 (Green):** Add column mappers.

**Step 3:** Run tests, commit.

---

### Task 3: Schema updates

**Files:**

- Modify: `backend/src/msai/schemas/portfolio.py`

**Step 1 (Red):** Write tests:

- `PortfolioCreate` accepts `downside_target: float | None`
- `PortfolioAllocationInput.weight: float | None = None` (relaxed from required — enables heuristic weighting)
- `PortfolioRunResponse` exposes `series`, `allocations`, `error_message`, `heartbeat_at`, `max_parallelism`, `updated_at`

**Step 2 (Green):** Add fields. For `PortfolioAllocationInput.weight`: change to `float | None = Field(default=None, ge=0.0, le=1.0)`. `portfolio_service.create()` already validates — no additional changes.

**Step 3:** Commit.

---

### Task 4: Helper functions — `_heuristic_weight` and `_effective_leverage`

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Create: `backend/tests/unit/services/test_portfolio_orchestration.py`

**Step 1 (Red):** Write tests:

```python
def test_heuristic_weight_maximize_sharpe_uses_sharpe_metric():
    candidate = {"metrics": {"sharpe": 1.5}}
    assert _heuristic_weight(candidate, "maximize_sharpe") == 1.5

def test_heuristic_weight_maximize_sharpe_floors_at_zero():
    candidate = {"metrics": {"sharpe": -0.3}}
    assert _heuristic_weight(candidate, "maximize_sharpe") == 1.0  # floor

def test_heuristic_weight_equal_weight_returns_unity():
    assert _heuristic_weight({"metrics": {}}, "equal_weight") == 1.0

def test_effective_leverage_scales_down_by_downside_target():
    import pandas as pd
    returns = pd.Series([0.01, -0.02, 0.015, -0.03], index=pd.date_range("2024-01-01", periods=4, freq="D"))
    weighted = [("strat", 1.0, returns)]
    lev = _effective_leverage(weighted_series=weighted, requested_leverage=2.0, downside_target=0.05)
    assert 0.1 < lev <= 2.0  # scaled down from 2.0
```

**Step 2 (Green):** Port `_heuristic_weight()` and `_effective_leverage()` from `codex-version/backend/src/msai/services/portfolio_service.py:409-437`. Adapt to read metrics directly from candidate dict (not `candidate["selection"]["metrics"]`).

**Step 3:** Commit.

---

### Task 5: `_resolve_allocations` — DB-backed allocation resolution

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Modify: test file from Task 4

**Step 1 (Red):** Integration test with fixture: create `Strategy`, `GraduationCandidate`, `Portfolio`, `PortfolioAllocation`. Call `_resolve_allocations(session, portfolio, objective="equal_weight")` and assert returned rows have `candidate_id`, `strategy_id`, `strategy_file_path`, `config`, `instruments`, `weight` (normalized).

**Step 2 (Green):** Implement `_resolve_allocations` reading from DB eager-loaded relationships:

- For each `PortfolioAllocation`: load `candidate` (selectinload `strategy`) → extract:
  - `strategy_file_path` = `strategy.file_path`
  - `strategy_class` = `strategy.strategy_class`
  - `config` = merge of `strategy.default_config or {}` overlaid with `candidate.config or {}`
  - `instruments` = `candidate.config.get("instruments") or strategy.default_config.get("instruments") or []` — raise `PortfolioDefinitionError` if empty
  - `asset_class` = `candidate.config.get("asset_class") or strategy.default_config.get("asset_class") or "stocks"`
- Use `allocation.weight` if > 0, else `_heuristic_weight(candidate.metrics or {}, objective)`
- Pass rows through `analytics_math.normalize_weights`

**Step 3:** Commit.

---

### Task 6: `_run_candidate_backtest` — single-allocation execution

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Modify: test file

**Step 1 (Red):** Test with mocked `BacktestRunner.run` returning canned `BacktestResult`. Assert output dict has `candidate_id`, `strategy_name`, `instruments`, `weight`, `metrics`, `returns` (list[float]), `timestamps` (list[str]).

**Step 2 (Green):** Port `_run_candidate_backtest` — adapted to Claude signatures:

- `instrument_ids = ensure_catalog_data(symbols=allocation["instruments"], raw_parquet_root=Path(settings.data_root)/"parquet", catalog_root=settings.nautilus_catalog_root, asset_class=allocation.get("asset_class", "stocks"))` — returns canonical IDs
- `result = BacktestRunner().run(strategy_file=allocation["strategy_file_path"], strategy_config=allocation["config"], instrument_ids=instrument_ids, start_date=start_date, end_date=end_date, catalog_path=settings.nautilus_catalog_root)`
- Extract returns from `result.account_df`: compute pct_change of `total_equity` or `net_liquidation` column (whichever exists); fall back to empty series
- Return dict: `{candidate_id, strategy_name, instruments, weight, metrics, returns (list[float]), timestamps (list[str])}`

**Note:** Multi-asset-class portfolios are a follow-up; the `asset_class` is per-allocation and propagates through `ensure_catalog_data`.

**Step 3:** Commit.

---

### Task 7: `_execute_candidate_backtests` — parallel orchestration

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Modify: test file

**Step 1 (Red):** Test that 2 mocked candidates run in parallel (use time-based mock to verify concurrency).

**Step 2 (Green):** Port from Codex (ThreadPoolExecutor). Single-worker fast path preserved.

**Step 3:** Commit.

---

### Task 8: `_load_benchmark_returns` — benchmark fetch via MarketDataQuery

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Modify: test file

**Step 1 (Red):** Test returns `None` when benchmark symbol is empty/None. Test returns a `pd.Series` when MarketDataQuery returns bar data (mock).

**Step 2 (Green):** Adapt from Codex — **key adaptation:** Claude's `MarketDataQuery.get_bars(symbol, start, end, interval)` returns `list[dict]` directly, not a `{"bars": [...]}` wrapper. Iterate rows directly. Use the un-dotted raw symbol: `symbol.split(".", 1)[0]`.

**Step 3:** Commit.

---

### Task 9: `run_portfolio_backtest` — end-to-end orchestration

**Files:**

- Modify: `backend/src/msai/services/portfolio_service.py`
- Create: `backend/tests/integration/test_portfolio_job_orchestration.py`

**Step 1 (Red):** Integration test: seed DB with portfolio + 2 allocations + mocked BacktestRunner returning canned returns. Call `run_portfolio_backtest(session, run_id)`. Assert `PortfolioRun` row after:

- `status == "completed"`
- `metrics` has `total_return`, `sharpe`, `num_strategies == 2`, `effective_leverage`
- `series` is non-empty list
- `allocations` has 2 entries with weights summing to 1.0
- `report_path` is set and file exists

**Step 2 (Green):** Port `run_portfolio_backtest`:

1. Fetch portfolio + allocations + run from DB
2. `_resolve_allocations(session, portfolio, objective)`
3. `_execute_candidate_backtests(...)` — parallel
4. Build `weighted_series` tuples
5. `_effective_leverage(...)`
6. `combine_weighted_returns(...)` (already in Claude's analytics_math)
7. `_load_benchmark_returns(...)` if specified
8. `compute_series_metrics(...)` — already in Claude's analytics_math
9. `build_series_from_returns(...)` — already in Claude's analytics_math
10. `html = ReportGenerator().generate_tearsheet(combined_returns, benchmark=benchmark_series)`; `report_path = ReportGenerator().save_report(html, backtest_id=str(run_id), data_root=str(settings.data_root))` — uses `{data_root}/reports/{run_id}.html`, shared with single-backtest reports (same namespace is fine since run IDs are UUIDs).
11. `_update_run()` — persist status/metrics/series/allocations/report_path/heartbeat_at/completed_at

**Step 3:** Commit.

---

### Task 10: Worker replacement

**Files:**

- Modify: `backend/src/msai/workers/portfolio_job.py`

**Step 1 (Red):** Integration test: arq worker enqueues a job for a prepared `PortfolioRun`, asserts run reaches `completed` state with populated metrics.

**Step 2 (Green):** Replace placeholder with real implementation:

- Load run, mark running
- Acquire compute_slots (existing Claude service) with `slot_count = max_parallelism or 1`
- Call `PortfolioService().run_portfolio_backtest(session, run_id)`
- Mark failed on exception with error_message; release slots in finally

Keep it simpler than Codex's heartbeat loop for now — rely on compute_slots TTL for stale detection. (Heartbeat loop deferred to Phase 2 alongside alerting.)

**Step 3:** Commit.

---

### Task 11: Smoke test — manual end-to-end via arq

**Files:**

- Manual validation (not code)

**Step 1:** Start Docker Compose dev stack. Seed test data via CLI: a 2-candidate portfolio with both strategies already graduated.

**Step 2:** POST `/api/v1/portfolio/{id}/runs` → assert 200 + run_id. Watch `portfolio-worker` logs.

**Step 3:** GET `/api/v1/portfolio/runs/{run_id}` after completion — assert status=completed, metrics populated, `report_path` downloadable.

**Step 4:** Document run in CONTINUITY, commit smoke-test evidence as a screenshot if meaningful.

---

## Testing Strategy

- **Unit (Task 4 helpers + Task 3 schemas):** Fast, pure-function, no DB.
- **Integration (Tasks 5-10):** pytest + real Postgres + pytest fixtures that seed `Strategy`, `GraduationCandidate`, `Portfolio`, `PortfolioAllocation`, `PortfolioRun`. Mock `BacktestRunner.run` to avoid Nautilus overhead in tests; the real BacktestRunner is already tested independently in PR#5.
- **Smoke (Task 11):** One end-to-end run in Docker Compose dev to confirm the full path works.

---

## E2E Use Cases

**N/A — backend-only port. No user-facing behavior change until frontend portfolio UI is later wired (separate follow-up).** The frontend already calls `/api/v1/portfolio/runs/{id}` — the new fields (`series`, `allocations`) will simply be `null` until this port lands, then populated. No breaking changes to the response contract.

---

## Risks & Mitigations

| Risk                                                                      | Mitigation                                                                                                                         |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| BacktestRunner subprocess pattern interacts badly with ThreadPoolExecutor | Codex uses the same pattern; proven to work. Fall back to sequential execution if tests reveal a deadlock.                         |
| GraduationCandidate.config missing `instruments`                          | Validate at `_resolve_allocations` time; raise `PortfolioDefinitionError` with clear message.                                      |
| Migration race with existing data                                         | All new columns are nullable or have defaults. Safe on populated DBs.                                                              |
| `compute_slots` lease expires during long backtest                        | Existing Claude compute_slots has renewal; Task 10 will use default TTL. If backtests exceed TTL, Phase 2 heartbeat port will fix. |

---

## Success Criteria

1. `uv run pytest tests/ -v` passes including new tests
2. `uv run ruff check src/` clean
3. `uv run mypy src/ --strict` clean on modified files
4. Manual smoke test: 2-candidate portfolio → `completed` run with populated metrics + downloadable report
5. Zero changes to existing API response contract (backwards compatible)
