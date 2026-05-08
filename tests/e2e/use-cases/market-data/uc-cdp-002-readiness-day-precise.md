# UC-CDP-002 — Readiness endpoint reflects day-precise gap (API)

**Interface:** API
**Priority:** Must
**Status:** GRADUATED 2026-05-07 from `docs/plans/2026-05-07-coverage-day-precise.md`
**Last Result:** PASS

## Intent

`GET /api/v1/symbols/readiness` returns a per-symbol day-precise coverage report with sub-month `missing_ranges` and trading-day-min/max `covered_range`.

## Setup

Same as UC-CDP-001 — re-uses the freshly-onboarded symbol with a sub-month parquet.

## Steps

1. `GET /api/v1/symbols/readiness?symbol={SYMBOL}&asset_class=equity&start=2024-01-01&end=2024-04-30`.

   > **Path note.** The readiness endpoint takes `symbol` as a query parameter, NOT a path parameter. `/api/v1/symbols/{symbol}/readiness` returns 404. Phase 5.4 verify-e2e found this stale path-style.

## Verification

- Response 200.
- `coverage_status == "gapped"`.
- `missing_ranges` contains `{"start": "2024-01-02", "end": "2024-01-12"}`.
- `backtest_data_available == false` (gap means not full).
- `covered_range` is a non-null string of the form `"2024-01-16 → 2024-04-29"` — trading-day min/max from the parquet footer cache, NOT the request window. Format: `"YYYY-MM-DD → YYYY-MM-DD"` (en-dash separator). Matches `services/symbol_onboarding/coverage.py:_derive_covered_range`.

## Persistence

Re-fetch → identical body.
