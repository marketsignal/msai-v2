# Backtest account returns and actual execution records

## Reproduced problem

The October 3, 2026 local and Azure assessments found completed backtests whose headline return, chart and execution log disagreed. The local AAPL reference had 100 fills, ended flat, and lost $0.53 on $1,000,000 starting capital. Its stored return was 100 times too large; the chart omitted the first day's $0.04 loss and used a different capital basis. The log presented unavailable fill P&L and fees as zeros.

## Causes and repair contract

- Nautilus's `PnL% (total)` is percentage-valued; public return fields are fractional ratios. A genuine zero is not missing data. Headline account returns and charts need the same opening-capital and daily balance inputs.
- Daily compaction must include the first session relative to opening capital. Asynchronous account reports require carrying each account's state forward before summing; absent updates do not mean the other account vanished. Unsupported currency combinations or missing opening states must fail explicitly.
- Drawdown starts from opening capital, including a first-period loss. The frontend also needs that capital as its cumulative-return baseline instead of the first post-return observation.
- Order reports describe requested intent. Individual execution reports describe fills, including partial quantities, execution time, price and commission. Preserve both roles separately; never persist canceled unfilled orders as executions.
- Unknown per-fill P&L remains null. New versioned records preserve actual recorded zero fees; unversioned legacy rows contain placeholders and must expose their economics as unknown without rewriting historical storage.

The additive accounting object identifies version, actual initial capital, currency, realized account-balance basis and engine-recorded costs. It lives in existing JSON fields; no migration is required. The API exposes it even when chart materialization fails. Legacy rows remain clearly separate from corrected results.

The installed QuantStats scalar drawdown calculation handles an initial loss, but its HTML episode summary can omit the maximum drawdown when the series starts below the opening peak and never recovers. The report adapter presents the application's canonical primary metrics and identifies the remaining QuantStats statistics as supplemental. Unexpected headline markup yields a canonical-only report with a clear explanation. Small nonzero percentages retain enough precision to remain visible; no synthetic return observation is added.

## Limits

This is realized balance accounting, not complete marked-to-market NAV. Open-position unrealized P&L, realistic fee/slippage calibration, exchange-session calendars, data completeness, futures instrument definitions and unbiased selection remain separate acceptance gates. `num_fills` and the compatible `num_trades` field count executions, not independent closed trading outcomes. Correcting order-row counts can intentionally change whether an existing fill threshold is met.

Do not mix historical optimization studies with corrected results as if their accounting basis were equivalent. Existing numeric sentinels for undefined risk statistics remain compatible; a short or inactive sample does not establish a meaningful Sharpe ratio.

## Verification

The [repair plan](../../plans/2026-10-03-research-foundation.md) defines first-session, initial-loss, asynchronous-account, partial-fill, currency, legacy and presentation regressions. The independent acceptance helper reconciles recorded fills with decimal arithmetic without importing application accounting code. Its baseline fails with expected return `-5.3e-7` versus stored `-5.299999999115244e-5`.

Producer verification passed 103 accounting/worker/control tests, 47 schema/API tests, 35 CLI/configuration tests, four chart/precision checks and four harness configuration checks. Focused lint/type checks and the integrated frontend build passed. The report tests include actual installed-library HTML for initial-loss and positive controls, plus unknown-layout handling.

Actual new API, CLI and real-browser runs now reconcile the 100-fill reference loss. The CLI verifier found and closed a metadata-loss bug in full-page exports (11 focused regressions); browser acceptance found and closed false-empty/stale-filter history behavior (four isolated browser regressions and real Retry/Refresh recovery). The actual managed harness also starts a fresh frontend successfully. The [candidate verification record](../../audits/2026-10-03/research-foundation-verification.md) records IDs, evidence and remaining acceptance. Unit fixtures or a healthy HTTP response alone do not close the accounting findings.

Keep failed reads distinct from zero records and avoid stale responses overwriting a newer filter. Explicit Retry/Refresh controls are sufficient here; no speculative polling framework or network retry policy is needed. Intermittent first-load transport errors remain unresolved unless their cause is separately demonstrated.
