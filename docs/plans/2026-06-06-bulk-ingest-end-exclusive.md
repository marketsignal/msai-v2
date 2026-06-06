# Fix: Bulk ingest end-exclusivity off-by-one (last requested day never ingested)

## Goal

Every bulk-ingest entrypoint (CLI `msai ingest`, API `POST /api/v1/market-data/ingest`, symbol onboarding, smoke runner, backtest auto-heal) permanently misses the **last day** of the requested window: the operator's inclusive `end` date is forwarded verbatim to Databento's **exclusive** `get_range`, while `compute_coverage` judges the closed `[start, end]` window. Result: any onboarded window reports `gapped` / `backtest_data_available=false` forever. Fix: declare `ingest_historical`'s `end` **inclusive** and translate to provider semantics at the single provider-aware boundary (`_fetch_bars`), keeping the cost estimator's quote aligned.

## Architecture

- `DataIngestionService.ingest_historical(asset_class, symbols, start, end)` — **operator semantics: `end` inclusive** (matches `compute_coverage`'s closed-window judgment and every caller's intent).
- `DataIngestionService._fetch_bars` — the only point where the resolved provider is known; performs the translation: Databento branch fetches `[start, end+1d)` (SDK end is exclusive — `databento/historical/api/timeseries.py:68`); Polygon branch passes `end` through (Polygon `/v2/aggs` is end-inclusive).
- `DataIngestionService.ingest_daily` — STOPS pre-compensating (`data_ingestion.py:239` currently passes `session_date + 1d`); passes `session_date` as the inclusive end. Net Databento window unchanged (`[X, X+1)`); Polygon daily improves (no more documented X/X+1 double-fetch).
- `cost_estimator.estimate_cost` — applies the same `end + 1d` to `metadata.get_cost` (also exclusive — SDK `metadata.py:426`) so the quoted window equals the fetched window.
- Clients (`DatabentoClient.fetch_bars`, `PolygonClient.fetch_bars`) — UNCHANGED: they keep raw provider semantics. `DatabentoClient.fetch_definition_instruments` stays documented `[start, end)`.

## Tech Stack

Python 3.12, FastAPI service layer, Databento SDK (installed version's `get_range`/`get_cost` both end-exclusive — verified in `.venv` source), pytest.

## Approach Comparison (final — 3.1c VALIDATE)

| Axis             | **A: Service-inclusive, translate at `_fetch_bars`** (CHOSEN)                                                                                   | B: Caller-side `+1` at every entrypoint                             | C: Make `DatabentoClient` inclusive                                               |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Mechanism        | `ingest_historical.end` declared inclusive; `_fetch_bars` adds `+1d` on Databento branch; `ingest_daily` un-compensates; `cost_estimator` `+1d` | `+1d` at CLI, API worker, orchestrator, smoke, auto-heal (5+ sites) | `fetch_bars` adds `+1d` internally                                                |
| Complexity       | Low — 2 source files + help-text + tests                                                                                                        | Medium — repeated fix                                               | Low                                                                               |
| Blast radius     | Single boundary everything routes through; fixes Polygon daily double-fetch quirk too                                                           | Scattered; next caller forgets (the mechanism that caused this bug) | Client internally inconsistent with `fetch_definition_instruments` `[start, end)` |
| Reversibility    | High                                                                                                                                            | Low                                                                 | Medium                                                                            |
| Time to validate | Boundary unit tests + E2E fresh-window onboard                                                                                                  | Per-caller                                                          | Same as A + wart                                                                  |
| Correctness risk | `ingest_daily` must be un-compensated in the SAME change (else 2-day daily fetch) — test-pinned                                                 | High long-term                                                      | Maintainer confusion                                                              |

## Contrarian Verdict

**VALIDATE** (Codex gpt-5.5, 2026-06-06, repo-readable): "Option A clearly centralizes inclusive operator semantics at the service/provider boundary, aligns fetch and coverage/cost behavior, and preserves Databento client methods as raw SDK-semantic APIs."

## Root-Cause Evidence (verified)

| Claim                                  | Evidence                                                                                                                                                                                                                                                           |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Databento `get_range` end is exclusive | SDK source `databento/historical/api/timeseries.py:67-68` ("The exclusive end of the request range")                                                                                                                                                               |
| Databento `get_cost` end is exclusive  | SDK source `databento/historical/api/metadata.py:399` (`def get_cost`) / `:426` ("The exclusive end of the request range")                                                                                                                                         |
| Bulk path forwards `end` verbatim      | `data_ingestion.py:120` (`_fetch_bars(plan, raw_symbol, start, end)`) → `databento_client.py:127` (`get_range(..., end=end)`)                                                                                                                                      |
| Coverage judges inclusively            | `coverage.py:3` ("`[start, end]` window"), `:77` (`trading_days(start, end)`)                                                                                                                                                                                      |
| Daily path already compensates         | `data_ingestion.py:239` (`end = session_date + 1d`, Codex iter-3 P1 comment)                                                                                                                                                                                       |
| Empirical                              | Dev: SPY missing exactly `2024-12-31`, NFLX exactly `2024-04-30`, AAPL exactly `2024-01-31` — each precisely its onboard window's last day; `readiness` flips `gapped→full` when the request end moves off the ingested-window end (SPY `end=2024-12-30` → `full`) |

## Tasks

### T1 — RED: window-translation unit tests (new file `backend/tests/unit/services/test_data_ingestion_window_semantics.py`)

Stub Databento/Polygon clients that capture `(start, end)`; construct `DataIngestionService` with them. **Stubs must return a small NON-EMPTY OHLCV frame (1 row, valid columns)** — `ingest_historical` raises `RuntimeError` when ALL symbols return zero bars (`data_ingestion.py:151-155`), so empty-frame stubs would fail the capture tests for the wrong reason.

**Side-effect isolation recipe** (these tests run the REAL `ingest_historical` — the existing `test_data_ingestion_ingest_symbols.py` mocks it entirely, so this is the first unit file to exercise it):

- `ParquetStore(str(tmp_path / "parquet"))` with NO `partition_index_refresh` callback (constructor arg optional — avoids the AsyncEngine/DB hop).
- **Monkeypatch `msai.services.data_ingestion.ensure_catalog_data` to a no-op stub** — it is called unconditionally post-write (`data_ingestion.py:171-176`) against GLOBAL `settings.parquet_root`/`settings.nautilus_catalog_root` (not the test store's root) and `Raises: FileNotFoundError ... if any symbol is missing raw data` (`catalog_builder.py:393-395`), so un-stubbed it fails these tests for the wrong reason or drags Nautilus catalog machinery into a window-semantics unit test. Catalog sync behavior is covered by integration/smoke (`smoke/test_runner_coverage.py`, catalog-freshness suite). Assert the stub was called with the ingested symbols (cheap interaction pin), nothing more.
- Monkeypatch `settings.data_root` to `tmp_path` (drives `status_file`, `data_ingestion.py:71`) so `_write_status` lands in the sandbox.

- `test_ingest_historical_databento_requests_exclusive_end_plus_one` — call with `end="2024-12-31"`; assert the Databento stub received `end="2025-01-01"`. **(RED today: receives `2024-12-31`)**
- `test_ingest_historical_polygon_receives_end_verbatim` — Polygon branch unchanged (`end="2024-12-31"` received as-is).
- `test_ingest_daily_databento_net_window_unchanged` — `target_date=2024-12-30` → Databento stub receives `start="2024-12-30"`, `end="2024-12-31"` (same as pre-fix; pins against double-`+1`).
- `test_ingest_daily_polygon_no_double_fetch` — Polygon stub receives `end="2024-12-30"` (== session date; pre-fix received `2024-12-31`).
- `test_ingest_historical_end_before_start_raises_existing_all_empty_error` — `start="2024-12-31"`, `end="2024-01-01"`: translation is unconditional (stub receives `end="2024-01-02"`), the provider stub returns an empty frame, and the service raises the EXISTING all-empty `RuntimeError` (`data_ingestion.py:151-155`). Pins that the `+1` adds no new validation surface and the current all-empty guard is the failure mode.

### T2 — GREEN: implement translation in `backend/src/msai/services/data_ingestion.py`

- `_fetch_bars`: Databento branch computes `provider_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()`; Polygon branch passes `end` unchanged. Docstring states the per-provider translation.
- `ingest_historical` docstring: `end` is **inclusive** (operator semantics; matches `compute_coverage`).
- `ingest_daily`: pass `start = end = session_date.isoformat()`; rewrite the lines 191-213 docstring block (the `[target_date, target_date+1)` note and the Polygon double-fetch caveat are superseded — note the normalization landed here).
- Keep `IngestTargets`/status payloads recording the OPERATOR window (`start`, `end` inclusive) — `_write_status` and the `"end": end` payload at `data_ingestion.py:165` already record the operator's value; verify nothing records the translated value.

### T3 — cost estimator alignment (`backend/src/msai/services/symbol_onboarding/cost_estimator.py`)

- RED in `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py`: stub `metadata.get_cost` capture — for `spec.end = 2024-12-31`, assert quoted `end="2025-01-01"`.
- GREEN: `end=(spec_end + timedelta(days=1)).isoformat()` in the `get_cost` call; bucket key stays the operator window; docstring notes the exclusivity translation.

### T4 — operator-facing help-text (no behavior change)

- `backend/src/msai/cli.py` top-level `ingest` command (~:312): `help="End date YYYY-MM-DD (inclusive)"`.
- `backend/src/msai/cli.py` `market-data ingest` sub-command (~:2949, the API-backed second ingest surface posting to `/api/v1/market-data/ingest`): same `--end` help update.
- `backend/src/msai/schemas/market_data.py:27`: `description="End date YYYY-MM-DD (inclusive)"`.
- Check `onboard` schema (`schemas/symbol_onboarding.py` / `symbol_onboarding/manifest.py`) for an `end` description; add "(inclusive)" where present.

### T5 — existing-test sweep

- `backend/tests/unit/services/test_data_ingestion_ingest_symbols.py` (asserts `"end": "2024-12-31"` at :22/:133 — these assert the STATUS payload, which keeps operator values; verify they still pass, update only if they pinned the provider-received end).
- `backend/tests/unit/test_nightly_ingest_scheduler.py` — **`test_ingest_daily_window_fetches_target_session` (~:540-564) monkeypatches `ingest_historical` and asserts `end == target_date + 1`; it MUST be updated** to assert `end == target_date` (the inclusive contract — translation now lives below `ingest_historical`, invisible to that capture point). Update its comment accordingly.
- `backend/tests/unit/test_cli.py`, `test_databento_client.py`, `symbol_onboarding/test_cost_estimator.py`, `smoke/test_runner_coverage.py` — run; update any that pin the old provider-received window.

### T6 — E2E + heal existing dev data (Phase 5.4)

Run the UCs below via verify-e2e; as part of verification, heal the known-broken dev rows by re-onboarding under a fresh watchlist name (new idempotency digest → fresh run → ParquetStore dedup merges the missing last day).

## Developer Briefing

**What I'll fix:** Any data download you request (onboarding a symbol, CLI ingest, the API) silently skips the final day you asked for, and the coverage report then permanently claims the data is incomplete — blocking backtests. After the fix, the last day arrives and coverage reads "full". `[planned]`

**How it'll fit:** one translation point where the app talks to Databento converts our "through this date" to Databento's "up to but not including" convention; the daily updater stops doing its own private version of this conversion; the cost preview uses the same conversion so the quoted price matches what's fetched. `[planned]`

**Planned file-map:** `backend/src/msai/services/data_ingestion.py`, `backend/src/msai/services/symbol_onboarding/cost_estimator.py`, `backend/src/msai/cli.py` (help-text), `backend/src/msai/schemas/market_data.py` (description), new + updated tests under `backend/tests/unit/`.

**Key decisions:** operator semantics are inclusive end (matches coverage + intent); provider clients keep raw SDK semantics; translation at `_fetch_bars`/cost-estimator only.

#### E2E Use Cases

**Surface coverage decision:**

- **API: Covered** (UC-IEX-001)
- **CLI: Covered** (UC-IEX-002)
- **UI: N/A** — the fix changes no UI behavior; the inventory/coverage UI renders the same coverage data the API serves (same internal data path — duplicating the assertion through a second interface is explicitly not E2E per testing.md). Existing UI UCs (uc-cdp-ui-001/002) continue to cover the UI rendering of coverage.

**UC-IEX-001 — Onboarded window is complete through its final day (API)**

```
Actor:         Operator onboarding a new symbol via the HTTP API for backtesting
Scenario:      They need a specific historical window (ending on a known trading day)
               available for a backtest. Pre-fix, the readiness report permanently
               showed the final day missing, blocking the backtest gate.
Interface:     API
Intent:        The operator onboards a symbol for an exact date window and sees the
               whole window — including the final day — reported as available.
Setup:         Stack up; auth via X-API-Key. Pick a symbol NOT in inventory
               (GET /api/v1/symbols/inventory first) and a FRESH watchlist name
               (idempotency digest is per watchlist+window — a reused name would
               replay an old run). Window must END on a trading day (e.g.
               2024-06-03 → 2024-06-28). Do NOT pre-ingest the symbol.
Steps:         1) POST /api/v1/symbols/onboard {watchlist, [{symbol, equity, start, end}]}
               2) Poll GET /api/v1/symbols/onboard/{run_id}/status to terminal `completed`
               3) GET /api/v1/symbols/readiness?symbol=X&asset_class=equity&start=<start>&end=<end>
Verification:  Readiness response includes coverage_status="full",
               backtest_data_available=true, missing_ranges=[], and covered_range
               whose right edge is the FINAL TRADING DAY of the requested window
               (pre-fix: gapped with missing_ranges=[{end-day, end-day}]). The
               operator can proceed to run a backtest over the full window.
Persistence:   Re-request the same readiness call after a delay — still full; the
               inventory endpoint lists the symbol with coverage_status="full" for
               the window.
```

**UC-IEX-002 — Shell ingest covers the requested range through the end date (CLI)**

```
Actor:         Operator running the msai CLI on the host to pull data for research
Scenario:      They ingest a specific symbol/date range from the shell before a
               research session. Pre-fix the final day silently never arrived.
Interface:     CLI
Intent:        The operator ingests a date range from the shell and confirms, from
               the shell, that data through the end date landed.
Setup:         Stack up; CLI env (MSAI_API_URL, MSAI_API_KEY, DATABENTO_API_KEY).
               Pick a symbol+window slice not yet on disk (a different month of the
               UC-IEX-001 symbol is fine). End on a trading day.
Steps:         1) Run `msai ingest stocks <SYM> <start> <end>` (worktree backend)
               2) Run readiness check from the shell: curl readiness with the same
                  window (or `msai data-status` + the readiness endpoint)
Verification:  Ingest stdout reports bars written for the symbol with exit 0; the
               follow-up readiness invocation returns coverage_status="full" with
               missing_ranges=[] for the requested window INCLUDING the end date.
Persistence:   A fresh shell invocation of the readiness check returns the same
               "full" answer (data on disk, not session state).
```

**Heal verification (part of the E2E run, not a separate UC):** re-onboard SPY full-2024 under a fresh watchlist name → readiness `start=2024-01-01&end=2024-12-31` flips from `gapped` (missing 2024-12-31) to `full`. This also un-breaks UC-SYM-004 as-written for item 2b's regression sweep.

## Edge note — future-dated `end`

A request whose inclusive `end` is today (or later) now sends Databento `end = end+1`, which can lie beyond the dataset's available range.

- **`get_range`:** safe by production evidence — the daily path already sends ends at/after the available edge (tz-aware nightly passes `target_date = current.date()` post-close → exclusive end = tomorrow relative to UTC at enqueue) and is green.
- **`get_cost`:** NOT separately proven for future ends — but the exposure is **not new**: `OnboardSymbolSpec` validates `start <= today` only (`schemas/symbol_onboarding.py:73` — `end` is unbounded above), and the estimator already passes `spec.end` verbatim (`cost_estimator.py:121,128`), so a manifest with a future `end` reaches `get_cost` un-clamped TODAY. The `+1` extends any such window by one day; it does not create the future-end class. The estimator's existing `except Exception` → `confidence="low"` fallback (`cost_estimator.py:130-141`) already degrades gracefully if Databento rejects a window. No code guard added; behavior documented here. (`manifest.py:82` `trailing_5y` resolves to yesterday — the common path stays past-dated.)

## Out of scope

- Polygon rip-out (separate PR per standing decision; the Polygon branch here is pass-through only).
- The `Persistence`/repair UX around idempotent re-onboards (UC-SYM-007 semantics unchanged).
- Multi-resolution bar_type work.

## Dispatch Plan

| # | Task | Writes (concrete files) | Depends on | Schedule |
|---|------|--------------------------|------------|----------|
| S1 | T1 + T2 — RED window-semantics tests, GREEN translation, daily un-compensation + nightly-test contract update | `backend/src/msai/services/data_ingestion.py`; `backend/tests/unit/services/test_data_ingestion_window_semantics.py` (new); `backend/tests/unit/test_nightly_ingest_scheduler.py` | — | Parallel group A |
| S2 | T3 — cost-estimator quote alignment (TDD) | `backend/src/msai/services/symbol_onboarding/cost_estimator.py`; `backend/tests/unit/services/symbol_onboarding/test_cost_estimator.py` | — | Parallel group A |
| S3 | T4 — operator help-text "(inclusive)" | `backend/src/msai/cli.py`; `backend/src/msai/schemas/market_data.py`; `backend/src/msai/schemas/symbol_onboarding.py` (only if an `end` description exists) | — | Parallel group A |
| S4 | T5 — full unit-suite sweep; fix any remaining tests pinning the old provider-received window | `backend/tests/unit/test_cli.py`; `backend/tests/unit/test_databento_client.py`; `backend/tests/unit/services/smoke/test_runner_coverage.py` (only those that actually fail) | S1, S2, S3 | Serial after group A |
| S5 | T6 — E2E (UC-IEX-001/002) + heal SPY full-2024 | none (verify-e2e is read-only; report persisted by main agent) | S4 | Phase 5.4 |

Write-set disjointness: S1/S2/S3 touch disjoint files (ownership of `test_cost_estimator.py` is S2's, NOT S4's sweep; `test_cli.py` expectations affected by S3's help-text land in S4 — hence S4 depends on all of group A). Concurrency: 3 ≤ cap.
