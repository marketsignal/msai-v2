# UC5 — Remove a symbol from inventory

**Interface:** UI + API
**Priority:** Should (Pablo confirmed in v1 — Override O-3)
**Maps to PRD:** US-005

## Intent

User removes a symbol from the inventory. Confirmation dialog explains soft-delete semantics (Parquet preserved, strategies not blocked, re-onboard restores). On confirm, the row disappears immediately. Re-onboarding the same symbol restores it (Override O-11 race fix).

## Setup

- Stack running.
- At least one onboarded symbol (e.g., `AAPL` equity) visible in inventory.
- Authenticated as Pablo.

## Steps

### Phase A — Remove

1. Navigate to `/market-data`.
2. Locate the `AAPL` row.
3. Click the kebab menu (`data-testid="row-menu-AAPL"`) → `Remove`.
4. Confirm-remove dialog appears (`data-testid="remove-confirm-dialog"`):
   - Title reads `Remove AAPL from inventory?`.
   - Description explains soft-delete + active-strategy non-blocking.
5. Click `Remove` (`data-testid="remove-confirm-action"`).
6. Toast: `Symbol removed from inventory`.
7. Row disappears from the table immediately.

### Phase B — Re-onboard restores

8. Click `Add symbol` → enter `AAPL` / `Equity` / default dates.
9. Estimate cost (banner shows $0).
10. Confirm.
11. Wait ~30s for the onboard run to complete.

## Verification

### Phase A

- `DELETE /api/v1/symbols/AAPL?asset_class=equity` returns `204 No Content` (Network panel).
- The row vanishes from the rendered table.
- `GET /api/v1/symbols/inventory` no longer includes `AAPL`.
- The Parquet directory `data/parquet/equity/AAPL/` is **untouched** (verified by ops, NOT by E2E peeking — this is documented invariant, not asserted from the test).

### Phase B

- After re-onboard succeeds, `AAPL` reappears with status `Ready` (or `Backtest only`).
- `GET /api/v1/symbols/inventory` includes `AAPL`. The `hidden_from_inventory` flag was cleared by the API handler's pre-dedup UPDATE (Override O-11).

## Persistence

Reload `/market-data` between Phase A and Phase B → AAPL stays hidden until re-onboarded. Reload after Phase B → AAPL stays visible.

## Race-mode note (informational, NOT an E2E gate)

Override O-15 covers the worker-UPSERT race: if the user removes AAPL while a prior onboard for AAPL is still in flight, the worker MUST NOT un-hide it. That invariant is enforced by integration test `test_worker_upsert_does_not_modify_hidden_from_inventory` rather than by this UI use case.
