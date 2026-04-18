# Alias windowing must honor the backtest start date

## Problem

Historical backtests silently returned the wrong contract/venue alias after a
venue change or futures roll. Example: an AAPL backtest with
`start_date=2022-06-01` (when AAPL was listed on NASDAQ) would receive
`AAPL.ARCA` — the venue active _today_, not during the backtest window. This
mis-partitions parquet reads and can make a backtest appear to use data that
never existed for that contract.

## Root Cause

`SecurityMaster.resolve_for_backtest` in
`claude-version/backend/src/msai/services/nautilus/security_master/service.py`
had two warm-path resolution branches that ignored the backtest's `start` kwarg:

1. **Path 2 (dotted alias):**
   `registry.find_by_alias(sym, provider="databento")` — no `as_of_date` passed,
   so `find_by_alias` defaulted to today UTC. If today falls outside the closed
   `[effective_from, effective_to)` window of the requested alias (e.g.
   AAPL.NASDAQ closed in 2023), the query returned `None` and the resolver
   raised `DatabentoDefinitionMissing` — misleading the operator into thinking
   the registry was cold when it was actually pinned to today-only semantics.
2. **Path 3 (bare ticker):** Picked the alias with `effective_to IS NULL` via
   `next((a for a in idef.aliases if a.effective_to is None))`. That's
   "currently open" — the post-roll alias — not the alias active on
   `start_date`.

Path 1 (`.Z.N` continuous pattern) already threaded `start`/`end` correctly via
`_resolve_databento_continuous`.

## Solution

Parse the existing `start: str | None` kwarg once before the symbol loop and
thread the resolved `date` through both warm paths:

```python
# Window alias lookups by start so historical backtests get the alias
# that was active during the backtest window, not today's front-month.
as_of = date.fromisoformat(start) if start else datetime.now(UTC).date()

# Path 2
idef = await registry.find_by_alias(
    sym, provider="databento", as_of_date=as_of
)

# Path 3
active_alias = next(
    (
        a for a in idef.aliases
        if a.effective_from <= as_of
        and (a.effective_to is None or a.effective_to > as_of)
    ),
    None,
)
```

`find_by_alias` already accepted `as_of_date: date | None = None` (defaulting
to today), so no signature change was needed in `registry.py`.

## Prevention

Three guardrails to avoid this class of bug in the future:

1. **Any registry lookup that returns date-sensitive rows MUST accept an
   `as_of_date` kwarg at its public entry point.** The SQL in
   `InstrumentRegistry.find_by_alias` had the half-open window predicate from
   day one; the bug was a caller not passing the parameter. New code that
   consumes aliases should prefer `find_by_alias(..., as_of_date=as_of)` over
   the `find_by_raw_symbol → iterate aliases` pattern.
2. **The `effective_to IS NULL` shortcut is only correct for live/today
   resolvers.** `resolve_for_live`, `_resolve_databento_continuous`, and the
   "get the active alias" inner loop in `resolve_for_backtest` all previously
   used this shortcut. Two of those three are actually correct under live
   semantics — but any copy-paste into a backtest context needs the full
   window predicate.
3. **Integration tests must seed alias windows that span the backtest
   window.** A regression test of
   `test_resolve_for_backtest_bare_ticker_honors_start_date` is cheap and
   directly guards the bug (seed two consecutive venue aliases on a single
   instrument, assert the older one comes back for a historical start).

## Related

- Bug flagged in PR #32 `docs/CHANGELOG.md` under "Known limitations
  discovered post-Task 20 (limitation #2)".
- Integration tests at
  `claude-version/backend/tests/integration/test_security_master_resolve_backtest.py`
  (`test_resolve_for_backtest_dotted_alias_honors_start_date`,
  `test_resolve_for_backtest_bare_ticker_honors_start_date`,
  `test_resolve_for_backtest_bare_ticker_no_start_uses_today`).
