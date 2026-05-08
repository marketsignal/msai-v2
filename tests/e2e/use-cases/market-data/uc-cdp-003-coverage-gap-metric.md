# UC-CDP-003 — Coverage gap emits Prometheus metric + alert (API)

**Interface:** API
**Priority:** Must (Hawk prereq #5 acceptance)
**Status:** GRADUATED 2026-05-07
**Last Result:** PASS

## Intent

Every `compute_coverage` call that returns `status="gapped"` increments `msai_coverage_gap_detected_total{symbol, asset_class}` AND routes through `AlertingService.send_alert(...)`. Other exits (vacuous-full, none, post-tolerance-full) MUST NOT emit.

## Setup

Same as UC-CDP-001 — gap state exists for the freshly-onboarded symbol.

## Steps

1. Read the metrics surface (unauthenticated, internal):

   ```bash
   curl -s http://localhost:8800/metrics | grep msai_coverage_gap_detected_total
   ```

2. Note the current value `V_before` for `{symbol="<SYMBOL>",asset_class="stocks"}` (zero or absent if first scan).

   > **Note on the metric label.** The counter labels use the **INGEST** taxonomy (`stocks`, `forex`, `futures`, `options`, `crypto`) — not the registry taxonomy (`equity`, `fx`, `option`). `compute_coverage` is called from the API/orchestrator with `asset_class=ingest_asset` (post-`normalize_asset_class_for_ingest`), and the counter increments under THAT value. Phase 5.4 verify-e2e caught this stale-label issue.

3. Trigger a fresh inventory scan: `GET /api/v1/symbols/inventory?start=2024-01-01&end=2024-04-30`.
4. Re-read `/metrics`.

## Verification

- Step 4 shows a line `msai_coverage_gap_detected_total{asset_class="stocks",symbol="<SYMBOL>"} V_after` with `V_after >= V_before + 1`.
- A matching alert appears via `GET /api/v1/alerts/?limit=10` — most recent record has `level="warning"` and `title` containing the symbol.

## Persistence

Re-fetch `/api/v1/alerts/` → the alert is durable across the 200-record cap.
