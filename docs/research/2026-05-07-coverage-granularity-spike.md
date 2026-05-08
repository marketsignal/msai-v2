# Spike: Can production paths emit partial-month parquet files?

**Date:** 2026-05-07
**Author:** Claude (Pablo's session)
**Question (from council):** Before committing to Scope B for the `compute_coverage` day-precise refactor, prove whether real ingest/backfill/retry paths can create partial-month parquet files. If impossible by construction, Scope B may be reduced; if possible, Scope B remains mandatory.

## Verdict

**Yes — partial-month parquet files are absolutely producible by today's production paths. Scope B is mandatory.**

## Evidence

### 1. Writer is day-precise and idempotent on merge

`backend/src/msai/services/parquet_store.py:35-85` — `write_bars` partitions by `(year, month)` and writes only the rows present in `df`. Existing month-files are merged + dedup'd by timestamp. The writer never pads, extends, or backfills the requested range; it writes exactly what the provider returned within whatever month-boundaries those rows fall on.

Implication: any caller that submits a sub-month range will create a partial-month file on first write; any caller that submits a sub-month range _after_ a successful full-month ingest is harmless (merge dedup keeps the existing rows).

### 2. Sub-month onboarding requests are first-class

`backend/src/msai/api/symbol_onboarding.py` exposes `POST /api/v1/symbols/onboard` with arbitrary `start`/`end` per-symbol (see `OnboardSymbolSpec` in `manifest.py:43-47`, the schema in `schemas/symbol_onboarding.py`, and `_DatabentoClientProto` cost-estimation). The PRD `docs/prds/symbol-onboarding.md` and the existing UI dialog (`frontend/src/components/market-data/dialogs/add-symbol-dialog.tsx`) explicitly let users pick any `start → end` window.

**Concrete failure case:** user onboards `MSFT equity 2024-12-15 → 2025-04-30`. Worker writes day-precise rows into `2024/12.parquet` (days 15-31), `2025/01.parquet`, `2025/02.parquet`, `2025/03.parquet`, `2025/04.parquet`. `compute_coverage` scans → all 5 month files exist → `status="full"`. But December 1-14 was never ingested. A backtest run on `MSFT 2024-12-01 → 2025-04-30` would silently read the partial December.

### 3. Per-range Repair flow writes sub-month windows

`frontend/src/components/market-data/row-drawer.tsx:99-118` — each entry in `row.missing_ranges` rendered as a "Repair" button that calls `onRepairRange(row, r)` with the exact `(r.start, r.end)` of that gap. `useRefreshSymbol` (`frontend/src/lib/hooks/use-symbol-mutations.ts:52-83`) submits this verbatim as an `OnboardRequest`.

Today this is _self-healing_ (because `compute_coverage` only emits whole-month `missing_ranges`, repairs are always month-aligned). After Scope B lands, intra-month `missing_ranges` would flow through this path correctly — meaning Scope B unlocks the per-range Repair UI it was designed for, not just E2E ARRANGE.

### 4. Provider-side partial returns

`backend/src/msai/services/data_ingestion.py:117-134` — the worker writes whatever frame `_fetch_bars` returns. Databento quota mid-stream, Polygon partial responses for thin ETF coverage, network interrupts during streaming downloads — all produce frames with fewer rows than the requested `(start, end)`. PR #48 explicitly mentions "cost-cap fallback gated on `databento_api_key`" which exists precisely because this happens.

### 5. CLI parity

`backend/src/msai/cli.py` exposes `msai ingest stocks AAPL 2024-01-15 2024-01-20` directly (positional `start end`). Operators routinely use sub-month ranges for spot fixes and testing. Anything written via the CLI has the same partial-month exposure.

## Scope B prerequisites (Contrarian's 4 + Hawk's 2)

The council demanded these be pinned BEFORE coding:

| #   | Prereq                           | Recommended answer (subject to confirmation in plan)                                                                                                                                                                                                                                                                                |
| --- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | "Day-precise" definition         | **Trading days, not calendar days.** Use `nautilus_trader.MarketHours` or pandas `bdate_range` filtered by exchange holidays. Calendar-day inspection would falsely flag every weekend as a gap. (Confirmed: `services/nautilus/market_hours.py` exists.)                                                                           |
| 2   | Authoritative timestamp column   | `timestamp` (per `parquet_store.py:60` and `_normalize_bars_frame` in `data_ingestion.py`). Single column, single schema. No multi-version tax to pay.                                                                                                                                                                              |
| 3   | Performance bound                | Read **parquet footer min/max** via `pyarrow.parquet.ParquetFile.metadata` (no full-row read). Cache `(min_ts, max_ts, row_count, file_mtime, file_size)` in a new `parquet_partition_index` table keyed by `(asset_class, symbol, year, month)`. Refresh only when `mtime` or `size` changes. Inventory page p95 stays sub-second. |
| 4   | Capture-before-change            | `scripts/snapshot_inventory.py` — dump current `/api/v1/symbols/inventory` to `tests/fixtures/coverage-pre-scope-b.json` before any code change. After landing, diff to a `coverage-post-scope-b.json`. Newly-flagged gaps are explainable per-symbol.                                                                              |
| 5   | Alert wiring (Hawk)              | Emit `coverage_gap_detected{symbol,asset_class,asset_subclass}` Prometheus metric on every `compute_coverage` call that returns non-empty `missing_ranges` for symbols marked production. Route to existing `services/alerting`.                                                                                                    |
| 6   | Metadata cache invariants (Hawk) | Index table refreshes on `mtime`/`size` change; write-path triggers index update at end of `write_bars`. Backfill via one-time `scripts/build_partition_index.py`.                                                                                                                                                                  |

## Scope-B work surface

`compute_coverage` is the entry point but the change ripples across:

- `coverage.py` — data model: `set[(year, month)]` → either `set[date]` of trading days OR `list[tuple[date, date]]` of contiguous covered runs derived from footer min/max
- `_apply_trailing_edge_tolerance` — currently month-aligned; rewrite for day-aligned cutoff
- `_collapse_missing`, `_run_to_date_range`, `_derive_covered_range` — all month-tuple math, all need rework
- `inventory.py:is_trailing_only` — currently keyed off `missing_ranges[0].start` cohort with month resolution; semantics survive but tests will need new data shapes
- `derive_status` consumers (`gapped`/`stale`/`backtest_only`/`ready` matrix) — likely unchanged at the function-signature level, but every status-derivation test needs day-precise fixture data
- New `parquet_partition_index` table + Alembic migration
- New script: `build_partition_index.py` (one-time backfill)
- New script: `snapshot_inventory.py` (capture-before-change)
- Alerting wiring + metric registration

**Estimated effort:** 2-3 days for a competent IC including review iterations. This does **not** fit `/fix-bug` (which targets 1-2 file fixes). The Pragmatist was right.

## Recommendation to Pablo

1. **Re-classify Item 3 as `/new-feature`** (`coverage-day-precise`), not `/fix-bug`. The Pragmatist's CONDITIONAL becomes the operative path.
2. **Author a proper plan** at `docs/plans/2026-05-07-coverage-day-precise.md` covering all 6 prereqs.
3. **Run capture-before-change first** — `scripts/snapshot_inventory.py` against current main, commit the fixture, then start coding.
4. **Item 2 (vitest) becomes `/quick-fix`** — it's mechanical and orthogonal; no reason to chain it behind a multi-day backend change.

## Files reviewed during spike

- `backend/src/msai/services/parquet_store.py:35-85` (writer)
- `backend/src/msai/services/symbol_onboarding/coverage.py:70-152` (scanner — full file)
- `backend/src/msai/services/symbol_onboarding/manifest.py:108-141` (manifest dedup widening)
- `backend/src/msai/services/symbol_onboarding/orchestrator.py:150-220` (ingest call site)
- `backend/src/msai/services/data_ingestion.py:71-164` (provider→writer)
- `backend/src/msai/services/backtests/auto_heal.py:84-200` (auto-heal flow — full range only)
- `backend/src/msai/api/symbol_onboarding.py:499-581` (repair endpoint — re-uses parent range)
- `frontend/src/components/market-data/row-drawer.tsx:90-120` (per-range Repair UI)
- `frontend/src/lib/hooks/use-symbol-mutations.ts:52-83` (refresh mutation contract)
- `backend/src/msai/cli_symbols.py:90-110` (CLI repair) + `backend/src/msai/cli.py` (`msai ingest` arg shape)
