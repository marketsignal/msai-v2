# PRD: Backtest Auto-Ingest on Missing Data

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-21
**Last Updated:** 2026-04-21

---

## 1. Overview

When a backtest fails because required historical market data is missing from the Parquet catalog, the platform transparently downloads the missing data (bounded to the backtest's requested symbols + date range, max 10 years) and re-runs the backtest — without surfacing an error to the caller. Today the user sees a failed backtest with a `FailureCode.MISSING_DATA` envelope + a manual `msai ingest` remediation command (shipped in PR #39). This PR closes the self-heal gap demanded by Pablo's "AI-first, self-sufficient platform" directive: agents submit backtests via API/CLI and expect them to succeed, not to surface errors that a human has to fix. The error surface only triggers when auto-heal itself fails (provider returns no data, guardrails tripped, or 30-minute wall-clock cap exceeded).

## 2. Goals & Success Metrics

### Goals

- **Primary:** Eliminate the "backtest fails with missing-data error" experience as a first-class user state; replace it with transparent auto-heal that a caller polling `/backtests/{id}/status` sees as `running` + `phase=awaiting_data` + `progress_message`.
- **Secondary:** Close the `asset_class` scope-defer from PR #39 — derive server-side from canonical instrument ID so auto-heal routes futures to Databento and stocks to Polygon correctly without UI input.
- **Tertiary:** Make the self-heal pipeline safe enough to run unattended — cost guardrails, dedupe, queue isolation, and coverage verification so an agent-driven workflow can't accidentally generate a provider bill incident.

### Success Metrics

| Metric                                                                    | Target                                                                                                                                                                                                             | How Measured                                                                 |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| Backtest success rate on cold-symbol requests (in-guardrail)              | ≥ 95% succeed after auto-heal within 30 minutes                                                                                                                                                                    | Manual E2E drill + post-merge observation over first 10 cold-symbol requests |
| Reduction in user-visible `FailureCode.MISSING_DATA` on in-scope requests | ≥ 90% reduction vs PR #39 baseline                                                                                                                                                                                 | Compare `backtests` rows `error_code="missing_data"` count before/after      |
| Duplicate provider downloads on concurrent requests                       | 0                                                                                                                                                                                                                  | Redis lock telemetry + structured heal logs (`heal_dedupe_short_circuited`)  |
| Queue starvation events (backtest blocked > 5min by ingest in same queue) | 0 (separate ingest queue eliminates the shared-slot class of outage)                                                                                                                                               | arq worker logs + heartbeat monitor                                          |
| Auto-heal cost per cold-symbol request                                    | Bounded by guardrails (10y cap + symbol-fan-out cap + no options expansion); no numerical target set in this PR — will be derived from Phase 2 research's provider billing findings and enforced via configuration | Structured heal logs emit `estimated_cost_usd` field (best-effort)           |
| `asset_class` routing correctness                                         | 100% — every auto-heal invocation routes to the correct provider (Databento for futures, Polygon for stocks)                                                                                                       | Unit test coverage + E2E drill across asset classes                          |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Eager pre-seed of "every symbol × every asset class × 10yr × 1min"** — cost and options-chain explosion make this unsafe; deferred to a later operator-managed curated-universe seed PR.
- ❌ **Streaming progress endpoint (SSE/WebSocket)** — pull-based status polling carries the progress contract in this PR.
- ❌ **Dedicated auto-heal telemetry dashboard** — structured logs + `/status` fields cover observability until operational data shows a dashboard is needed.
- ❌ **Rich cost visibility UI** ("this backtest cost $X.YZ") — structured logs only; follow-up PR if Pablo wants a dashboard.
- ❌ **Partial-range backfill** (ingest only missing months within the requested range) — re-ingest full range if any gap; optimization deferred.
- ❌ **"Retry Backtest" button on FailureCard** — council overruled; deferred until a first-class retry endpoint with explicit attempt semantics exists.
- ❌ **"Force refresh data" button** — operator runs `msai ingest` manually if they want to force refresh.
- ❌ **Auto-heal for non-`MISSING_DATA` FailureCodes** — TIMEOUT/STRATEGY_IMPORT_ERROR/ENGINE_CRASH/UNKNOWN stay manual; only MISSING_DATA is auto-healed.
- ❌ **Auto-expanding options chains** — auto-heal hard-rejects options-chain fan-out requests; operator must explicitly scope option strikes via manual CLI.
- ❌ **N-with-backoff retry loops** — one auto-heal cycle per backtest, hard 30-minute cap.
- ❌ **Changing existing `FailureCode` enum members** — this PR only flips `Remediation.auto_available: False → True` for `kind="ingest_data"` and wires the healing pipeline.

## 3. User Personas

### Platform Agent (primary — per AI-first directive)

- **Role:** Automated caller (Claude agent, future LLM-driven strategy researcher, or scripted CI). Submits backtests via `POST /api/v1/backtests/run`, polls `GET /api/v1/backtests/{id}/status` for completion.
- **Permissions:** Same as any authenticated user (Azure Entra JWT or `X-API-Key` header).
- **Goals:** Run backtests end-to-end without needing human intervention when the only missing piece is data. See stable `status=running` + `phase=awaiting_data` + human-readable `progress_message` during heal. See `status=completed` + metrics on success, or `status=failed` + structured `ErrorEnvelope` on guardrail/timeout failure.

### Human Operator (Pablo)

- **Role:** Monitors the platform via the Next.js UI at `/backtests` and `/backtests/{id}`. Occasionally intervenes when auto-heal fails (e.g., requests a wider guardrail, manually runs `msai ingest` for out-of-scope ranges).
- **Permissions:** Full access.
- **Goals:** At a glance know whether a long-running backtest is slow because of strategy execution or because of data fetch (subtle "Downloading data…" indicator). Trust that the platform isn't silently burning provider credits (structured logs + guardrail enforcement). Be able to diagnose post-facto why an auto-heal failed (ErrorEnvelope + `msai ingest ...` remediation command).

### Cost Stakeholder (Pablo wearing operator hat)

- **Role:** Sets and enforces provider-spend policy.
- **Permissions:** Can tune `settings.auto_heal_*` configuration (caps, ceilings, options policy) via env vars at deploy time.
- **Goals:** Never surprised by a provider bill. Accept bounded on-demand cost for real backtests; reject accidental unbounded requests.

## 4. User Stories

### US-001: Transparent auto-heal for a cold symbol (happy path)

**As a** platform agent
**I want** the platform to auto-download missing historical data when my backtest needs it
**So that** I don't have to handle "missing data" errors — my backtest either completes successfully or fails for a real reason (strategy bug, guardrail trip).

**Scenario:**

```gherkin
Given I have submitted a backtest for AAPL on 2022-01-01 to 2024-12-31
And the Parquet catalog has no bars for AAPL
When the backtest worker's ensure_catalog_data() raises FileNotFoundError
Then the worker classifies the failure as MISSING_DATA
And auto-heal enqueues a run_ingest job on the dedicated ingest queue for AAPL/stocks/2022-01-01..2024-12-31
And the backtest row transitions to status=running with phase=awaiting_data and progress_message="Fetching AAPL from Polygon..."
And after ingest completes, ensure_catalog_data() re-validates per-symbol coverage
And the backtest subprocess runs with the now-present data
And the backtest eventually transitions to status=completed with real metrics
```

**Acceptance Criteria:**

- [ ] `/backtests/{id}/status` returns `status=running` + `phase=awaiting_data` + non-empty `progress_message` while the heal is in flight.
- [ ] `/backtests/{id}/status` returns `status=completed` + metrics + `phase` absent after successful heal + re-run.
- [ ] The ingest is routed by `asset_class` derived server-side from the canonical instrument ID (no UI dropdown required, no default-to-`stocks` fallback).
- [ ] Structured log event `backtest_auto_heal_started` fires once per auto-heal invocation with fields `{backtest_id, symbols, asset_class, start, end, ingest_job_id}`.
- [ ] Structured log event `backtest_auto_heal_completed` fires once per successful heal with fields `{backtest_id, ingest_duration_seconds, bars_ingested_total}`.
- [ ] Zero change in envelope shape for non-MISSING_DATA failures (backward-compatible with PR #39 `ErrorEnvelope`).

**Edge Cases:**

| Condition                                                              | Expected Behavior                                                                                                        |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Parquet exists but per-symbol coverage is partial (gap in middle)      | Coverage verification fails → treat as MISSING_DATA → full re-ingest of requested range                                  |
| Backtest catalog-builder finds partial coverage after ingest completes | Surface `FailureCode.MISSING_DATA` with remediation pointing to manual `msai ingest` + the specific missing date window  |
| Symbol exists at provider but for narrower range than requested        | Ingest succeeds with whatever provider has; coverage re-check fails → backtest fails with specific missing-range message |

**Priority:** Must Have

---

### US-002: Visible progress indicator during auto-heal (UI)

**As a** human operator monitoring the `/backtests/{id}` page
**I want** a subtle "Downloading data…" indicator when the backtest is blocked on auto-heal
**So that** I know why the backtest is taking longer than usual without having to read logs.

**Scenario:**

```gherkin
Given I am viewing /backtests/{id} in the UI for a backtest currently in auto-heal
When the page polls /backtests/{id}/status every 3 seconds
Then the status shows "Running" with a secondary "Downloading AAPL data…" indicator
And the progress_message updates as new polls arrive
When the heal completes
Then the indicator disappears and the normal running spinner continues
When the backtest completes
Then the completed-state UI renders as today
```

**Acceptance Criteria:**

- [ ] `/backtests/{id}` detail page renders a secondary indicator when `status.phase === "awaiting_data"`.
- [ ] Indicator shows `status.progress_message` verbatim (sanitized — no DATA_ROOT paths or secrets, same sanitizer as PR #39 envelope).
- [ ] `/backtests` list page shows a compact "Fetching data…" badge alongside the existing "Running" badge for rows whose status has `phase=awaiting_data`.
- [ ] No new page, no new route, no modal — subtle additive UI only.
- [ ] `data-testid` attributes added: `backtest-phase-indicator`, `backtest-phase-message` for E2E selectors.
- [ ] UI continues to work if the backend returns `phase` absent (graceful degradation — current running spinner).

**Edge Cases:**

| Condition                                            | Expected Behavior                                            |
| ---------------------------------------------------- | ------------------------------------------------------------ |
| `progress_message` is null but `phase=awaiting_data` | Render generic "Downloading data…" fallback text             |
| User navigates away and returns during heal          | Indicator state re-hydrates from `/status` response on mount |
| `phase` field missing entirely (older server or bug) | UI hides indicator, falls back to current running behavior   |

**Priority:** Must Have

---

### US-003: Workload guardrails reject out-of-scope auto-heal attempts

**As** Pablo (cost stakeholder)
**I want** auto-heal to hard-reject requests outside safe bounds (>10y date range, >N symbols, options chains)
**So that** a malformed or agent-generated backtest can't accidentally generate a large provider bill.

**Scenario:**

```gherkin
Given I submit a backtest for AAPL options chain spanning 2015-01-01 to 2025-12-31 (11 years)
When the backtest worker's ensure_catalog_data() raises FileNotFoundError
And the classifier produces a Remediation envelope
And auto-heal evaluates guardrails
Then guardrail check fails because (a) range > 10 years AND (b) asset_class == "options" (options-fan-out disallowed)
And auto-heal is NOT invoked
And the backtest fails with status=failed + FailureCode.MISSING_DATA + ErrorEnvelope.suggested_action pointing to the explicit manual CLI
And ErrorEnvelope.remediation.auto_available = false (opted out because of guardrail)
And the FailureCard UI shows the same copy-to-clipboard remediation command as PR #39
```

**Acceptance Criteria:**

- [ ] Configurable guardrail settings exposed as env vars: `AUTO_HEAL_MAX_YEARS` (default 10), `AUTO_HEAL_MAX_SYMBOLS` (default TBD from Phase 2 research — placeholder 20), `AUTO_HEAL_ALLOW_OPTIONS` (default `false`).
- [ ] Guardrail evaluation runs before `enqueue_ingest` is called.
- [ ] Guardrail rejection is logged as structured event `backtest_auto_heal_guardrail_rejected` with fields `{backtest_id, reason, requested_range_years, requested_symbol_count, requested_asset_class}`.
- [ ] Guardrail rejection sets `ErrorEnvelope.remediation.auto_available = false` so the UI FailureCard + CLI remediation stay identical to PR #39 behavior.
- [ ] `ErrorEnvelope.message` includes which guardrail tripped (e.g., "Auto-download disabled — 11-year range exceeds 10-year cap. Run: msai ingest stocks AAPL 2015-01-01 2025-12-31").
- [ ] Unit tests cover each guardrail in isolation (range, symbol count, options).

**Edge Cases:**

| Condition                                                                                  | Expected Behavior                                                |
| ------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| Range is exactly 10 years (edge)                                                           | Allowed (cap is inclusive)                                       |
| Symbol count is exactly the cap                                                            | Allowed                                                          |
| Request is for futures (not options) but the instrument happens to be an option underlying | Allowed if `asset_class == "futures"` (derived server-side)      |
| Multi-asset-class request (half stocks, half options)                                      | Rejected wholesale — mixed asset-class auto-heal is not in scope |

**Priority:** Must Have

---

### US-004: Concurrent cold-symbol requests dedupe

**As a** platform agent issuing parallel backtest submissions
**I want** concurrent requests for the same missing symbol/range to share a single ingest job
**So that** my parallel workflow doesn't double-spend provider credits.

**Scenario:**

```gherkin
Given I submit two backtests within 5 seconds, both requiring AAPL 2022-01-01..2024-12-31
And neither's data is in the catalog
When both worker instances call ensure_catalog_data() and raise FileNotFoundError
And both attempt auto-heal
Then the second auto-heal finds an existing Redis lock for (AAPL, stocks, 2022-01-01, 2024-12-31)
And the second auto-heal does NOT enqueue a duplicate ingest job
And the second auto-heal polls the lock status + waits for the first job to complete
And both backtests proceed once ingest is done (with coverage-verification short-circuit catching the parquet presence)
And only ONE structured log event `backtest_auto_heal_ingest_enqueued` fires for the whole cluster
```

**Acceptance Criteria:**

- [ ] Redis lock keyed by `(asset_class, sorted(symbols), start, end)` with TTL ≥ auto-heal wall-clock cap + buffer (e.g., 45 minutes).
- [ ] Lock acquire uses `SET NX EX` semantics; non-acquirer transitions to "wait for existing lock" path.
- [ ] Service-layer parquet-coverage short-circuit runs before ingest enqueue (belt-and-suspenders in case Redis lock is missing).
- [ ] Watchdog (`services/job_watchdog.py`) extends to clear stale ingest locks (holder died mid-ingest) — timeout = TTL + 1 minute.
- [ ] Unit tests cover: (a) single-holder happy path, (b) two-concurrent same-key → second waits, (c) stale-lock cleanup by watchdog.

**Edge Cases:**

| Condition                                                                                                     | Expected Behavior                                                                               |
| ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Two concurrent requests with different symbol lists but overlapping symbols (e.g. `[AAPL]` vs `[AAPL, MSFT]`) | Different lock keys → two separate ingests. Accept this cost; not worth the complexity in v1.   |
| Holder worker crashes during ingest                                                                           | Lock TTL expires → watchdog clears → next request acquires fresh lock + re-enqueues ingest      |
| Redis unavailable at lock-acquire time                                                                        | Log warning + proceed with service-layer short-circuit only (degraded dedupe, still functional) |

**Priority:** Must Have

---

### US-005: Server-derived asset_class (closes PR #39 scope-defer)

**As a** platform agent
**I want** the platform to derive `asset_class` from my submitted canonical instrument ID
**So that** auto-heal routes futures symbols to Databento and stocks to Polygon correctly without me having to specify it.

**Scenario:**

```gherkin
Given I submit a backtest with instrument ID "ES.n.0" (a futures contract)
When the worker fails with MISSING_DATA
And the classifier computes the Remediation envelope
Then asset_class is derived from the canonical instrument ID via SecurityMaster lookup → "futures"
And auto-heal enqueue_ingest(asset_class="futures", symbols=["ES.n.0"], ...) routes to Databento
And ErrorEnvelope.suggested_action correctly reads "Run: msai ingest futures ES.n.0 ..." not the PR-#39 buggy "stocks ES.n.0 ..." fallback
```

**Acceptance Criteria:**

- [ ] `Remediation.asset_class` is populated server-side via the existing `SecurityMaster` / `InstrumentRegistry` lookup in the classifier path.
- [ ] Fallback: if registry lookup fails (unknown instrument), the classifier uses a deterministic shape-based heuristic (e.g., `.n.0` suffix → futures, `.NASDAQ/.ARCA/.NYSE` → stocks) before falling through to the worker-supplied `asset_class` kwarg.
- [ ] PR #39's known "UI defaults to `stocks`" bug is closed — the UI's Run Backtest form no longer needs `asset_class` input; backend is the single source of truth.
- [ ] CLI: `msai ingest` positional arg still accepts `stocks|futures|options` explicitly (operator control preserved); this PR only changes the auto-derivation path.
- [ ] Unit tests parameterize across `(AAPL.NASDAQ, stocks)`, `(ES.n.0, futures)`, `(SPY.ARCA, stocks)`, `(EUR/USD.IDEALPRO, forex)`, `(unknown_symbol, fallback-heuristic)`.

**Edge Cases:**

| Condition                                         | Expected Behavior                                                                                                                           |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Instrument ID is not in the registry              | Fall through to shape-based heuristic; if that also fails, `asset_class = "stocks"` default + log `asset_class_derivation_fallback` warning |
| Instrument ID is ambiguous across registries      | Honor `SecurityMaster.resolve_for_backtest(start=)` windowing (already in place); pick the one effective at `start_date`                    |
| UI callers explicitly pass `asset_class` (legacy) | Server-side derivation wins; UI-supplied value is logged but ignored                                                                        |

**Priority:** Must Have

---

### US-006: 30-minute wall-clock cap with clean failure

**As a** platform agent
**I want** auto-heal to fail cleanly if it takes longer than 30 minutes
**So that** a wedged provider or a massive ingest doesn't hold my backtest workflow forever.

**Scenario:**

```gherkin
Given an auto-heal starts at T=0 for a backtest
And the ingest job is still running at T=30 minutes
When the auto-heal poll loop checks wall-clock elapsed
Then auto-heal transitions the backtest to status=failed
And FailureCode.MISSING_DATA is surfaced with ErrorEnvelope.message "Data download timed out after 30 minutes"
And ErrorEnvelope.suggested_action suggests manual retry with `msai backtest run ...`
And the in-flight ingest job is NOT cancelled — it continues running in the background
And when the ingest eventually completes, the data IS in the catalog for future backtests (cache benefit preserved)
And no structured error is logged for the ingest itself — only for the backtest's timed-out auto-heal cycle
```

**Acceptance Criteria:**

- [ ] Wall-clock cap is configurable via `AUTO_HEAL_WALL_CLOCK_CAP_SECONDS` env (default 1800).
- [ ] One auto-heal cycle per backtest — no recursive retry after cap-hit.
- [ ] Cap enforcement: auto-heal polls ingest status at a reasonable interval (e.g., 10s); if wall-clock exceeds cap, exit the poll loop and mark backtest failed.
- [ ] `ErrorEnvelope.code = "missing_data"` (not a new code like `ingest_timeout` — keeps envelope schema stable with PR #39).
- [ ] `ErrorEnvelope.message` differentiates timeout from "provider has no data" via prefix.
- [ ] The ingest job's `run_ingest` continues independently (arq doesn't cancel in-flight work when a different row moves to failed); the ingested data IS available for the next backtest.
- [ ] Structured log `backtest_auto_heal_timeout` fires once with fields `{backtest_id, wall_clock_seconds, ingest_job_id_still_running}`.

**Edge Cases:**

| Condition                                                   | Expected Behavior                                                                                     |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Ingest completes at T=30:01 (just after cap)                | Backtest already marked failed; data is in catalog; next re-submission of same backtest will succeed  |
| Ingest fails before cap                                     | Auto-heal catches failure, marks backtest failed with provider-specific `ErrorEnvelope.message`       |
| User re-submits backtest at T=15min while heal is in flight | Second submission hits Redis lock dedupe path from US-004; second backtest's auto-heal joins the wait |

**Priority:** Must Have

---

### US-007: Structured logs for auto-heal audit trail

**As** Pablo (cost stakeholder)
**I want** every auto-heal invocation to emit structured log events with cost-relevant fields
**So that** I can grep/aggregate post-facto to answer "how much did I spend on auto-heal this week?" without building a dashboard.

**Scenario:**

```gherkin
Given an auto-heal runs end-to-end
When the heal cycle starts, ingest completes, and backtest re-runs
Then structured logs emit in order:
  1. backtest_auto_heal_started {backtest_id, symbols, asset_class, start, end, estimated_bars}
  2. backtest_auto_heal_ingest_enqueued {backtest_id, ingest_job_id, dedupe_result}
  3. backtest_auto_heal_ingest_completed {backtest_id, ingest_duration_seconds, bars_ingested_total, provider}
  4. backtest_auto_heal_completed {backtest_id, total_wall_clock_seconds, success=true}
And in failure paths:
  - backtest_auto_heal_guardrail_rejected {reason, requested_*}
  - backtest_auto_heal_timeout {backtest_id, wall_clock_seconds}
  - backtest_auto_heal_ingest_failed {backtest_id, error, provider}
```

**Acceptance Criteria:**

- [ ] All events use `structlog` (project's existing logger).
- [ ] No sensitive data (credentials, secrets, full DATA_ROOT paths) in log fields.
- [ ] Events are JSON-parsable from the container stdout.
- [ ] Event names are stable — treat them as a log-grep contract.
- [ ] The `estimated_bars` / `bars_ingested_total` fields are best-effort; null is acceptable if provider doesn't expose it.

**Edge Cases:**

| Condition                                            | Expected Behavior                                               |
| ---------------------------------------------------- | --------------------------------------------------------------- |
| structlog backend unavailable                        | Fall back to stdlib logging with key=value formatting; no crash |
| Event emit raises                                    | Swallowed — never crash auto-heal on a logging failure          |
| Best-effort field (e.g., estimated_bars) unavailable | Emit `null` or `unknown` for that field, not skip the event     |

**Priority:** Must Have

---

## 5. Technical Constraints

### Known Limitations

- Current `BacktestStatus` column is `String(50)` with in-use values `pending | running | completed | failed`. Auto-heal must NOT introduce a new top-level state (council-overruled — would churn every status filter query + watchdog + every frontend union type). Instead, `phase` + `progress_message` are additive columns on the backtest row surfacing through `BacktestStatusResponse`.
- Current arq worker config at `backend/src/msai/workers/settings.py` runs `run_backtest` + `run_ingest` on a single worker with `max_jobs=2`. This PR MUST split into a separate ingest queue/worker lane — it is a blocking objection accepted by council.
- `ensure_catalog_data()` in `services/nautilus/catalog_builder.py` currently checks file-existence; does not verify per-symbol time coverage. Per-symbol coverage verification MUST ship in this PR to avoid silent data gaps (blocking objection from Scalability Hawk).
- Nautilus gotcha #12: options chain loading explodes (thousands of strikes per underlying × days). Auto-heal hard-rejects `asset_class == "options"` in v1; operator must run manual `msai ingest` with explicit strike scoping.
- `BacktestRunner.run()` wraps subprocess exceptions as `RuntimeError(str(traceback))`. Auto-heal decision-point is BEFORE the subprocess spawns (outer worker path where `FileNotFoundError` is still typed), so no traceback-peeking is needed for the heal trigger — only the existing classifier path from PR #39.

### Dependencies

- **Requires:** PR #39 (Backtest failure surfacing — the ErrorEnvelope contract + `FailureCode.MISSING_DATA` + `Remediation(kind="ingest_data", ..., auto_available=False)`). Already merged to main at `44d6329`.
- **Requires:** `SecurityMaster` / `InstrumentRegistry` for server-side `asset_class` derivation (already in place post-PR #32 / #35).
- **Requires:** `enqueue_ingest()` + `run_ingest()` arq primitives (already in place at `core/queue.py:147` + `workers/settings.py:66`).
- **Blocked by:** None.

### Integration Points

- **Polygon.io** (stocks/options per-API-call billing): already integrated via `services/data_ingestion.run_ingest` auto-routing.
- **Databento** (futures per-symbol per-schema licensing): already integrated via same path.
- **Redis:** Used for arq queue + new auto-heal dedupe lock (`SET NX EX`-based).
- **Postgres:** 4 new columns on `backtests` table (phase, progress_message, heal_started_at, heal_job_id) via Alembic single-step migration (Postgres 16 `attmissingval` fast-path, same pattern as PR #39's 4-column migration).

## 6. Data Requirements

### New Data Models

- **`backtests.phase` column** (`String(32) | null`): current sub-phase within `running` status. Values: `"awaiting_data"` | null. Null means "no auto-heal in progress or heal has finished." Alphabetic enum-style string (no DB-enforced enum; FastAPI-side validation via Pydantic `Literal`).
- **`backtests.progress_message` column** (`Text | null`): human-readable sanitized message for UI display. Populated during heal (`"Fetching AAPL 2022-01-01..2024-12-31 from Polygon..."`), cleared on heal completion or terminal state.
- **`backtests.heal_started_at` column** (`Timestamp | null`): wall-clock start of the heal cycle — used for the 30-minute cap enforcement.
- **`backtests.heal_job_id` column** (`String(64) | null`): arq job id of the ingest job this backtest's heal is waiting on — used for dedupe + debugging.

No new tables.

### Data Validation Rules

- `phase` must be `"awaiting_data"` or null. Any other value is a bug; API response serialization includes a `Literal["awaiting_data"] | None` type.
- `progress_message` must pass the PR #39 `sanitize_public_message` pipeline before hitting the API response or DB write.
- `heal_started_at` is set atomically alongside `phase` transition to `"awaiting_data"`; both cleared together on terminal transition.
- `heal_job_id` references an arq job id, not enforced by FK (arq doesn't have a Postgres-backed job table).

### Data Migration

- Single-step Alembic migration adds 4 nullable columns (no `NOT NULL DEFAULT` required since null is the meaningful default). Same Postgres 16 fast-path as PR #39's migration.
- Backward compatibility: historical rows get `phase=null` + `progress_message=null` + `heal_started_at=null` + `heal_job_id=null`. No backfill needed.
- Forward compatibility: removing these columns requires a separate alembic downgrade; no data loss beyond heal-cycle metadata.

## 7. Security Considerations

- **Authentication:** Auto-heal is server-side only; no new user-facing endpoint. Existing auth on `POST /backtests/run` + `GET /backtests/{id}/status` covers the surface.
- **Authorization:** Auto-heal inherits the backtest's `created_by` user — a user only auto-heals their own backtests (indirectly, via the worker serving their row). No new permission checks needed.
- **Data Protection:** `progress_message` passes through PR #39's `sanitize_public_message` (strips DATA_ROOT, DSN credentials, JWT tokens, Bearer headers, secret kv-pairs). No full paths, no tokens, no stack traces in progress UI or logs.
- **Audit:** Structured logs at 7 event points (US-007) capture every auto-heal decision. Log fields intentionally avoid raw provider API keys — they are referenced by provider name only (`"polygon"` / `"databento"`).
- **Cost as a security surface:** Guardrails (US-003) are the primary defense against a compromised agent or a malicious backtest request triggering unbounded provider spend. Guardrail settings are env-configured (not DB-stored) so an attacker with DB access cannot relax them; they'd need deploy access.
- **Redis lock TTL:** Lock TTL is bounded (≤ 45 min) + watchdog-cleaned on staleness so a crashed worker cannot permanently block future auto-heal for a symbol.

## 8. Open Questions

> Resolved in Phase 2 research (Missing Evidence from council synthesis):

- [ ] Actual Databento + Polygon billing math for 10y/1min pulls by asset class (options contracts vs underlyings) → determines `AUTO_HEAL_MAX_SYMBOLS` default and whether a per-heal cost-estimate log field is feasible.
- [ ] Wall-clock ingest measurements at current API limits for 1y and 10y ranges across 1/5/20 symbol counts → validates the 30-minute cap default and the symbol-count guardrail.
- [ ] Whether the current `catalog_builder.ensure_catalog_data` logic can validate per-symbol time-range coverage or only file existence → determines whether coverage verification is a ~20-line addition or a ~200-line refactor.
- [ ] Whether the deployed arq topology can cleanly add a second `ingest_queue` / worker pool without extra ops work (docker-compose service, helm chart, etc.).
- [ ] Whether the existing `_nightly_if_due` cron overlaps with what a future curated-universe seed PR would need (reuse path for the follow-up PR).
- [ ] Safe workload thresholds per asset class before latency/cost becomes unacceptable → validates initial guardrail values.

> Resolved in Phase 3 design:

- [ ] Exact dedupe-lock key normalization (are `[AAPL]` and `[AAPL, MSFT]` distinct enough to warrant different ingests? Or should the larger superset ingest satisfy the smaller subset's wait?).
- [ ] How the backtest worker "pauses" while waiting — does it release the arq slot (preferred, re-enqueues self with delay) or block synchronously on the ingest job? The latter is simpler; the former protects the backtest queue from starvation even with the separate ingest queue.
- [ ] Whether `phase` + `progress_message` are updated by the backtest worker itself or by a separate orchestrator — depends on pause strategy above.

## 9. References

- **Discussion Log:** `docs/prds/backtest-auto-ingest-on-missing-data-discussion.md`
- **Council Verdict:** Preserved verbatim in discussion log "Council Verdict" section + full chairman synthesis in session transcript 2026-04-21.
- **Prior PR (foundation):** PR #39 — "Backtest failure surfacing — structured envelope across API/CLI/UI" (merged to main at `44d6329`, 2026-04-21). Source of `FailureCode.MISSING_DATA`, `Remediation(auto_available=False)` contract, `<FailureCard>` component.
- **Related PR (prior):** PR #32 + PR #35 — instrument registry + `SecurityMaster` lookup (source of truth for server-side `asset_class` derivation).
- **Related file — classifier:** `backend/src/msai/services/backtests/classifier.py` (produces `FailureClassification`).
- **Related file — ingest queue primitive:** `backend/src/msai/core/queue.py:147` (`enqueue_ingest`).
- **Related file — ingest worker:** `backend/src/msai/workers/settings.py:66` (`run_ingest`).
- **Related file — catalog builder (raises FileNotFoundError + re-check point):** `backend/src/msai/services/nautilus/catalog_builder.py`.
- **Project philosophy directive (verbatim):** Pablo 2026-04-21 — "AI first, CLI second, UI third. Agents talking to a platform, not too many humans. Platform smart, self-heal, self-sufficient. 10yr/1min default for every asset class."
- **Competitor reference:** NautilusTrader + Zipline + Backtrader all lack first-class auto-ingest-on-miss; this is platform-specific behavior layered on top of Nautilus. No direct competitor pattern to copy.

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                                                                                            |
| ------- | ---------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-04-21 | Claude + Pablo | Initial PRD. Scope locked by 5-advisor council verdict; 7 user stories; 9 non-goals; 6 open questions routed to Phase 2 research + Phase 3 design. |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design (`/superpowers:brainstorming` or directly to `/superpowers:writing-plans` given the council has already validated the approach)
