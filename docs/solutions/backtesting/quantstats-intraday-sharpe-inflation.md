# QuantStats Sharpe inflation on intraday backtests

## Problem

Backtests on minute-bar data reported Sharpe ratios ~20× higher than reality. Sortino and volatility were similarly inflated. Daily backtests were correct.

## Root cause

QuantStats treats every row in a returns series as one trading period and annualises with `sqrt(252)`. Feeding 390 minute bars per trading day × 252 trading days makes it see 98,280 "periods" per year — so annualised stats scale by roughly `sqrt(390) ≈ 19.7` above the correct value.

Claude's `ReportGenerator.generate_tearsheet` handed the raw per-bar returns series straight to `qs.reports.html` without period normalization. The docstring even said "Period returns series (daily or intraday)" — it accepted both but silently produced nonsense for the intraday case.

## Solution

Port Codex's `_normalize_report_returns` helper into `report_generator.py`. Before calling QuantStats:

1. Coerce values to numeric, drop NaNs.
2. Parse the index into a UTC `DatetimeIndex` when possible.
3. Group by `index.normalize()` (strips time to midnight, preserves tz).
4. Compound each day: `(1 + returns).prod() - 1`.
5. Sort by index.

Already-daily input round-trips unchanged because `(1 + r).prod()` over a one-element group equals `1 + r`.

```python
normalized = ((1.0 + series).groupby(series.index.normalize()).prod() - 1.0).astype(float)
```

## Prevention

- Regression test `test_intraday_sharpe_matches_daily_sharpe_after_normalize` splits a known daily return series into 390 multiplicative bar returns per day and asserts the normaliser reproduces the original daily series. If anyone removes the groupby step, this fails immediately.
- Edge-case tests cover: empty, None, non-numeric values, non-datetime index, tz-aware midnight crossings, tz-naive DatetimeIndex, unsorted timestamps.

## Reference

- Claude fix: `claude-version/backend/src/msai/services/report_generator.py`
- Codex original: `codex-version/backend/src/msai/services/report_generator.py`
