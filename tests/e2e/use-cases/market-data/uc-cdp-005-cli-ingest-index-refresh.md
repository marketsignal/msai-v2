# UC-CDP-005 — CLI ingest refreshes parquet_partition_index (CLI)

**Interface:** CLI
**Priority:** Must
**Status:** GRADUATED 2026-05-07
**Last Result:** PASS (after fix `e9c2952` — original Phase 5.4 run found this as FAIL_BUG)

## Intent

Operator-driven `msai ingest` invocations correctly update `parquet_partition_index` after writing parquet, so day-precise `compute_coverage` reflects the new data immediately (no manual `build_partition_index.py` rerun needed).

## Setup

Migration applied; index backfilled. Stack running.

## Steps

1. Wipe any existing cache row for the (asset_class, symbol, year, month) we'll target — to make the test deterministic:

   ```bash
   docker exec msai-claude-postgres psql -U msai -d msai -c \
     "DELETE FROM parquet_partition_index WHERE symbol='AAPL' AND year=2025;"
   ```

2. Run the CLI ingest (host-`uv` doesn't have the right `DATABASE_URL`; use the container):

   ```bash
   docker exec msai-claude-backend bash -lc \
     "cd /app && uv run python -m msai.cli ingest stocks AAPL 2025-01-22 2025-01-24"
   ```

3. After completion: `docker exec msai-claude-backend bash -lc "cd /app && uv run python -m msai.cli data-status"`.

## Verification

- Step 2 exits 0; stdout indicates rows written (`bars: <N>`, `first_timestamp`, `last_timestamp`).
- `parquet_partition_index` has exactly 1 row for `(stocks, AAPL, 2025, 1)`:

  ```bash
  docker exec msai-claude-postgres psql -U msai -d msai -c \
    "SELECT symbol, year, month, min_ts, max_ts, row_count FROM parquet_partition_index WHERE symbol='AAPL' AND year=2025;"
  ```

  - `min_ts.date() == 2025-01-22` (or earlier if previous data remained on disk and was merged).
  - `max_ts.date() == 2025-01-23` (Friday before the inclusive end).
  - `row_count > 0`.

- Step 3 (`data-status`) exits 0; doesn't crash.

## Persistence

Re-run the CLI command (idempotent) → same `parquet_partition_index` row (mtime/size may change if writer rewrote the file; row_count unchanged because of timestamp dedup at write time).

## Why

Pre-fix `e9c2952`, the CLI's `ParquetStore(...)` constructor did NOT pass `partition_index_refresh`. Result: parquet on disk but cache empty → silent staleness. Fix wires `make_refresh_callback(database_url=settings.database_url)` into all 4 `ParquetStore(...)` construction sites (3 in `cli.py`, 1 in `workers/nightly_ingest.py`).
