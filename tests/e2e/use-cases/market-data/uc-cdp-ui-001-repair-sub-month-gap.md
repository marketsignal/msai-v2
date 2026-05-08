# UC-CDP-UI-001 — Repair a sub-month coverage gap (UI)

**Interface:** UI
**Priority:** Must
**Status:** GRADUATED 2026-05-07 (DRAFT — pending Azure auth setup before re-run)
**Last Result:** FAIL_INFRA (Azure Entra ID auth not configured for E2E; pre-existing infra gap)

## Intent

A user sees a `Gapped` row with a sub-month missing range, clicks the per-range Repair button, and the worker re-fetches that exact window. After completion the row flips to `Ready` (or `Backtest only`).

## Setup

Same gap state as UC-CDP-001 (sub-month parquet exists for the symbol; inventory shows `coverage_status="gapped"` with `missing_ranges` containing `{start:"2024-01-02", end:"2024-01-12"}`).

The pre-existing UC1-UC6 from this directory continue to operate; UC-CDP-UI-001 is an extension proving sub-month range shape, not a replacement.

## Steps

1. Authenticated as Pablo, navigate to `http://localhost:3300/market-data`.
2. Confirm the row shows status `Gapped` and the toolbar's gappedCount ≥ 1.
3. Click the row to open the drawer (`data-testid="row-drawer"`).
4. In the drawer's `Coverage` section, locate the missing-range entry `Missing 2024-01-02 → 2024-01-12`.
5. Click the per-range Repair button (`data-testid="repair-2024-01-02-2024-01-12"`).
6. Watch for the toast `Refresh queued (run <prefix>…)`.
7. Poll the inventory query (the mutation invalidates `["inventory"]` in TanStack Query) until the row status flips.

## Verification

- The Repair button POSTs `/api/v1/symbols/onboard` with body `symbols[0]={symbol:"<SYMBOL>", asset_class:"equity", start:"2024-01-02", end:"2024-01-12"}` — scoped to the gap, NOT the original full window. Verify via Playwright `page.expect_request(/symbols\/onboard/)`.
- After the run completes: drawer's Coverage section shows `No gaps in current window.`; row status flips to `Ready` (or `Backtest only` if not IB-qualified).
- Toolbar's gappedCount decrements by 1.

## Persistence

Reload `/market-data` → row remains `Ready`; reopening the drawer confirms no gaps.

## Why this is sub-month-specific (not a duplicate of UC4)

`uc4-repair-gap.md` exercises a month-aligned `2024-07-01 → 2024-08-31` gap. UC-CDP-UI-001 proves a missing range can start on a non-1st day (`2024-01-02`) and end mid-month (`2024-01-12`). Pre-Scope-B that range shape was structurally impossible — the month-granularity scan only emitted month-aligned tuples.

## Pre-condition before re-running

UI E2E requires authenticated browser state. As of 2026-05-07 the project has no Azure Entra ID dev-bypass token or persisted MSAL storage state. Before this UC can run:

- Either configure `frontend/tests/e2e/.auth/admin.json` with a persisted MSAL state, OR
- Configure a backend dev-bypass that the UI can use (parallel to the existing API `MSAI_API_KEY=msai-dev-key`).

This is a pre-existing infra gap, not a Scope B bug. Tracked as a separate ticket.
