# UC6 — Jobs drawer polling discipline

**Interface:** UI
**Priority:** Must
**Maps to PRD:** US-006, US-008, NFR §8.1

## Intent

The Jobs drawer polls each in-progress run every 2 seconds while the tab is visible, exponential-backoff-capped at 30s on no-state-change, pauses while the tab is hidden, and stops entirely once the run reaches a terminal status (`completed | failed | completed_with_failures`). This use case verifies all three rules — the "Hawk-blocker" disciplines codified in `computeRefetchInterval` (Override O-7 + O-13 + O-16).

## Setup

- Stack running.
- Symbol that takes ≥ 60s to onboard (use a multi-month window so the worker has real work to do — e.g., `AAPL equity 2020-01-01 → 2026-01-01`).
- Authenticated as Pablo.
- Browser DevTools open with the Network panel filtered to `status` (matches `/onboard/{run_id}/status`).

## Steps

### Phase A — Active polling at 2s

1. Navigate to `/market-data`.
2. Click `Add symbol` → enter `AAPL equity 2020-01-01 → 2026-01-01` → `Estimate` → `Confirm`.
3. Click `Jobs` header button → drawer opens (`data-testid="jobs-drawer"`) with the new run in `Active`.
4. Watch the Network panel for ~10 seconds.

### Phase B — Visibility pause

5. Switch to a different browser tab (or minimize the window) for 10 seconds.
6. Switch back to the `/market-data` tab.

### Phase C — Terminal stop

7. Stay on the page until the run reaches `completed` or `completed_with_failures` (typically < 90s).
8. Continue watching the Network panel for 10 seconds after terminal status.

## Verification

### Phase A

- `/onboard/{run_id}/status` requests fire every 2s while `in_progress` (count: ~5 requests in 10s).
- Each response shows `status: "in_progress"` (or `pending` initially).

### Phase B

- During the hidden-tab window: NO new `/status` requests are made.
- After returning to the tab: polling resumes.

### Phase C

- After terminal status: NO further `/status` requests for that run_id.
- The active count in the `Jobs` header (`Jobs (1)` → `Jobs (0)`) reflects the run leaving the active set.

## Backoff observation (optional)

If the run stays in the same status (e.g., long `pending` queue) for several consecutive polls, the interval should grow: 2s → 4s → 8s → 16s → 30s (capped). The current pure helper is `computeRefetchInterval({status, prevStatus, consecutiveSameCount})` — testable as a pure function once vitest is added in v1.1.

## Persistence

Refresh the page mid-run → the run remains in `activeRunIds` only if the page state is preserved (currently in-memory; v1 trade-off, page reload clears the active set). The underlying onboard run continues to completion regardless; reload a few seconds later and inspect via `GET /api/v1/symbols/onboard/{run_id}/status` to confirm.
