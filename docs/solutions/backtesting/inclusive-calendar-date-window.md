# Inclusive backtest calendar dates

## Reproduced problem

At base `b48d0ad88c4ebcff498d2c14dc6fb834bea9c32a`, a backtest requested for December 2–3, 2024 passed December 3 midnight as its end to both Nautilus cutoffs. The installed NautilusTrader 1.223.0 uses inclusive `ts_init` bounds. Ordinary December 3 intraday bars were omitted; a December 3–3 backtest completed with no fills. The shared runner also serves research and portfolio member simulations.

Actual engine reproduction consumed one of three intended bars in a five-bar boundary fixture. Existing AAPL.NASDAQ data consumed 478 bars rather than 980 for December 2–3. A separate explicit-timestamp December 3 reference consumed 502 bars and produced 32 fills, ending flat with a recorded $1.13 gain on $1,000,000 capital. These observations concern this stored dataset and recorded zero fees, not session completeness, realistic costs or alpha.

## Repair contract

Calendar dates mean whole UTC days. Normalize the start to midnight and the end to the last nanosecond of its day, then supply those same integer bounds to `BacktestDataConfig` and `BacktestRunConfig`. Because Nautilus includes its end, the next day's midnight is excluded; adding a day without subtracting a nanosecond is incorrect. Explicit timestamps retain their instants, including timezone offsets. Invalid, reversed or out-of-range bounds fail clearly. Public backtest requests permit equal dates and reject reversed dates before enqueueing.

The forms and CLI describe the inclusive UTC meaning. The backtest form renders the API's Pydantic validation message without echoing the submitted request and retains its existing structured configuration/coverage error handling. Stored request dates stay unchanged.

New results expose optional `metrics.num_bars` from native engine iterations. This runner currently consumes bars only, so the count is total processed bars across all requested instruments, not a per-symbol completeness measure. A known zero displays as zero; historical results without the field display **Not recorded**. No saved result is rewritten and no schema migration is needed.

Research split, purge and portfolio slicing arithmetic remain unchanged. Their underlying simulations now consume their inclusive final days, so new results can legitimately differ from old results. An old completed job does not silently acquire corrected data or economics; rerun it when the corrected date contract matters.

## Local evidence and remaining acceptance

The candidate is in `fix/equity-backtest-window`, based on b48d0ad; it is not installed on Azure. Focused verification passed 44 owning schema/config/native-engine checks, 72 research controls and one portfolio slicing control, plus scoped lint/typing and frontend lint/build. The initial frontend font download failed; the identical unchanged production build subsequently passed. The original failure is retained.

Real local API job `bf106ecc-1e7d-4011-9e87-a3e41f7673a2` and CLI job `87171af1-2e92-4a08-a299-e0c7e96dfc02` completed with 502 bars, 32 fills and $1,000,001.13 final equity for December 3–3. Reversed dates were refused with actionable guidance and no added job. Subsequent public reads retained the dates/results and the prior 100-fill reference's canonical public JSON hash.

Actual sweep `eaf750c6-9cce-48f5-a65d-26a97e11920c` retained 980 training, 471 held-out diagnostic and 1,928 full-period bars. Walk-forward `68365971-bfca-4232-bf24-262139477190` retained 478 training, 477 held-out diagnostic and 471 test bars. Discovery retained training selection/configuration and the logical windows.

Preliminary native computer-use acceptance subsequently passed. The operator corrected a reversed range in the same form, submitted actual job `f83f5563-38c3-4b23-9875-6006e931c79e`, observed 502 bars/32 fills/+0.000113%, rendered its December 3 full report, and retained those results through reload and history reopening. The prior 100-fill row showed **Not recorded** for its absent count. The initial locked-Mac PARTIAL report is preserved alongside the resumed PASS report; native screenshots were emitted inline, without a saved-file claim.

The [graduated journeys](../../../tests/e2e/use-cases/backtests/inclusive-date-window.md) and [real browser regression](../../../frontend/tests/e2e/specs/inclusive-date-window-real.spec.ts) use real services and existing data. This document is the candidate-stage snapshot; exact-candidate final certification is recorded in local Forge receipts. Publication, integration and Azure acceptance remain separate gates. Detailed local evidence is under `.forge/local/evidence/equity-backtest-window/`. The [Master Map](../../../MASTER_MAP.md) and [Master Plan](../../../MASTER_PLAN.md) retain the release boundary and remaining Milestone 1 dependencies.
