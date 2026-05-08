# UC-CDP-001 — Inventory surfaces a sub-month gap (API)

**Interface:** API
**Priority:** Must
**Status:** GRADUATED 2026-05-07 from `docs/plans/2026-05-07-coverage-day-precise.md` (Phase 6.2b, post Phase 5.4 PASS)
**Last Result:** PASS

## Intent

Day-precise `compute_coverage` surfaces an intra-month missing range that the pre-Scope-B month-granularity scan would have masked as `status="full"`.

## Setup (ARRANGE — sanctioned API only)

1. Stack running: `docker compose -f docker-compose.dev.yml up -d`; `curl -sf http://localhost:8800/health`.
2. Migration applied: `docker exec msai-claude-backend uv run alembic upgrade head` (head must be `aa00b11c22d3` or later).
3. Backfill index (one-time per environment): `docker exec msai-claude-backend bash -lc "cd /app && uv run python scripts/build_partition_index.py"`.
4. Onboard a fresh symbol with a sub-month start window (use a symbol NOT yet in the registry; e.g. GOOGL, NFLX, META — checking via `GET /api/v1/symbols/inventory` first):

   ```bash
   curl -sf -X POST http://localhost:8800/api/v1/symbols/onboard \
     -H "X-API-Key: $MSAI_API_KEY" -H "Content-Type: application/json" \
     -d '{"watchlist_name":"e2e-cdp-001","symbols":[{"symbol":"GOOGL","asset_class":"equity","start":"2024-01-15","end":"2024-04-30"}]}'
   ```

5. Poll the returned `run_id` via `GET /api/v1/symbols/onboard/{run_id}/status` until `status` reaches a terminal value (`completed` or `completed_with_failures`).

## Steps

1. `GET /api/v1/symbols/inventory?start=2024-01-01&end=2024-04-30&asset_class=equity`.

## Verification

- Response 200; the freshly-onboarded symbol's row is present.
- `coverage_status == "gapped"`.
- `missing_ranges` contains an entry `{"start": "2024-01-02", "end": "2024-01-12"}` (sub-month — proves day-precision; 2024-01-01 is New Year's, 2024-01-13/14 are weekend).
- `is_stale == false` (the gap is older than the 7-trading-day trailing-edge window).

## Persistence

Re-fetch the same endpoint after a 5-second wait → identical body. The cache table state survives.

## Why this is the happy-path day-precise contract

Pre-Scope-B, this exact ARRANGE returned `coverage_status: "full"` because all four month files existed on disk. The non-month-aligned `missing_ranges` tuple is the visible day-precise behavior change.

## Pre-existing AAPL caveat

If AAPL has been onboarded for `2024-01-01 → 2024-12-31` already (Task 0 baseline state), DON'T use AAPL — its existing parquet for Jan 2-12 will mask the gap. Always pick a NEW symbol.
