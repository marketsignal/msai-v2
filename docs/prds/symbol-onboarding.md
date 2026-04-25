# PRD: Symbol Onboarding

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-24
**Last Updated:** 2026-04-24

---

## 1. Overview

Ship a thin orchestration layer that turns symbol intent (declared as git-tracked `watchlists/*.yaml` manifests) into ready-to-backtest historical data — composing the three primitives that already exist after PR #44: `/instruments/bootstrap` (Databento registry), `/market-data/ingest` (arq-queued Parquet backfill), and optional `msai instruments refresh --provider interactive_brokers` (IB live-qualification). The feature removes the current manual, three-step-in-a-row friction between "I want SPY + ES.n.0 + IWM + AAPL for 2021-01-01 → today" and running a backtest against them. **Single-user v1** (Pablo), API-primary, CLI-secondary, UI deferred.

## 2. Goals & Success Metrics

### Goals

- **Primary:** one command (`msai symbols onboard watchlists/core.yaml`) or one API call takes Pablo from an empty catalog to backtest-ready in minutes, without manually running bootstrap → ingest → refresh.
- **Secondary:** eliminate manual SQL / manual registry seeding (which `run_auto_heal` and bootstrap still require occasionally when universe intent is new).
- **Secondary:** make symbol intent auditable via git history on `watchlists/*.yaml` (same model as `strategies/*.py`).
- **Secondary:** prevent accidental wallet-drain by requiring preflight cost estimate + ceiling.

### Success Metrics

| Metric                                        | Target                                                      | How Measured                                                                       |
| --------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Cold-start to backtest-ready (5 equities, 1y) | < 3 min wall-clock                                          | `msai symbols onboard` total duration in integration test                          |
| Zero manual SQL seeds needed                  | 0 over a full session of new-symbol introduction            | Session log review; no `docker exec … psql -c "INSERT"` in runbooks                |
| Coverage correctness on idempotent re-apply   | 100% no re-download of already-covered months               | Integration test asserts Databento call-count unchanged on second `apply`          |
| `backtest_data_available` never lies          | 100% scoped by (provider, window)                           | Unit + integration tests reject any code path that returns a symbol-global `true`  |
| Budget ceiling prevents accidental overspend  | 100% of runs above `cost_ceiling_usd` halt                  | Integration test with mocked Databento `metadata.get_cost` returning high estimate |
| Partial-batch failure clarity                 | Per-symbol `step` + `error` + `next_action` in every result | Response schema lint + E2E                                                         |

### Non-Goals (Explicitly Out of Scope)

- ❌ **UI surface** (`/universe` Next.js page). v1 is API + CLI only. UI deferred post-v1 once API shape is proven.
- ❌ **Cancel mid-run.** Defer until jobs demonstrably hurt; single-user workload doesn't justify the state machine.
- ❌ **Rollback semantics.** Partial ingest persists; no transactional undo across Databento fetches.
- ❌ **Silent auto-retry** on coverage gaps. Many gaps are permanent (delistings, entitlements); silent retries burn money and hide bugs.
- ❌ **Per-strategy manifests** (`strategies/<strat>/watchlist.yaml`). One symbol in two strategies would duplicate; wrong ownership model.
- ❌ **Single top-level `watchlist.yaml`.** Doesn't scale past one list; force named files from day one.
- ❌ **DB-backed watchlist CRUD.** Watchlists are YAML files, period; git is the audit trail.
- ❌ **1-second / tick bars.** Different Databento schema, ~60× cost; separate PRD.
- ❌ **Multi-user / RBAC / sharing.** Explicit single-user product per `CLAUDE.md`.
- ❌ **Cron-based onboarding scheduler.** Manual apply only in v1.
- ❌ **New data providers** (Polygon, Alpaca, etc.). Databento + IB only.
- ❌ **Auto-extend on repeat calls.** Hidden extension is how wallets get drained.
- ❌ **Re-materialize catalog eagerly after ingest.** Keep PR #16's lazy path.

## 3. User Personas

### Pablo (operator + trader)

- **Role:** single human operator. Writes strategies, configures universes, runs backtests, graduates to paper/live.
- **Permissions:** full CRUD on watchlist files (via git, not API); full access to `/symbols/onboard` API + `msai symbols …` CLI.
- **Goals:** declare "I care about these N symbols over this window" once, get backtest-ready, iterate fast.

### Read-only API/UI consumers (future-proof)

- **Role:** internal — strategies reading "is SPY live-qualified?" at `on_start`, dashboards showing coverage state, pre-live-deploy checks.
- **Permissions:** can `GET` watchlist names, coverage, readiness; cannot `POST /symbols/onboard` or edit watchlist files.
- **Goals:** cheap, non-mutating reads that don't contend with in-flight onboarding jobs.

---

## 4. User Stories

### US-001: Declare a universe via YAML manifest

**As** Pablo
**I want** to define a named set of symbols with explicit date windows in `watchlists/*.yaml`
**So that** my universe is git-versioned alongside my strategies and reviewable as a diff

**Scenario:**

```gherkin
Given an empty `watchlists/` directory
When I create `watchlists/core-equities.yaml`:
  """
  name: core-equities
  symbols:
    - { symbol: SPY,    asset_class: equity, start: 2021-01-01, end: 2025-12-31 }
    - { symbol: AAPL,   asset_class: equity, start: 2021-01-01, end: 2025-12-31 }
    - { symbol: IWM,    asset_class: equity, start: 2022-01-01, end: 2025-12-31 }
    - { symbol: ES.n.0, asset_class: futures, start: 2023-01-01, end: 2025-12-31 }
  request_live_qualification: false
  """
And I commit it to the repo
Then the file is the source of truth for this watchlist's intent
And running `msai symbols validate watchlists/core-equities.yaml` parses successfully
And duplicate entries (same symbol + asset_class in two watchlists) are detected on batch-validate
```

**Acceptance Criteria:**

- [ ] YAML schema validates: `name`, `symbols` (list of `{symbol, asset_class, start, end}`), optional `request_live_qualification` (default `false`), optional `trailing` sugar (e.g. `start: trailing_5y`).
- [ ] `asset_class` restricted to `equity | futures | fx | option` (matches the PR #44 Pydantic Literal).
- [ ] `start` and `end` REQUIRED per symbol (no hidden default like "last 5 years").
- [ ] CLI sugar `trailing_5y` / `trailing_1y` is client-side-expanded; server always sees concrete ISO dates.
- [ ] Manifest parse errors include file path + line number + specific field.
- [ ] Cross-watchlist dedup rule: when two watchlists both contain `(SPY, equity)`, the wider date window wins; log the dedup decision.

**Edge Cases:**

| Condition                               | Expected Behavior                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------- |
| `start` in future                       | Validation error: "start must be <= today"                                    |
| `end` < `start`                         | Validation error: "end must be >= start"                                      |
| Unknown `asset_class`                   | Validation error lists allowed values                                         |
| Duplicate `symbol` within one watchlist | Validation error, or implicit merge with the widest window — decision in Plan |
| Manifest file with zero symbols         | Validation error: "symbols list cannot be empty"                              |
| Symbol in two different watchlists      | Allowed; dedup at compile time, wider window wins, log the decision           |

**Priority:** Must Have

---

### US-002: Onboard a watchlist via API (async job)

**As** Pablo
**I want** `POST /api/v1/symbols/onboard` to accept a watchlist body and return `202 + job_id`
**So that** I don't block on a multi-minute ingest over an HTTP connection

**Scenario:**

```gherkin
Given I POST `/api/v1/symbols/onboard`:
  """
  {
    "watchlist_name": "core-equities",
    "symbols": [ … SPY, AAPL, IWM, ES.n.0 … ],
    "request_live_qualification": false,
    "cost_ceiling_usd": 25.00
  }
  """
When the request is accepted
Then I receive HTTP 202 + body `{ "job_id": "<uuid>" }`
And the orchestrator enqueues an arq job that:
  1. Calls `/instruments/bootstrap` for every new symbol (PR #44)
  2. Calls `/market-data/ingest` for every (symbol, window) tuple not already covered
  3. Calls `msai instruments refresh --provider interactive_brokers` for symbols only if `request_live_qualification=true`
And `GET /api/v1/symbols/onboard/{job_id}/status` returns job state + per-symbol progress
```

**Acceptance Criteria:**

- [ ] `POST /api/v1/symbols/onboard` returns `202` with `{ "job_id": UUID, "watchlist_name": str }`.
- [ ] Request body validates with Pydantic V2; invalid input returns `422` with the project's canonical `{"error": {"code", "message", "details"}}` envelope.
- [ ] Orchestrator is an arq job (reuses project queue, not a new worker type).
- [ ] `/market-data/ingest` is the lower-level primitive; onboarding fans out per-symbol rather than reimplementing ingest.
- [ ] Idempotent: submitting the same watchlist twice only re-enqueues uncovered ranges.
- [ ] Concurrency cap: at most 3 in-flight Databento calls per job (mirrors PR #44 `max_concurrent=3`).

**Edge Cases:**

| Condition                                             | Expected Behavior                                                       |
| ----------------------------------------------------- | ----------------------------------------------------------------------- |
| Watchlist body references unknown symbol              | Per-symbol failure propagates; other symbols continue (continue-others) |
| Watchlist includes already-ingested window            | No new ingest job enqueued; orchestrator marks symbol `already_covered` |
| `request_live_qualification=true` but IB Gateway down | Per-symbol failure on IB step; bootstrap+ingest still succeed (partial) |
| Missing `DATABENTO_API_KEY`                           | `500 DATABENTO_NOT_CONFIGURED` (reuses PR #44 error helper)             |
| Missing `cost_ceiling_usd`                            | Use `MSAI_MAX_INGEST_USD` env default; if neither set, require explicit |

**Priority:** Must Have

---

### US-003: Poll job status with per-symbol progress

**As** Pablo
**I want** `GET /api/v1/symbols/onboard/{job_id}/status` to show per-symbol state
**So that** I know exactly what succeeded, what failed, and what to do next

**Scenario:**

```gherkin
Given an onboarding job with 4 symbols
When I GET `/api/v1/symbols/onboard/{job_id}/status`
Then response body shape:
  """
  {
    "job_id": "...",
    "watchlist_name": "core-equities",
    "status": "in_progress" | "completed" | "completed_with_failures" | "failed",
    "progress": { "total": 4, "succeeded": 2, "failed": 0, "in_progress": 2 },
    "symbol_states": [
      { "symbol": "SPY",    "step": "backfilling", "status": "in_progress", "error": null,  "next_action": null },
      { "symbol": "AAPL",   "step": "completed",   "status": "ok",          "error": null,  "next_action": null },
      { "symbol": "IWM",    "step": "registering", "status": "in_progress", "error": null,  "next_action": null },
      { "symbol": "ES.n.0", "step": "completed",   "status": "ok",          "error": null,  "next_action": null }
    ]
  }
  """
```

**Acceptance Criteria:**

- [ ] `step` values: `registering` / `backfilling` / `qualifying_live` / `completed` (matches council-ratified names).
- [ ] `status` per symbol: `ok` / `failed` / `in_progress` / `not_started` / `already_covered`.
- [ ] On every `failed`, `error` is structured (not free text) and `next_action` is operator-friendly (e.g. `"Extend Databento entitlement for XNYS.PILLAR"`, `"Retry with msai symbols repair {watchlist_name} {symbol}"`, `"Run IB Gateway (COMPOSE_PROFILES=broker) and retry"`).
- [ ] Job `status=completed_with_failures` when any symbol failed; `status=failed` only when a systemic fault (auth, rate-limit ceiling, provider outage) occurred.
- [ ] Response is JSON; no HTML, no streaming SSE in v1 (polling only).

**Edge Cases:**

| Condition                                 | Expected Behavior                                                                     |
| ----------------------------------------- | ------------------------------------------------------------------------------------- |
| Unknown `job_id`                          | `404` with envelope code `JOB_NOT_FOUND`                                              |
| Job still enqueued (not started)          | `status=pending`, all symbols `not_started`                                           |
| Systemic Databento auth failure mid-batch | Remaining symbols marked `not_started`; job `failed` with `error.code=DATABENTO_AUTH` |
| All symbols already covered               | Job `completed` in < 5s; per-symbol `status=already_covered`                          |

**Priority:** Must Have

---

### US-004: Preflight cost estimate + spend ceiling

**As** Pablo
**I want** to know "this batch will cost about $N ± error" before I commit to ingest
**So that** I don't accidentally spend $500 on Databento from a typo

**Scenario:**

```gherkin
Given a watchlist with 50 symbols × 5 years of minute bars
When I POST `/api/v1/symbols/onboard?dry_run=true`
Then response body:
  """
  {
    "watchlist_name": "core-equities",
    "dry_run": true,
    "estimated_cost_usd": 12.50,
    "estimate_basis": "databento.metadata.get_cost(...)",
    "estimate_confidence": "medium",
    "breakdown": [ { "symbol": "...", "dataset": "...", "usd": 0.25 }, ... ],
    "symbol_count": 50,
    "total_bytes_estimate": 4500000000
  }
  """
And when I POST `/api/v1/symbols/onboard` (no dry_run, with `cost_ceiling_usd: 10.00`)
Then response = `422` with envelope `{ "error": { "code": "COST_CEILING_EXCEEDED", "details": { "estimated": 12.50, "ceiling": 10.00 } } }`
And the arq job is NOT enqueued
```

**Acceptance Criteria:**

- [ ] `GET /api/v1/symbols/onboard/dry-run` (or `POST /api/v1/symbols/onboard?dry_run=true`) returns estimate synchronously.
- [ ] `estimated_cost_usd` is a float; `estimate_confidence` ∈ `{high, medium, low}`.
- [ ] `estimate_basis` is a short string explaining the source (e.g. `databento.metadata.get_cost`).
- [ ] Execution request honors `cost_ceiling_usd`; fails closed with `422 COST_CEILING_EXCEEDED` if estimate > ceiling.
- [ ] Env var `MSAI_MAX_INGEST_USD` (default `$25.00`) is the fallback ceiling when request omits `cost_ceiling_usd`.
- [ ] If Databento `metadata.get_cost` fails, the preflight returns `503 ESTIMATOR_UNAVAILABLE` — do NOT proceed with a guess.
- [ ] CLI surface: `msai symbols onboard --dry-run watchlists/core.yaml` prints the estimate; normal invocation without `--confirm-spend $N` fails closed if estimate > default ceiling.

**Edge Cases:**

| Condition                          | Expected Behavior                                                        |
| ---------------------------------- | ------------------------------------------------------------------------ |
| Symbol outside Databento catalog   | Estimate treats as $0 with confidence `low`; actual run fails per-symbol |
| Databento API down during estimate | `503 ESTIMATOR_UNAVAILABLE` — don't ship with fake precision             |
| `cost_ceiling_usd: 0.00`           | Validated, but rejects any non-zero estimate — effectively no-op dry-run |
| IB-only flow (no Databento calls)  | `estimated_cost_usd: 0.00` with confidence `high`                        |

**Priority:** Must Have

---

### US-005: Partial-batch failure with per-symbol next action

**As** Pablo
**I want** one bad symbol (typo, delisted, entitlement missing) to NOT block the other 19 in a batch
**So that** I can fix or remove the failing one and move on

**Scenario:**

```gherkin
Given a 20-symbol onboarding request
And "XYZW" is an unknown ticker in all 3 Databento equity datasets
When the job completes
Then 19 symbols are `ok` and XYZW is `failed` with:
  - step: "registering"
  - error: { "code": "UPSTREAM_SYMBOL_NOT_FOUND", "message": "XYZW not found in XNAS.ITCH / XNYS.PILLAR / ARCX.PILLAR" }
  - next_action: "Remove XYZW from watchlists/core-equities.yaml OR verify the correct Databento dataset"
And job.status = "completed_with_failures"
And exit code of `msai symbols onboard` is 0 (batch completed; check per-symbol for failures)
```

**Acceptance Criteria:**

- [ ] Per-symbol failure doesn't abort the batch (reuse PR #44's 207-style continuation).
- [ ] Systemic failure (auth 401, provider-wide rate-limit exhaustion, IB Gateway unreachable when `request_live_qualification=true`) DOES abort the batch. Subsequent symbols marked `not_started` with `error.code` pointing at the systemic cause.
- [ ] CLI exit code: `0` when job reached `completed` OR `completed_with_failures`; non-zero only on `failed` (systemic) or `ESTIMATOR_UNAVAILABLE`.
- [ ] Every `failed` symbol has a non-empty `next_action` — operator should never be stuck reading source to diagnose.

**Edge Cases:**

| Condition                                                  | Expected Behavior                                                     |
| ---------------------------------------------------------- | --------------------------------------------------------------------- |
| Transient Databento 5xx on one symbol                      | tenacity retry (existing); if exhausted, per-symbol failure, continue |
| Databento-wide 429 rate-limit                              | Systemic; abort batch, mark remaining `not_started`                   |
| One symbol's IB qualification fails but the others succeed | IB step is per-symbol; partial OK                                     |

**Priority:** Must Have

---

### US-006: Window + provider-scoped `backtest_data_available`

**As** a strategy/operator reading readiness state
**I want** `backtest_data_available` to always be scoped by `(provider, window)`
**So that** the API never lies about whether AAPL is ready for my specific backtest range

**Scenario:**

```gherkin
Given AAPL has been ingested for 2023-01-01 → 2024-12-31 via Databento
And I GET `/api/v1/instruments/{id}/readiness?provider=databento&start=2023-01-01&end=2024-12-31`
Then response: `{ "registered": true, "backtest_data_available": true, "live_qualified": false, "covered_range": "2023-01-01 → 2024-12-31", "provider": "databento" }`
And when I GET `/api/v1/instruments/{id}/readiness?provider=databento&start=2021-01-01&end=2024-12-31`
Then response: `{ "registered": true, "backtest_data_available": false, "live_qualified": false, "coverage_status": "gapped", "missing_ranges": ["2021-01-01 → 2022-12-31"], "provider": "databento" }`
And when I GET `/api/v1/instruments/{id}/readiness` (no window in query)
Then response: `{ "registered": true, "backtest_data_available": null, "live_qualified": false, "coverage_summary": "partial, 2y coverage" }` — never `true` without a window
```

**Acceptance Criteria:**

- [ ] `backtest_data_available` return type is `bool | None`. `None` when no window is in scope.
- [ ] Extended response schema includes `covered_range`, `coverage_status ∈ {full, gapped, none}`, `missing_ranges: list[DateRange]` when gapped.
- [ ] List endpoints (`GET /api/v1/instruments/`) that don't take a window return `coverage_summary: str` (human-friendly), NOT a symbol-global boolean.
- [ ] Pydantic schema migration: the `BootstrapResultItem` field `backtest_data_available: bool | None` stays — now documented as "requires window context at read time."
- [ ] Unit test rejects any code path that returns `backtest_data_available=True` without a window in scope.

**Edge Cases:**

| Condition                                           | Expected Behavior                                                           |
| --------------------------------------------------- | --------------------------------------------------------------------------- |
| Symbol ingested for multiple non-contiguous ranges  | `coverage_status=gapped` with 2+ entries in `missing_ranges`                |
| Symbol registered but no data ingested              | `coverage_status=none`, `backtest_data_available=false`                     |
| Symbol covered by Polygon but asked about Databento | Scoped by provider; `backtest_data_available=false` for Databento query     |
| Symbol fully covered for requested window           | `backtest_data_available=true`, `coverage_status=full`, `missing_ranges=[]` |

**Priority:** Must Have (this is the Contrarian's pin #3 amendment)

---

### US-007: Explicit repair ("fill gaps") action

**As** Pablo
**I want** an explicit repair command that only re-ingests gap ranges, not the full window
**So that** fixing a holiday-week gap doesn't re-download 2 years of data

**Scenario:**

```gherkin
Given AAPL is covered 2023-01-01 → 2024-12-31 EXCEPT 2023-07-03 → 2023-07-07 (holiday-week feed glitch)
When I call `POST /api/v1/symbols/repair` with:
  """
  { "watchlist_name": "core-equities", "symbols": ["AAPL"] }
  """
Then the job ONLY ingests the missing range 2023-07-03 → 2023-07-07
And on completion, AAPL.coverage_status = "full"
And CLI equivalent: `msai symbols repair core-equities --symbols AAPL`
```

**Acceptance Criteria:**

- [ ] New endpoint `POST /api/v1/symbols/repair` or reuse `/symbols/onboard` with a `mode=repair` flag (Plan decides).
- [ ] Repair job is a first-class arq job type; same status/polling API as onboard.
- [ ] Repair only enqueues missing ranges (reuses `coverage_status=gapped` detection from US-006).
- [ ] Repair NEVER silently fires when `run_auto_heal` runs during a backtest — repair is operator-initiated; auto-heal stays failure-initiated; they don't race.
- [ ] If repair leaves a residual gap (entitlement missing, permanent delisting), per-symbol `next_action` names the permanent cause.

**Edge Cases:**

| Condition                             | Expected Behavior                                                  |
| ------------------------------------- | ------------------------------------------------------------------ |
| No gaps exist                         | Repair returns `status=completed`, zero symbols processed          |
| Gap spans a delisting date            | Repair fails per-symbol with `error.code=SYMBOL_DELISTED_IN_RANGE` |
| Gap in `live_qualified` IB alias only | Repair re-runs IB qualification; does NOT re-ingest historical     |

**Priority:** Should Have

---

### US-008: CLI — `msai symbols onboard`

**As** Pablo
**I want** `msai symbols onboard watchlists/core-equities.yaml` to behave identically to the API
**So that** git + YAML + one command is the common flow

**Scenario:**

```gherkin
Given I edit watchlists/core-equities.yaml and git-commit
When I run `msai symbols onboard watchlists/core-equities.yaml`
Then stderr shows progress:
  """
  Loaded watchlist 'core-equities' (4 symbols)
  Preflight estimate: $2.40 (confidence: medium) — budget ceiling $25.00 ✓
  Onboarding job <uuid> enqueued
  [12s] SPY → ok (registering → backfilling → completed)
  [18s] AAPL → ok
  [21s] IWM → ok
  [35s] ES.n.0 → ok
  Job completed: 4/4 ok, 0 failed
  """
And stdout is valid JSON (the full job status response) for programmatic use
And exit code is 0
```

**Acceptance Criteria:**

- [ ] CLI command: `msai symbols onboard <manifest-file>` (positional arg).
- [ ] Flags: `--dry-run`, `--max-cost $N`, `--live-qualify`, `--json` (stdout-only JSON, suppress stderr progress).
- [ ] Preflight cost check is automatic; `--confirm-spend $N` override required if estimate > default ceiling.
- [ ] Stderr is human-readable progress; stdout is clean JSON (so `| jq` works).
- [ ] Exit code: `0` on `completed` / `completed_with_failures`; non-zero on `failed` / `ESTIMATOR_UNAVAILABLE` / `COST_CEILING_EXCEEDED`.
- [ ] Authentication: same pattern as PR #44 — `X-API-Key` header from `MSAI_API_KEY` env, or OAuth JWT.

**Edge Cases:**

| Condition                                    | Expected Behavior                                                        |
| -------------------------------------------- | ------------------------------------------------------------------------ |
| Manifest file doesn't exist                  | Exit code 2, stderr "file not found: <path>"                             |
| Manifest YAML parse error                    | Exit code 2, stderr includes line number + field                         |
| Network failure reaching API                 | Exit code 3, stderr includes the endpoint URL + retry hint               |
| `--dry-run` with `--max-cost` below estimate | Prints warning + estimate; does NOT exit non-zero (dry-run is read-only) |

**Priority:** Must Have

---

### US-009: CLI — `msai symbols status`

**As** Pablo
**I want** a single command to see the readiness of every symbol in every watchlist
**So that** "what's ready to backtest?" is a 1-second lookup

**Scenario:**

```gherkin
Given 3 watchlists exist with 15 symbols combined
When I run `msai symbols status`
Then stderr/stdout shows:
  """
  Watchlist: core-equities (4 symbols)
    SPY    | registered ✓ | backtest 2023–2024 ✓ | live ✗
    AAPL   | registered ✓ | backtest 2023–2024 ✓ | live ✓
    IWM    | registered ✓ | backtest 2023–2024 ✓ | live ✗
    ES.n.0 | registered ✓ | backtest 2023–2024 ✓ | live ✗

  Watchlist: vol-regimes (5 symbols)
    VXX    | registered ✓ | backtest 2022–2024 (gapped: 2023-03) | live ✗
    ...

  Watchlist: fx-majors (6 symbols)
    EUR/USD | registered ✓ | backtest 2023–2024 ✓ | live ✗
    ...

  Summary: 15 registered, 14 backtest-ready, 1 live-qualified, 1 gap needs repair
  """
And `msai symbols status <watchlist>` narrows to one watchlist
And `--json` outputs machine-readable JSON
```

**Acceptance Criteria:**

- [ ] `msai symbols status` lists all watchlists; `msai symbols status <name>` narrows.
- [ ] Default output is TTY-friendly with color/unicode-tick indicators; `--json` returns structured JSON for scripting.
- [ ] Per-symbol line includes: watchlist name, registered flag, backtest window(s), live-qualified flag, gap markers.
- [ ] Reads from API (`GET /api/v1/symbols/coverage` or equivalent) — not from DB directly.
- [ ] Exit code: `0` always (this is a read; no failure semantics).

**Edge Cases:**

| Condition                         | Expected Behavior                                  |
| --------------------------------- | -------------------------------------------------- |
| No watchlists exist               | "No watchlists found in watchlists/\*.yaml"        |
| Watchlist references unknown file | Warning line, skip; continue with others           |
| Symbol in two watchlists          | Shown under EACH watchlist; dedup count in summary |
| API unreachable                   | Exit code 3, stderr "API unreachable: <url>"       |

**Priority:** Should Have

---

### US-010: Remove `/api/v1/universe` HTTP router; keep service + table for nightly ingest

**As** a future maintainer reading the codebase
**I want** one clear source of truth for symbol intent, not two conflicting HTTP surfaces
**So that** the mental model doesn't fracture (Maintainer's blocking objection)

**Context.** A caller-grep over `frontend/`, `cli.py`, and `src/msai/` confirmed **zero production callers** of any `/api/v1/universe/*` HTTP route. The `AssetUniverseService` Python class IS used by `workers/nightly_ingest.py` (imported directly, not via HTTP), so the service + `asset_universe` table must keep working. Given zero HTTP callers, 410-Gone deprecation ceremony is unnecessary — we delete the router outright. The Maintainer's mental-model-fracture concern is still satisfied: there will be exactly one HTTP surface for symbol intent (`/api/v1/symbols/onboard`) and one internal service contract (`AssetUniverseService`, internal-only, not reachable over HTTP).

**Scenario:**

```gherkin
Given the existing `/api/v1/universe/` HTTP router (GET / POST / DELETE / POST /ingest)
When this feature ships
Then the entire router is REMOVED from `src/msai/api/asset_universe.py`
And the `universe_router` import + `app.include_router(universe_router)` are removed from `src/msai/main.py`
And all HTTP paths under `/api/v1/universe/*` return HTTP 404 (FastAPI default for unknown routes)
And `AssetUniverseService` remains importable from `src/msai/services/asset_universe.py`
And `workers/nightly_ingest.py` continues to import and use `AssetUniverseService` directly (internal Python, not HTTP)
And `asset_universe` Postgres table is untouched — nightly ingest keeps populating + reading it
And tests in `tests/unit/test_asset_universe.py` that exercised HTTP routes are DELETED; tests for the service layer are KEPT
And the `asset_universe.resolution` column decision (rename vs. drop vs. keep-as-is) is made in Phase 3 Plan based on whether `nightly_ingest.py` reads it
```

**Acceptance Criteria:**

- [ ] `src/msai/api/asset_universe.py` — delete the file (the 4 `@router.*` routes + `APIRouter` declaration + `_service` module global).
- [ ] `src/msai/main.py` — remove `from msai.api.asset_universe import router as universe_router` and `app.include_router(universe_router)`.
- [ ] `src/msai/schemas/asset_universe.py` — Plan Phase 3 decides: keep if `AssetUniverseService` still uses the schemas internally, delete if not.
- [ ] `src/msai/services/asset_universe.py` — **KEEP**. `AssetUniverseService` stays importable; no API changes.
- [ ] `src/msai/models/asset_universe.py` — **KEEP**. Table stays.
- [ ] `alembic/versions/*` — NO migration for `asset_universe.resolution` in this PR unless Plan Phase 3 confirms it's unused. If kept, add a module docstring to `asset_universe.py` explaining the field is legacy-storage-resolution (NOT user-intent bar-size).
- [ ] `src/msai/workers/nightly_ingest.py` — unchanged; still reads `AssetUniverseService`.
- [ ] `tests/unit/test_asset_universe.py` — delete tests that hit HTTP routes (via `TestClient`); keep tests that exercise `AssetUniverseService` methods directly.
- [ ] No `msai universe` CLI sub-app exists to deprecate (confirmed by grep).
- [ ] Docs updated: `docs/architecture/` notes that `/api/v1/universe` is GONE (not deprecated — deleted); new HTTP surface is `/api/v1/symbols/onboard`.

**Edge Cases:**

| Condition                                              | Expected Behavior                                                                                                                        |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| External shell script / cron hits `/api/v1/universe/*` | HTTP 404 (no 410 ceremony). Breakage surfaces immediately; script owner migrates. Accepted risk since repo grep found zero callers.      |
| Nightly ingest cron runs post-merge                    | Works unchanged — it uses `AssetUniverseService` Python, not HTTP                                                                        |
| Someone imports `msai.api.asset_universe`              | `ModuleNotFoundError` — file is deleted. Surfaces at CI time.                                                                            |
| OpenAPI schema consumer depends on `/universe/` paths  | Schema regenerates without them. API client generation breaks — if any external consumer exists, that's the migration trigger. Accepted. |

**Priority:** Must Have (Maintainer's blocking objection — mental-model integrity)

---

## 5. Technical Constraints

### Council-ratified architectural constraints (binding for implementation plan)

These come from the Phase 3 orchestrator-topology council verdict 2026-04-24 (4 OBJECT / 1 CONDITIONAL on fan-out topology; Approach 1 adopted). The Plan Phase 3 must respect these literally:

1. **Single arq entrypoint:** `run_symbol_onboarding(ctx, run_id)` — one worker, one job, one row. Task lives in `backend/src/msai/workers/symbol_onboarding_job.py`.
2. **No full-batch fan-out.** Do NOT enqueue per-symbol child arq jobs. Do NOT `asyncio.gather(_onboard_one_symbol(...))` across the whole batch.
3. **`_onboard_one_symbol(run, symbol, ...)` seam** — the per-symbol function is cleanly factored so a future in-place rewrite to bounded concurrency is a one-commit change (NO migration of state-model or task shape).
4. **Phase-local bounded concurrency is ONLY allowed inside the bootstrap phase** (mirrors `DatabentoBootstrapService` pattern: `Semaphore(max_concurrent<=3)` + `gather(..., return_exceptions=True)`). Ingest + IB qualification remain strictly sequential.
5. **`asyncio.wait_for(timeout=120)` around every IB Gateway call** inside `_onboard_one_symbol`. Hung IB sockets must not park the worker slot. On `TimeoutError` record terminal state `FAILED_IB_TIMEOUT` in `symbol_states[symbol]`.
6. **100-symbol hard cap at the API layer.** `POST /api/v1/symbols/onboard` returns `422 BATCH_TOO_LARGE` if `len(symbols) > 100`. Single-user v1 scope; chunking is a v2 problem.
7. **3 Prometheus metrics (low-cardinality, mandatory v1):**
   - `msai_onboarding_runs_total{status}` — counter. `status ∈ {started, completed, completed_with_failures, failed}`.
   - `msai_onboarding_step_duration_seconds{step}` — histogram. `step ∈ {bootstrap, ingest, ib_refresh}`. Buckets `[1, 5, 15, 60, 300, 1800]`.
   - `msai_onboarding_ib_timeout_total` — counter. Any non-zero rate pages (2am-actionable).
8. **One `SymbolOnboardingRun` Postgres row per request.** `symbol_states` JSONB column holds per-symbol progress. Updates via `UPDATE ... SET symbol_states = jsonb_set(...)` — NO advisory lock needed for a single-row single-task orchestrator. Advisory locks are reserved for the existing `_upsert_definition_and_alias` path (PR #44).
9. **Truthful status semantics take priority over fan-out** (Contrarian's core point): the Plan must pin the status machine explicitly before any code is written — `pending` / `in_progress` / `completed` / `completed_with_failures` / `failed`, plus per-symbol `not_started` / `registering` / `backfilling` / `qualifying_live` / `ok` / `already_covered` / `failed`. Each transition is a writable row update, not a derived quantity.

### Known Limitations

- **Single worker pool for arq.** Large onboarding jobs will contend with backtest jobs. Mitigation: one-task orchestrator short-circuits serialization at the queue level; council ruled this acceptable at current universe sizes (<50 symbols). Revisit if measured contention appears.
- **Serial per-symbol wall-clock:** ~5 min for a 20-symbol 1-year equity batch, ~15 min for a 50-symbol 5-year batch. Accepted as coffee-break latency for an operator-initiated batch job; not on Pablo's iteration hot path (strategy edit → backtest). Measured step-level timing data deferred to a first-run instrumentation pass (council-flagged "missing evidence").
- **Databento pricing is per-query**, not per-GB; `metadata.get_cost` is best-effort, not a guaranteed quote. `estimate_confidence` reflects this (declared classification: `high` when `end < today-1d` and no ambiguous/continuous symbols, `medium` otherwise).
- **No multi-node scale.** Entire feature is single-VM, single Docker-Compose deployment.
- **Lazy Nautilus catalog rebuild** means the FIRST backtest after a fresh onboarding pays a ~30s rebuild latency.
- **IB Gateway is behind the `broker` compose profile.** `request_live_qualification=true` requires operator to have IB Gateway running; not guaranteed in dev.
- **tenacity retry inside `max_concurrent=3`** serializes under real rate-limit. Observed in PR #44. No circuit-breaker in v1.

### Dependencies

- **Requires:** PR #44 shipped (✓). `/instruments/bootstrap` + Pydantic schemas + Databento client with tenacity retry + `compute_advisory_lock_key`.
- **Requires:** Existing `/market-data/ingest` arq worker (✓).
- **Requires:** `msai instruments refresh --provider interactive_brokers` CLI (✓, from PR #35).
- **Requires:** `run_auto_heal` orchestrator (✓, from PR #40) — stays as the failure-initiated backfill path, does NOT overlap with repair.
- **Requires:** Databento `Historical.metadata.get_cost()` to return stable-enough estimates for a v1 shipping decision — flagged as a research spike in Phase 2.

### Integration Points

- **Databento (external):** `Historical.timeseries.get_range` for bars, `Historical.metadata.get_cost` for estimates, `Historical.metadata.list_datasets` for provider enumeration.
- **Interactive Brokers Gateway (external, opt-in):** `instruments refresh` call when `request_live_qualification=true`.
- **Postgres (internal):** `instrument_definitions` + `instrument_aliases` (from PR #44 registry) + possibly a new `onboarding_jobs` table (Plan decides — might reuse existing arq job records).
- **Parquet on disk (internal):** `{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet` — written by `/market-data/ingest`, read by coverage check.
- **arq / Redis (internal):** job queue.
- **FastAPI (internal):** new endpoints under `/api/v1/symbols/`.
- **CLI (internal):** new `msai symbols` sub-app in Typer.

## 6. Data Requirements

### New Data Models

- **`SymbolOnboardingRun`** (Postgres table OR arq job record — Plan chooses):
  - `job_id: UUID`
  - `watchlist_name: str`
  - `status: JobStatus ENUM` (`pending / in_progress / completed / completed_with_failures / failed`)
  - `requested_at: datetime`, `started_at`, `completed_at`
  - `symbol_states: jsonb` (list of `{symbol, step, status, error, next_action}`)
  - `estimated_cost_usd: float`, `actual_cost_usd: float | null`, `cost_ceiling_usd: float | null`
  - `request_user: str` (from auth)

- **`CoverageSnapshot`** (derived view or table — Plan decides):
  - `(instrument_uid, provider) → covered_ranges: list[DateRange]`
  - Backs the window-scoped readiness endpoint for US-006.
  - Could be computed on-the-fly from Parquet directory listing, or cached in a table — decision in Phase 2 research + Plan.

- **Watchlist YAML schema** (filesystem, NOT a DB model):
  - `name: str`
  - `symbols: list[SymbolEntry]`
  - `request_live_qualification: bool = false`

### Data Validation Rules

- `name` — kebab-case, matches filename stem, unique across `watchlists/*.yaml`.
- `symbol` — must match the existing regex from PR #44: `^[A-Za-z0-9._/-]+$`, length 1–32.
- `asset_class` — `Literal["equity", "futures", "fx", "option"]`.
- `start`, `end` — ISO-8601 dates; `start <= today`; `end >= start`.
- `cost_ceiling_usd` — `Field(ge=0.0, le=10_000.00)`; default = `MSAI_MAX_INGEST_USD` env var or $25.
- `watchlist_name` — matches a `.yaml` file in `watchlists/`; or inline request body (for API-driven use without a file).

### Data Migration

- **`asset_universe.resolution`** column: rename (`legacy_resolution`) OR drop (if no callers). Alembic migration required. Plan decides based on a grep for actual usage.
- No backfill needed for `SymbolOnboardingRun` table — it's write-only on new runs.
- Any existing `asset_universe` rows stay as-is; read path via `/api/v1/universe/` GET continues to work.

## 7. Security Considerations

- **Authentication:** same as PR #44 — `X-API-Key: MSAI_API_KEY` header (dev/CLI) OR Azure Entra ID Bearer JWT. All endpoints under `/api/v1/symbols/` require auth; reading `/api/v1/symbols/coverage` OK to be less strict if internal-only.
- **Authorization:** single-user; no per-user universe scoping.
- **Data Protection:** no sensitive data. Manifest files are plain YAML committed to git — no secrets inside.
- **Audit:** every `POST /api/v1/symbols/onboard` logs `user_sub`, `watchlist_name`, `estimated_cost_usd`, `cost_ceiling_usd`, `symbols_count` at INFO. Every `COST_CEILING_EXCEEDED` logs at WARNING. Every `ENDPOINT_DEPRECATED` 410 logs at INFO with caller info for migration tracking.
- **Rate limiting:** not in scope v1 (single-user). Databento's own rate-limit + tenacity retry handles upstream.
- **Input validation:** Pydantic V2 with `model_validator(mode="after")` for cross-field invariants (e.g. `end >= start`, `exact_ids ⊂ symbols`, `cost_ceiling_usd > 0`).
- **Path safety:** watchlist manifest path MUST be validated to live inside `watchlists/` (prevent `../etc/passwd`). Reuse PR #44's `Path.is_relative_to` pattern.

## 8. Open Questions

- [ ] **Databento `metadata.get_cost` accuracy** — how close is the estimate to actual? Research spike in Phase 2.
- [x] ~~**`/api/v1/universe` live callers**~~ — **RESOLVED 2026-04-24 via repo grep.** Zero production callers (frontend, cli.py, services, workers all clean). The `AssetUniverseService` Python class is used directly by `workers/nightly_ingest.py`, but no HTTP caller exists. Decision: delete the HTTP router outright (Option A), no 410-Gone ceremony — see US-010.
- [ ] **`asset_universe.resolution` usage** — Phase 3 Plan greps for reads. If only nightly ingest reads it, either rename (`legacy_resolution`) or leave with a docstring clarifying it's storage-side (NOT user-intent bar-size per pin #1).
- [ ] **`SymbolOnboardingRun` persistence** — new Postgres table vs. reuse arq's job records vs. materialized-view pattern. Plan Phase 3 decides based on queryability requirements.
- [ ] **`CoverageSnapshot` computation** — on-the-fly from Parquet directory (slow for 2000 symbols) vs. cached table (consistency issues). Plan decides; Scalability Hawk flagged 2000-symbol case.
- [ ] **Prometheus metrics set** — narrowed from Scalability Hawk's 3 to council-suggested 2: `msai_onboarding_cost_usd_total{watchlist,provider}` (counter) + `msai_onboarding_symbols_total{status}` (counter). Plan may add a duration histogram if trivial.
- [x] ~~**Deprecation grace period for `/api/v1/universe` POST/DELETE**~~ — **RESOLVED:** no grace period. Zero callers means immediate deletion, not deprecation. See US-010.
- [ ] **CLI sugar for `trailing_5y`** — how is it expanded? `end = today`, `start = today - 5y`? What about holiday adjustments? Plan decides.

## 9. References

- **Discussion Log:** `docs/prds/symbol-onboarding-discussion.md` (includes full 5-advisor council + Codex chairman verdict).
- **Predecessor PRD:** `docs/prds/databento-registry-bootstrap.md` (PR #44 shipped the registration plumbing).
- **Related PRs:**
  - PR #32 — DB-backed instrument registry schema.
  - PR #35 — `msai instruments refresh --provider interactive_brokers` CLI.
  - PR #37 — Live-path wiring onto registry.
  - PR #40 — Backtest auto-ingest on missing data (the existing failure-initiated backfill).
  - PR #44 — Databento registry bootstrap (just shipped, 2026-04-24).
- **Council decision doc** (symbol-onboarding): TBD — will create during Phase 3 if the verdict is referenced by the plan.
- **Competitor reference:** QuantConnect `ManualUniverseSelectionModel` (code-declarative, NOT our model — MSAI owns the data catalog + pays per-query, so onboarding is genuinely novel UX). See discussion log.

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                               |
| ------- | ---------- | -------------- | ----------------------------------------------------- |
| 1.0     | 2026-04-24 | Claude + Pablo | Initial PRD. Council verdict pinned. 10 user stories. |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design (`/superpowers:brainstorming`)
