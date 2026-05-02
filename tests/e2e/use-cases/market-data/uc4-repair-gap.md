# UC4 — Repair a mid-window gap

**Interface:** UI
**Priority:** Must
**Maps to PRD:** US-004

## Intent

User sees a row marked `Gapped` (one or more mid-window missing months). The drawer shows each missing range with its own `Repair` button. Clicking it kicks off a scoped onboard limited to that range. After completion, the gap closes and the status flips to `Ready`.

## Setup

- Stack running.
- Sanctioned ARRANGE: onboard a symbol over a window with deliberately split coverage.
  - Onboard `AAPL` over `2024-01-01 → 2024-06-30`.
  - Onboard `AAPL` over `2024-09-01 → 2024-12-31`.
  - Result: `AAPL` is `Gapped` with one missing range `2024-07-01 → 2024-08-31`.
  - **NOT allowed:** direct Parquet file deletion or DuckDB writes (per `.claude/rules/critical-rules.md` ARRANGE rules).
- Authenticated as Pablo.

## Steps

1. Navigate to `/market-data`.
2. Confirm `AAPL` row shows status `Gapped` and coverage shows `2024-01 → 2024-12 · 1 gap`.
3. Click the row to open the drawer (`data-testid="row-drawer"`).
4. In the `Coverage` section, locate the missing-range entry `Missing 2024-07-01 → 2024-08-31`.
5. Click the per-range `Repair` button (`data-testid="repair-2024-07-01-2024-08-31"`).
6. Watch the toast: `Refresh queued (run <prefix>…)`.
7. Wait for the run to complete (poll via `/onboard/{run_id}/status`).
8. The drawer's `Coverage` section refreshes (inventory invalidated by the mutation onSuccess).

## Verification

- Initial state: row status = `Gapped`, drawer's `Coverage` section lists exactly one missing range.
- POST `/api/v1/symbols/onboard` body symbols spec is `{symbol: "AAPL", asset_class: "equity", start: "2024-07-01", end: "2024-08-31"}` — scoped to the gap, not the full window.
- After completion: drawer shows `No gaps in current window.`; row status flips to `Ready` (or `Backtest only` if not IB-qualified).
- The `gappedCount` in the toolbar (`<N> gapped · Repair all`) decrements by 1.

## Persistence

Reload → row remains `Ready`, drawer reopen confirms no gaps.
