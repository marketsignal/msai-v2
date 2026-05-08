# UC-CDP-UI-002 — Inventory page renders sub-month gap as a single Repair button (UI, smoke)

**Interface:** UI
**Priority:** Should
**Status:** GRADUATED 2026-05-07 (DRAFT — pending Azure auth setup before re-run)
**Last Result:** FAIL_INFRA (Azure Entra ID auth not configured for E2E; pre-existing infra gap)

## Intent

Smoke test: the inventory drawer renders a sub-month missing range with a single Repair button using the predictable `data-testid` pattern.

## Setup

Same as UC-CDP-UI-001 (sub-month gap state).

## Steps

1. Navigate to `/market-data`.
2. Click the row to open the drawer.

## Verification

- Coverage section renders one `<div>` containing the text `Missing 2024-01-02 → 2024-01-12` and a single Repair button with `data-testid="repair-2024-01-02-2024-01-12"`.
- The toolbar shows `<N> gapped · Repair all` where N reflects the count of rows with `coverage_status == "gapped"` (≥ 1).

## Persistence

Close + reopen the drawer → identical render.

## Pre-condition before re-running

Same as UC-CDP-UI-001 — Azure Entra ID auth setup needed.
