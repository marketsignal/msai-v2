# PRD: Databento registry bootstrap for equities, ETFs & futures

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo (Codex-advised)
**Created:** 2026-04-23
**Last Updated:** 2026-04-23

---

## 1. Overview

Adds a Databento-backed path for populating the instrument registry so fresh checkouts / wiped databases / cold environments can register equity, ETF, and futures symbols WITHOUT requiring an IB Gateway Docker container, IB login, or IB market-data entitlements. Ships as a new CLI verb (`msai instruments bootstrap`) and HTTP endpoint (`POST /api/v1/instruments/bootstrap`), both wrapping the same write path. The contract is explicit: a Databento-bootstrapped row is **backtest-discoverable only** — live graduation still requires a separate `instruments refresh --provider interactive_brokers` step. Closes the concrete pain surfaced during the PR #40 SPY live demo (raw SQL INSERTs to seed equities) and unblocks the iteration loop "spot ticker → backtest → graduate → live" without an IB dependency at onboarding time.

## 2. Goals & Success Metrics

### Goals

- **Primary:** Remove the IB Gateway dependency for onboarding equity, ETF, and futures symbols for backtest use. A fresh-DB user can `bootstrap AAPL,SPY,ES.n.0` and immediately run a backtest without touching IB.
- **Secondary:** Make the registry write path explicit about readiness — operators see `registered` / `backtest_data_available` / `live_qualified` as distinct states, not a single boolean.
- **Tertiary:** Preserve the PR #37 architectural decision that the registry is an operator-managed control plane (no auto-warming, no catalog-sync cron).

### Success Metrics

| Metric                                                           | Target                                                                                                      | How Measured                                                                                          |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Cold-start onboarding time (fresh DB → first backtest on `AAPL`) | < 2 minutes end-to-end (bootstrap + auto-heal + backtest submission)                                        | E2E acceptance test wall-clock                                                                        |
| Manual SQL INSERTs required to seed equities                     | 0                                                                                                           | Grep `docs/runbooks/` + CONTINUITY for `psql -c INSERT INTO instrument_definitions` post-merge        |
| Batch bootstrap throughput                                       | 10 symbols in < 60 s (at `max_concurrent=3`)                                                                | `msai_registry_bootstrap_duration_seconds` histogram, p95                                             |
| Rate-limit resilience                                            | 0 unrecoverable 429s across typical Pablo batches (≤20 symbols)                                             | `msai_databento_api_calls_total{outcome="rate_limited_recovered"}` vs `outcome="rate_limited_failed"` |
| Venue-divergence detection                                       | 100% of IB/Databento venue mismatches logged + counted                                                      | `msai_registry_venue_divergence_total` fires on every conflict (verified by integration test)         |
| Per-symbol outcome clarity                                       | Every response includes `{registered, backtest_data_available, live_qualified}` — no implicit "ready" state | API schema + CLI JSON output inspection                                                               |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Options support** — deferred to separate PRD (chain loading, strike-band policy, OPRA entitlement). Nautilus gotcha #12 makes this a distinct problem.
- ❌ **Forex via Databento** — Databento's Spot FX is "Coming soon" as of 2026-04-23. FX continues on the existing IB-only path (PR #37 live drill ran EUR/USD via IB). If Databento ships FX, amend in a follow-up.
- ❌ **Cash indexes (SPX, NDX, RUT)** — not directly tradeable; Databento doesn't publish index bars. Use ETF proxies (SPY/QQQ/IWM) or index futures (ES/NQ/RTY) instead.
- ❌ **Recurring catalog-sync cron / auto-warming** — undoes the PR #37 operator-managed registry decision. Drift surfaces via divergence counter; operator runs bootstrap/refresh manually.
- ❌ **Databento replaces IB as canonical** — IB stays the authoritative source for live qualification. Databento is a peer provider for backtest discoverability.
- ❌ **UI for symbol onboarding** — the UI shell lives in a separate "Symbol Onboarding" PRD (item #2 in backlog). This PRD ships the API + CLI primitives the future UI will call.
- ❌ **Per-symbol cost estimation in Prometheus** — requires extra pricing calls + creates false precision. Estimated-request-count is returned in the API response JSON instead.
- ❌ **Bulk seed from file (`--from-file sp500.txt`)** — future v2 if ever useful. v1 is on-demand only.
- ❌ **Multiple bar-timeframe proofs in acceptance demo** — Databento publishes `1s/1m/1h/1d` natively; `5m/10m/30m` aggregate from 1m in the existing pipeline. Acceptance proves 1m full E2E + one aggregated smoke.

## 3. User Personas

### Pablo (single power-user)

- **Role:** Operator + trader + developer of MSAI v2. Sole user today.
- **Permissions:** Full admin — registry write, backtest submit, portfolio deploy, kill-all.
- **Goals:** Iterate fast on strategy research. Spot a ticker → backtest it at the right timeframe → graduate to paper → live. Avoid operational friction (IB Gateway spin-up, raw SQL) that breaks flow.
- **Interfaces (north-star ordering):** API primary (scripts/integrations), CLI secondary (terminal work), UI tertiary (future).

## 4. User Stories

### US-001: Bootstrap a batch of symbols via API

**As** Pablo
**I want** to call `POST /api/v1/instruments/bootstrap` with a list of symbols and provider
**So that** I can register equity/ETF/futures symbols programmatically without running IB Gateway

**Scenario:**

```gherkin
Given a fresh database with no rows in instrument_definitions
When I POST /api/v1/instruments/bootstrap with {"provider": "databento", "symbols": ["AAPL", "SPY", "ES.n.0"]}
Then the response is HTTP 200 (all succeeded) with a results[] array
And each result contains {symbol, canonical_id, outcome, registered, backtest_data_available, live_qualified}
And each symbol now has a row in instrument_definitions + instrument_aliases
And registered=true, backtest_data_available=false, live_qualified=false for each
```

**Acceptance Criteria:**

- [ ] `POST /api/v1/instruments/bootstrap` accepts `{provider: "databento", symbols: string[], asset_class_override?: string, max_concurrent?: int, exact_ids?: {[symbol]: int}}`
- [ ] Response body: `{results: [{symbol, canonical_id, outcome, registered, backtest_data_available, live_qualified, dataset, asset_class, diagnostics?}], summary: {total, created, noop, alias_rotated, failed}}`
- [ ] Returns HTTP 200 when all symbols succeed, HTTP 207 Multi-Status when mixed
- [ ] Requires JWT or `X-API-Key` auth (same as other `/api/v1/instruments/*` endpoints)
- [ ] Registered rows pass the existing `SecurityMaster.resolve_for_backtest` and registry-read path in `/backtests/run`

**Edge Cases:**

| Condition                                  | Expected Behavior                                                  |
| ------------------------------------------ | ------------------------------------------------------------------ |
| Empty `symbols` array                      | HTTP 422 `EMPTY_SYMBOL_LIST`                                       |
| Unknown provider                           | HTTP 422 `UNSUPPORTED_PROVIDER`                                    |
| Missing `DATABENTO_API_KEY` server-side    | HTTP 500 `DATABENTO_NOT_CONFIGURED` with operator-hint             |
| Databento returns 401 (unentitled dataset) | HTTP 502 `DATABENTO_UNAUTHORIZED` with dataset name + upgrade hint |
| Databento API timeout / network error      | HTTP 502 `DATABENTO_UPSTREAM_ERROR` + retry guidance               |
| Partial success (3/5 symbols OK)           | HTTP 207 Multi-Status with per-symbol outcomes                     |

**Priority:** Must Have

---

### US-002: Bootstrap the same batch via CLI

**As** Pablo
**I want** `msai instruments bootstrap --provider databento --symbols AAPL,SPY,ES.n.0`
**So that** I can onboard symbols from a terminal without scripting against the API

**Scenario:**

```gherkin
Given a fresh database and DATABENTO_API_KEY configured
When I run: msai instruments bootstrap --provider databento --symbols AAPL,SPY
Then the CLI prints per-symbol status lines to stderr ("AAPL -> registered (XNAS.ITCH)", "SPY -> registered (ARCX.PILLAR)")
And the CLI prints a structured JSON result block to stdout
And the exit code is 0
And instrument_definitions has new rows for AAPL and SPY
```

**Acceptance Criteria:**

- [ ] `msai instruments bootstrap` subcommand exists (new verb, NOT a flag on `refresh`)
- [ ] Required flags: `--provider {databento}`, `--symbols SYMBOLS` (comma-separated)
- [ ] Optional flags: `--asset-class equity|etf|future`, `--max-concurrent INT` (default 3, capped 3 in v1), `--exact-id SYMBOL:ID` (repeatable)
- [ ] CLI is a thin wrapper that calls the API via the internal HTTP client (same `_api_call` pattern as existing CLI)
- [ ] Stderr: human-readable summary (one line per symbol + final total)
- [ ] Stdout: structured JSON via `_emit_json` (mirrors API response shape)
- [ ] Exit code 0 when all symbols succeed; non-zero if any symbol failed (partial-success still prints full result block)
- [ ] `--help` text explicitly states: "Registers symbols as backtest-discoverable. Does NOT qualify live IB instruments — run `msai instruments refresh --provider interactive_brokers` before live deployment."

**Edge Cases:**

| Condition                     | Expected Behavior                                          |
| ----------------------------- | ---------------------------------------------------------- |
| Symbol list empty after trim  | Exit code 2, stderr "no symbols provided"                  |
| `--asset-class` value invalid | Typer validation error, exit code 2                        |
| `--max-concurrent > 3`        | Typer validation error: "max_concurrent capped at 3 in v1" |
| API returns HTTP 207          | Print all outcomes, exit code 1                            |
| API returns 5xx               | Print error envelope, exit code 1                          |

**Priority:** Must Have

---

### US-003: Immediate backtest after bootstrap

**As** Pablo
**I want** to run a backtest on a symbol immediately after bootstrapping it
**So that** my iteration loop is unblocked without manual intervention between steps

**Scenario:**

```gherkin
Given I just bootstrapped AAPL and registered=true, backtest_data_available=false
When I POST /api/v1/backtests/run with {strategy: "ema_cross", symbols: ["AAPL"], bar_spec: "1-MINUTE", start: "2024-01-02", end: "2024-01-10"}
Then the request does NOT return HTTP 422 UNKNOWN_SYMBOL
And the backtest job is enqueued
And auto-heal (PR #40) downloads missing bars for AAPL 2024-01-02..2024-01-10
And the backtest completes with metrics
And a subsequent GET /api/v1/instruments/AAPL would return backtest_data_available=true (future work)
```

**Acceptance Criteria:**

- [ ] After `bootstrap AAPL`, an immediate `/backtests/run` for AAPL does not fail on registry miss
- [ ] Auto-heal (existing PR #40 pipeline) handles the bar-ingest step without code changes in this PR
- [ ] End-to-end timing: bootstrap (≤10 s) + backtest submission (immediate) + auto-heal data download (variable) + backtest compute (variable) — no code-path delay introduced by the bootstrap layer

**Edge Cases:**

| Condition                                                              | Expected Behavior                                                             |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Auto-heal fails (Databento historical bars unentitled for that window) | Backtest fails with existing `MISSING_DATA` envelope; bootstrap is unaffected |
| Symbol bootstrapped but wrong asset_class tag                          | Strategy may reject at config validation — existing path, not this PR         |

**Priority:** Must Have

---

### US-004: Ambiguous symbol handling

**As** Pablo
**I want** a 422 response with candidate list when a symbol matches multiple Databento instruments
**So that** I never silently register the wrong contract

**Scenario:**

```gherkin
Given Databento has multiple matching instruments for "BRK.B" on XNAS.ITCH
When I POST /api/v1/instruments/bootstrap with {"provider": "databento", "symbols": ["BRK.B"]}
Then the response is HTTP 422 with code AMBIGUOUS_BOOTSTRAP_SYMBOL
And the response body includes candidates: [{raw_symbol, listing_venue, dataset, security_type, databento_instrument_id, description}]
And no row is written for BRK.B in instrument_definitions
And I can retry with exact_ids: {"BRK.B": 12345} to select the intended candidate
```

**Acceptance Criteria:**

- [ ] Multi-match detection fires when `fetch_definition_instruments` returns >1 candidate for a single request
- [ ] HTTP 422 envelope: `{error: {code: "AMBIGUOUS_BOOTSTRAP_SYMBOL", message: "...", details: {candidates: [...]}}}`
- [ ] CLI prints the candidates list to stderr + full JSON to stdout + retry command example
- [ ] `exact_ids` / `--exact-id` selects the specified candidate; if the ID doesn't match any candidate, fails with `UNKNOWN_EXACT_ID`
- [ ] No partial writes on ambiguity — registry state is unchanged after a 422

**Edge Cases:**

| Condition                                                           | Expected Behavior                                                           |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| All candidates have same `databento_instrument_id` (duplicate rows) | Dedupe by `instrument_id`; if still >1 distinct, 422                        |
| Candidate list is paginated by Databento                            | Fetch all pages before deciding ambiguous — no guessing on a truncated list |

**Priority:** Must Have

---

### US-005: Idempotent re-run

**As** Pablo
**I want** to re-run `bootstrap SPY` without errors
**So that** my iteration scripts can be rerun safely

**Scenario:**

```gherkin
Given SPY is already bootstrapped with canonical_id "SPY.ARCA" and effective_from=2026-04-23
When I POST /api/v1/instruments/bootstrap with {"provider": "databento", "symbols": ["SPY"]}
And Databento still returns canonical_id "SPY.ARCA"
Then the response outcome is "noop"
And no new rows are written
And registered=true, unchanged
```

**And:**

```gherkin
Given SPY is already bootstrapped with canonical_id "SPY.ARCA"
When I re-bootstrap and Databento NOW returns "SPY.BATS" (venue migration)
Then the response outcome is "alias_rotated"
And the previous alias is closed (effective_to=today)
And a new alias is inserted (effective_from=today, canonical_id="SPY.BATS")
And msai_registry_bootstrap_total{outcome="alias_rotated"} is incremented
```

**Acceptance Criteria:**

- [ ] Outcomes: `created` (first-time), `noop` (exact match), `alias_rotated` (canonical changed)
- [ ] `alias_rotated` path acquires the advisory lock (see US-008) to prevent race with concurrent IB refresh
- [ ] `noop` path touches zero rows (no `updated_at` bump)
- [ ] Response always HTTP 200 (or 207 in batch), never 409
- [ ] Same-calendar-day rotation is supported: when seed and rotation occur on the same UTC date, the closing UPDATE sets `effective_to = today` on the seed row (whose `effective_from` is also `today`), producing a zero-width `[today, today)` audit row. The `ck_instrument_aliases_effective_window` CHECK admits this via `effective_to >= effective_from` (migration `b6c7d8e9f0a1`); the half-open interval contains no dates, so the zero-width row is never selected as the active alias.

**Priority:** Must Have

---

### US-006: Explicit readiness states in response

**As** Pablo
**I want** the API response to show three readiness states per symbol
**So that** the future UI and I can never confuse "bootstrapped" with "live-ready"

**Scenario:**

```gherkin
Given a symbol AAPL just bootstrapped via Databento
When I inspect the bootstrap API response
Then the per-symbol result includes:
  - registered: true (this bootstrap succeeded)
  - backtest_data_available: false (auto-heal hasn't run yet)
  - live_qualified: false (IB hasn't qualified this symbol)
And the CLI prints these three flags in the human-readable stderr output too
```

**Acceptance Criteria:**

- [ ] Response schema includes `registered: bool`, `backtest_data_available: bool`, `live_qualified: bool` per symbol
- [ ] `registered` is set by this bootstrap endpoint
- [ ] `backtest_data_available` is computed by checking the existing Parquet store coverage (cheap lookup; does NOT trigger auto-heal). If unknown/expensive, return `null` and document.
- [ ] `live_qualified` is derived by reading whether an active alias with `provider="interactive_brokers"` exists for the same symbol (existing alias data)
- [ ] CLI `--help` and the API response `documentation_url` field both link to the readiness-states explanation

**Edge Cases:**

| Condition                                                                    | Expected Behavior                                                                                                                     |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Symbol has Databento alias but IB refresh never ran                          | `live_qualified=false`; response includes `next_action: "run msai instruments refresh --provider interactive_brokers --symbols AAPL"` |
| `backtest_data_available` check would be expensive (e.g., large window scan) | Return `null` with `backtest_data_available_checked=false` and a note                                                                 |

**Priority:** Must Have

---

### US-007: Bootstrap ES (futures) end-to-end

**As** Pablo
**I want** `bootstrap ES.n.0` to register the continuous-futures contract
**So that** my futures strategy research works the same way as equities research

**Scenario:**

```gherkin
Given a fresh database and GLBX.MDP3 is in my Databento plan
When I run: msai instruments bootstrap --provider databento --symbols ES.n.0
Then the continuous-futures resolver fetches the current front-month definition
And registers the canonical alias (e.g., "ESM6.CME" with effective window covering today)
And a subsequent backtest on ES.n.0 1m bars succeeds via GLBX.MDP3
```

**Acceptance Criteria:**

- [ ] Bootstrap accepts `.n.N` and `.c.N` continuous-futures syntax
- [ ] Reuses the existing `continuous_futures.raw_symbol_from_request` + `_upsert_definition_and_alias` path
- [ ] Codex's claim that `BE-01` (`FuturesContract.to_dict()` signature drift) is fixed on this branch is VERIFIED by Phase 2 research. If `BE-01` is NOT fixed, this PR fixes it.
- [ ] Acceptance includes a 1m ES.n.0 backtest over a short paper window

**Edge Cases:**

| Condition                                               | Expected Behavior                                                        |
| ------------------------------------------------------- | ------------------------------------------------------------------------ |
| GLBX.MDP3 not in Databento plan                         | HTTP 502 `DATABENTO_UNAUTHORIZED` with dataset name                      |
| Front-month roll happens during bootstrap (midnight CT) | Same behavior as PR #35 — pinning `as_of_date` via Chicago local time    |
| User passes raw `ESM6` (not continuous)                 | Route to same `_upsert_definition_and_alias` with `asset_class="future"` |

**Priority:** Must Have

---

### US-008: Metered-mindful rate limiting

**As** Pablo
**I want** bootstrap to respect Databento rate limits and retry transparently
**So that** a batch of 20 symbols doesn't leave my registry half-seeded on a 429 storm

**Scenario:**

```gherkin
Given I bootstrap 10 symbols and Databento returns 429 on the 4th API call
When the rate-limit handler fires
Then the request retries with exponential backoff (2 attempts, 1s and 3s)
And symbols 1-3 succeed, symbol 4 eventually succeeds on retry
And msai_databento_api_calls_total{endpoint, outcome="rate_limited_recovered"} is incremented
```

**Acceptance Criteria:**

- [ ] `databento_client.py` wraps `fetch_definition_instruments` with `tenacity` retry on `databento.BentoHttpError` status 429 + 5xx
- [ ] Retry policy: 3 attempts, exponential backoff (1s, 3s, 9s), abort on 401/403
- [ ] Concurrency cap: `max_concurrent` limits parallel Databento calls (default 3, hard cap 3 in v1)
- [ ] Counter `msai_databento_api_calls_total{endpoint,outcome}` increments on every call (outcomes: `success`, `rate_limited_recovered`, `rate_limited_failed`, `unauthorized`, `network_error`)
- [ ] API response includes `estimated_requests: int` so the operator sees total API-call count up front

**Edge Cases:**

| Condition                           | Expected Behavior                                                                  |
| ----------------------------------- | ---------------------------------------------------------------------------------- |
| All 3 retry attempts fail on 429    | That symbol fails with `DATABENTO_RATE_LIMITED`; batch continues for other symbols |
| Databento returns 500/503           | Retry with same policy as 429                                                      |
| Databento returns 401               | NO retry; immediate `DATABENTO_UNAUTHORIZED`                                       |
| User batch exceeds `max_concurrent` | Queue internally; no request-level rejection                                       |

**Priority:** Must Have

---

### US-009: Divergence observability (post-normalization semantics)

**As** Pablo
**I want** the registry to detect when IB enrichment later disagrees with a Databento-authored venue AFTER normalization
**So that** I can catch REAL venue migrations (e.g., ETF moves ARCA → BATS) without noise from notation-only differences (XNAS vs NASDAQ)

**Scenario:**

```gherkin
Given SPY was bootstrapped via Databento with canonical_id "SPY.ARCA" (post-normalization — raw DBN emitted SPY.XARC)
When I later run: msai instruments refresh --provider interactive_brokers --symbols SPY
And IB qualifies SPY as "SPY.BATS" (hypothetical venue migration)
Then the existing registry write path logs a structured "registry_bootstrap_divergence" event
And increments msai_registry_venue_divergence_total{databento_venue="ARCA", ib_venue="BATS"}
And writes a new alias row (IB becomes authoritative) without blocking the refresh
```

**Post-normalization guarantee:** the Databento-authored alias_string has already been through `normalize_alias_for_registry` at write time (Venue Council 2026-04-23 verdict), so it is stored as `SPY.ARCA` (exchange-name), not `SPY.XARC` (MIC). That means the divergence comparison sees `ARCA` vs `BATS` — a real migration — and NOT `XARC` vs `ARCA` which would be notation churn. Without normalization, this counter would fire on every equity bootstrap-then-refresh and become useless.

**Acceptance Criteria:**

- [ ] The existing IB refresh path (`cli.py:697`, `SecurityMaster._upsert_definition_and_alias`) gains divergence detection: compares the new venue against the most-recent active alias
- [ ] On mismatch, emits structured `registry_bootstrap_divergence` log (keys: `raw_symbol`, `asset_class`, `previous_provider`, `previous_venue`, `new_provider`, `new_venue`, `effective_from_old`, `effective_to_old`, `effective_from_new`)
- [ ] Counter fires exactly once per divergence (integration test)
- [ ] IB refresh does NOT block on divergence — it writes through (IB is authoritative for live)

**Edge Cases:**

| Condition                                                       | Expected Behavior                                      |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| Same venue returned by both providers                           | No divergence log, no counter increment                |
| First IB refresh ever (no prior Databento alias)                | No divergence — treat as clean add                     |
| Divergence from three-way rotation (Databento → IB → Databento) | Each step logs independently; counter increments twice |

**Priority:** Must Have

---

### US-010: Graduation (explicit two-step workflow)

**As** Pablo
**I want** to never be able to accidentally live-deploy a symbol that was only Databento-bootstrapped
**So that** the control-plane safety invariants from PR #37 hold

**Scenario:**

```gherkin
Given AAPL was bootstrapped via Databento but IB refresh has never run
When I submit POST /api/v1/live/start-portfolio with a strategy on AAPL
Then the existing registry read at live-start finds no IB-qualified alias for AAPL
And returns HTTP 422 UNKNOWN_SYMBOL with the existing operator hint "run `msai instruments refresh --provider interactive_brokers --symbols AAPL` first"
```

**Acceptance Criteria:**

- [ ] No code changes to the live-start path in this PR — the PR #37 fail-fast behavior already enforces this invariant
- [ ] The CLI `bootstrap` `--help` text + the API endpoint's OpenAPI description both explicitly state: "Databento-bootstrapped symbols are backtest-discoverable only. Live deployment requires a separate IB refresh."
- [ ] US-006's `live_qualified` flag in the bootstrap response is `false` post-bootstrap, making the two-step workflow visible in the response body

**Priority:** Must Have

---

## 5. Technical Constraints

### Known Limitations

- **Databento plan entitlements are not pre-verified** — the PR assumes Pablo's current plan covers `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3`. Phase 2 research (OQ-1) verifies. If any dataset is unentitled, the PR either scopes down OR documents the fallback (`EQUS.MINI` for equity bars-only fallback).
- **No native Databento FX** — ruled out for v1 by vendor availability.
- **No native Databento cash-index bars** — ruled out for v1 by vendor coverage; proxies used.
- **Databento native timeframes:** `1s / 1m / 1h / 1d`. Intermediate bars (`5m / 10m / 30m`) come from the existing aggregation pipeline, not from Databento directly.
- **`max_concurrent` hard cap = 3** in v1 — conservative until real rate-limit evidence exists.

### Dependencies

- **Requires:**
  - `fetch_definition_instruments` in `databento_client.py:102` (already exists, dataset-agnostic).
  - `_upsert_definition_and_alias` in `security_master/service.py:694` (already exists; PR #37 postscript `8f5f943` added race-free alias rotation for IB rolls — this PR extends the same protection to Databento bootstrap via advisory lock).
  - `run_auto_heal` pipeline from PR #40 (already exists, no changes).
  - `DATABENTO_API_KEY` environment variable (already configured).
  - `tenacity` Python library (already in `pyproject.toml` via transitive dependencies; confirm direct dep or add).
- **Blocked by:** none. All preconditions met on main.

### Integration Points

- **Databento Historical API:**
  - `timeseries.get_range(schema="definition", ...)` for symbol resolution — current primary call.
  - `metadata.list_datasets` for entitlement discovery — Phase 2 research only, not runtime.
  - Rate limits unknown; treat metered-mindful per Q4.
- **Existing MSAI HTTP stack:**
  - New route on `api/instruments.py` (or existing file if present) mounted at `/api/v1/instruments/bootstrap`.
  - Reuses existing auth middleware (JWT or `X-API-Key`).
  - Reuses existing HTTP error envelope `{error: {code, message, details}}`.
- **Existing MSAI CLI:**
  - New `bootstrap` subcommand under `instruments_app` in `cli.py`.
  - Reuses `_api_call` + `_emit_json` pattern from `cli.py:120-150` for API delegation and output formatting.
- **Existing Prometheus metrics registry:**
  - New counters + histogram registered at module import (mirror the `msai_backtest_*` pattern introduced in PR #41).

## 6. Data Requirements

### New Data Models

**None.** The PR reuses existing tables:

- `instrument_definitions` (PR #32 schema) — UUID-keyed control-plane rows.
- `instrument_aliases` (PR #32 schema) — effective-date-windowed alias rows with `provider`, `venue`, `asset_class`.

### Data Validation Rules

- `symbols`: 1 ≤ len ≤ 50 per request (hard cap to match `max_concurrent` batching sanity)
- Each symbol: 1 ≤ len ≤ 32 chars, matches `^[A-Za-z0-9._/-]+$`
- `provider` ∈ `{"databento"}` (others rejected with `UNSUPPORTED_PROVIDER`)
- `asset_class_override` ∈ `{"equity", "etf", "future"}` when provided (`"option"` rejected in v1)
- `max_concurrent`: 1 ≤ val ≤ 3 in v1 (422 if >3)
- `exact_ids` keys must be a subset of `symbols`; values must match a Databento `instrument_id` integer type

### Data Migration

- **None.** Schema is unchanged. Existing registry rows continue to work. New rows use the same columns.

### Advisory Lock (US-008 implementation detail)

- Pattern: `pg_advisory_xact_lock(hashtext(provider || '|' || raw_symbol || '|' || asset_class))` acquired inside the transaction that performs the alias close + insert cycle.
- Prevents two concurrent bootstrap-or-refresh calls from corrupting the alias window for the same symbol.
- Phase 2 research (OQ-4) verifies whether this is strictly required given existing constraints; default is to ship the lock (defensive).

## 7. Security Considerations

- **Authentication:** Required. JWT or `X-API-Key` header — same as other `/api/v1/instruments/*` endpoints. No anonymous bootstrap.
- **Authorization:** Single-user platform, no RBAC differentiation in v1. All authenticated callers can bootstrap.
- **Data Protection:** `DATABENTO_API_KEY` is server-side only — never echoed in responses or logs. Already handled by existing `settings.databento_api_key` loader.
- **Audit:** Every bootstrap operation emits a structured log (`registry_bootstrap_start` with `user_sub`, `symbols`, `provider` + `registry_bootstrap_complete` with `outcomes`). Divergence events logged per US-009.
- **Rate-limit DoS vector:** `max_concurrent=3` hard cap + batch size ≤ 50 limits the blast radius of a rogue client call. Existing auth prevents unauthenticated abuse.
- **No PII:** symbol data is public market data; no customer-identifiable information flows through this path.

## 8. Open Questions

Resolved via Phase 2 `research-first` agent (writes to `docs/research/YYYY-MM-DD-databento-registry-bootstrap.md`):

- [ ] **OQ-1:** Does Pablo's current Databento plan cover `XNAS.ITCH` / `XNYS.PILLAR` / `ARCX.PILLAR` / `GLBX.MDP3`? Verify via `curl -H "Authorization: Bearer $DATABENTO_API_KEY" https://hist.databento.com/v0/metadata.list_datasets | jq`.
- [ ] **OQ-2:** Real Databento rate limits on `metadata.list_symbols` and `timeseries.get_range?schema=definition`. Drives `max_concurrent=3` default + retry policy calibration.
- [ ] **OQ-3:** Verify Codex's claim that `BE-01` (`FuturesContract.to_dict()` signature drift, `CONTINUITY.md BE-01`) is already fixed on current branch. If NOT fixed, must fix in this PR.
- [ ] **OQ-4:** Confirm advisory-lock necessity — do existing UniqueConstraint + CHECK already serialize the alias-upsert path, or is `pg_advisory_xact_lock` strictly required?
- [ ] **OQ-5:** What does `fetch_definition_instruments` return for an ambiguous equity symbol (`BRK.B`, `BF.B`, dual-listed ADRs)? Shape the `candidates[]` payload accordingly.
- [ ] **OQ-6:** Does the existing pipeline's `5m / 10m / 30m` aggregation from `1m` reside in the backtest worker or in the Nautilus catalog layer? Confirms whether US-003's "auto-heal handles bars without code changes" holds for non-native timeframes.

## 9. References

- **Discussion Log:** `docs/prds/databento-registry-bootstrap-discussion.md`
- **Council Verdict:** `docs/decisions/databento-registry-bootstrap.md` (5 advisors + Codex xhigh chairman, 2026-04-23)
- **Related PRDs / Plans:**
  - `docs/plans/2026-04-17-db-backed-strategy-registry.md` — original registry implementation (PR #32/#35)
  - `docs/decisions/live-path-registry-wiring.md` — PR #37 live-path wiring decision
  - `docs/plans/2026-04-21-backtest-auto-ingest-on-missing-data.md` — PR #40 auto-heal pipeline
- **Databento Documentation Sources** (verified by Codex 2026-04-23):
  - Venues and datasets: https://databento.com/docs/venues-and-datasets
  - OHLCV schema: https://databento.com/docs/schemas-and-data-formats/ohlcv
  - GLBX.MDP3: https://databento.com/docs/knowledge-base/datasets/glbx-mdp3
  - Spot FX waitlist ("Coming soon" as of 2026-04-23): https://databento.com/signup/waitlist

---

## Appendix A: Revision History

| Version | Date       | Author                         | Changes     |
| ------- | ---------- | ------------------------------ | ----------- |
| 1.0     | 2026-04-23 | Claude + Pablo (Codex-advised) | Initial PRD |

## Appendix B: Approval

- [ ] Pablo approval
- [ ] Ready for technical design (Phase 2 research → Phase 3 plan)
