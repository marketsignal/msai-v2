# UC1 — Browse the market-data inventory

**Interface:** API + UI
**Priority:** Must
**Maps to PRD:** US-001

## Intent

User opens /market-data and sees every registered symbol with its asset class, status badge, coverage, and last-refresh time.

## Setup (ARRANGE — sanctioned methods only)

- Stack running via `docker compose -f docker-compose.dev.yml up -d`
- At least 3 registered symbols across asset classes via `msai symbols onboard <manifest>` (CLI; sanctioned). Example manifest:
  ```json
  {
    "watchlist_name": "e2e-uc1",
    "symbols": [
      {
        "symbol": "AAPL",
        "asset_class": "equity",
        "start": "2024-01-01",
        "end": "2026-01-01"
      },
      {
        "symbol": "ES.c.0",
        "asset_class": "futures",
        "start": "2024-01-01",
        "end": "2026-01-01"
      }
    ]
  }
  ```
- Authenticated as Pablo via existing dev-auth bypass (X-API-Key header with `MSAI_API_KEY`)

## Steps

1. **API:** `GET /api/v1/symbols/inventory?start=2021-05-01&end=2026-05-01`
2. **UI:** Navigate to `http://localhost:3300/market-data`
3. Wait for inventory load (< 2s expected for ≤ 80 rows per PRD §8.1)
4. Inspect table rows

## Verification

- API: response is `200 OK`, body is JSON array of length ≥ 3, every row has fields `{instrument_uid, symbol, asset_class, provider, registered, backtest_data_available, coverage_status, covered_range, missing_ranges, is_stale, live_qualified, last_refresh_at, status}`.
- API: `status` is one of `ready | stale | gapped | backtest_only | live_only | not_registered`.
- UI: visible table with one row per registered symbol (look for `data-testid="inventory-row-AAPL"` etc.).
- UI: each row shows the status pill (Ready / Stale / Gapped / Backtest only / Live only / Not registered).
- UI: coverage column shows the date range or "none".
- UI: stale rows have a yellow background tint (DevTools: row class includes `bg-yellow-500/[0.06]`).

## Persistence

Reload the page → same rows visible (no client-only state; matrix is server-derived from `instrument_definitions` + `instrument_aliases` + `parquet_store.compute_coverage`).
