# UC-CDP-004 — Vacuous-window symbol does NOT emit gap alert (API, edge)

**Interface:** API
**Priority:** Should
**Status:** GRADUATED 2026-05-07
**Last Result:** PASS

## Intent

When the request window has ZERO expected trading days (e.g. Sat→Sun), `compute_coverage` returns `status="full"` vacuously and MUST NOT emit the `coverage_gap_detected` counter or fire an alert. Confirms the metric is gated on the gapped exit only.

## Setup

Stack running. Pick a symbol that has zero registered data (or any registered symbol — the test is read-only).

## Steps

1. `GET /api/v1/symbols/readiness?symbol=MSFT&asset_class=equity&start=2024-01-06&end=2024-01-07` (Sat→Sun, no trading days in the window).

## Verification

- Response 200.
- `coverage_status == "full"` (vacuous full — no expected trading days).
- `missing_ranges == []`.
- `/metrics` does NOT increment `msai_coverage_gap_detected_total{symbol="MSFT",asset_class="stocks"}`.
- `/api/v1/alerts/` shows no new MSFT alert.

## Why

Confirms `compute_coverage` correctly handles windows with zero trading days. Pre-Scope-B this returned `status="full"` for any window (since no months were present); Scope B preserves that semantic for the right reason (empty expected set, not empty present set). Counter gating prevents alert noise on weekend windows.
