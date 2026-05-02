# UC2 — Add a new symbol with $0 cost (in-plan)

**Interface:** UI
**Priority:** Must
**Maps to PRD:** US-002

## Intent

User adds a new symbol. Cost preview shows $0.00 (Pablo's Databento plan covers v1 schemas: equity dbeq.basic, futures glbx.mdp.3, FX-futures glbx.mdp.3). Submit succeeds; row appears in inventory after the worker run completes.

## Setup

- Stack running.
- Symbol `MSFT` is NOT yet onboarded (use a fresh symbol if MSFT already exists; verify via `GET /api/v1/symbols/inventory` first).
- Authenticated as Pablo.

## Steps

1. Navigate to `/market-data`.
2. Click the `Add symbol` header button (`data-testid="header-add-symbol"`).
3. In the modal (`data-testid="add-symbol-dialog"`):
   - Type `MSFT` into the symbol input (`data-testid="add-symbol-input"`).
   - Select asset class `Equity`.
   - Leave the default dates (5y trailing window).
4. Click `Estimate cost`.
5. Wait for the dry-run response (typically < 1s).
6. Verify the cost-preview banner reads `$0.00 — included in your Databento plan` (emerald background).
7. Click `Confirm` (`data-testid="add-symbol-confirm"`).
8. Modal closes. A success toast appears (sonner).
9. Wait up to 30s for the onboard run to complete (poll `/api/v1/symbols/onboard/{run_id}/status` or watch the inventory refresh).

## Verification

- The cost preview banner is the **emerald-tinted "$0 included"** version, NOT a sky-blue "Estimated $X" banner and NOT a red "cap exceeded" error.
- `POST /api/v1/symbols/onboard` returns 202 + `run_id` (visible in DevTools Network panel).
- The Jobs drawer (`Jobs` header button → opens drawer with `data-testid="jobs-drawer"`) shows the run with `succeeded` status after completion.
- After ~30s: MSFT row appears in the inventory with status `Ready` or `Backtest only` (depending on IB qualification — the worker may not yet have qualified MSFT live).
- The row's coverage column shows the requested date range.

## Persistence

Reload `/market-data` → MSFT row still visible. Underlying state lives in `instrument_definitions` + Parquet, not in client memory.
