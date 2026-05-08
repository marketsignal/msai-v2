# Coverage Day-Precise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `compute_coverage`'s month-granularity scan (`set[(year, month)]` → "month present means month covered") with a trading-day-precise scan that detects intra-month gaps using parquet footer metadata cached in Postgres, and route resulting `coverage_gap_detected` events through the existing alerting service.

**Architecture:** Trading-day inventory is derived from `pyarrow.parquet.ParquetFile.metadata` (footer min/max timestamps + row count) cached per `(asset_class, symbol, year, month)` in a new `parquet_partition_index` Postgres table; cache invalidates on `(file_mtime, file_size)` change and is refreshed on every `ParquetStore.write_bars` call. The set of "expected trading days" comes from `exchange_calendars` (NYSE/CME/etc.) keyed off asset class. `compute_coverage` returns `missing_ranges` as contiguous date runs of trading days absent from the cached footer windows; trailing-edge tolerance is now day-aligned (configurable, defaults to 7 trading days). Alerting wires through the existing `AlertingService` + Prometheus counter `msai_coverage_gap_detected_total{symbol,asset_class}`. The public `CoverageReport` shape (`status` literal + `covered_range: str | None` + `missing_ranges: list[tuple[date, date]]`) is preserved so callers (`api/symbol_onboarding.py`, `services/symbol_onboarding/orchestrator.py`, `services/backtests/auto_heal.py`) compile unchanged. Capture-before-change snapshot script (`scripts/snapshot_inventory.py`) runs against current state BEFORE any code change, post-deployment diff explains every newly-flagged gap per symbol.

**Tech Stack:** Python 3.12 · `exchange_calendars>=4.5,<5.0` (new dep) · pyarrow (existing) · pandas (existing) · SQLAlchemy 2.0 + asyncpg + Alembic · Postgres 16 · Prometheus (hand-rolled `services/observability/metrics`) · structlog · pytest-asyncio · TDD via superpowers.

---

## Approach Comparison

> Persisted from Phase 3.1b → 3.1c. Source of truth: `docs/research/2026-05-07-coverage-granularity-spike.md` (binding research artifact, on this branch as commit `278b239`) + the prior brainstorming conversation's council verdict. The spike's "Verdict" section ratifies Scope B; the "Scope B prerequisites" table enumerates the constraints that won the vote.

### Chosen Default

**Scope B — day-precise refactor of `compute_coverage` with parquet-footer cache + alerting + capture-before-change snapshot.** Replaces month-granularity scan with trading-day-precise scan; caches footer metadata in a new `parquet_partition_index` Postgres table; emits `coverage_gap_detected` Prometheus metric on non-empty `missing_ranges`; routes through existing `AlertingService`. Six prereqs (4 Contrarian + 2 Hawk) pinned in the spike are honored by the task list above, with two **scoped deviations** documented in the cross-check table under Implementation Notes: (a) the metric label set is `{symbol, asset_class}` only — `asset_subclass` is deferred (see Out of scope) because subclass values don't yet exist in the registry; (b) the alert fires for every non-empty `missing_ranges` regardless of an `is_production` flag — the registry has no such flag today, so production-vs-staging filtering is delegated to the alert-rule layer.

### Best Credible Alternative

**Scope A — minimal trailing-edge tolerance fix only.** Keep month-granularity scan; just relax `_apply_trailing_edge_tolerance` so it stops false-flagging the latest in-flight month. Preserves the current `set[(year, month)]` data model. ~1-day patch.

### Null option (rejected explicitly by the spike)

**Do nothing — ship as-is.** Backtests would continue to run silently on partial-month data when production paths emit sub-month parquet files; no observability surfaces this until a strategy P&L diverges from expectation in a code review.

### Scoring (5 axes from `rules/workflow.md` Approach-Comparison Protocol)

| Axis                      | Scope B (chosen)                                                                                                                                                                   | Scope A                                                                                                                                                                | Do nothing                                                                                                                       |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Complexity**            | HIGH — new table, new service, calendar dep, full coverage.py rewrite, ~12 tasks                                                                                                   | LOW — one helper function tweak, ~1 task                                                                                                                               | NONE                                                                                                                             |
| **Blast Radius**          | MEDIUM — touches `coverage.py`, `inventory.py`, `parquet_store.py`, `api/symbol_onboarding.py`, `orchestrator.py`, `trading_metrics.py` + new migration; preserves public schemas  | LOW — single helper                                                                                                                                                    | NONE                                                                                                                             |
| **Reversibility**         | MEDIUM — Alembic migration has clean `downgrade`; capture-before-change snapshot enables surgical post-deploy diff; per-symbol explainability                                      | EASY                                                                                                                                                                   | EASY                                                                                                                             |
| **Time to Validate**      | MEDIUM — 2-3 days incl. plan-review + code-review loops + pre/post snapshot diff                                                                                                   | FAST — existing tests cover the helper                                                                                                                                 | INSTANT                                                                                                                          |
| **User/Correctness Risk** | LOW — intra-month gaps now surface in inventory + alerts; backtests can no longer silently consume partial-month data; capture-before-change diff explains every newly-flagged gap | **HIGH** — spike confirms production paths (sub-month onboarding, provider partial returns, CLI spot fixes) DO emit partial-month files; Scope A leaves them invisible | **HIGH** — same silent-partial-data risk as Scope A; additionally, the per-range Repair UI Pablo built in PR #48 stays vestigial |

### Cheapest Falsifying Test (already ran during the spike)

> "If production paths cannot emit partial-month parquet files by construction, Scope B is over-engineering and Scope A suffices."

The spike examined `parquet_store.write_bars`, `symbol_onboarding.py:onboard`, `data_ingestion.py`, `cli_symbols.py`, and `cli.py:msai ingest`. Every entry path was confirmed to accept arbitrary `(start, end)` windows and to write only the rows actually returned by the provider. The falsifying test FAILED — partial-month files ARE producible. Scope A is therefore insufficient; Scope B is mandatory.

## Contrarian Verdict

**Gate result:** **COUNCIL** (Phase 3.1c).

The 3.1c gate dispatched the Engineering Council in auto-trigger mode (`/council` skill). Five advisors weighed in; the chairman synthesis (recorded in the brainstorming session that wrote the spike) ratified **Scope B with constraints**. The Contrarian (Codex persona) and the Hawk both objected to the "Scope B as proposed without prereqs" baseline; their objections were converted to six binding prereqs (4 Contrarian + 2 Hawk) that became preconditions on Scope B winning the verdict. Five of six are honored in full; prereq #5 ships with two **scoped deviations** (no `asset_subclass` label, no `is_production` gating — both delegated to follow-up work because the underlying schema fields don't exist yet). See the "Contrarian + Hawk prereq cross-check" table under Implementation Notes for the full matrix. The Pragmatist's CONDITIONAL ("Scope A is sufficient if production paths cannot produce partial-month files") was tested by the spike and rejected on evidence — see Cheapest Falsifying Test above. Final chairman recommendation: ship Scope B, prereqs binding, capture-before-change is non-negotiable.

---

## File Structure

### New files

| Path                                                                              | Responsibility                                                                                                                                                     |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `backend/src/msai/services/trading_calendar.py`                                   | Asset-class → exchange map + cached `trading_days(start, end, asset_class)` returning `set[date]`. Wraps `exchange_calendars`.                                     |
| `backend/src/msai/services/symbol_onboarding/partition_index.py`                  | `PartitionIndexService` — reads parquet footer metadata, caches `(min_ts, max_ts, row_count, file_mtime, file_size)` in Postgres, refreshes lazily on stat-change. |
| `backend/src/msai/models/parquet_partition_index.py`                              | SQLAlchemy 2.0 model for the cache table.                                                                                                                          |
| `backend/alembic/versions/aa00b11c22d3_parquet_partition_index.py`                | Migration to create `parquet_partition_index` table.                                                                                                               |
| `backend/scripts/build_partition_index.py`                                        | One-time backfill: walks `{DATA_ROOT}/parquet/`, indexes every file. Idempotent.                                                                                   |
| `scripts/snapshot_inventory.py`                                                   | Capture-before-change: dumps `/api/v1/symbols/inventory` to a JSON fixture. Runs against current dev DB BEFORE Task 1. Sibling diff post-deploy.                   |
| `backend/tests/unit/services/test_trading_calendar.py`                            | Unit tests for calendar wrapper (NYSE holidays, CME holidays, fall-back to bdate_range for unknown asset classes).                                                 |
| `backend/tests/unit/services/symbol_onboarding/test_partition_index.py`           | Unit tests for footer-reader + cache-invalidation logic.                                                                                                           |
| `backend/tests/integration/services/symbol_onboarding/test_partition_index_db.py` | Integration tests: real Postgres + real Parquet files in `tmp_path`.                                                                                               |
| `tests/e2e/use-cases/market-data/uc7-repair-sub-month-gap.md`                     | DRAFT use case (graduates after Phase 5.4 passes): user repairs an intra-month gap that Scope A would have missed.                                                 |

### Modified files

| Path                                                                        | Change                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/pyproject.toml`                                                    | Add `exchange_calendars>=4.5,<5.0` to `dependencies`.                                                                                                                                                                                                                                                                           |
| `backend/src/msai/services/symbol_onboarding/coverage.py`                   | Full rewrite of `_scan_filesystem`, `_apply_trailing_edge_tolerance`, `_collapse_missing`, `_run_to_date_range`, `_derive_covered_range`. `CoverageReport` shape unchanged. New module-level constant `_TRAILING_EDGE_TOLERANCE_TRADING_DAYS = 7`.                                                                              |
| `backend/src/msai/services/symbol_onboarding/inventory.py:is_trailing_only` | Day-aligned cutoff (≤ 7 trading days from `today`) instead of "missing range starts ≥ previous-month-1st".                                                                                                                                                                                                                      |
| `backend/src/msai/services/parquet_store.py:write_bars`                     | After each successful `atomic_write_parquet`, invoke an injected sync `partition_index_refresh` callback. Writer never owns a session or crosses event loops (P1 Codex iteration 2 fix); caller binds session+loop via `make_refresh_callback`.                                                                                 |
| `backend/src/msai/services/observability/trading_metrics.py`                | Register `COVERAGE_GAP_DETECTED` counter.                                                                                                                                                                                                                                                                                       |
| `backend/src/msai/api/symbol_onboarding.py`                                 | Pass `partition_index` into `compute_coverage` at the readiness + inventory endpoints (~lines 687, 742). Pass `asset_class` into `is_trailing_only` (~lines 779 + readiness branch). Public response schema unchanged.                                                                                                          |
| `backend/src/msai/services/symbol_onboarding/orchestrator.py:188`           | Pass `partition_index` (constructed from worker's session) into `compute_coverage`.                                                                                                                                                                                                                                             |
| `backend/src/msai/services/data_ingestion.py`                               | (1) Construct `ParquetStore` with `partition_index_refresh=make_refresh_callback(database_url=settings.database_url)`. (2) Replace every `self.parquet_store.write_bars(...)` call inside `async def` methods with `await asyncio.to_thread(self.parquet_store.write_bars, ...)` — required by the writer's sync-only contract. |
| `backend/tests/unit/services/symbol_onboarding/test_coverage.py`            | Replace `_touch` (empty parquet) with a `_write_partition(path, year, month, days)` helper that produces real partition files; rewrite all four existing tests + add ~10 new day-precise cases.                                                                                                                                 |
| `backend/tests/unit/services/symbol_onboarding/test_inventory.py`           | Update fixtures so `missing_ranges` use sub-month dates; add tests for day-aligned trailing-edge tolerance.                                                                                                                                                                                                                     |
| `backend/tests/integration/api/test_inventory_endpoint.py`                  | Update fixtures (parquet now requires real bars); add intra-month-gap assertion. Update happens IN Task 6d's commit, NOT a follow-up commit, to honor "never commit with failing tests" (P2-3 plan-review fix).                                                                                                                 |
| `backend/src/msai/services/symbol_onboarding/coverage.py` (alerting hook)   | After `compute_coverage` builds the report, if `missing_ranges` is non-empty, call `alerting_service.send_alert(...)` and `COVERAGE_GAP_DETECTED.inc(symbol=..., asset_class=...)`.                                                                                                                                             |

### Files NOT touched

- `backend/src/msai/services/backtests/auto_heal.py` — currently submits the _full_ requested range, not per-gap. Its self-healing semantics survive: a sub-month gap will trigger a sub-month re-fetch from the provider. Verified during spike (line 84-200).
- `frontend/src/components/market-data/row-drawer.tsx` and `frontend/src/lib/hooks/use-symbol-mutations.ts` — already render `missing_ranges` as a list with per-range Repair buttons (`data-testid="repair-{start}-{end}"`); no change needed. Day-precise dates render correctly today.

---

## Tasks

### Task 0: Capture-before-change inventory snapshot

**Files:**

- Create: `scripts/snapshot_inventory.py`
- Create: `tests/fixtures/coverage-pre-scope-b.json` (committed, even though usually gitignored — pre-baseline reference)

This task **MUST run before any code change** per Contrarian prereq #4. It produces a JSON snapshot of the current month-granularity coverage output so that after Scope B lands we can diff `coverage-pre-scope-b.json` vs `coverage-post-scope-b.json` and explain every newly-flagged gap per symbol.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Snapshot the current /api/v1/symbols/inventory output for diffing
post-Scope B. Capture-before-change per Contrarian prereq #4.

Usage:
    python scripts/snapshot_inventory.py \
        --base-url http://localhost:8800 \
        --api-key "$MSAI_API_KEY" \
        --window 2024-01-01:2025-12-31 \
        --output tests/fixtures/coverage-pre-scope-b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8800")
    p.add_argument("--api-key", required=True, help="MSAI_API_KEY")
    p.add_argument(
        "--window",
        required=True,
        help="ISO date pair separated by ':' (e.g. 2024-01-01:2025-12-31)",
    )
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--asset-class",
        default=None,
        help="Optional asset_class filter (equity, futures, fx, ...)",
    )
    args = p.parse_args()

    start, end = args.window.split(":", 1)
    params: dict[str, str] = {"start": start, "end": end}
    if args.asset_class:
        params["asset_class"] = args.asset_class

    headers = {"X-API-Key": args.api_key}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"{args.base_url}/api/v1/symbols/inventory",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        rows = resp.json()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"window": args.window, "rows": rows}, indent=2, sort_keys=True))
    print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/snapshot_inventory.py
```

- [ ] **Step 3: Run it against the live dev stack**

Bring the stack up first if it's not running:

```bash
docker compose -f docker-compose.dev.yml up -d
sleep 5
curl -sf http://localhost:8800/health
```

Then capture:

```bash
python scripts/snapshot_inventory.py \
  --base-url http://localhost:8800 \
  --api-key "$MSAI_API_KEY" \
  --window 2024-01-01:2025-12-31 \
  --output tests/fixtures/coverage-pre-scope-b.json
```

Expected: `wrote N rows to tests/fixtures/coverage-pre-scope-b.json` on stderr; file contains a JSON object with `window` + `rows` keys; `rows` is a list of inventory rows.

If `N == 0`: there are no registered symbols in the dev DB. Onboard at least one symbol via the UI's "Add symbol" dialog or `POST /api/v1/symbols/onboard` and re-run, otherwise the diff will be vacuous.

- [ ] **Step 4: Sanity-check the snapshot**

Open `tests/fixtures/coverage-pre-scope-b.json`. Confirm at least one row has `coverage_status` set (`"full"` or `"gapped"` or `"none"`) and a non-null `covered_range`. If every row shows `coverage_status: null`, the `start`/`end` query params didn't reach the endpoint — re-run.

- [ ] **Step 5: Commit**

The fixture is intentionally checked in despite the usual `.gitignore` of `tests/fixtures/` (if present). Use `git add -f` if needed.

```bash
git add -f tests/fixtures/coverage-pre-scope-b.json
git add scripts/snapshot_inventory.py
git commit -m "feat(coverage): capture-before-change baseline snapshot

Snapshot of the current month-granularity /api/v1/symbols/inventory
output BEFORE the day-precise refactor lands. Sibling
coverage-post-scope-b.json will be diffed after Phase 5 to explain
every newly-flagged gap per symbol (Contrarian prereq #4)."
```

---

### Task 1: Add `exchange_calendars` dep + `trading_calendar.py` module

**Files:**

- Modify: `backend/pyproject.toml` — add `exchange_calendars` to `dependencies`
- Create: `backend/src/msai/services/trading_calendar.py`
- Create: `backend/tests/unit/services/test_trading_calendar.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/services/test_trading_calendar.py`:

```python
from __future__ import annotations

from datetime import date

import pytest

from msai.services.trading_calendar import (
    asset_class_to_exchange,
    trading_days,
)


def test_nyse_excludes_weekends() -> None:
    # 2025-07-04 (Fri) is Independence Day; 2025-07-05 + 06 are weekend.
    days = trading_days(date(2025, 7, 1), date(2025, 7, 7), asset_class="equity")
    assert date(2025, 7, 1) in days  # Tue
    assert date(2025, 7, 2) in days  # Wed
    assert date(2025, 7, 3) in days  # Thu
    assert date(2025, 7, 4) not in days  # holiday
    assert date(2025, 7, 5) not in days  # weekend
    assert date(2025, 7, 6) not in days  # weekend
    assert date(2025, 7, 7) in days  # Mon


def test_cme_for_futures() -> None:
    # CME (Globex) trades through some bank holidays that NYSE closes
    # for (e.g. MLK is open on Globex with reduced hours). Pick a
    # holiday CMES *does* close for — Christmas 2025-12-25 (Thu) —
    # so we can assert the wiring routes through the CMES calendar
    # (not the weekday-only fallback, which would include Thursday).
    days = trading_days(date(2025, 12, 23), date(2025, 12, 26), asset_class="futures")
    assert date(2025, 12, 23) in days  # Tue
    assert date(2025, 12, 24) in days  # Wed (CMES early-close, but session exists)
    assert date(2025, 12, 25) not in days  # Christmas — CMES closed
    assert date(2025, 12, 26) in days  # Fri


def test_unknown_asset_class_falls_back_to_bdate_range() -> None:
    # crypto: trades 24/7 in reality, but our parquet partition convention
    # is weekday-only; fall-back to bdate_range avoids requiring a calendar.
    days = trading_days(date(2025, 7, 5), date(2025, 7, 7), asset_class="crypto")
    assert date(2025, 7, 5) not in days
    assert date(2025, 7, 6) not in days
    assert date(2025, 7, 7) in days


def test_asset_class_to_exchange_map_is_explicit() -> None:
    # Ingest-taxonomy keys (the canonical input — produced by
    # normalize_asset_class_for_ingest):
    assert asset_class_to_exchange("stocks") == "XNYS"
    assert asset_class_to_exchange("options") == "XNYS"
    assert asset_class_to_exchange("forex") == "XNYS"  # FX OTC 24/5 — NYSE proxy
    assert asset_class_to_exchange("futures") == "CMES"
    assert asset_class_to_exchange("crypto") is None
    # Registry-taxonomy aliases (defensive for tests / ad-hoc scripts):
    assert asset_class_to_exchange("equity") == "XNYS"
    assert asset_class_to_exchange("option") == "XNYS"
    assert asset_class_to_exchange("fx") == "XNYS"


def test_normalize_then_map_for_fx_routes_to_nyse() -> None:
    """End-to-end: a registry-side ``"fx"`` flows through
    ``normalize_asset_class_for_ingest`` → ``"forex"`` → ``XNYS``.
    The mapping must hold for the post-normalize string the API endpoint
    actually passes into ``compute_coverage``."""
    from msai.services.symbol_onboarding import normalize_asset_class_for_ingest

    ingest = normalize_asset_class_for_ingest("fx")
    assert ingest == "forex"
    assert asset_class_to_exchange(ingest) == "XNYS"


def test_trading_days_inclusive_of_both_endpoints() -> None:
    days = trading_days(date(2025, 7, 1), date(2025, 7, 1), asset_class="equity")
    assert days == {date(2025, 7, 1)}


def test_trading_days_empty_when_start_after_end() -> None:
    days = trading_days(date(2025, 7, 5), date(2025, 7, 1), asset_class="equity")
    assert days == set()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/services/test_trading_calendar.py -v
```

Expected: `ModuleNotFoundError: No module named 'msai.services.trading_calendar'`.

- [ ] **Step 3: Add the dep**

Edit `backend/pyproject.toml`. Find the `dependencies` list and add (alphabetically near `exception` … or append):

```toml
    "exchange_calendars>=4.5,<5.0",
```

Then sync the venv:

```bash
cd backend && uv sync
```

Expected: `+ exchange-calendars==4.x.x` in the resolution diff.

- [ ] **Step 4: Write the minimal implementation**

`backend/src/msai/services/trading_calendar.py`:

```python
"""Trading-day calendar service.

Maps an MSAI asset class to a single ``exchange_calendars`` key and
returns the set of trading days in a date range. Cached per-process
because ``exchange_calendars`` calendar construction is non-trivial
and trading-day membership is queried inside the day-precise coverage
scan (one call per `(symbol, window)` row on the inventory page).

Asset-class → calendar map:

    equity / stocks / option   → XNYS  (NYSE)
    futures                    → CMES  (CME Globex)
    fx                         → XNYS  (FX is OTC 24/5; NYSE schedule is the
                                        closest match — we don't trade FX
                                        through stock holidays anyway)
    crypto                     → None  (24/7 — fall back to weekday-only via
                                        pandas.bdate_range)
    unknown asset class        → None  (same fall-back; logs a warning)

The module is import-safe: ``exchange_calendars`` is imported lazily
inside the cached factory so a misconfigured environment doesn't break
process startup.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING

import pandas as pd

from msai.core.logging import get_logger

if TYPE_CHECKING:
    import exchange_calendars  # noqa: F401  (typing only)

log = get_logger(__name__)

__all__ = ["asset_class_to_exchange", "trading_days"]


_ASSET_CLASS_TO_EXCHANGE: dict[str, str] = {
    # Ingest-taxonomy keys (what compute_coverage actually receives —
    # callers normalize via normalize_asset_class_for_ingest before
    # invoking the scan; see services/nautilus/security_master/types.py
    # REGISTRY_TO_INGEST_ASSET_CLASS):
    "stocks": "XNYS",
    "options": "XNYS",
    "forex": "XNYS",  # FX is OTC 24/5; NYSE schedule is the closest match
    "futures": "CMES",
    # Registry-taxonomy keys — accepted defensively for callers that
    # bypass the normalizer (tests, ad-hoc scripts):
    "equity": "XNYS",
    "option": "XNYS",
    "fx": "XNYS",
}


def asset_class_to_exchange(asset_class: str) -> str | None:
    """Return the ``exchange_calendars`` key for an asset class, or
    ``None`` for asset classes without a recognized exchange calendar
    (crypto, unknown). The caller falls back to weekday-only filtering
    via ``pandas.bdate_range`` for ``None``."""
    return _ASSET_CLASS_TO_EXCHANGE.get(asset_class.lower())


@lru_cache(maxsize=8)
def _calendar(exchange_key: str) -> object:
    """Cached calendar instance. Lazy import of ``exchange_calendars``
    so missing-dep doesn't break process startup."""
    import exchange_calendars as ec

    return ec.get_calendar(exchange_key)


def trading_days(start: date, end: date, *, asset_class: str) -> set[date]:
    """Return the set of trading days (inclusive of both endpoints) for
    ``asset_class``'s calendar. Falls back to weekday-only filter when
    no exchange is mapped (crypto, unknown).

    Empty set when ``start > end``.
    """
    if start > end:
        return set()

    exchange_key = asset_class_to_exchange(asset_class)
    if exchange_key is None:
        # Weekday-only fall-back. ``bdate_range`` excludes Sat/Sun.
        idx = pd.bdate_range(start=start, end=end)
        return {ts.date() for ts in idx}

    cal = _calendar(exchange_key)
    sessions = cal.sessions_in_range(
        pd.Timestamp(start),
        pd.Timestamp(end),
    )
    return {ts.date() for ts in sessions}
```

- [ ] **Step 5: Run tests**

```bash
cd backend && uv run pytest tests/unit/services/test_trading_calendar.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Lint + types**

```bash
cd backend && uv run ruff check src/msai/services/trading_calendar.py tests/unit/services/test_trading_calendar.py
cd backend && uv run mypy src/msai/services/trading_calendar.py --strict
```

Expected: clean. If mypy complains about the `_calendar` return type, narrow to `Any` (it's a third-party type without stubs):

```python
@lru_cache(maxsize=8)
def _calendar(exchange_key: str) -> "Any":
    ...
```

with `from typing import TYPE_CHECKING, Any` updated.

- [ ] **Step 7: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/msai/services/trading_calendar.py backend/tests/unit/services/test_trading_calendar.py
git commit -m "feat(coverage): trading_calendar service wraps exchange_calendars

Asset-class → exchange map (NYSE for equity/option/fx, CMES for futures,
weekday-only fallback for crypto/unknown). lru_cache'd calendar
construction. Used by day-precise coverage scan to skip holidays."
```

---

### Task 2: `parquet_partition_index` table — model + migration

**Files:**

- Create: `backend/src/msai/models/parquet_partition_index.py`
- Create: `backend/alembic/versions/aa00b11c22d3_parquet_partition_index.py`

- [ ] **Step 1: Write the SQLAlchemy model**

`backend/src/msai/models/parquet_partition_index.py`:

```python
"""Partition-level metadata cache for Parquet files in the bar store.

Rows are keyed by ``(asset_class, symbol, year, month)``. Cached
fields are: footer ``min_ts`` / ``max_ts`` (the actual data window
inside the partition file), ``row_count``, and the file's POSIX
``mtime`` + ``size`` for cache-invalidation. ``compute_coverage``
reads this table instead of opening every parquet footer on every
inventory request.

Cache invariants (Hawk prereq #6):
    1. ``ParquetStore.write_bars`` calls ``refresh_for_partition``
       AFTER each successful atomic write.
    2. ``PartitionIndexService.get`` re-reads the footer if either
       ``file_mtime`` or ``file_size`` no longer matches the on-disk
       file (defends against out-of-band file replacement).
    3. The one-time backfill script
       ``scripts/build_partition_index.py`` populates the table from
       a clean filesystem walk for every existing partition.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy Mapped[] resolves at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base

if TYPE_CHECKING:
    pass


class ParquetPartitionIndex(Base):
    __tablename__ = "parquet_partition_index"

    __table_args__ = (
        CheckConstraint("month >= 1 AND month <= 12", name="ck_partition_index_month_range"),
        CheckConstraint("row_count >= 0", name="ck_partition_index_row_count_nonneg"),
        CheckConstraint("file_size >= 0", name="ck_partition_index_file_size_nonneg"),
        CheckConstraint("max_ts >= min_ts", name="ck_partition_index_ts_order"),
    )

    asset_class: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)

    min_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_mtime: Mapped[float] = mapped_column(Float, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)

    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 2: Register the model in the alembic env**

Inspect `backend/alembic/env.py` to see how new models are picked up. If it auto-imports via `msai.models`, add the import in `backend/src/msai/models/__init__.py`:

```bash
grep -n "import\|from" backend/src/msai/models/__init__.py | head
```

If the file is empty or only contains `from msai.models.base import Base`, append:

```python
from msai.models.parquet_partition_index import ParquetPartitionIndex  # noqa: F401
```

If `models/__init__.py` doesn't import each model individually (some projects autoload), skip this step — the alembic env will pick up `Base.metadata` directly.

- [ ] **Step 3: Generate the migration scaffold**

```bash
cd backend && uv run alembic revision --autogenerate -m "parquet partition index"
```

This creates a new file in `backend/alembic/versions/`. **DO NOT** keep the auto-generated filename — rename it to the convention used in this repo (12-char ID prefix):

```bash
cd backend/alembic/versions
mv $(ls -1t *parquet_partition*.py | head -1) aa00b11c22d3_parquet_partition_index.py
```

- [ ] **Step 4: Replace the migration body**

Open `backend/alembic/versions/aa00b11c22d3_parquet_partition_index.py` and replace its contents with:

```python
"""Add parquet_partition_index — footer-metadata cache for day-precise coverage.

Revision ID: aa00b11c22d3
Revises: 1e2d728f1b32
Create Date: 2026-05-07 00:00:00.000000

Cache table for Parquet partition footer metadata (min_ts, max_ts,
row_count, file_mtime, file_size). Read by ``compute_coverage`` so
the day-precise scan does not open every parquet file on every
inventory request. Refreshed by ``ParquetStore.write_bars`` and the
one-time ``scripts/build_partition_index.py`` backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "aa00b11c22d3"
down_revision: str = "1e2d728f1b32"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "parquet_partition_index",
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("min_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.Float(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "asset_class",
            "symbol",
            "year",
            "month",
            name="pk_parquet_partition_index",
        ),
        sa.CheckConstraint(
            "month >= 1 AND month <= 12",
            name="ck_partition_index_month_range",
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name="ck_partition_index_row_count_nonneg",
        ),
        sa.CheckConstraint(
            "file_size >= 0",
            name="ck_partition_index_file_size_nonneg",
        ),
        sa.CheckConstraint(
            "max_ts >= min_ts",
            name="ck_partition_index_ts_order",
        ),
    )
    op.create_index(
        "ix_partition_index_symbol",
        "parquet_partition_index",
        ["symbol", "asset_class"],
    )


def downgrade() -> None:
    op.drop_index("ix_partition_index_symbol", table_name="parquet_partition_index")
    op.drop_table("parquet_partition_index")
```

- [ ] **Step 5: Apply the migration**

```bash
cd backend && uv run alembic upgrade head
```

Expected: `INFO  [alembic.runtime.migration] Running upgrade 1e2d728f1b32 -> aa00b11c22d3, parquet partition index`.

Verify the table exists by connecting to the dev DB:

```bash
docker compose -f docker-compose.dev.yml exec -T postgres psql -U msai -d msai -c "\d parquet_partition_index"
```

Expected: a column listing matching the schema above.

- [ ] **Step 6: Verify model importability**

```bash
cd backend && uv run python -c "from msai.models.parquet_partition_index import ParquetPartitionIndex; print(ParquetPartitionIndex.__tablename__)"
```

Expected: `parquet_partition_index`.

- [ ] **Step 7: Commit**

```bash
git add backend/src/msai/models/parquet_partition_index.py backend/src/msai/models/__init__.py backend/alembic/versions/aa00b11c22d3_parquet_partition_index.py
git commit -m "feat(coverage): parquet_partition_index table + migration

Footer-metadata cache for day-precise coverage. Keyed by
(asset_class, symbol, year, month). Stores min_ts/max_ts/row_count
plus file_mtime/file_size for cache invalidation."
```

---

### Task 3: `PartitionIndexService` — footer reader + cache logic

**Files:**

- Create: `backend/src/msai/services/symbol_onboarding/partition_index.py`
- Create: `backend/tests/unit/services/symbol_onboarding/test_partition_index.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/services/symbol_onboarding/test_partition_index.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.symbol_onboarding.partition_index import (
    PartitionFooter,
    read_parquet_footer,
)


def _write_parquet(path: Path, timestamps: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * len(timestamps),
            "high": [1.1] * len(timestamps),
            "low": [0.9] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [100] * len(timestamps),
        }
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)


def test_read_footer_returns_min_max_and_row_count(tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    timestamps = [
        datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc),
        datetime(2024, 1, 30, 21, 0, tzinfo=timezone.utc),
    ]
    _write_parquet(path, timestamps)

    footer = read_parquet_footer(path)

    assert footer is not None
    assert footer.min_ts == timestamps[0]
    assert footer.max_ts == timestamps[-1]
    assert footer.row_count == 3
    assert footer.file_size > 0
    assert footer.file_mtime > 0


def test_read_footer_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert read_parquet_footer(tmp_path / "nope.parquet") is None


def test_read_footer_returns_none_when_no_timestamp_column(tmp_path: Path) -> None:
    path = tmp_path / "broken.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"foo": [1, 2, 3]})
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)

    assert read_parquet_footer(path) is None


def test_read_footer_handles_naive_timestamps(tmp_path: Path) -> None:
    # Some legacy parquet files may have naive (no-tz) timestamps. We treat
    # them as UTC for indexing — coverage scan is day-resolution so the
    # tz interpretation only matters for late-evening boundaries.
    path = tmp_path / "naive.parquet"
    naive = [datetime(2024, 1, 2, 14, 30), datetime(2024, 1, 30, 21, 0)]
    _write_parquet(path, naive)

    footer = read_parquet_footer(path)

    assert footer is not None
    assert footer.min_ts.date() == naive[0].date()
    assert footer.max_ts.date() == naive[1].date()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_partition_index.py -v
```

Expected: `ModuleNotFoundError: No module named 'msai.services.symbol_onboarding.partition_index'`.

- [ ] **Step 3: Implement the footer reader (Step 3a)**

Create `backend/src/msai/services/symbol_onboarding/partition_index.py` with **only** the footer-reader piece for now. The DB-cache piece comes in Step 4 (after these unit tests pass).

```python
"""Parquet partition-footer reader + DB-cache service.

Two layers:

* :func:`read_parquet_footer` — pure filesystem read. Opens the parquet
  footer (no row read), pulls min/max of the ``timestamp`` column from
  the per-column statistics, returns a :class:`PartitionFooter` plus
  the file's mtime/size for cache invalidation.

* :class:`PartitionIndexService` — DB-cache layer (Step 4 below).
  Reads from ``parquet_partition_index``; refreshes lazily on
  mtime/size mismatch; exposes ``get_for_symbol(asset_class, symbol)``
  used by day-precise ``compute_coverage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from msai.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["PartitionFooter", "read_parquet_footer"]


@dataclass(frozen=True, slots=True)
class PartitionFooter:
    min_ts: datetime
    max_ts: datetime
    row_count: int
    file_mtime: float
    file_size: int


def read_parquet_footer(path: Path) -> PartitionFooter | None:
    """Return footer metadata for a parquet file, or ``None`` if the
    file is missing, unreadable, or lacks a ``timestamp`` column.

    Reads only the parquet footer (no row data) via
    ``ParquetFile.metadata`` + per-column statistics. This stays
    sub-millisecond even for multi-million-row files.
    """
    if not path.is_file():
        return None

    try:
        stat = path.stat()
        pf = pq.ParquetFile(path)
        schema = pf.schema_arrow
        ts_idx = next(
            (i for i, name in enumerate(schema.names) if name == "timestamp"),
            None,
        )
        if ts_idx is None:
            log.warning("parquet_footer_no_timestamp_column", path=str(path))
            return None

        meta = pf.metadata
        if meta.num_rows == 0:
            return None

        # Aggregate min/max across row groups.
        min_ts: datetime | None = None
        max_ts: datetime | None = None
        for rg_idx in range(meta.num_row_groups):
            stats = meta.row_group(rg_idx).column(ts_idx).statistics
            if stats is None or not stats.has_min_max:
                continue
            rg_min = _coerce_datetime(stats.min)
            rg_max = _coerce_datetime(stats.max)
            if min_ts is None or rg_min < min_ts:
                min_ts = rg_min
            if max_ts is None or rg_max > max_ts:
                max_ts = rg_max

        if min_ts is None or max_ts is None:
            log.warning("parquet_footer_no_stats", path=str(path))
            return None

        return PartitionFooter(
            min_ts=min_ts,
            max_ts=max_ts,
            row_count=int(meta.num_rows),
            file_mtime=stat.st_mtime,
            file_size=stat.st_size,
        )
    except (OSError, pa.ArrowInvalid) as exc:  # pragma: no cover — defensive
        log.warning("parquet_footer_read_failed", path=str(path), error=str(exc))
        return None


def _coerce_datetime(value: object) -> datetime:
    """Coerce a parquet stats value (datetime, pd.Timestamp, int ns)
    to a tz-aware UTC ``datetime``. Naive values are interpreted as
    UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # pyarrow may surface int64 nanoseconds for timestamp[ns]
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1e9, tz=timezone.utc)
    raise TypeError(f"unsupported parquet stats value type: {type(value)!r}")
```

- [ ] **Step 4: Run tests**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_partition_index.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Add DB-cache service tests**

Append to `backend/tests/unit/services/symbol_onboarding/test_partition_index.py`:

```python
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker

from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
)


@pytest.mark.asyncio
async def test_service_get_returns_cached_row_when_mtime_size_match(
    tmp_path: Path,
) -> None:
    # Build a real parquet file the service can stat.
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=timezone.utc)])
    stat = path.stat()

    cached = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        max_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        row_count=1,
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )

    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=cached)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=path,
    )

    assert row == cached
    db.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_service_get_re_reads_footer_when_mtime_changed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=timezone.utc)])

    stale = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        max_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        row_count=1,
        file_mtime=0.0,  # stale
        file_size=0,
        file_path=str(path),
    )

    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=stale)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=path,
    )

    assert row is not None
    assert row.file_mtime != 0.0
    db.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_service_get_returns_none_when_file_missing_and_no_cache(
    tmp_path: Path,
) -> None:
    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=tmp_path / "missing.parquet",
    )

    assert row is None
    db.upsert.assert_not_called()
```

- [ ] **Step 6: Run tests, see them fail**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_partition_index.py -v
```

Expected: 4 pass, 3 fail (`ImportError: cannot import name 'PartitionIndexService'`).

- [ ] **Step 7: Implement `PartitionIndexService` and `PartitionRow`**

Append to `backend/src/msai/services/symbol_onboarding/partition_index.py`:

```python
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PartitionRow:
    """In-memory representation of a ``parquet_partition_index`` row."""

    asset_class: str
    symbol: str
    year: int
    month: int
    min_ts: datetime
    max_ts: datetime
    row_count: int
    file_mtime: float
    file_size: int
    file_path: str


class PartitionIndexGatewayProto(Protocol):
    """Narrow gateway the service depends on. Real implementation lives
    in :mod:`msai.services.symbol_onboarding.partition_index_db`; tests
    pass an :class:`unittest.mock.AsyncMock`. Keeps the service file
    free of SQLAlchemy boilerplate so it stays small."""

    async def fetch_one(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
    ) -> PartitionRow | None: ...

    async def fetch_many(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]: ...

    async def upsert(self, row: PartitionRow) -> None: ...


class PartitionIndexService:
    """Reads + writes the ``parquet_partition_index`` cache.

    Read path:
        1. ``fetch_one`` from cache.
        2. If file missing → return ``None``.
        3. If no cache row → read footer, upsert, return.
        4. If cached ``(mtime, size)`` matches on-disk file → return cached.
        5. Else (file mutated) → re-read footer, upsert, return.

    Write path:
        ``refresh_for_partition`` is called by ``ParquetStore.write_bars``
        unconditionally after each successful atomic write.
    """

    def __init__(self, *, db_gateway: PartitionIndexGatewayProto) -> None:
        self._db = db_gateway

    async def get(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        cached = await self._db.fetch_one(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
        )

        if not path.is_file():
            return None

        stat = path.stat()
        if (
            cached is not None
            and cached.file_mtime == stat.st_mtime
            and cached.file_size == stat.st_size
        ):
            return cached

        return await self._refresh(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            path=path,
        )

    async def get_for_symbol(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]:
        """All cached rows for a symbol, sorted ``(year, month)`` ascending.
        Used by ``compute_coverage`` to assemble the full covered-day set
        in one DB round-trip."""
        rows = await self._db.fetch_many(asset_class=asset_class, symbol=symbol)
        return sorted(rows, key=lambda r: (r.year, r.month))

    async def refresh_for_partition(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        """Force a footer re-read + upsert. Called by ``ParquetStore``
        after each successful write."""
        return await self._refresh(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            path=path,
        )

    async def _refresh(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        footer = read_parquet_footer(path)
        if footer is None:
            return None
        row = PartitionRow(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            min_ts=footer.min_ts,
            max_ts=footer.max_ts,
            row_count=footer.row_count,
            file_mtime=footer.file_mtime,
            file_size=footer.file_size,
            file_path=str(path),
        )
        await self._db.upsert(row)
        return row
```

Update `__all__`:

```python
__all__ = [
    "CacheRefreshMisuseError",  # added in Task 4 Step 6 — caller-contract violation class
    "PartitionFooter",
    "PartitionIndexGatewayProto",
    "PartitionIndexService",
    "PartitionRow",
    "make_refresh_callback",  # added in Task 4 Step 6
    "read_parquet_footer",
]
```

- [ ] **Step 8: Run tests**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_partition_index.py -v
```

Expected: 7 passed.

- [ ] **Step 9: Add the SQLAlchemy gateway implementation**

Create `backend/src/msai/services/symbol_onboarding/partition_index_db.py`:

```python
"""SQLAlchemy implementation of :class:`PartitionIndexGatewayProto`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.models.parquet_partition_index import ParquetPartitionIndex
from msai.services.symbol_onboarding.partition_index import PartitionRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PartitionIndexGateway:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def fetch_one(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
    ) -> PartitionRow | None:
        stmt = select(ParquetPartitionIndex).where(
            ParquetPartitionIndex.asset_class == asset_class,
            ParquetPartitionIndex.symbol == symbol,
            ParquetPartitionIndex.year == year,
            ParquetPartitionIndex.month == month,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _to_dataclass(row)

    async def fetch_many(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]:
        stmt = select(ParquetPartitionIndex).where(
            ParquetPartitionIndex.asset_class == asset_class,
            ParquetPartitionIndex.symbol == symbol,
        )
        return [_to_dataclass(r) for r in (await self._session.execute(stmt)).scalars()]

    async def upsert(self, row: PartitionRow) -> None:
        stmt = pg_insert(ParquetPartitionIndex).values(
            asset_class=row.asset_class,
            symbol=row.symbol,
            year=row.year,
            month=row.month,
            min_ts=row.min_ts,
            max_ts=row.max_ts,
            row_count=row.row_count,
            file_mtime=row.file_mtime,
            file_size=row.file_size,
            file_path=row.file_path,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                ParquetPartitionIndex.asset_class,
                ParquetPartitionIndex.symbol,
                ParquetPartitionIndex.year,
                ParquetPartitionIndex.month,
            ],
            set_={
                "min_ts": stmt.excluded.min_ts,
                "max_ts": stmt.excluded.max_ts,
                "row_count": stmt.excluded.row_count,
                "file_mtime": stmt.excluded.file_mtime,
                "file_size": stmt.excluded.file_size,
                "file_path": stmt.excluded.file_path,
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()


def _to_dataclass(row: ParquetPartitionIndex) -> PartitionRow:
    return PartitionRow(
        asset_class=row.asset_class,
        symbol=row.symbol,
        year=row.year,
        month=row.month,
        min_ts=row.min_ts,
        max_ts=row.max_ts,
        row_count=row.row_count,
        file_mtime=row.file_mtime,
        file_size=row.file_size,
        file_path=row.file_path,
    )
```

- [ ] **Step 10: Add an integration test against real Postgres**

Create `backend/tests/integration/services/symbol_onboarding/test_partition_index_db.py`:

```python
"""Integration tests: real Postgres + real Parquet via tmp_path.

Uses the ``db_session`` fixture from ``conftest.py`` (real Postgres,
auto-rollback per test).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
)
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway


def _write_parquet(path: Path, timestamps: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"timestamp": timestamps, "close": [1.0] * len(timestamps)})
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)


@pytest.mark.asyncio
async def test_upsert_then_fetch_round_trip(db_session, tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=timezone.utc)])
    stat = path.stat()

    gw = PartitionIndexGateway(session=db_session)
    row = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        max_ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
        row_count=1,
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )
    await gw.upsert(row)

    fetched = await gw.fetch_one(
        asset_class="stocks", symbol="AAPL", year=2024, month=1
    )
    assert fetched == row


@pytest.mark.asyncio
async def test_service_full_path_with_real_db(db_session, tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(
        path,
        [
            datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 30, 21, 0, tzinfo=timezone.utc),
        ],
    )

    gw = PartitionIndexGateway(session=db_session)
    svc = PartitionIndexService(db_gateway=gw)

    row = await svc.get(
        asset_class="stocks", symbol="AAPL", year=2024, month=1, path=path
    )

    assert row is not None
    assert row.row_count == 2
    assert row.min_ts.date().day == 2
    assert row.max_ts.date().day == 30
```

- [ ] **Step 11: Run integration tests**

```bash
cd backend && uv run pytest tests/integration/services/symbol_onboarding/test_partition_index_db.py -v
```

Expected: 2 passed.

If `db_session` fixture is named differently in this repo, inspect existing integration tests:

```bash
grep -rn "@pytest.fixture\|db_session\|async_session" backend/tests/integration/conftest.py 2>/dev/null
```

…and rename to whatever the project uses (probably `session` or `async_session`).

- [ ] **Step 12: Lint + types**

```bash
cd backend && uv run ruff check src/msai/services/symbol_onboarding/partition_index.py src/msai/services/symbol_onboarding/partition_index_db.py
cd backend && uv run mypy src/msai/services/symbol_onboarding/partition_index.py src/msai/services/symbol_onboarding/partition_index_db.py --strict
```

Expected: clean.

- [ ] **Step 13: Commit**

```bash
git add backend/src/msai/services/symbol_onboarding/partition_index.py backend/src/msai/services/symbol_onboarding/partition_index_db.py backend/tests/unit/services/symbol_onboarding/test_partition_index.py backend/tests/integration/services/symbol_onboarding/test_partition_index_db.py
git commit -m "feat(coverage): PartitionIndexService — pyarrow footer reader + DB cache

PartitionFooter holds (min_ts, max_ts, row_count, file_mtime, file_size).
read_parquet_footer is pure-FS; PartitionIndexService is the Postgres-cache
read path with mtime/size invalidation. PartitionIndexGateway is the
SQLAlchemy implementation (separate file to keep service free of
asyncpg specifics)."
```

---

### Task 4: Wire `ParquetStore.write_bars` to refresh the index

**Files:**

- Modify: `backend/src/msai/services/parquet_store.py:35-85`
- Modify (or extend): `backend/tests/unit/services/test_parquet_store.py` (or wherever the existing parquet_store tests live — find with `grep -l "ParquetStore" backend/tests/`)

- [ ] **Step 1: Locate existing parquet_store tests**

```bash
grep -rln "ParquetStore\|write_bars" backend/tests/ | head
```

Note the file. The test file we extend is whichever one currently covers `write_bars` — probably `backend/tests/unit/services/test_parquet_store.py` or `backend/tests/integration/services/test_parquet_store.py`.

- [ ] **Step 2: Write the failing test**

> **Architecture note (P1 Codex iteration 2 fix):** the writer takes a `partition_index_refresh: Callable[[str, str, int, int, Path], None] | None` — a sync callback. The writer never owns a DB session, never spawns a thread, never crosses event loops. The CALLER (ingest worker / CLI / API) builds the callback in its own context and binds it to its session and event loop. SQLAlchemy's async engine is therefore only ever used from the loop it was created on (per the [SQLAlchemy multi-loop note](https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)). The earlier session_factory-+-thread approach (iteration 1 P1-3 fix) shared the global async engine across event loops; Codex iteration 2 flagged that as P1.

In the located test file, add:

```python
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from msai.services.parquet_store import ParquetStore


def test_write_bars_invokes_partition_index_refresh(tmp_path: Path) -> None:
    """The writer calls the supplied partition_index_refresh callback
    once per (year, month) group with the right partition coordinates."""
    captured: list[tuple[str, str, int, int, Path]] = []

    def refresh(asset_class: str, symbol: str, year: int, month: int, path: Path) -> None:
        captured.append((asset_class, symbol, year, month, path))

    store = ParquetStore(
        data_root=str(tmp_path),
        partition_index_refresh=refresh,
    )
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    datetime(2024, 1, 2, tzinfo=timezone.utc),
                    datetime(2024, 1, 30, tzinfo=timezone.utc),
                ]
            ),
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [100, 100],
        }
    )
    checksum = store.write_bars("stocks", "AAPL", df)
    assert checksum

    assert len(captured) == 1
    asset_class, symbol, year, month, path = captured[0]
    assert (asset_class, symbol, year, month) == ("stocks", "AAPL", 2024, 1)
    assert path.name == "01.parquet"


def test_write_bars_swallows_runtime_callback_errors(tmp_path: Path, caplog) -> None:
    """A genuine runtime callback exception (DB down, transient
    network) is logged at WARN with traceback (P3-2 fix) and swallowed —
    the parquet file is the source of truth and the next
    compute_coverage call will refresh the cache from the footer."""
    import logging

    def boom(*_args: object, **_kw: object) -> None:
        raise ConnectionError("DB unavailable")

    store = ParquetStore(data_root=str(tmp_path), partition_index_refresh=boom)
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([datetime(2024, 1, 2, tzinfo=timezone.utc)]),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [100],
        }
    )
    with caplog.at_level(logging.WARNING):
        checksum = store.write_bars("stocks", "AAPL", df)  # MUST NOT raise
    assert checksum
    assert any("partition_index_refresh_failed" in r.message for r in caplog.records)


def test_write_bars_propagates_misuse_error(tmp_path: Path) -> None:
    """A CacheRefreshMisuseError signals a caller-contract violation
    (write_bars invoked from async code without to_thread). The writer
    MUST let it propagate so the engineer sees the misuse instead of
    a silently-stale cache. P2 Codex iteration 4 fix."""
    from msai.services.symbol_onboarding.partition_index import (
        CacheRefreshMisuseError,
    )

    def misuse(*_args: object, **_kw: object) -> None:
        raise CacheRefreshMisuseError("test caller violated the contract")

    store = ParquetStore(data_root=str(tmp_path), partition_index_refresh=misuse)
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([datetime(2024, 1, 2, tzinfo=timezone.utc)]),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [100],
        }
    )
    with pytest.raises(CacheRefreshMisuseError, match="test caller violated"):
        store.write_bars("stocks", "AAPL", df)


def test_write_bars_works_without_callback(tmp_path: Path) -> None:
    """CLI seed scripts and ad-hoc tooling don't have DB wiring; backfill
    (Task 5) catches up. ``partition_index_refresh=None`` must be a valid
    construction."""
    store = ParquetStore(data_root=str(tmp_path), partition_index_refresh=None)
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([datetime(2024, 1, 2, tzinfo=timezone.utc)]),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [100],
        }
    )
    checksum = store.write_bars("stocks", "AAPL", df)
    assert checksum
```

- [ ] **Step 3: Run tests, see them fail**

```bash
cd backend && uv run pytest tests/unit/services/test_parquet_store.py -v -k "partition_index or callback"
```

Expected: `TypeError: ParquetStore() got an unexpected keyword argument 'partition_index_refresh'`.

- [ ] **Step 4: Implement the wiring**

Modify `backend/src/msai/services/parquet_store.py`:

```python
# At top of file, add to imports:
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    PartitionIndexRefresh = Callable[[str, str, int, int, Path], None]
```

Replace the `__init__` and amend `write_bars`:

```python
    def __init__(
        self,
        data_root: str,
        *,
        partition_index_refresh: "PartitionIndexRefresh | None" = None,
    ) -> None:
        """Construct the store.

        ``partition_index_refresh`` is a SYNC callable invoked once per
        ``(year, month)`` group after each atomic write succeeds. The
        callback owns its own DB session + event-loop binding (see
        Step 6 for the canonical builder). The writer itself is event-
        loop-agnostic and never opens a session, so we do not cross
        SQLAlchemy's "one async engine per event loop" rule
        (https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops).

        Pass ``None`` for environments without DB access (CLI seed
        scripts, ad-hoc tooling). The one-time backfill (Task 5)
        catches up the cache.
        """
        self.data_root = Path(data_root)
        self._refresh_callback = partition_index_refresh

    def write_bars(self, asset_class: str, symbol: str, df: pd.DataFrame) -> str:
        # ... (existing body unchanged through `last_checksum = atomic_write_parquet(...)`)

        for (year, month), group in df.groupby([df["timestamp"].dt.year, df["timestamp"].dt.month]):
            target = self._bar_path(asset_class, symbol, int(year), int(month))

            if target.exists():
                existing = pd.read_parquet(target)
                merged = pd.concat([existing, group], ignore_index=True)
                group = dedup_bars(merged, key_columns=dedup_key)

            table = pa.Table.from_pandas(group, preserve_index=False)
            last_checksum = atomic_write_parquet(table, target)
            log.info(
                "wrote_bars",
                symbol=symbol,
                year=year,
                month=month,
                rows=len(group),
            )

            if self._refresh_callback is not None:
                try:
                    self._refresh_callback(
                        asset_class, symbol, int(year), int(month), target
                    )
                except CacheRefreshMisuseError:
                    # Caller-contract violation (write_bars invoked from
                    # async without to_thread wrap). NOT a runtime
                    # data-layer problem — propagate so the engineer
                    # sees the misuse instead of a silent stale cache.
                    # P2 Codex iteration 4 fix.
                    raise
                except Exception:
                    # Genuine cache-update failure (DB down, transient
                    # network error). Best-effort: the parquet file is
                    # the source of truth and the next compute_coverage
                    # call will refresh from the footer. P3-2 plan-review
                    # fix: include traceback for diagnosability.
                    log.warning(
                        "partition_index_refresh_failed",
                        symbol=symbol,
                        asset_class=asset_class,
                        year=int(year),
                        month=int(month),
                        exc_info=True,
                    )

        return last_checksum
```

> **`CacheRefreshMisuseError` class** — declared at module scope in
> `backend/src/msai/services/symbol_onboarding/partition_index.py` and
> imported by `parquet_store.py`. Definition:
>
> ```python
> class CacheRefreshMisuseError(RuntimeError):
>     """Raised by make_refresh_callback's callback when invoked from
>     inside a running event loop. Signals a caller-contract violation
>     (write_bars must be wrapped in asyncio.to_thread when called from
>     async code). Distinct from runtime cache-update failures so the
>     writer can let it propagate."""
> ```
>
> Add `"CacheRefreshMisuseError"` to the module's `__all__`. The
> callback's defensive guard (Task 4 Step 6) raises this class instead
> of a bare `RuntimeError`.

- [ ] **Step 5: Run tests**

```bash
cd backend && uv run pytest tests/unit/services/test_parquet_store.py -v
```

Expected: all tests pass. (Existing tests should still pass because `partition_index_refresh=None` is the default.)

- [ ] **Step 6: Build the canonical refresh callback at each construction site**

Add a small helper in `backend/src/msai/services/symbol_onboarding/partition_index.py` (alongside `PartitionIndexService`):

```python
def make_refresh_callback(*, database_url: str) -> "PartitionIndexRefresh":
    """Build a SYNC callback for ``ParquetStore(partition_index_refresh=...)``.

    The callback **always** opens a fresh ``AsyncEngine`` (with
    ``NullPool``), a fresh ``AsyncSession``, runs the refresh to
    completion via ``asyncio.run`` on whatever thread is calling, and
    disposes the engine before returning. The engine and session
    therefore never cross event loops, and we never share the global
    ``async_session_factory`` engine with a fresh loop — addressing the
    SQLAlchemy "one async engine per loop" rule (P1 Codex iteration 3
    fix; see https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops).

    **Caller contract:** ``write_bars`` is sync. It MUST be called from
    one of:

    - A truly sync context (CLI script's main, a worker job's sync
      callback). The refresh callback's ``asyncio.run`` simply runs to
      completion.
    - A sync context obtained from async code via ``asyncio.to_thread``
      (the ingest worker pattern). The refresh callback's ``asyncio.run``
      runs in the worker thread and blocks only that thread; the
      caller's loop stays free.

    **Calling ``write_bars`` directly from an async function** (without
    ``asyncio.to_thread``) is unsupported: the refresh callback would
    raise ``RuntimeError: This event loop is already running``. Wrap
    every async call site in ``await asyncio.to_thread(...)``. The plan
    explicitly updates ``services/data_ingestion.py:ingest_historical``
    to do this — see Step 7 below.

    Per-call cost is one engine create + dispose (~ms-level for an
    asyncpg connection); negligible against the parquet write itself
    and acceptable on the cache-update path (not the hot read path).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway

    async def _do_refresh(
        asset_class: str, symbol: str, year: int, month: int, path: Path
    ) -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            session_maker = async_sessionmaker(engine, class_=AsyncSession)
            async with session_maker() as session:
                gateway = PartitionIndexGateway(session=session)
                service = PartitionIndexService(db_gateway=gateway)
                await service.refresh_for_partition(
                    asset_class=asset_class,
                    symbol=symbol,
                    year=year,
                    month=month,
                    path=path,
                )
        finally:
            await engine.dispose()

    def _callback(
        asset_class: str, symbol: str, year: int, month: int, path: Path
    ) -> None:
        # Defensive guard: refuse to run inside an already-running loop
        # (would RuntimeError on asyncio.run). Raise a custom class so
        # the writer's outer try/except (Task 4 Step 4) can let
        # contract-violation errors propagate instead of swallowing
        # them as transient failures (P2 Codex iteration 4 fix).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # No loop on this thread — safe to proceed.
        else:
            raise CacheRefreshMisuseError(
                "make_refresh_callback's callback was invoked from "
                "inside a running event loop. write_bars must be "
                "called from a sync context — wrap async call sites "
                "in `await asyncio.to_thread(store.write_bars, ...)`."
            )

        asyncio.run(_do_refresh(asset_class, symbol, year, month, path))

    return _callback
```

- [ ] **Step 7: Wire each `ParquetStore(...)` construction site**

```bash
grep -rn "ParquetStore(" backend/src/msai/ | grep -v "test_"
```

The ingest worker (`backend/src/msai/services/data_ingestion.py`) is the primary write path. The plan touches TWO things at this site:

1. Pass the callback to `ParquetStore`.
2. Wrap the `write_bars` call in `await asyncio.to_thread(...)` — currently `data_ingestion.ingest_historical` calls `self.parquet_store.write_bars(...)` synchronously inside an async method, which would deadlock the refresh callback (P1 Codex iter 3 fix).

Async ingest pattern:

```python
import asyncio
from msai.core.config import settings
from msai.services.parquet_store import ParquetStore
from msai.services.symbol_onboarding.partition_index import make_refresh_callback

class DataIngestionService:
    def __init__(self, ...):
        refresh_cb = make_refresh_callback(database_url=settings.database_url)
        self.parquet_store = ParquetStore(
            data_root=settings.data_root,
            partition_index_refresh=refresh_cb,
        )

    async def ingest_historical(self, ...):
        # ... fetch bars ...
        # Was: written = self.parquet_store.write_bars(asset_class, symbol, df)
        # Now: capture the return value through to_thread so downstream
        # consumers (the bytes-written audit log, the integrity checksum)
        # still see it. P1 Codex iteration 4 fix.
        written = await asyncio.to_thread(
            self.parquet_store.write_bars, asset_class, symbol, df
        )
        # ... use `written` exactly as before ...
```

Sync CLI pattern (no event loop in scope, e.g. `scripts/build_partition_index.py` — though that one uses the index service directly, not write_bars; this pattern is for hypothetical sync writers):

```python
from msai.core.config import settings
from msai.services.parquet_store import ParquetStore
from msai.services.symbol_onboarding.partition_index import make_refresh_callback

refresh_cb = make_refresh_callback(database_url=settings.database_url)
store = ParquetStore(
    data_root=settings.data_root, partition_index_refresh=refresh_cb,
)
store.write_bars(asset_class, symbol, df)
```

The `scripts/seed_market_data.py` synthetic-data script does NOT need a callback — backfill (Task 5) handles its partitions. Pass `partition_index_refresh=None`.

**Verify the to_thread wiring landed.** The check below is AST-level — it catches BOTH footguns: (a) direct sync call `self.parquet_store.write_bars(...)` inside an `async def`, AND (b) `asyncio.to_thread(self.parquet_store.write_bars, ...)` without the leading `await`. P2 Codex iteration 4 + iteration 5 fix.

```bash
# Step A: enumerate write_bars references for human inspection.
rg -n "self\.parquet_store\.write_bars" backend/src/msai/services/data_ingestion.py

# Step B: AST-level enforcement — every reference to
# `self.parquet_store.write_bars` (whether called directly OR passed
# as a callable to to_thread) must be inside `await asyncio.to_thread(...)`.
cd backend && uv run python << 'PY'
import ast, sys

src = open("src/msai/services/data_ingestion.py").read()
tree = ast.parse(src)

# Build a {child_id: parent} map.
parents: dict[int, ast.AST] = {}
for parent in ast.walk(tree):
    for child in ast.iter_child_nodes(parent):
        parents[id(child)] = parent


def is_self_pq_write_bars(node: ast.AST) -> bool:
    """True if node is the AST attribute ``self.parquet_store.write_bars``
    (whether used as a callee or passed as a value)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "write_bars"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parquet_store"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def inside_await_to_thread(node: ast.AST) -> bool:
    """Walk up parents (max 8 hops) looking for an ``ast.Await`` whose
    ``.value`` is an ``asyncio.to_thread(...)`` Call enclosing ``node``."""
    cur = node
    for _ in range(8):
        cur = parents.get(id(cur))
        if cur is None:
            return False
        if (
            isinstance(cur, ast.Call)
            and isinstance(cur.func, ast.Attribute)
            and cur.func.attr == "to_thread"
        ):
            return isinstance(parents.get(id(cur)), ast.Await)
    return False


violations: list[int] = []
for node in ast.walk(tree):
    if is_self_pq_write_bars(node) and not inside_await_to_thread(node):
        violations.append(node.lineno)

if violations:
    print(f"VIOLATIONS at lines {sorted(set(violations))} — "
          "self.parquet_store.write_bars must be inside `await asyncio.to_thread(...)`")
    sys.exit(1)
print("OK — every self.parquet_store.write_bars reference is inside await asyncio.to_thread(...)")
PY
```

Expected: Step A prints one or more lines (existence sanity); Step B prints `OK — every self.parquet_store.write_bars reference is inside await asyncio.to_thread(...)` and exits 0. Any `VIOLATIONS at lines ...` output means an `await` is missing — fix before committing.

- [ ] **Step 8: Commit**

```bash
git add backend/src/msai/services/parquet_store.py backend/src/msai/services/symbol_onboarding/partition_index.py backend/src/msai/services/data_ingestion.py backend/tests/unit/services/test_parquet_store.py
git commit -m "feat(coverage): ParquetStore takes partition_index_refresh callback

Writer is event-loop-agnostic; caller injects a sync callback built by
make_refresh_callback (NullPool engine + asyncio.run per call so we
never share an async engine across loops). data_ingestion's
async write site is wrapped in asyncio.to_thread to satisfy the
caller-must-be-sync contract."
```

---

### Task 5: One-time backfill script `build_partition_index.py`

**Files:**

- Create: `backend/scripts/build_partition_index.py`

- [ ] **Step 1: Write the script**

```python
"""One-time backfill of parquet_partition_index from the on-disk catalog.

Walks ``{DATA_ROOT}/parquet/<asset_class>/<symbol>/<YYYY>/<MM>.parquet``
and upserts a ``parquet_partition_index`` row for every file. Idempotent —
re-running on an already-populated table simply re-affirms each row.

Usage:
    cd backend && uv run python scripts/build_partition_index.py
    cd backend && uv run python scripts/build_partition_index.py --asset-class stocks --symbol AAPL
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
)
from msai.services.symbol_onboarding.partition_index_db import (
    PartitionIndexGateway,
)

log = get_logger(__name__)


async def _run(asset_class_filter: str | None, symbol_filter: str | None) -> int:
    parquet_root = Path(settings.data_root) / "parquet"
    if not parquet_root.is_dir():
        log.warning("parquet_root_missing", path=str(parquet_root))
        return 0

    indexed = 0
    async with async_session_factory() as session:
        gw = PartitionIndexGateway(session=session)
        svc = PartitionIndexService(db_gateway=gw)

        for ac_dir in sorted(parquet_root.iterdir()):
            if not ac_dir.is_dir():
                continue
            if asset_class_filter and ac_dir.name != asset_class_filter:
                continue

            for sym_dir in sorted(ac_dir.iterdir()):
                if not sym_dir.is_dir():
                    continue
                if symbol_filter and sym_dir.name != symbol_filter:
                    continue

                for year_dir in sorted(sym_dir.iterdir()):
                    if not year_dir.is_dir() or not year_dir.name.isdigit():
                        continue
                    year = int(year_dir.name)

                    for parquet_path in sorted(year_dir.glob("*.parquet")):
                        stem = parquet_path.stem
                        if not stem.isdigit():
                            continue
                        month = int(stem)
                        if not (1 <= month <= 12):
                            continue

                        row = await svc.refresh_for_partition(
                            asset_class=ac_dir.name,
                            symbol=sym_dir.name,
                            year=year,
                            month=month,
                            path=parquet_path,
                        )
                        if row is not None:
                            indexed += 1
                            log.info(
                                "indexed_partition",
                                asset_class=ac_dir.name,
                                symbol=sym_dir.name,
                                year=year,
                                month=month,
                                row_count=row.row_count,
                            )

    log.info("backfill_complete", indexed=indexed)
    return indexed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-class", default=None)
    p.add_argument("--symbol", default=None)
    args = p.parse_args()

    indexed = asyncio.run(_run(args.asset_class, args.symbol))
    print(f"Indexed {indexed} partitions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the dev DB**

```bash
docker compose -f docker-compose.dev.yml up -d
cd backend && uv run python scripts/build_partition_index.py
```

Expected: `Indexed N partitions` on stderr; `N` matches the count of `*.parquet` files under `data/parquet/`.

Verify in Postgres:

```bash
docker compose -f docker-compose.dev.yml exec -T postgres psql -U msai -d msai -c "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM parquet_partition_index;"
```

- [ ] **Step 3: Re-run for idempotency check**

```bash
cd backend && uv run python scripts/build_partition_index.py
```

Expected: same `Indexed N`. The DB row count should be unchanged (upsert).

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/build_partition_index.py
git commit -m "feat(coverage): build_partition_index.py one-time backfill

Walks DATA_ROOT/parquet and upserts a parquet_partition_index row for
every existing file. Idempotent. Run once per environment after the
table migration lands; not part of the request critical path."
```

---

### Task 6: Rewrite `compute_coverage` (day-precise)

This task is broken into sub-steps because the rewrite of `coverage.py` touches every internal helper. Each sub-step adds focused tests + the matching slice of implementation, runs green, then commits.

**Files:**

- Modify: `backend/src/msai/services/symbol_onboarding/coverage.py` (full rewrite of internals; public `CoverageReport` shape preserved; `compute_coverage` adds an optional `partition_index` kwarg)
- Modify: `backend/tests/unit/services/symbol_onboarding/test_coverage.py` (full rewrite — old tests assume empty parquet files; new tests need real bars)
- Modify: `backend/src/msai/api/symbol_onboarding.py:687,742` — pass `partition_index` (constructed from the request's DB session)
- Modify: `backend/src/msai/services/symbol_onboarding/orchestrator.py:188` — pass `partition_index` (constructed from the worker's session)

#### Task 6a: Test fixture helper + first day-precise test

- [ ] **Step 1: Replace test fixture helper**

Open `backend/tests/unit/services/symbol_onboarding/test_coverage.py`. Replace the `_touch` helper with a real-bar writer:

```python
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.symbol_onboarding.coverage import compute_coverage
from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
)


def _write_partition(
    base: Path,
    *,
    year: int,
    month: int,
    days: list[int],
) -> Path:
    """Write a parquet file with one bar per requested day-of-month at 16:00 UTC."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{month:02d}.parquet"
    timestamps = [
        datetime(year, month, d, 16, 0, tzinfo=timezone.utc) for d in days
    ]
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * len(days),
            "high": [1.1] * len(days),
            "low": [0.9] * len(days),
            "close": [1.0] * len(days),
            "volume": [100] * len(days),
        }
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


def _make_index_with_rows(rows: list[PartitionRow]) -> PartitionIndexService:
    """Build a PartitionIndexService backed by an in-memory mock gateway
    pre-populated with the given rows. The mock obeys the gateway protocol."""
    by_key = {(r.asset_class, r.symbol, r.year, r.month): r for r in rows}

    gw = AsyncMock()

    async def _fetch_one(*, asset_class, symbol, year, month):
        return by_key.get((asset_class, symbol, year, month))

    async def _fetch_many(*, asset_class, symbol):
        return [r for (ac, s, _, _), r in by_key.items() if ac == asset_class and s == symbol]

    async def _upsert(row):
        by_key[(row.asset_class, row.symbol, row.year, row.month)] = row

    gw.fetch_one.side_effect = _fetch_one
    gw.fetch_many.side_effect = _fetch_many
    gw.upsert.side_effect = _upsert
    return PartitionIndexService(db_gateway=gw)


def _seed_row(
    path: Path,
    *,
    asset_class: str,
    symbol: str,
    year: int,
    month: int,
    days: list[int],
) -> PartitionRow:
    """Build a PartitionRow that mirrors what `read_parquet_footer` would
    return for the file at ``path`` written by `_write_partition` with
    the same days. Used to seed the mock cache so `compute_coverage`
    sees the same view production code would after Task 4's writer-
    side refresh has run."""
    stat = path.stat()
    timestamps = [
        datetime(year, month, d, 16, 0, tzinfo=timezone.utc) for d in days
    ]
    return PartitionRow(
        asset_class=asset_class,
        symbol=symbol,
        year=year,
        month=month,
        min_ts=min(timestamps),
        max_ts=max(timestamps),
        row_count=len(days),
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )
```

- [ ] **Step 2: Write the first day-precise test (intra-month gap)**

Append to the same test file:

```python
@pytest.mark.asyncio
async def test_intra_month_gap_is_detected(tmp_path: Path) -> None:
    """User onboards 2024-01-15 → 2024-04-30. The writer creates Jan/Feb/Mar/Apr
    parquet files but Jan only contains days 15-31. The old month-granularity
    scan would call this 'full' (all four month files exist). Day-precise
    must report 2024-01-02 through 2024-01-12 as missing trading days."""
    base = tmp_path / "parquet" / "stocks" / "AAPL"
    # Jan: only days 15-31
    jan_days = list(range(15, 32))
    feb_days = list(range(1, 29))
    mar_days = list(range(1, 32))
    apr_days = list(range(1, 31))
    p_jan = _write_partition(base / "2024", year=2024, month=1, days=jan_days)
    p_feb = _write_partition(base / "2024", year=2024, month=2, days=feb_days)
    p_mar = _write_partition(base / "2024", year=2024, month=3, days=mar_days)
    p_apr = _write_partition(base / "2024", year=2024, month=4, days=apr_days)

    index = _make_index_with_rows(
        [
            _seed_row(p_jan, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=jan_days),
            _seed_row(p_feb, asset_class="stocks", symbol="AAPL", year=2024, month=2, days=feb_days),
            _seed_row(p_mar, asset_class="stocks", symbol="AAPL", year=2024, month=3, days=mar_days),
            _seed_row(p_apr, asset_class="stocks", symbol="AAPL", year=2024, month=4, days=apr_days),
        ]
    )

    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 4, 30),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 6, 1),  # well past the window — no trailing tolerance fires
    )

    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
    miss_start, miss_end = report.missing_ranges[0]
    # 2024-01-01 is New Year's holiday; 2024-01-02 (Tue) is the first trading day.
    # Jan 12 (Fri) is the last trading day before our partition begins on Jan 15.
    assert miss_start == date(2024, 1, 2)
    assert miss_end == date(2024, 1, 12)
```

- [ ] **Step 3: Run test, see it fail**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py::test_intra_month_gap_is_detected -v
```

Expected: failure (current `compute_coverage` reads month-presence only, returns `status="full"`).

- [ ] **Step 4: Rewrite `coverage.py` core**

Replace the entirety of `backend/src/msai/services/symbol_onboarding/coverage.py` with:

```python
"""Day-precise coverage scan for parquet partitions.

For a given ``(asset_class, symbol, [start, end])`` window, build the
set of trading days the asset class's exchange calendar expects, then
subtract the set of days actually present in the cached parquet
partition footers (read from ``parquet_partition_index`` via
:class:`PartitionIndexService`). Remaining trading days are
"missing"; contiguous runs collapse into ``missing_ranges``.

The public shape of :class:`CoverageReport` is preserved so call sites
in ``api/symbol_onboarding.py`` and the onboarding orchestrator
compile unchanged. The semantics shift from "month is missing" to
"day is missing". Every call site already handled the existing
``missing_ranges: list[tuple[date, date]]``; the only difference is
that those tuples can now have intra-month spans.

Trailing-edge tolerance is now day-aligned: the most recent
``_TRAILING_EDGE_TOLERANCE_TRADING_DAYS`` trading days are forgiven so
a healthy ingest pipeline running ~T+1 doesn't trigger a stale-only
gap on every refresh. The constant is tuned to 7 trading days
(roughly two business weeks worth of slack) — long enough to cover
weekend + holiday + provider-scheduling latency, short enough that a
genuine multi-week regression still surfaces as ``stale``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from msai.services.trading_calendar import trading_days

if TYPE_CHECKING:
    from msai.services.symbol_onboarding.partition_index import (
        PartitionIndexService,
        PartitionRow,
    )

log = structlog.get_logger(__name__)

__all__ = ["CoverageReport", "compute_coverage"]

_TRAILING_EDGE_TOLERANCE_TRADING_DAYS = 7


@dataclass(frozen=True, slots=True)
class CoverageReport:
    status: Literal["full", "gapped", "none"]
    covered_range: str | None
    missing_ranges: list[tuple[date, date]]


async def compute_coverage(
    *,
    asset_class: str,
    symbol: str,
    start: date,
    end: date,
    data_root: Path,
    partition_index: "PartitionIndexService",
    today: date | None = None,
) -> CoverageReport:
    today = today or date.today()
    expected_days = trading_days(start, end, asset_class=asset_class)
    if not expected_days:
        # Window entirely outside the calendar (e.g. start > end, or
        # crypto with no trading days under our convention) — vacuously
        # full. Note: Sat→Sun returns "full" here, NOT "none" — semantics
        # change from pre-Scope-B (no months → "none"). The intent is "no
        # trading days were expected, so nothing is missing."
        return CoverageReport(status="full", covered_range=None, missing_ranges=[])

    rows = await partition_index.get_for_symbol(
        asset_class=asset_class, symbol=symbol
    )
    covered_days = _covered_days_from_rows(
        rows,
        start=start,
        end=end,
        asset_class=asset_class,
    )

    if not covered_days:
        # Nothing in the partition index for this symbol that overlaps
        # [start, end]. Backfill (Task 5) populates the index from
        # existing files; the writer (Task 4) refreshes on every
        # successful write. If the index is empty here, no parquet data
        # is available — surface that as ``status="none"`` and a single
        # window-spanning missing range so the auto-heal flow sees a
        # cleanly-shaped repair request.
        return CoverageReport(
            status="none",
            covered_range=None,
            missing_ranges=[(start, end)],
        )

    missing = sorted(expected_days - covered_days)
    if missing:
        missing = _apply_trailing_edge_tolerance(
            missing,
            today=today,
            asset_class=asset_class,
        )

    if not missing:
        return CoverageReport(
            status="full",
            covered_range=_derive_covered_range(covered_days),
            missing_ranges=[],
        )

    return CoverageReport(
        status="gapped",
        covered_range=_derive_covered_range(covered_days),
        missing_ranges=_collapse_missing(missing),
    )


def _covered_days_from_rows(
    rows: list["PartitionRow"],
    *,
    start: date,
    end: date,
    asset_class: str,
) -> set[date]:
    """Covered days = trading-day intersection of every partition's
    ``[min_ts.date(), max_ts.date()]`` window.

    P1-1 fix from plan-review iteration 1: the previous implementation
    walked calendar days and admitted weekends + holidays as "covered"
    whenever a partition spanned them, which silently cancelled gap
    detection for any partition with data on both the first and last
    trading day of the month. Trading-day intersection is the only
    correct definition of "this partition covers day D".

    A partition with internal gaps (provider returned days 1-5 + 15-31
    in the same January file) is the residual blind spot — see the
    "Residual: internal-partition gaps" note in Implementation Notes.

    Returns the intersection with the requested ``[start, end]`` window
    so callers see only the days they asked about.
    """
    covered: set[date] = set()
    for row in rows:
        partition_first = row.min_ts.date()
        partition_last = row.max_ts.date()
        # Clip to the requested window before asking the calendar.
        clipped_first = max(partition_first, start)
        clipped_last = min(partition_last, end)
        if clipped_first > clipped_last:
            continue
        # ``trading_days`` is vectorized via exchange_calendars'
        # ``sessions_in_range``; far cheaper than a per-day Python loop
        # even for multi-year partitions.
        covered |= trading_days(
            clipped_first, clipped_last, asset_class=asset_class
        )
    return covered


def _apply_trailing_edge_tolerance(
    missing: list[date],
    *,
    today: date,
    asset_class: str,
) -> list[date]:
    """Drop the most recent ``_TRAILING_EDGE_TOLERANCE_TRADING_DAYS``
    trading days from ``missing``.

    We compute the set of "tolerated" trading days as the last N
    trading days strictly before ``today`` (today itself is also
    tolerated since the day's bars don't usually land until after
    close). For a typical Mon-Fri market this is ``today`` plus the
    seven prior trading days.
    """
    from datetime import timedelta

    # Look back ~3 calendar weeks to harvest 7 trading days reliably,
    # even across two long-weekend holidays.
    lookback_start = today - timedelta(days=21)
    recent = sorted(trading_days(lookback_start, today, asset_class=asset_class))
    tolerated = set(recent[-_TRAILING_EDGE_TOLERANCE_TRADING_DAYS:])
    tolerated.add(today)
    return [d for d in missing if d not in tolerated]


def _collapse_missing(missing: list[date]) -> list[tuple[date, date]]:
    """Collapse a sorted list of dates into contiguous ranges. Two
    dates are contiguous when the second is the next *trading* day
    after the first — but for the public ``missing_ranges`` shape the
    range endpoints are calendar dates, and consumers (Repair UI,
    backtest auto-heal) submit a calendar [start, end] window to
    re-fetch. So contiguity here is calendar-day adjacency on the
    sorted-trading-days list. Practically: if two trading days are
    less than 5 calendar days apart with no other trading days in
    between, treat as one run."""
    from datetime import timedelta

    if not missing:
        return []
    ranges: list[tuple[date, date]] = []
    run_start = missing[0]
    prev = run_start
    for current in missing[1:]:
        if (current - prev).days <= 5:
            prev = current
            continue
        ranges.append((run_start, prev))
        run_start = current
        prev = current
    ranges.append((run_start, prev))
    return ranges


def _derive_covered_range(covered: set[date]) -> str:
    """Render covered-days set as ``"YYYY-MM-DD → YYYY-MM-DD"`` using
    the min and max — even if there are internal gaps. The covered_range
    field is a human-readable hint, not a contract."""
    if not covered:
        return ""
    first = min(covered)
    last = max(covered)
    return f"{first.isoformat()} → {last.isoformat()}"
```

- [ ] **Step 5: Run the test**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py::test_intra_month_gap_is_detected -v
```

Expected: PASS.

**Note on cache state in tests** (P2-4 plan-review fix): tests use the `_make_index_with_rows([])` factory which provides an empty in-memory cache backing. Production deployments populate the cache via the one-time backfill (Task 5) and the writer-side refresh (Task 4); coverage scans NEVER walk the filesystem to lazy-prime, because that would add ~4800 `stat()` calls per inventory page load (80 symbols × 60 partitions). For the test to pass with empty backing, the test fixture must seed `_make_index_with_rows([...])` with real `PartitionRow` entries derived from the parquet file the test writes — see Task 6a Step 1's helper for the pattern.

If the test FAILs with `covered_days = set()` and `status = "none"` despite a parquet file on disk: the test's index seeding is missing. Update the test to construct the cache row from the on-disk file before calling `compute_coverage`. Example:

```python
    path = base / "01.parquet"  # already written by _write_partition
    stat = path.stat()
    seed_row = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 15, 16, 0, tzinfo=timezone.utc),
        max_ts=datetime(2024, 1, 31, 16, 0, tzinfo=timezone.utc),
        row_count=17,
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )
    index = _make_index_with_rows([seed_row])
```

The plan's Task 6a test fixture should already do this. If it doesn't, fix the fixture, not `compute_coverage`.

- [ ] **Step 6: Commit**

```bash
git add backend/src/msai/services/symbol_onboarding/coverage.py backend/tests/unit/services/symbol_onboarding/test_coverage.py
git commit -m "feat(coverage): day-precise scan via partition_index + trading_calendar

compute_coverage now derives expected days from the asset class's
exchange calendar (NYSE/CME) and covered days from cached parquet
footer min/max. Intra-month gaps that the month-granularity scan
treated as 'full' are now reported. Public CoverageReport shape
unchanged."
```

#### Task 6b: Trailing-edge tolerance test (day-aligned)

- [ ] **Step 1: Add the test**

Append to `backend/tests/unit/services/symbol_onboarding/test_coverage.py`:

```python
@pytest.mark.asyncio
async def test_trailing_edge_tolerance_forgives_recent_days(tmp_path: Path) -> None:
    """Today is 2024-01-22 (Mon). Coverage exists through Friday 2024-01-12.
    The seven trading days {Jan 16-19, 22} (skipping MLK = Jan 15) are
    inside the trailing-edge window and forgiven; status='full'."""
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    days = list(range(2, 13))
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )
    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 1, 22),
    )

    assert report.status == "full"
    assert report.missing_ranges == []


@pytest.mark.asyncio
async def test_older_gaps_are_NOT_forgiven(tmp_path: Path) -> None:
    """A two-week-old gap is outside the 7-day trailing-edge window and
    surfaces as 'gapped'."""
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    # Day 2 only — leaves 3-12 missing (10 trading days back from 2024-01-22).
    days = [2]
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )
    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 1, 22),
    )

    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
```

- [ ] **Step 2: Run tests**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py -v
```

Expected: 3 passed (the original from Task 6a + 2 new).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/unit/services/symbol_onboarding/test_coverage.py
git commit -m "test(coverage): trailing-edge tolerance is day-aligned"
```

#### Task 6c: Empty / vacuous-window edge cases

- [ ] **Step 1: Add tests**

Append to `backend/tests/unit/services/symbol_onboarding/test_coverage.py`:

```python
@pytest.mark.asyncio
async def test_no_data_returns_status_none(tmp_path: Path) -> None:
    index = _make_index_with_rows([])
    report = await compute_coverage(
        asset_class="stocks",
        symbol="ZZZZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
        partition_index=index,
        today=date(2025, 6, 1),
    )
    assert report.status == "none"
    assert report.covered_range is None
    assert report.missing_ranges == [(date(2024, 1, 1), date(2024, 12, 31))]


@pytest.mark.asyncio
async def test_full_year_coverage_returns_full(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    seed_rows: list[PartitionRow] = []
    for month in range(1, 13):
        days = list(range(1, 32 if month not in (2, 4, 6, 9, 11) else 29 if month == 2 else 31))
        p = _write_partition(base, year=2024, month=month, days=days)
        seed_rows.append(
            _seed_row(p, asset_class="stocks", symbol="SPY", year=2024, month=month, days=days)
        )

    index = _make_index_with_rows(seed_rows)
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
        partition_index=index,
        today=date(2025, 6, 1),
    )
    assert report.status == "full"
    assert report.missing_ranges == []
    assert report.covered_range is not None


@pytest.mark.asyncio
async def test_window_with_no_trading_days_is_full(tmp_path: Path) -> None:
    """A window like Sat→Sun (no trading days) is vacuously full.

    P2-2 plan-review note: this is a SEMANTIC CHANGE from pre-Scope-B
    behavior. The month-granularity scan returned status='none' for any
    no-data window (since no months were present); day-precise returns
    status='full' when ZERO trading days are EXPECTED. Consumers
    relying on status='none' to detect "nothing ingested" for a weekend
    window will get the new vacuous-full result. The design intent:
    "no trading days expected → nothing to be missing → vacuously full."
    """
    index = _make_index_with_rows([])  # Empty cache OK — we never look up rows.
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 6),  # Sat
        end=date(2024, 1, 7),  # Sun
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 6, 1),
    )
    assert report.status == "full"
    assert report.missing_ranges == []
    assert report.covered_range is None  # No real data to summarize.
```

- [ ] **Step 2: Run + commit**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py -v
git add backend/tests/unit/services/symbol_onboarding/test_coverage.py
git commit -m "test(coverage): edge cases — none / full / vacuous window"
```

#### Task 6d: Wire callers

- [ ] **Step 1: Update the API endpoint**

Modify `backend/src/msai/api/symbol_onboarding.py`. Find the imports near the top and add:

```python
from msai.services.symbol_onboarding.partition_index import PartitionIndexService
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway
```

In `readiness_get` (around line 687), replace the `compute_coverage` call:

```python
    report = await compute_coverage(
        asset_class=ingest_asset,
        symbol=symbol,
        start=start,
        end=end,
        data_root=_FsPath(settings.data_root),
        partition_index=PartitionIndexService(
            db_gateway=PartitionIndexGateway(session=db),
        ),
    )
```

Same change at `inventory` line 742:

```python
            report = await compute_coverage(
                asset_class=ingest_asset,
                symbol=item.raw_symbol,
                start=start,
                end=end,
                data_root=_FsPath(settings.data_root),
                partition_index=PartitionIndexService(
                    db_gateway=PartitionIndexGateway(session=db),
                ),
                today=today,
            )
```

- [ ] **Step 2: Update the orchestrator caller**

Modify `backend/src/msai/services/symbol_onboarding/orchestrator.py:188`. Add the import near the top and rebuild the call.

```python
from msai.services.symbol_onboarding.partition_index import PartitionIndexService
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway
```

The orchestrator opens its own session via `db_factory`. Add a session-scoped index construction inside the call:

```python
    # ---- Phase 3: coverage scan (must be 'full' to advance) ----
    await _persist_step(db_factory, run_id, spec.symbol, step=SymbolStepStatus.COVERAGE)
    async with db_factory() as session:
        coverage = await compute_coverage(
            asset_class=ingest_asset,
            symbol=spec.symbol,
            start=spec.start,
            end=spec.end,
            data_root=data_root,
            partition_index=PartitionIndexService(
                db_gateway=PartitionIndexGateway(session=session),
            ),
            today=today,
        )
```

- [ ] **Step 3: Update the integration tests in the SAME commit**

> **P2-3 plan-review fix:** the project's `rules/testing.md` Rule 7 ("never commit with failing tests") forbids leaving `test_inventory_endpoint.py` red across commits. Update the fixtures here, in this commit, alongside the caller wiring.

Run the integration suite to find the breaks:

```bash
cd backend && uv run pytest tests/integration/api/test_inventory_endpoint.py -v
```

For each failing test, replace the empty-parquet `_touch` (or equivalent fixture helper) with `write_partition` from `backend/tests/conftest.py` (the helper added in Task 10 Step 1 — pull that step's helper definition forward into this commit). Each test's setup must:

1. Write at least one parquet partition with real timestamps via the `write_partition` fixture.
2. Pre-seed the `parquet_partition_index` table with a matching row using a sanctioned setup path. Two options:
   - Run `cd backend && uv run python scripts/build_partition_index.py` against the test data root inside the test's setup; OR
   - Construct a `PartitionRow` and call `PartitionIndexGateway(session=db_session).upsert(row)` directly in the test fixture (not the assertion path — ARRANGE only).

Re-run:

```bash
cd backend && uv run pytest tests/integration/api/test_inventory_endpoint.py tests/unit/ -q
```

Expected: green.

If a test surfaces a real bug (not just a fixture gap), DO NOT mark it `@pytest.mark.skip` — fix the bug per the NO BUGS LEFT BEHIND policy.

- [ ] **Step 4: Run the full unit + integration suite for ripple checks**

```bash
cd backend && uv run pytest tests/unit/ tests/integration/ -q
```

Expected: green. If anything else broke (e.g. backtest auto-heal tests that asserted month-aligned `missing_ranges`), fix the assertion in this commit.

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/api/symbol_onboarding.py backend/src/msai/services/symbol_onboarding/orchestrator.py backend/tests/integration/api/test_inventory_endpoint.py backend/tests/conftest.py
git commit -m "refactor(coverage): callers pass PartitionIndexService into compute_coverage

API readiness + inventory endpoints and the symbol-onboarding
orchestrator now pass a session-scoped PartitionIndexService into
compute_coverage. Integration test fixtures updated in the same
commit to seed real parquet partitions + index rows."
```

---

### Task 7: Update `is_trailing_only` to be day-aligned

**Files:**

- Modify: `backend/src/msai/services/symbol_onboarding/inventory.py:31-48`
- Modify: `backend/tests/unit/services/symbol_onboarding/test_inventory.py`

The current `is_trailing_only` checks "missing range starts on or after the previous calendar month's first day". After Scope B, missing ranges can be sub-month, so the heuristic shifts to "missing range starts within the last 7 trading days". This keeps the `stale` ↔ `gapped` semantics that the inventory page exposes today.

- [ ] **Step 1: Write the failing test**

Update `backend/tests/unit/services/symbol_onboarding/test_inventory.py` — find the `TestIsTrailingOnly` class (or add one) and replace its body:

```python
from datetime import timedelta

from msai.services.symbol_onboarding.inventory import is_trailing_only

TODAY = date(2024, 1, 22)  # Mon


class TestIsTrailingOnly:
    def test_trailing_when_single_range_within_7_trading_days(self) -> None:
        # Range starts 2024-01-12 (Fri) — 6 trading days back: 12,16,17,18,19,22 — within 7.
        assert is_trailing_only(
            missing_ranges=[(date(2024, 1, 12), date(2024, 1, 22))],
            today=TODAY,
            asset_class="equity",
        )

    def test_not_trailing_when_range_starts_8_or_more_trading_days_back(self) -> None:
        # Range starts 2024-01-02 (Tue) — 14 trading days back: outside window.
        assert not is_trailing_only(
            missing_ranges=[(date(2024, 1, 2), date(2024, 1, 22))],
            today=TODAY,
            asset_class="equity",
        )

    def test_multiple_ranges_never_count_as_trailing(self) -> None:
        assert not is_trailing_only(
            missing_ranges=[
                (date(2024, 1, 2), date(2024, 1, 5)),
                (date(2024, 1, 18), date(2024, 1, 22)),
            ],
            today=TODAY,
            asset_class="equity",
        )

    def test_empty_ranges_returns_false(self) -> None:
        assert not is_trailing_only(
            missing_ranges=[],
            today=TODAY,
            asset_class="equity",
        )
```

- [ ] **Step 2: Run, see it fail**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_inventory.py::TestIsTrailingOnly -v
```

Expected: failures (current `is_trailing_only` doesn't take `asset_class`).

- [ ] **Step 3: Update `is_trailing_only`**

Replace the body of `is_trailing_only` in `backend/src/msai/services/symbol_onboarding/inventory.py`:

```python
def is_trailing_only(
    *,
    missing_ranges: list[tuple[date, date]],
    today: date,
    asset_class: str = "equity",
) -> bool:
    """True iff there is exactly ONE missing range AND its start sits
    within the last 7 trading days for ``asset_class``'s calendar.

    The 7-day window matches the trailing-edge tolerance baked into
    ``compute_coverage``; together they form the stale ↔ gapped
    boundary the inventory page renders. ``asset_class`` defaults to
    equity for legacy callers that pre-date the day-precise refactor.
    """
    if len(missing_ranges) != 1:
        return False
    from datetime import timedelta

    from msai.services.trading_calendar import trading_days

    range_start, _end = missing_ranges[0]
    lookback_start = today - timedelta(days=21)
    recent = sorted(trading_days(lookback_start, today, asset_class=asset_class))
    cutoff_idx = max(0, len(recent) - 7)
    cutoff_day = recent[cutoff_idx] if recent else today
    return range_start >= cutoff_day
```

- [ ] **Step 4: Update every caller to pass `asset_class`**

```bash
grep -rn "is_trailing_only(" backend/src/ backend/tests/
```

Update each call site. The two production callers are in `backend/src/msai/api/symbol_onboarding.py`:

```python
# Around line 779:
            is_stale=is_trailing_only(
                missing_ranges=missing_ranges_typed,
                today=today,
                asset_class=item.asset_class,  # NEW
            ),
```

And the readiness branch (search for the second occurrence in the file):

```python
            is_stale=is_trailing_only(
                missing_ranges=missing_ranges_typed,
                today=today,
                asset_class=ingest_asset,  # NEW
            ),
```

- [ ] **Step 5: Run inventory tests**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_inventory.py -v
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add backend/src/msai/services/symbol_onboarding/inventory.py backend/src/msai/api/symbol_onboarding.py backend/tests/unit/services/symbol_onboarding/test_inventory.py
git commit -m "refactor(coverage): is_trailing_only uses 7-trading-day window

Mirrors compute_coverage's trailing-edge tolerance so the
stale ↔ gapped boundary is consistent. Adds asset_class kwarg
(default 'equity' for legacy callers)."
```

---

### Task 8: Register Prometheus metric `coverage_gap_detected_total`

**Files:**

- Modify: `backend/src/msai/services/observability/trading_metrics.py`

- [ ] **Step 1: Add the counter declaration**

Open `backend/src/msai/services/observability/trading_metrics.py`. After the existing counter declarations (e.g., `LIVE_INSTRUMENT_RESOLVED_TOTAL`), add:

```python
# Coverage scan outcomes — emitted by compute_coverage when missing_ranges
# is non-empty. Labels:
#   asset_class — the ingest-side asset class string (matches AssetClass enum)
#   asset_subclass — currently the same as asset_class; reserved for future
#                    subdivision (e.g., 'stocks/etf' vs 'stocks/equity')
COVERAGE_GAP_DETECTED = _r.counter(
    "msai_coverage_gap_detected_total",
    "Number of compute_coverage calls that returned non-empty missing_ranges, "
    "labeled by symbol/asset_class. Use for gap-rate dashboards and alert "
    "rules.",
)
```

- [ ] **Step 2: Confirm import works**

```bash
cd backend && uv run python -c "from msai.services.observability.trading_metrics import COVERAGE_GAP_DETECTED; print(COVERAGE_GAP_DETECTED.metric_type)"
```

Expected: `counter`.

- [ ] **Step 3: Confirm it renders in `/metrics`**

Increment it once and render:

```bash
cd backend && uv run python -c "
from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import COVERAGE_GAP_DETECTED
COVERAGE_GAP_DETECTED.inc(symbol='AAPL', asset_class='stocks')
print(get_registry().render())
" | grep coverage_gap_detected
```

Expected: a line like `msai_coverage_gap_detected_total{asset_class="stocks",symbol="AAPL"} 1.0`.

- [ ] **Step 4: Commit**

```bash
git add backend/src/msai/services/observability/trading_metrics.py
git commit -m "feat(observability): register coverage_gap_detected_total counter"
```

---

### Task 9: Wire `compute_coverage` to alerting + metric

**Files:**

- Modify: `backend/src/msai/services/symbol_onboarding/coverage.py`
- Add tests: `backend/tests/unit/services/symbol_onboarding/test_coverage.py`

Hawk's prereq #5: emit `coverage_gap_detected{symbol,asset_class,asset_subclass}` Prometheus metric on every `compute_coverage` call that returns non-empty `missing_ranges`. Route to existing `services/alerting`.

The spike noted "for symbols marked production". MSAI v2 does not currently have an `is_production` flag on instruments. The pragmatic interpretation: emit the metric for **every** non-empty missing-ranges call (label cardinality stays bounded by registered-symbol count, ~80 today, ~500 ceiling). Alert _rules_ can filter by symbol-allowlist later when production / staging cohorts are introduced.

- [ ] **Step 1: Write the failing test**

> **P1-5 plan-review fix:** the hand-rolled `Counter` class in `services/observability/metrics.py` exposes `inc(**labels)`, `labels(**labels)`, and `render()` — but NO `get(**labels)`. Don't reach for `Counter.get(...)`. Use the registry's `render()` output (Prometheus text exposition format) and grep for the labeled metric line. The test below reads the value before and after via render-grep so it works on the actual API.

Append to `backend/tests/unit/services/symbol_onboarding/test_coverage.py`:

```python
import re

from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import COVERAGE_GAP_DETECTED  # noqa: F401 — import side-effect registers the counter


def _read_counter_value(metric_name: str, **labels: str) -> float:
    """Read a labeled counter value from the registry's exposition
    output. Returns 0.0 when the labeled series hasn't been touched
    yet (Prometheus convention)."""
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    pattern = rf"^{re.escape(metric_name)}\{{{re.escape(label_str)}\}}\s+(\S+)\s*$"
    for line in get_registry().render().splitlines():
        m = re.match(pattern, line)
        if m:
            return float(m.group(1))
    return 0.0


@pytest.mark.asyncio
async def test_gapped_emits_metric_and_alert(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    days = [2]  # Jan 2 only — leaves 3-12 missing
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )

    sent_alerts: list[tuple[str, str, str]] = []

    class _StubAlerts:
        def send_alert(self, level: str, title: str, message: str) -> None:
            sent_alerts.append((level, title, message))

    monkeypatch.setattr(
        "msai.services.symbol_onboarding.coverage._get_alerting_service",
        lambda: _StubAlerts(),
    )

    before = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="AAPL",
    )

    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 4, 1),
    )

    assert report.status == "gapped"
    after = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="AAPL",
    )
    assert after >= before + 1
    assert len(sent_alerts) == 1
    level, title, message = sent_alerts[0]
    assert level in ("warning", "info")
    assert "AAPL" in title or "AAPL" in message
```

- [ ] **Step 2: Run, see it fail**

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py::test_gapped_emits_metric_and_alert -v
```

Expected: failure (`AttributeError` on `_get_alerting_service`).

- [ ] **Step 3: Wire alerting + metric in `compute_coverage`**

Modify `backend/src/msai/services/symbol_onboarding/coverage.py`. Add imports at the top:

```python
from msai.services.alerting import alerting_service as _default_alerting_service
from msai.services.observability.trading_metrics import COVERAGE_GAP_DETECTED
```

Add a hookable accessor (so tests can monkeypatch):

```python
def _get_alerting_service():  # noqa: ANN202
    """Indirection to allow tests to substitute an alerting double."""
    return _default_alerting_service
```

In `compute_coverage`, the alert+metric hook fires ONLY on the `status="gapped"` exit. Earlier exits — `expected_days` empty (vacuous full), `covered_days` empty (status="none"), and `missing` empty after tolerance (status="full") — do NOT alert. P2 Codex iteration 2 fix: gate explicitly on the gapped path; do NOT add a second `if not covered_days` block after the alert (that branch is unreachable because the empty-covered_days exit happens earlier in the function — see Task 6 Step 4).

```python
    if not missing:
        return CoverageReport(
            status="full",
            covered_range=_derive_covered_range(covered_days),
            missing_ranges=[],
        )

    # --- Hawk prereq #5: emit metric + alert on the gapped exit ---
    # Reachable only when missing is non-empty (post-tolerance) AND
    # covered_days is non-empty (else we returned status="none" earlier
    # in the function). All four exits of compute_coverage:
    #   1. expected_days empty  → "full" (vacuous), no alert
    #   2. covered_days empty   → "none", no alert
    #   3. missing empty (post-tolerance) → "full", no alert
    #   4. THIS PATH            → "gapped", alert
    COVERAGE_GAP_DETECTED.inc(symbol=symbol, asset_class=asset_class)
    try:
        _get_alerting_service().send_alert(
            level="warning",
            title=f"Coverage gap detected: {asset_class}/{symbol}",
            message=(
                f"compute_coverage returned {len(missing)} missing trading days "
                f"in window {start.isoformat()} → {end.isoformat()}. "
                f"First gap starts {missing[0].isoformat()}."
            ),
        )
    except Exception:  # noqa: BLE001 — alerting must never block coverage
        log.warning(
            "coverage_alert_send_failed",
            symbol=symbol,
            asset_class=asset_class,
            exc_info=True,
        )

    return CoverageReport(
        status="gapped",
        covered_range=_derive_covered_range(covered_days),
        missing_ranges=_collapse_missing(missing),
    )
```

- [ ] **Step 4: Run the test + add a status="none" no-alert assertion**

Append to `backend/tests/unit/services/symbol_onboarding/test_coverage.py`:

```python
@pytest.mark.asyncio
async def test_status_none_does_NOT_emit_metric_or_alert(
    tmp_path: Path, monkeypatch
) -> None:
    """When the partition_index is empty for a symbol (no parquet data
    indexed), compute_coverage returns status='none' with a window-
    spanning missing range — but it MUST NOT increment the
    coverage_gap_detected metric or fire an alert. status='none' is a
    DATA-MISSING signal, not a coverage-gap signal; alert rules will be
    different.
    """
    sent_alerts: list[tuple[str, str, str]] = []

    class _StubAlerts:
        def send_alert(self, level: str, title: str, message: str) -> None:
            sent_alerts.append((level, title, message))

    monkeypatch.setattr(
        "msai.services.symbol_onboarding.coverage._get_alerting_service",
        lambda: _StubAlerts(),
    )

    before = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="ZZZZ",
    )

    index = _make_index_with_rows([])  # empty cache → status="none"
    report = await compute_coverage(
        asset_class="stocks",
        symbol="ZZZZ",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 4, 1),
    )

    assert report.status == "none"
    after = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="ZZZZ",
    )
    assert after == before  # NO metric increment
    assert sent_alerts == []  # NO alert
```

Run:

```bash
cd backend && uv run pytest tests/unit/services/symbol_onboarding/test_coverage.py::test_gapped_emits_metric_and_alert tests/unit/services/symbol_onboarding/test_coverage.py::test_status_none_does_NOT_emit_metric_or_alert -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/symbol_onboarding/coverage.py backend/tests/unit/services/symbol_onboarding/test_coverage.py
git commit -m "feat(coverage): emit coverage_gap_detected metric + alert

Every compute_coverage call that returns non-empty missing_ranges
increments the Prometheus counter and routes through AlertingService.
Alert send failures are logged but never block the coverage scan."
```

---

### Task 10: Update existing inventory + readiness integration tests

**Files:**

- Modify: `backend/tests/integration/api/test_inventory_endpoint.py`

Existing fixtures rely on the old empty-parquet-touch convention. Replace with real-bar writes via the helper from Task 6a (move it to `backend/tests/integration/conftest.py` so both unit and integration tests can share). At minimum:

- [ ] **Step 1: Move `_write_partition` to a shared fixture**

Open `backend/tests/conftest.py` (top-level) and add:

```python
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
from datetime import datetime, timezone


@pytest.fixture
def write_partition():
    """Helper to write a real Parquet partition file under
    ``tmp_path/parquet/<asset>/<symbol>/<YYYY>/<MM>.parquet`` with
    one bar per requested day at 16:00 UTC."""

    def _write(
        data_root: Path,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        days: list[int],
    ) -> Path:
        base = data_root / "parquet" / asset_class / symbol / str(year)
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{month:02d}.parquet"
        timestamps = [
            datetime(year, month, d, 16, 0, tzinfo=timezone.utc) for d in days
        ]
        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.0] * len(days),
                "high": [1.1] * len(days),
                "low": [0.9] * len(days),
                "close": [1.0] * len(days),
                "volume": [100] * len(days),
            }
        )
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
        return path

    return _write
```

- [ ] **Step 2: Update `test_inventory_endpoint.py`**

Identify each fixture / setup that previously used `_touch` or empty-file writes. Replace with calls to `write_partition`. The contract every test needs to assert is: a row's `coverage_status` reflects intra-month gaps.

Run the suite to find the specific failures:

```bash
cd backend && uv run pytest tests/integration/api/test_inventory_endpoint.py -v
```

Update each failing fixture / assertion. The shape of `missing_ranges` is unchanged (`list[{start,end}]`); the _values_ can now be sub-month.

- [ ] **Step 3: Add an intra-month-gap assertion**

Add a new test:

```python
@pytest.mark.asyncio
async def test_inventory_reports_intra_month_gap(
    client, db_session, write_partition, tmp_path, monkeypatch
):
    """When a registered symbol has a partition with mid-month gap, the
    inventory endpoint surfaces a sub-month missing_range."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr("msai.core.config.settings.data_root", str(tmp_path))

    # Write a partition covering only days 15-22 of January 2024.
    write_partition(
        tmp_path,
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        days=list(range(15, 23)),
    )

    # … register AAPL via the documented seed/onboard path (no direct DB inserts) …
    # The exact ARRANGE depends on the existing test's helpers — re-use them.

    resp = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2024-01-01", "end": "2024-01-31"},
        headers={"X-API-Key": "msai-dev-key"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert aapl["coverage_status"] == "gapped"
    assert any(
        mr["start"] == "2024-01-02" and mr["end"] <= "2024-01-12"
        for mr in aapl["missing_ranges"]
    )
```

- [ ] **Step 4: Run + commit**

```bash
cd backend && uv run pytest tests/integration/api/test_inventory_endpoint.py -v
git add backend/tests/conftest.py backend/tests/integration/api/test_inventory_endpoint.py
git commit -m "test(coverage): inventory integration test covers intra-month gap"
```

---

### Task 11: Post-Scope-B snapshot + diff against pre-baseline

**Files:**

- Create: `tests/fixtures/coverage-post-scope-b.json` (committed)
- Create: `docs/plans/2026-05-07-coverage-day-precise-diff-report.md`

Mirrors Task 0. After all unit + integration tests pass, capture the new inventory output from the same dev DB and produce a per-symbol diff explaining every newly-flagged gap.

- [ ] **Step 1: Restart workers + re-run backfill**

```bash
./scripts/restart-workers.sh
cd backend && uv run python scripts/build_partition_index.py
```

Expected: backfill repopulates `parquet_partition_index` with one row per existing parquet file (idempotent — Task 5 confirmed this).

- [ ] **Step 2: Capture the post-snapshot**

```bash
python scripts/snapshot_inventory.py \
  --base-url http://localhost:8800 \
  --api-key "$MSAI_API_KEY" \
  --window 2024-01-01:2025-12-31 \
  --output tests/fixtures/coverage-post-scope-b.json
```

- [ ] **Step 3: Diff and explain every changed row**

```bash
diff <(jq -S . tests/fixtures/coverage-pre-scope-b.json) \
     <(jq -S . tests/fixtures/coverage-post-scope-b.json) | head -200
```

For every row whose `coverage_status` flipped `full → gapped` or whose `missing_ranges` grew: open the corresponding parquet file, read the timestamp column, and confirm the newly-flagged days truly are absent. Document each newly-flagged gap in `docs/plans/2026-05-07-coverage-day-precise-diff-report.md`:

```markdown
# Scope B Coverage Diff Report

| Symbol | Asset class | Window            | Pre status | Post status | Newly-flagged days      | Root cause                                                                  |
| ------ | ----------- | ----------------- | ---------- | ----------- | ----------------------- | --------------------------------------------------------------------------- |
| AAPL   | stocks      | 2024-01 → 2024-12 | full       | gapped      | 2024-07-15 → 2024-07-19 | provider partial-return during ingest 2024-07-20 (see ingest_runs row #...) |
| ...    | ...         | ...               | ...        | ...         | ...                     | ...                                                                         |

## Summary

- Newly-flagged gaps: N rows
- All explained: yes / no
- Action items (if any): ...
```

If a newly-flagged gap is **unexplained**, that's a real data integrity issue — log it as a separate `/fix-bug` follow-up and DO NOT proceed to Phase 5 PR creation. (NO BUGS LEFT BEHIND policy applies.)

- [ ] **Step 4: Commit**

```bash
git add -f tests/fixtures/coverage-post-scope-b.json
git add docs/plans/2026-05-07-coverage-day-precise-diff-report.md
git commit -m "docs(coverage): post-Scope-B diff report

Per-symbol explanation of every newly-flagged gap. Honors
Contrarian prereq #4: capture-before-change pre/post comparison."
```

---

## E2E Use Cases (Phase 3.2b)

Project type = `fullstack` per CLAUDE.md → **API-first**, then UI. Per `rules/testing.md`, use cases live in this plan during dev (Phases 3.2b → 5.4) and graduate to `tests/e2e/use-cases/market-data/` at Phase 6.2b after the verify-e2e agent runs them green. ARRANGE must use sanctioned interfaces (public API, CLI, UI flows, documented seed scripts) — never raw DB inserts, never Parquet hand-writes. VERIFY uses the same interface as the use case targets.

The `verify-e2e` agent (Phase 5.4) executes these in order: API first; if any API case fails the agent halts (`rules/critical-rules.md` — API failure = contract broken). UI cases run only after API is green.

### UC-CDP-001 — Inventory surfaces a sub-month gap (API, happy path)

**Interface:** API
**Priority:** Must

**Setup (ARRANGE) — sanctioned API only:**

1. Stack running: `docker compose -f docker-compose.dev.yml up -d`; `curl -sf http://localhost:8800/health`.
2. Apply migrations: `cd backend && uv run alembic upgrade head`.
3. Backfill index: `cd backend && uv run python scripts/build_partition_index.py`.
4. Onboard `AAPL` with a sub-month start window via the public API:
   ```bash
   curl -sf -X POST http://localhost:8800/api/v1/symbols/onboard \
     -H "X-API-Key: $MSAI_API_KEY" -H "Content-Type: application/json" \
     -d '{"watchlist_name":"e2e-cdp-001","symbols":[{"symbol":"AAPL","asset_class":"equity","start":"2024-01-15","end":"2024-04-30"}]}'
   ```
5. Poll the returned `run_id` via `GET /api/v1/symbols/onboard/{run_id}/status` until `status=succeeded`.

**Steps:**

1. `GET /api/v1/symbols/inventory?start=2024-01-01&end=2024-04-30&asset_class=equity`.

**Verification:**

- Response 200; AAPL row present.
- `coverage_status == "gapped"`.
- `missing_ranges` contains exactly one entry `{"start": "2024-01-02", "end": "2024-01-12"}` (sub-month — proves day-precision).
- `is_stale == false` (the gap is older than the 7-trading-day trailing-edge window).

**Persistence:** Re-fetch the same endpoint after a 5-second wait → identical body. The cache table state survives.

**Why this is the happy-path day-precise contract:** Pre-Scope-B, this exact ARRANGE would return `coverage_status: "full"` because four month files exist on disk. The non-month-aligned `missing_ranges` tuple is the visible day-precise behavior change.

### UC-CDP-002 — Readiness endpoint reflects day-precise gap (API)

**Interface:** API
**Priority:** Must

**Setup:** Same as UC-CDP-001 (re-uses the AAPL onboarding state).

**Steps:**

1. `GET /api/v1/symbols/readiness?symbol=AAPL&asset_class=equity&start=2024-01-01&end=2024-04-30` (the readiness endpoint takes `symbol` as a query parameter, NOT a path parameter — corrected after Phase 5.4 verify-e2e found the path-stale).

**Verification:**

- Response 200.
- `coverage_status == "gapped"`.
- `missing_ranges` contains `{"start": "2024-01-02", "end": "2024-01-12"}`.
- `backtest_data_available == false` (gap means not full).
- `covered_range` is a non-null string of the form `"2024-01-16 → 2024-04-29"` (trading-day min/max — NOT the request window; format matches `services/symbol_onboarding/coverage.py:_derive_covered_range`).

**Persistence:** Re-fetch → identical body.

### UC-CDP-003 — Coverage gap emits Prometheus metric (API)

**Interface:** API
**Priority:** Must (Hawk prereq #5 acceptance)

**Setup:** Same AAPL state from UC-CDP-001 (gap exists).

**Steps:**

1. Read the metrics surface (unauthenticated, internal):
   ```bash
   curl -s http://localhost:8800/metrics | grep msai_coverage_gap_detected_total
   ```
2. Note the current value `V_before` for `{symbol="AAPL",asset_class="stocks"}` (zero or absent if first scan).

   > **Note on the metric label.** The counter labels use the INGEST taxonomy (`stocks`, `forex`, `futures`, `options`, `crypto`) — not the registry taxonomy (`equity`, `fx`, `futures`, `option`, `crypto`). `compute_coverage` is called from the API/orchestrator with `asset_class=ingest_asset` (already normalized via `normalize_asset_class_for_ingest`), and the counter increments under THAT value. Phase 5.4 verify-e2e caught this stale label — the use case asserts the actual emitted value.

3. Trigger a fresh inventory scan: `GET /api/v1/symbols/inventory?start=2024-01-01&end=2024-04-30`.
4. Re-read `/metrics`.

**Verification:**

- Step 4 shows a line `msai_coverage_gap_detected_total{asset_class="stocks",symbol="AAPL"} V_after` with `V_after >= V_before + 1`.
- A matching alert appears via `GET /api/v1/alerts/?limit=10` — most recent record has `level="warning"` and `title` containing `"AAPL"`.

**Persistence:** Re-fetch `/api/v1/alerts/` → the alert is durable across the 200-record cap.

### UC-CDP-004 — Onboarding a vacuous-window symbol does NOT emit gap alert (API, edge case)

**Interface:** API
**Priority:** Should

**Setup:** Stack running; pick a symbol that has zero registered data and an asset_class with a known calendar (e.g. `MSFT equity`).

**Steps:**

1. `GET /api/v1/symbols/MSFT/readiness?start=2024-01-06&end=2024-01-07&asset_class=equity` (Sat→Sun, no trading days in the window).

**Verification:**

- Response 200.
- `coverage_status == "full"` (vacuous full — no expected days).
- `missing_ranges == []`.
- `/metrics` does NOT increment `msai_coverage_gap_detected_total{symbol="MSFT"}`.
- `/api/v1/alerts/` shows no new MSFT alert.

**Why:** Confirms `compute_coverage` correctly handles windows with zero trading days. Pre-Scope-B this returned `status="full"` for any window (since there were no months) — Scope B preserves that for the right reason (empty expected set, not empty present set).

### UC-CDP-005 — CLI ingest stays green after the index migration (CLI)

**Interface:** CLI

**Setup:** Migration applied; index backfilled.

**Steps:**

1. `cd backend && uv run msai ingest stocks AAPL 2025-01-15 2025-01-20` (sub-month range).
2. After completion: `cd backend && uv run msai data-status`.

**Verification:**

- Step 1 exits 0; stdout indicates rows written.
- A `parquet_partition_index` row exists for `(stocks, AAPL, 2025, 1)` with `min_ts.date() == 2025-01-15` and `max_ts.date() == 2025-01-17` (Jan 18 is Sat — the 17 is a Fri trading day; the 20 is MLK so excluded by exchange calendar but the writer wrote a bar for it because we don't filter at write time). The cache row reflects what the writer actually persisted; coverage scan filters via the calendar.
- `msai data-status` does not crash.

**Persistence:** Re-run the CLI command (idempotent) → same `parquet_partition_index` row (mtime/size may change if the writer rewrote the file; row_count unchanged).

### UC-CDP-UI-001 — Repair a sub-month coverage gap (UI, happy path)

**Interface:** UI

**Setup:** Same AAPL state from UC-CDP-001 (sub-month gap exists). The pre-existing UC1-UC6 from `tests/e2e/use-cases/market-data/` continue to operate; this is an extension, not a replacement.

**Steps:**

1. Authenticated as Pablo, navigate to `http://localhost:3300/market-data`.
2. Confirm the `AAPL` row shows status `Gapped` and the toolbar's gappedCount ≥ 1.
3. Click the row to open the drawer (`data-testid="row-drawer"`).
4. In the drawer's `Coverage` section, locate the missing-range entry `Missing 2024-01-02 → 2024-01-12`.
5. Click the per-range Repair button (`data-testid="repair-2024-01-02-2024-01-12"`).
6. Watch for the toast `Refresh queued (run <prefix>…)`.
7. Poll the inventory query (the mutation invalidates `["inventory"]` in TanStack Query) until the row status flips.

**Verification:**

- The Repair button POSTs `/api/v1/symbols/onboard` with body `symbols[0]={symbol:"AAPL", asset_class:"equity", start:"2024-01-02", end:"2024-01-12"}` — scoped to the gap, NOT the original full window. Verify via Playwright `expect(page).toRespond('POST', /\/api\/v1\/symbols\/onboard/)` capture.
- After the run completes: drawer's Coverage section shows `No gaps in current window.`; row status flips to `Ready` (or `Backtest only` if not IB-qualified).
- Toolbar's gappedCount decrements by 1.

**Persistence:** Reload `/market-data` → AAPL remains `Ready`; reopening the drawer confirms no gaps.

**Why this is sub-month-specific (not a duplicate of UC4):** UC4 today exercises a month-aligned `2024-07-01 → 2024-08-31` gap. UC-CDP-UI-001 proves a missing range can start on a non-1st day (`2024-01-02`) and end mid-month (`2024-01-12`). Pre-Scope-B that range shape was structurally impossible — month-granularity scan only emitted month-aligned tuples.

### UC-CDP-UI-002 — Inventory page renders sub-month gap as a single Repair button (UI, smoke)

**Interface:** UI

**Setup:** Same AAPL state.

**Steps:**

1. Navigate to `/market-data`.
2. Click the `AAPL` row to open the drawer.

**Verification:**

- Coverage section renders one `<div>` with the text `Missing 2024-01-02 → 2024-01-12` and a single Repair button with `data-testid="repair-2024-01-02-2024-01-12"`.
- The toolbar shows `<N> gapped · Repair all` where N reflects the count of rows with `coverage_status == "gapped"` (≥ 1 for AAPL).

**Persistence:** Close + reopen the drawer → identical render.

### UC-CDP-006 — Capture-before-change diff explains every newly-flagged gap (operator)

**Interface:** API + filesystem (operator workflow, not regression)

**Priority:** Must (Contrarian prereq #4 acceptance) — runs ONCE during Phase 5.4, then becomes a one-shot artifact under `docs/plans/2026-05-07-coverage-day-precise-diff-report.md` (see Task 11). Not graduated to `tests/e2e/use-cases/` because it's a one-time pre/post audit, not a recurring regression check.

**Setup:** Two snapshots in `tests/fixtures/`:

- `coverage-pre-scope-b.json` — captured by Task 0 against pre-Scope-B stack.
- `coverage-post-scope-b.json` — captured by Task 11 against post-Scope-B stack.

**Steps:**

1. `diff <(jq -S . tests/fixtures/coverage-pre-scope-b.json) <(jq -S . tests/fixtures/coverage-post-scope-b.json)`.
2. For every row whose `coverage_status` flipped `full → gapped` OR whose `missing_ranges` list grew: open the corresponding parquet file with `pyarrow` and confirm the newly-flagged trading days truly are absent from the timestamp column.
3. Document each flagged gap in `docs/plans/2026-05-07-coverage-day-precise-diff-report.md` with root cause.

**Verification:**

- Every newly-flagged gap is explained (provider partial-return / sub-month onboarding / CLI spot fix).
- Zero unexplained gaps.

**If unexplained gaps exist:** That's a real data integrity issue. Open a `/fix-bug` follow-up; do NOT proceed to Phase 5 PR creation (NO BUGS LEFT BEHIND policy).

---

## Implementation Notes

### Execution-order dependencies

```
Task 0 (snapshot)           — no code change; runs against pre-Scope-B stack
Task 1 (trading_calendar)   — independent
Task 2 (model + migration)  — independent
Task 3 (PartitionIndex)     — depends on Task 2 (model)
Task 4 (write_bars wiring)  — depends on Task 3
Task 5 (backfill script)    — depends on Task 3 + Task 4
Task 6 (compute_coverage)   — depends on Task 1 + Task 3 + Task 5
Task 7 (is_trailing_only)   — depends on Task 1
Task 8 (metric counter)     — independent
Task 9 (alerting wire-up)   — depends on Task 6 + Task 8
Task 10 (integration tests) — depends on Task 6 + Task 7 + Task 9
Task 11 (post-snapshot)     — depends on every prior task; runs against post-Scope-B stack
E2E use cases (3.2b)        — authored in this plan (above); graduate to tests/e2e/use-cases/ at Phase 6.2b after Phase 5.4 PASSes
```

### Contrarian + Hawk prereq cross-check

| Prereq #                           | Source     | Honored by                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1 (trading-day definition)         | Contrarian | Task 1 (`trading_calendar.py` wraps `exchange_calendars`; falls back to `bdate_range` for crypto)                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 2 (authoritative timestamp column) | Contrarian | Task 3 (`read_parquet_footer` reads only `timestamp`; logs + returns None on mismatch)                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 3 (performance bound)              | Contrarian | Task 3 (`pyarrow` footer-only read, sub-millisecond) + Task 2 (DB cache table) + Task 4 (writer-side refresh) — inventory page p95 stays sub-second by reading the cache, not the footer                                                                                                                                                                                                                                                                                                                                                                     |
| 4 (capture-before-change)          | Contrarian | Task 0 (pre-snapshot) + Task 11 (post-snapshot + diff report)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 5 (alert wiring) — **scoped**      | Hawk       | Task 8 (counter declaration) + Task 9 (counter increment on `status="gapped"` exit + `AlertingService.send_alert`). **Deviations:** (a) label set is `{symbol, asset_class}` only; `asset_subclass` is deferred (no subclass values exist in the registry — see Out of scope). (b) Alert fires for every gapped result; `is_production` gating is delegated to alert-rule layer because the registry has no production flag yet. Both deviations are reversible: adding the label is a one-liner once subclass exists; production gating is a config change. |
| 6 (metadata cache invariants)      | Hawk       | Task 3 (`PartitionIndexService._refresh` re-reads on mtime/size mismatch) + Task 4 (write-path triggers `refresh_for_partition`) + Task 5 (one-time backfill)                                                                                                                                                                                                                                                                                                                                                                                                |

### Conventions

- **Imports inside functions** — `_apply_trailing_edge_tolerance` and `is_trailing_only` import `trading_days` lazily to avoid a circular-import risk between `coverage.py` ↔ `trading_calendar.py` ↔ `partition_index.py`. Keep them lazy.
- **`DATA_ROOT`** — every path computation uses `Path(settings.data_root) / "parquet"`. Do not hard-code `/app/data`.
- **`asset_class` values** — the ingest-side strings (`stocks`, `futures`, `fx`, `option`, `crypto`) flow through `compute_coverage`. The exchange map handles both `equity` (registry-side) and `stocks` (ingest-side); they map to the same calendar.
- **Async + sync seam** — `compute_coverage` is `async`; `ParquetStore.write_bars` is `sync`. The writer takes a SYNC `partition_index_refresh` callback and is itself event-loop-agnostic (Task 4). The callback is built by `make_refresh_callback(database_url=settings.database_url)`, which **always** opens its own `NullPool` `AsyncEngine`, runs `asyncio.run` on the calling thread, and disposes the engine — so the cache update never shares an engine across loops (P1 Codex iteration 3 fix; see [SQLAlchemy multi-loop note](https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops)). The caller contract: `write_bars` must be invoked from a sync context. Async callers (the ingest worker) MUST wrap the call in `await asyncio.to_thread(store.write_bars, ...)` so the writer (and its synchronous `asyncio.run` refresh) runs in a worker thread instead of the caller's loop thread. The callback raises `CacheRefreshMisuseError` (a `RuntimeError` subclass) if it detects a running loop on the calling thread; the writer's outer `try/except` lets that class propagate while still swallowing genuine runtime failures (DB down, network blip) as best-effort cache misses (P2 Codex iteration 4 fix). The distinction matters: contract violations fail loud so the engineer fixes the call site; transient infrastructure failures fall back to read-side footer re-derivation. Don't make `write_bars` async — it's called from sync CLI contexts too.
- **`covered_range` field** — purely a human-readable hint. Tests should not assert specific formatting beyond presence of `→` separator. Consumers may parse it but should not.

### Residual: internal-partition gaps

`_covered_days_from_rows` derives covered days from each partition's `[min_ts.date(), max_ts.date()]` window, intersected with the asset class's trading-day calendar. A partition that contains, say, days 1-5 + 15-31 of a month (provider partial-return mid-stream) reports `min_ts.date()=1, max_ts.date()=31` — the 6-14 internal gap is invisible to the cache.

We accept this trade-off:

- **Catch path 1 (writer-side metric):** Hawk prereq #5's `coverage_gap_detected` counter increments on every non-empty `missing_ranges` from `compute_coverage`. An intra-month gap in `[start, end]` that the spike-pattern partition spans WILL surface as a missing run as long as the request window's expected trading days include the gap days. Operators see the alert, can drill in via the snapshot fixture from Task 11.
- **Catch path 2 (backtest auto-heal):** when a backtest queries data for a sub-month window that falls inside an internal partition gap, the existing `auto_heal` flow re-fetches the whole window. The data lands; the partition is rewritten; the writer triggers a refresh that captures the new `min_ts/max_ts`. Eventually consistent.
- **Out of scope:** a per-day bitmap or an in-footer day-presence column. Either would catch the gap on the first read but multiplies cache row size by 31× — not worth it for a residual case the existing self-heal handles.

If audit data later shows internal-partition gaps are common (e.g. > 1% of partitions on a given symbol), revisit with a `/fix-bug` for that symbol's provider quirk before adding an entire new column to the cache schema.

### Worker-restart hook

After Task 4's commit (touches `services/parquet_store.py` + `services/data_ingestion.py`), and after Task 6's commit (touches `services/symbol_onboarding/`), and after Task 9's commit (touches `services/symbol_onboarding/`), run:

```bash
./scripts/restart-workers.sh
```

The arq workers cache the imported modules at startup; the restart picks up the new code paths. Skipping this leaves stale imports in flight (memory feedback `feedback_restart_workers_after_merges.md`).

### Verification matrix (all run before Phase 5 PR creation)

| What                        | Command                                                                    | Expected                                        |
| --------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------- |
| Unit tests                  | `cd backend && uv run pytest tests/unit/ -q`                               | green                                           |
| Integration tests           | `cd backend && uv run pytest tests/integration/ -q`                        | green                                           |
| Migration up                | `cd backend && uv run alembic upgrade head`                                | clean                                           |
| Migration down (round-trip) | `cd backend && uv run alembic downgrade -1 && uv run alembic upgrade head` | clean (Postgres-only — skip if SQLite-fallback) |
| Lint                        | `cd backend && uv run ruff check src/`                                     | clean                                           |
| Type check                  | `cd backend && uv run mypy src/ --strict`                                  | clean                                           |
| Backfill idempotency        | `cd backend && uv run python scripts/build_partition_index.py` (run twice) | same row count                                  |
| Snapshot diff               | Task 11 explanation report                                                 | every newly-flagged gap explained               |
| E2E use cases               | `verify-e2e` agent against UC1-UC7                                         | all PASS or FAIL_STALE (graduated separately)   |

### Out of scope (explicitly NOT in this PR)

- Compactor / GC of `parquet_partition_index` rows when a parquet file is deleted on disk by an operator. The cache row goes stale and `compute_coverage` would falsely treat its days as covered. In practice operators do not delete parquet files by hand on this stack; this is a documented residual. Add a sweeper in a follow-up `/quick-fix` if it ever bites. The single-partition reader path (`PartitionIndexService.get(path=...)`) DOES re-stat the file and re-derive on mismatch; the bulk-symbol reader (`get_for_symbol`) does not, by design, to keep inventory page p95 sub-second.
- `asset_subclass` label refinement on the metric. Currently identical to `asset_class`; reserved for a future ETF-vs-equity split.
- Strategy-level backtest auto-heal of sub-month gaps via the per-range Repair API. The auto-heal flow today still submits the _full_ requested range, which is fine (idempotent re-fetch) but suboptimal. A follow-up could narrow auto-heal to `report.missing_ranges` only.
- UI affordance for "1 of 4 missing intra-month gaps repaired." The drawer renders one Repair button per range today; no count-of-N indicator. Cosmetic — can ship as a `/quick-fix` later.

### Self-review notes (writing-plans skill)

**1. Spec coverage:** Every prereq in the spike's "Scope B prerequisites" table (rows 1-6) maps to at least one task. ✓
**2. Placeholder scan:** No "TBD", no "implement later", no "similar to Task N" — every code block is self-contained. ✓
**3. Type consistency:** `PartitionFooter` (read-only return type from `read_parquet_footer`) vs `PartitionRow` (DB row + extra fields like `asset_class`/`symbol`/`year`/`month`/`file_path`) — distinct on purpose; the service materializes `PartitionRow` from a `PartitionFooter` + the partition coordinates. `PartitionIndexService.get` returns `PartitionRow | None`. `PartitionIndexService.get_for_symbol` returns `list[PartitionRow]`. `PartitionIndexService.refresh_for_partition` returns `PartitionRow | None`. Names and signatures match across Tasks 3, 4, 5, 6, and 9.
**4. Caller signature change:** `compute_coverage` adds a new **required** kwarg `partition_index: PartitionIndexService`. All call sites are enumerated in Task 6d. No optional default — forcing the caller to construct the service makes the DB-session ownership explicit.
