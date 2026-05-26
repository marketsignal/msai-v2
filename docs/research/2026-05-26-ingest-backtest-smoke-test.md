# Research: ingest-backtest-smoke-test

**Date:** 2026-05-26
**Feature:** Operator-driven end-to-end ingest+backtest smoke producing structured risk metrics across CLI / UI / nightly schedule / opt-in pre-deploy preflight.
**Researcher:** research-first agent

---

## Libraries Touched

| Library                   | Our Version (pinned/installed)             | Latest Stable       | Breaking Changes since ours                                 | Source                                                                                                                                                                  |
| ------------------------- | ------------------------------------------ | ------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| databento (Python SDK)    | 0.71.0 (installed)                         | 0.71.0 (2026-02-18) | n/a — at latest                                             | [PyPI](https://pypi.org/project/databento/) (2026-05-26)                                                                                                                |
| nautilus_trader           | 1.223.0 (installed; pyproject `>=1.222.0`) | 1.226.0             | 1.224–1.226 not adopted                                     | [Release 1.223.0](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.223.0) (2026-05-26)                                                                 |
| quantstats                | 0.0.81 (installed)                         | 0.0.81 (2026-01-13) | n/a — at latest                                             | [PyPI / repo](https://github.com/ranaroussi/quantstats) (2026-05-26)                                                                                                    |
| GitHub Actions (platform) | n/a — workflow file dialect                | Current platform    | Boolean inputs + `schedule:` static-cron contract unchanged | [Boolean inputs](https://github.com/orgs/community/discussions/29796), [Cron from vars not supported](https://github.com/orgs/community/discussions/25960) (2026-05-26) |
| `prefers-reduced-motion`  | CSS standard                               | n/a                 | n/a                                                         | n/a — confirmed via project's `CLAUDE.md ## Visual Design Preferences`                                                                                                  |

The project's pinned NautilusTrader floor is `>=1.222.0`. The installed version in `backend/.venv` is `1.223.0`. The latest upstream is `1.226.0`. The smoke does not need to chase upstream — `1.223.0` already supports everything required (verified by reading the installed source).

---

## Per-Library Analysis

### 1. Databento Python SDK — `databento` 0.71.0

**Current API summary**

The smoke needs three things from the Databento SDK: (a) request OHLCV 1-minute bars for US equities (AAPL, SPY) on `EQUS.MINI`, (b) request OHLCV 1-minute bars for CME futures (ES) on `GLBX.MDP3` as a continuous front-month contract, (c) surface a clear remediation hint when an instrument is not entitled.

Confirmed call shape (already used by the project's `services/data_sources/databento_client.py`):

```python
client = databento.Historical(key=DATABENTO_API_KEY)
data = client.timeseries.get_range(
    dataset="EQUS.MINI" | "GLBX.MDP3",
    schema="ohlcv-1m",
    symbols=["AAPL"] | ["SPY"] | ["ES.c.0"],
    start="2024-12-01",
    end="2024-12-31",
    stype_in="raw_symbol" | "continuous",
)
df = data.to_df()
```

- **Equities (AAPL, SPY):** dataset `EQUS.MINI`, schema `ohlcv-1m`, `stype_in="raw_symbol"`, symbol `"AAPL"` / `"SPY"`. This is exactly the project's existing default — `settings.databento_equities_dataset = "EQUS.MINI"`, `settings.databento_default_schema = "ohlcv-1m"` (`backend/src/msai/core/config.py:55-57`).
- **Futures (ES):** dataset `GLBX.MDP3`, schema `ohlcv-1m`, `stype_in="continuous"`, symbol `"ES.c.0"` (Databento's proprietary continuous front-month rolling notation — `c` = volume-and-OI ranking, `0` = lead month). Confirmed by Databento's official symbology guide. The project's `_databento_stype_in()` helper already auto-detects the `^[A-Z]+\.[a-z]\.\d+$` pattern and switches to `"continuous"` (`databento_client.py:330-339`).
- **Standard plan + OHLCV coverage:** Standard tier ($199/month) provides **"Entire history in core schemas"** — and OHLCV-1m sits inside L0 ("Aggregate bars (second, minute, hour, day OHLCV)") per [databento.com/pricing](https://databento.com/pricing). The PRD's resolved assumption ("OHLCV is a core schema with entire-history coverage") is **CONFIRMED**. Both `smoke:fast` (1 month) and `smoke:nightly` (2024 full-year) fit under the existing subscription without a tier upgrade.
- **Tier matrix under Standard:** L0 = entire history; L1 = 12 months rolling; L2 = 1 month rolling; L3 = 1 month rolling. If the smoke is ever reshaped to use TBBO/trades (L1) or MBP-10/MBO (L2/L3) the window must shrink or the tier must upgrade.
- **Entitlement-error shape:** The SDK raises `databento.common.error.BentoClientError` with `http_status` in {401, 403, 429} and `BentoServerError` for 5xx. The project's existing client maps 401/403 → `DatabentoUnauthorizedError`, 429 → `DatabentoRateLimitedError`, 4xx/5xx other → `DatabentoUpstreamError` (see `databento_errors.py` and `databento_client.py:226-266`). For "ES is not in the operator's entitlement" the path is typically a 403 — already handled. Rate-limit policy already implemented: tenacity 3 attempts with 1s → 3s → 9s exponential backoff, retries 429 + 5xx only.
- **Rate limits:** Databento publicly states "Historical REST rate limits are not publicly documented" (per the project's existing inline comment at `databento_client.py:64-66`). The SDK auto-retries batch downloads but NOT `timeseries.get_range` — the project already wraps it manually.
- **0.71.0 release info (2026-02-18):** Added `slow_reader_behavior` to AuthenticationRequest, added compression support to the Live client constructor, downgraded pyo3 to 0.27.2. Removed `CBBOMsg` / `BBOMsg` from root package in favor of `databento_dbn` aliases; removed deprecated `packaging` parameter from `Historical.batch.submit_job`. **None of these affect `Historical.timeseries.get_range` or `ohlcv-1m`.**
- **Existing project adapter:** `backend/src/msai/services/data_sources/databento_client.py` (the project also has `polygon_client.py` — but per `CLAUDE.md` and MSAI memory, **Polygon is a dead-but-not-yet-ripped-out fallback and MUST NOT be referenced as a supported data source**).

**Sources**

1. [Databento Standard plan / tier matrix](https://databento.com/pricing) — accessed 2026-05-26
2. [Historical.timeseries.get_range API reference](https://databento.com/docs/api-reference-historical/timeseries/timeseries-get-range) — accessed 2026-05-26
3. [Continuous contract symbology (`ES.c.0`, `ES.v.0`)](https://databento.com/docs/examples/symbology/continuous) — accessed 2026-05-26
4. [databento-python 0.71.0 changelog](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) — accessed 2026-05-26
5. Project source: `backend/src/msai/services/data_sources/databento_client.py` (lines 64-339)
6. Project source: `backend/src/msai/core/config.py:55-57`

**Design impact**

- **No new client.** The smoke MUST reuse the existing `DatabentoClient`. Its symbol-type auto-detection (`_databento_stype_in`) already routes `ES.c.0` to `stype_in="continuous"`. The smoke config strings should be:
  - AAPL → `dataset="EQUS.MINI"`, `symbol="AAPL"`, `stype_in="raw_symbol"`
  - SPY → `dataset="EQUS.MINI"`, `symbol="SPY"`, `stype_in="raw_symbol"`
  - ES → `dataset="GLBX.MDP3"`, `symbol="ES.c.0"`, `stype_in="continuous"`
- **Remediation hint for missing ES entitlement** (PRD US-001 edge case): the entitlement failure surfaces as `DatabentoUnauthorizedError` (HTTP 403) from the existing pipeline. The CLI's structural-FAIL path should catch that specific exception and emit a hint pointing to broker.databento.com → Subscriptions; the design phase should NOT invent a new error class — wrap the existing one.
- **PRD Open Question CLOSED: "Databento tier coverage."** The PRD already marked this as resolved (Standard plan + entire-history OHLCV); research independently confirms this on `databento.com/pricing` (2026-05-26 fetch). Note in the design that the assumption remains valid only for OHLCV schemas; if the smoke is ever reshaped to L1+ this constraint flips.

**Test implication**

- Mock the `databento.Historical` client at the SDK boundary in unit tests; the project's existing pattern uses tenacity-aware retry, which means mock fixtures must throw real `BentoClientError(http_status=...)` and `BentoServerError(http_status=...)` shapes.
- An integration test with a real Databento key would be required to genuinely test the entitlement-failure path for ES, but this is operator-environment-dependent — keep that test marked `@pytest.mark.opt_in` and gated behind an env flag (akin to the existing `ib_paper` marker pattern).
- For the smoke's deterministic-trades floor (≥3 trades from `smoke_market_order` × 3 instruments), there's no Databento test seam — that lives in the Nautilus path.

**Open risks**

- The smoke depends on a future operator's Databento subscription continuing to entitle `EQUS.MINI` + `GLBX.MDP3` Standard tier. A silent downgrade would fail the smoke with the same 403 path → covered by existing remediation hint. **No mitigation needed at design time; surface in operator runbook.**
- Continuous symbology `ES.c.0` uses Databento's volume-and-OI ranking (`c` symbol). If Databento ever changes the ranking semantics (unlikely — it's documented and stable since 2024), trade counts could shift. **Out of scope for v1 — annotate as a metric-drift watch.**

---

### 2. NautilusTrader — `nautilus_trader` 1.223.0

**Current API summary**

The PRD's central open question (P7-Q1) is: does `POST /api/v1/backtests/run` accept a multi-strategy payload yielding a **single** Backtest row, or does the smoke need the `/api/v1/live-portfolios/` revision-frozen path with a backtest-mode flag, or does v1 fall back to 6 single-strategy backtests under a synthetic parent?

The Nautilus-side answer is unambiguous (verified by reading the installed library source):

- `BacktestEngineConfig` (subclass of `NautilusKernelConfig`) has a field `strategies: list[ImportableStrategyConfig] = []` (verified: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/system/config.py:71,119` and `backtest/config.py:324-376`). **One BacktestEngine + one BacktestRun can host N strategies in a single run.**
- `BacktestRunConfig` (`backtest/config.py:378-427`) accepts the `BacktestEngineConfig` as its `engine` field, plus `venues: list[BacktestVenueConfig]` and `data: list[BacktestDataConfig]`. A single run can therefore have:
  - N strategies
  - N venues (e.g., separate venues for equities `XNAS` and futures `GLBX`/`CME` — though the project pins everything to `SIM` for backtest per `.claude/rules/nautilus.md` gotcha #4)
  - N data configs (one per instrument)
- `BacktestNode(configs=[BacktestRunConfig])` runs N RunConfigs and returns `list[BacktestResult]` (verified: `backtest/node.py:76-120`, `backtest/results.py:20-40`).
- `BacktestResult` exposes per-venue PnL / returns (`stats_pnls: dict[str, dict[str, float]]`, `stats_returns: dict[str, float]`) but does NOT carry a per-strategy breakdown directly. **The per-strategy breakdown is reachable via `engine.trader.generate_orders_report()` / `generate_positions_report()`, which return DataFrames including a `strategy_id` column.** The project's existing `backtest_runner.py` already calls these (`backtest_runner.py:341-345`).

**Whether the existing project code already supports multi-strategy in `/api/v1/backtests/run`** is internal-design territory (PRD §7 marks this for code reading in the design phase). The Nautilus side imposes no architectural blocker: passing multiple `ImportableStrategyConfig`s in one `BacktestEngineConfig` is a first-class supported pattern.

**Sources**

1. [Nautilus 1.223.0 release notes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.223.0) — accessed 2026-05-26
2. [Backtest concepts documentation](https://nautilustrader.io/docs/latest/concepts/backtesting/) — accessed 2026-05-26
3. Installed source: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/backtest/config.py:324-427`
4. Installed source: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/system/config.py:71,119`
5. Installed source: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/backtest/results.py:20-40`
6. Existing project source: `backend/src/msai/services/nautilus/backtest_runner.py:241-345,547-630`
7. [Nautilus top-20 gotchas reference](file:///Users/pablomarin/Code/msai-v2/.claude/rules/nautilus.md) — pinned project doc

**Design impact**

- **PRD §7 Open Question on submission shape:** The Nautilus engine SUPPORTS the canonical multi-strategy → single Backtest row shape. **Whether `/api/v1/backtests/run` already accepts that payload, or needs a minor extension, is the design-phase question** — Nautilus is not a blocker. Design phase should read `backend/src/msai/api/backtests.py` + `backend/src/msai/schemas/backtest.py` + `backend/src/msai/workers/backtest_job.py` + `backend/src/msai/services/nautilus/backtest_runner.py` to decide between three options the PRD already enumerates (1 row multi-strategy / live-portfolios revision path / N single-strategy backtests under synthetic parent).
- **Per-strategy trade-count breakdown** (G5 requirement): NOT in `BacktestResult` directly — must be derived from `engine.trader.generate_orders_report()`'s `strategy_id` column, which the project's `backtest_runner.py` already extracts. The design must wire that per-strategy aggregation into the smoke's structured-metrics block.
- **Venue pinning matters** (`.claude/rules/nautilus.md` gotcha #4): If the smoke uses three different venues (`XNAS` / `XNAS` / `CME`/`GLBX`), every instrument MUST have a matching `BacktestVenueConfig`. Simplest path is pinning everything to `SIM` for the smoke. Design phase decision.
- **`generate_account_report(venue=...)`** is REQUIRED (gotcha #2). Existing `backtest_runner.py:341-345` already does this. No new design hazard.
- **uvloop policy gotcha** (gotcha #1): the smoke runs through the existing arq pipeline, which already has the uvloop fix in `workers/settings.py`. Not a new hazard.
- **Backtest fills are optimistic** (gotcha #14): the smoke is a smoke, not a strategy-validation test — optimistic fills are FINE. Do NOT introduce a FillModel for the smoke; document it.

**Test implication**

- Unit-test the metrics extraction logic (per-strategy trade counts from `orders_df`) using a synthetic `orders_df` fixture; don't run a real BacktestNode in unit tests.
- Integration test the multi-strategy run end-to-end with the existing test catalog. The `smoke_market_order` strategy's "exactly one market order per instrument on first bar" invariant gives a deterministic floor of 3 trades — assert that floor in the integration test.
- Verify the `strategy_id` column exists and is populated in `orders_df` when the run has 2+ strategies. The project's `FailureIsolatedStrategy` wrapper (`CLAUDE.md ## Portfolio-per-account`) should not interfere.
- Validate that `BacktestResult.stats_pnls` aggregates correctly across instruments under one venue.

**Open risks**

- **The PRD's "Backtest row" assumption** assumes one DB row per smoke run. If the design ends up at the "N single-strategy backtests under synthetic parent" fallback, the existing `/api/v1/backtests` history surface needs a parent-child concept — that's a non-trivial schema delta. **Reduce this risk in the design phase by confirming the existing payload shape in code BEFORE committing the design.**
- Per-strategy alpha/beta vs SPY is awkward when one of the portfolio strategies trades SPY itself (alpha vs self-as-benchmark for that strategy is non-meaningful). v1 reports portfolio-level alpha/beta vs SPY (PRD §7 already resolves this).
- Nautilus pinned floor is `>=1.222.0`; installed is `1.223.0`; latest is `1.226.0`. Upgrading to 1.224+ is out of scope for this feature but should be tracked as housekeeping (not blocking).

---

### 3. QuantStats — `quantstats` 0.0.81

**Current API summary**

The smoke needs a structured metrics block, NOT the HTML report. QuantStats provides this via `quantstats.reports.metrics(...)` and individual `quantstats.stats.*` functions.

Verified installed signatures (`backend/.venv/lib/python3.12/site-packages/quantstats/`):

- `quantstats.reports.metrics(returns, benchmark=None, rf=0.0, display=True, mode="basic", sep=False, compounded=True, periods_per_year=252, prepare_returns=True, match_dates=True, **kwargs)` — `reports.py:1135-1147`. With `display=False`, returns a `pd.DataFrame`/`dict` of metrics suitable for programmatic capture. `mode="full"` yields a wider set including drawdown-detail and rolling metrics.
- `quantstats.stats.sharpe(returns, rf=0.0, periods=252, annualize=True, smart=False)` — `stats.py:841`
- `quantstats.stats.sortino(returns, rf=0, periods=252, annualize=True, smart=False)` — `stats.py:982`
- `quantstats.stats.cagr(returns, rf=0.0, compounded=True, ...)` — `stats.py:1507`
- `quantstats.stats.max_drawdown(prices)` — `stats.py:2451`. NOTE: takes a **price/cumulative-returns** series, NOT period returns. Misuse here is a common bug.
- `quantstats.stats.greeks(returns, benchmark, periods=252.0, prepare_returns=True)` — `stats.py:2676`. Returns `pd.Series({'beta': ..., 'alpha': ...})`. Alpha is annualized by multiplying by `periods`.
- `quantstats.stats.comp(returns)` — total compounded return (final cumulative value) — `stats.py:99`.

**Library health:** v0.0.81 released 2026-01-13 ("Bugfixes for 0.0.78 release"). 32 total releases, Apache 2.0 license, Python 3.10+ required. **Actively maintained** as of 2026-01 — confirms MSAI memory note ("quantstats is the current pick; pyfolio dead since 2019").

**Alternative: `empyrical`** is the legacy successor to pyfolio's metric math; it is also effectively unmaintained (last release 2020). **No reason to consider it.**

**Existing project usage:** `backend/src/msai/services/report_generator.py:88` already calls `qs.reports.html(...)` for the HTML tearsheet. The smoke needs the programmatic `qs.reports.metrics(..., display=False)` plus direct `qs.stats.greeks(...)` for alpha/beta — neither is wired today. The QuantStats compounding-from-minute-bars gotcha is already handled by `_normalize_report_returns()` (`report_generator.py:67-74`) — the design must reuse that helper.

**Sources**

1. [QuantStats GitHub repo (Apache 2.0)](https://github.com/ranaroussi/quantstats) — accessed 2026-05-26 (latest release 0.0.81, 2026-01-13)
2. [QuantStats stats.py source](https://github.com/ranaroussi/quantstats/blob/main/quantstats/stats.py) — accessed 2026-05-26
3. Installed source: `backend/.venv/lib/python3.12/site-packages/quantstats/stats.py:841,982,1507,2451,2676`
4. Installed source: `backend/.venv/lib/python3.12/site-packages/quantstats/reports.py:1135-1147`
5. Existing project source: `backend/src/msai/services/report_generator.py:31-105`

**Design impact**

- **Single API path for the structured block:** call `qs.reports.metrics(returns, benchmark=SPY_returns, display=False, mode="full")` to capture a DataFrame of standard metrics, then complement with `qs.stats.greeks(returns, SPY_returns)` for alpha/beta. **Do NOT recompute these by hand** — KISS, use library output.
- **Periods-per-year arithmetic:** QuantStats defaults assume daily returns (252 trading days). The minute-bar pipeline must compound to daily before any metric calculation (existing `_normalize_report_returns()` handles this — design must call it on the smoke's metric path too, not just the HTML path).
- **`max_drawdown` takes prices, not returns** — easy footgun. Convert with `qs.stats.to_drawdown_series(returns)` or use the value returned by `qs.reports.metrics()` (which handles this internally).
- **Benchmark plumbing:** SPY is one of the smoke's three traded symbols. The benchmark series for alpha/beta is the SPY OHLCV-1m → daily-returns series — already on disk by the time the backtest finishes. The smoke must load it from the Parquet catalog (via the existing market-data service), NOT re-fetch from Databento.
- **PRD G5 (structured metrics block) mapping:**

  | PRD G5 metric       | QuantStats source                                                        |
  | ------------------- | ------------------------------------------------------------------------ |
  | Total return        | `qs.stats.comp(returns)`                                                 |
  | P&L                 | derived from `BacktestResult.stats_pnls` (Nautilus side, not QuantStats) |
  | Sharpe              | `qs.stats.sharpe(returns)` or `metrics()['Sharpe']`                      |
  | Sortino             | `qs.stats.sortino(returns)` or `metrics()['Sortino']`                    |
  | Alpha vs SPY        | `qs.stats.greeks(returns, spy_returns)['alpha']`                         |
  | Beta vs SPY         | `qs.stats.greeks(returns, spy_returns)['beta']`                          |
  | Max drawdown        | `metrics()['Max Drawdown']`                                              |
  | Trade count / strat | Nautilus `orders_df` groupby `strategy_id` (not QuantStats)              |

**Test implication**

- Stub `qs.reports.metrics` / `qs.stats.greeks` is unnecessary — they're pure functions on pandas. Unit-test the metrics extractor with a fixed synthetic returns + benchmark series; assert exact metric values match a captured baseline.
- Test the minute-bar → daily-compounded conversion against a known fixture to prove the Sharpe-inflation footgun is closed.
- For the alpha/beta vs SPY path, include a unit test where `returns == benchmark` to assert `beta ≈ 1.0` and `alpha ≈ 0.0` (sanity check).
- The `Persistence` E2E step for CLI use cases should re-invoke the smoke with the same Parquet on disk and confirm metric values are deterministic (or document any acceptable random-seed jitter — none expected for backtests on fixed data).

**Open risks**

- QuantStats `reports.metrics()` is documented but its return shape (DataFrame vs Series vs dict) varies by mode; verify experimentally during implementation. Have a fallback to direct `qs.stats.*` calls if the metrics() return shape changes between versions.
- The library is on a single-maintainer fork pattern (`ranaroussi/quantstats`); fork drift to `joedenis/quantstats` exists but is not adopted. Stay on the main fork.

---

### 4. GitHub Actions — `workflow_dispatch` + `workflow_run` + `schedule:`

**Current API summary**

The smoke needs:

- **US-003: nightly schedule via `schedule:` cron.** The PRD demands the cron string be "held in a single repo Variable so it can be retuned without code change."
- **US-004: opt-in pre-deploy preflight via `workflow_dispatch` boolean input on `deploy.yml`.** Must coexist with the existing `workflow_run` trigger from `build-and-push.yml`.

Findings:

- **`workflow_dispatch` `type: boolean`:** GitHub supports `boolean` as an input type (since 2021-11). HOWEVER — when accessed via the `github.event.inputs` context, the value is a **string `"true"`/`"false"`**, NOT a real boolean. The `inputs` context (lowercase, not `github.event.inputs`) DOES preserve real booleans for `workflow_call` but **still passes strings for `workflow_dispatch`**. Conditional gating must therefore use `if: ${{ github.event.inputs.run_smoke == 'true' }}` — **the project's existing `deploy.yml` already uses this pattern** (e.g., `inputs.bootstrap` at deploy.yml:71-75 is `type: boolean, default: false`).
- **`schedule:` cron from `vars.<X>`:** **NOT SUPPORTED.** GitHub Actions processes the workflow file at parse-time, before `vars` are available, so `cron: ${{ vars.SOMETHING }}` errors with `Unrecognized named-value: 'vars'`. **This breaks PRD US-003 AC #1** ("cron configurable via a single repo Variable so it can be retuned without a code change").

  Workarounds (in increasing order of complexity):
  1. **Hard-code the cron and accept a code change to retune** — simplest, fits MSAI's "brutal simplicity" principle. Pablo is the only operator; PR friction is low.
  2. **Define multiple `schedule:` entries and gate on `github.event.schedule == '<expr>'`** in conditional `if:` — clutter, doesn't actually retune dynamically.
  3. **Use `repository_dispatch` + an external cron** — overengineering for a personal-fund operator.

  **Recommendation:** Accept #1. Document the cron at the top of the workflow file and the PRD ASSUMPTION should be downgraded.

- **`workflow_run` chain:** the project's existing `deploy.yml` already triggers on `workflow_run` from "Build and Push Images" (deploy.yml:27-30). Adding a `workflow_dispatch` opt-in alongside is the project's current pattern (deploy.yml:31-76). The PRD's US-004 design fits this pattern unchanged.
- **`workflow_dispatch` job gating:** confirmed pattern at deploy.yml:91:
  ```yaml
  if: ${{ github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success' }}
  ```

**Sources**

1. [GitHub Actions input types changelog (2021-11)](https://github.blog/changelog/2021-11-10-github-actions-input-types-for-manual-workflows/) — accessed 2026-05-26
2. [Discussion: Boolean inputs are strings via `github.event.inputs`](https://github.com/orgs/community/discussions/29796) — accessed 2026-05-26
3. [Discussion: Dynamic cron from `vars` not supported](https://github.com/orgs/community/discussions/25960) — accessed 2026-05-26
4. Project source: `.github/workflows/deploy.yml:26-100`

**Design impact**

- **PRD US-003 AC #1 ("cron configurable via repo Variable") needs revision.** Recommend wording change to: _"The cron is a literal `0 5 * * *` at the top of the workflow file; retuning requires a small PR. Operator workflow accepts this trade-off."_ This should be flagged in the design phase's plan-review with a one-line patch to the PRD wording.
- **PRD US-004 boolean gate** works directly with the project's existing pattern. Use `if: ${{ github.event.inputs.run_smoke == 'true' }}` for the preflight job — NOT `inputs.run_smoke` (which would silently always evaluate `false` in `workflow_dispatch`).
- **No new credential model needed** — both US-003 and US-004 reuse the existing `deploy.yml` OIDC + transient-NSG-rule SSH chain.

**Test implication**

- The workflow files are testable via `actionlint` (already in the project per MSAI memory note about Slice 2). Add the new nightly workflow + the deploy.yml `run_smoke` branch to the actionlint coverage.
- The CI minute budget impact (PRD §5 platform constraint): the nightly path runs ON the VM (not on the runner) — runner only orchestrates SSH, ~1 minute of runner time per nightly. The opt-in preflight is the only non-trivial runner-minute cost.
- E2E for US-003 / US-004 is at the workflow level — verify the dispatch boolean gates the job correctly (e.g., `act` locally or `gh workflow run deploy.yml -f run_smoke=false` and observe that the preflight step is skipped).

**Open risks**

- **Critical: PRD US-003 ACs assume dynamic cron via Variables.** This is impossible on GitHub Actions. The PRD needs a v1.2 amendment or the design must explicitly downgrade the wording. Surface this in the plan-review loop.
- `workflow_run`-triggered jobs do NOT have `github.event.inputs` (no manual dispatch); the conditional must be defensive about that path (the existing deploy.yml already handles this).
- A future GitHub change could promote `inputs.<x>` to real boolean for `workflow_dispatch`; if so, the `== 'true'` string-comparison pattern would still work (forward-compatible).

---

### 5. `prefers-reduced-motion` + shadcn/ui motion patterns (UI button)

**Current API summary**

The UI button (US-002) is small in scope — a single button on the existing `/backtests` page. The MSAI `CLAUDE.md ## Visual Design Preferences` mandates `prefers-reduced-motion` handling, but explicitly **scopes the "always include at least one dynamic element" rule to hero sections and key visual moments** — NOT to incidental controls.

- shadcn/ui's existing primitives in this project (`frontend/src/components/ui/`) use Tailwind CSS animation utilities (`animate-spin`, `animate-pulse`, transition classes) that respect `prefers-reduced-motion: reduce` via Tailwind's built-in `motion-safe:` / `motion-reduce:` variant prefixes.
- A submit button with a brief pending spinner is the standard pattern; no bespoke animation work is required.

**Sources**

- Project source: `CLAUDE.md ## Visual Design Preferences` (in the worktree CLAUDE.md context above)
- shadcn/ui standard patterns (well-known; no fetch required for this scope)

**Design impact**

- The "Run smoke" button does not need new motion design. Reuse the project's existing button primitive with the standard pending-state pattern (e.g., `<Loader2 className="animate-spin" />` from `lucide-react`). Tailwind's `motion-reduce:` variant covers the `prefers-reduced-motion` requirement.
- No design impact beyond reusing the existing component.

**Test implication**

- E2E UC for US-002 (per `.claude/rules/testing.md`) verifies the click → row-in-history journey, not the button animation. No specific motion-test required.
- Standard coverage sufficient.

**Open risks**

- None.

---

## Not Researched (with justification)

- **Internal `/api/v1/backtests/run` payload shape** — PRD §7 explicitly defers to design-phase code reading; out of research scope per the brief.
- **Internal arq job + worker plumbing** — same as above.
- **Internal `/api/v1/alerts/` schema (kind=smoke-result)** — same as above.
- **Internal Backtest DB model** — same as above.
- **Strategy code (`smoke_market_order.py`, `ema_cross.py`)** — already inspected by PRD authors; not new research territory.
- **NautilusTrader 1.224 / 1.225 / 1.226** — current pinned floor (>=1.222.0) and installed (1.223.0) are sufficient for the feature; upgrade is housekeeping, separate from this feature.
- **Polygon Python SDK** — `CLAUDE.md` explicitly prohibits Polygon as a data source. Databento is the only supported provider for this feature.
- **Recharts / TradingView Lightweight Charts** — the smoke UI button does NOT render a new chart (PRD US-002 reuses the existing backtest details view).

---

## Open Risks (cross-target)

1. **PRD US-003 cron-via-Variable is incompatible with GitHub Actions.** Hard-code the cron and downgrade the PRD wording; design phase must catch and patch. (Risk owner: design phase plan-review loop.)
2. **The PRD's "multi-strategy → single Backtest row" submission shape** is a Nautilus-feasible pattern but its mapping onto the existing `/api/v1/backtests/run` payload is unverified. If the existing endpoint can't accept it, the fallback (N single-strategy backtests under synthetic parent) introduces a parent-child DB concept and bloats scope. **Read `api/backtests.py` + `schemas/backtest.py` early in design.**
3. **Per-strategy alpha/beta is a confused metric** when the strategy trades the benchmark itself (e.g., a strategy that holds SPY has alpha vs itself). v1 reports only PORTFOLIO-level alpha/beta vs SPY — design must be explicit that no per-strategy alpha is computed.
4. **QuantStats `reports.metrics()` return shape varies by mode and version.** Validate the shape during implementation; fall back to direct `qs.stats.*` calls if it changes. Pin `quantstats` to `>=0.0.81,<0.1` (current behavior).
5. **Databento SDK 0.71.0 → future** breaking changes are minor (and project is already on latest), but the `Live` client refactor (compression support) is upcoming churn territory. The smoke uses Historical only, so this is not a v1 risk.
6. **No mitigation needed for `prefers-reduced-motion`** — Tailwind's built-in variant covers it.
7. **No mitigation needed for Nautilus uvloop, account-report, IB-port, or instrument pre-load gotchas** for this feature — backtest path only, no IB Gateway, no live trading. The smoke is well inside the safe envelope of the existing backtest pipeline.
