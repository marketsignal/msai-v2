# Bulk ingest end-exclusivity off-by-one — last requested day never ingested

## Problem

Every bulk-ingest entrypoint (CLI `msai ingest`, API `POST /api/v1/market-data/ingest`, symbol
onboarding, smoke runner, backtest auto-heal) permanently missed the **last day** of the requested
window. Coverage/readiness then reported `gapped` / `backtest_data_available=false` forever for any
onboarded window — blocking the backtest gate. The symptom was misdiagnosed in the 2026-06-05
regression report as a "Databento dev data edge" ("no SPY 2024-12-31"), with UC-window adjustments
prescribed as the remedy.

## Root Cause

Contract mismatch across three layers:

- Databento's `get_range` (and `get_cost`) treat `end` as **exclusive** (SDK
  `databento/historical/api/timeseries.py:68`, `metadata.py:426`).
- `DataIngestionService.ingest_historical` forwarded the operator's `end` **verbatim** to the
  provider (`data_ingestion.py` → `databento_client.fetch_bars` → `get_range`).
- `compute_coverage` judges the **closed inclusive** `[start, end]` window (`coverage.py`).

Only `ingest_daily` compensated (`end = session_date + 1d` — a prior Codex review fix), proving the
intended operator semantics were inclusive while the shared bulk method was de-facto exclusive.

## Diagnosis pattern (how it was caught)

Per-symbol missing-day inventory showed each symbol missing **exactly the last day of its own
onboard window** (SPY 2024-12-31, NFLX 2024-04-30, AAPL 2024-01-31). A "data edge" would not track
per-request windows; an off-by-one does. **When every record is missing exactly the boundary of its
own request, suspect interval-convention mismatch, not missing upstream data.**

## Solution (PR: fix/bulk-ingest-end-exclusive)

Option A, contrarian-validated: declare `ingest_historical.end` **inclusive** (operator semantics);
translate at the single provider-aware boundary `_fetch_bars` (Databento branch: `+1d`; Polygon
`/v2/aggs` is already end-inclusive — pass-through). `ingest_daily` un-compensated in the same
change (double-`+1` pinned by test). `cost_estimator` applies the same `+1d` to its `get_cost`
argument so quote == fetched window (bucket keys stay operator-windowed). Malformed `end` strings
raise an actionable `end date must be ISO YYYY-MM-DD, got '...'` at the first parse point.
Help-text: "(inclusive)" on both CLI ingest surfaces + `IngestRequest.end` + `OnboardSymbolSpec`.

Tests: `backend/tests/unit/services/test_data_ingestion_window_semantics.py` runs the REAL
`ingest_historical` against capturing provider stubs (isolation recipe: no-op `ensure_catalog_data`
stub — it raises `FileNotFoundError` on missing raw data; `settings.data_root` → tmp_path; non-empty
1-row frames — the all-empty guard raises `RuntimeError`).

## Healing existing data

Already-onboarded symbols keep their phantom last-day gap until re-ingested. Re-onboard under a
**FRESH watchlist name** — the idempotency digest is per (watchlist, symbols, windows), so a reused
name replays the historical (possibly pre-fix-failed) run instead of re-ingesting. ParquetStore's
timestamp dedup merges the missing day into the existing files. Verified: SPY full-2024 flipped
`gapped → full` (same instrument_uid).

## Prevention

1. **Document interval conventions at every boundary** — the fix's docstrings state
   inclusive/exclusive at `ingest_historical`, `_fetch_bars`, `cost_estimator`, the CLI help, and
   the schemas. Per-provider translation lives ONLY where the provider is known.
2. **When a provider SDK has exclusive ends, grep for every call site** — the daily path was fixed
   in isolation once before (Codex iter-3) and the bulk path missed; this fix centralizes so there
   is exactly one translation point per consumer type (fetch + cost-quote).
3. **E2E setup gotcha:** fixed watchlist names in UCs replay historical onboard runs via the
   idempotency digest — a pre-fix failed run can masquerade as a fresh failure. Use fresh names
   when the prior run predates a data fix (UC-SYM-004 caveat, resolved via worker logs).
4. **Container CLI invocations must use the venv interpreter** — `docker compose exec backend
python` is the system interpreter (missing most deps); the documented pattern is
   `/app/.venv/bin/python -m msai.cli ...`.
