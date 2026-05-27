# Smoke AAPL-Zero-Trades + Weak-Floor Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the operational smoke genuinely exercise every configured symbol (AAPL + SPY) and FAIL loudly when any symbol produces zero trades — fixing the prod run where `smoke_market_order/AAPL=0` passed green behind SPY's trades.

**Architecture:** Two defense-in-depth fixes. (1) The smoke pre-flight force-rebuilds the Nautilus catalog from parquet so the catalog the backtest reads can never be stale relative to ingested data, with a binary "bars present in window" backstop that force-ingests once if a symbol is genuinely empty. (2) The CLI structural floor becomes per-instrument: each `__smoke__/smoke_market_order/<symbol>` strategy must emit ≥1 trade, so a single symbol's silent zero can never again hide behind another symbol's volume.

**Tech Stack:** Python 3.12, NautilusTrader catalog (`ParquetDataCatalog`), Typer CLI, pytest.

---

## Approach Comparison (Phase 3.1b)

| Axis                  | **Chosen Default: A — catalog force-rebuild + per-instrument floor**                     | Alt B — assert-only (no self-heal)        | Alt C — always force re-ingest                                           |
| --------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------ |
| Complexity            | Moderate (reuses `ensure_catalog_data`, adds `force` pass-through + binary window check) | Low                                       | Low                                                                      |
| Blast Radius          | Smoke pre-flight + CLI floor only                                                        | Same                                      | Same                                                                     |
| Reversibility         | High (additive pre-flight step + stricter floor)                                         | High                                      | High                                                                     |
| Time to Validate      | dev smoke run (~minutes) + prod UC-003 re-run                                            | same                                      | same                                                                     |
| User/Correctness Risk | Low — targets the confirmed invariant; self-heals stale catalog                          | Leaves prod smoke RED until manual ingest | Breaks warm-path runtime budget (≤180s) — re-fetches Databento every run |

### Chosen Default

A — force-rebuild the catalog from parquet in the smoke pre-flight (eliminates the stale-catalog trigger, the most likely cause given the pipeline is symmetric AAPL vs SPY), plus a binary "bars in window" backstop that force-ingests once for a genuinely-empty symbol, plus a per-instrument deterministic floor in the CLI.

### Best Credible Alternative

B (assert-only). Rejected because it leaves the prod smoke persistently RED until a human manually ingests — the smoke should self-heal a stale/partial catalog.

## Contrarian Verdict (Phase 3.1c)

Codex (gpt-5.5, xhigh) returned **VALIDATE** — "default A wins, with rebuild-first and gap-tolerance guardrails." Adopted refinements:

1. Rebuild-first, ingest-second: force a catalog rebuild from existing parquet BEFORE any Databento re-ingest.
2. **Gap tolerance (load-bearing):** a spike confirmed `verify_catalog_coverage` reports weekend/holiday edge-gaps (Dec 1 2024 = Sunday; trailing edge after the last bar) EVEN for fully-ingested data — for BOTH symbols identically. So gap-analysis cannot distinguish "AAPL broken" from "AAPL fine." The fix uses a **binary "bars overlap window" check**, not gap-analysis.
3. Don't bypass the existing ingest mutex on the forced re-ingest fallback.
4. Prefer instrument IDs returned by `ensure_catalog_data` over hand-built `f"{sym}.NASDAQ"`.

**Related latent finding (deferred, documented):** Codex flagged that `orchestration._prepare_strategy_config` only _fills missing_ `instrument_id`/`bar_type`, while `workers/backtest_job.py:663` _always overwrites_ them from the resolved catalog id — a separate stale-config zero-bars trap for portfolio runs whose config carries a stale instrument_id. NOT the smoke's trigger (the smoke's seed config id `AAPL.NASDAQ` already matches the resolved catalog id), and it affects ALL portfolio runs (broader blast radius). Deferred with rationale; see "Deferred follow-ups" below.

---

## Plan-Review Corrections (iteration 1 — these OVERRIDE the task bodies below where they conflict)

Codex plan-review (gpt-5.5, xhigh) against the actual code found 2×P1, 2×P2, 1×P3. All folded in:

1. **[P1] `force=True` must purge-then-rebuild (Task 1).** Confirmed in code: `_purge_catalog_for_instrument` is nested inside the `if not force:` block (catalog_builder.py:159-209), so `force=True` currently skips BOTH the staleness skip AND the purge → it writes new bars on top of stale ones (interleave / overlap-error / silent no-op). Restructure `build_catalog_for_symbol` so `force=True` purges the instrument's bar dir FIRST, then rebuilds:

   ```python
   if force:
       # Unlink the marker FIRST (iter-2 P2): it is re-written only after all
       # bars land (catalog_builder.py:250-251). If a force rebuild crashes
       # after writing partial bars but before the marker rewrite, an old
       # marker whose hash still matches source_hash would let the next
       # force=False skip on (marker-match + partial-bars). Removing it means
       # a mid-write crash leaves NO marker -> next call treats the partial
       # bars as legacy-unmarked and purges+rebuilds.
       marker_path.unlink(missing_ok=True)
       _purge_catalog_for_instrument(catalog_root, instrument_id_str, bar_spec=_BAR_SPEC)
   else:
       # existing marker/skip/stale-purge/legacy-purge logic unchanged
       ...
   ```

   This both enables Fix #1's clean rebuild AND repairs the latent `force` bug. Tests: (a) `force=True` removes stale bars (build window A, then force-rebuild from parquet covering only window B → catalog has B, not A); (b) a force rebuild interrupted before the marker rewrite is not skipped on the next non-forced call (simulate: delete marker + leave partial bars → next build purges+rebuilds, not "already_populated").

2. **[P1] Per-instrument floor asserts EXPECTED keys, not just present keys (Task 3).** A missing `__smoke__/smoke_market_order/AAPL` key (not merely `=0`) would slip through a "floor present keys" check. Derive the expected set from `SMOKE_CONFIGS[config].symbols` and assert, for each symbol, that `__smoke__/smoke_market_order/{symbol}` is PRESENT in `trade_count_by_strategy` AND ≥1. The CLI must know `config` (it already takes `--config`); map symbols via `SMOKE_CONFIGS[config].symbols`.

3. **[P2] `catalog_has_bars_in_window` uses the Nautilus catalog API, NOT filename parsing.** The placeholder int-strip parser is invalid for the real filename (`2024-12-02T12-00-00-000000000Z_2024-12-30T23-24-00-000000000Z.parquet`) and is fragile to Nautilus format changes. Implement via the catalog's own query: build `bar_type = BarType.from_str(f"{instrument_id}-{bar_spec}")` and call `ParquetDataCatalog(path=str(catalog_root)).bars(bar_types=[str(bar_type)], start=<start_ns>, end=<end_ns>)`; return `len(result) > 0`. (Verify the exact `bars(...)` windowed-query signature against the installed Nautilus in the dev container before implementing — `catalog.bars(instrument_ids=[...])` is already used at catalog_builder.py:163,198, so the API is available; confirm whether it accepts `start`/`end`/`bar_types`.) Guard the missing-dir / empty-catalog case to return `False`.

4. **[P2] Task 2 is CHECK-FIRST, not force-rebuild-every-run (avoids the double-build + warm-path cost).** Restructured `_ensure_catalog_fresh`:
   a. `ids = ensure_catalog_data(instrument_ids, force=False)` — normal build; a no-op when already populated (no wasted rebuild on the healthy warm path, and no double-build after `_ensure_ingested`'s cold build).
   b. Binary check `catalog_has_bars_in_window` per instrument.
   c. For empty instruments → `ensure_catalog_data([those], force=True)` (purge+rebuild from existing parquet — fixes the stale/partial-catalog case, which is the confirmed prod trigger). Re-check.
   d. Still empty → mutex-guarded `_force_ingest` (always fetches, unlike `_ensure_ingested`) → `ensure_catalog_data(force=True)` → re-check.
   e. Still empty → raise `RuntimeError` naming the symbols.
   This force-rebuilds ONLY when the binary check shows a problem. Benchmark the nightly (full-2024 × 2 symbols) path stays within the 600s cold budget (the cold path's catalog build already happens in `_ensure_ingested`; step (a) is then a no-op).

5. **[P3] Update stale tests/docs that reference the old total floor.** `backend/tests/unit/test_cli.py:523` (asserts old `trade_count_total < 2` behavior) and `tests/e2e/use-cases/backtests/smoke-cli-fast.md:53` (references the floor of 2). Update both to the per-instrument semantics in the relevant task.

**Residual (documented, acceptable):** the binary "any bars in window" check does not detect a PARTIAL catalog (e.g., only Dec 1-5 present). That's not the prod symptom (total absence → 0) and detecting partial coverage robustly is the calendar-noise trap the spike exposed. The per-instrument floor is the backstop. Out of scope.

## Root Cause (confirmed mechanism)

- AAPL trades iff the `AAPL.NASDAQ` Nautilus catalog holds bars for the window. Verified in dev: after ingesting AAPL Dec-2024 (9829 bars), the smoke trades AAPL 442+2 (884 total).
- The smoke pre-flight (`_ensure_ingested` → `_symbols_with_gaps`) checks RAW-PARQUET coverage by symbol (trading-day-aware via `compute_coverage`), but the backtest reads the Nautilus CATALOG keyed by the strategy's exact `instrument_id+bar_type`. The catalog build short-circuits on a matching source-hash marker (`nautilus_catalog_already_populated`), so a stale/partial catalog can serve 0 bars for the window despite parquet being present. On prod, AAPL's catalog lacked Dec-2024 bars at backtest time → 0 trades; SPY's was fresh → 440.
- The structural floor `trade_count_total < 2` (cli.py:2036,2192) is a SUM, so SPY's 2 masked AAPL's 0.

---

## File Structure

- `backend/src/msai/services/nautilus/catalog_builder.py` — add `force` pass-through to `ensure_catalog_data`; add `catalog_has_bars_in_window()` binary helper.
- `backend/src/msai/services/smoke/runner.py` — add `_ensure_catalog_fresh()` pre-flight step (force-rebuild + binary backstop), call it in `run_smoke` after `_ensure_ingested`.
- `backend/src/msai/cli.py` — replace total-based floor with per-instrument `smoke_market_order/<symbol>` floor.
- `backend/tests/unit/services/nautilus/test_catalog_builder.py` — tests for `force` + `catalog_has_bars_in_window`.
- `backend/tests/unit/services/smoke/test_runner_coverage.py` — tests for `_ensure_catalog_fresh`.
- `backend/tests/unit/test_cli.py` — tests for the per-instrument floor.

---

## Task 1: Binary "bars in window" helper + `force` pass-through (catalog_builder.py)

**Files:**

- Modify: `backend/src/msai/services/nautilus/catalog_builder.py`
- Test: `backend/tests/unit/services/nautilus/test_catalog_builder.py`

- [ ] **Step 1: Write the failing test for `catalog_has_bars_in_window`**

```python
# backend/tests/unit/services/nautilus/test_catalog_builder.py (add)
from datetime import UTC, date, datetime
from pathlib import Path
from msai.services.nautilus.catalog_builder import (
    build_catalog_for_symbol, catalog_has_bars_in_window,
)

def test_catalog_has_bars_in_window_true_for_overlapping_data(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"; catalog_root = tmp_path / "catalog"
    _write_synthetic_parquet(raw_root, rows=2 * 24 * 60, start_ts=datetime(2024, 12, 2, tzinfo=UTC), symbol="AAPL")
    iid = build_catalog_for_symbol(symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root)
    # window overlaps the written bars
    assert catalog_has_bars_in_window(catalog_root=catalog_root, instrument_id=iid, start=date(2024, 12, 1), end=date(2024, 12, 31)) is True

def test_catalog_has_bars_in_window_false_when_no_overlap(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"; catalog_root = tmp_path / "catalog"
    _write_synthetic_parquet(raw_root, rows=2 * 24 * 60, start_ts=datetime(2024, 12, 2, tzinfo=UTC), symbol="AAPL")
    iid = build_catalog_for_symbol(symbol="AAPL", raw_parquet_root=raw_root, catalog_root=catalog_root)
    # window is a different year — no overlapping bar files
    assert catalog_has_bars_in_window(catalog_root=catalog_root, instrument_id=iid, start=date(2023, 1, 1), end=date(2023, 1, 31)) is False

def test_catalog_has_bars_in_window_false_when_dir_missing(tmp_path: Path) -> None:
    assert catalog_has_bars_in_window(catalog_root=tmp_path / "catalog", instrument_id="AAPL.NASDAQ", start=date(2024, 12, 1), end=date(2024, 12, 31)) is False
```

(`_write_synthetic_parquet` already exists in this test module — reuse it.)

- [ ] **Step 2: Run the tests; expect ImportError / failures**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_catalog_builder.py -k catalog_has_bars_in_window -v`
Expected: FAIL (`catalog_has_bars_in_window` not defined).

- [ ] **Step 3: Implement `catalog_has_bars_in_window` + `force` pass-through**

Implement per **Correction #3** — query the Nautilus catalog API, NOT filename parsing (Codex iter-2 confirmed `bars(bar_types=, instrument_ids=, start=, end=)` exists in nautilus_trader 1.223.0 and forwards to `query(..., start=, end=)`):

```python
# In catalog_builder.py — add near verify_catalog_coverage.
def catalog_has_bars_in_window(
    *,
    catalog_root: Path,
    instrument_id: str,
    start: date,
    end: date,
    bar_spec: str = _BAR_SPEC,
) -> bool:
    """Binary: does the catalog hold ANY bar in [start, end] for this instrument?

    Robust to weekend/holiday edge-gaps that ``verify_catalog_coverage`` reports
    (Dec 1 Sunday, trailing edge) because it asks "any bars?" not "zero gaps?".
    Uses the Nautilus catalog query (start/end ns window) — no fragile filename
    parsing.
    """
    bar_type = BarType.from_str(f"{instrument_id}-{bar_spec}")
    start_ns = dt_to_unix_nanos(pd.Timestamp(start, tz="UTC"))
    end_ns = dt_to_unix_nanos(pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)) - 1
    catalog = ParquetDataCatalog(path=str(catalog_root))
    try:
        bars = catalog.bars(bar_types=[str(bar_type)], start=start_ns, end=end_ns)
    except (FileNotFoundError, ValueError):
        # Missing instrument/bar dir or empty catalog -> no bars.
        return False
    return len(bars) > 0
```

> Implementer: confirm the exact `bars(...)` kwarg names against the installed
> Nautilus in the dev container (`catalog.bars(instrument_ids=[...])` is already
> used at catalog_builder.py:163,198). Use `dt_to_unix_nanos` from
> `nautilus_trader.core.datetime` (already imported in this module or import it).
> Add a test that builds a real Dec-2024 catalog and asserts
> `catalog_has_bars_in_window(... Dec 2024) is True` and `(... 2023) is False`,
> AND verify against the REAL dev catalog (AAPL.NASDAQ Dec-2024 → True) before
> claiming done.

Then add `force` to `ensure_catalog_data` AND fix `force` to purge-then-rebuild per **Correction #1** (restructure the `if not force:` block in `build_catalog_for_symbol` so `force=True` unlinks the marker + purges before rebuilding):

```python
def ensure_catalog_data(
    symbols: list[str],
    raw_parquet_root: Path,
    catalog_root: Path,
    *,
    asset_class: str = "stocks",
    raw_symbols: list[str] | None = None,
    force: bool = False,
) -> list[str]:
    ...
    instrument_ids.append(
        build_catalog_for_symbol(
            symbol=symbol,
            raw_parquet_root=raw_parquet_root,
            catalog_root=catalog_root,
            asset_class=asset_class,
            raw_symbol_override=raw_override,
            force=force,
        )
    )
```

- [ ] **Step 4: Run tests; expect PASS**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_catalog_builder.py -v`
Expected: PASS. **Then verify `catalog_has_bars_in_window` against the REAL dev catalog** (AAPL.NASDAQ Dec-2024 → True; 2023 → False) via a one-off `docker exec` before committing.

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/catalog_builder.py backend/tests/unit/services/nautilus/test_catalog_builder.py
git commit -m "feat(catalog): force pass-through on ensure_catalog_data + binary catalog_has_bars_in_window"
```

---

## Task 2: Smoke pre-flight catalog freshness (runner.py)

**Files:**

- Modify: `backend/src/msai/services/smoke/runner.py`
- Test: `backend/tests/unit/services/smoke/test_runner_coverage.py`

- [ ] **Step 1: Write the failing test**

```python
# CHECK-FIRST flow (Correction #4). Mock ensure_catalog_data /
# catalog_has_bars_in_window / _force_ingest and assert ordering for 3 cases:
#   Case healthy: ensure_catalog_data(force=False) -> catalog_has_bars_in_window
#     returns True for all -> NO force rebuild, NO ingest.
#   Case stale (catalog empty, parquet present): ensure_catalog_data(force=False)
#     -> binary check False for AAPL -> ensure_catalog_data([AAPL], force=True)
#     -> re-check True -> NO _force_ingest. (rebuild-from-parquet fixed it.)
#   Case missing (still empty after rebuild): force=False -> empty -> force=True
#     -> still empty -> _force_ingest(AAPL) -> ensure_catalog_data(force=True)
#     -> still empty -> raise RuntimeError naming AAPL.
# Assert the warm/healthy path never calls force=True (no every-run rebuild).
```

- [ ] **Step 2: Run; expect FAIL** (`_ensure_catalog_fresh` undefined).
      Run: `cd backend && uv run pytest tests/unit/services/smoke/test_runner_coverage.py -k catalog_fresh -v`

- [ ] **Step 3: Implement `_ensure_catalog_fresh` + wire into `run_smoke`**

```python
# runner.py — new helper, called in run_smoke after _ensure_ingested.
from msai.services.nautilus.catalog_builder import (
    catalog_has_bars_in_window, ensure_catalog_data,
)
from msai.services.nautilus.instruments import DEFAULT_EQUITY_VENUE

def _is_empty(catalog_root, instrument_id, start_date, end_date) -> bool:
    return not catalog_has_bars_in_window(
        catalog_root=catalog_root, instrument_id=instrument_id,
        start=start_date, end=end_date,
    )

async def _ensure_catalog_fresh(symbols: tuple[str, ...], start: str, end: str) -> None:
    """Guarantee the Nautilus catalog the smoke backtest reads actually covers
    the window for each strategy instrument. CHECK-FIRST (Correction #4) — only
    force-rebuilds when a symbol is actually empty for the window, so the
    healthy warm path stays a no-op and there is no double-build after the
    cold-path build in ``_ensure_ingested``.
    """
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()
    cat_root = settings.nautilus_catalog_root
    instrument_ids_input = [f"{s}.{DEFAULT_EQUITY_VENUE}" for s in symbols]

    # (a) Normal build — no-op when already populated + hash matches.
    ids = await asyncio.to_thread(
        ensure_catalog_data,
        symbols=instrument_ids_input,
        raw_parquet_root=settings.parquet_root,
        catalog_root=cat_root,
        asset_class="stocks",
        force=False,
    )
    pairs = list(zip(symbols, ids, strict=True))

    # (b) Binary check. (c) Force purge+rebuild from existing parquet for the
    # empties — fixes the confirmed stale/partial-catalog prod trigger cheaply
    # (no Databento round-trip).
    empty = [(sym, iid) for sym, iid in pairs if _is_empty(cat_root, iid, start_date, end_date)]
    if empty:
        log.warning("smoke_catalog_empty_force_rebuild", symbols=[s for s, _ in empty])
        await asyncio.to_thread(
            ensure_catalog_data,
            symbols=[iid for _, iid in empty],
            raw_parquet_root=settings.parquet_root,
            catalog_root=cat_root,
            asset_class="stocks",
            force=True,
        )
        empty = [(sym, iid) for sym, iid in empty if _is_empty(cat_root, iid, start_date, end_date)]

    # (d) Still empty after rebuild => parquet genuinely lacks the window =>
    # mutex-guarded force ingest, then force rebuild, then re-check.
    if empty:
        log.warning("smoke_catalog_empty_force_ingest", symbols=[s for s, _ in empty])
        await _force_ingest(tuple(s for s, _ in empty), start, end)
        await asyncio.to_thread(
            ensure_catalog_data,
            symbols=[iid for _, iid in empty],
            raw_parquet_root=settings.parquet_root,
            catalog_root=cat_root,
            asset_class="stocks",
            force=True,
        )
        still = [sym for sym, iid in empty if _is_empty(cat_root, iid, start_date, end_date)]
        # (e) Loud failure beats a silent 0-trade backtest.
        if still:
            raise RuntimeError(
                f"Smoke catalog still has no bars for {still} in {start}..{end} after "
                "force-rebuild + forced ingest — Databento may lack data for the window."
            )
```

> Implementer notes:
>
> - Add `import asyncio` if not present.
> - `_force_ingest` is a thin mutex-guarded wrapper that ALWAYS calls
>   `ingest_symbols` (unlike `_ensure_ingested`, which skips on parquet
>   coverage). Reuse the lock acquire/release pattern from `_ensure_ingested`
>   (per-`(symbol, month)` `acquire_ingest_lock`/`release_ingest_lock`).
>   Do NOT bypass the mutex (Codex guardrail #3).
> - Wire `await _ensure_catalog_fresh(config.symbols, ...)` into `run_smoke`
>   immediately AFTER `await _ensure_ingested(...)` (step 1) and BEFORE the
>   portfolio bootstrap (step 2).

- [ ] **Step 4: Run; expect PASS.** Run: `cd backend && uv run pytest tests/unit/services/smoke/test_runner_coverage.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/smoke/runner.py backend/tests/unit/services/smoke/test_runner_coverage.py
git commit -m "fix(smoke): force-rebuild catalog + force-ingest empty symbols in pre-flight"
```

---

## Task 3: Per-instrument deterministic floor (cli.py)

**Files:**

- Modify: `backend/src/msai/cli.py`
- Test: `backend/tests/unit/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# Given a smoke result JSON with trade_count_by_strategy where
# smoke_market_order/AAPL == 0 and smoke_market_order/SPY == 2 and
# trade_count_total == 440 (the prod bug shape), the CLI must classify it as
# a structural FAIL naming __smoke__/smoke_market_order/AAPL.
# Conversely, AAPL=2 + SPY=2 (both >=1) is a PASS for the floor.
# ema_cross/AAPL == 0 alone must NOT fail (ema_cross is not floored).
```

- [ ] **Step 2: Run; expect FAIL** (current code passes the prod shape — total 440 ≥ 2).
      Run: `cd backend && uv run pytest tests/unit/test_cli.py -k per_instrument -v`

- [ ] **Step 3: Implement the per-instrument floor**

Replace cli.py:2192-2197 (the `trade_total < _SMOKE_TRADE_FLOOR` block) with:

```python
        by_strategy: dict[str, Any] = metrics.get("trade_count_by_strategy") or {}
        # Per-instrument deterministic floor (Correction #2): assert the
        # EXPECTED smoke_market_order strategies from the config, not just the
        # present keys — a MISSING __smoke__/smoke_market_order/AAPL key (not
        # merely =0) must also fail. Each smoke_market_order/<symbol> is
        # designed to emit ≥1 order/instrument every run; a 0 (or absent key)
        # means that symbol's catalog had no bars — fail loudly rather than let
        # another symbol's volume mask it (the AAPL=0 / SPY=440 prod incident).
        # ema_cross is NOT floored (can legitimately be 0 in a short window).
        expected = [
            f"__smoke__/smoke_market_order/{sym}"
            for sym in SMOKE_CONFIGS[config].symbols
        ]
        for name in expected:
            count = int(by_strategy.get(name, -1))
            if count < 1:
                shown = "absent" if name not in by_strategy else str(count)
                structural_problems.append(
                    f"{name} produced {shown} trades; smoke_market_order must emit "
                    "≥1 per instrument (that symbol's catalog likely had no bars)"
                )
```

**Add the import** (iter-4 P2: `SMOKE_CONFIGS` is NOT currently imported in cli.py): add `from msai.services.smoke.config import SMOKE_CONFIGS` to cli.py's imports. `config` is the resolved `--config` value. This asserts BOTH presence and ≥1 for every configured symbol.

Update the `_SMOKE_TRADE_FLOOR` comment (cli.py:2032-2036) to reflect the per-instrument semantics, and update the docstring at cli.py:2071 ("trade_count_total below the deterministic floor of 2") to describe the per-instrument floor. `_SMOKE_TRADE_FLOOR` is referenced only by the block being replaced (grep-confirmed) — remove the constant + its comment cleanly.

- [ ] **Step 3b: Update the stale existing tests in `backend/tests/unit/test_cli.py`** (iter-4 P2 — these assert the OLD total floor):
  - `:519` asserts `payload["metrics"]["trade_count_total"] == 2` — fine to keep (it's a passing-shape fixture), but ensure the fixture's `trade_count_by_strategy` includes `__smoke__/smoke_market_order/{AAPL,SPY}` ≥1 so the per-instrument floor passes.
  - `:523-543` `test_structural_fail_when_trade_count_below_floor_exits_nonzero` — rewrite to the per-instrument shape: a `completed` run with `trade_count_by_strategy = {"__smoke__/smoke_market_order/AAPL": 0, "__smoke__/smoke_market_order/SPY": 2, "__smoke__/ema_cross/AAPL": 0, "__smoke__/ema_cross/SPY": 438}` and `trade_count_total: 440` (the exact prod bug shape) MUST exit nonzero and name `__smoke__/smoke_market_order/AAPL`. Add a sibling test: all four present + both smoke_market_order ≥1 → PASS.
  - `:656/:670` — the `smoke_alert_cmd` passthrough test seeds a sample `structural_problems = ["trade_count_total=0 < floor 2"]`. Update the sample string to a per-instrument example (e.g. `"__smoke__/smoke_market_order/AAPL produced 0 trades; ..."`) so the fixture isn't misleading; the assertion still verifies passthrough, not floor logic.

- [ ] **Step 4: Run; expect PASS.** Run: `cd backend && uv run pytest tests/unit/test_cli.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/cli.py backend/tests/unit/test_cli.py
git commit -m "fix(smoke): per-instrument deterministic floor (each smoke_market_order >=1)"
```

---

#### E2E Use Cases (Phase 3.2b)

**Surface coverage decision** (project exposes UI + API + CLI per CLAUDE.md `## E2E Configuration`; the smoke capability is reachable via CLI `msai backtest smoke`, API `POST /api/v1/portfolios/smoke/runs`, the `/backtests` UI "Run smoke" button, and the nightly `smoke.yml` workflow):

- **CLI: Covered** — UC-1 below. The prod incident manifested through the nightly workflow, which wraps the CLI; CLI is the primary smoke surface.
- **API: N/A — internal seam backing the same runner.** The `/smoke/runs` endpoint and the CLI both call `services.smoke.runner.run_smoke`; the fix is entirely inside that shared runner + the CLI's result classification. The API contract is unchanged. Covering the same `run_smoke` path twice would be the duplicate-assertion anti-pattern. (Endpoint contract stays in integration tests.)
- **UI: N/A — UI-only trigger, no behavior change in the button.** The "Run smoke" button POSTs to the same endpoint; the fix changes neither the button nor the run-detail page.

##### UC-1 — Operator runs the fast smoke and sees both symbols actually trade

```
Actor:         Operator running the MSAI CLI to validate the data→backtest→metrics pipeline before merging
Scenario:      They just changed the smoke pipeline and need to confirm the fast smoke genuinely
               exercises BOTH configured symbols (AAPL and SPY), not just one — the prod nightly had
               silently passed with AAPL contributing zero trades.
Interface:     CLI
Intent:        The operator runs the fast smoke and confirms every deterministic per-symbol order
               actually fired, so a green smoke truly means the whole pipeline works for all symbols.
Setup:         Dev stack up (docker-compose.dev.yml). Do NOT pre-run the smoke (that's the action
               under test) and do NOT touch the catalog/parquet by hand — the smoke is idempotent and
               self-heals its own data, so it must pass from whatever state the dev box is in.
Steps:         1. Run `msai backtest smoke --config fast --json` in the backend container.
               2. Parse the emitted JSON's trade_count_by_strategy.
Verification:  stdout JSON shows status="completed" with structural_problems == [] AND
               trade_count_by_strategy has BOTH __smoke__/smoke_market_order/AAPL ≥ 1 AND
               __smoke__/smoke_market_order/SPY ≥ 1 (the deterministic per-instrument floor — the
               regression guard for the prod incident). The ema_cross/{AAPL,SPY} keys are present
               (observed, typically >0 for a month of minute bars but NOT a hard requirement — they
               are not floored). The human-readable table (non-JSON run) prints "PASS".
Persistence:   Re-run `msai backtest smoke --config fast --json` a second time (warm path); it still
               reports status="completed", structural_problems == [], and AAPL's smoke_market_order
               still ≥ 1 — confirming the catalog-freshness guarantee holds across runs, not just on
               the cold ingest.
```

> Prod confirmation (Phase 5.4 / post-merge): re-run `gh workflow run smoke.yml -f config=fast` (UC-003)
> and confirm `smoke_market_order/AAPL ≥ 1` in the prod alert payload. This is the ultimate proof the
> fix works against the real prod data state that triggered the incident.

---

## Dispatch Plan (Phase 4.0)

| Task               | Writes                                                                          | Depends on                                 | Parallel?                                  |
| ------------------ | ------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------ |
| T1 catalog_builder | `catalog_builder.py`, `tests/unit/services/nautilus/test_catalog_builder.py`    | —                                          | serial first                               |
| T2 smoke runner    | `services/smoke/runner.py`, `tests/unit/services/smoke/test_runner_coverage.py` | T1 (`force`, `catalog_has_bars_in_window`) | after T1                                   |
| T3 cli floor       | `cli.py`, `tests/unit/test_cli.py`                                              | — (independent of T1/T2)                   | may run parallel to T1/T2 (disjoint files) |

T1 and T3 touch disjoint files and could parallelize; T2 depends on T1's new symbols. Given the small size and tight coupling of the smoke contract, **serial execution (T1 → T2 → T3) is the safe default** — no shared-file races, and each task's tests gate the next.

---

## Deferred follow-ups (documented, not in this branch)

- **`_prepare_strategy_config` fill-vs-overwrite (Codex finding):** `orchestration._prepare_strategy_config` only fills MISSING `instrument_id`/`bar_type`, while `workers/backtest_job.py:663` always overwrites from the resolved catalog id. This is a latent stale-config zero-bars trap for portfolio runs whose candidate config carries a stale instrument_id (cf. `feedback_candidate_config_snapshots_stale_default_config.md`). It is NOT the smoke's trigger (the smoke's seed config id already matches the resolved catalog id) and changing it affects ALL portfolio backtests (broader blast radius), so it is out of scope here. Track as a separate `/fix-bug` if a non-smoke portfolio run hits it.
