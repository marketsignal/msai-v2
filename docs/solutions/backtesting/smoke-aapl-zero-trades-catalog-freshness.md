# Smoke passed green while AAPL produced 0 trades (catalog freshness + weak floor)

**Date:** 2026-05-26
**Surface:** operational smoke (`msai backtest smoke`), `services/smoke/runner.py`, `services/nautilus/catalog_builder.py`, `cli.py`

## Problem

The operational smoke (PR #80) passed structurally on prod but `trade_count_by_strategy` showed:

```
__smoke__/ema_cross/AAPL:        0
__smoke__/ema_cross/SPY:         438
__smoke__/smoke_market_order/AAPL: 0   ← deterministic order did NOT fire
__smoke__/smoke_market_order/SPY:  2
trade_count_total: 440   status: completed
```

`smoke_market_order` is designed to emit exactly 1 order per instrument every run, so `smoke_market_order/AAPL = 0` means **AAPL received zero bars** — yet the smoke went green.

## Root Cause

Two independent defects:

1. **Catalog vs parquet coverage divergence.** The smoke pre-flight (`_ensure_ingested` → `_symbols_with_gaps` → `compute_coverage`) checked **raw-parquet** coverage by symbol, but the backtest reads the **Nautilus catalog** keyed by the exact `instrument_id+bar_type` (`AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL`). The catalog build short-circuits on a matching source-hash marker (`nautilus_catalog_already_populated`), so a **stale/missing catalog can serve 0 bars** for the window even when parquet coverage looks "full" (or when a stale partition index made the ingest skip). On prod, AAPL's `AAPL.NASDAQ` catalog lacked Dec-2024 bars at backtest time → 0 trades. SPY's was fresh → 440. The pipeline is otherwise symmetric (dev reproduces AAPL trading 442+2 once the catalog has the bars).

2. **Floor was a SUM, not per-instrument.** The structural floor checked `trade_count_total < 2` — a sum — so SPY's 2 masked AAPL's 0.

## Solution

1. **Catalog-freshness pre-flight** (`runner._ensure_catalog_fresh`, called from `run_smoke` after `_ensure_ingested`): UNDER the ingest mutex (serializes concurrent CLI/UI/scheduler smokes), do a read-only `catalog_has_bars_in_window` check per strategy instrument; if empty → force purge+rebuild from existing parquet (`ensure_catalog_data(force=True)`, tolerating `FileNotFoundError`) → if still empty, in-lock `ingest_symbols` + rebuild → if STILL empty, raise (loud failure > silent 0-trade). `catalog_has_bars_in_window` uses `ParquetDataCatalog.get_intervals` (filename-range overlap, no bar materialization, robust to the weekend/holiday edge-gaps `verify_catalog_coverage` reports).
2. **`build_catalog_for_symbol(force=True)` now purges-and-rebuilds** (unlinks the source-hash marker + purges the bar dir BEFORE rewriting) — previously the purge was nested under `if not force:`, so `force=True` wrote on top of stale bars.
3. **Per-instrument deterministic floor** (`cli.py`): each `__smoke__/smoke_market_order/<symbol>` (symbols from `SMOKE_CONFIGS[config].symbols`) must produce ≥1 trade; an absent key also fails. Replaces the maskable SUM floor.

## Prevention

- The per-instrument floor is the safety net: any future per-symbol zero (any cause) makes the smoke RED, naming the symbol.
- When a check (parquet coverage) is a _proxy_ for the thing a consumer actually reads (the catalog by exact instrument_id+bar_type), verify the consumed artifact directly — proxies drift.
- Concurrency lesson: an unlocked "fast-path" check that coexists with concurrent mutation is inherently racy; serialize the check + mutation under the same mutex (this cost 4 review iterations before dropping the unlocked fast path).

## Verified

- Unit: `tests/unit/services/nautilus/test_catalog_builder.py`, `tests/unit/services/smoke/test_runner_coverage.py`, `tests/unit/test_cli.py`.
- E2E (live dev): `msai backtest smoke --config fast --json` → AAPL+SPY `smoke_market_order=2` each, cold + warm. Report: `tests/e2e/reports/2026-05-26-19-30-smoke-aapl-zero-trades.md`.
- Definitive prod confirmation deferred to post-merge UC-003 (`gh workflow run smoke.yml -f config=fast`) against the real prod data state that triggered the incident.
