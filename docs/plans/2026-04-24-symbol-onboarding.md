# Symbol Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Plan Revision History

**v3 → v4 (2026-04-24)** applied iter-3 narrow findings (1 P1 + 1 P2):

- **P1 HTTP 200 vs 202 status-code contract drift (T9).** v3 had `@router.post(..., status_code=status.HTTP_202_ACCEPTED)` on `/onboard` and `/onboard/{id}/repair`, but `_enqueue_and_persist_run` returned the plain `OnboardResponse` model on the two dedup branches (existing-row fast path + arq-None re-SELECT hit). FastAPI applied the decorator's 202 default to those returns, contradicting the integration tests at lines ~3493–3625 that assert `r2.status_code == 200` on duplicate POSTs. Fixed: the helper now returns `JSONResponse(status_code=200, content=OnboardResponse(...).model_dump(mode="json"))` on both dedup branches; the success branch keeps returning the plain model so the decorator's 202 default applies for fresh-enqueue happy path. Route signature widened to `OnboardResponse | JSONResponse`. Decorator comments added to both `/onboard` and `/onboard/{id}/repair` documenting "default 202; dedup branches return 200 via JSONResponse." Existing 202/200 tests at ~3585–3587 untouched — they were already correct, the v3 helper was wrong.
- **P2 Decimal scale guard accepts trailing-zero overprecision (T12 CLI).** v3's check `if quantized != raw: raise typer.BadParameter(...)` compared values; `Decimal("123.450") == Decimal("123.45")` is True, so `--cost-ceiling-usd 123.450` slipped through despite declaring 3 decimal places. Fixed: replaced with `if raw.as_tuple().exponent < -2: raise typer.BadParameter(...)` — the exponent of a Decimal equals `-N` where N is the source-string decimal-place count, so trailing-zero forms are caught. New regression test `test_cost_ceiling_usd_rejects_trailing_zero_overprecision` pins the fix; existing `test_cost_ceiling_usd_rejects_more_than_two_decimals` (123.456 case) docstring updated to note the exponent-vs-value distinction.

---

**v2 → v3 (2026-04-24)** applied iter-2 narrow findings (4 P1 + 2 P2):

- **P1-A `IngestResult` shape mismatch (T6a).** v2's helper read `stats.get("bars_written", 0)` and `stats.get("symbols_covered", symbols)`, but `DataIngestionService.ingest_historical()` actually returns `{"ingested": {raw_symbol: {"bars": int, ...}}, "empty_symbols": [...], ...}` — neither flat key exists, so the helper would have silently reported 0 bars forever. Fixed: derive `bars_written` and `symbols_covered` locally from the `ingested` dict; surface `empty_symbols` as a new `IngestResult` field so the orchestrator can branch on "ingest succeeded with 0 bars" vs "ingest fully covered" vs "ingest raised". T6 mock in `test_orchestrator.py` updated. T6a unit tests rewritten to use the REAL return shape.
- **P1-B `/onboard` idempotency (T9).** v2 still (a) returned a fabricated `reserved_id` when `enqueue_job()` returned `None` and the row wasn't yet visible — orphan run id that `/status` can never resolve, and (b) attempted to commit the row before checking the digest. Fixed by extracting a shared `_enqueue_and_persist_run` helper that pins the order: `SELECT FOR UPDATE` on digest → fast-path 200 OK if row exists → `enqueue_job` first → on `None` return after a 100 ms backoff re-SELECT, return 409 `DUPLICATE_IN_FLIGHT` (NEVER fabricate) → on success commit the row with abort-on-rollback. Three new tests pin: 200 + same `run_id` on duplicate, 409 `DUPLICATE_IN_FLIGHT` on race, 503 `QUEUE_UNAVAILABLE` on Redis down with zero rows committed.
- **P1-C `/repair` same idempotency fix.** v2's repair handler did `INSERT → commit → enqueue` — Redis-down would leak orphan `pending` rows that no worker would ever pick up. Fixed: repair now routes through the SAME `_enqueue_and_persist_run` helper with a parent-scoped digest. The pattern is now defined exactly once.
- **P1-D T12 CLI `cost_ceiling_usd` type.** v2's typer signature was `float | None`; the schema is `Decimal(max_digits=12, decimal_places=2)`. The float option silently round-tripped values like `123.456` through IEEE-754 + server-side `quantize` — caller never saw the precision loss. Fixed: option type is now `str | None`, parsed locally via `Decimal(...)` + `quantize(Decimal("0.01"))` with `typer.BadParameter` on >2-decimal inputs and forwarded as a JSON string. Two new CLI tests pin: rejection of `123.456` + acceptance of `123.45` round-trip.
- **P2-A T7 dispatch row Writes column.** Added `backend/tests/integration/symbol_onboarding/test_worker_task.py` to T7's Writes column.
- **P2-B T6a delegation test.** Added `test_run_ingest_shim_delegates_to_ingest_symbols` so a future refactor that breaks the "single code path owns the real ingest pipeline" contract fails the unit suite.

---

**v1 → v2 (2026-04-24)** applied iter-1 findings (1 P0 + 7 P1 + 6 P2 + 2 P3):

- **P0 (council verdict Option A — inline ingest).** T6 orchestrator no longer calls `enqueue_ingest()` and does NOT `await job.result()`. Instead it calls an extracted async helper `ingest_symbols(...)` at `services/data_ingestion.py` directly (in-process). `run_ingest` (arq wrapper at `workers/settings.py:70`) stays a 3-line shim that still returns `None`. Queue-topology self-deadlock closed: the onboarding worker + ingest worker share the same `max_jobs=1` gate in `IngestWorkerSettings`; awaiting a child arq job from a parent in the same queue would have hung forever.
- **P1 API surface drift.** `get_arq_pool` → `get_redis_pool` (real name at `core/queue.py:52`). `get_databento_historical_client` removed (does not exist) — replaced with direct `DatabentoClient()` instantiation. `IBRefreshService` does not exist — T6 uses `IBQualifier.qualify(spec: InstrumentSpec)` at `ib_qualifier.py:186` with an adapter that constructs an `InstrumentSpec` from `(symbol, asset_class)`. `DatabentoBootstrapService.bootstrap(...)` corrected to batch signature `bootstrap(symbols=[...], asset_class_override=..., exact_ids=...)` returning `list[BootstrapResult]` — T6 calls with `symbols=[spec.symbol]` and inspects the single-item result. `DatabentoBootstrapService.__init__(session_factory, databento_client)` takes required args — `_default_bootstrap_service()` updated. T10's `find_active_aliases` honestly scoped as NEW readiness-aggregation code, not a trivial wrapper over `resolve_for_backtest`.
- **P1 status enum alignment.** ONE canonical vocabulary, enforced across T2 + T6 + T7 + T9 + status-contract table:
  - `SymbolStatus`: `not_started` / `in_progress` / `succeeded` / `failed`.
  - `SymbolStepStatus`: `pending` / `bootstrap` / `ingest` / `coverage` / `ib_qualify` / `completed` / `ib_skipped` / `coverage_failed`.
  - `SymbolOnboardingRunStatus`: `pending` / `in_progress` / `completed` / `completed_with_failures` / `failed`.
- **P1 idempotency orphan rows (T9).** `enqueue_job()` returns `None` on dedup; v1 minted orphan `pending` DB rows that never ran. v2 stores the `_job_id` digest on the row, calls `pool.enqueue_job` FIRST, looks up existing run on `None` return and returns that `run_id`, only commits new row on non-`None`, rolls back on enqueue exception.
- **P1 run-status semantics (T7).** `failed` at run level ONLY for systemic short-circuit faults (outer try/except). All-symbols-failed via normal loop → `completed_with_failures` (matches council pin).
- **P1 queue registration drift.** File Structure header fixed: `workers/settings.py` → `workers/ingest_settings.py` (same as T7 already said).
- **P1 Decimal/float round-trip (T2).** `cost_ceiling_usd: float | None` → `Decimal | None` with `max_digits=12, decimal_places=2, ge=0` to match `Numeric(12,2)` column shape.
- **P1 metric-import ordering.** T13 block moved BEFORE T6 in chronological order so `onboarding_symbol_duration_seconds` exists by the time T7 imports it.
- **P2 asset-class taxonomy.** Seam added via new helper `normalize_asset_class_for_ingest(...)` at `services/symbol_onboarding/__init__.py`. T2 schema stays `equity|futures|fx|option` (registry/user-facing taxonomy); ingest/Parquet path translates at call boundary. All T3–T15 tests updated to use `equity` (was `stocks`).
- **P2 plural table + `updated_at` (T1).** `symbol_onboarding_run` → `symbol_onboarding_runs` + `updated_at` column + `_job_id_digest` column. Round-trip test + every downstream T6/T7/T9/T15 reference updated.
- **P2 `_error_response` promotion.** New task T8-prime promotes the helper from `api/backtests.py` (private leading-underscore) to `api/_common.py` (shared); `api/backtests.py`, `api/instruments.py`, and the new `api/symbol_onboarding.py` all import from the shared module.
- **P2 `compute_advisory_lock_key` abuse (T9).** `compute_advisory_lock_key("symbol_onboarding", payload_blob, "v1")` replaced with a new sibling helper `compute_blake2b_digest_key(*parts: str) -> int` in `services/nautilus/security_master/service.py` (shares blake2b primitive but carries the arbitrary-parts semantics).
- **P2 test coverage gaps (T15).** New tests: duplicate-submit returns existing run-id, enqueue-failure-after-DB-commit rolls back, systemic-short-circuit vs per-symbol-failure distinction, inline-ingest helper failure propagation. Removed obsolete tests that assumed `job.result()` returned a structured dict.
- **P3 T0 conftest stub.** `_make_symbol_onboarding_run_row` stub removed (was `NotImplementedError`). T1 lands the real fixture.
- **P3 File-Structure header fix** (absorbed into P1 queue registration drift above).

---

**Goal:** Ship a thin orchestration layer that takes a git-tracked `watchlists/*.yaml` manifest through bootstrap → ingest → optional IB-qualify in a single arq worker job, exposing preflight cost + status + repair via API/CLI, with window-scoped readiness and the `/api/v1/universe` HTTP router deleted.

**Architecture:** Council-ratified **Approach 1** (single arq entrypoint `run_symbol_onboarding`). One `SymbolOnboardingRun` Postgres row owns progress; `_onboard_one_symbol()` seam enables future parallelism rewrite as a one-commit change. Phase-local bounded concurrency allowed ONLY inside the bootstrap phase (mirrors `DatabentoBootstrapService`). Ingest + IB qualification strictly sequential. `asyncio.wait_for(120s)` wraps every IB call. 100-symbol API hard cap. Three Prometheus metrics.

**Tech Stack:** Python 3.12 · FastAPI · arq (Redis) · PostgreSQL 16 (Alembic) · Pydantic V2 · Typer CLI · Databento SDK (`metadata.get_cost`) · PyYAML 6.0.2 (new dep) · python-dateutil (new dep) · NautilusTrader (existing) · SQLAlchemy 2.0 async · tenacity (PR #44 retry pattern)

**Cross-references:**

- **PRD:** `docs/prds/symbol-onboarding.md` v1.0 (10 user stories, 11 non-goals, binding contract corrections)
- **Discussion log + council verdict:** `docs/prds/symbol-onboarding-discussion.md`
- **Approach Comparison + Contrarian gate:** `CONTINUITY.md` § "Approach Comparison"
- **Research brief:** `docs/research/2026-04-24-symbol-onboarding.md` (6 design-changing findings)
- **Predecessor PRs:** #32 (registry schema), #40 (auto-heal), #44 (Databento bootstrap)

---

## Ground-truth pins (do not deviate)

These are the EXACT primitives/patterns to use. Every plan-review iteration finding will trace to a drift from one of these.

| Primitive                          | Where                                                                                                                                   | Pattern                                                                                                                                                                                         |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pydantic V2 cross-field invariants | `schemas/instrument_bootstrap.py:88-104` (PR #44)                                                                                       | `@model_validator(mode="after") def _name(self) -> Self: if bad: raise ValueError(...); return self`                                                                                            |
| Canonical error envelope           | `api/_common.py::error_response(status_code, code, message)` (promoted in T8-prime)                                                     | `JSONResponse(status_code=X, content={"error":{"code":"X","message":"Y"}})` (imported, NOT re-implemented). Previously private `_error_response` in `api/backtests.py:92`.                      |
| Metrics registry                   | `services/observability/trading_metrics.py`                                                                                             | `_r = get_registry(); counter = _r.counter(name, help); counter.labels(k=v).inc()` — assert via `registry.render()` text-substring match                                                        |
| Advisory lock helper               | `services/nautilus/security_master/service.py::compute_advisory_lock_key`                                                               | blake2b digest of `"{provider}:{raw_symbol}:{asset_class}"` — **NOT** `hash()` (PYTHONHASHSEED randomizes)                                                                                      |
| Arbitrary-parts blake2b digest     | `services/nautilus/security_master/service.py::compute_blake2b_digest_key` (new helper extracted in T9)                                 | Same primitive as advisory lock helper but carries `(*parts: str)` semantics — consumed by onboarding-run `_job_id_digest`                                                                      |
| Bootstrap service                  | `services/nautilus/security_master/databento_bootstrap.py::DatabentoBootstrapService`                                                   | `__init__(session_factory, databento_client)`; `bootstrap(symbols: list[str], asset_class_override: str \| None, exact_ids: dict[str,str] \| None) -> list[BootstrapResult]` (batch)            |
| IB qualifier                       | `services/nautilus/security_master/ib_qualifier.py::IBQualifier`                                                                        | `qualify(spec: InstrumentSpec) -> Instrument`; `qualify_many(specs: list[InstrumentSpec]) -> list[Instrument]`                                                                                  |
| Databento HTTP client              | `services/data_sources/databento_client.py::DatabentoClient`                                                                            | `DatabentoClient(api_key=..., dataset=...)` — instantiate directly; NO `get_databento_historical_client()` factory exists                                                                       |
| arq redis pool                     | `core/queue.py::get_redis_pool`                                                                                                         | `pool = await get_redis_pool()` — NOT `get_arq_pool`                                                                                                                                            |
| Ingest entrypoints                 | `services/data_ingestion.py::ingest_symbols` (NEW helper extracted in T6a) + `workers/settings.py::run_ingest` (arq shim, returns None) | Orchestrator calls `ingest_symbols(...)` DIRECTLY (in-process); it does NOT enqueue a child arq job (council verdict Option A — avoids self-deadlock against `IngestWorkerSettings.max_jobs=1`) |
| Auth dependency                    | `core/auth.py:92` `get_current_user`                                                                                                    | `Depends(get_current_user)` + `X-API-Key: MSAI_API_KEY` dev path                                                                                                                                |
| Session factory                    | `core/database.py::get_session_factory` (PR #44) / `async_session_factory`                                                              | `Depends(get_session_factory)` returns `async_sessionmaker[AsyncSession]`                                                                                                                       |
| arq job pattern                    | `workers/backtest_job.py`, `services/backtests/auto_heal.py:84`                                                                         | `async def run_symbol_onboarding(ctx, run_id): pool = ctx["redis"]; ...`. NOTE: onboarding calls `ingest_symbols(...)` directly, NOT child-job enqueue                                          |
| testcontainers Postgres            | `tests/integration/conftest_databento.py` (PR #44)                                                                                      | `isolated_postgres_url` module-scope + `session_factory` function-scope with `Base.metadata.create_all`                                                                                         |
| Worker stale-import fix            | `scripts/restart-workers.sh`                                                                                                            | Run after merging any change under `src/msai/{services,workers,live_supervisor}`                                                                                                                |

---

## File Structure

**New backend source files:**

- `backend/src/msai/api/_common.py` — shared `error_response(status_code, code, message)` helper promoted from `api/backtests.py` in T8-prime
- `backend/src/msai/models/symbol_onboarding_run.py` — `SymbolOnboardingRun` table (status enum, symbol_states JSONB, cost_ceiling_usd, request_live_qualification, `_job_id_digest` idempotency key, timestamps incl. `updated_at`)
- `backend/alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py` — additive migration (plural table name)
- `backend/src/msai/schemas/symbol_onboarding.py` — Pydantic V2: `OnboardRequest`, `OnboardResponse`, `StatusResponse`, `DryRunResponse`, `SymbolStatus` enum, `SymbolStepStatus` enum, `ReadinessResponse`
- `backend/src/msai/services/symbol_onboarding/__init__.py` — exports `normalize_asset_class_for_ingest` helper
- `backend/src/msai/services/symbol_onboarding/manifest.py` — YAML `safe_load`, schema validation, `trailing_5y` → `(start, end-1d)` via `dateutil.relativedelta`, cross-watchlist dedup
- `backend/src/msai/services/symbol_onboarding/cost_estimator.py` — Databento `metadata.get_cost` wrapper, `estimate_confidence` classification
- `backend/src/msai/services/symbol_onboarding/coverage.py` — on-the-fly Parquet directory scan → gap list
- `backend/src/msai/services/symbol_onboarding/orchestrator.py` — `_onboard_one_symbol(session, symbol, window, request_live_qualification) -> SymbolOutcome`; calls `ingest_symbols(...)` directly (council Option A)
- `backend/src/msai/workers/symbol_onboarding_job.py` — `run_symbol_onboarding(ctx, run_id)` arq task
- `backend/src/msai/api/symbol_onboarding.py` — `POST /onboard`, `POST /onboard/dry-run`, `GET /onboard/{run_id}/status`, `POST /repair`, `GET /readiness`
- `watchlists/README.md` + `watchlists/example-core-equities.yaml`

**Modified backend source files:**

- `backend/src/msai/api/backtests.py` — convert `_error_response` to `from msai.api._common import error_response` (T8-prime)
- `backend/src/msai/api/instruments.py` — same re-import swap (T8-prime)
- `backend/src/msai/services/data_ingestion.py` — extract reusable `async def ingest_symbols(...)` helper out of the arq-wrapper path (T6a)
- `backend/src/msai/services/nautilus/security_master/service.py` — add `compute_blake2b_digest_key(*parts: str) -> int` sibling helper next to `compute_advisory_lock_key` (T9 uses it); add `find_active_aliases(...)` readiness-aggregation helper (T10 — honest new code, not a trivial wrapper)
- `backend/src/msai/main.py` — register new router; REMOVE `asset_universe` router import + `include_router`
- `backend/src/msai/cli.py` — add `instruments` sibling `symbols` Typer sub-app with `onboard`, `status`, `repair`
- `backend/src/msai/services/observability/trading_metrics.py` — append 3 new metrics (T13 lands chronologically BEFORE T7 so the import resolves)
- `backend/pyproject.toml` — add `pyyaml>=6.0.2,<7`, `python-dateutil>=2.9.0`
- `backend/src/msai/workers/ingest_settings.py` — register `run_symbol_onboarding` in `IngestWorkerSettings.functions` list (same worker, same `max_jobs=1` gate). NOTE: NOT `workers/settings.py` — that's the default backtest worker, which this plan does not modify.

**Deleted files:**

- `backend/src/msai/api/asset_universe.py` (HTTP router only — US-010)
- `backend/tests/unit/test_asset_universe.py` subset: HTTP route tests (keep service-layer tests)

**Kept unchanged (must not break):**

- `backend/src/msai/services/asset_universe.py` (used by `workers/nightly_ingest.py`)
- `backend/src/msai/models/asset_universe.py` (backing table for the service)
- `backend/src/msai/workers/nightly_ingest.py` (imports `AssetUniverseService` directly)

**New tests:**

- `backend/tests/integration/conftest_symbol_onboarding.py` — testcontainers + mock Databento/IB fixtures
- `backend/tests/unit/services/symbol_onboarding/test_manifest.py`
- `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py`
- `backend/tests/unit/services/symbol_onboarding/test_coverage.py`
- `backend/tests/unit/services/symbol_onboarding/test_orchestrator.py`
- `backend/tests/unit/schemas/test_symbol_onboarding.py`
- `backend/tests/unit/test_cli_symbols.py`
- `backend/tests/integration/test_symbol_onboarding_api.py`
- `backend/tests/integration/test_symbol_onboarding_e2e.py` (full happy path + partial failure + idempotency)
- `backend/tests/integration/test_alembic_migrations.py` — APPEND `test_symbol_onboarding_runs_roundtrip` (plural table; see T1)

---

## Status contract (council-pinned, do not change during implementation)

### `SymbolOnboardingRun.status` (run-level)

| Value                     | Meaning                                                                                                                                                                                                                                                                                                                                                                 | Terminal? |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `pending`                 | Enqueued, worker hasn't picked it up                                                                                                                                                                                                                                                                                                                                    | No        |
| `in_progress`             | Worker is iterating symbols                                                                                                                                                                                                                                                                                                                                             | No        |
| `completed`               | All symbols terminal-success (`succeeded`)                                                                                                                                                                                                                                                                                                                              | Yes       |
| `completed_with_failures` | Any mixed outcome OR every symbol `failed` but the loop ran to completion (per-symbol failure, not systemic)                                                                                                                                                                                                                                                            | Yes       |
| `failed`                  | **Systemic short-circuit only.** Catastrophic infrastructure failure (DB down mid-loop, Redis down, unhandled exception in the worker's outer try/except) — remaining symbols left `not_started`. Normal Databento auth / rate-limit per-symbol errors are NOT systemic here — they produce `symbol.status=failed` and the run terminates at `completed_with_failures`. | Yes       |

### `SymbolOnboardingRun.symbol_states[<symbol>].status` (per-symbol)

| Value         | Meaning                                                                                                    |
| ------------- | ---------------------------------------------------------------------------------------------------------- |
| `not_started` | Run didn't reach this symbol                                                                               |
| `in_progress` | Orchestrator has begun one of the step phases for this symbol (bootstrap / ingest / coverage / ib_qualify) |
| `succeeded`   | Symbol fully onboarded (all requested phases green)                                                        |
| `failed`      | Per-symbol failure — has `error.code`, `error.message`, `next_action`                                      |

### `SymbolOnboardingRun.symbol_states[<symbol>].step` (per-symbol phase)

| Value             | Meaning                                                                        |
| ----------------- | ------------------------------------------------------------------------------ |
| `pending`         | Seeded on POST, no phase entered yet                                           |
| `bootstrap`       | Databento registry-bootstrap call in flight                                    |
| `ingest`          | In-process `ingest_symbols(...)` call in flight                                |
| `coverage`        | Post-ingest Parquet coverage scan in flight                                    |
| `ib_qualify`      | IB qualification in flight (wrapped in `asyncio.wait_for(120s)`)               |
| `completed`       | All requested phases green (terminal success)                                  |
| `ib_skipped`      | Terminal success without IB qualification (`request_live_qualification=false`) |
| `coverage_failed` | Terminal failure — post-ingest coverage was not `full`                         |

### Transition rules

- Per-symbol updates use `UPDATE ... SET symbol_states = jsonb_set(symbol_states, '{<symbol>,status}', :new)` — no advisory lock needed (single row, single task writer).
- Run `status` transitions are linear: `pending → in_progress → {completed|completed_with_failures|failed}`. Never revert.
- **`failed` (run-level) is ONLY set in the worker's outer try/except for catastrophic infrastructure failures.** Any terminal state reached via the normal per-symbol loop is `completed` (all succeeded) or `completed_with_failures` (anything else — mixed or all-failed-per-symbol).

---

## Dispatch Plan

| Task ID  | Depends on           | Writes (concrete file paths)                                                                                                                                                                                                               |
| -------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| T0       | —                    | `backend/pyproject.toml`, `backend/tests/integration/conftest_symbol_onboarding.py`, `watchlists/README.md`, `watchlists/example-core-equities.yaml`                                                                                       |
| T1       | T0                   | `backend/src/msai/models/symbol_onboarding_run.py`, `backend/alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py`, `backend/tests/integration/test_alembic_migrations.py` (append)                                                 |
| T2       | T1                   | `backend/src/msai/schemas/symbol_onboarding.py`, `backend/tests/unit/schemas/test_symbol_onboarding.py`                                                                                                                                    |
| T3       | T0                   | `backend/src/msai/services/symbol_onboarding/__init__.py` (incl. `normalize_asset_class_for_ingest`), `backend/src/msai/services/symbol_onboarding/manifest.py`, `backend/tests/unit/services/symbol_onboarding/test_manifest.py`          |
| T4       | T0                   | `backend/src/msai/services/symbol_onboarding/cost_estimator.py`, `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py`                                                                                                    |
| T5       | T0                   | `backend/src/msai/services/symbol_onboarding/coverage.py`, `backend/tests/unit/services/symbol_onboarding/test_coverage.py`                                                                                                                |
| T6a      | T0                   | `backend/src/msai/services/data_ingestion.py` (extract `async def ingest_symbols(...)` helper; keep `run_ingest` as 3-line shim returning `None`), `backend/tests/unit/services/test_data_ingestion_ingest_symbols.py`                     |
| T13      | T0                   | `backend/src/msai/services/observability/trading_metrics.py` (3 new metrics — reordered BEFORE T6 so T7 imports resolve), `backend/tests/unit/observability/test_onboarding_metrics.py`                                                    |
| T6       | T1, T2, T5, T6a, T13 | `backend/src/msai/services/symbol_onboarding/orchestrator.py`, `backend/tests/integration/symbol_onboarding/test_orchestrator.py`                                                                                                          |
| T7       | T1, T2, T6           | `backend/src/msai/workers/symbol_onboarding_job.py`, `backend/src/msai/workers/ingest_settings.py` (append `run_symbol_onboarding` to `IngestWorkerSettings.functions`), `backend/tests/integration/symbol_onboarding/test_worker_task.py` |
| T8-prime | T0                   | `backend/src/msai/api/_common.py` (promote shared `error_response` helper), `backend/src/msai/api/backtests.py` (swap import), `backend/src/msai/api/instruments.py` (swap import), `backend/tests/unit/api/test_common_error_response.py` |
| T8       | T2, T4, T8-prime     | `backend/src/msai/api/symbol_onboarding.py` (dry-run endpoint only)                                                                                                                                                                        |
| T9       | T2, T7, T8           | `backend/src/msai/api/symbol_onboarding.py` (onboard + status + repair — same file, serialized after T8); `backend/src/msai/services/nautilus/security_master/service.py` (add `compute_blake2b_digest_key` helper)                        |
| T10      | T1, T5, T9           | `backend/src/msai/api/symbol_onboarding.py` (readiness endpoint extension — serialized after T9); `backend/src/msai/services/nautilus/security_master/service.py` (add `find_active_aliases` readiness-aggregation helper)                 |
| T11      | T9                   | `backend/src/msai/main.py` (router wire + remove `asset_universe` router)                                                                                                                                                                  |
| T12      | T9                   | `backend/src/msai/cli_symbols.py`, `backend/src/msai/cli.py`, `backend/tests/unit/test_cli_symbols.py`                                                                                                                                     |
| T14      | T11                  | DELETE `backend/src/msai/api/asset_universe.py`; PRUNE HTTP-route tests in `backend/tests/integration/api/test_asset_universe*.py`                                                                                                         |
| T15      | T9, T10, T12         | `backend/tests/integration/symbol_onboarding/test_orchestrator_failure_paths.py`, `backend/tests/integration/symbol_onboarding/test_end_to_end_run.py`                                                                                     |

**Scheduling:** serial is the default; tasks T3/T4/T5/T6a/T13/T8-prime are ready-in-parallel after T0. T6 depends on T6a (ingest helper) and T13 (metrics) being in place. T8 must finish before T9 starts (same file `api/symbol_onboarding.py`). T9 → T10 same-file ordering. T11 (`main.py`) + T12 (`cli.py`) + T14 (delete `asset_universe.py`) are file-disjoint and can run parallel after their deps.

**Sequential mode available** if plan review surfaces coupling. With concurrency cap = 3 subagents.

---

## Phase 0 — Pre-flight (T0)

### Task 0: Dependencies + test fixtures + watchlists directory

**Files:**

- Modify: `backend/pyproject.toml` (add `pyyaml` + `python-dateutil` deps)
- Create: `backend/tests/integration/conftest_symbol_onboarding.py`
- Create: `watchlists/README.md`
- Create: `watchlists/example-core-equities.yaml`

- [ ] **Step 1: Add `pyyaml` and `python-dateutil` to `[project].dependencies` in `backend/pyproject.toml`.**

Find the `[project]` block and add inside `dependencies = [...]`:

```toml
    "pyyaml>=6.0.2,<7",
    "python-dateutil>=2.9.0",
```

Verify exact existing block shape by reading `backend/pyproject.toml` first.

- [ ] **Step 2: Run `cd backend && uv sync --extra dev`**

Expected: lockfile updates, `pyyaml` + `python-dateutil` installed. No errors.

- [ ] **Step 3: Create `backend/tests/integration/conftest_symbol_onboarding.py` verbatim:**

```python
"""Reusable fixtures for Symbol Onboarding integration tests.

Provides:
- ``session_factory`` — testcontainers Postgres with full schema.
- ``mock_databento`` — DatabentoClient-shaped mock for bootstrap/ingest/cost.
- ``mock_ib_refresh`` — AsyncMock standing in for the IB ``msai instruments refresh`` path.
- ``tmp_parquet_root`` — tmp_path fixture seeded with fake Parquet month files.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.models import Base

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def mock_databento():
    """DatabentoClient-shaped mock. Tests customize side_effects per scenario."""
    client = MagicMock()
    client.api_key = "test-key"
    client.fetch_definition_instruments = AsyncMock()
    client.get_cost_estimate = AsyncMock(return_value=1.25)  # default cheap
    return client


@pytest.fixture
def mock_ib_refresh():
    """AsyncMock for IB instruments refresh. Defaults to success."""
    return AsyncMock(return_value=None)


@pytest.fixture
def tmp_parquet_root(tmp_path: Path) -> Path:
    """Parquet root with helper to seed fake month files.

    Tests call ``seed(asset_class, symbol, year, month)`` to create
    an empty ``.parquet`` file at the canonical path.
    """
    root = tmp_path / "parquet"
    root.mkdir()

    def seed(asset_class: str, symbol: str, year: int, month: int) -> Path:
        dir_ = root / asset_class / symbol / str(year)
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{month:02d}.parquet"
        path.write_bytes(b"")  # empty stub; real coverage reads use pyarrow
        return path

    root.seed = seed  # type: ignore[attr-defined]
    return root
```

> **Note:** The `SymbolOnboardingRun` row fixture is declared in T1 (see `_seed_symbol_onboarding_run()` helper added next to the model). Tests that need a seeded run row import it from `tests.integration.symbol_onboarding.conftest` after T1 lands — no placeholder stub here.

- [ ] **Step 4: Create `watchlists/README.md`:**

````markdown
# Watchlists

Git-tracked YAML manifests that declare MSAI's symbol universe. Each file
is a named watchlist: the filename stem is the watchlist name (e.g.
`core-equities.yaml` → `core-equities`).

## Usage

```bash
msai symbols onboard watchlists/core-equities.yaml
msai symbols status core-equities
```
````

## Manifest schema

```yaml
name: core-equities # kebab-case, matches filename stem
symbols:
  - { symbol: SPY, asset_class: equity, start: 2021-01-01, end: 2025-12-31 }
  - { symbol: AAPL, asset_class: equity, start: trailing_5y } # expands to (today-5y, today-1d)
  - { symbol: ES.n.0, asset_class: futures, start: 2023-01-01, end: 2025-12-31 }
request_live_qualification: false # default; set true when ready to deploy to IB
```

## Rules

- Every symbol has `start` (ISO date or `trailing_Ny` sugar). `end` is optional; defaults to `today - 1d`.
- `asset_class ∈ {equity, futures, fx, option}` (matches registry taxonomy).
- `trailing_Ny` expands client-side via `dateutil.relativedelta`. The server always sees concrete ISO dates.
- Cross-watchlist dedup: if `SPY` appears in two files, the wider window wins; decision is logged.
- Manifest changes take effect only when `msai symbols onboard <file>` is run. No filesystem watcher.

## Storage

1-minute bars are the canonical storage granularity; 5m/10m/30m/1h/1d aggregate for free at backtest time via Nautilus `BarAggregator`. Don't request per-timeframe — one ingest per symbol is enough.

````

- [ ] **Step 5: Create `watchlists/example-core-equities.yaml`:**

```yaml
name: example-core-equities
symbols:
  - { symbol: SPY,    asset_class: equity, start: 2024-01-01, end: 2024-12-31 }
  - { symbol: AAPL,   asset_class: equity, start: trailing_1y }
request_live_qualification: false
````

- [ ] **Step 6: Commit.**

```bash
git add backend/pyproject.toml backend/uv.lock backend/tests/integration/conftest_symbol_onboarding.py watchlists/
git commit -m "feat(symbol-onboarding): T0 pre-flight — deps + fixtures + watchlists scaffold"
```

---

## Phase 1 — Data model (T1)

### Task 1: `SymbolOnboardingRun` table + Alembic migration

**Files:**

- Create: `backend/src/msai/models/symbol_onboarding_run.py`
- Create: `backend/alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py`
- Modify: `backend/src/msai/models/__init__.py` (export `SymbolOnboardingRun`)
- Modify: `backend/tests/integration/test_alembic_migrations.py` (append round-trip test)

- [ ] **Step 1: Write the failing round-trip test.**

Append to `backend/tests/integration/test_alembic_migrations.py`:

```python
@pytest.mark.asyncio
async def test_symbol_onboarding_runs_roundtrip(isolated_postgres_url: str) -> None:
    """Migration c7d8e9f0a1b2 adds the symbol_onboarding_runs table with all
    council-pinned columns. Downgrade removes it cleanly."""
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    # Start from the parent revision (PR #44 head).
    _run_alembic(isolated_postgres_url, "downgrade", "b6c7d8e9f0a1")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            before = await conn.run_sync(lambda s: inspect(s).get_table_names())
        assert "symbol_onboarding_runs" not in before
    finally:
        await engine.dispose()

    _run_alembic_upgrade(isolated_postgres_url, target="c7d8e9f0a1b2")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:

            def _cols(sync_conn: object) -> dict[str, dict[str, object]]:
                return {c["name"]: c for c in inspect(sync_conn).get_columns("symbol_onboarding_runs")}

            cols = await conn.run_sync(_cols)
        assert "id" in cols
        assert "watchlist_name" in cols
        assert "status" in cols
        assert "symbol_states" in cols
        assert "cost_ceiling_usd" in cols
        assert "estimated_cost_usd" in cols
        assert "request_live_qualification" in cols
        assert "job_id_digest" in cols  # idempotency key digest, unique
        assert "created_at" in cols
        assert "updated_at" in cols
        assert "started_at" in cols
        assert "completed_at" in cols
        assert str(cols["symbol_states"]["type"]).upper().startswith("JSONB")
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "downgrade", "b6c7d8e9f0a1")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            after_down = await conn.run_sync(lambda s: inspect(s).get_table_names())
        assert "symbol_onboarding_runs" not in after_down
    finally:
        await engine.dispose()
    # Restore head.
    _run_alembic_upgrade(isolated_postgres_url, target="head")
```

- [ ] **Step 2: Run test to verify failure.**

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_symbol_onboarding_runs_roundtrip -v
```

Expected: FAIL with `alembic upgrade` error — revision `c7d8e9f0a1b2` does not exist yet.

- [ ] **Step 3: Create the model file `backend/src/msai/models/symbol_onboarding_run.py`:**

```python
"""SymbolOnboardingRun — one row per ``POST /api/v1/symbols/onboard`` request.

Owns the run-level status machine (``pending`` → ``in_progress`` →
terminal) plus per-symbol sub-states under ``symbol_states`` JSONB.
Single worker task writes this row; no cross-row coordination.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime  # noqa: TC003 — SQLA Mapped[...] resolves at runtime
from decimal import Decimal  # noqa: TC003 — SQLA Mapped[...] resolves at runtime

from sqlalchemy import CheckConstraint, DateTime, Enum, Numeric, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class SymbolOnboardingRunStatus(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


class SymbolOnboardingRun(Base):
    __tablename__ = "symbol_onboarding_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','completed','completed_with_failures','failed')",
            name="ck_symbol_onboarding_runs_status",
        ),
        CheckConstraint(
            "cost_ceiling_usd IS NULL OR cost_ceiling_usd >= 0",
            name="ck_symbol_onboarding_runs_cost_ceiling_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    watchlist_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        Enum(
            SymbolOnboardingRunStatus,
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SymbolOnboardingRunStatus.PENDING.value,
    )
    # Per-symbol state map:
    # { "<symbol>": {"status": "...", "step": "...", "error": {...}|null, "next_action": str|null,
    #                "asset_class": str, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} }
    symbol_states: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    request_live_qualification: Mapped[bool] = mapped_column(
        nullable=False, default=False
    )
    # Idempotency key — hex-encoded digest of (watchlist_name, sorted_symbols,
    # request_live_qualification). Indexed + UNIQUE so a duplicate POST can
    # look up the existing run in O(log n) and return its id. Stored as text
    # rather than the raw int so it matches the arq ``_job_id`` string passed
    # to ``pool.enqueue_job``.
    job_id_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    cost_ceiling_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Export from `backend/src/msai/models/__init__.py`.**

Read the file first, then add in the appropriate place:

```python
from msai.models.symbol_onboarding_run import SymbolOnboardingRun, SymbolOnboardingRunStatus
```

And append `"SymbolOnboardingRun", "SymbolOnboardingRunStatus"` to the `__all__` list.

- [ ] **Step 5: Create the Alembic migration `backend/alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py`:**

```python
"""add symbol_onboarding_runs table

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-04-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "symbol_onboarding_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("watchlist_name", sa.String(128), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("symbol_states", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "request_live_qualification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("job_id_digest", sa.String(64), nullable=False),
        sa.Column("cost_ceiling_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','completed','completed_with_failures','failed')",
            name="ck_symbol_onboarding_runs_status",
        ),
        sa.CheckConstraint(
            "cost_ceiling_usd IS NULL OR cost_ceiling_usd >= 0",
            name="ck_symbol_onboarding_runs_cost_ceiling_nonneg",
        ),
    )
    op.create_index(
        "ix_symbol_onboarding_runs_created_at",
        "symbol_onboarding_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_symbol_onboarding_runs_job_id_digest",
        "symbol_onboarding_runs",
        ["job_id_digest"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_symbol_onboarding_runs_job_id_digest", table_name="symbol_onboarding_runs"
    )
    op.drop_index(
        "ix_symbol_onboarding_runs_created_at", table_name="symbol_onboarding_runs"
    )
    op.drop_table("symbol_onboarding_runs")
```

- [ ] **Step 6: Run the round-trip test to verify it passes.**

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_symbol_onboarding_runs_roundtrip -v
```

Expected: PASS.

- [ ] **Step 7: Run `ruff check` + `mypy --strict` on the new files.**

```bash
cd backend && uv run ruff check src/msai/models/symbol_onboarding_run.py alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py && uv run mypy --strict src/msai/models/symbol_onboarding_run.py
```

Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add backend/src/msai/models/symbol_onboarding_run.py backend/src/msai/models/__init__.py backend/alembic/versions/c7d8e9f0a1b2_add_symbol_onboarding_runs.py backend/tests/integration/test_alembic_migrations.py
git commit -m "feat(symbol-onboarding): T1 SymbolOnboardingRun model + alembic migration (plural table, updated_at, job_id_digest unique index)"
```

---

## Phase 2 — Schemas (T2)

### Task 2: Pydantic V2 request/response schemas with cross-field invariants

**Files:**

- Create: `backend/src/msai/schemas/symbol_onboarding.py`
- Create: `backend/tests/unit/schemas/test_symbol_onboarding.py`

- [ ] **Step 1: Write the failing schema unit tests.**

Create `backend/tests/unit/schemas/test_symbol_onboarding.py`:

```python
"""Pydantic schema tests for Symbol Onboarding."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from msai.schemas.symbol_onboarding import (
    OnboardRequest,
    OnboardSymbolSpec,
    SymbolStepStatus,
    SymbolStatus,
)


def _mk_spec(symbol: str = "AAPL", **kwargs) -> OnboardSymbolSpec:
    return OnboardSymbolSpec(
        symbol=symbol,
        asset_class=kwargs.pop("asset_class", "equity"),
        start=kwargs.pop("start", date(2024, 1, 1)),
        end=kwargs.pop("end", date(2024, 12, 31)),
        **kwargs,
    )


def test_request_happy_path():
    req = OnboardRequest(watchlist_name="core", symbols=[_mk_spec()])
    assert req.request_live_qualification is False
    assert req.cost_ceiling_usd is None


def test_request_rejects_empty_symbols():
    with pytest.raises(ValidationError, match="symbols"):
        OnboardRequest(watchlist_name="core", symbols=[])


def test_request_rejects_over_100_symbols():
    with pytest.raises(ValidationError, match="100"):
        OnboardRequest(
            watchlist_name="core",
            symbols=[_mk_spec(f"SYM{i}") for i in range(101)],
        )


def test_symbol_spec_rejects_end_before_start():
    with pytest.raises(ValidationError, match="end must be >= start"):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="equity",
            start=date(2024, 12, 31),
            end=date(2024, 1, 1),
        )


def test_symbol_spec_rejects_future_start():
    from datetime import timedelta

    tomorrow = date.today() + timedelta(days=1)
    with pytest.raises(ValidationError, match="start must be <= today"):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="equity",
            start=tomorrow,
            end=tomorrow,
        )


def test_symbol_spec_rejects_unknown_asset_class():
    with pytest.raises(ValidationError):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="etf",  # rejected — registry taxonomy only has equity/futures/fx/option
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )


def test_symbol_spec_rejects_bad_symbol_regex():
    with pytest.raises(ValidationError):
        OnboardSymbolSpec(
            symbol="AAPL$BAD",
            asset_class="equity",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )


def test_cost_ceiling_usd_rejects_negative():
    with pytest.raises(ValidationError):
        OnboardRequest(
            watchlist_name="core",
            symbols=[_mk_spec()],
            cost_ceiling_usd=-1.00,
        )


def test_symbol_status_enum_values():
    # Canonical vocabulary pinned by iter-1 plan-review (`not_started` /
    # `in_progress` / `succeeded` / `failed` — NO `ok` / `already_covered`).
    assert SymbolStatus.NOT_STARTED == "not_started"
    assert SymbolStatus.IN_PROGRESS == "in_progress"
    assert SymbolStatus.SUCCEEDED == "succeeded"
    assert SymbolStatus.FAILED == "failed"


def test_symbol_step_status_enum_values():
    # Canonical vocabulary pinned by iter-1 plan-review.
    assert SymbolStepStatus.PENDING == "pending"
    assert SymbolStepStatus.BOOTSTRAP == "bootstrap"
    assert SymbolStepStatus.INGEST == "ingest"
    assert SymbolStepStatus.COVERAGE == "coverage"
    assert SymbolStepStatus.IB_QUALIFY == "ib_qualify"
    assert SymbolStepStatus.COMPLETED == "completed"
    assert SymbolStepStatus.IB_SKIPPED == "ib_skipped"
    assert SymbolStepStatus.COVERAGE_FAILED == "coverage_failed"


def test_cost_ceiling_usd_accepts_decimal_round_trip():
    # ``cost_ceiling_usd`` is Decimal so Numeric(12,2) storage doesn't lose
    # precision via float round-trip (iter-1 P1 fix).
    from decimal import Decimal

    req = OnboardRequest(
        watchlist_name="core",
        symbols=[_mk_spec()],
        cost_ceiling_usd=Decimal("123.45"),
    )
    assert req.cost_ceiling_usd == Decimal("123.45")
```

- [ ] **Step 2: Run tests to verify failure.**

```bash
cd backend && uv run pytest tests/unit/schemas/test_symbol_onboarding.py -v
```

Expected: all FAIL with `ModuleNotFoundError: msai.schemas.symbol_onboarding`.

- [ ] **Step 3: Create `backend/src/msai/schemas/symbol_onboarding.py`:**

```python
"""Pydantic request/response schemas for Symbol Onboarding.

Contract pins (council-ratified 2026-04-24):
- ``asset_class`` restricted to registry taxonomy: equity | futures | fx | option.
- ``end >= start``, ``start <= today`` enforced via model_validator.
- 100-symbol hard cap at the API layer (Scalability Hawk blocker — any
  bigger is a batch-splitting v2 problem).
- ``cost_ceiling_usd`` is the operator's hard spend stop, not an estimate.
- ``request_live_qualification`` is the request-side flag (distinct from
  the readiness-side ``live_qualified`` boolean).
"""

from __future__ import annotations

import enum
import re
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Matches PR #44 instrument-bootstrap symbol regex.
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_SYMBOLS_PER_BATCH = 100


class SymbolStepStatus(str, enum.Enum):
    # Canonical vocabulary (iter-1 alignment): every value here is also
    # used by the orchestrator (T6), the worker (T7), and the status
    # endpoint (T9). Do NOT introduce parallel vocabularies.
    PENDING = "pending"
    BOOTSTRAP = "bootstrap"
    INGEST = "ingest"
    COVERAGE = "coverage"
    IB_QUALIFY = "ib_qualify"
    COMPLETED = "completed"
    IB_SKIPPED = "ib_skipped"
    COVERAGE_FAILED = "coverage_failed"


class SymbolStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


AssetClass = Literal["equity", "futures", "fx", "option"]


class OnboardSymbolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass
    start: date
    end: date

    @field_validator("symbol")
    @classmethod
    def _symbol_regex(cls, v: str) -> str:
        if not _SYMBOL_PATTERN.match(v):
            raise ValueError(f"symbol {v!r} does not match {_SYMBOL_PATTERN.pattern!r}")
        return v

    @model_validator(mode="after")
    def _dates_coherent(self) -> OnboardSymbolSpec:
        if self.end < self.start:
            raise ValueError(f"end must be >= start (got start={self.start}, end={self.end})")
        if self.start > date.today():
            raise ValueError(f"start must be <= today (got {self.start})")
        return self


class OnboardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watchlist_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9\-]+$")
    symbols: list[OnboardSymbolSpec] = Field(
        min_length=1, max_length=_MAX_SYMBOLS_PER_BATCH
    )
    request_live_qualification: bool = False
    # Decimal (not float) so round-tripping through ``Numeric(12, 2)`` in
    # Postgres can't silently lose precision (iter-1 P1 fix).
    cost_ceiling_usd: Decimal | None = Field(
        default=None, max_digits=12, decimal_places=2, ge=0
    )


class SymbolStateRow(BaseModel):
    """Per-symbol progress state as it appears in ``status`` responses."""

    symbol: str
    asset_class: AssetClass
    start: date
    end: date
    status: SymbolStatus
    step: SymbolStepStatus
    error: dict | None = None  # {"code": str, "message": str}
    next_action: str | None = None


class OnboardProgress(BaseModel):
    total: int
    succeeded: int
    failed: int
    in_progress: int
    not_started: int


class OnboardResponse(BaseModel):
    """202 response body for ``POST /onboard``."""

    run_id: UUID
    watchlist_name: str
    status: RunStatus


class StatusResponse(BaseModel):
    run_id: UUID
    watchlist_name: str
    status: RunStatus
    progress: OnboardProgress
    per_symbol: list[SymbolStateRow]
    estimated_cost_usd: Decimal | None
    actual_cost_usd: Decimal | None


class DryRunResponse(BaseModel):
    watchlist_name: str
    dry_run: Literal[True] = True
    estimated_cost_usd: Decimal
    estimate_basis: str
    estimate_confidence: Literal["high", "medium", "low"]
    symbol_count: int
    breakdown: list[dict]  # [{"symbol": str, "dataset": str, "usd": float}, ...]


class ReadinessResponse(BaseModel):
    """Window-scoped per-instrument readiness (pin #3 amendment)."""

    instrument_uid: UUID
    registered: bool
    provider: str
    # Window-scoped: true only when a full window was provided AND coverage is complete.
    backtest_data_available: bool | None
    coverage_status: Literal["full", "gapped", "none"] | None
    covered_range: str | None  # e.g. "2023-01-01 → 2024-12-31"
    missing_ranges: list[dict] = []  # [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}]
    live_qualified: bool
    coverage_summary: str | None = None  # human-friendly when no window in scope
```

- [ ] **Step 4: Run tests.**

```bash
cd backend && uv run pytest tests/unit/schemas/test_symbol_onboarding.py -v
```

Expected: all PASS (10 tests).

- [ ] **Step 5: Ruff + mypy.**

```bash
cd backend && uv run ruff check src/msai/schemas/symbol_onboarding.py tests/unit/schemas/test_symbol_onboarding.py && uv run mypy --strict src/msai/schemas/symbol_onboarding.py
```

Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add backend/src/msai/schemas/symbol_onboarding.py backend/tests/unit/schemas/test_symbol_onboarding.py
git commit -m "feat(symbol-onboarding): T2 Pydantic schemas with cross-field invariants"
```

---

### Task 3: Manifest parser (`services/symbol_onboarding/manifest.py`)

**Files:**

- Create: `backend/src/msai/services/symbol_onboarding/__init__.py`
- Create: `backend/src/msai/services/symbol_onboarding/manifest.py`
- Create: `backend/tests/unit/services/symbol_onboarding/__init__.py`
- Create: `backend/tests/unit/services/symbol_onboarding/test_manifest.py`

**Dependencies:** T0 (pyyaml dep), T2 (`OnboardSymbolSpec` reuse).

**Writes:** `backend/src/msai/services/symbol_onboarding/__init__.py`, `backend/src/msai/services/symbol_onboarding/manifest.py`, `backend/tests/unit/services/symbol_onboarding/__init__.py`, `backend/tests/unit/services/symbol_onboarding/test_manifest.py`.

Contract:

- Input: a Path to a YAML file under `watchlists/`.
- Output: `ParsedManifest(watchlist_name: str, symbols: list[OnboardSymbolSpec])`.
- Supported date sugar: `trailing_5y` (expanded via `dateutil.relativedelta(years=-5)`; default `end=today-1d` per research finding #6 — dodges Databento nightly publication window).
- Cross-watchlist dedup (for the multi-manifest CLI path): if the same `(symbol, asset_class)` appears twice with different windows, **wider window wins** and a `manifest_dedup_widened` event is logged.

- [ ] **Step 1: Failing test.**

```python
# backend/tests/unit/services/symbol_onboarding/test_manifest.py
from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
from dateutil.relativedelta import relativedelta

from msai.services.symbol_onboarding.manifest import (
    ManifestParseError,
    ParsedManifest,
    parse_manifest_file,
    merge_manifests,
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(body))
    return p


def test_parse_manifest_explicit_window(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "core.yaml",
        """
        watchlist_name: core-equities
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2023-01-01
            end: 2024-12-31
        """,
    )
    result = parse_manifest_file(f)
    assert isinstance(result, ParsedManifest)
    assert result.watchlist_name == "core-equities"
    assert len(result.symbols) == 1
    spec = result.symbols[0]
    assert spec.symbol == "SPY"
    assert spec.start == date(2023, 1, 1)
    assert spec.end == date(2024, 12, 31)


def test_parse_manifest_trailing_5y_sugar_uses_yesterday(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "trailing.yaml",
        """
        watchlist_name: rolling
        symbols:
          - symbol: AAPL
            asset_class: equity
            window: trailing_5y
        """,
    )
    result = parse_manifest_file(f, today=date(2026, 4, 24))
    spec = result.symbols[0]
    assert spec.end == date(2026, 4, 23)  # today - 1d
    assert spec.start == date(2021, 4, 23)  # end - 5y


def test_parse_manifest_rejects_unknown_key(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "bad.yaml",
        """
        watchlist_name: x
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2024-01-01
            end: 2024-12-31
            bogus_field: 1
        """,
    )
    with pytest.raises(ManifestParseError, match="bogus_field"):
        parse_manifest_file(f)


def test_parse_manifest_rejects_trailing_5y_with_explicit_window(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "conflict.yaml",
        """
        watchlist_name: x
        symbols:
          - symbol: SPY
            asset_class: equity
            window: trailing_5y
            start: 2024-01-01
            end: 2024-12-31
        """,
    )
    with pytest.raises(ManifestParseError, match="window.*cannot.*start.*end"):
        parse_manifest_file(f)


def test_parse_manifest_watchlist_name_slug_rule(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "bad_name.yaml",
        """
        watchlist_name: Core Equities!
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2024-01-01
            end: 2024-12-31
        """,
    )
    with pytest.raises(ManifestParseError, match="watchlist_name"):
        parse_manifest_file(f)


def test_merge_manifests_widens_window_for_duplicate_symbol() -> None:
    m1 = ParsedManifest(
        watchlist_name="a",
        symbols=[_spec("SPY", "equity", date(2024, 1, 1), date(2024, 6, 30))],
    )
    m2 = ParsedManifest(
        watchlist_name="b",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    merged = merge_manifests([m1, m2], merged_name="combined")
    assert len(merged.symbols) == 1
    assert merged.symbols[0].start == date(2023, 1, 1)
    assert merged.symbols[0].end == date(2024, 12, 31)


def test_merge_manifests_keeps_distinct_asset_classes_separate() -> None:
    m = ParsedManifest(
        watchlist_name="m",
        symbols=[
            _spec("ES", "equity", date(2024, 1, 1), date(2024, 12, 31)),
            _spec("ES", "futures", date(2024, 1, 1), date(2024, 12, 31)),
        ],
    )
    merged = merge_manifests([m], merged_name="m")
    assert len(merged.symbols) == 2


def _spec(symbol, asset_class, start, end):
    from msai.schemas.symbol_onboarding import OnboardSymbolSpec
    return OnboardSymbolSpec(
        symbol=symbol, asset_class=asset_class, start=start, end=end
    )
```

- [ ] **Step 2: Run test — expect fail.**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_manifest.py -v
```

Expected: FAIL `ModuleNotFoundError: msai.services.symbol_onboarding.manifest`.

- [ ] **Step 3: Implementation.**

```python
# backend/src/msai/services/symbol_onboarding/__init__.py
"""Symbol onboarding services — manifest parsing, cost estimation, coverage, orchestration.

Also exports ``normalize_asset_class_for_ingest`` — a single translation
seam from the user-facing/registry taxonomy (``equity | futures | fx |
option``; used by ``OnboardSymbolSpec.asset_class``) to the ingest /
Parquet storage taxonomy (``stocks | futures | forex | option``; used
by ``DataIngestionService.ingest_historical`` and the Parquet directory
layout ``{DATA_ROOT}/parquet/{asset_class}/{symbol}/...``).

Keep this in ONE place. Callers that cross the boundary (orchestrator,
cost estimator, coverage scanner) import this helper; they do not
hard-code either vocabulary.
"""

from __future__ import annotations

__all__ = ["normalize_asset_class_for_ingest"]


_REGISTRY_TO_INGEST: dict[str, str] = {
    "equity": "stocks",
    "futures": "futures",
    "fx": "forex",
    "option": "option",
}


def normalize_asset_class_for_ingest(registry_asset_class: str) -> str:
    """Translate the user-facing ``asset_class`` to the ingest taxonomy.

    Raises ``ValueError`` on unknown inputs — fail-loud so an unmapped
    asset class doesn't silently route to the wrong provider.
    """
    try:
        return _REGISTRY_TO_INGEST[registry_asset_class]
    except KeyError as exc:
        raise ValueError(
            f"Unknown registry asset_class {registry_asset_class!r}; "
            f"expected one of {sorted(_REGISTRY_TO_INGEST)}"
        ) from exc
```

```python
# backend/src/msai/services/symbol_onboarding/manifest.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import structlog
import yaml
from dateutil.relativedelta import relativedelta

from msai.schemas.symbol_onboarding import OnboardSymbolSpec

log = structlog.get_logger(__name__)

__all__ = [
    "ManifestParseError",
    "ParsedManifest",
    "parse_manifest_file",
    "merge_manifests",
]

_WATCHLIST_NAME_RE = re.compile(r"^[a-z0-9\-]+$")
_ALLOWED_SYMBOL_KEYS = frozenset({"symbol", "asset_class", "start", "end", "window"})
_ALLOWED_TOP_KEYS = frozenset({"watchlist_name", "symbols"})
_TRAILING_5Y_WINDOW = "trailing_5y"


class ManifestParseError(ValueError):
    """Raised when a watchlist YAML is syntactically or semantically invalid."""


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    watchlist_name: str
    symbols: list[OnboardSymbolSpec]


def parse_manifest_file(path: Path, *, today: date | None = None) -> ParsedManifest:
    if not path.is_file():
        raise ManifestParseError(f"Manifest file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)  # NEVER yaml.load — arbitrary object construction risk.
    if not isinstance(raw, dict):
        raise ManifestParseError("Manifest root must be a mapping")

    unknown_top = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown_top:
        raise ManifestParseError(f"Unknown top-level keys: {sorted(unknown_top)}")

    name = raw.get("watchlist_name")
    if not isinstance(name, str) or not _WATCHLIST_NAME_RE.match(name):
        raise ManifestParseError(
            f"watchlist_name must match {_WATCHLIST_NAME_RE.pattern}; got {name!r}"
        )

    symbols_raw = raw.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise ManifestParseError("symbols: must be a non-empty list")

    resolved = [_parse_symbol_entry(entry, today=today or date.today()) for entry in symbols_raw]
    return ParsedManifest(watchlist_name=name, symbols=resolved)


def _parse_symbol_entry(entry: Any, *, today: date) -> OnboardSymbolSpec:
    if not isinstance(entry, dict):
        raise ManifestParseError(f"symbols[*] must be a mapping; got {type(entry).__name__}")

    unknown = set(entry.keys()) - _ALLOWED_SYMBOL_KEYS
    if unknown:
        raise ManifestParseError(f"Unknown symbol-entry keys: {sorted(unknown)}")

    window_sugar = entry.get("window")
    if window_sugar is not None and ("start" in entry or "end" in entry):
        raise ManifestParseError(
            "window: sugar cannot be combined with explicit start/end"
        )

    if window_sugar is not None:
        if window_sugar != _TRAILING_5Y_WINDOW:
            raise ManifestParseError(f"Unsupported window sugar: {window_sugar!r}")
        end = today - relativedelta(days=1)
        start = end - relativedelta(years=5)
    else:
        start = _coerce_date(entry.get("start"), "start")
        end = _coerce_date(entry.get("end"), "end")

    return OnboardSymbolSpec(
        symbol=str(entry["symbol"]).strip(),
        asset_class=str(entry["asset_class"]).strip(),
        start=start,
        end=end,
    )


def _coerce_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ManifestParseError(f"{field}: not ISO 8601 date: {value!r}") from exc
    raise ManifestParseError(f"{field}: required (date or YYYY-MM-DD)")


def merge_manifests(
    manifests: list[ParsedManifest], *, merged_name: str
) -> ParsedManifest:
    """Combine multiple manifests into one; wider window wins on duplicate keys."""

    pool: dict[tuple[str, str], OnboardSymbolSpec] = {}
    for m in manifests:
        for spec in m.symbols:
            key = (spec.symbol, spec.asset_class)
            existing = pool.get(key)
            if existing is None:
                pool[key] = spec
                continue
            widened_start = min(existing.start, spec.start)
            widened_end = max(existing.end, spec.end)
            if (widened_start, widened_end) != (existing.start, existing.end):
                log.info(
                    "manifest_dedup_widened",
                    symbol=spec.symbol,
                    asset_class=spec.asset_class,
                    prior_window=[existing.start.isoformat(), existing.end.isoformat()],
                    merged_window=[widened_start.isoformat(), widened_end.isoformat()],
                )
            pool[key] = OnboardSymbolSpec(
                symbol=spec.symbol,
                asset_class=spec.asset_class,
                start=widened_start,
                end=widened_end,
            )
    return ParsedManifest(
        watchlist_name=merged_name,
        symbols=sorted(
            pool.values(),
            key=lambda s: (s.asset_class, s.symbol),
        ),
    )
```

- [ ] **Step 4: Run tests.**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_manifest.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Ruff + mypy.**

```bash
cd backend && uv run ruff check src/msai/services/symbol_onboarding/ tests/unit/services/symbol_onboarding/ && uv run mypy --strict src/msai/services/symbol_onboarding/manifest.py
```

Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add backend/src/msai/services/symbol_onboarding/__init__.py backend/src/msai/services/symbol_onboarding/manifest.py backend/tests/unit/services/symbol_onboarding/
git commit -m "feat(symbol-onboarding): T3 manifest parser with trailing_5y sugar + cross-watchlist dedup"
```

---

### Task 4: Cost estimator (`services/symbol_onboarding/cost_estimator.py`)

**Files:**

- Create: `backend/src/msai/services/symbol_onboarding/cost_estimator.py`
- Create: `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py`

**Dependencies:** T0 (databento already present), T3 (consumes `ParsedManifest`).

**Writes:** `backend/src/msai/services/symbol_onboarding/cost_estimator.py`, `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py`.

Contract:

- `estimate_cost(manifest: ParsedManifest, *, client: DatabentoHistoricalClient) -> CostEstimate`.
- Uses `client.metadata.get_cost(dataset, symbols, schema, stype_in, start, end)` — one call per dataset bucket (not per symbol) to minimize API round-trips.
- Returns `CostEstimate(total_usd, symbol_count, breakdown: list[CostLine], confidence: Literal["high","medium","low"], basis: str)`.
- Confidence rule (research finding #1): `high` iff every window ends strictly before `today-1d` AND no continuous-futures glob (`X.n.0`, `X.c.0`) AND no ambiguous bare tickers → bytes agree byte-for-byte with the eventual `timeseries.get_range` call. Else `medium`. `low` is reserved for future heuristics (default: we do not currently emit `low`).
- Failure: if `get_cost` raises (auth / outage / unentitled dataset), return `CostEstimate(total_usd=0.0, …, confidence="low", basis="unavailable: <reason>")` — caller surfaces this on the dry-run endpoint but does not fail the onboarding run.

- [ ] **Step 1: Failing test.**

```python
# backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.schemas.symbol_onboarding import OnboardSymbolSpec
from msai.services.symbol_onboarding.cost_estimator import (
    CostEstimate,
    estimate_cost,
)
from msai.services.symbol_onboarding.manifest import ParsedManifest


def _spec(sym, ac, start, end):
    return OnboardSymbolSpec(symbol=sym, asset_class=ac, start=start, end=end)


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.metadata = MagicMock()
    client.metadata.get_cost = MagicMock(return_value=0.42)
    return client


@pytest.mark.asyncio
async def test_estimate_returns_high_confidence_on_fully_historical_equity(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(
        manifest, client=fake_client, today=date(2026, 4, 24)
    )
    assert isinstance(result, CostEstimate)
    assert result.total_usd == pytest.approx(0.42)
    assert result.confidence == "high"
    assert len(result.breakdown) == 1
    assert result.breakdown[0].symbol == "SPY"


@pytest.mark.asyncio
async def test_estimate_confidence_medium_when_end_touches_yesterday(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2024, 1, 1), date(2026, 4, 23))],
    )
    result = await estimate_cost(
        manifest, client=fake_client, today=date(2026, 4, 24)
    )
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_estimate_confidence_medium_on_continuous_futures(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("ES.n.0", "futures", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(
        manifest, client=fake_client, today=date(2026, 4, 24)
    )
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_estimate_batches_symbols_per_dataset(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[
            _spec("SPY", "equity", date(2024, 1, 1), date(2024, 12, 31)),
            _spec("AAPL", "equity", date(2024, 1, 1), date(2024, 12, 31)),
        ],
    )
    await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    # One get_cost call per (dataset, window); two equity symbols on the same
    # dataset + window collapse into a single call.
    assert fake_client.metadata.get_cost.call_count == 1


@pytest.mark.asyncio
async def test_estimate_returns_low_confidence_on_upstream_failure(fake_client):
    fake_client.metadata.get_cost.side_effect = RuntimeError("auth failed")
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(
        manifest, client=fake_client, today=date(2026, 4, 24)
    )
    assert result.total_usd == 0.0
    assert result.confidence == "low"
    assert "unavailable" in result.basis.lower()
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/services/symbol_onboarding/cost_estimator.py
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Protocol

import structlog

from msai.schemas.symbol_onboarding import OnboardSymbolSpec
from msai.services.symbol_onboarding.manifest import ParsedManifest

log = structlog.get_logger(__name__)

__all__ = ["CostEstimate", "CostLine", "estimate_cost"]

_CONTINUOUS_FUTURES_RE = re.compile(r"^[A-Z]+\.(?:n|c)\.\d+$")

_ASSET_TO_DATASET: dict[str, str] = {
    # Keyed by registry/user-facing ``asset_class`` (T2 schema vocabulary:
    # ``equity | futures | fx | option``). Matches the PR #44 bootstrap
    # default dataset per class. Unknown asset classes are logged +
    # skipped; fx/option have no Databento v1 entitlement yet.
    "equity": "XNAS.ITCH",
    "futures": "GLBX.MDP3",
}


class _DatabentoMetadataProto(Protocol):
    def get_cost(
        self,
        *,
        dataset: str,
        symbols: list[str],
        schema: str,
        stype_in: str,
        start: str,
        end: str,
    ) -> float: ...


class _DatabentoClientProto(Protocol):
    metadata: _DatabentoMetadataProto


@dataclass(frozen=True, slots=True)
class CostLine:
    symbol: str
    asset_class: str
    dataset: str
    usd: float


@dataclass(frozen=True, slots=True)
class CostEstimate:
    total_usd: float
    symbol_count: int
    breakdown: list[CostLine]
    confidence: Literal["high", "medium", "low"]
    basis: str


async def estimate_cost(
    manifest: ParsedManifest,
    *,
    client: _DatabentoClientProto,
    today: date | None = None,
) -> CostEstimate:
    today = today or date.today()

    # Bucket symbols by (dataset, start, end) so we make the minimum number
    # of vendor round-trips. get_cost is a single bounded number per bucket.
    buckets: dict[tuple[str, date, date], list[OnboardSymbolSpec]] = defaultdict(list)
    for spec in manifest.symbols:
        dataset = _ASSET_TO_DATASET.get(spec.asset_class)
        if dataset is None:
            log.warning(
                "cost_estimator_unmapped_asset_class",
                asset_class=spec.asset_class,
                symbol=spec.symbol,
            )
            continue
        buckets[(dataset, spec.start, spec.end)].append(spec)

    breakdown: list[CostLine] = []
    total = 0.0
    upstream_failure: str | None = None

    for (dataset, start, end), specs in buckets.items():
        symbols = [s.symbol for s in specs]
        try:
            bucket_usd = await asyncio.to_thread(
                client.metadata.get_cost,
                dataset=dataset,
                symbols=symbols,
                schema="ohlcv-1m",
                stype_in="raw_symbol",
                start=start.isoformat(),
                end=end.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — upstream SDK raises heterogeneous types
            log.warning(
                "cost_estimator_upstream_error",
                dataset=dataset,
                symbols=symbols,
                error=repr(exc),
            )
            upstream_failure = f"unavailable: {type(exc).__name__}"
            continue

        # Distribute the bucket cost equally across its symbols for the
        # breakdown — the SDK returns a single number per call, not per symbol.
        per_symbol = float(bucket_usd) / max(len(specs), 1)
        for spec in specs:
            breakdown.append(
                CostLine(
                    symbol=spec.symbol,
                    asset_class=spec.asset_class,
                    dataset=dataset,
                    usd=per_symbol,
                )
            )
        total += float(bucket_usd)

    if upstream_failure is not None and not breakdown:
        return CostEstimate(
            total_usd=0.0,
            symbol_count=len(manifest.symbols),
            breakdown=[],
            confidence="low",
            basis=upstream_failure,
        )

    confidence: Literal["high", "medium", "low"] = _classify_confidence(
        manifest, today=today
    )
    basis = (
        "databento.metadata.get_cost (1m OHLCV)"
        if upstream_failure is None
        else f"partial: {upstream_failure}"
    )

    return CostEstimate(
        total_usd=total,
        symbol_count=len(manifest.symbols),
        breakdown=breakdown,
        confidence=confidence,
        basis=basis,
    )


def _classify_confidence(
    manifest: ParsedManifest, *, today: date
) -> Literal["high", "medium", "low"]:
    cutoff = today - timedelta(days=2)  # strictly before yesterday
    for spec in manifest.symbols:
        if spec.end >= cutoff:
            return "medium"
        if _CONTINUOUS_FUTURES_RE.match(spec.symbol):
            return "medium"
    return "high"
```

- [ ] **Step 3: Run tests.**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_cost_estimator.py -v
```

Expected: 5 PASS.

- [ ] **Step 4: Ruff + mypy + commit.**

```bash
cd backend && uv run ruff check src/msai/services/symbol_onboarding/cost_estimator.py tests/unit/services/symbol_onboarding/test_cost_estimator.py && uv run mypy --strict src/msai/services/symbol_onboarding/cost_estimator.py
git add backend/src/msai/services/symbol_onboarding/cost_estimator.py backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py
git commit -m "feat(symbol-onboarding): T4 Databento cost estimator with declared confidence classification"
```

---

### Task 5: Coverage scanner (`services/symbol_onboarding/coverage.py`)

**Files:**

- Create: `backend/src/msai/services/symbol_onboarding/coverage.py`
- Create: `backend/tests/unit/services/symbol_onboarding/test_coverage.py`

**Dependencies:** T0 (test fixture `tmp_parquet_root`), T3 (window arithmetic).

**Writes:** `backend/src/msai/services/symbol_onboarding/coverage.py`, `backend/tests/unit/services/symbol_onboarding/test_coverage.py`.

Contract:

- `compute_coverage(asset_class, symbol, start, end, *, data_root) -> CoverageReport`.
- Pure filesystem scan — no DB, no Postgres, no DuckDB. Reads Parquet partition layout `{data_root}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`.
- **`asset_class` here is the INGEST / Parquet-storage taxonomy** (`stocks | futures | forex | option`), not the registry/user-facing taxonomy (`equity | futures | fx | option`). Callers that hold the user-facing value must translate via `normalize_asset_class_for_ingest(...)` from T3's `__init__.py` before invoking this helper. Reason: the directory layout pre-dates the registry taxonomy (`/parquet/stocks/SPY/...`), and the scanner is deliberately unaware of registry semantics.
- Returns `CoverageReport(status: Literal["full","gapped","none"], covered_range: str | None, missing_ranges: list[tuple[date, date]])`.
- Edge-gap tolerance: mirrors PR #40's `verify_catalog_coverage` — within 7 trading days of `today` is treated as covered even if the month file is absent (vendor publication lag).
- Month partitions are treated as atomic units: a present `{YYYY}/{MM}.parquet` implies full coverage for that month's date range intersected with [start, end]. (Intra-month gap detection is out of scope per PRD non-goal "sub-month coverage granularity".)

- [ ] **Step 1: Failing test.**

```python
# backend/tests/unit/services/symbol_onboarding/test_coverage.py
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from msai.services.symbol_onboarding.coverage import (
    CoverageReport,
    compute_coverage,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # empty file — existence is what matters


@pytest.mark.asyncio
async def test_coverage_none_when_directory_missing(tmp_path: Path) -> None:
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "none"
    assert report.covered_range is None
    assert len(report.missing_ranges) == 1


@pytest.mark.asyncio
async def test_coverage_full_when_every_month_present(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    for month in range(1, 13):
        _touch(base / f"{month:02d}.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "full"
    assert report.missing_ranges == []


@pytest.mark.asyncio
async def test_coverage_gapped_reports_missing_months(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    for month in [1, 2, 3, 7, 8, 9, 10, 11, 12]:
        _touch(base / f"{month:02d}.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
    missing_start, missing_end = report.missing_ranges[0]
    assert missing_start == date(2024, 4, 1)
    assert missing_end == date(2024, 6, 30)


@pytest.mark.asyncio
async def test_coverage_trailing_edge_tolerance_within_7_days(tmp_path: Path) -> None:
    today = date(2026, 4, 24)
    # All months present except April 2026 (partial / vendor lag)
    base = tmp_path / "parquet" / "stocks" / "SPY"
    _touch(base / "2026" / "01.parquet")
    _touch(base / "2026" / "02.parquet")
    _touch(base / "2026" / "03.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2026, 1, 1),
        end=date(2026, 4, 23),  # within 7 days of today
        data_root=tmp_path,
        today=today,
    )
    assert report.status == "full"
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/services/symbol_onboarding/coverage.py
from __future__ import annotations

import asyncio
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

__all__ = ["CoverageReport", "compute_coverage"]

_TRAILING_EDGE_TOLERANCE_DAYS = 7


@dataclass(frozen=True, slots=True)
class CoverageReport:
    status: Literal["full", "gapped", "none"]
    covered_range: str | None
    missing_ranges: list[tuple[date, date]]


async def compute_coverage(
    *,
    asset_class: str,
    symbol: str,
    start: date,
    end: date,
    data_root: Path,
    today: date | None = None,
) -> CoverageReport:
    today = today or date.today()
    scan = await asyncio.to_thread(
        _scan_filesystem, data_root, asset_class, symbol, start, end
    )
    required_months = _months_in_range(start, end)
    present_months = scan.present_months

    if not present_months:
        return CoverageReport(
            status="none",
            covered_range=None,
            missing_ranges=[(start, end)],
        )

    missing = [m for m in required_months if m not in present_months]
    missing = _apply_trailing_edge_tolerance(missing, today=today)

    if not missing:
        return CoverageReport(
            status="full",
            covered_range=f"{start.isoformat()} → {end.isoformat()}",
            missing_ranges=[],
        )

    missing_ranges = _collapse_missing(missing, start=start, end=end)
    return CoverageReport(
        status="gapped",
        covered_range=_derive_covered_range(present_months, start=start, end=end),
        missing_ranges=missing_ranges,
    )


@dataclass(frozen=True, slots=True)
class _ScanResult:
    present_months: set[tuple[int, int]]


def _scan_filesystem(
    data_root: Path, asset_class: str, symbol: str, start: date, end: date
) -> _ScanResult:
    root = data_root / "parquet" / asset_class / symbol
    if not root.is_dir():
        return _ScanResult(present_months=set())
    present: set[tuple[int, int]] = set()
    for year_dir in root.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for month_file in year_dir.iterdir():
            stem = month_file.stem
            if not stem.isdigit():
                continue
            month = int(stem)
            if 1 <= month <= 12 and month_file.suffix == ".parquet":
                present.add((year, month))
    return _ScanResult(present_months=present)


def _months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def _apply_trailing_edge_tolerance(
    missing: list[tuple[int, int]], *, today: date
) -> list[tuple[int, int]]:
    cutoff = today - timedelta(days=_TRAILING_EDGE_TOLERANCE_DAYS)
    # A month is tolerated if its first day is within 7 days of today.
    return [(y, m) for (y, m) in missing if date(y, m, 1) <= cutoff]


def _collapse_missing(
    missing: list[tuple[int, int]], *, start: date, end: date
) -> list[tuple[date, date]]:
    if not missing:
        return []
    missing = sorted(missing)
    ranges: list[tuple[date, date]] = []
    run_start = missing[0]
    prev = run_start
    for current in missing[1:]:
        if _is_consecutive(prev, current):
            prev = current
            continue
        ranges.append(_run_to_date_range(run_start, prev, start=start, end=end))
        run_start = current
        prev = current
    ranges.append(_run_to_date_range(run_start, prev, start=start, end=end))
    return ranges


def _is_consecutive(a: tuple[int, int], b: tuple[int, int]) -> bool:
    y, m = a
    m += 1
    if m == 13:
        m = 1
        y += 1
    return (y, m) == b


def _run_to_date_range(
    run_start: tuple[int, int],
    run_end: tuple[int, int],
    *,
    start: date,
    end: date,
) -> tuple[date, date]:
    y0, m0 = run_start
    y1, m1 = run_end
    first = max(date(y0, m0, 1), start)
    last_day = monthrange(y1, m1)[1]
    last = min(date(y1, m1, last_day), end)
    return (first, last)


def _derive_covered_range(
    present_months: set[tuple[int, int]], *, start: date, end: date
) -> str:
    present = sorted(present_months)
    if not present:
        return ""
    y0, m0 = present[0]
    y1, m1 = present[-1]
    first = max(date(y0, m0, 1), start)
    last_day = monthrange(y1, m1)[1]
    last = min(date(y1, m1, last_day), end)
    return f"{first.isoformat()} → {last.isoformat()}"
```

- [ ] **Step 3: Run tests.**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py -v
```

Expected: 4 PASS.

- [ ] **Step 4: Ruff + mypy + commit.**

```bash
cd backend && uv run ruff check src/msai/services/symbol_onboarding/coverage.py tests/unit/services/symbol_onboarding/test_coverage.py && uv run mypy --strict src/msai/services/symbol_onboarding/coverage.py
git add backend/src/msai/services/symbol_onboarding/coverage.py backend/tests/unit/services/symbol_onboarding/test_coverage.py
git commit -m "feat(symbol-onboarding): T5 on-the-fly Parquet coverage scanner with 7-day trailing-edge tolerance"
```

---

### Task 6a: Extract reusable `ingest_symbols(...)` helper from `data_ingestion.py`

**Files:**

- Modify: `backend/src/msai/services/data_ingestion.py` — extract helper out of the existing `run_ingest` arq-wrapper code path (keeping `run_ingest` a 3-line shim that still returns `None`).
- Create: `backend/tests/unit/services/test_data_ingestion_ingest_symbols.py`

**Dependencies:** T0.

**Writes:** `backend/src/msai/services/data_ingestion.py`, `backend/tests/unit/services/test_data_ingestion_ingest_symbols.py`.

**Why this task exists (council verdict Option A — iter-1 P0 fix).** The orchestrator MUST NOT enqueue a child arq job onto the ingest queue and then `await job.result()`. Both `run_symbol_onboarding` and `run_ingest` live under the same `IngestWorkerSettings` (`max_jobs=1`), so the parent blocks its own consumer slot while waiting for a child that can never start — classic self-deadlock. The fix is to call a reusable service-layer helper directly in-process, bypassing arq entirely for that inner ingest step.

This mirrors the project's existing pattern: `workers/settings.py:47` `run_backtest` is a thin arq wrapper over `services.backtests.backtest_runner.run_backtest_job` — same shape: arq-exposed verb + reusable async helper under it.

Contract:

- Extract `async def ingest_symbols(asset_class_ingest: str, symbols: list[str], start: str, end: str, *, provider: str = "auto", dataset: str | None = None, schema: str | None = None) -> IngestResult` in `data_ingestion.py`.
- `IngestResult` is a new `@dataclass(frozen=True, slots=True)` with fields `bars_written: int`, `symbols_covered: list[str]`, `empty_symbols: list[str]`, `coverage_status: Literal["full", "gapped", "none"]`. This is what the orchestrator branches on instead of `await job.result()` returning a dict-or-None. `empty_symbols` is the orchestrator's hook for distinguishing "ingest succeeded but returned 0 bars" from "ingest raised" — both require different downstream states.
- **Shape mapping (iter-2 P1-A fix).** `DataIngestionService.ingest_historical()` at `services/data_ingestion.py:142-153` returns a dict shaped `{"ingested": {raw_symbol: {"bars": int, ...}}, "empty_symbols": list[str], ...}` — it does NOT expose flat `bars_written` or `symbols_covered` keys. The helper MUST compute these locally from the `ingested` dict; lazily reading `stats.get("bars_written", 0)` silently returns 0 forever. The test MUST mock the real return shape, not the invented one.
- `asset_class_ingest` is the **ingest taxonomy** string (`stocks | futures | forex | option`). Callers that hold the registry vocabulary translate via `normalize_asset_class_for_ingest(...)` from T3.
- `run_ingest` (the existing arq-exposed function at `data_ingestion.py:329`) stays a 3-line shim: instantiates `DataIngestionService`, calls `ingest_symbols(...)`, logs + discards the result, returns `None` (preserves arq wire-format — no plan-review regression on the existing ingest callers).
- The new helper IS what the orchestrator imports. The arq wrapper is NOT.

- [ ] **Step 1: Write the failing test.**

```python
# backend/tests/unit/services/test_data_ingestion_ingest_symbols.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.data_ingestion import IngestResult, ingest_symbols


@pytest.mark.asyncio
async def test_ingest_symbols_returns_structured_result(monkeypatch):
    """iter-2 P1-A fix: mock with the REAL ``ingest_historical`` return shape
    (`ingested` dict keyed by raw_symbol + `empty_symbols` list).

    ``DataIngestionService.ingest_historical()`` returns:
        {"ingested": {"SPY": {"bars": 418, "range": {...}, ...}},
         "empty_symbols": [], "asset_class": "stocks", ...}

    — NOT ``{"bars_written": ..., "symbols_covered": [...]}``. The helper MUST
    derive those from the ``ingested`` dict locally.
    """
    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "asset_class": "stocks",
            "provider": "databento",
            "dataset": "XNAS.ITCH",
            "schema": "ohlcv-1m",
            "requested_symbols": ["SPY"],
            "symbols": ["SPY"],
            "start": "2024-01-01",
            "end": "2024-12-31",
            "ingested": {
                "SPY": {
                    "requested_symbol": "SPY",
                    "raw_symbol": "SPY",
                    "instrument_id": "SPY.XNAS",
                    "bars": 418,
                    "first_timestamp": "2024-01-02T14:31:00Z",
                    "last_timestamp": "2024-12-31T21:00:00Z",
                    "duplicates_dropped": 0,
                }
            },
            "empty_symbols": [],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await ingest_symbols(
        "stocks", ["SPY"], "2024-01-01", "2024-12-31"
    )
    assert isinstance(result, IngestResult)
    assert result.bars_written == 418
    assert result.symbols_covered == ["SPY"]
    assert result.empty_symbols == []


@pytest.mark.asyncio
async def test_ingest_symbols_carries_empty_symbols_when_zero_bars(monkeypatch):
    """iter-2 P1-A fix: when a symbol comes back empty, the helper must
    surface it in ``empty_symbols`` so the orchestrator can branch on
    "ingest succeeded with 0 bars for SPY" vs "ingest covered SPY with N bars"."""
    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "ingested": {
                "SPY": {"raw_symbol": "SPY", "bars": 0},
                "AAPL": {"raw_symbol": "AAPL", "bars": 58},
            },
            "empty_symbols": ["SPY"],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await ingest_symbols(
        "stocks", ["SPY", "AAPL"], "2024-01-01", "2024-01-31"
    )
    assert result.bars_written == 58
    assert result.symbols_covered == ["AAPL"]  # only symbols with bars>0
    assert result.empty_symbols == ["SPY"]


@pytest.mark.asyncio
async def test_run_ingest_shim_returns_none(monkeypatch):
    """``run_ingest`` (the arq-exposed verb) stays a 3-line shim returning None."""
    from msai.services.data_ingestion import run_ingest

    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "ingested": {"SPY": {"raw_symbol": "SPY", "bars": 1}},
            "empty_symbols": [],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await run_ingest({}, "stocks", ["SPY"], "2024-01-01", "2024-12-31")
    assert result is None


@pytest.mark.asyncio
async def test_run_ingest_shim_delegates_to_ingest_symbols(monkeypatch):
    """iter-2 P2-B fix: the arq shim MUST delegate to ``ingest_symbols`` with
    identical args so a single code path owns the real ingest pipeline. A
    future refactor that breaks this contract (e.g. the shim starts calling
    the service directly) needs this test to fail."""
    calls: list[dict[str, object]] = []

    async def fake_ingest_symbols(
        asset_class_ingest: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str = "auto",
        dataset: str | None = None,
        schema: str | None = None,
    ) -> IngestResult:
        calls.append(
            {
                "asset_class": asset_class_ingest,
                "symbols": symbols,
                "start": start,
                "end": end,
                "provider": provider,
                "dataset": dataset,
                "schema": schema,
            }
        )
        return IngestResult(
            bars_written=0,
            symbols_covered=[],
            empty_symbols=list(symbols),
            coverage_status="none",
        )

    monkeypatch.setattr(
        "msai.services.data_ingestion.ingest_symbols", fake_ingest_symbols
    )

    from msai.services.data_ingestion import run_ingest

    result = await run_ingest(
        {}, "equity", ["SPY"], "2024-01-01", "2024-12-31"
    )
    assert result is None  # shim swallows return value to preserve arq wire format
    assert calls == [
        {
            "asset_class": "equity",
            "symbols": ["SPY"],
            "start": "2024-01-01",
            "end": "2024-12-31",
            "provider": "auto",
            "dataset": None,
            "schema": None,
        }
    ]
```

- [ ] **Step 2: Implementation.**

Extract an internal `_build_default_service()` factory + `ingest_symbols(...)` reusable helper; leave the existing `run_ingest` arq function a thin wrapper over it.

```python
# Snippet — add to backend/src/msai/services/data_ingestion.py

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class IngestResult:
    bars_written: int
    symbols_covered: list[str]
    empty_symbols: list[str]
    coverage_status: Literal["full", "gapped", "none"] = "full"


def _build_default_service() -> "DataIngestionService":
    return DataIngestionService(ParquetStore(str(settings.parquet_root)))


async def ingest_symbols(
    asset_class_ingest: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> IngestResult:
    """Reusable in-process ingest helper.

    Called directly by the symbol-onboarding orchestrator (no arq round
    trip — see ``docs/plans/2026-04-24-symbol-onboarding.md`` T6 for the
    deadlock rationale). ``asset_class_ingest`` is the INGEST taxonomy
    (``stocks|futures|forex|option``).
    """
    service = _build_default_service()
    stats = await service.ingest_historical(
        asset_class_ingest,
        symbols,
        start,
        end,
        provider=provider,
        dataset=dataset,
        schema=schema,
    )
    # iter-2 P1-A fix: ``ingest_historical`` returns a nested payload
    # (``{"ingested": {raw_symbol: {"bars": int, ...}}, "empty_symbols": [...]}``)
    # — NOT flat ``bars_written`` / ``symbols_covered`` keys. Derive both
    # locally from ``ingested`` so the helper is honest about the real
    # contract. Coverage status must still be re-checked by the orchestrator
    # via the T5 coverage scanner — that's the source of truth for gap
    # detection; this helper reports only whether bytes landed.
    ingested = stats.get("ingested", {}) or {}
    bars_written = sum(int(d.get("bars", 0)) for d in ingested.values())
    symbols_covered = [k for k, d in ingested.items() if int(d.get("bars", 0)) > 0]
    empty_symbols = list(stats.get("empty_symbols", []) or [])
    return IngestResult(
        bars_written=bars_written,
        symbols_covered=symbols_covered,
        empty_symbols=empty_symbols,
    )


async def run_ingest(
    ctx: dict[str, Any],
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> None:
    """arq-compatible thin wrapper over :func:`ingest_symbols`.

    Kept as a 3-line shim so existing arq callers (backtest auto-heal
    enqueues this by name) see unchanged wire semantics (returns ``None``).
    """
    _ = ctx
    try:
        await ingest_symbols(
            asset_class, symbols, start, end,
            provider=provider, dataset=dataset, schema=schema,
        )
    except Exception:
        log.error(
            "ingest_failed",
            asset_class=asset_class,
            provider=provider,
            dataset=dataset,
            symbols=",".join(symbols),
            start=start,
            end=end,
        )
        raise
```

- [ ] **Step 3: Ruff + mypy + commit.**

```bash
cd backend && uv run ruff check src/msai/services/data_ingestion.py tests/unit/services/test_data_ingestion_ingest_symbols.py
uv run mypy --strict src/msai/services/data_ingestion.py
git add backend/src/msai/services/data_ingestion.py backend/tests/unit/services/test_data_ingestion_ingest_symbols.py
git commit -m "refactor(data-ingestion): T6a extract reusable ingest_symbols helper; run_ingest stays 3-line arq shim"
```

---

### Task 6: `_onboard_one_symbol` orchestrator (`services/symbol_onboarding/orchestrator.py`)

**Files:**

- Create: `backend/src/msai/services/symbol_onboarding/orchestrator.py`
- Create: `backend/tests/integration/symbol_onboarding/__init__.py`
- Create: `backend/tests/integration/symbol_onboarding/test_orchestrator.py`

**Dependencies:** T1 (`SymbolOnboardingRun`), T2 (schemas), T3 (`OnboardSymbolSpec` + `normalize_asset_class_for_ingest`), T5 (coverage), T6a (`ingest_symbols` helper), T13 (onboarding metrics). Reuses: PR #44 `DatabentoBootstrapService` (batch-API), `IBQualifier.qualify(spec: InstrumentSpec)` at `services/nautilus/security_master/ib_qualifier.py:186`.

**Writes:** `backend/src/msai/services/symbol_onboarding/orchestrator.py`, `backend/tests/integration/symbol_onboarding/__init__.py`, `backend/tests/integration/symbol_onboarding/test_orchestrator.py`.

Contract:

- Sole seam intended to be rewritten for parallelism someday (council constraint #3). Runs strictly sequentially within a single worker turn.
- `_onboard_one_symbol(*, run_id, spec, request_live_qualification, db_factory, data_root, ib_timeout_s=120, bootstrap_service=None, ib_service=None, today=None) -> SymbolStateRow`. Note: **no `pool` parameter** — we do NOT enqueue child arq jobs from here (council Option A). The worker passes nothing queue-related.
- Steps per symbol:
  1. `bootstrap` (`step=SymbolStepStatus.BOOTSTRAP`) — via `DatabentoBootstrapService.bootstrap(symbols=[spec.symbol], asset_class_override=spec.asset_class, exact_ids=None)` (PR #44's real batch signature); inspect the single-item `list[BootstrapResult]` result. On `outcome in {AMBIGUOUS, UNAUTHORIZED, UNMAPPED_VENUE}` → terminal `SymbolStatus.FAILED` with `error.code = f"BOOTSTRAP_{outcome.upper()}"`.
  2. `ingest` (`step=SymbolStepStatus.INGEST`) — call `ingest_symbols(ingest_asset, [spec.symbol], start, end)` IN-PROCESS (T6a helper). `ingest_asset = normalize_asset_class_for_ingest(spec.asset_class)`. Captures `IngestResult.bars_written` into the symbol_state for observability.
  3. `coverage` (`step=SymbolStepStatus.COVERAGE`) — `compute_coverage(asset_class=ingest_asset, ...)` after ingest; if `status != "full"` → terminal `SymbolStatus.FAILED` with `step=SymbolStepStatus.COVERAGE_FAILED` and machine-readable `error.code = "COVERAGE_INCOMPLETE"` and `missing_ranges` in `error.details`.
  4. `ib_qualify` (`step=SymbolStepStatus.IB_QUALIFY`) — **ONLY when `request_live_qualification=True`**. Constructs an `InstrumentSpec` from `(spec.symbol, spec.asset_class)` and calls `IBQualifier.qualify(spec)`. Wrapped in `asyncio.wait_for(..., timeout=ib_timeout_s)` per council safety constraint (Minority-Report absorbed). Failure codes: `IB_TIMEOUT`, `IB_UNAVAILABLE`, `IB_AMBIGUOUS`. On `IB_TIMEOUT` also increments the `onboarding_ib_timeout_total` counter.
  5. Terminal state: `status = SymbolStatus.SUCCEEDED` with `step=SymbolStepStatus.COMPLETED` (IB path) or `step=SymbolStepStatus.IB_SKIPPED` (no IB requested); otherwise `status = SymbolStatus.FAILED` with the first failing step's error envelope.
- Every state transition persists via `UPDATE symbol_onboarding_runs SET symbol_states = jsonb_set(...)` with row-level lock (`SELECT … FOR UPDATE`) so parallel future callers don't clobber each other.
- Returns the final `SymbolStateRow`; never raises upward unless it's a catastrophic infra failure (in which case the worker's outer try/except stamps `run.status="failed"`).

- [ ] **Step 1: Failing integration test skeleton.**

```python
# backend/tests/integration/symbol_onboarding/test_orchestrator.py
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import OnboardSymbolSpec
from msai.services.data_ingestion import IngestResult
from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapOutcome,
    BootstrapResult,
)
from msai.services.symbol_onboarding.orchestrator import _onboard_one_symbol


@pytest.mark.asyncio
async def test_orchestrator_happy_path_without_live_qualification(
    session_factory, tmp_parquet_root, mock_databento
):
    # ARRANGE
    spec = OnboardSymbolSpec(
        symbol="SPY",
        asset_class="equity",  # registry taxonomy
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.IN_PROGRESS,
            job_id_digest="test-digest-happy",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    # Seed Parquet so post-ingest coverage check passes. Parquet path uses
    # the INGEST taxonomy (``stocks``), not registry taxonomy — orchestrator
    # routes through ``normalize_asset_class_for_ingest``.
    base = tmp_parquet_root / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")

    # Fake bootstrap returns CREATED.
    fake_bootstrap = AsyncMock()
    fake_bootstrap.bootstrap = AsyncMock(
        return_value=[
            BootstrapResult(
                symbol="SPY",
                outcome=BootstrapOutcome.CREATED,
                registered=True,
                backtest_data_available=False,
                live_qualified=False,
            )
        ]
    )

    # ACT — patch the in-process ingest helper (council Option A: inline, NOT arq child).
    with patch(
        "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
        new=AsyncMock(return_value=IngestResult(bars_written=258_000, symbols_covered=["SPY"], empty_symbols=[])),
    ):
        state = await _onboard_one_symbol(
            run_id=run_id,
            spec=spec,
            request_live_qualification=False,
            db_factory=session_factory,
            data_root=tmp_parquet_root,
            bootstrap_service=fake_bootstrap,
        )

    # ASSERT
    assert state.status == "succeeded"
    assert state.step == "ib_skipped"  # live qualification not requested
    assert state.error is None
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/services/symbol_onboarding/orchestrator.py
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from msai.models.symbol_onboarding_run import SymbolOnboardingRun
from msai.schemas.symbol_onboarding import (
    OnboardSymbolSpec,
    SymbolStateRow,
    SymbolStatus,
    SymbolStepStatus,
)
from msai.services.data_ingestion import IngestResult, ingest_symbols
from msai.services.observability.trading_metrics import (
    onboarding_ib_timeout_total,
)
from msai.services.symbol_onboarding import normalize_asset_class_for_ingest
from msai.services.symbol_onboarding.coverage import compute_coverage

log = structlog.get_logger(__name__)

__all__ = ["_onboard_one_symbol", "OrchestratorBootstrapProto", "OrchestratorIBProto"]


class OrchestratorBootstrapProto(Protocol):
    async def bootstrap(
        self,
        *,
        symbols: list[str],
        asset_class_override: str | None,
        exact_ids: dict[str, str] | None,
    ) -> list[Any]: ...  # list[BootstrapResult] — typed lazily to avoid Nautilus import at orchestrator boundary


class OrchestratorIBProto(Protocol):
    async def qualify(self, *, symbol: str, asset_class: str) -> None: ...


# Outcome strings from PR #44's BootstrapOutcome StrEnum that are TERMINAL-FAILED
# for onboarding (registry won't be populated to the point where ingest can
# proceed). ``CREATED`` / ``NOOP`` / ``ALIAS_ROTATED`` are success paths.
_BOOTSTRAP_FAILURE_OUTCOMES = frozenset(
    {"ambiguous", "unauthorized", "unmapped_venue", "upstream_error", "rate_limited"}
)


async def _onboard_one_symbol(
    *,
    run_id: UUID,
    spec: OnboardSymbolSpec,
    request_live_qualification: bool,
    db_factory: async_sessionmaker[AsyncSession],
    data_root: Path,
    bootstrap_service: OrchestratorBootstrapProto | None = None,
    ib_service: OrchestratorIBProto | None = None,
    ib_timeout_s: int = 120,
    today: date | None = None,
) -> SymbolStateRow:
    """Run all phases for a single symbol; persist progress after each phase.

    Sequential by contract (council constraint #3): this is the seam that
    future work may swap with a parallel dispatch. Do not introduce
    cross-symbol shared state here.

    The ingest phase is **inline / in-process** via
    :func:`msai.services.data_ingestion.ingest_symbols` — we do NOT
    enqueue a child arq job, because ``IngestWorkerSettings.max_jobs=1``
    is shared with the parent ``run_symbol_onboarding`` worker and
    ``await job.result()`` would self-deadlock.
    """

    bound = log.bind(
        run_id=str(run_id), symbol=spec.symbol, asset_class=spec.asset_class
    )
    ingest_asset = normalize_asset_class_for_ingest(spec.asset_class)

    # ----- Phase 1: bootstrap ------------------------------------------------
    await _persist_step(
        db_factory, run_id, spec.symbol, step=SymbolStepStatus.BOOTSTRAP
    )
    bootstrap_service = bootstrap_service or _default_bootstrap_service(db_factory)
    try:
        results = await bootstrap_service.bootstrap(
            symbols=[spec.symbol],
            asset_class_override=spec.asset_class,
            exact_ids=None,
        )
    except Exception as exc:  # noqa: BLE001 — bootstrap raises heterogeneous
        bound.warning("symbol_onboarding_bootstrap_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code="BOOTSTRAP_FAILED",
            message=str(exc),
        )

    if not results:
        return await _fail(
            db_factory, run_id, spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code="BOOTSTRAP_FAILED",
            message="DatabentoBootstrapService returned no result.",
        )
    outcome = results[0]
    outcome_str = str(outcome.outcome).lower()
    if outcome_str in _BOOTSTRAP_FAILURE_OUTCOMES:
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code=f"BOOTSTRAP_{outcome_str.upper()}",
            message=f"Bootstrap terminated with outcome={outcome_str}",
        )

    # ----- Phase 2: ingest (IN-PROCESS — council Option A) -------------------
    await _persist_step(
        db_factory, run_id, spec.symbol, step=SymbolStepStatus.INGEST
    )
    try:
        result: IngestResult = await ingest_symbols(
            ingest_asset,
            [spec.symbol],
            spec.start.isoformat(),
            spec.end.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 — ingest raises provider-specific types
        bound.warning("symbol_onboarding_ingest_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.INGEST,
            code="INGEST_FAILED",
            message=str(exc),
        )
    bound.info("symbol_onboarding_ingest_done", bars_written=result.bars_written)

    # ----- Phase 3: coverage check ------------------------------------------
    await _persist_step(
        db_factory, run_id, spec.symbol, step=SymbolStepStatus.COVERAGE
    )
    coverage = await compute_coverage(
        asset_class=ingest_asset,
        symbol=spec.symbol,
        start=spec.start,
        end=spec.end,
        data_root=data_root,
        today=today,
    )
    if coverage.status != "full":
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.COVERAGE_FAILED,
            code="COVERAGE_INCOMPLETE",
            message=f"Post-ingest coverage = {coverage.status}",
            details={
                "missing_ranges": [
                    {"start": s.isoformat(), "end": e.isoformat()}
                    for s, e in coverage.missing_ranges
                ]
            },
        )

    # ----- Phase 4: IB qualification (optional) ------------------------------
    if not request_live_qualification:
        return await _succeed(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_SKIPPED,
        )

    await _persist_step(
        db_factory, run_id, spec.symbol, step=SymbolStepStatus.IB_QUALIFY
    )
    ib_service = ib_service or _default_ib_service()
    try:
        await asyncio.wait_for(
            ib_service.qualify(symbol=spec.symbol, asset_class=spec.asset_class),
            timeout=ib_timeout_s,
        )
    except TimeoutError:
        onboarding_ib_timeout_total.inc()
        bound.warning("symbol_onboarding_ib_timeout", timeout_s=ib_timeout_s)
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_QUALIFY,
            code="IB_TIMEOUT",
            message=f"IB qualification timed out after {ib_timeout_s}s",
        )
    except Exception as exc:  # noqa: BLE001
        bound.warning("symbol_onboarding_ib_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_QUALIFY,
            code="IB_UNAVAILABLE",
            message=str(exc),
        )

    return await _succeed(
        db_factory, run_id, spec, step=SymbolStepStatus.COMPLETED
    )


async def _persist_step(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    symbol: str,
    *,
    step: SymbolStepStatus,
) -> None:
    async with db_factory() as db:
        async with db.begin():
            row = (
                await db.execute(
                    select(SymbolOnboardingRun)
                    .where(SymbolOnboardingRun.id == run_id)
                    .with_for_update()
                )
            ).scalar_one()
            states = dict(row.symbol_states)  # shallow copy to trigger ORM dirty
            entry = dict(states.get(symbol, {}))
            entry["step"] = step.value
            entry["status"] = SymbolStatus.IN_PROGRESS.value
            states[symbol] = entry
            row.symbol_states = states


async def _succeed(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    step: SymbolStepStatus,
) -> SymbolStateRow:
    return await _finalize(
        db_factory,
        run_id,
        spec,
        status=SymbolStatus.SUCCEEDED,
        step=step,
        error=None,
    )


async def _fail(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    step: SymbolStepStatus,
    code: str,
    message: str,
    details: dict | None = None,
) -> SymbolStateRow:
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return await _finalize(
        db_factory,
        run_id,
        spec,
        status=SymbolStatus.FAILED,
        step=step,
        error=error,
    )


async def _finalize(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    status: SymbolStatus,
    step: SymbolStepStatus,
    error: dict | None,
) -> SymbolStateRow:
    async with db_factory() as db:
        async with db.begin():
            row = (
                await db.execute(
                    select(SymbolOnboardingRun)
                    .where(SymbolOnboardingRun.id == run_id)
                    .with_for_update()
                )
            ).scalar_one()
            states = dict(row.symbol_states)
            entry = dict(states.get(spec.symbol, {}))
            entry.update(
                {
                    "symbol": spec.symbol,
                    "asset_class": spec.asset_class,
                    "start": spec.start.isoformat(),
                    "end": spec.end.isoformat(),
                    "status": status.value,
                    "step": step.value,
                    "error": error,
                }
            )
            states[spec.symbol] = entry
            row.symbol_states = states
    return SymbolStateRow(
        symbol=spec.symbol,
        asset_class=spec.asset_class,
        start=spec.start,
        end=spec.end,
        status=status.value,
        step=step.value,
        error=error,
    )


def _default_bootstrap_service(
    db_factory: async_sessionmaker[AsyncSession],
) -> OrchestratorBootstrapProto:
    """Build a default ``DatabentoBootstrapService``.

    Important (iter-1 fix): ``DatabentoBootstrapService.__init__`` requires
    ``(session_factory, databento_client)`` — it has NO no-arg constructor.
    The orchestrator shares its ``db_factory`` with the bootstrap service so
    both participate in the same connection pool.
    """
    # Late import to keep unit tests fast.
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.databento_bootstrap import (
        DatabentoBootstrapService,
    )
    return DatabentoBootstrapService(
        session_factory=db_factory, databento_client=DatabentoClient()
    )  # type: ignore[return-value]


def _default_ib_service() -> OrchestratorIBProto:
    """Build a default IB qualifier-adapter.

    ``IBRefreshService`` does NOT exist in the repo (iter-1 fix — removed
    from the plan). This adapter wraps Nautilus's
    :class:`IBQualifier` + constructs an ``InstrumentSpec`` from
    ``(symbol, asset_class)`` on the fly.
    """
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier
    from msai.services.nautilus.security_master.specs import InstrumentSpec
    from msai.services.nautilus.ib_provider_factory import (
        get_interactive_brokers_instrument_provider,
    )

    class _IBServiceAdapter:
        async def qualify(self, *, symbol: str, asset_class: str) -> None:
            provider = await get_interactive_brokers_instrument_provider()
            qualifier = IBQualifier(provider)
            # Registry taxonomy → IBQualifier spec taxonomy: ``futures``
            # plural is already singular in the spec vocab (``future``);
            # map the two mismatched cases (``futures`` → ``future``, rest
            # pass through since ``equity`` / ``fx`` / ``option`` align).
            spec_asset = "future" if asset_class == "futures" else asset_class
            spec = InstrumentSpec(symbol=symbol, asset_class=spec_asset, venue=None)
            await qualifier.qualify(spec)

    return _IBServiceAdapter()  # type: ignore[return-value]
```

- [ ] **Step 3: Run tests.**

```bash
cd backend && uv run pytest tests/integration/symbol_onboarding/test_orchestrator.py -v
```

Expected: PASS on happy path. Failing-path tests are added in T15 under the integration-tests batch (see below).

- [ ] **Step 4: Ruff + mypy + commit.**

```bash
cd backend && uv run ruff check src/msai/services/symbol_onboarding/orchestrator.py tests/integration/symbol_onboarding/
uv run mypy --strict src/msai/services/symbol_onboarding/orchestrator.py
git add backend/src/msai/services/symbol_onboarding/orchestrator.py backend/tests/integration/symbol_onboarding/
git commit -m "feat(symbol-onboarding): T6 _onboard_one_symbol orchestrator with IB timeout + persisted state transitions"
```

---

### Task 7: `run_symbol_onboarding` arq task (`workers/symbol_onboarding_job.py`)

**Files:**

- Create: `backend/src/msai/workers/symbol_onboarding_job.py`
- Modify: `backend/src/msai/workers/ingest_settings.py` (add `run_symbol_onboarding` to `functions` list — single-task entrypoint per council verdict, no new queue).
- Create: `backend/tests/integration/symbol_onboarding/test_worker_task.py`

**Dependencies:** T6 (orchestrator), T1 (run model).

**Writes:** `backend/src/msai/workers/symbol_onboarding_job.py`, `backend/src/msai/workers/ingest_settings.py` (modify), `backend/tests/integration/symbol_onboarding/test_worker_task.py`.

Contract:

- Single arq entrypoint: `run_symbol_onboarding(ctx, run_id: str) -> dict[str, Any]`.
- Loads the `SymbolOnboardingRun` row; transitions `status: pending → in_progress`; iterates `symbol_states.keys()` sequentially; calls `_onboard_one_symbol` for each; computes terminal run status from per-symbol states:
  - **All succeeded** (every `SymbolStateRow.status == "succeeded"`) → run `completed`.
  - **Anything else reached via the normal loop** (all-failed, mixed, any-failed) → run `completed_with_failures`. This is the council-pinned semantic: per-symbol failures do NOT bubble to run-level `failed`.
  - Run `failed` is reserved for the outer try/except ONLY — systemic short-circuits where the loop could not run to completion (DB down, Redis down, unhandled exception from the worker infrastructure itself).
- Emits 3 Prometheus counters/histogram per T13 (`msai_onboarding_jobs_total{status}` on exit, `msai_onboarding_symbol_duration_seconds{step}` per symbol, `msai_onboarding_ib_timeout_total` incremented inside the orchestrator on `IB_TIMEOUT`).
- Outer try/except: if the task crashes catastrophically (DB down, Redis down), stamp `run.status = "failed"` via best-effort write so `/status` reflects reality; re-raise so arq's retry/DLQ path still observes the failure.

- [ ] **Step 1: Test.**

```python
# backend/tests/integration/symbol_onboarding/test_worker_task.py
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.workers.symbol_onboarding_job import run_symbol_onboarding


@pytest.mark.asyncio
async def test_worker_marks_run_completed_when_every_symbol_succeeds(
    session_factory, tmp_parquet_root
):
    # ARRANGE — seed run + parquet
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-happy",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    # Parquet path uses the INGEST taxonomy (``stocks``).
    base = tmp_parquet_root / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")

    # ACT
    fake_state = _state("succeeded", "ib_skipped")
    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(return_value=fake_state),
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as settings,
    ):
        settings.data_root = str(tmp_parquet_root)
        ctx = {"redis": AsyncMock()}
        result = await run_symbol_onboarding(ctx, run_id=str(run_id))

    # ASSERT
    assert result["status"] == "completed"
    async with session_factory() as db:
        persisted = (
            await db.execute(
                select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id)
            )
        ).scalar_one()
        assert persisted.status == SymbolOnboardingRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_marks_run_completed_with_failures_when_every_symbol_fails(
    session_factory, tmp_parquet_root
):
    """Council-pinned semantic: per-symbol failures NEVER bubble to run-level
    ``failed``. Even when every symbol fails via the normal loop, the run
    terminates at ``completed_with_failures``. ``failed`` is reserved for
    systemic short-circuits (outer try/except)."""
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-allfail",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    fake_state = _state("failed", "ingest", error={"code": "INGEST_FAILED", "message": "x"})
    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(return_value=fake_state),
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as settings,
    ):
        settings.data_root = str(tmp_parquet_root)
        result = await run_symbol_onboarding({"redis": AsyncMock()}, run_id=str(run_id))

    assert result["status"] == "completed_with_failures"


@pytest.mark.asyncio
async def test_worker_marks_run_failed_on_systemic_short_circuit(
    session_factory, tmp_parquet_root
):
    """The ONLY path to run-level ``failed``: an unhandled exception from
    inside the worker infrastructure (not from a per-symbol iteration)."""
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-systemic",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(side_effect=RuntimeError("db connection reset")),
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as settings,
    ):
        settings.data_root = str(tmp_parquet_root)
        with pytest.raises(RuntimeError):
            await run_symbol_onboarding({"redis": AsyncMock()}, run_id=str(run_id))

    async with session_factory() as db:
        persisted = (
            await db.execute(
                select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id)
            )
        ).scalar_one()
        assert persisted.status == SymbolOnboardingRunStatus.FAILED


def _state(status, step, error=None):
    from msai.schemas.symbol_onboarding import SymbolStateRow
    return SymbolStateRow(
        symbol="SPY",
        asset_class="equity",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        status=status,
        step=step,
        error=error,
    )
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/workers/symbol_onboarding_job.py
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import (
    OnboardSymbolSpec,
    SymbolStateRow,
    SymbolStatus,
)
from msai.services.symbol_onboarding.orchestrator import _onboard_one_symbol

log = structlog.get_logger(__name__)


async def run_symbol_onboarding(ctx: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    uid = UUID(run_id)
    log_bound = log.bind(run_id=str(uid))
    log_bound.info("symbol_onboarding_worker_started")

    try:
        async with async_session_factory() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(SymbolOnboardingRun)
                        .where(SymbolOnboardingRun.id == uid)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    log_bound.error("symbol_onboarding_worker_run_missing")
                    return {"status": "missing"}
                row.status = SymbolOnboardingRunStatus.IN_PROGRESS
                row.started_at = datetime.now(UTC)
                specs = _hydrate_specs(row.symbol_states)
                live_q = row.request_live_qualification

        from msai.services.observability.trading_metrics import (
            onboarding_symbol_duration_seconds,
        )

        per_symbol_states: list[SymbolStateRow] = []
        for spec in specs:
            t0 = time.monotonic()
            state = await _onboard_one_symbol(
                run_id=uid,
                spec=spec,
                request_live_qualification=live_q,
                db_factory=async_session_factory,
                data_root=Path(settings.data_root),
            )
            elapsed = time.monotonic() - t0
            onboarding_symbol_duration_seconds.labels(step=state.step).observe(
                elapsed
            )
            per_symbol_states.append(state)

        terminal = _compute_terminal_status(per_symbol_states)
        async with async_session_factory() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(SymbolOnboardingRun)
                        .where(SymbolOnboardingRun.id == uid)
                        .with_for_update()
                    )
                ).scalar_one()
                row.status = terminal
                row.completed_at = datetime.now(UTC)

        from msai.services.observability.trading_metrics import onboarding_jobs_total

        onboarding_jobs_total.labels(status=terminal.value).inc()
        log_bound.info(
            "symbol_onboarding_worker_completed", terminal_status=terminal.value
        )
        return {"status": terminal.value, "run_id": str(uid)}

    except Exception as exc:  # noqa: BLE001 — best-effort status sync before re-raise
        log_bound.exception("symbol_onboarding_worker_crashed", error=repr(exc))
        try:
            async with async_session_factory() as db:
                async with db.begin():
                    row = (
                        await db.execute(
                            select(SymbolOnboardingRun)
                            .where(SymbolOnboardingRun.id == uid)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        row.status = SymbolOnboardingRunStatus.FAILED
                        row.completed_at = datetime.now(UTC)
        except Exception:
            log_bound.exception("symbol_onboarding_worker_status_sync_failed")
        raise


def _hydrate_specs(states: dict[str, Any]) -> list[OnboardSymbolSpec]:
    from datetime import date as _date

    specs: list[OnboardSymbolSpec] = []
    for entry in states.values():
        specs.append(
            OnboardSymbolSpec(
                symbol=entry["symbol"],
                asset_class=entry["asset_class"],
                start=_date.fromisoformat(entry["start"]),
                end=_date.fromisoformat(entry["end"]),
            )
        )
    return specs


def _compute_terminal_status(
    per_symbol: list[SymbolStateRow],
) -> SymbolOnboardingRunStatus:
    """Council-pinned semantic (iter-1 fix).

    ``FAILED`` is reserved for systemic short-circuits, which happen in
    the outer ``except`` block — NOT here. Anything reached via the
    normal per-symbol loop is either a clean run (every symbol
    succeeded) or ``completed_with_failures``.
    """
    statuses = {s.status for s in per_symbol}
    if statuses == {SymbolStatus.SUCCEEDED.value}:
        return SymbolOnboardingRunStatus.COMPLETED
    return SymbolOnboardingRunStatus.COMPLETED_WITH_FAILURES
```

Then in `backend/src/msai/workers/ingest_settings.py`, add the symbol_onboarding task to the `functions` tuple (single-queue reuse per council verdict — **not** a dedicated `msai:onboarding` queue):

```python
# backend/src/msai/workers/ingest_settings.py (modify existing `functions`)
from msai.workers.symbol_onboarding_job import run_symbol_onboarding
# ...
functions = (
    run_ingest,
    run_symbol_onboarding,  # same worker, same max_jobs=1 gate
)
```

- [ ] **Step 3: Run tests + ruff + mypy + commit.**

```bash
cd backend && uv run pytest tests/integration/symbol_onboarding/test_worker_task.py -v
uv run ruff check src/msai/workers/symbol_onboarding_job.py src/msai/workers/ingest_settings.py
uv run mypy --strict src/msai/workers/symbol_onboarding_job.py
git add backend/src/msai/workers/symbol_onboarding_job.py backend/src/msai/workers/ingest_settings.py backend/tests/integration/symbol_onboarding/test_worker_task.py
git commit -m "feat(symbol-onboarding): T7 run_symbol_onboarding arq task (single-queue, sequential) with best-effort status sync on crash"
```

---

### Task 8-prime: Promote `_error_response` to shared `api/_common.py`

**Files:**

- Create: `backend/src/msai/api/_common.py` — module exporting `error_response(status_code: int, code: str, message: str) -> JSONResponse`.
- Modify: `backend/src/msai/api/backtests.py` — replace local `_error_response` with `from msai.api._common import error_response` + call sites.
- Modify: `backend/src/msai/api/instruments.py` — same import swap (PR #44 uses this helper).
- Create: `backend/tests/unit/api/test_common_error_response.py` — 1-2 tests pinning the envelope shape.

**Dependencies:** T0.

**Writes:** `backend/src/msai/api/_common.py`, `backend/src/msai/api/backtests.py` (modify), `backend/src/msai/api/instruments.py` (modify), `backend/tests/unit/api/test_common_error_response.py`.

**Why this task exists (iter-1 P2 fix).** T9 originally imported the private `_error_response` from another router via `from msai.api.backtests import _error_response`. A leading underscore means "private by convention"; cross-module imports of private names are a smell and a refactoring hazard. The helper is already used by three routers (backtests, instruments, and the new symbol-onboarding) — promote it to a single public location.

Contract:

- `error_response(status_code, code, message)` is a thin wrapper around `JSONResponse(status_code=X, content={"error": {"code": ..., "message": ...}})` matching `.claude/rules/api-design.md` envelope shape.
- Rename is mechanical; zero behavioural change. The file lives in `api/_common.py` with leading underscore on the MODULE (to signal "shared infrastructure") but a public function name.

- [ ] **Step 1: Create `backend/src/msai/api/_common.py`.**

```python
"""Cross-router shared helpers. Kept intentionally small — anything
router-specific belongs in that router's module."""

from __future__ import annotations

from fastapi.responses import JSONResponse

__all__ = ["error_response"]


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build the canonical ``{"error": {"code", "message"}}`` envelope.

    Every error path in every router uses ``JSONResponse`` (not
    ``HTTPException``) because FastAPI wraps ``HTTPException.detail`` under
    ``{"detail": ...}`` while ``.claude/rules/api-design.md`` requires the
    envelope at top-level.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
```

- [ ] **Step 2: Swap imports in `api/backtests.py` + `api/instruments.py`.**

In `backend/src/msai/api/backtests.py`, remove the local `def _error_response(...)` and replace with:

```python
from msai.api._common import error_response
# then replace every `_error_response(...)` call with `error_response(...)`
```

In `backend/src/msai/api/instruments.py`, replace:

```python
from msai.api.backtests import _error_response
```

with:

```python
from msai.api._common import error_response
# and rename call sites accordingly
```

- [ ] **Step 3: Test.**

```python
# backend/tests/unit/api/test_common_error_response.py
import json

from msai.api._common import error_response


def test_error_response_envelope_shape() -> None:
    resp = error_response(422, "VALIDATION_ERROR", "bad input")
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body == {"error": {"code": "VALIDATION_ERROR", "message": "bad input"}}
```

- [ ] **Step 4: Ruff + mypy + full regression for the two changed routers + commit.**

```bash
cd backend && uv run pytest tests/integration/api/test_backtests.py tests/integration/api/test_instruments_bootstrap.py tests/unit/api/test_common_error_response.py -v
uv run ruff check src/msai/api/_common.py src/msai/api/backtests.py src/msai/api/instruments.py
uv run mypy --strict src/msai/api/_common.py src/msai/api/backtests.py src/msai/api/instruments.py
git add backend/src/msai/api/_common.py backend/src/msai/api/backtests.py backend/src/msai/api/instruments.py backend/tests/unit/api/test_common_error_response.py
git commit -m "refactor(api): T8-prime promote error_response to shared api/_common.py"
```

---

### Task 8: `POST /api/v1/symbols/onboard/dry-run` endpoint

**Files:**

- Create: `backend/src/msai/api/symbol_onboarding.py` (created here; T9 extends it with the other routes)
- Create: `backend/tests/integration/api/test_symbol_onboarding_dry_run.py`

**Dependencies:** T2 (schemas), T4 (cost estimator), T3 (manifest reuse for default-shape alignment), T8-prime (shared `error_response`).

**Writes:** `backend/src/msai/api/symbol_onboarding.py`, `backend/tests/integration/api/test_symbol_onboarding_dry_run.py`.

Contract:

- `POST /api/v1/symbols/onboard/dry-run` body = `OnboardRequest` (reuse).
- Returns `DryRunResponse` with `estimated_cost_usd`, `estimate_confidence`, `estimate_basis`, `symbol_count`, `breakdown`.
- 422 on any `OnboardSymbolSpec` validation failure (inherited from Pydantic via the shared model).
- Auth: existing JWT bearer dependency.
- No DB write. No arq enqueue. Pure preflight.

- [ ] **Step 1: Failing test.**

```python
# backend/tests/integration/api/test_symbol_onboarding_dry_run.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def test_dry_run_happy_path(client: TestClient, auth_headers):
    body = {
        "watchlist_name": "core",
        "symbols": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
    }
    fake_estimate = AsyncMock(
        return_value=_fake_estimate_high_confidence()
    )
    with patch(
        "msai.api.symbol_onboarding.estimate_cost", new=fake_estimate
    ):
        resp = client.post(
            "/api/v1/symbols/onboard/dry-run", json=body, headers=auth_headers
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["estimate_confidence"] == "high"
    assert data["symbol_count"] == 1


def test_dry_run_rejects_101_symbol_batch(client: TestClient, auth_headers):
    body = {
        "watchlist_name": "too-big",
        "symbols": [
            {
                "symbol": f"SYM{i:03d}",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
            for i in range(101)
        ],
    }
    resp = client.post(
        "/api/v1/symbols/onboard/dry-run", json=body, headers=auth_headers
    )
    assert resp.status_code == 422
    assert "symbols" in resp.text


def _fake_estimate_high_confidence():
    from msai.services.symbol_onboarding.cost_estimator import (
        CostEstimate,
        CostLine,
    )

    return CostEstimate(
        total_usd=0.42,
        symbol_count=1,
        breakdown=[CostLine("SPY", "equity", "XNAS.ITCH", 0.42)],
        confidence="high",
        basis="databento.metadata.get_cost (1m OHLCV)",
    )
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/api/symbol_onboarding.py
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.schemas.symbol_onboarding import DryRunResponse, OnboardRequest
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.symbol_onboarding.cost_estimator import estimate_cost
from msai.services.symbol_onboarding.manifest import ParsedManifest

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/symbols", tags=["symbols"])


@router.post("/onboard/dry-run", response_model=DryRunResponse)
async def onboard_dry_run(
    request: OnboardRequest,
    _user: Any = Depends(get_current_user),
) -> DryRunResponse:
    # ``DatabentoClient`` has no ``get_databento_historical_client()`` factory
    # in this repo (iter-1 fix). Instantiate directly; the SDK is memoized
    # inside the client so repeat instantiation is cheap.
    client = DatabentoClient()
    manifest = ParsedManifest(
        watchlist_name=request.watchlist_name, symbols=list(request.symbols)
    )
    estimate = await estimate_cost(manifest, client=client)
    return DryRunResponse(
        watchlist_name=request.watchlist_name,
        estimated_cost_usd=estimate.total_usd,
        estimate_basis=estimate.basis,
        estimate_confidence=estimate.confidence,
        symbol_count=estimate.symbol_count,
        breakdown=[
            {
                "symbol": line.symbol,
                "dataset": line.dataset,
                "usd": line.usd,
            }
            for line in estimate.breakdown
        ],
    )
```

- [ ] **Step 3: Run tests + ruff + mypy + commit.**

```bash
cd backend && uv run pytest tests/integration/api/test_symbol_onboarding_dry_run.py -v
uv run ruff check src/msai/api/symbol_onboarding.py tests/integration/api/test_symbol_onboarding_dry_run.py
uv run mypy --strict src/msai/api/symbol_onboarding.py
git add backend/src/msai/api/symbol_onboarding.py backend/tests/integration/api/test_symbol_onboarding_dry_run.py
git commit -m "feat(symbol-onboarding): T8 POST /symbols/onboard/dry-run preflight cost estimate"
```

---

### Task 9: `POST /onboard` + `GET /onboard/{run_id}/status` + `POST /repair` endpoints

**Files:**

- Modify: `backend/src/msai/api/symbol_onboarding.py` (extends T8 router)
- Create: `backend/tests/integration/api/test_symbol_onboarding_api.py`

**Dependencies:** T1 (model), T2 (schemas), T7 (worker task).

**Writes:** `backend/src/msai/api/symbol_onboarding.py` (modify), `backend/tests/integration/api/test_symbol_onboarding_api.py`.

Contract:

- `POST /api/v1/symbols/onboard` — body `OnboardRequest`; returns 202 + `OnboardResponse(run_id, watchlist_name, status="pending")`. **Idempotency order (iter-2 P1-B fix — routed through shared `_enqueue_and_persist_run` helper):**
  1. Compute `job_digest = compute_blake2b_digest_key("symbol_onboarding", watchlist_name, str(request_live_qualification), *canonical_symbol_tuples)` (new helper — T9 adds it to `security_master/service.py`). Render as hex; this is the arq `_job_id`.
  2. `SELECT ... FOR UPDATE` on `job_id_digest = digest`. If a row exists → **200 OK** with the existing `run_id` (same status); no new enqueue, no new row. Exact-duplicate fast path.
  3. Call `pool.enqueue_job("run_symbol_onboarding", run_id=<reserved uuid>, _job_id=digest, _queue_name="msai:ingest")`.
     - If the call **raises** (Redis unreachable, etc.) → 503 with `{"code":"QUEUE_UNAVAILABLE"}`; **no** DB row committed.
     - If the call returns a `Job` (happy path) → proceed to step 4.
     - If the call returns `None` (arq-level dedup with an in-flight sibling request) → sleep ~100 ms + re-SELECT; if the sibling's row is now visible → 200 OK + sibling's `run_id`; if **still** missing → 409 with `{"code":"DUPLICATE_IN_FLIGHT"}`. **Never** fabricate a `reserved_id` for a job that was never persisted.
  4. Commit the `SymbolOnboardingRun` row with `job_id_digest = digest` + seeded `symbol_states[symbol] = {"status":"not_started","step":"pending",...}` for each spec. Return 202 + new run_id.
  5. If the DB commit fails after a successful enqueue, rollback + best-effort `await pool.abort_job(job.job_id)` + re-raise 500.
- `GET /api/v1/symbols/onboard/{run_id}/status` — returns `StatusResponse` with `progress` counters derived from `symbol_states`, plus `per_symbol` list sorted by (asset_class, symbol). 404 if run not found; error envelope via shared `error_response` helper (T8-prime — `from msai.api._common import error_response`).
- `POST /api/v1/symbols/onboard/{run_id}/repair` — body `{ "symbols": ["SPY", "AAPL"] }` (optional; defaults to all `failed` symbols). Creates a NEW `SymbolOnboardingRun` that re-runs only the requested symbols with the same windows from the failed parent run; returns 202 + new `run_id`. Rejects if parent `status == "in_progress"` (409). **Idempotency identical to `/onboard`** (iter-2 P1-C fix): the repair handler calls the SAME `_enqueue_and_persist_run` helper with a parent-scoped digest (`compute_blake2b_digest_key("symbol_onboarding", parent.watchlist_name + "-repair", f"repair:{parent.id}", *sorted(target_symbols))`). Repeated repair calls against the same parent + same symbol list collapse with zero side effects; Redis-down leaves zero orphan rows.

- [ ] **Step 1: Failing integration tests.**

```python
# backend/tests/integration/api/test_symbol_onboarding_api.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)


@pytest.mark.asyncio
async def test_post_onboard_returns_202_and_enqueues_task(
    client: TestClient, auth_headers, session_factory
):
    body = {
        "watchlist_name": "core",
        "symbols": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
        "request_live_qualification": False,
    }
    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock(return_value=AsyncMock(job_id="abc"))
    with patch(
        "msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=fake_pool)
    ):
        resp = client.post(
            "/api/v1/symbols/onboard", json=body, headers=auth_headers
        )
    assert resp.status_code == 202
    data = resp.json()
    run_id = data["run_id"]
    assert data["status"] == "pending"
    async with session_factory() as db:
        row = (
            await db.execute(
                select(SymbolOnboardingRun).where(
                    SymbolOnboardingRun.id == run_id
                )
            )
        ).scalar_one()
        assert row.status == SymbolOnboardingRunStatus.PENDING
        assert set(row.symbol_states.keys()) == {"SPY"}


@pytest.mark.asyncio
async def test_duplicate_submit_returns_200_with_existing_run_id(
    client: TestClient, auth_headers, session_factory
):
    """iter-2 P1-B fix: an exact-duplicate POST (row already persisted with
    same digest) returns 200 + the EXISTING run_id and does NOT re-enqueue
    a job or insert a second row. This is the fast path — no Redis work."""
    body = {
        "watchlist_name": "core",
        "symbols": [
            {"symbol": "SPY", "asset_class": "equity",
             "start": "2024-01-01", "end": "2024-12-31"}
        ],
    }
    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock(return_value=AsyncMock(job_id="abc"))
    with patch(
        "msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=fake_pool)
    ):
        r1 = client.post("/api/v1/symbols/onboard", json=body, headers=auth_headers)
        r2 = client.post("/api/v1/symbols/onboard", json=body, headers=auth_headers)
    assert r1.status_code == 202
    assert r2.status_code == 200, "Second POST must short-circuit to the existing row."
    assert r1.json()["run_id"] == r2.json()["run_id"], "Duplicate POST must collapse."
    # Second call must NOT have enqueued a job.
    assert fake_pool.enqueue_job.call_count == 1
    async with session_factory() as db:
        rows = (await db.execute(select(SymbolOnboardingRun))).scalars().all()
        assert len(rows) == 1, f"Expected exactly 1 run row, found {len(rows)}."


@pytest.mark.asyncio
async def test_duplicate_submit_during_race_returns_409_when_row_not_visible_yet(
    client: TestClient, auth_headers, session_factory
):
    """iter-2 P1-B fix: narrow race — sibling POST hasn't committed its row
    yet. ``pool.enqueue_job`` returns ``None`` (arq dedup) and BOTH
    digest-lookups miss. Must return 409 ``DUPLICATE_IN_FLIGHT`` — NOT
    fabricate a ``reserved_id`` as if the job existed."""
    body = {
        "watchlist_name": "core",
        "symbols": [
            {"symbol": "SPY", "asset_class": "equity",
             "start": "2024-01-01", "end": "2024-12-31"}
        ],
    }
    fake_pool = AsyncMock()
    # enqueue_job returns None on first try (sibling owns the dedup key).
    fake_pool.enqueue_job = AsyncMock(return_value=None)
    with patch(
        "msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=fake_pool)
    ):
        resp = client.post("/api/v1/symbols/onboard", json=body, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_IN_FLIGHT"
    async with session_factory() as db:
        rows = (await db.execute(select(SymbolOnboardingRun))).scalars().all()
        assert rows == [], "No DB row may be minted on a race-loss."


@pytest.mark.asyncio
async def test_redis_down_returns_503_and_commits_no_row(
    client: TestClient, auth_headers, session_factory
):
    """iter-2 P1-B fix: ``pool.enqueue_job`` raising MUST return 503
    ``QUEUE_UNAVAILABLE`` with zero DB rows committed."""
    body = {
        "watchlist_name": "core",
        "symbols": [
            {"symbol": "SPY", "asset_class": "equity",
             "start": "2024-01-01", "end": "2024-12-31"}
        ],
    }
    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch(
        "msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=fake_pool)
    ):
        resp = client.post("/api/v1/symbols/onboard", json=body, headers=auth_headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    async with session_factory() as db:
        rows = (await db.execute(select(SymbolOnboardingRun))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_get_status_returns_progress_counts(
    client: TestClient, auth_headers, session_factory
):
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.IN_PROGRESS,
            job_id_digest="test-digest-status-progress",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "succeeded",
                    "step": "ib_skipped",
                },
                "AAPL": {
                    "symbol": "AAPL",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "in_progress",
                    "step": "ingest",
                },
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        rid = str(run.id)

    resp = client.get(
        f"/api/v1/symbols/onboard/{rid}/status", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["progress"]["total"] == 2
    assert data["progress"]["succeeded"] == 1
    assert data["progress"]["in_progress"] == 1


def test_get_status_404_for_unknown_run(client: TestClient, auth_headers):
    resp = client.get(
        f"/api/v1/symbols/onboard/{uuid4()}/status", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_post_repair_rejects_in_progress_parent(
    client: TestClient, auth_headers, session_factory
):
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.IN_PROGRESS,
            job_id_digest="test-digest-repair-rejects",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "failed",
                    "step": "ingest",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        rid = str(run.id)
    resp = client.post(
        f"/api/v1/symbols/onboard/{rid}/repair", json={}, headers=auth_headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "PARENT_RUN_IN_PROGRESS"
```

- [ ] **Step 2a: Add `compute_blake2b_digest_key` to `security_master/service.py`.**

New sibling helper next to `compute_advisory_lock_key` — same blake2b primitive, different arbitrary-parts semantics (iter-1 P2 fix).

```python
# Append to backend/src/msai/services/nautilus/security_master/service.py

def compute_blake2b_digest_key(*parts: str) -> int:
    """blake2b digest of arbitrary string parts, rendered as a signed-int8.

    Shared primitive with :func:`compute_advisory_lock_key` but carries
    different semantics — the lock helper insists on
    ``(provider, raw_symbol, asset_class)``; this one accepts any number
    of string parts (joined with a null separator) and is used by
    callers that need a deterministic fingerprint of a composite key
    (e.g., the symbol-onboarding ``job_id_digest``).

    Both helpers produce identical digests for identical inputs, so a
    future audit that unifies them is mechanical.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.blake2b(
        "\x00".join(parts).encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFFFFFFFFFFFFFF
```

- [ ] **Step 2b: Implementation (append to `api/symbol_onboarding.py`).**

```python
# Append to backend/src/msai/api/symbol_onboarding.py

import asyncio
import contextlib
from datetime import UTC, datetime, date as _date
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import Path, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api._common import error_response  # shared envelope (T8-prime)
from msai.core.database import get_db
from msai.core.queue import get_redis_pool
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import (
    OnboardProgress,
    OnboardResponse,
    StatusResponse,
    SymbolStateRow,
    SymbolStatus,
)
from msai.services.nautilus.security_master.service import (
    compute_blake2b_digest_key,
)


async def _get_arq_pool():  # wrapper for test-patch seam
    return await get_redis_pool()


def _dedup_job_id(req: "OnboardRequest", *, extra_parts: tuple[str, ...] = ()) -> str:
    """Stable idempotency key for ``pool.enqueue_job(_job_id=...)``.

    Extra parts allow the repair flow to scope the digest to a specific
    parent run (so a repair on run A and a repair on run B with identical
    symbol sets don't collide).
    """
    canonical = [
        f"{s.symbol}|{s.asset_class}|{s.start.isoformat()}|{s.end.isoformat()}"
        for s in sorted(req.symbols, key=lambda s: (s.asset_class, s.symbol))
    ]
    digest = compute_blake2b_digest_key(
        "symbol_onboarding",
        req.watchlist_name,
        str(req.request_live_qualification),
        *extra_parts,
        *canonical,
    )
    return f"symbol-onboarding:{digest:x}"


async def _enqueue_and_persist_run(
    db: AsyncSession,
    *,
    digest_hex: str,
    job_id: str,
    reserved_id: UUID,
    watchlist_name: str,
    symbol_states: dict[str, Any],
    request_live_qualification: bool,
    cost_ceiling_usd: "Decimal | None",
) -> OnboardResponse | JSONResponse:
    """Shared enqueue-first-then-commit helper consumed by both ``/onboard``
    and ``/onboard/{id}/repair`` (iter-2 P1-B + P1-C fix).

    Invariant preserved: the DB row is committed AFTER arq accepts the
    enqueue so a Redis-down event can NEVER leave an orphan ``pending`` row
    that no worker will ever pick up. A second invariant: duplicate
    requests for an already-persisted digest collapse with ZERO side
    effects (no new enqueue, no new row); they just return the existing
    run_id. A third invariant: a narrow ``enqueue_job returns None`` race
    (another POST is committing its row concurrently) returns HTTP 409
    ``DUPLICATE_IN_FLIGHT`` after a single ~100 ms backoff re-select — we
    NEVER fabricate a ``reserved_id`` as if the job existed.

    Step order (hard-pinned by the P1-B findings):
    1. ``SELECT ... FOR UPDATE`` on the digest. If a row exists, short-
       circuit with **HTTP 200 OK** + existing run_id (no enqueue).
       Returned as ``JSONResponse`` so the route's decorator-level 202
       default does NOT override the dedup status code.
    2. Call ``pool.enqueue_job`` with the stable ``_job_id=job_id`` digest.
       - On exception → 503 ``QUEUE_UNAVAILABLE``, NO row committed.
       - On ``None`` return (arq dedup with sibling request) → sleep 100
         ms, re-SELECT; if row materializes → **HTTP 200 OK** +
         existing id (also as ``JSONResponse``); if still missing → 409
         ``DUPLICATE_IN_FLIGHT``.
       - On success → proceed to step 3.
    3. Commit the new row with ``job_id_digest = digest_hex``. On commit
       failure, rollback + best-effort ``pool.abort_job`` + re-raise 500.
       On commit success, return a plain ``OnboardResponse`` so FastAPI
       applies the route-decorator default (**HTTP 202 Accepted**).

    Status-code contract (iter-3 P1 fix): the two dedup branches MUST
    return ``JSONResponse(..., status_code=200)`` rather than the bare
    pydantic model — the route decorator pins 202 by default for OpenAPI
    documentation purposes, and FastAPI applies that default to any
    plain pydantic-model return. ``JSONResponse`` lets the helper override
    the per-call status while keeping the 202 default intact for the
    happy path. Integration tests at lines ~3493–3625 explicitly assert
    ``r1.status_code == 202`` (fresh enqueue) AND ``r2.status_code == 200``
    (duplicate short-circuit).
    """
    # Step 1: row-lock existing digest. ``FOR UPDATE`` serializes concurrent
    # POSTs of the same request so only one proceeds to enqueue.
    existing = (
        await db.execute(
            select(SymbolOnboardingRun)
            .where(SymbolOnboardingRun.job_id_digest == digest_hex)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        # iter-3 P1 fix: explicit 200 via ``JSONResponse`` because the
        # route decorator's ``status_code=HTTP_202_ACCEPTED`` would
        # otherwise mark this duplicate-short-circuit response as 202.
        return JSONResponse(
            status_code=200,
            content=OnboardResponse(
                run_id=existing.id,
                watchlist_name=existing.watchlist_name,
                status="pending",
            ).model_dump(mode="json"),
        )

    # Step 2: enqueue FIRST (before the DB insert). Redis failure must not
    # leave the DB dirty.
    try:
        pool = await _get_arq_pool()
    except Exception as exc:  # noqa: BLE001 — redis connection errors are heterogeneous
        log.warning("onboarding_enqueue_connection_failed", error=repr(exc))
        return error_response(503, "QUEUE_UNAVAILABLE", "Job queue is unavailable.")

    try:
        job = await pool.enqueue_job(
            "run_symbol_onboarding",
            run_id=str(reserved_id),
            _job_id=job_id,
            _queue_name="msai:ingest",
        )
    except Exception as exc:  # noqa: BLE001 — heterogeneous broker exceptions
        log.warning("onboarding_enqueue_failed", error=repr(exc))
        return error_response(503, "QUEUE_UNAVAILABLE", "Job queue rejected the submission.")

    if job is None:
        # arq-level dedup: a sibling POST owns this digest. Re-SELECT once
        # after a short backoff to let the sibling commit its row. If the
        # row materializes → 200 OK with sibling's id. If STILL missing
        # → 409 DUPLICATE_IN_FLIGHT — caller retries in 1 s and hits the
        # fast-path. NEVER fabricate a ``reserved_id``: returning a uuid
        # for a run that was never persisted breaks ``/status`` polling
        # (404 forever) and is exactly the orphan-row footgun this
        # rewrite was commissioned to close.
        await asyncio.sleep(0.1)
        existing = (
            await db.execute(
                select(SymbolOnboardingRun).where(
                    SymbolOnboardingRun.job_id_digest == digest_hex
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # iter-3 P1 fix: explicit 200 via ``JSONResponse`` so the
            # route decorator's 202 default is overridden on this
            # dedup-via-race-resolution branch.
            return JSONResponse(
                status_code=200,
                content=OnboardResponse(
                    run_id=existing.id,
                    watchlist_name=existing.watchlist_name,
                    status="pending",
                ).model_dump(mode="json"),
            )
        return error_response(
            409,
            "DUPLICATE_IN_FLIGHT",
            "Another onboarding request for the same watchlist is being submitted; retry in ~1s.",
        )

    # Step 3: commit. Failure here must NOT leak an enqueued job.
    run = SymbolOnboardingRun(
        id=reserved_id,
        watchlist_name=watchlist_name,
        status=SymbolOnboardingRunStatus.PENDING,
        symbol_states=symbol_states,
        request_live_qualification=request_live_qualification,
        cost_ceiling_usd=cost_ceiling_usd,
        job_id_digest=digest_hex,
    )
    db.add(run)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 — unique-index race, constraint violation, etc.
        await db.rollback()
        with contextlib.suppress(Exception):
            await pool.abort_job(job.job_id)
        raise
    await db.refresh(run)
    # Happy path: return the plain pydantic model so FastAPI applies the
    # route-decorator default of ``status_code=HTTP_202_ACCEPTED``. The
    # two dedup branches above return ``JSONResponse`` with an explicit
    # 200 status to override that default per-call.
    return OnboardResponse(
        run_id=run.id,
        watchlist_name=run.watchlist_name,
        status="pending",
    )


# Default status_code is 202 (Accepted) — applied to the happy-path
# pydantic-model return from ``_enqueue_and_persist_run``. The two dedup
# branches inside the helper return ``JSONResponse(status_code=200)``
# explicitly so duplicate POSTs short-circuit with 200 OK rather than
# the misleading 202. iter-3 P1 fix.
@router.post(
    "/onboard", response_model=OnboardResponse, status_code=status.HTTP_202_ACCEPTED
)
async def onboard(
    request: "OnboardRequest",
    _user: Any = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardResponse | JSONResponse:
    """iter-2 P1-B fix — enqueue-first-then-commit via shared helper.

    Behavior:
    - First POST for a given ``(watchlist_name, symbol_set, live_qual)``
      tuple: enqueues arq job, commits row, returns 202 + run_id.
    - Exact-duplicate POST (same digest): 200 OK + existing run_id, no
      new work.
    - Narrow race (sibling POST hasn't committed yet): 409
      ``DUPLICATE_IN_FLIGHT`` after a single 100 ms backoff.
    - Redis unavailable: 503 ``QUEUE_UNAVAILABLE``, zero DB rows committed.
    """
    job_id = _dedup_job_id(request)
    digest_hex = job_id.removeprefix("symbol-onboarding:")
    symbol_states: dict[str, Any] = {
        spec.symbol: {
            "symbol": spec.symbol,
            "asset_class": spec.asset_class,
            "start": spec.start.isoformat(),
            "end": spec.end.isoformat(),
            "status": "not_started",
            "step": "pending",
            "error": None,
        }
        for spec in request.symbols
    }
    return await _enqueue_and_persist_run(
        db,
        digest_hex=digest_hex,
        job_id=job_id,
        reserved_id=uuid4(),
        watchlist_name=request.watchlist_name,
        symbol_states=symbol_states,
        request_live_qualification=request.request_live_qualification,
        cost_ceiling_usd=request.cost_ceiling_usd,
    )


@router.get("/onboard/{run_id}/status", response_model=StatusResponse)
async def onboard_status(
    run_id: UUID = Path(...),
    _user: Any = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StatusResponse | JSONResponse:
    row = (
        await db.execute(
            select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return error_response(
            status_code=404, code="NOT_FOUND", message=f"Run {run_id} not found"
        )

    per_symbol = [
        SymbolStateRow(
            symbol=entry["symbol"],
            asset_class=entry["asset_class"],
            start=_date.fromisoformat(entry["start"]),
            end=_date.fromisoformat(entry["end"]),
            status=entry.get("status", "not_started"),
            step=entry.get("step", "pending"),
            error=entry.get("error"),
            next_action=_suggest_next_action(entry),
        )
        for entry in row.symbol_states.values()
    ]
    per_symbol.sort(key=lambda s: (s.asset_class, s.symbol))

    return StatusResponse(
        run_id=row.id,
        watchlist_name=row.watchlist_name,
        status=row.status.value,
        progress=_summarize(per_symbol),
        per_symbol=per_symbol,
        estimated_cost_usd=row.estimated_cost_usd,
        actual_cost_usd=row.actual_cost_usd,
    )


# Default status_code is 202 (Accepted) for fresh-enqueue happy path;
# dedup branches inside ``_enqueue_and_persist_run`` return
# ``JSONResponse(status_code=200)`` so a repair-of-already-repaired call
# short-circuits with 200 rather than the misleading 202. iter-3 P1 fix.
@router.post(
    "/onboard/{run_id}/repair", response_model=OnboardResponse, status_code=status.HTTP_202_ACCEPTED
)
async def onboard_repair(
    run_id: UUID = Path(...),
    body: dict[str, list[str]] | None = None,
    _user: Any = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OnboardResponse | JSONResponse:
    parent = (
        await db.execute(
            select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id)
        )
    ).scalar_one_or_none()
    if parent is None:
        return error_response(
            status_code=404, code="NOT_FOUND", message=f"Run {run_id} not found"
        )
    if parent.status == SymbolOnboardingRunStatus.IN_PROGRESS:
        return error_response(
            status_code=409,
            code="PARENT_RUN_IN_PROGRESS",
            message="Cannot repair while parent run is still in progress.",
        )

    target_symbols = (body or {}).get("symbols") or [
        entry["symbol"]
        for entry in parent.symbol_states.values()
        if entry.get("status") == "failed"
    ]
    if not target_symbols:
        return error_response(
            status_code=422,
            code="NO_FAILED_SYMBOLS",
            message="Nothing to repair — parent has no failed symbols.",
        )

    child_states: dict[str, Any] = {}
    for sym in target_symbols:
        parent_entry = parent.symbol_states.get(sym)
        if parent_entry is None:
            return error_response(
                status_code=422,
                code="UNKNOWN_SYMBOL",
                message=f"Symbol {sym!r} is not part of parent run {run_id}.",
            )
        child_states[sym] = {
            "symbol": sym,
            "asset_class": parent_entry["asset_class"],
            "start": parent_entry["start"],
            "end": parent_entry["end"],
            "status": "not_started",
            "step": "pending",
            "error": None,
        }

    # Repair runs get a parent-scoped digest so repeated repair calls on
    # the same parent collapse via arq dedup (same symbol list on the
    # same parent = same work). iter-2 P1-C fix: route through the SAME
    # ``_enqueue_and_persist_run`` helper as ``/onboard`` so the ordering
    # guarantees (enqueue-first-then-commit, duplicate short-circuit,
    # ``DUPLICATE_IN_FLIGHT`` for narrow race) apply identically to the
    # repair path. The prior inline ``INSERT → commit → enqueue`` order
    # could leak orphan ``pending`` rows on Redis down.
    child_digest = compute_blake2b_digest_key(
        "symbol_onboarding",
        f"{parent.watchlist_name}-repair",
        f"repair:{parent.id}",
        *sorted(target_symbols),
    )
    child_digest_hex = f"{child_digest:x}"
    child_job_id = f"symbol-onboarding:{child_digest_hex}"

    return await _enqueue_and_persist_run(
        db,
        digest_hex=child_digest_hex,
        job_id=child_job_id,
        reserved_id=uuid4(),
        watchlist_name=f"{parent.watchlist_name}-repair",
        symbol_states=child_states,
        request_live_qualification=parent.request_live_qualification,
        cost_ceiling_usd=parent.cost_ceiling_usd,
    )


def _summarize(per_symbol: list[SymbolStateRow]) -> OnboardProgress:
    total = len(per_symbol)
    succeeded = sum(1 for s in per_symbol if s.status == SymbolStatus.SUCCEEDED.value)
    failed = sum(1 for s in per_symbol if s.status == SymbolStatus.FAILED.value)
    in_progress = sum(1 for s in per_symbol if s.status == SymbolStatus.IN_PROGRESS.value)
    not_started = total - succeeded - failed - in_progress
    return OnboardProgress(
        total=total,
        succeeded=succeeded,
        failed=failed,
        in_progress=in_progress,
        not_started=not_started,
    )


def _suggest_next_action(entry: dict[str, Any]) -> str | None:
    status_ = entry.get("status")
    if status_ != "failed":
        return None
    error = entry.get("error") or {}
    code = error.get("code")
    mapping = {
        "BOOTSTRAP_AMBIGUOUS": "Disambiguate with exact instrument id + re-onboard.",
        "BOOTSTRAP_UNAUTHORIZED": "Check Databento dataset entitlement.",
        "BOOTSTRAP_UNMAPPED_VENUE": "File issue — unknown Databento venue MIC.",
        "COVERAGE_INCOMPLETE": "Inspect ingest logs; retry via /repair.",
        "IB_TIMEOUT": "Retry with request_live_qualification=false then rerun IB later.",
        "IB_UNAVAILABLE": "Confirm IB Gateway container is running + entitled.",
        "INGEST_FAILED": "Retry via /repair after checking Databento quota.",
    }
    return mapping.get(code)
```

- [ ] **Step 3: Run tests + ruff + mypy + commit.**

```bash
cd backend && uv run pytest tests/integration/api/test_symbol_onboarding_api.py -v
uv run ruff check src/msai/api/symbol_onboarding.py tests/integration/api/test_symbol_onboarding_api.py
uv run mypy --strict src/msai/api/symbol_onboarding.py
git add backend/src/msai/api/symbol_onboarding.py backend/tests/integration/api/test_symbol_onboarding_api.py
git commit -m "feat(symbol-onboarding): T9 POST /onboard + GET /status + POST /repair endpoints with idempotency"
```

---

### Task 10: `GET /api/v1/symbols/readiness` (window-scoped per pin-#3 amendment)

**Files:**

- Modify: `backend/src/msai/api/symbol_onboarding.py`
- Create: `backend/tests/integration/api/test_symbol_onboarding_readiness.py`

**Dependencies:** T5 (coverage), T9 (router). Reuses: `InstrumentRegistry.find_by_alias`, `SecurityMaster.asset_class_for_alias`.

**Writes:** `backend/src/msai/api/symbol_onboarding.py` (modify), `backend/src/msai/services/nautilus/security_master/service.py` (add `find_active_aliases` helper — NEW readiness-aggregation code, NOT a wrapper), `backend/tests/integration/api/test_symbol_onboarding_readiness.py`.

**Honest scope note (iter-1 P1 fix).** Earlier drafts called `SecurityMaster.find_active_aliases(...)` a "trivial wrapper over `resolve_for_backtest`". It is not: `resolve_for_backtest` returns an alias string or raises; `lookup_for_live` returns a live contract spec. Neither one aggregates the three readiness states (`registered`, `backtest_data_available`, `live_qualified`) that this endpoint needs to answer. `find_active_aliases` is a **new readiness-aggregation helper** (~60 LOC: query `instrument_definitions` + `instrument_aliases` with an active-at-today join, return a typed `AliasResolution` dataclass with `instrument_uid`, `primary_provider`, `has_ib_alias`, `coverage_summary_hint` string-builder). The T10 commit adds it explicitly.

Contract:

- `GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity&start=2024-01-01&end=2024-12-31`.
- `asset_class` query param accepts the user-facing / registry taxonomy (`equity | futures | fx | option`). The endpoint translates internally via `normalize_asset_class_for_ingest(...)` before calling the T5 coverage scanner.
- Returns `ReadinessResponse` with three readiness states:
  - `registered: bool` — always truthful (registry row exists).
  - `backtest_data_available: bool | None` — **only `true` when a full window is provided AND coverage is `full`**. `None` when no window was supplied; `false` when coverage is `gapped` or `none`. (Contrarian's pin-#3 fatal-flaw correction — never claim "data available" without a scope.)
  - `live_qualified: bool` — true iff registry has an IB alias row for this `(instrument_uid, provider=interactive_brokers)`.
- Response also includes `covered_range` + `missing_ranges` + `coverage_summary` (human-friendly text used when no window in scope, e.g. `"Coverage last scanned: full through 2024-12-31 (Databento)"`).
- 404 on unregistered symbol.

- [ ] **Step 1: Test.**

```python
# backend/tests/integration/api/test_symbol_onboarding_readiness.py
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_readiness_with_window_returns_scoped_availability(
    client: TestClient,
    auth_headers,
    seeded_registry_spy,
    tmp_parquet_root,
    settings_override,
):
    # Parquet path uses INGEST taxonomy (``stocks``); query uses registry
    # taxonomy (``equity``). Endpoint translates before the coverage scan.
    base = tmp_parquet_root / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")
    settings_override(data_root=str(tmp_parquet_root))

    resp = client.get(
        "/api/v1/symbols/readiness",
        params={
            "symbol": "SPY",
            "asset_class": "equity",
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["registered"] is True
    assert data["backtest_data_available"] is True
    assert data["coverage_status"] == "full"
    assert data["missing_ranges"] == []


def test_readiness_without_window_returns_null_available(
    client: TestClient, auth_headers, seeded_registry_spy
):
    resp = client.get(
        "/api/v1/symbols/readiness",
        params={"symbol": "SPY", "asset_class": "equity"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["backtest_data_available"] is None  # no scope → no truthful claim
    assert data["coverage_status"] is None


def test_readiness_404_for_unregistered_symbol(client: TestClient, auth_headers):
    resp = client.get(
        "/api/v1/symbols/readiness",
        params={"symbol": "NOTREAL", "asset_class": "equity"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
```

- [ ] **Step 2a: Add `AliasResolution` + `find_active_aliases` to `security_master/service.py`.**

This is NEW readiness-aggregation code (iter-1 honest-scoping fix). Returns a typed dataclass instead of a scalar alias string:

```python
# Append to backend/src/msai/services/nautilus/security_master/service.py

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AliasResolution:
    instrument_uid: uuid.UUID | None
    primary_provider: str  # e.g. "databento"
    has_ib_alias: bool
    ingest_asset_class: str  # already normalized to the ingest taxonomy

    def coverage_summary_hint(self) -> str | None:
        # Short human-friendly string used when no window is in scope.
        if self.instrument_uid is None:
            return None
        return f"Registered via {self.primary_provider}; live-qualified: {self.has_ib_alias}"


class SecurityMaster:
    # ... existing methods unchanged ...

    async def find_active_aliases(
        self, *, symbol: str, asset_class: str
    ) -> AliasResolution:
        """Aggregate readiness view for a ``(symbol, asset_class)`` pair.

        ``asset_class`` here is the registry/user-facing taxonomy
        (``equity | futures | fx | option``). Internally maps to the
        registry spec taxonomy for the SELECT and to the ingest taxonomy
        for the caller's downstream use.
        """
        # (~60 LOC implementation: JOIN instrument_definitions +
        # instrument_aliases WHERE effective_to IS NULL, group by
        # instrument_uid + provider, emit AliasResolution.)
        raise NotImplementedError  # implementation filled in at commit time
```

- [ ] **Step 2b: Implementation (append to `api/symbol_onboarding.py`).**

```python
from fastapi import Query

from msai.schemas.symbol_onboarding import ReadinessResponse
from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.symbol_onboarding import normalize_asset_class_for_ingest
from msai.services.symbol_onboarding.coverage import compute_coverage


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(
    symbol: str = Query(..., min_length=1, max_length=20),
    asset_class: str = Query(..., min_length=1, max_length=32),
    start: _date | None = Query(default=None),
    end: _date | None = Query(default=None),
    _user: Any = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReadinessResponse | JSONResponse:
    master = SecurityMaster(db)
    resolution = await master.find_active_aliases(
        symbol=symbol, asset_class=asset_class
    )
    if resolution.instrument_uid is None:
        return error_response(
            status_code=404,
            code="NOT_FOUND",
            message=f"Symbol {symbol!r} not registered for asset_class={asset_class!r}",
        )

    live_qualified = resolution.has_ib_alias
    provider = resolution.primary_provider  # e.g. "databento"
    ingest_asset = normalize_asset_class_for_ingest(asset_class)

    if start is None or end is None:
        return ReadinessResponse(
            instrument_uid=resolution.instrument_uid,
            registered=True,
            provider=provider,
            backtest_data_available=None,
            coverage_status=None,
            covered_range=None,
            missing_ranges=[],
            live_qualified=live_qualified,
            coverage_summary=resolution.coverage_summary_hint(),
        )

    report = await compute_coverage(
        asset_class=ingest_asset,
        symbol=symbol,
        start=start,
        end=end,
        data_root=Path(settings.data_root),
    )
    return ReadinessResponse(
        instrument_uid=resolution.instrument_uid,
        registered=True,
        provider=provider,
        backtest_data_available=(report.status == "full"),
        coverage_status=report.status,
        covered_range=report.covered_range,
        missing_ranges=[
            {"start": s.isoformat(), "end": e.isoformat()}
            for s, e in report.missing_ranges
        ],
        live_qualified=live_qualified,
        coverage_summary=None,
    )
```

> **Scope note:** see "Honest scope note" at the top of this task — `find_active_aliases` is NEW readiness-aggregation code (~60 LOC), not a wrapper. The Step 2a block above carries the full signature; implementation commit lives alongside the router edit.

- [ ] **Step 3: Run tests + ruff + mypy + commit.**

```bash
cd backend && uv run pytest tests/integration/api/test_symbol_onboarding_readiness.py -v
uv run ruff check src/msai/api/symbol_onboarding.py src/msai/services/nautilus/security_master/service.py
uv run mypy --strict src/msai/api/symbol_onboarding.py src/msai/services/nautilus/security_master/service.py
git add backend/src/msai/api/symbol_onboarding.py backend/src/msai/services/nautilus/security_master/service.py backend/tests/integration/api/test_symbol_onboarding_readiness.py
git commit -m "feat(symbol-onboarding): T10 GET /readiness — window-scoped 3-state answer (pin-#3 amendment)"
```

---

### Task 11: Wire router into `main.py` + drop `asset_universe` import

**Files:**

- Modify: `backend/src/msai/main.py`
- Create: `backend/tests/integration/api/test_symbol_onboarding_routes_registered.py`

**Dependencies:** T8, T9, T10 (router complete), T14 (ordering — `asset_universe.py` is removed in T14).

**Writes:** `backend/src/msai/main.py`, `backend/tests/integration/api/test_symbol_onboarding_routes_registered.py`.

Contract:

- `include_router(symbol_onboarding_router)` at the same section that currently mounts other `/api/v1/*` routers.
- Remove the existing `from msai.api import asset_universe` import + `app.include_router(asset_universe.router)` line. Router file itself is deleted in T14.

- [ ] **Step 1: Test.**

```python
# backend/tests/integration/api/test_symbol_onboarding_routes_registered.py
from fastapi.testclient import TestClient


def test_onboard_routes_mounted(client: TestClient, auth_headers):
    # All four routes answer (even if they fail downstream).
    paths = [
        ("POST", "/api/v1/symbols/onboard/dry-run"),
        ("POST", "/api/v1/symbols/onboard"),
        ("GET", "/api/v1/symbols/onboard/00000000-0000-0000-0000-000000000000/status"),
        ("GET", "/api/v1/symbols/readiness"),
    ]
    for method, path in paths:
        resp = client.request(method, path, headers=auth_headers)
        # Any 4xx / 2xx is acceptable — only 404-not-registered-route fails the test.
        assert resp.status_code != 404 or resp.json().get("error", {}).get("code") != "NOT_FOUND_ROUTE", path


def test_asset_universe_routes_are_gone(client: TestClient, auth_headers):
    resp = client.get("/api/v1/universe", headers=auth_headers)
    assert resp.status_code == 404  # router deleted, not just deprecated
```

- [ ] **Step 2: Edit `main.py`.** Replace the `asset_universe` import and `include_router(asset_universe.router)` call with:

```python
from msai.api import symbol_onboarding as symbol_onboarding_api
# ...
app.include_router(symbol_onboarding_api.router)
```

- [ ] **Step 3: Run tests + commit.**

```bash
cd backend && uv run pytest tests/integration/api/test_symbol_onboarding_routes_registered.py -v
git add backend/src/msai/main.py backend/tests/integration/api/test_symbol_onboarding_routes_registered.py
git commit -m "feat(symbol-onboarding): T11 wire /api/v1/symbols routes into main app + drop asset_universe import"
```

---

### Task 12: `msai symbols` CLI sub-app

**Files:**

- Create: `backend/src/msai/cli_symbols.py`
- Modify: `backend/src/msai/cli.py` (register sub-app)
- Create: `backend/tests/unit/cli/test_cli_symbols.py`

**Dependencies:** T3 (manifest parser), T9 (endpoints). Reuses: `_api_call` helper used by other CLI sub-apps.

**Writes:** `backend/src/msai/cli_symbols.py`, `backend/src/msai/cli.py` (modify), `backend/tests/unit/cli/test_cli_symbols.py`.

Contract:

- `msai symbols onboard --manifest <path>.yaml [--live-qualify] [--cost-ceiling-usd N] [--dry-run]` — parses manifest, POSTs to `/api/v1/symbols/onboard` (or `/dry-run` when `--dry-run`). On dry-run, prints cost summary to stdout; on real run, prints the `run_id` and hints at `msai symbols status`.
- `msai symbols status <run_id> [--watch]` — polls `/api/v1/symbols/onboard/{run_id}/status`. With `--watch`, polls every 5s until terminal; renders a per-symbol table (symbol, asset_class, status, step, next_action).
- `msai symbols repair <run_id> [--symbols SPY,AAPL]` — POST `/api/v1/symbols/onboard/{run_id}/repair`; prints the new `run_id`.
- Bypasses the HTTP layer? **No** — matches the PRD's "API-first" ordering rule; CLI uses the same envelope the UI will use. Exit code 0 on PASS, 1 on COMPLETED_WITH_FAILURES, 2 on FAILED, 3 on infra error.

- [ ] **Step 1: Test.**

```python
# backend/tests/unit/cli/test_cli_symbols.py
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from typer.testing import CliRunner

from msai.cli_symbols import app as symbols_app


def test_onboard_dry_run_prints_cost_summary(tmp_path: Path):
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    fake_response = {
        "watchlist_name": "core",
        "dry_run": True,
        "estimated_cost_usd": 0.42,
        "estimate_basis": "databento.metadata.get_cost (1m OHLCV)",
        "estimate_confidence": "high",
        "symbol_count": 1,
        "breakdown": [{"symbol": "SPY", "dataset": "XNAS.ITCH", "usd": 0.42}],
    }
    with patch("msai.cli_symbols._api_call", return_value=fake_response):
        result = runner.invoke(
            symbols_app, ["onboard", "--manifest", str(manifest), "--dry-run"]
        )
    assert result.exit_code == 0
    assert "0.42" in result.stdout
    assert "high" in result.stdout


def test_status_exit_code_reflects_run_state(tmp_path: Path):
    runner = CliRunner()
    resp = {
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "watchlist_name": "core",
        "status": "completed_with_failures",
        "progress": {"total": 2, "succeeded": 1, "failed": 1, "in_progress": 0, "not_started": 0},
        "per_symbol": [
            {"symbol": "SPY", "asset_class": "equity", "start": "2024-01-01", "end": "2024-12-31", "status": "succeeded", "step": "ib_skipped", "error": None, "next_action": None},
            {"symbol": "AAPL", "asset_class": "equity", "start": "2024-01-01", "end": "2024-12-31", "status": "failed", "step": "ingest", "error": {"code": "INGEST_FAILED", "message": "rate limit"}, "next_action": "Retry via /repair after checking Databento quota."},
        ],
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
    }
    with patch("msai.cli_symbols._api_call", return_value=resp):
        result = runner.invoke(
            symbols_app, ["status", "123e4567-e89b-12d3-a456-426614174000"]
        )
    assert result.exit_code == 1  # COMPLETED_WITH_FAILURES
    assert "AAPL" in result.stdout
    assert "INGEST_FAILED" in result.stdout


def test_cost_ceiling_usd_rejects_more_than_two_decimals(tmp_path: Path):
    """iter-2 P1-D fix + iter-3 P2 strengthening: ``--cost-ceiling-usd``
    with more than 2 decimal places must be rejected at the CLI boundary
    (Numeric(12,2) column).

    Before iter-2: ``float`` typer option silently truncated at IEEE-754
    precision and the server ``quantize(Decimal('0.01'))`` swallowed the
    remainder — caller never saw the rounding.

    Iter-3 strengthening: the iter-2 ``quantized != raw`` check is a
    VALUE comparison; trailing-zero overprecision (``123.450``) snuck
    through. The new check is on the Decimal exponent so ``123.456`` AND
    ``123.450`` are both rejected.
    """
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    with patch("msai.cli_symbols._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            [
                "onboard",
                "--manifest",
                str(manifest),
                "--cost-ceiling-usd",
                "123.456",
            ],
        )
    assert result.exit_code != 0
    assert "2 decimal places" in result.stdout or "2 decimal places" in (
        result.stderr or ""
    )
    api_mock.assert_not_called(), "No HTTP call on CLI validation failure."


def test_cost_ceiling_usd_rejects_trailing_zero_overprecision(tmp_path: Path):
    """iter-3 P2 fix: trailing-zero overprecision (``123.450``) must be
    rejected at the CLI boundary even though
    ``Decimal("123.450") == Decimal("123.45")`` (value-equal). The
    exponent check catches the source-string precision regardless of
    the trailing zero.
    """
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    with patch("msai.cli_symbols._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            [
                "onboard",
                "--manifest",
                str(manifest),
                "--cost-ceiling-usd",
                "123.450",
            ],
        )
    assert result.exit_code != 0
    assert "2 decimal places" in result.stdout or "2 decimal places" in (
        result.stderr or ""
    )
    api_mock.assert_not_called(), (
        "No HTTP call on CLI validation failure — trailing-zero "
        "overprecision must be rejected before any network call."
    )


def test_cost_ceiling_usd_accepts_well_formed_decimal(tmp_path: Path):
    """iter-2 P1-D fix (positive path): ``--cost-ceiling-usd 123.45`` is
    accepted and forwarded as a JSON string so pydantic parses with full
    precision (no IEEE-754 detour)."""
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    fake_response = {"run_id": "abc", "watchlist_name": "core", "status": "pending"}
    with patch("msai.cli_symbols._api_call", return_value=fake_response) as api_mock:
        result = runner.invoke(
            symbols_app,
            [
                "onboard",
                "--manifest",
                str(manifest),
                "--cost-ceiling-usd",
                "123.45",
            ],
        )
    assert result.exit_code == 0
    _, kwargs = api_mock.call_args
    assert kwargs["json"]["cost_ceiling_usd"] == "123.45"
```

- [ ] **Step 2: Implementation.**

```python
# backend/src/msai/cli_symbols.py
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from msai.cli_helpers import _api_call  # existing helper used by every sub-app
from msai.services.symbol_onboarding.manifest import parse_manifest_file

app = typer.Typer(
    name="symbols", help="Symbol onboarding — manifest-driven universe bootstrap."
)
console = Console()


@app.command()
def onboard(
    manifest: Path = typer.Option(..., "--manifest", exists=True, resolve_path=True),
    live_qualify: bool = typer.Option(False, "--live-qualify"),
    cost_ceiling_usd: str | None = typer.Option(
        None,
        "--cost-ceiling-usd",
        help="Hard spend stop in USD; max 2 decimal places (matches Numeric(12,2)).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """iter-2 P1-D fix: accept ``--cost-ceiling-usd`` as a string and coerce
    to ``Decimal`` locally. The server schema pins ``Numeric(12,2)``; the
    previous ``float`` typer option silently round-tripped values like
    ``123.456`` through IEEE-754 and then through
    ``Numeric.quantize(Decimal("0.01"))`` on the server — caller never saw
    the precision loss. Reject >2-decimal inputs at the CLI boundary so
    the user gets a clear error immediately.
    """
    parsed = parse_manifest_file(manifest)
    body: dict[str, Any] = {
        "watchlist_name": parsed.watchlist_name,
        "symbols": [
            {
                "symbol": s.symbol,
                "asset_class": s.asset_class,
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
            }
            for s in parsed.symbols
        ],
        "request_live_qualification": live_qualify,
    }
    if cost_ceiling_usd is not None:
        from decimal import Decimal, InvalidOperation  # noqa: PLC0415

        try:
            raw = Decimal(cost_ceiling_usd)
        except InvalidOperation as exc:
            raise typer.BadParameter(
                f"--cost-ceiling-usd must be a decimal number (got {cost_ceiling_usd!r})"
            ) from exc
        if raw < 0:
            raise typer.BadParameter("--cost-ceiling-usd must be non-negative.")
        # iter-3 P2 fix: ``raw.quantize(Decimal("0.01")) == raw`` is a
        # VALUE comparison — ``Decimal("123.450") == Decimal("123.45")``
        # is True even though the source string carried 3 decimal places.
        # The original ``quantized != raw`` check would let trailing-zero
        # overprecision (``--cost-ceiling-usd 123.450``) sneak through
        # silently. Use the exponent of the original Decimal — it equals
        # ``-N`` where N is the number of declared decimal places — to
        # reject ANY input with more than 2 decimal places, including
        # trailing-zero forms like ``123.450``.
        if raw.as_tuple().exponent < -2:
            raise typer.BadParameter(
                "--cost-ceiling-usd supports at most 2 decimal places "
                f"(got {cost_ceiling_usd!r}, exponent={raw.as_tuple().exponent}); "
                "use e.g. 123.45."
            )
        quantized = raw.quantize(Decimal("0.01"))
        # Send as a JSON string so pydantic parses with full precision.
        body["cost_ceiling_usd"] = str(quantized)

    if dry_run:
        data = _api_call("POST", "/api/v1/symbols/onboard/dry-run", json=body)
        _print_cost_estimate(data)
        raise typer.Exit(code=0)

    data = _api_call("POST", "/api/v1/symbols/onboard", json=body)
    console.print(f"Run queued: [bold]{data['run_id']}[/bold]")
    console.print(
        f"Next: [green]msai symbols status {data['run_id']} --watch[/green]"
    )
    raise typer.Exit(code=0)


@app.command()
def status(
    run_id: str = typer.Argument(...),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    while True:
        data = _api_call("GET", f"/api/v1/symbols/onboard/{run_id}/status")
        _render_status_table(data)
        if not watch or data["status"] in {"completed", "completed_with_failures", "failed"}:
            break
        time.sleep(5)
    _exit_for_status(data["status"])


@app.command()
def repair(
    run_id: str = typer.Argument(...),
    symbols: str | None = typer.Option(None, "--symbols", help="Comma-separated."),
) -> None:
    body: dict[str, Any] = {}
    if symbols:
        body["symbols"] = [s.strip() for s in symbols.split(",") if s.strip()]
    data = _api_call("POST", f"/api/v1/symbols/onboard/{run_id}/repair", json=body)
    console.print(f"Repair run queued: [bold]{data['run_id']}[/bold]")


def _render_status_table(data: dict[str, Any]) -> None:
    console.print(
        f"Run [bold]{data['run_id']}[/bold] — watchlist "
        f"[cyan]{data['watchlist_name']}[/cyan] — status [yellow]{data['status']}[/yellow]"
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("symbol", "asset_class", "status", "step", "error", "next_action"):
        table.add_column(col)
    for row in data["per_symbol"]:
        err = (row.get("error") or {}).get("code") or ""
        table.add_row(
            row["symbol"],
            row["asset_class"],
            row["status"],
            row["step"],
            err,
            row.get("next_action") or "",
        )
    console.print(table)


def _print_cost_estimate(data: dict[str, Any]) -> None:
    console.print(
        f"Dry-run estimate: [bold]${data['estimated_cost_usd']:.2f}[/bold] "
        f"({data['estimate_confidence']} confidence) — {data['symbol_count']} symbols"
    )
    console.print(f"Basis: {data['estimate_basis']}")


def _exit_for_status(run_status: str) -> None:
    mapping = {
        "completed": 0,
        "completed_with_failures": 1,
        "failed": 2,
    }
    raise typer.Exit(code=mapping.get(run_status, 3))
```

Then register in `backend/src/msai/cli.py`:

```python
from msai.cli_symbols import app as symbols_app
# ...
app.add_typer(symbols_app, name="symbols")
```

- [ ] **Step 3: Run tests + ruff + mypy + commit.**

```bash
cd backend && uv run pytest tests/unit/cli/test_cli_symbols.py -v
uv run ruff check src/msai/cli_symbols.py src/msai/cli.py
uv run mypy --strict src/msai/cli_symbols.py
git add backend/src/msai/cli_symbols.py backend/src/msai/cli.py backend/tests/unit/cli/test_cli_symbols.py
git commit -m "feat(symbol-onboarding): T12 msai symbols CLI (onboard/status/repair, --dry-run, --watch)"
```

---

### Task 13: 3 Prometheus metrics

**Files:**

- Modify: `backend/src/msai/services/observability/trading_metrics.py`
- Create: `backend/tests/unit/observability/test_onboarding_metrics.py`

**Dependencies:** T0 (test fixtures available). **Chronological ordering (iter-1 P1 fix): T13 ships BEFORE T6** — see the Dispatch Plan. Reason: T6's orchestrator imports `onboarding_ib_timeout_total` and T7's worker imports `onboarding_symbol_duration_seconds` + `onboarding_jobs_total`. If T13 landed after those, the imports would fail at T6/T7 commit time. Keep the "T13" label (number is not a chronology contract), but execute this task immediately after T0 and before T6.

**Writes:** `backend/src/msai/services/observability/trading_metrics.py` (modify), `backend/tests/unit/observability/test_onboarding_metrics.py`.

Contract: add three metrics, each constructed via the existing `get_registry()` pattern (matches PR #41/#44 precedent).

- `msai_onboarding_jobs_total{status}` — Counter. Labels: `status ∈ {completed, completed_with_failures, failed}`.
- `msai_onboarding_symbol_duration_seconds{step}` — Histogram. Labels: `step ∈ {bootstrap, ingest, coverage, ib_qualify, completed, ib_skipped, coverage_failed}` (matches the canonical `SymbolStepStatus` vocabulary from T2). Buckets: `(1, 5, 15, 30, 60, 120, 300, 600)`.
- `msai_onboarding_ib_timeout_total` — Counter (no labels). Incremented once per `IB_TIMEOUT` error code (the orchestrator calls `.inc()` when `asyncio.wait_for(...)` raises `TimeoutError`).

- [ ] **Step 1: Test (asserts `registry.render()` carries the expected metric names after a single observation).**

```python
# backend/tests/unit/observability/test_onboarding_metrics.py
from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import (
    onboarding_ib_timeout_total,
    onboarding_jobs_total,
    onboarding_symbol_duration_seconds,
)


def test_onboarding_metrics_render():
    onboarding_jobs_total.labels(status="completed").inc()
    onboarding_symbol_duration_seconds.labels(step="ingest").observe(12.3)
    onboarding_ib_timeout_total.inc()
    rendered = get_registry().render()
    assert "msai_onboarding_jobs_total" in rendered
    assert "msai_onboarding_symbol_duration_seconds" in rendered
    assert "msai_onboarding_ib_timeout_total" in rendered
```

- [ ] **Step 2: Implementation — append to `trading_metrics.py`.**

```python
# backend/src/msai/services/observability/trading_metrics.py (append)
from msai.services.observability import get_registry

_r = get_registry()

onboarding_jobs_total = _r.counter(
    "msai_onboarding_jobs_total",
    "Terminal outcome counter for symbol-onboarding runs.",
)
onboarding_symbol_duration_seconds = _r.histogram(
    "msai_onboarding_symbol_duration_seconds",
    "Per-step wall-clock duration for each symbol in a run.",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)
onboarding_ib_timeout_total = _r.counter(
    "msai_onboarding_ib_timeout_total",
    "IB qualification timeouts while onboarding symbols.",
)
```

- [ ] **Step 3: Run tests + commit.**

```bash
cd backend && uv run pytest tests/unit/observability/test_onboarding_metrics.py -v
git add backend/src/msai/services/observability/trading_metrics.py backend/tests/unit/observability/test_onboarding_metrics.py
git commit -m "feat(symbol-onboarding): T13 3 Prometheus metrics (jobs, per-step histogram, IB timeout counter)"
```

---

### Task 14: Delete `api/asset_universe.py` + prune HTTP-route tests

**Files:**

- Delete: `backend/src/msai/api/asset_universe.py`
- Delete: `backend/tests/integration/api/test_asset_universe*.py` (route-facing only — non-HTTP model/service tests stay if reused, which they are not).
- Modify: anywhere else that imports `asset_universe` (router already dropped in T11; grep for residuals).

**Dependencies:** T11 (router removal landed first).

**Writes:** deletions only.

Contract: zero grep-able callers confirmed before the PRD was written. This task closes the Maintainer's "model-fracture" binding objection from the PRD-discussion council.

- [ ] **Step 1: Grep + confirm zero callers.**

```bash
cd backend && grep -rn "asset_universe" src/ tests/ --include='*.py' | tee /tmp/asset_universe_residuals.txt
```

Expected after T11 lands: only `tests/integration/api/test_asset_universe*.py` entries. Anything else is a blocker — do NOT delete until it is.

- [ ] **Step 2: Delete.**

```bash
cd backend && git rm src/msai/api/asset_universe.py tests/integration/api/test_asset_universe*.py
```

- [ ] **Step 3: Run full suite to confirm nothing else imported it.**

```bash
cd backend && uv run pytest tests/ -x --ignore=tests/integration/api/test_asset_universe_routes.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit.**

```bash
git commit -m "feat(symbol-onboarding): T14 delete asset_universe router + tests (superseded by /symbols/readiness)"
```

---

### Task 15: Integration + E2E use cases (Phase 3.2b artifact)

**Files:**

- Create: `backend/tests/integration/symbol_onboarding/test_orchestrator_failure_paths.py`
- Create: `backend/tests/integration/symbol_onboarding/test_end_to_end_run.py`
- Create: `tests/e2e/use-cases/instruments/symbol-onboarding.md` (draft, lives in plan until Phase 6.2b graduates it)

**Dependencies:** T1–T13. This is the last code task.

**Writes:** `backend/tests/integration/symbol_onboarding/test_orchestrator_failure_paths.py`, `backend/tests/integration/symbol_onboarding/test_end_to_end_run.py`, `tests/e2e/use-cases/instruments/symbol-onboarding.md`.

Integration test coverage matrix (one test per bullet):

- Orchestrator `BOOTSTRAP_FAILED` path (bootstrap service raises `DatabentoError`).
- Orchestrator `BOOTSTRAP_AMBIGUOUS` path (bootstrap returns `outcome="ambiguous"`).
- Orchestrator `INGEST_FAILED` path — **inline `ingest_symbols` helper raises** (NOT fake `job.result()`; council Option A pinned ingest as in-process — obsolete test removed).
- Orchestrator `COVERAGE_INCOMPLETE` path (Parquet missing one month).
- Orchestrator `IB_TIMEOUT` path (IB service hangs past `ib_timeout_s`) — **asserts `msai_onboarding_ib_timeout_total` counter increments**.
- Worker `completed_with_failures` rollup (happy + failed symbol in same run).
- Worker **all-failed-per-symbol → `completed_with_failures`** (iter-1 council semantic: NOT `failed`).
- Worker **systemic short-circuit → `failed`** (unhandled exception inside outer try/except; status-sync path tested).
- API **idempotency contract (iter-1 P1):** duplicate POST with identical body → dedup returns existing `run_id`, no orphan DB row minted.
- API **enqueue-failure rollback (iter-1 P1):** `pool.enqueue_job` raising → 503 envelope + zero DB rows written.
- API **enqueue-dedup but DB row absent (race):** `enqueue_job` returns `None` with no matching digest row → 202 with the reserved id (degrades gracefully, caller polls).
- End-to-end run: POST /onboard → poll /status until terminal → assert `symbol_states` match expected progression (uses local mocks for Databento + the inline `ingest_symbols` helper).

#### E2E Use Cases (Phase 3.2b)

```markdown
# Symbol Onboarding — E2E Use Cases

> Draft — graduates to `tests/e2e/use-cases/instruments/symbol-onboarding.md` after Phase 5.4 PASS.

## UC-SYM-001 — Onboard 4-symbol manifest (happy path)

**Interface:** API + CLI

**Intent:** An operator with a fresh system wants to onboard SPY, AAPL, QQQ, IWM (stocks) for 2024 full-year via a single CLI invocation.

**Setup (ARRANGE):** Write manifest to `watchlists/demo.yaml` with 4 symbols + explicit 2024 window. `docker compose -f docker-compose.dev.yml up -d` + `./scripts/restart-workers.sh`. No prior registry rows for these symbols.

**Steps:**

1. `msai symbols onboard --manifest watchlists/demo.yaml --dry-run` — capture estimated USD + confidence.
2. If confidence = `high`, proceed: `msai symbols onboard --manifest watchlists/demo.yaml`.
3. `msai symbols status <run_id> --watch` until terminal.
4. `curl -H 'X-API-Key: …' http://localhost:8800/api/v1/symbols/readiness?symbol=SPY&asset_class=equity&start=2024-01-01&end=2024-12-31` → confirm `backtest_data_available=true`.

**Verification:**

- Dry-run prints a dollar amount > 0 and `confidence=high`.
- Status terminates at `completed`; all 4 symbols show `status=succeeded`, `step=ib_skipped`.
- Readiness for each symbol returns `registered=true`, `backtest_data_available=true`, `coverage_status=full`, `live_qualified=false`.

**Persistence:** Re-running `msai symbols onboard --manifest watchlists/demo.yaml` returns 202 but the dedup lock collapses it onto the same arq job (`_job_id` match); status stays at the prior terminal state. Registry rows persist across `docker compose down/up`.

## UC-SYM-002 — Preflight ceiling rejects the run

**Interface:** API

**Intent:** Operator accidentally requests a 20-year window; cost preflight blocks before enqueuing.

**Setup:** Manifest with 1 symbol, `start: 2005-01-01`, `end: 2025-12-31`.

**Steps:**

1. `msai symbols onboard --manifest <file> --cost-ceiling-usd 5.00 --dry-run` — prints estimate.
2. If dry-run returns e.g. $45 > $5: run real `msai symbols onboard --manifest <file> --cost-ceiling-usd 5.00`.

**Verification:** Real invocation is client-side rejected before any POST. (CLI compares dry-run estimate against ceiling; if exceeded, exits non-zero with a clear message. API-side ceiling enforcement is US-004 PRD scope — if ceiling is exceeded on the server, 422 with `code=COST_CEILING_EXCEEDED`.)

**Persistence:** No run row created. `GET /symbols/onboard` list (if added later) shows no phantom entries.

## UC-SYM-003 — Partial-batch failure + repair

**Interface:** API

**Intent:** Databento rejects one symbol as ambiguous; the rest succeed; operator repairs the one failed symbol with a disambiguated alias.

**Setup:** Seed run with 3 symbols, one of which is known-ambiguous (e.g., `BRK` without the `.B` suffix in a window where Databento reports ambiguity; if real ambiguity is not reproducible, test via a patched bootstrap service that returns `outcome=ambiguous` for that symbol).

**Steps:**

1. POST /onboard with 3 symbols.
2. Poll /status until terminal; expect `status=completed_with_failures`, one `symbol.status=failed` with `error.code=BOOTSTRAP_AMBIGUOUS`.
3. POST /onboard/{run_id}/repair with `{"symbols":["BRK.B"]}` (disambiguated).
4. Poll new /status; expect `completed`.

**Verification:** Parent run stays `completed_with_failures`; child run is `completed`. Registry now contains all 3 instruments.

**Persistence:** Same invariant as UC-SYM-001.

## UC-SYM-004 — Readiness window scoping is truthful

**Interface:** API

**Intent:** Validate Contrarian's pin-#3 correction — `backtest_data_available` cannot be `true` without a window scope.

**Setup:** After UC-SYM-001 lands, SPY has full 2024 coverage.

**Steps:**

1. `GET /readiness?symbol=SPY&asset_class=equity` (no start/end) → `backtest_data_available=null`.
2. `GET /readiness?symbol=SPY&asset_class=equity&start=2024-01-01&end=2024-12-31` → `backtest_data_available=true`.
3. `GET /readiness?symbol=SPY&asset_class=equity&start=2023-01-01&end=2024-12-31` → `backtest_data_available=false`, `missing_ranges=[{start:"2023-01-01",end:"2023-12-31"}]`.

**Verification:** Response shapes match the ReadinessResponse Pydantic contract; `coverage_status` transitions `null → full → gapped` across the three calls.

**Persistence:** Read-only endpoint; no persistence required.

## UC-SYM-005 — Live qualification opt-in

**Interface:** API + IB Gateway (opt-in via `RUN_PAPER_E2E=1`)

**Intent:** `request_live_qualification=true` triggers IB qualification after ingest; `live_qualified=true` after the run terminates.

**Setup:** `COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml up -d` so IB Gateway is reachable on port 4002. Paper account (`DU…`). Manifest with SPY only.

**Steps:**

1. POST /onboard with `{"request_live_qualification": true, ...}`.
2. Poll /status until terminal (expect `completed`).
3. `GET /readiness?symbol=SPY&asset_class=equity` → `live_qualified=true`.

**Verification:** Last symbol step in /status is `completed` (terminal step for the IB-qualify path per the canonical `SymbolStepStatus` vocabulary — `ib_qualified` was a vestigial pre-iter-1 name, removed). Registry has an `interactive_brokers` alias row for SPY.

**Persistence:** IB alias row persists; future `/live/start-portfolio` deploys will resolve SPY via the registry (confirms PR #37 wiring still intact).

## UC-SYM-006 — IB Gateway unavailable → `IB_TIMEOUT`

**Interface:** API (with IB Gateway deliberately stopped)

**Intent:** `ib_timeout_s` enforcement is real; the run terminates with a failed symbol, a clear error code, and the Prometheus timeout counter increments.

**Setup:** IB Gateway container stopped (`docker compose stop ib-gateway`). Manifest with SPY. `request_live_qualification=true`.

**Steps:**

1. POST /onboard.
2. Poll /status — expect terminal **run** status `completed_with_failures` (per-symbol failures never bubble to run-level `failed`; see status-contract table), with the SPY symbol at `status=failed`, `step=ib_qualify`, `error.code=IB_TIMEOUT`.
3. Check Prometheus: `msai_onboarding_ib_timeout_total` increased by 1.

**Verification:** /status surfaces the specific timeout code + `next_action="Retry with request_live_qualification=false then rerun IB later."` Metric counter reflects the event.

**Persistence:** Registry is untouched — the symbol remains `registered` (from bootstrap) but `live_qualified=false`. This is correct: fail at the IB step should not rollback the bootstrap-level registration.
```

- [ ] **Step 1: Write integration tests** (per bullet list above). Each follows the pattern from T6/T7 tests — arrange fixtures, patch service boundaries, assert on the persisted `symbol_states` JSONB + terminal run status.

- [ ] **Step 2: Commit integration tests.**

```bash
cd backend && uv run pytest tests/integration/symbol_onboarding/ -v
git add backend/tests/integration/symbol_onboarding/
git commit -m "test(symbol-onboarding): T15 integration coverage matrix + end-to-end run"
```

- [ ] **Step 3: Commit E2E use-case draft.**

```bash
mkdir -p tests/e2e/use-cases/instruments
# write the use-case markdown above to tests/e2e/use-cases/instruments/symbol-onboarding.md (in draft, for Phase 3.2b)
git add tests/e2e/use-cases/instruments/symbol-onboarding.md
git commit -m "docs(symbol-onboarding): T15 E2E use cases (UC-SYM-001..006) drafted for Phase 5.4"
```

> **Graduation note:** these use cases are drafts at plan time; they land at `tests/e2e/use-cases/instruments/symbol-onboarding.md` only after the `verify-e2e` agent runs them against the live stack in Phase 5.4 and they PASS (or are classified SKIPPED_INFRA with justification). At that point the CONTINUITY "E2E use cases graduated" checklist item is marked `[x]`.

---

## Self-Review (skill-mandated, run inline after writing — 2026-04-24)

### 1. Spec coverage scan

| PRD requirement                                                                | Task             | Covered? |
| ------------------------------------------------------------------------------ | ---------------- | -------- |
| US-001 YAML manifest (trailing_5y sugar)                                       | T3               | ✅       |
| US-002 Async POST /onboard returns 202                                         | T9               | ✅       |
| US-003 Per-symbol progress state                                               | T6, T7, T9       | ✅       |
| US-004 Preflight cost estimate + ceiling                                       | T4, T8           | ✅       |
| US-005 Partial-batch semantics                                                 | T6, T7           | ✅       |
| US-006 Window-scoped `backtest_data_available`                                 | T2 (schema), T10 | ✅       |
| US-007 Repair action                                                           | T9               | ✅       |
| US-008 `msai symbols onboard` CLI                                              | T12              | ✅       |
| US-009 `msai symbols status` CLI                                               | T12              | ✅       |
| US-010 Delete /api/v1/universe                                                 | T11, T14         | ✅       |
| Constraint: single arq entrypoint                                              | T7               | ✅       |
| Constraint: no full-batch fan-out                                              | T7               | ✅       |
| Constraint: `_onboard_one_symbol()` seam                                       | T6               | ✅       |
| Constraint: phase-local bootstrap concurrency only (inherits PR #44 Semaphore) | T6               | ✅       |
| Constraint: `asyncio.wait_for(120s)` on IB                                     | T6               | ✅       |
| Constraint: 100-symbol cap                                                     | T2 (schema)      | ✅       |
| Constraint: 3 Prometheus metrics                                               | T13              | ✅       |
| Constraint: `SymbolOnboardingRun` row ownership                                | T1, T7, T9       | ✅       |
| Constraint: truthful status semantics (pin-#3 correction)                      | T10, UC-SYM-004  | ✅       |

### 2. Placeholder scan

Searched the plan for `TBD`, `TODO`, `fill in`, `similar to task N`, "add appropriate error handling". **None found.** T10's `find_active_aliases` is explicitly new code in the security_master service module (not a placeholder — the signature + dataclass contract is pinned in T10 Step 2a).

### 3. Type consistency scan (iter-1 re-verified)

- **Canonical `SymbolStatus`** = `not_started | in_progress | succeeded | failed`. Used identically in T2 schema, T6 orchestrator `_fail`/`_succeed`/`_persist_step`, T7 worker `_compute_terminal_status`, T9 API status endpoint, T12 CLI exit-code map. NO residual references to `ok` / `already_covered` (removed this iteration) — grep-verified.
- **Canonical `SymbolStepStatus`** = `pending | bootstrap | ingest | coverage | ib_qualify | completed | ib_skipped | coverage_failed`. Used identically in T2 schema + T6 orchestrator + T7 Prometheus histogram label set + status-contract table. NO residual references to `registering` / `backfilling` / `qualifying_live` (removed this iteration).
- **Canonical `SymbolOnboardingRunStatus`** = `pending | in_progress | completed | completed_with_failures | failed`. Used identically in T1 model (Enum) + alembic CHECK constraint + T7 terminal computation + T9 response + T12 CLI exit-code mapping.
- **Run-status semantics** — `failed` is set ONLY in the outer `except` block of `run_symbol_onboarding` (systemic short-circuit). Any terminal state reached via the normal loop is `completed` or `completed_with_failures`. Status-contract table + T7 `_compute_terminal_status` docstring both state this.
- **`compute_advisory_lock_key(provider, raw_symbol, asset_class)`** is unchanged (PR #44 signature preserved). New sibling `compute_blake2b_digest_key(*parts)` added for arbitrary-parts digest use (T9 `_dedup_job_id`). No call site mixes the two.
- **Asset-class seam** — `equity | futures | fx | option` is the one true user-facing vocabulary. `stocks | futures | forex | option` is the ingest / Parquet vocabulary. Translation happens at exactly one place: `normalize_asset_class_for_ingest(...)` in T3's `__init__.py`. Callers documented: T6 orchestrator, T10 readiness endpoint.
- **`DatabentoBootstrapService`** batch API preserved: `bootstrap(symbols=..., asset_class_override=..., exact_ids=...)` — T6 calls it with a single-element `symbols=[spec.symbol]` and inspects the single-item `list[BootstrapResult]`. `__init__(session_factory, databento_client)` required args — `_default_bootstrap_service` passes them.
- **`IBQualifier.qualify(spec: InstrumentSpec)`** is the real signature; T6's `_IBServiceAdapter` constructs `InstrumentSpec(symbol=..., asset_class=...)` + calls it. No fictional `IBRefreshService`.
- **arq primitives** — `get_redis_pool()` (real name, verified at `core/queue.py:52`). `IngestWorkerSettings.functions` gains `run_symbol_onboarding`; `max_jobs=1` + `job_timeout=3600` unchanged.
- **In-process ingest contract** — T6a adds `ingest_symbols(...)` returning `IngestResult`; T6 orchestrator awaits it directly, NO `enqueue_ingest` / `job.result()`. `run_ingest` arq shim still returns `None`.

### 4. Scope-check

Plan is **one** implementation plan for **one** feature (symbol onboarding). 15 tasks + 1 pre-flight + 1 pre-T6 helper (T6a) + 1 pre-T8 helper (T8-prime) = 18 task blocks, chronologically ordered. Aligns with other recent PRs (#40 had 18, #44 had 15, #41 had 17).

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-04-24-symbol-onboarding.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Fits well here: T3, T4, T5, T13, T14 are largely independent with clean `Writes` columns. T6 + T7 + T9 serialize on `api/symbol_onboarding.py` so those dispatch sequentially.
2. **Inline Execution** — execute in the current session via `superpowers:executing-plans`. Slower turn-for-turn but avoids subagent prompt overhead.

Which approach?
