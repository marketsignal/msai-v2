# UC3 — Refresh a stale symbol

**Interface:** UI
**Priority:** Must
**Maps to PRD:** US-003

## Intent

User sees a row marked `Stale` (last refresh > 7 days OR trailing-edge-only missing month). Clicking Refresh kicks off an onboard run that brings the row back to `Ready`.

## Setup

- Stack running with at least one symbol that is currently `Stale`.
- Sanctioned ARRANGE options:
  1. **Pre-existing stale row** — onboard a symbol over a window that ends 8+ days in the past (e.g., `start=2024-01-01, end=<today minus 10 days>`).
  2. **Trailing-edge gap** — onboard with `end=<today minus prev-month>` so `is_trailing_only` triggers stale via the readiness derivation.
- Authenticated as Pablo.

## Steps

1. Navigate to `/market-data`.
2. Confirm at least one row shows the `Stale` status pill (yellow tint).
3. Click the kebab menu on the stale row (`data-testid="row-menu-<SYMBOL>"`).
4. Click `Refresh`.
5. Watch the toast: `Refresh queued (run <prefix>…)`.
6. Open `Jobs` drawer; verify the run appears in `Active`.
7. Wait for terminal status (poll up to 60s; backoff 2s → 30s per `computeRefetchInterval`).
8. After completion, the run disappears from `Active` (terminal-stop) and the inventory query is invalidated.

## Verification

- Initial state: row's status pill = `Stale` and last-refresh column shows yellow text.
- POST `/api/v1/symbols/onboard` body matches the row's symbol / asset_class with the current toolbar window.
- After completion: row's status pill flips to `Ready` (or `Backtest only` if not IB-qualified); `last_refresh_at` is < 1 minute old.
- DevTools network: `/onboard/{run_id}/status` polls every 2s while in_progress. On completion the polling STOPS (no further requests).

## Persistence

Reload → row remains `Ready`; the `Stale` tint is gone. Stale flag is server-derived; no client-only state.
