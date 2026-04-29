<!-- forge:doc how-symbols-work -->

# How Symbols Work

This is doc 01 of the developer journey. Before you can backtest anything, the system has to know what `AAPL`, `ES`, or `EUR/USD` actually _is_ — which exchange, which contract, which trading hours, which provider has data, and which Nautilus `InstrumentId` to thread through the engine. That knowledge lives in two Postgres tables (`instrument_definitions` + `instrument_aliases`) and a flat Parquet catalog at `{DATA_ROOT}/parquet/`. Onboarding a symbol is the four-phase pipeline that fills both: bootstrap the registry rows, ingest historical bars, verify coverage, and (optionally) qualify the contract against IB Gateway.

The orchestration is fully audited — every batch is a `SymbolOnboardingRun` row with a per-symbol state map and a deterministic blake2b digest as its idempotency key. Same request, same digest, same job. Failures are scoped to the offending symbol; siblings keep going. Repair re-runs the failed subset as a child run, parent intact for audit.

---

## Component Diagram

```
                ┌─ DATA SOURCES ───────────────┐    ┌─ CONTROL PLANE ──────────────────┐
                │                              │    │                                  │
                │   Polygon.io     Databento   │    │   FastAPI  ── arq queue ── Redis │
                │   (stocks)       (futures+   │    │   :8800     workers       :6380  │
                │                   equities)  │    │   /api/v1/symbols/*              │
                └────────┬──────────┬──────────┘    └──────┬───────────────────────────┘
                         │          │                      │
                         │          ▼                      ▼
                         │   ┌─ COST ESTIMATE ──────┐ ┌─ ONBOARD ROUTER ────────────────┐
                         │   │ DatabentoClient      │ │  api/symbol_onboarding.py       │
                         │   │ .metadata.get_cost() │ │  POST /onboard/dry-run          │
                         │   │ → CostEstimate       │ │  POST /onboard                  │
                         │   └──────────┬───────────┘ │  GET  /onboard/{run_id}/status  │
                         │              │             │  POST /onboard/{run_id}/repair  │
                         │              │             │  GET  /readiness                │
                         │              │             └────────────┬────────────────────┘
                         │              │                          │
                         ▼              ▼                          ▼
              ┌─ ORCHESTRATOR ──────────────────────────────────────────────────┐
              │  workers/symbol_onboarding_job.run_symbol_onboarding (arq)      │
              │  └─ services/symbol_onboarding/orchestrator._onboard_one_symbol │
              │     ┌──────────┐ ┌────────┐ ┌─────────┐ ┌───────────────┐       │
              │     │bootstrap │→│ ingest │→│coverage │→│ ib_qualify (?)│       │
              │     └────┬─────┘ └───┬────┘ └────┬────┘ └─────┬─────────┘       │
              └──────────┼───────────┼───────────┼────────────┼─────────────────┘
                         │           │           │            │
            ┌────────────┘           │           │            └─────────────────┐
            │                        │           │                              │
            ▼                        ▼           ▼                              ▼
 ┌─ INSTRUMENT REGISTRY ──┐   ┌─ PARQUET CATALOG ───┐  ┌─ COVERAGE ────────┐ ┌─ IB GATEWAY ──┐
 │ instrument_definitions │   │ {DATA_ROOT}/parquet │  │ services/         │ │ paper :4002   │
 │   (UUID PK,            │   │   /{asset_class}/   │  │ symbol_onboarding │ │ live  :4001   │
 │    raw_symbol,         │   │   /{symbol}/        │  │ /coverage.py      │ │  short-lived  │
 │    asset_class,        │   │   /{YYYY}/{MM}.pq   │  │ scan + tolerate   │ │  qualifier    │
 │    provider,           │   │                     │  │ trailing 7 days   │ │  client       │
 │    routing_venue,      │   │ DuckDB reads        │  └───────────────────┘ └───────────────┘
 │    trading_hours)      │   │ in-memory           │
 │                        │   └─────────────────────┘
 │ instrument_aliases     │
 │   (alias_string,       │
 │    effective_from/to,  │
 │    venue_format,       │
 │    provider)           │
 │                        │
 │ SecurityMaster         │
 │   .resolve()           │
 │   .resolve_for_backtest│
 │   .find_active_aliases │
 └────────────────────────┘
            ▲
            │
 ┌──────────┴───────────────────────────────────────────────────────────────┐
 │ SURFACES                                                                 │
 │   API   /api/v1/symbols/* + /api/v1/instruments/bootstrap                │
 │   CLI   msai symbols onboard|status|repair  ·  msai instruments refresh │
 │   UI    frontend/src/app/data-management/page.tsx (read-only browse)    │
 └──────────────────────────────────────────────────────────────────────────┘
```

The dotted-line groupings are conceptual layers; arrows are real data and command flow. The orchestrator runs the four phases sequentially per symbol; the registry, Parquet catalog, and IB Gateway are the three durable side effects.

---

## TL;DR

A symbol becomes tradeable in MSAI v2 when (a) its registry rows exist with a current alias window, (b) its historical Parquet bars cover the requested date range, and (c) — only if you asked for it — IB Gateway has qualified the contract. You drive that pipeline through any of three surfaces:

| Surface | Entry point                                                    | Use it when                                       |
| ------- | -------------------------------------------------------------- | ------------------------------------------------- |
| API     | `POST /api/v1/symbols/onboard` + `/status` + `/repair`         | Programmatic / scripted ingest, the canonical way |
| CLI     | `msai symbols onboard --manifest <path>` + `status` + `repair` | Operator from a shell, manifest-driven batches    |
| UI      | `/data-management` page                                        | Read-only browse of what is already registered    |

The API is the contract; everything else flows through it (the CLI is a thin client over the same HTTP routes). Phase 1 explicitly does not have a UI for triggering onboarding — `/data-management` lists registered symbols only. Triggering goes through API or CLI.

---

## Table of Contents

1. [Concepts and data model](#1-concepts-and-data-model)
2. [The three surfaces](#2-the-three-surfaces-parity-table)
3. [Internal sequence](#3-internal-sequence-what-happens-after-the-request)
4. [See / Verify / Troubleshoot](#4-see--verify--troubleshoot)
5. [Common failures](#5-common-failures)
6. [Idempotency and retry](#6-idempotency-and-retry-behavior)
7. [Rollback and repair](#7-rollback-and-repair)
8. [Key files](#8-key-files)

---

## 1. Concepts and data model

### 1.1 The run and its state machines

Every onboarding batch creates one `SymbolOnboardingRun` row with two layered state machines: a coarse run-level status and a fine per-symbol status + step pair.

```
                ┌── RUN-LEVEL ──────────────────────────────┐
                │  PENDING ──► IN_PROGRESS ──► COMPLETED    │
                │                            │              │
                │                            ├─► COMPLETED_WITH_FAILURES
                │                            │              │
                │                            └─► FAILED      ◄── reserved for
                │                                              systemic crash
                └───────────────────────────────────────────┘

                ┌── PER-SYMBOL (inside symbol_states JSONB) ┐
                │ status ∈ {not_started, in_progress,       │
                │           succeeded, failed}              │
                │ step   ∈ {pending, bootstrap, ingest,     │
                │           coverage, ib_qualify,           │
                │           completed, ib_skipped,          │
                │           coverage_failed}                │
                └───────────────────────────────────────────┘
```

Run-level `FAILED` is **reserved for the worker's outer try/except** (`backend/src/msai/workers/symbol_onboarding_job.py:134-149`) — if every per-symbol failure became a run-level failure, the operator couldn't distinguish "one symbol broke" from "the whole queue is down." `_compute_terminal_status` (lines 181-192) only chooses between `COMPLETED` and `COMPLETED_WITH_FAILURES`; it never returns `FAILED`. The `FAILED` write happens exclusively in the worker's `except Exception` branch.

The status enums live in `backend/src/msai/schemas/symbol_onboarding.py:27-50`:

- `SymbolStepStatus` (StrEnum): `pending | bootstrap | ingest | coverage | ib_qualify | completed | ib_skipped | coverage_failed` (lines 27-35).
- `SymbolStatus` (StrEnum): `not_started | in_progress | succeeded | failed` (lines 38-42).
- `RunStatus` (StrEnum): `pending | in_progress | completed | completed_with_failures | failed` (lines 45-50).

### 1.2 The run row

`SymbolOnboardingRun` (`backend/src/msai/models/symbol_onboarding_run.py:31-80`) is a single Postgres row that owns the audit trail for one batch:

| Column                       | Type              | Purpose                                                                                    |
| ---------------------------- | ----------------- | ------------------------------------------------------------------------------------------ |
| `id`                         | UUID PK           | Run identifier surfaced as `run_id` in API responses.                                      |
| `watchlist_name`             | String(128)       | User-facing batch label (kebab-case).                                                      |
| `status`                     | Enum              | Run-level status (above).                                                                  |
| `symbol_states`              | JSONB             | Per-symbol map: `{<symbol>: {status, step, error, next_action, asset_class, start, end}}`. |
| `request_live_qualification` | bool              | Whether the operator asked for IB qualification.                                           |
| `job_id_digest`              | String(64) UNIQUE | blake2b hex; the dedup primary signal.                                                     |
| `cost_ceiling_usd`           | Numeric(12,2)     | Hard spend cap, NULL if absent.                                                            |
| `estimated_cost_usd`         | Numeric(12,2)     | Set from dry-run estimate at enqueue time.                                                 |
| `actual_cost_usd`            | Numeric(12,2)     | Filled by orchestrator on completion.                                                      |
| `created_at`/`updated_at`    | DateTime          | Audit timestamps.                                                                          |
| `started_at`/`completed_at`  | DateTime          | Wall-clock pipeline boundaries.                                                            |

Constraints (same file): `ck_symbol_onboarding_runs_status` enforces the enum, and `ck_symbol_onboarding_runs_cost_ceiling_nonneg` rejects negative ceilings. The unique index on `job_id_digest` is the floor of the idempotency story (§ 6).

### 1.3 The instrument registry

The registry is two tables.

**`instrument_definitions`** (`backend/src/msai/models/instrument_definition.py:34-95`):

- Primary key: `instrument_uid` (UUID, default `gen_random_uuid()`).
- Unique constraint: `(raw_symbol, provider, asset_class)` — one definition per ticker/provider/class. You can have `AAPL.equity@interactive_brokers` and `AAPL.equity@databento` side by side.
- CHECK constraints (lines 38-49):
  - `asset_class IN ('equity','futures','fx','option','crypto')`. Note: although the CHECK accepts `crypto`, the Symbol Onboarding API surface (`OnboardSymbolSpec.asset_class` and `ReadinessAssetClass`) restricts inputs to `equity | futures | fx | option` — there is no Phase 1 path to onboard a crypto symbol through the API (see § 1.4).
  - `lifecycle_state IN ('staged','active','retired')` (the workflow state).
  - `continuous_pattern` matches `^\.[A-Za-z]\.[0-9]+$` or NULL — gates ".Z.5"-style continuous futures.
- Other columns: `raw_symbol` (indexed), `listing_venue` (e.g., `XNAS`), `routing_venue` (e.g., `SMART`), `provider`, `roll_policy`, `trading_hours` (JSONB; NULL = fail-open in `MarketHoursService`), `refreshed_at`, `created_at`, `updated_at`.
- Cascade delete to `instrument_aliases`.

**`instrument_aliases`** (`backend/src/msai/models/instrument_alias.py:33-86`):

- Primary key: `id` (UUID).
- Foreign key: `instrument_uid` → `instrument_definitions` (cascade delete).
- Unique constraint: `(alias_string, provider, effective_from)` — the effective-date is part of the unique key so futures rolls don't collide.
- CHECK constraints (lines 37-44):
  - `venue_format IN ('exchange_name','mic_code','databento_continuous')`.
  - `effective_to IS NULL OR effective_to >= effective_from` (relaxed from strict `>` by migration `b6c7d8e9f0a1` so same-day rotations work).
- Other columns: `alias_string` (indexed; the venue-qualified instrument ID like `AAPL.XNAS` or `ES.Z.24`), `source_venue_raw` (raw Databento MIC pre-normalization), `effective_from` (Date, inclusive), `effective_to` (Date, exclusive; NULL = currently active).

The reason aliases are time-windowed: futures roll. When the front-month rolls, the orchestrator sets `effective_to` on the old row and inserts a new one with the next expiry. Backtests reading historical data must resolve `ES` to whatever was front-month _on that historical date_, not today's. See `SecurityMaster.resolve_for_backtest` (`backend/src/msai/services/nautilus/security_master/service.py:400-503`), which honors a `start` kwarg for historical alias windowing.

### 1.4 Asset-class taxonomy bridges

Three separate naming conventions need to agree, and they don't. The canonical mapping table lives in `backend/src/msai/services/nautilus/security_master/types.py:32-50` (`REGISTRY_TO_INGEST_ASSET_CLASS`) — it's shared by the SecurityMaster and the symbol-onboarding orchestrator, which prevents silent Parquet-routing hazards.

| Layer               | Type alias                                                                          | Values                                              |
| ------------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------- |
| Registry (DB CHECK) | `RegistryAssetClass` (`security_master/types.py:14`)                                | `equity, futures, fx, option, crypto`               |
| API surface         | `AssetClass` (`schemas/symbol_onboarding.py:53`) + `ReadinessAssetClass` (`api:63`) | `equity, futures, fx, option` — **no `crypto`**     |
| Parquet storage     | `IngestAssetClass` (`security_master/types.py:19`)                                  | `stocks, futures, options, forex, crypto`           |
| Nautilus spec       | (constructed inline)                                                                | `equity, future, forex, option, crypto` (singular!) |
| Provider            | `Provider` (`security_master/types.py:22`)                                          | `interactive_brokers, databento`                    |
| Alias type          | `VenueFormat` (`security_master/types.py:26`)                                       | `exchange_name, mic_code, databento_continuous`     |

The two API-surface literals (`AssetClass` for `OnboardSymbolSpec`, `ReadinessAssetClass` for the readiness query) are 4 values; the registry CHECK constraint accepts a 5th (`crypto`). The result: an operator cannot reach the registry's `crypto` row through `/onboard` or `/readiness` in Phase 1 — that taxonomy entry is reserved for future onboarding paths. Pin asset_class to one of the four supported values when calling the API.

The fail-loud helper `normalize_asset_class_for_ingest` (`backend/src/msai/services/symbol_onboarding/__init__.py:24-36`) raises `ValueError` on unknown input rather than silently routing files to the wrong directory.

### 1.5 Cost estimation

Onboarding can reach a real money spend, so the dry-run path is mandatory before any committing call. `backend/src/msai/services/symbol_onboarding/cost_estimator.py` (`estimate_cost` at lines 88-174) calls `client.metadata.get_cost()` once per `(dataset, start, end)` bucket via the Databento client (bucketing collapses identical-window symbols into a single upstream call). Confidence drops as the requested end date approaches today (Databento's quote is less stable for trailing-edge windows).

- `_ASSET_TO_DATASET` (lines 28-31): `equity → "XNAS.ITCH"`, `futures → "GLBX.MDP3"`. Asset classes outside this map raise `UnpriceableAssetClassError` (lines 34-51), which the API surfaces as 422 `UNPRICEABLE_ASSET_CLASS`.
- `CostLine` (lines 71-76): per-symbol breakdown row.
- `CostEstimate` (lines 79-85): aggregate result with `total_usd`, `basis`, `confidence` (`high|medium|low`), `breakdown[]`.

The dry-run path requires `DATABENTO_API_KEY` to be configured server-side. `_get_databento_client` (`api/symbol_onboarding.py:90-107`) raises `RuntimeError` if the key is unset; the `/onboard/dry-run` handler only catches `UnpriceableAssetClassError`, so an unconfigured key surfaces as **HTTP 500**, not 422. Operators running dry-runs against an environment without Databento credentials should expect 500 — fix it by setting `DATABENTO_API_KEY`, not by retrying.

### 1.6 Coverage check

`backend/src/msai/services/symbol_onboarding/coverage.py:1-164` walks the Parquet directory tree at `{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet` and compares the months that are present against the months the request asked for.

- `compute_coverage` (lines 26-62): the entry point. Returns a `CoverageReport` (lines 19-23) with `status ∈ {full, gapped, none}`, `covered_range`, and `missing_ranges`.
- `_apply_trailing_edge_tolerance` (lines 103-107): suppresses misses within the last 7 days. The Databento backfill is not real-time; without this tolerance, every end-on-yesterday request would report `gapped`.
- `_collapse_missing` (lines 110-127): groups consecutive missing months into ranges so the operator sees `[(2024-03, 2024-05)]` instead of three separate rows.

### 1.7 Futures alias rotation

For continuous futures (`<root>.Z.<N>` form), `SecurityMaster._resolve_databento_continuous` (`backend/src/msai/services/nautilus/security_master/service.py:505-595`) is the only path that synthesizes on cold-miss. Two steps:

1. Warm-hit by `raw_symbol`: if a definition row exists, return its current alias.
2. Cold-miss: fetch the Databento definition, synthesize a `ResolvedInstrumentDefinition`, upsert via `_upsert_definition_and_alias` (scoped on `(raw_symbol, provider, asset_class)` for the def + `(alias_string, provider, effective_from)` for the alias — the same UNIQUE constraint described in § 1.3 — so repeats refresh timestamps without `IntegrityError`).

The supporting helpers are in `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`:

- `exchange_local_today()` (lines 61-69): returns today in CME exchange-local (`America/Chicago`), used by writer (when stamping `effective_from`) **and** reader (when computing `as_of_date` in the resolver). UTC `date.today()` would put the late-Central-evening window on the wrong side of midnight and make freshly-refreshed aliases temporarily invisible — a real bug, fixed inline at `service.py:_upsert_definition_and_alias`.
- `third_friday_of(year, month)` (lines 72-82): standard CME quarterly expiration.
- `current_quarterly_expiry(today)` (lines 85-104): returns the earliest quarter whose third-Friday expiry is **strictly after** today. On roll-day, that's the next quarter — conservative, correct.

### 1.8 Request validators (the 422s you can hit before the job ever enqueues)

Every operator-facing 422 from `/onboard` and `/onboard/dry-run` originates either in Pydantic's schema validation or in `OnboardRequest.model_validator`. Knowing these prevents wasted "why is my batch rejected?" round-trips. From `backend/src/msai/schemas/symbol_onboarding.py:23-102`:

| Validator                                   | Constraint                                                                                                                      | Where                       |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| `_MAX_SYMBOLS_PER_BATCH = 100`              | `OnboardRequest.symbols` accepts at most 100 entries per batch (`max_length=100`)                                               | line 24, applied at line 84 |
| `watchlist_name` pattern                    | `min_length=1, max_length=128, pattern=r"^[a-z0-9\-]+$"` — kebab-case only                                                      | line 83                     |
| `OnboardSymbolSpec.symbol` regex            | `^[A-Za-z0-9._/-]+$` (`_SYMBOL_PATTERN`); `min_length=1, max_length=32`                                                         | lines 23, 59, 64-69         |
| `OnboardSymbolSpec.asset_class`             | `Literal["equity", "futures", "fx", "option"]` — see § 1.4                                                                      | line 53                     |
| `OnboardSymbolSpec` date coherence          | `end >= start` AND `start <= today` (raised by the model_validator)                                                             | lines 71-77                 |
| `cost_ceiling_usd`                          | `Decimal`, `max_digits=12`, `decimal_places=2`, `ge=0` (must be non-negative)                                                   | line 86                     |
| Per-batch uniqueness                        | duplicate `(symbol, asset_class)` tuples are rejected — JSONB `symbol_states` is keyed by `symbol` and would silently overwrite | lines 88-102                |
| `model_config = ConfigDict(extra="forbid")` | Unknown fields on `OnboardRequest` and `OnboardSymbolSpec` cause 422                                                            | lines 57, 81                |

These all surface as standard FastAPI 422 with the Pydantic detail array. They run before any DB write or queue interaction, so they cost nothing.

The `cost_ceiling_usd` field has a second layer: when `/onboard` is called with a non-`None` ceiling, `_compute_cost_estimate` runs synchronously inline (one `metadata.get_cost` per `(dataset, start, end)` bucket). If the resulting `total_usd` exceeds the ceiling, the API returns **422 `COST_CEILING_EXCEEDED`** — a real load-bearing 422 that gates job enqueue (`api/symbol_onboarding.py:343-357`). Same code path the dry-run uses, so a passing dry-run guarantees a passing ceiling check.

---

## 2. The three surfaces (parity table)

Every onboarding operation has three peer entry points and one observation surface. The canonical operations are: dry-run a cost estimate, onboard symbols, poll status, repair failed symbols, query readiness, refresh instrument registry, bootstrap registry rows. Most lay across all three surfaces; a few (repair, dry-run) are API + CLI only.

| Intent                          | API                                                                                              | CLI                                                             | UI                                                        | Observe / Verify                                                                |
| ------------------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **Dry-run cost estimate**       | `POST /api/v1/symbols/onboard/dry-run` → `DryRunResponse` (200)                                  | `msai symbols onboard --manifest <p> --dry-run`                 | n/a — not on this surface (Phase 1 has no trigger UI)     | Response body `estimated_cost_usd`, `estimate_confidence` ; CLI prints estimate |
| **Onboard symbols**             | `POST /api/v1/symbols/onboard` → `OnboardResponse` (202 / 200 dup)                               | `msai symbols onboard --manifest <p>`                           | n/a — not on this surface (Phase 1 read-only)             | Returned `run_id` ; `GET …/status` ; `msai data-status`                         |
| **Poll run status**             | `GET /api/v1/symbols/onboard/{run_id}/status` → `StatusResponse` (200)                           | `msai symbols status <run_id>` (or `--watch` to tail)           | n/a — not on this surface (no run-detail page in Phase 1) | Per-symbol step transitions; `progress` aggregate                               |
| **Repair failed symbols**       | `POST /api/v1/symbols/onboard/{run_id}/repair` → child `OnboardResponse` (202)                   | `msai symbols repair <run_id> [--symbols A,B,C]`                | n/a — not on this surface (Phase 1 has no repair button)  | Child `run_id` ; poll same way as new run                                       |
| **Readiness query**             | `GET /api/v1/symbols/readiness?symbol=…&asset_class=…&start=…&end=…` → `ReadinessResponse` (200) | n/a — read-through API only in Phase 1                          | n/a — not yet surfaced as a row badge (deferred)          | `registered`, `backtest_data_available`, `live_qualified` flags                 |
| **Bootstrap (registry only)**   | `POST /api/v1/instruments/bootstrap` → `BootstrapResponse` (200/207/422)                         | `msai instruments bootstrap --provider databento --symbols A,B` | n/a — not on this surface                                 | Response array (`registered`, `canonical_id`, `candidates`)                     |
| **Refresh instrument registry** | n/a — handled inline by onboard / via separate provider-side warm                                | `msai instruments refresh --symbols X,Y --provider <p>`         | n/a — not on this surface                                 | CLI prints upsert counts; subsequent `resolve_for_backtest` warm-hits           |
| **Browse what is registered**   | `GET /api/v1/market-data/symbols`                                                                | `msai data-status`                                              | `/data-management` (read-only table)                      | Symbol rows by asset class; storage stats panel                                 |

### 2.1 Notes on the parity

**Why the UI is read-only.** Phase 1 made the explicit decision that the trigger surface is API + CLI; the UI exists to show what is registered (and, in later phases, will gain a manifest-upload page). This is consistent with the project's API-first / UI-third order — see `CLAUDE.md` and `00-developer-journey.md`. The `/data-management` page (`frontend/src/app/data-management/page.tsx:32-134`) renders a "Trigger Download" button (lines 63-78) that scrolls into a future feature; today it is a stub.

**Why dry-run before onboard.** `OnboardRequest` (`backend/src/msai/schemas/symbol_onboarding.py:80-102`) accepts a `cost_ceiling_usd` field. When `/onboard` is invoked with a non-`None` ceiling, `_compute_cost_estimate` runs synchronously inline (`api/symbol_onboarding.py:343-357`) and the API returns **422 `COST_CEILING_EXCEEDED`** if the total exceeds the ceiling — the job is never enqueued. So the dry-run is both informational (does this fit my budget?) and load-bearing (it's the same code path that gates `/onboard`).

**The dry-run is pure** (`backend/src/msai/api/symbol_onboarding.py:110-157`). It does not write to the DB, does not enqueue, does not touch Databento beyond the `metadata.get_cost()` calls. You can call it freely.

**The CLI is a thin HTTP client.** `backend/src/msai/cli_symbols.py:1-154` shells out to `_api_call` (imported from `cli.py`), which uses `MSAI_API_KEY` for auth via `X-API-Key` (the dev/CLI bypass over the JWT). The CLI does no business logic — every behavior listed above is enforced server-side.

**`msai instruments bootstrap` vs `POST /api/v1/instruments/bootstrap`.** Same orchestrator (`DatabentoBootstrapService`), but the CLI command (`backend/src/msai/cli.py:1175-1277`) bypasses the standard `_api_call` to handle 207 Multi-Status correctly — `_api_call` raises on non-2xx, but 207 is the intended outcome for a mixed batch. The endpoint returns 200 if all symbols succeed, 207 if some succeed and some fail, 422 if all fail.

**`msai instruments refresh` is the warm-up command** (`backend/src/msai/cli.py:785-979`). It pre-loads the registry so future deployments / backtests never hit cold-miss at bar-event time. For `--provider databento`, it pulls the Databento definition payload and upserts. For `--provider interactive_brokers`, it spins up a short-lived Nautilus IB client, qualifies against IB Gateway, upserts, disconnects. The relevant settings (`IB_HOST`, `IB_PORT`, `IB_ACCOUNT_ID`, `IB_CONNECT_TIMEOUT_SECONDS`, `IB_REQUEST_TIMEOUT_SECONDS`, `IB_INSTRUMENT_CLIENT_ID`) are read from the environment.

### 2.2 Auth across surfaces

All `/api/v1/*` routes require auth via `Depends(get_current_user)`. The two accepted credentials are:

- Bearer JWT (Azure Entra ID via PyJWT) — the production path, used by the frontend.
- `X-API-Key: $MSAI_API_KEY` — the dev/CLI bypass; the CLI uses this for `_api_call`.

`/health` and `/ready` are unauthenticated. Everything in this doc is authenticated.

---

## 3. Internal sequence — what happens after the request

```
Caller                Router                 _enqueue_and_persist_run         arq                run_symbol_onboarding         _onboard_one_symbol
  │                     │                              │                       │                        │                            │
  │ POST /onboard       │                              │                       │                        │                            │
  ├────────────────────►│                              │                       │                        │                            │
  │                     │ build OnboardRequest         │                       │                        │                            │
  │                     │ _dedup_job_id (blake2b)      │                       │                        │                            │
  │                     ├─────────────────────────────►│                       │                        │                            │
  │                     │                              │ SELECT FOR UPDATE     │                        │                            │
  │                     │                              │   WHERE digest = …    │                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │                              │ ┌─ row exists? ──┐    │                        │                            │
  │                     │                              │ │  yes → 200 OK  │    │                        │                            │
  │                     │                              │ │  with existing │    │                        │                            │
  │                     │                              │ │  run_id        │    │                        │                            │
  │                     │                              │ └────────────────┘    │                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │                              │ enqueue_job(          │                        │                            │
  │                     │                              │   "run_symbol_…",     │                        │                            │
  │                     │                              │   _job_id=digest_str, │                        │                            │
  │                     │                              │   run_id=reserved)    │                        │                            │
  │                     │                              ├──────────────────────►│                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │                              │  ┌─ enqueue raises ─┐ │                        │                            │
  │                     │                              │  │ infra error →    │ │                        │                            │
  │                     │                              │  │ 503 QUEUE_…      │ │                        │                            │
  │                     │                              │  │ (no row commit)  │ │                        │                            │
  │                     │                              │  └──────────────────┘ │                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │                              │ commit row (PENDING)  │                        │                            │
  │                     │                              │ on commit fail →      │                        │                            │
  │                     │                              │   abort_job + raise   │                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │ 202 OnboardResponse          │                       │                        │                            │
  │◄────────────────────┤                              │                       │                        │                            │
  │                     │                              │                       │                        │                            │
  │                     │                              │                       │ pull _job_id           │                            │
  │                     │                              │                       ├───────────────────────►│                            │
  │                     │                              │                       │                        │ Phase A: SELECT FOR UPDATE │
  │                     │                              │                       │                        │   PENDING → IN_PROGRESS    │
  │                     │                              │                       │                        │   (started_at = now)       │
  │                     │                              │                       │                        │                            │
  │                     │                              │                       │                        │ Phase B: for each spec:    │
  │                     │                              │                       │                        ├───────────────────────────►│
  │                     │                              │                       │                        │                            │ 1. bootstrap (Databento)
  │                     │                              │                       │                        │                            │    → upsert def + alias
  │                     │                              │                       │                        │                            │ 2. ingest (Polygon/Databento)
  │                     │                              │                       │                        │                            │    → write Parquet
  │                     │                              │                       │                        │                            │ 3. coverage (Parquet scan)
  │                     │                              │                       │                        │                            │    → full / gapped / none
  │                     │                              │                       │                        │                            │ 4. ib_qualify (optional)
  │                     │                              │                       │                        │                            │    → register IB alias
  │                     │                              │                       │                        │ persist row state          │
  │                     │                              │                       │                        │   under SELECT FOR UPDATE  │
  │                     │                              │                       │                        │   at every phase boundary  │
  │                     │                              │                       │                        │◄───────────────────────────┤
  │                     │                              │                       │                        │                            │
  │                     │                              │                       │                        │ Phase C:                   │
  │                     │                              │                       │                        │ _compute_terminal_status   │
  │                     │                              │                       │                        │   all succeeded → COMPLETED│
  │                     │                              │                       │                        │   any failed →             │
  │                     │                              │                       │                        │     COMPLETED_WITH_FAILURES│
  │                     │                              │                       │                        │ emit Prometheus metric     │
  │                     │                              │                       │                        │ completed_at = now         │
  │                     │                              │                       │                        │                            │
  │ GET /onboard/{id}/status                           │                       │                        │                            │
  ├────────────────────►│                              │                       │                        │                            │
  │                     │ load row + symbol_states     │                       │                        │                            │
  │                     │ _summarize → progress        │                       │                        │                            │
  │                     │ _suggest_next_action per row │                       │                        │                            │
  │ 200 StatusResponse  │                              │                       │                        │                            │
  │◄────────────────────┤                              │                       │                        │                            │
```

The four boxes inside `_onboard_one_symbol` are the per-symbol seam — `services/symbol_onboarding/orchestrator.py:92+`. The interface boundary uses two protocols (`OrchestratorBootstrapProto` lines 73-83, `OrchestratorIBProto` lines 85-89) so tests inject fakes; production wires `DatabentoBootstrapService` and the IB qualifier. State is persisted **under SELECT FOR UPDATE at every phase boundary** by the worker (the orchestrator delegates the per-phase persistence inside `_onboard_one_symbol`), so if the worker crashes mid-symbol the row reflects exactly which phase landed and which didn't — no recovery ambiguity.

The `_BOOTSTRAP_FAILURE_OUTCOMES` frozenset (`orchestrator.py:62-70`) is the set of outcomes that terminate the per-symbol pipeline immediately: `AMBIGUOUS, UNAUTHORIZED, UNMAPPED_VENUE, UPSTREAM_ERROR, RATE_LIMITED`. Anything else lets the loop continue to the next phase.

---

## 4. See / Verify / Troubleshoot

You ran an onboard. How do you confirm it worked?

### 4.1 Through the API

The canonical poll loop:

```bash
curl -s -H "X-API-Key: $MSAI_API_KEY" \
  http://localhost:8800/api/v1/symbols/onboard/$RUN_ID/status | jq .
```

`StatusResponse` (`backend/src/msai/schemas/symbol_onboarding.py:134-141`) returns:

- `run_id`, `watchlist_name`, `status` (the run-level enum).
- `progress`: `{total, succeeded, failed, in_progress, not_started}`.
- `per_symbol[]`: each row has `symbol`, `asset_class`, `start`, `end`, `status`, `step`, `error` (`{code, message}` or null), `next_action` (operator hint or null).
- `estimated_cost_usd`, `actual_cost_usd`.

If `status == "completed"` and every `per_symbol[].status == "succeeded"`, you are done. If `status == "completed_with_failures"`, scope a `/repair` (§ 7).

### 4.2 Through the CLI

```bash
uv run msai symbols status $RUN_ID --watch
```

`--watch` polls every 5 seconds until the run reaches a terminal state, then exits with a status code that maps to the run state: `0 = completed`, `1 = completed_with_failures`, `2 = failed`, `3 = unknown` (`cli_symbols.py:148-154`). The CLI renders a `rich.Table` with one row per symbol and a colored summary line; the `error` column shows just the error code for compactness.

For a global view of everything that has been ingested:

```bash
uv run msai data-status
```

This is independent of any specific run — it reports what the Parquet catalog actually contains, which is the ground truth for "is this symbol ready to backtest?"

### 4.3 Through the UI

The `/data-management` page (`frontend/src/app/data-management/page.tsx:32-134`) is the read-only dashboard. It calls `getMarketDataSymbols(token)` (`@/lib/api`) on mount, flattens the `{assetClass: [symbols]}` payload into a flat `SymbolRow[]`, and renders:

- A page header with title and a stub "Trigger Download" button (lines 63-78).
- Error toast on data-load failure (lines 80-84).
- A 5-column grid (lines 87-90) showing `StorageChart` and `IngestionStatus` components.
- A "Data Symbols" card with a table of `symbol` + `asset_class` badge per row (lines 93-131).

It does not show running onboards — that surface is API + CLI in Phase 1.

### 4.4 Readiness probe

For the question "is this single instrument ready to use for a specific date range?", the targeted endpoint is `GET /api/v1/symbols/readiness?symbol=AAPL&asset_class=equity&start=2024-01-01&end=2024-12-31`. The `asset_class` query parameter is restricted to `equity | futures | fx | option` (see § 1.4). Response (`ReadinessResponse`, `backend/src/msai/schemas/symbol_onboarding.py:154-165`):

- `instrument_uid`: UUID from the registry, the stable identifier across UI / API / DB.
- `registered`: any active alias exists.
- `provider`: which provider's row won (preference: `databento` first, then `interactive_brokers`, then `sorted(provider_set)[0]` of whatever else is left, per `SecurityMaster.find_active_aliases` at `service.py:778-783`).
- `backtest_data_available`: bool or null. Null means you didn't pass **either** `start` **or** `end` — the gate is `if start is None or end is None:` (`api/symbol_onboarding.py:609`), so omitting just one is enough to skip the Parquet scan.
- `coverage_status`: `full | gapped | none` or null.
- `covered_range`: human string of what's there.
- `missing_ranges`: list of gaps.
- `live_qualified`: bool — independent flag, set if **any** active alias row has `provider=interactive_brokers`.
- `coverage_summary`: short hint when start/end weren't passed.

The endpoint returns 422 with `code=AMBIGUOUS_INSTRUMENT` if the symbol + asset_class match more than one definition (e.g., the same ticker registered under multiple providers with conflicting metadata) — see `AmbiguousSymbolError` (`registry.py:33-66`).

### 4.5 Log scan

The orchestrator emits structured log entries at every phase boundary (`workers/symbol_onboarding_job.py:84-107`). Tail the worker logs for the run id:

```bash
docker compose -f docker-compose.dev.yml logs -f worker | grep $RUN_ID
```

You'll see one line per `(symbol, phase)` transition plus the run-level COMPLETED / COMPLETED_WITH_FAILURES emission with elapsed wall-clock.

---

## 5. Common failures

When `per_symbol[].status == "failed"`, the row carries an `error.code` and `_suggest_next_action` (`backend/src/msai/api/symbol_onboarding.py:534-554`) maps it to a one-line operator hint. The mapping below is the canonical "what does this mean and what do I do" reference for the codes that have hints. The orchestrator can also emit `BOOTSTRAP_FAILED` (`orchestrator.py:127-146`) — a catch-all when bootstrap raises or returns empty results — and `_suggest_next_action` returns `None` for it, so callers see `next_action == null` and need to consult the structured error message and worker logs directly.

| Error code                 | Phase      | Hint (verbatim from `_suggest_next_action`)                                                   | What it actually means                                                                                                                                                                                                                              |
| -------------------------- | ---------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BOOTSTRAP_AMBIGUOUS`      | bootstrap  | Disambiguate with exact instrument id + re-onboard.                                           | Databento returned multiple candidate definitions for the symbol; the orchestrator can't choose. Use `msai instruments bootstrap --exact-id SYMBOL:ALIAS_STRING` from the prior 422's `candidates` array, or pass `exact_ids` to the bootstrap API. |
| `BOOTSTRAP_UNAUTHORIZED`   | bootstrap  | Check Databento dataset entitlement.                                                          | Your `DATABENTO_API_KEY` lacks the dataset (`XNAS.ITCH` for equities, `GLBX.MDP3` for futures). Add the entitlement at databento.com.                                                                                                               |
| `BOOTSTRAP_UNMAPPED_VENUE` | bootstrap  | File issue — unknown Databento venue MIC.                                                     | A Databento payload arrived with a MIC code that isn't in the registry's normalization table. This is a code bug, not an operator one — file an issue with the raw `source_venue_raw` value.                                                        |
| `COVERAGE_INCOMPLETE`      | coverage   | Inspect ingest logs; retry via /repair.                                                       | The ingest phase reported success but coverage scan found missing months (and they're outside the trailing-edge tolerance). Usually a Databento backfill gap or a partial-write recovery left the catalog short.                                    |
| `IB_TIMEOUT`               | ib_qualify | Retry with request_live_qualification=false then rerun IB later.                              | IB Gateway didn't respond within `IB_REQUEST_TIMEOUT_SECONDS` for the qualify call. Often correlated with off-hours or rate-limit. Restart Gateway and rerun via `msai instruments refresh --provider interactive_brokers`.                         |
| `IB_UNAVAILABLE`           | ib_qualify | Confirm IB Gateway container is running + entitled.                                           | The container under the `broker` Compose profile isn't up, or the account has no live data entitlement (see `nautilus.md` gotcha #6).                                                                                                               |
| `IB_NOT_CONFIGURED`        | ib_qualify | Live qualification is not enabled in this build; rerun with request_live_qualification=false. | The IB qualifier was injected as `None` (e.g., backtest-only deployment). Don't request IB qualification from this build.                                                                                                                           |
| `INGEST_FAILED`            | ingest     | Retry via /repair after checking Databento quota.                                             | The Databento or Polygon ingest call raised. Check rate-limit / quota first; if those are clean, the upstream payload may be malformed for that date.                                                                                               |

A failure on bootstrap always pre-empts the later phases for that symbol (`_BOOTSTRAP_FAILURE_OUTCOMES`); a failure on ingest pre-empts coverage and ib_qualify; a failure on coverage marks `step=coverage_failed` and skips ib_qualify; an `IB_NOT_CONFIGURED` outcome marks `step=ib_skipped` rather than failing if `request_live_qualification=False` was the operator's choice.

Errors on the readiness endpoint are different — they're not pipeline outcomes, they're query errors. The two you'll hit:

- `404 NOT_FOUND`: no active alias rows for `(symbol, asset_class)` as of today. The symbol is genuinely not registered.
- `422 AMBIGUOUS_INSTRUMENT`: the symbol matches more than one definition (typically across providers with different `asset_class` recorded). Pin one explicitly or call `msai instruments bootstrap --exact-id` to register the canonical alias.

Pre-enqueue `/onboard` and `/onboard/dry-run` errors (these never produce a `run_id` — fix the request and resubmit):

- **422 `UNPRICEABLE_ASSET_CLASS`**: an `OnboardSymbolSpec.asset_class` falls outside `_ASSET_TO_DATASET` (`equity`, `futures`). Today this means a `fx` or `option` spec — there is no Databento dataset mapped for cost estimation, so the orchestrator can't enforce the ceiling.
- **422 `COST_CEILING_EXCEEDED`** (`api/symbol_onboarding.py:343-357`): when `cost_ceiling_usd` is set and the live `metadata.get_cost` total tops it, the API blocks the enqueue. Re-run the dry-run, lower the scope or raise the ceiling.
- **409 `DUPLICATE_IN_FLIGHT`** (`:281-285`): two writers raced for the same digest and the second one's `enqueue_job` returned `None` while the row hadn't materialized yet. Retry in ~1 s.
- **503 `QUEUE_UNAVAILABLE`** (`:250-263`): Redis/arq pool was unreachable. Restart the worker stack and retry.
- **HTTP 500 (uncoded) on dry-run when `DATABENTO_API_KEY` is unset**: `_get_databento_client` raises `RuntimeError`, which isn't caught by `onboard_dry_run`. Set the key server-side.

---

## 6. Idempotency and retry behavior

### 6.1 The blake2b digest job_id

`_dedup_job_id` (`backend/src/msai/api/symbol_onboarding.py:178-200`) computes a deterministic arq job id from a canonical fingerprint of the request:

```python
canonical = [
    f"{s.symbol}|{s.asset_class}|{s.start.isoformat()}|{s.end.isoformat()}"
    for s in sorted(req.symbols, key=lambda s: (s.asset_class, s.symbol))
]
ceiling = str(req.cost_ceiling_usd) if req.cost_ceiling_usd is not None else "no_ceiling"
digest  = compute_blake2b_digest_key(
    "symbol_onboarding",
    req.watchlist_name,
    str(req.request_live_qualification),
    ceiling,
    *extra_parts,
    *canonical,
)
return f"symbol-onboarding:{digest:x}"
```

Three things to notice:

1. **Symbols are sorted before digesting** (line 189) so the same request submitted with reordered symbols hashes the same way.
2. **`cost_ceiling_usd` is part of the fingerprint** (line 191). If you submitted `[AAPL]` with no ceiling, then re-submitted with a $5 ceiling, the second call must enqueue a new job — the first ran without a budget cap and you'd want the second to apply the cap. Same symbols, different intent → different digest.
3. **`request_live_qualification` is part of the fingerprint** (line 195). A run that didn't ask for IB qualification is a different run from one that did, even with identical symbols.

The digest is stamped onto the row's `job_id_digest` column (UNIQUE) **and** used as the arq `_job_id`. Both layers reject duplicates — Postgres on commit conflict, arq on enqueue.

### 6.2 The enqueue-first-then-commit dance

`_enqueue_and_persist_run` (`backend/src/msai/api/symbol_onboarding.py:203-317`) is the shared helper for `/onboard` and `/repair`. The order matters and the comments in the file enumerate every branch:

1. `SELECT FOR UPDATE` on `job_id_digest`. If a row exists → return 200 OK with the existing `run_id` (no enqueue).
2. `enqueue_job` with `_job_id=digest_str`. Three sub-branches:
   - Known infra error (Redis down, etc.) → 503 `QUEUE_UNAVAILABLE`, no row committed.
   - Unknown exception → propagate so programmer errors don't masquerade as 503.
   - Returns `None` (race against another writer who enqueued first) → sleep 100ms, re-`SELECT`, return 200 if the row materialized, 409 `DUPLICATE_IN_FLIGHT` if not.
3. Commit the row in `PENDING`. If commit fails → rollback + best-effort `abort_job` (logged at WARN if abort itself fails so orphan jobs are diagnosable) + re-raise.

The result: every `(API → enqueue → commit)` triple is either committed atomically or completely undone. There is no state where the row exists without a job, or vice versa.

### 6.3 What "same request, same job_id" buys you

Because the digest is deterministic, the safe default for any caller is "just retry on network error." If the prior call landed and you're hitting a flaky proxy, the second call gets 200 OK with the same `run_id` and you converge. If the prior call was ahead of you in the race, the same — you converge on the same row.

The duplicate response is identified by the HTTP status (`200 OK` vs `202 Accepted`), not just the body. Treat that as the contract.

### 6.4 Repair preserves the digest separation

The repair endpoint creates a **child** `SymbolOnboardingRun` with its own digest — the parent row stays intact for audit. The child digest extends `_dedup_job_id` with `extra_parts=(parent_run_id_str,)` so retrying the same repair scope produces the same child digest, but a fresh repair against a different scope is a different row.

---

## 7. Rollback and repair

### 7.1 The repair endpoint

`POST /api/v1/symbols/onboard/{run_id}/repair` (`backend/src/msai/api/symbol_onboarding.py:433-516`) re-runs **only the failed symbols** of a parent run. Two scopes:

- **No body**: retry every symbol in the parent that ended `failed`.
- **Body `{"symbols": ["AAPL", "ES"]}`**: retry only the listed subset (must all be from the parent's failed set; otherwise 422).

What it does:

1. Validate the parent run exists (404 if not), is **not** still in progress (409 if it is), and the requested scope is non-empty + a subset of the parent's failed symbols (422 otherwise).
2. Materialize the scope as a fresh `OnboardRequest` with the same `cost_ceiling_usd`, `request_live_qualification`, and per-symbol date windows.
3. Call `_enqueue_and_persist_run` with `extra_parts=(parent_run_id_hex,)` so the child digest can never collide with a fresh `/onboard` digest.
4. Return 202 Accepted with the child `run_id`.

The parent row is **never modified** by repair — it stays as the audit record of what failed. You poll the child `run_id` for the repair outcome; if the repair itself fails, you can repair the child (chain length is unbounded but each link costs one row).

### 7.2 What repair does **not** do

- Does not delete the parent `SymbolOnboardingRun`. Phase 1 has no UI and no API to delete a run — the audit trail is intentional. Successful onboards and failed onboards alike persist forever.
- Does not roll back partial writes to `instrument_definitions` / `instrument_aliases`. If bootstrap succeeded but ingest failed, the registry rows stay registered — the operator's repair will see them as warm hits and skip straight to ingest.
- Does not delete Parquet files. A failed coverage check leaves whatever was written in place; repair retries the ingest and reconciles.

### 7.3 Manual alias close-out

If you need to retire an alias entirely (e.g., a bad refresh introduced a wrong `routing_venue`), there is no API for it. The runbook is `docs/runbooks/instrument-cache-migration.md` — it walks through setting `effective_to` on the offending alias row directly in Postgres via Alembic-managed SQL, then re-running `msai instruments refresh` to mint the corrected row.

The CHECK constraint on `effective_to >= effective_from` (relaxed from strict `>` by migration `b6c7d8e9f0a1`) means same-day rotations work — you can close out a row at today's date and replace it with another row stamped `effective_from=today`.

### 7.4 Cancelling an in-flight run

There is no cancel endpoint. The pragmatic approach is:

- If the run is still `PENDING` (in arq queue, not yet picked up): kill the arq queue (`docker compose ... restart worker-ingest`) — the job will be lost. The run row stays at `PENDING` forever; it's harmless but cosmetically annoying.
- If the run is `IN_PROGRESS`: let it finish. Per-symbol failures don't block the row from reaching a terminal state; the worker's outer try/except (`workers/symbol_onboarding_job.py:130-163`) will eventually flip the row to `COMPLETED_WITH_FAILURES` or (on systemic crash) `FAILED`. Then `/repair` the failed subset.

---

## 8. Key files

| Path                                                                        | What lives there                                                         |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `backend/src/msai/api/symbol_onboarding.py`                                 | Onboard router (dry-run, onboard, status, repair, readiness)             |
| `:110-157` `onboard_dry_run`                                                | Pure preflight cost estimate                                             |
| `:178-200` `_dedup_job_id`                                                  | blake2b idempotency digest                                               |
| `:203-317` `_enqueue_and_persist_run`                                       | Enqueue-first-then-commit helper                                         |
| `:320-383` `onboard`                                                        | Start onboarding (202 Accepted)                                          |
| `:386-430` `onboard_status`                                                 | Poll job progress                                                        |
| `:433-516` `onboard_repair`                                                 | Re-run failed subset as child run                                        |
| `:519-531` `_summarize`                                                     | Aggregate progress counts                                                |
| `:534-554` `_suggest_next_action`                                           | Error-code → operator hint mapping                                       |
| `:557-641` `readiness`                                                      | Three-state per-instrument readiness aggregate                           |
| `backend/src/msai/api/instruments.py`                                       | Instruments router                                                       |
| `:40-85` `bootstrap_instruments`                                            | Batch bootstrap (200 / 207 / 422)                                        |
| `backend/src/msai/schemas/symbol_onboarding.py`                             | Onboarding request/response schemas                                      |
| `:27-35` `SymbolStepStatus`                                                 | Per-symbol step enum                                                     |
| `:38-42` `SymbolStatus`                                                     | Per-symbol status enum                                                   |
| `:45-50` `RunStatus`                                                        | Run-level status enum                                                    |
| `:53` `AssetClass`                                                          | API-surface literal: `equity \| futures \| fx \| option`                 |
| `:56-77` `OnboardSymbolSpec`                                                | Per-symbol entry; symbol regex + date validators                         |
| `:80-102` `OnboardRequest`                                                  | Onboard / dry-run / repair request; 100-symbol cap, kebab name           |
| `:105-115` `SymbolStateRow`                                                 | Per-symbol state in `StatusResponse`                                     |
| `:118-123` `OnboardProgress`                                                | Aggregate counts                                                         |
| `:134-141` `StatusResponse`                                                 | Status poll response                                                     |
| `:144-151` `DryRunResponse`                                                 | Dry-run cost estimate response                                           |
| `:154-165` `ReadinessResponse`                                              | Readiness query response                                                 |
| `backend/src/msai/workers/symbol_onboarding_job.py`                         | arq worker entrypoint                                                    |
| `:51-163` `run_symbol_onboarding`                                           | Parent task; orchestrates per-symbol pipeline                            |
| `:166-178` `_hydrate_specs`                                                 | Reconstruct `OnboardSymbolSpec` list from JSONB                          |
| `:181-192` `_compute_terminal_status`                                       | Per-symbol → run-level terminal state                                    |
| `backend/src/msai/services/symbol_onboarding/orchestrator.py`               | Per-symbol seam                                                          |
| `:62-70` `_BOOTSTRAP_FAILURE_OUTCOMES`                                      | Outcomes that terminate the per-symbol pipeline                          |
| `:73-83` `OrchestratorBootstrapProto`                                       | Bootstrap-service injection seam                                         |
| `:85-89` `OrchestratorIBProto`                                              | IB-qualifier injection seam                                              |
| `:92+` `_onboard_one_symbol`                                                | bootstrap → ingest → coverage → ib_qualify                               |
| `backend/src/msai/services/symbol_onboarding/cost_estimator.py`             | Databento cost estimation                                                |
| `:28-31` `_ASSET_TO_DATASET`                                                | equity → XNAS.ITCH, futures → GLBX.MDP3                                  |
| `:34-51` `UnpriceableAssetClassError`                                       | Raised when asset_class lacks a Databento dataset mapping (422)          |
| `:88-174` `estimate_cost`                                                   | Bucketed `metadata.get_cost` integration                                 |
| `backend/src/msai/services/symbol_onboarding/coverage.py`                   | Parquet catalog coverage scan                                            |
| `:26-62` `compute_coverage`                                                 | full / gapped / none determination                                       |
| `:103-107` `_apply_trailing_edge_tolerance`                                 | Suppress recent-date misses (last 7 days)                                |
| `backend/src/msai/services/symbol_onboarding/__init__.py`                   | Asset-class normalization                                                |
| `:24-36` `normalize_asset_class_for_ingest`                                 | Registry → ingest taxonomy bridge (fail-loud)                            |
| `backend/src/msai/services/nautilus/security_master/registry.py`            | Low-level alias lookup                                                   |
| `:37-66` `AmbiguousSymbolError`                                             | Raised when raw_symbol matches multiple asset classes                    |
| `:73-117` `find_by_alias`                                                   | Alias-string + provider + as_of_date → definition                        |
| `:119-157` `find_by_aliases_bulk`                                           | One SELECT for batch                                                     |
| `:159-196` `find_by_raw_symbol`                                             | raw_symbol + provider ± asset_class → definition                         |
| `:198-219` `require_definition`                                             | `find_by_alias` that raises on miss                                      |
| `backend/src/msai/services/nautilus/security_master/service.py`             | High-level resolver                                                      |
| `:98-116` `compute_advisory_lock_key`                                       | Postgres int8 advisory-lock key                                          |
| `:119-130` `compute_blake2b_digest_key`                                     | Generic blake2b digest helper (used by `_dedup_job_id`)                  |
| `:133-150` `DatabentoDefinitionMissing` / `DatabentoClientUnavailableError` | Resolver-side exceptions                                                 |
| `:154-176` `AliasResolution`                                                | Aggregate readiness dataclass                                            |
| `:196-209` `SecurityMaster.__init__`                                        | qualifier / databento_client injection                                   |
| `:211-251` `resolve`                                                        | Single-spec hot-path                                                     |
| `:253-285` `bulk_resolve`                                                   | Batched warm-hit + residual qualification                                |
| `:400-503` `resolve_for_backtest`                                           | Backtest canonical-id resolution (honors `start` for windowing)          |
| `:505-595` `_resolve_databento_continuous`                                  | `<root>.Z.<N>` warm-hit or synthesize                                    |
| `:686-790` `find_active_aliases`                                            | Aggregate readiness view (provider preference at `:778-783`)             |
| `backend/src/msai/services/nautilus/security_master/types.py`               | Type bridges                                                             |
| `:14` `RegistryAssetClass`                                                  | `equity \| futures \| fx \| option \| crypto`                            |
| `:19` `IngestAssetClass`                                                    | `stocks \| futures \| options \| forex \| crypto`                        |
| `:22` `Provider`                                                            | `interactive_brokers \| databento`                                       |
| `:26` `VenueFormat`                                                         | `exchange_name \| mic_code \| databento_continuous`                      |
| `:32-50` `REGISTRY_TO_INGEST_ASSET_CLASS`                                   | Canonical taxonomy map (shared by SecurityMaster + orchestrator)         |
| `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`           | Time-aware helpers                                                       |
| `:61-69` `exchange_local_today`                                             | CME-local today (writer + reader)                                        |
| `:72-82` `third_friday_of`                                                  | CME quarterly expiration helper                                          |
| `:85-104` `current_quarterly_expiry`                                        | Next quarterly contract id                                               |
| `backend/src/msai/models/symbol_onboarding_run.py`                          | Run row model                                                            |
| `:23-28` `SymbolOnboardingRunStatus`                                        | Run-level enum                                                           |
| `:31-80` `SymbolOnboardingRun`                                              | Table model + constraints                                                |
| `backend/src/msai/models/instrument_definition.py`                          | Definitions table                                                        |
| `:34-95` `InstrumentDefinition`                                             | UUID-keyed; unique on `(raw_symbol, provider, asset_class)`              |
| `backend/src/msai/models/instrument_alias.py`                               | Aliases table                                                            |
| `:33-86` `InstrumentAlias`                                                  | Effective-windowed; unique on `(alias_string, provider, effective_from)` |
| `backend/src/msai/cli.py`                                                   | Top-level CLI                                                            |
| `:785-979` `instruments refresh`                                            | Pre-warm registry via Databento or IB                                    |
| `:1175-1277` `instruments bootstrap`                                        | Databento batch bootstrap (handles 207)                                  |
| `:1282-1284` `symbols` sub-app registration                                 | Wire `cli_symbols` into `msai`                                           |
| `backend/src/msai/cli_symbols.py`                                           | Symbols sub-app                                                          |
| `:17-75` `onboard`                                                          | `--manifest` + optional `--dry-run`                                      |
| `:78-96` `status`                                                           | `--watch` polls every 5s                                                 |
| `:99-111` `repair`                                                          | `--symbols` to scope                                                     |
| `:148-154` `_exit_for_status`                                               | CLI exit codes (0/1/2/3)                                                 |
| `frontend/src/app/data-management/page.tsx`                                 | Data-management UI page                                                  |
| `:32-134` `DataManagementPage`                                              | Read-only browse of registered symbols                                   |
| `backend/alembic/versions/v0q1r2s3t4u5_instrument_registry.py`              | Registry tables migration                                                |
| `:19-93` `instrument_definitions`                                           | Initial table create                                                     |
| `:95-160+` `instrument_aliases`                                             | Initial table create                                                     |
| `backend/alembic/versions/b6c7d8e9f0a1_*.py`                                | Relax `effective_window` CHECK for same-day rotations                    |
| `backend/alembic/versions/d1e2f3g4h5i6_*.py`                                | Add `trading_hours` JSONB to `instrument_definitions`                    |
| `backend/alembic/versions/a5b6c7d8e9f0_*.py`                                | Add `source_venue_raw` to `instrument_aliases`                           |
| `backend/alembic/versions/e2f3g4h5i6j7_*.py`                                | Drop deprecated `instrument_cache` table (migration to registry)         |
| `docs/runbooks/instrument-cache-migration.md`                               | Manual alias close-out + cache-to-registry migration runbook             |

---

**Date verified against codebase:** 2026-04-28
**Previous doc:** [← The Developer Journey](00-developer-journey.md)
**Next doc:** [How Strategies Work →](how-strategies-work.md)
