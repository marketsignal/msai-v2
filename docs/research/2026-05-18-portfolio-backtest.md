# Research: portfolio-backtest

**Date:** 2026-05-18
**Feature:** Portfolio-level backtest engine (Quick + Full/walk-forward) with form-based composer, allocators, and one-click "Deploy as Live Portfolio."
**Researcher:** research-first agent
**Predecessor:** [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../decisions/2026-05-17-portfolio-backtest-deferred.md)

---

## Executive Summary (read first)

1. **The optimizer is already in-house.** `backend/src/msai/services/research_engine.py` already implements grid / successive-halving / **Optuna TPE** with walk-forward (`build_walk_forward_windows`), holdout/purge, IS-OOS gap (`generalization_gap`, `stability_ratio`), and `progress_callback` plumbing. **No new optimizer library is needed.** The design phase should extend this engine for the portfolio case, not replace it.
2. **The Nautilus multi-strategy primitive exists.** `BacktestRunConfig.strategies = [N ImportableStrategyConfig]` is the documented path; `Cache.positions(strategy_id=...)` and `Cache.position_ids(strategy_id=...)` give us per-strategy P&L attribution **natively** — no hand-rolled aggregator.
3. **Per-strategy realized-PnL is not on the `Portfolio` object.** `Portfolio.realized_pnls()` takes `(venue, account_id)` only — it has no `strategy_id` axis. To produce per-strategy contribution, **iterate `Cache.positions(strategy_id=sid)` and sum `position.realized_pnl`** (this matches `nautilus-natives-audit.md` § D, "Per-strategy aggregation — we must build it").
4. **QuantStats has no multi-portfolio reporter.** GitHub issue #161 confirms "one report for one portfolio." For per-strategy QuantStats sub-reports, call its API N times against the per-strategy equity series we already aggregate ourselves. The maintained fork `quantstats-lumi` exists but **stay on upstream `quantstats>=0.0.81`** (already pinned) — switching is out of scope.
5. **Frontend: add `nivo/heatmap` + a shadcn `<Command>`-based multi-select.** Recharts (already in stack) has **no heatmap primitive**. `@nivo/heatmap` 0.99 supports React 19 and is the standard choice. The shadcn ecosystem has no shipped multi-select primitive; the recommended pattern is `Command + Popover + Badge` chips (the "shadcn-expansions MultipleSelector" pattern). No new heavy chart library required for parallel-coordinates yet — discuss in design.
6. **Drawdown correlation is non-standard.** Industry consensus (RealTest / AlgoTest / Build Alpha): compute it on the **per-strategy underwater (drawdown) time-series**, then take Pearson on those series. A more sophisticated form ("Drawdown-Implied Correlation") exists in academic literature; defer to design.

---

## Libraries Touched

| Library                                                      | Our Version                    | Latest Stable        | Breaking Changes Since Ours                                                                                                                  | Source                                                                                                                                                                                         |
| ------------------------------------------------------------ | ------------------------------ | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| optuna                                                       | `>=4.4.0`                      | **4.8.0** (Mar 2026) | None affecting our usage (TPE + ask/tell + JournalStorage). `consider_prior` already deprecated pre-4.4.                                     | [Optuna 4.8 docs](https://optuna.readthedocs.io/en/stable/) (2026-05-18)                                                                                                                       |
| nautilus_trader[ib]                                          | `>=1.222.0`                    | **1.225.0** (Apr 6)  | 1.223 introduced **multi-account execution** and explicit per-strategy P&L docs; 1.225 removed deprecated `convert_quote_qty_to_base`.       | [NautilusTrader RELEASES.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/RELEASES.md) (2026-05-18)                                                                          |
| quantstats                                                   | `>=0.0.81`                     | **0.0.81** (stale)   | None. Project is **slow-maintained** — open PRs since 2024. No multi-portfolio reporter.                                                     | [quantstats PyPI](https://pypi.org/project/quantstats/) (2026-05-18); [issue #161 multi-asset reports](https://github.com/ranaroussi/quantstats/issues/161)                                    |
| arq                                                          | `>=0.26.0`                     | **0.28.0** (current) | None affecting our usage. `Worker.__init__` event-loop bug already worked around (gotcha #1 in `nautilus.md`).                               | [arq docs](https://arq-docs.helpmanual.io/) (2026-05-18); [PR #449 cancellation](https://github.com/python-arq/arq/issues/449)                                                                 |
| @nivo/heatmap (NEW)                                          | n/a                            | **0.99.0** (~1y)     | n/a (new add). React 19 supported per [issue #2618](https://github.com/plouc/nivo/issues/2618). Install size ~800 KB.                        | [npm @nivo/heatmap](https://www.npmjs.com/package/@nivo/heatmap) (2026-05-18)                                                                                                                  |
| shadcn `<Command>`/Combobox + chip (NEW pattern, no new dep) | implicit via `radix-ui ^1.4.3` | n/a                  | Use existing `radix-ui`; add a project-local `multi-select.tsx` modeled on shadcn-expansions MultipleSelector.                               | [shadcn Combobox docs](https://ui.shadcn.com/docs/components/radix/combobox) (2026-05-18); [shadcn-expansions MultipleSelector](https://shadcnui-expansions.typeart.cc/docs/multiple-selector) |
| In-house `research_engine.py`                                | n/a (internal)                 | n/a                  | Already supports walk-forward + holdout + Optuna + successive-halving + grid. **Single-strategy contract today** — must extend to portfolio. | [`backend/src/msai/services/research_engine.py`](../../backend/src/msai/services/research_engine.py)                                                                                           |
| skopt / hyperopt / DEAP / Ray Tune / cmaes                   | not used                       | various              | n/a — not added. See "Not Researched (Adopted)" below.                                                                                       | various                                                                                                                                                                                        |

---

## Per-Library Analysis

### 1. optuna (the optimizer)

**Versions:** ours `>=4.4.0`, latest **4.8.0** (released 2026-03-16). `research_engine.py` already constructs `create_study(sampler=TPESampler(), storage=JournalStorage(JournalFileBackend(...)), load_if_exists=True)` and uses the ask/tell loop with `study.tell(trial, state=TrialState.PRUNED|FAIL)`.

**Breaking changes since 4.4:** None relevant. The ask/tell API surface in 4.8 is identical to what we already use. GP-Sampler picked up multi-objective in 4.4 (not used by us).

**Maintenance:** Highly active. Optuna 4.6 (Nov 2025), 4.7 (Jan 2026), 4.8 (Mar 2026); v5 roadmap published. ASHA pruner is first-class.

**Recommended pattern (for portfolio Full mode):**

- Reuse `ResearchEngine._run_optuna_parameter_sweep` as the template — extend it so the per-trial run is a **portfolio backtest** (multi-strategy Nautilus run with the trial's risk-policy params applied per member), not a single-strategy backtest.
- Use **`MedianPruner` or `SuccessiveHalvingPruner`** for the 8h budget — Optuna ASHA pruner can stop a trial after the first walk-forward window if its IS objective is below median.
- Use **`TPESampler(constant_liar=True)`** if we go parallel (n_jobs>1) so concurrent workers don't sample the same neighborhood (Optuna 4.8 tutorial explicitly recommends this for batch).
- Use **`JournalFileBackend`** (already in use) for resume — survives worker crash. Trial history is on disk in `settings.optuna_root`.
- For **cancellation**: arq has `Job.abort()`. The Optuna trial loop must check a cancel flag between trials and call `study.tell(trial, state=TrialState.FAIL)` to mark the in-flight trial cleanly before exiting (otherwise the journal sees a pending trial on resume).

**Sources:**

1. [Optuna 4.8 ask-and-tell tutorial](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) — accessed 2026-05-18
2. [Optuna 4.8 distributed/parallelization](https://optuna.readthedocs.io/en/stable/tutorial/10_key_features/004_distributed.html) — accessed 2026-05-18
3. [Optuna releases page](https://github.com/optuna/optuna/releases) — accessed 2026-05-18

**Design impact:** Pin Optuna's role to **objective evaluation = portfolio backtest output**, not single-strategy. Decide pruner choice (Median vs SuccessiveHalving vs no-pruner) by walk-forward window count in the design phase. Add a cancel-flag check inside the trial loop. Storage path stays on disk (no DB change).

**Test implication:** Unit-test trial-cancellation cleanup (no orphaned `RUNNING` trials in the journal after abort). Add a fuzz test that resumes a half-completed study and verifies trial count is preserved.

---

### 2. NautilusTrader (multi-strategy BacktestNode + per-strategy attribution)

**Versions:** ours `>=1.222.0`, latest **1.225.0** (2026-04-06). The 1.224 release added matching-engine L1 quote-based queue position tracking — relevant for FillModel realism but not breaking. 1.223 added multi-account execution; we already handle one account per backtest so no migration.

**Breaking changes since 1.222 that touch this feature:**

- **1.225:** Several PyO3 getters changed to methods (`Position.events()`, `adjustments()`). Verify our codepaths in `backend/src/msai/services/nautilus/backtest_runner.py` and `parity/normalizer.py` do not use the old attribute style on `Position` once we upgrade.
- **1.225:** Removed `convert_quote_qty_to_base` from execution engine config — we do not use this.
- **1.224:** L1 quote queue position tracking is opt-in via `MatchingEngineConfig.fill_limit_inside_spread`; **does not auto-enable** but materially improves backtest realism (relevant to gotcha #14: backtest fills are optimistic). Worth designing in for Quick mode.

**Maintenance signal:** Beta tag still, but rapid steady cadence — ~one minor every 5–6 weeks. Backed by Nautech Systems commercially.

**Multi-strategy semantics (confirmed by venv source read):**

- One `BacktestRunConfig` accepts `strategies: list[ImportableStrategyConfig]`. All run **in the same engine, sharing the same `Cache` and `MessageBus`** — this is the **single-portfolio shared-equity model** that AmiBroker and Build Alpha v3 use. Per the discussion log this is also what MSAI wants.
- N concurrent strategies in one engine are processed **deterministically by event time**, not in parallel threads. That means a 3-strategy run is approximately the cost of one strategy on the same data — minus the per-strategy decision cost.
- **The engine is single-process and stateful** — `backtest_runner.py` already spawns a subprocess per run (the comment at line 8-11 explains why: "Nautilus maintains global Rust/Cython state per process"). Reuse that subprocess-per-run pattern; do **not** try to parallelize trials inside one process.

**Per-strategy P&L attribution (the key architecture question):**

- ✅ **Available natively:** `Cache.positions(strategy_id=sid)`, `Cache.position_ids(strategy_id=sid)`, `Cache.orders(strategy_id=sid)` — all accept a `strategy_id` filter (verified at `cache/base.pyx:282-510`).
- ❌ **NOT available natively:** `Portfolio.realized_pnls()` and `unrealized_pnls()` take `(venue, account_id)` only — no `strategy_id` parameter. To compute per-strategy realized P&L, **iterate `Cache.positions(strategy_id=sid)` after the run completes** and sum `position.realized_pnl`.
- ✅ **`Cache.strategy_ids()` enumerates all registered strategies** — use this as the iteration source rather than hard-coding the input list (defends against rename bugs).
- This matches `docs/nautilus-natives-audit.md` § D ("Per-strategy aggregation — Nautilus tracks per-position; we sum across positions where `position.strategy_id == target`").

**Per-strategy equity curve (needed for drawdown correlation and contribution chart):**

- Nautilus does **not** ship a per-strategy equity time-series natively. The engine's combined equity is captured by `PortfolioAnalyzer` after the run. To get per-strategy curves, we must subscribe to `PositionOpened|Changed|Closed` events during the run, attribute them by `position.strategy_id`, and build the time-series ourselves.
- Cheaper alternative: replay the **trade list** post-run (already exposed in our `BacktestResult.orders_df` / `positions_df`) — group by strategy_id, mark-to-market each strategy's equity along the bar timeline. This is post-processing; no engine modification needed.

**Sources:**

1. [NautilusTrader 1.225 release notes (RELEASES.md)](https://github.com/nautechsystems/nautilus_trader/blob/develop/RELEASES.md) — accessed 2026-05-18
2. [Backtesting concepts (multi-strategy ImportableStrategyConfig)](https://nautilustrader.io/docs/latest/concepts/backtesting/) — accessed 2026-05-18
3. [Portfolio API reference](https://nautilustrader.io/docs/nightly/concepts/portfolio/) — accessed 2026-05-18
4. venv source: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/cache/base.pyx:282-510` and `nautilus_trader/portfolio/portfolio.pyx:877` — verified 2026-05-18
5. Existing audit: [`docs/nautilus-natives-audit.md`](../nautilus-natives-audit.md) § D

**Design impact (critical):**

1. **No new aggregation class** — the per-strategy contribution viz reads `Cache.positions(strategy_id=sid)` and our existing `BacktestResult.positions_df` (the latter already attributes `strategy_id` since each position carries it).
2. **Per-strategy equity time-series is a post-run computation** — design a `PerStrategyEquityBuilder` that consumes `positions_df` + bar timestamps, not a runtime hook.
3. **Stay on shared-equity engine model** (one engine, N strategies, one cash account) — matches AmiBroker / Build Alpha v3 / industry consensus. Do not create N engines and stitch results.
4. **Subprocess-per-portfolio-backtest stays mandatory** — Nautilus global state. The optimizer trial loop therefore costs `trial_count × subprocess_spawn` — budget accordingly (Full-mode 8h cap should anticipate this).
5. **Plan the 1.222→1.225 upgrade explicitly** (or stay on 1.222 for this PR — the council's "No Bugs Left Behind" wants the upgrade in scope; design phase should choose).

**Test implication:**

- Unit-test the per-strategy P&L aggregator with a 2-strategy fixture whose positions and trades are pre-computed.
- Integration test: run a 2-strategy multi-day fixture and assert `sum(per_strategy_pnl) ≈ portfolio_realized_pnl` within rounding tolerance.
- Per-strategy equity series must monotonically integrate to the combined curve (sum at each timestamp).

---

### 3. QuantStats (reports, correlation helpers)

**Versions:** ours `>=0.0.81`, latest **0.0.81** (effectively frozen since 2024). The `quantstats-lumi` fork is the maintained alternative but is **out of scope** for this PR (council "No Bugs Left Behind" applies to discovered defects, not to opportunistic library swaps).

**Multi-portfolio reporting:**

- **Native support: NO.** [Issue #161](https://github.com/ranaroussi/quantstats/issues/161) confirms "quantstats can only make one report for one portfolio." The library expects a single returns series.
- **Workaround pattern:** aggregate per-strategy returns to a single portfolio-level returns series ourselves, then pass to `quantstats.reports.html()`. For per-strategy sub-reports (US-003 stretch), call `quantstats` N times with each strategy's series.

**Correlation helpers:** QuantStats exposes `quantstats.stats.compare()` and basic correlation utilities, but they operate on the single-series model and are **not the right tool** for a strategy × strategy matrix. Use **`pandas.DataFrame.corr()`** (Pearson) on a strategies-wide return frame for the return-correlation matrix; use Spearman as a secondary option if the design phase calls for non-linear robustness. Both are stdlib-grade.

**Sources:**

1. [QuantStats GitHub](https://github.com/ranaroussi/quantstats) — accessed 2026-05-18
2. [Issue #161 multi-asset reports](https://github.com/ranaroussi/quantstats/issues/161) — accessed 2026-05-18
3. [quantstats-lumi fork PyPI](https://pypi.org/project/quantstats-lumi/) — accessed 2026-05-18

**Design impact:** Build the portfolio-level returns series **before** calling QuantStats. Use `pandas.DataFrame.corr()` (Pearson by default; expose Spearman as a config flag) for the return correlation matrix. Do **not** invent a multi-portfolio QuantStats wrapper.

**Test implication:** Snapshot-test the generated HTML report for shape only (size, key headings); do not assert on QuantStats internals. Test correlation values against a hand-computed 3×3 fixture.

---

### 4. Walk-forward / cross-validation (in-house `research_engine.py`)

**Versions:** internal. Lives at `backend/src/msai/services/research_engine.py` (1397 LOC). Capabilities verified by direct read:

- ✅ `build_walk_forward_windows(start_date, end_date, train_days, test_days, step_days, mode="rolling"|"expanding")` — produces train/test window list with proper boundary check (raises if no windows fit).
- ✅ `resolve_train_holdout_split(...)` — IS/holdout split with purge gap (López de Prado style).
- ✅ `ResearchEngine.run_walk_forward(...)` — orchestrates per-window training sweep + OOS evaluation; returns a payload with `windows: [...]`, `summary: {avg_train_sharpe, avg_test_sharpe, generalization_gap, stability_ratio, best_config_consistency, ...}`.
- ✅ `generalization_gap()` and `stability_ratio()` — the IS-OOS gap math the PRD requires (US-002b acceptance).
- ✅ `_run_optuna_parameter_sweep(...)` — Optuna TPE branch with optional holdout, JournalFileBackend resume, and progress callback.
- ✅ `progress_callback` plumbing — the arq worker can wire status updates with `progress=10..98` and `completed_trials/total_trials`.

**Gaps for the portfolio case:**

- ❌ **It's strategy-singular.** Every helper takes `strategy_path: str` and `parameter_grid: dict`. For portfolio mode we need a **list of (strategy_path, parameter_grid)** plus the allocator config, and the "objective" must be computed off the **combined portfolio equity**, not a single-strategy metric.
- ❌ **`BacktestRunner.run()` takes a single strategy file.** Need a new entry point — let's call it `PortfolioBacktestRunner` — that constructs N `ImportableStrategyConfig` entries and the multi-strategy `BacktestRunConfig`.
- ❌ **No allocator-aware capital weighting in the harness today.** The allocator output (per-member weights) needs to land on the per-member `ImportableStrategyConfig.config["allocation"]` so the strategy's position sizing can read it. Design phase decides where the allocation hook lives.
- ❌ **No per-strategy hard caps in the search space.** The PRD wants `max_leverage / max_position_size / max_drawdown_halt` as **portfolio-level hard bounds**. Today the search space is just `parameter_grid: dict[str, list[Any]]`. The portfolio extension must either (a) clip the search space at construction time or (b) reject infeasible trials and log them — design decides.

**Sources:**

1. Source read: [`backend/src/msai/services/research_engine.py`](../../backend/src/msai/services/research_engine.py) — accessed 2026-05-18
2. Existing tests under `backend/tests/unit/services/research_engine/` — accessed 2026-05-18

**Design impact:** Extend `research_engine.py` to portfolio mode; do **not** create a parallel walk-forward implementation. Likely shape: introduce `run_portfolio_walk_forward(*, portfolio_revision_id, member_specs, allocator_config, objective_function, safety_caps, ...)`; reuse `build_walk_forward_windows`, `_run_optuna_parameter_sweep`, `generalization_gap`, `stability_ratio`, `best_config_consistency` verbatim. Replace `_run_one` body with a portfolio backtest call.

**Test implication:** The existing walk-forward unit tests give a regression floor. New tests should cover (a) safety-cap enforcement (no trial breaches `max_leverage`), (b) allocator weight application (members receive the computed weights), (c) generalization gap exposure in the result payload.

---

### 5. arq (job queue for 8h optimization runs)

**Versions:** ours `>=0.26.0`, latest **0.28.0**. No breaking changes affecting us.

**Capability matrix for Full mode:**

| PRD requirement           | arq support                                                                                                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8h-budget single job      | ✅ `job_timeout` parameter; `keep_result` retention                                                                                                                                    |
| Progress reporting        | ⚠️ No native progress channel. **Pattern in MSAI:** worker writes `progress`/`message`/`completed_trials` columns on the DB row (see `research_job.py` parallel). Frontend polls.      |
| Cancellation mid-flight   | ✅ `Job.abort()` requires `allow_abort_jobs=True` on worker; raises `asyncio.CancelledError` inside the task. Task must clean up (Optuna study tell-with-FAIL, subprocess SIGTERM).    |
| Resume after worker crash | ✅ Pessimistic execution — unfinished jobs stay in queue and re-run on worker restart. **BUT** Optuna trial state needs idempotent resume — use `JournalFileBackend` (already in use). |
| 8h ceiling not exceeded   | ✅ `job_timeout` enforces. Best to set Optuna `target_trials` such that the timeout is the safety net, not the primary stop signal.                                                    |

**Known issues:**

- arq stale-job recovery: if a worker SIGKILLs (not SIGTERM), the job stays in the in-progress set and isn't auto-requeued for up to `job_timeout` seconds. ([Issue #343](https://github.com/python-arq/arq/issues/343))
- Cancellation race: between `Job.abort()` and the task seeing `CancelledError`, the task may complete one more trial. The trial-loop must check the cancel flag at the **top** of each iteration.

**Sources:**

1. [arq docs](https://arq-docs.helpmanual.io/) — accessed 2026-05-18
2. [Cancellation issue #449](https://github.com/python-arq/arq/issues/449) — accessed 2026-05-18
3. [Worker crash issue #343](https://github.com/python-arq/arq/issues/343) — accessed 2026-05-18

**Design impact:** Reuse the **existing research-job progress pattern** (DB-row updates from `progress_callback`). Set `job_timeout` to slightly above the 8h cap (e.g., 8.5h). Make the optimizer's trial-checkpoint after-tell so a kill mid-trial leaves the study consistent.

**Test implication:** Integration test the cancellation path (arq `Job.abort()` → trial loop exits cleanly → DB run row is `CANCELED` with partial trials preserved). Test the resume path (kill worker mid-run → restart → study loads from journal → continues at the same trial count).

---

### 6. Statistics for drawdown correlation

**Industry norm (synthesized from RealTest, AlgoTest, Build Alpha v3 references):**

- **Drawdown time-series** = `equity_t / running_max(equity_t) - 1` (a negative-valued series; 0 at peaks, more negative in troughs).
- **Drawdown correlation matrix** = `pandas.DataFrame.corr()` on the per-strategy drawdown time-series, NOT on return series. This is what the PRD's "drawdown correlation" actually means.
- Pearson is the default. Spearman is a robustness check (drawdowns are right-skewed and non-normal — Spearman is often the more defensible single number).
- More academic: **"Drawdown-Implied Correlation" (DIC)** from CSSA — computes implied correlation from observed joint drawdown probabilities. Higher-fidelity for tail-risk; complex to implement. **Defer to v2.**

**Sources:**

1. [How correlation between strategies affects portfolio risk (Algomatic Substack)](https://algomatictrading.substack.com/p/how-correlation-between-strategies) — accessed 2026-05-18
2. [Drawdown-Implied Correlations (CSSA)](https://cssanalytics.wordpress.com/2024/12/23/drawdown-implied-correlations-part-1/) — accessed 2026-05-18
3. [Build Alpha v3 Portfolio Mode (correlation-aware)](https://www.buildalpha.com/software-update-v3/) — accessed 2026-05-18

**Design impact:** Compute drawdown time-series with **`pandas`** (in-house, no new dep): `(equity / equity.cummax() - 1)`. Then `df.corr(method="pearson")` for the matrix. Expose `method="spearman"` as a config flag if the design wants robustness mode. The viz still presents one matrix at a time.

**Test implication:** Unit-test on a hand-computed 2-strategy fixture where one strategy drawdowns in Q1 and the other in Q4 (low correlation expected); compare against the same strategies both drawdowning in Q2 (high correlation expected).

---

### 7. Frontend: heatmap component (nivo vs visx vs ECharts)

**Versions checked (all NEW adds — none currently in `frontend/package.json`):**

- **`@nivo/heatmap` 0.99.0** — SVG-based, design-conscious defaults, ~800 KB install. React 19 supported (issue #2618). Sister `@nivo/core` peer dep required. **38 dependents on npm.**
- **`@visx/heatmap`** (Airbnb) — lower-level building blocks, very small footprint, requires hand-styling. More work for a "drop-in heatmap with tooltips."
- **ECharts React** — supports 100K+ cells via Canvas; overkill for an N×N where N ≤ 10 (portfolio member count is bounded).
- **Recharts (already in stack)** — has **NO heatmap primitive.**
- **Tremor** — no heatmap.

**Sources:**

1. [npm @nivo/heatmap](https://www.npmjs.com/package/@nivo/heatmap) — accessed 2026-05-18
2. [Recharts v3 vs Tremor vs Nivo 2026 (PkgPulse)](https://www.pkgpulse.com/guides/recharts-v3-vs-tremor-vs-nivo-react-charting-2026) — accessed 2026-05-18
3. [nivo React 19 support (issue #2618)](https://github.com/plouc/nivo/issues/2618) — accessed 2026-05-18

**Design impact:** Recommend **`@nivo/heatmap` + `@nivo/core`** as the heatmap library. Bundle cost ~800 KB is acceptable for a once-per-results-page render; not on every-page critical path. Wrap in a thin `<CorrelationHeatmap data={matrix} method="pearson|spearman" />` to abstract away the library and keep the option open for visx if a future Linear-style dark-mode tweak demands more control. The sister sortable table is plain `<Table>` from existing shadcn primitives.

**Test implication:** Playwright test asserts the heatmap renders, each cell is reachable by `getByRole("cell")` or `data-testid="cell-<i>-<j>"`, and tooltip text matches the cell value. No nivo-internal selectors in E2E.

---

### 8. Frontend: multi-select strategies UI

**Versions:** no new dep. `radix-ui ^1.4.3` is in `package.json`; shadcn primitives `<Command>`, `<Popover>`, `<Badge>` are already present in `frontend/src/components/ui/`.

**Pattern (shadcn-canonical):** `Command + Popover + Badge` — a button opens a `<Popover>` containing a `<Command>` search input + checklist of strategies; selected strategies render as removable `<Badge>` chips. The "shadcn-expansions MultipleSelector" is the de-facto reference. **No new library.**

**Sources:**

1. [shadcn Combobox docs](https://ui.shadcn.com/docs/components/radix/combobox) — accessed 2026-05-18
2. [shadcn-expansions MultipleSelector](https://shadcnui-expansions.typeart.cc/docs/multiple-selector) — accessed 2026-05-18

**Design impact:** Build `frontend/src/components/portfolio-compose/strategy-multi-select.tsx` on shadcn primitives only. Existing patterns confirmed: `react-hook-form + zod` is in `package.json` for the form, and `<Select>` for allocator/objective dropdowns is already used in `backtests/run-form.tsx`. No new dep.

**Test implication:** Playwright uses `getByRole("combobox", { name: "Select strategies" })` to open and `getByRole("option")` + click to select. Chips: `getByRole("button", { name: /Remove .* strategy/ })`.

---

### 9. Frontend: optimization trace visualization

**Industry choices:**

- **Parallel coordinates plot** is the standard for hyperparameter sweeps (Optuna's own `plot_parallel_coordinate`, HiPlot, scikit-learn course). The React-ecosystem options are limited: **Plotly.js wraps `parcoords` natively** but adds ~3 MB to the bundle; **D3 + custom React** is doable but new code.
- **QuantConnect's UI** uses scatter for 1 param, **heatmap for 2 params**, 3D for 3 — practical and forgiving. ([QuantConnect Optimization Results](https://www.quantconnect.com/docs/v2/cloud-platform/optimization/results))
- **Simpler:** a sortable "Backtests table" + scatter (best-config-vs-objective for first param). Industry's QuantConnect ships this alongside the chart.

**Sources:**

1. [Plotly parallel coordinates](https://plotly.com/javascript/parallel-coordinates-plot/) — accessed 2026-05-18
2. [QuantConnect optimization results docs](https://www.quantconnect.com/docs/v2/cloud-platform/optimization/results) — accessed 2026-05-18
3. [Optuna `plot_parallel_coordinate`](https://optuna.readthedocs.io/en/stable/reference/visualization/generated/optuna.visualization.plot_parallel_coordinate.html) — accessed 2026-05-18 (Optuna ships HTML/Plotly; cannot directly embed without Plotly.js)

**Design impact:** Don't pick a viz library in this brief. The PRD explicitly defers "exact visualization deferred to design phase" (US-002b § "Acceptance Criteria"). Recommend the design phase choose **between (a) trials table + scatter using Recharts** (zero new dep, lowest bundle cost) and **(b) Plotly.js parcoords** (industry-standard parcoords; +3 MB). Lean toward (a) for v1; defer parcoords to v2 unless the operator demands it.

**Test implication:** None until design phase picks a viz. Plan to E2E-assert "trials table renders N rows, sorted by objective, click reveals trial details."

---

### 10. Form patterns for compose UX

No new research needed. `react-hook-form ^7.76.0` + `zod ^4.4.3` + `@hookform/resolvers ^5.2.2` are already in `package.json`. `<Select>`, `<Input>`, `<Form>`, `<Popover>`, `<Command>` are all present in `frontend/src/components/ui/`. The existing `backtests/run-form.tsx` is a working pattern for date pickers + numeric inputs + a dropdown — reuse its shape.

**Design impact:** N/A — proceed with existing stack. **No new dependency.**

**Test implication:** Standard form coverage — required-field validation, range validation, persisted state on reload.

---

### 11. Operator-product UX patterns (Composer / QuantConnect / Build Alpha / RealTest / AlgoTest)

| Product            | Optimization result presentation                                                                     | IS/OOS comparison                                                                | Drawdown correlation                                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| **Composer**       | Historical Allocation Graph + metrics table; OOS date preserved across symphony edits.               | OOS date marker on the equity curve.                                             | Not surfaced (return correlation only).                               |
| **QuantConnect**   | Scatter (1 param) / heatmap (2 params) / 3D (3 params) + Backtests table with parameter values.      | Parameter optimization on IS; same params back-tested on OOS portion explicitly. | Not in main UI (research-environment notebook only).                  |
| **Build Alpha v3** | Fitness function = optimizer objective. Walk-forward 5×5 matrix default. Portfolio Suggest AI ranks. | OOS percentage configurable per cell.                                            | **Yes — correlation-aware portfolio simulation with shared capital.** |
| **RealTest**       | Aggregate equity + per-strategy contribution.                                                        | Walk-forward implicit.                                                           | **Yes — drawdown correlation is an explicit deliverable.**            |
| **AlgoTest**       | Aggregate equity + correlation matrices.                                                             | Walk-forward implicit.                                                           | **Yes — drawdown correlation in main UI.**                            |

**Sources:**

1. [Composer backtest basics](https://www.composer.trade/learn/how-do-backtests-work-in-composer) — accessed 2026-05-18
2. [QuantConnect optimization results](https://www.quantconnect.com/docs/v2/cloud-platform/optimization/results) — accessed 2026-05-18
3. [Build Alpha v3 features](https://www.buildalpha.com/software-update-v3/) — accessed 2026-05-18
4. [Build Alpha walk-forward](https://www.buildalpha.com/walk-forward-optimization/) — accessed 2026-05-18

**Design impact:**

- The PRD's "drawdown correlation matrix + sortable table side-by-side" matches **RealTest / AlgoTest** convention — that's the right target.
- For the optimization trace, **QuantConnect's adaptive viz** (scatter/heatmap/3D by param count) is a sensible v1 mental model.
- For IS/OOS display, **Composer's "OOS date marker on the equity curve"** is the simplest pattern; pair with the IS/OOS metric row from `research_engine.py`'s `generalization_gap`.

**Test implication:** N/A — UX patterns inform the design phase, not test scope.

---

## Not Researched (with justification)

- **scikit-optimize (skopt):** Repo is in **read-only mode** (active development halted) — citing [scikit-optimize GitHub](https://github.com/scikit-optimize/scikit-optimize) which confirms read-only state. **Adopted decision: Do not use.** Optuna covers our needs.
- **hyperopt:** Still maintained but Databricks (a former major user) [moved off hyperopt to Optuna in 2024](https://docs.databricks.com/aws/en/machine-learning/automl-hyperparam-tuning). **Adopted decision: Do not use.** No advantage over Optuna.
- **DEAP / cmaes:** Maintained ([DEAP GitHub](https://github.com/DEAP/deap)). Useful for non-convex / discrete genetic algorithms. **Adopted decision: Do not add.** Optuna's CmaEsSampler covers the evolutionary case if we ever need it (no new dep).
- **Ray Tune:** Distributed, correct fit for >1 machine. **Adopted decision: Do not add.** Single-VM compute envelope; Optuna with arq-managed concurrency is sufficient.
- **`quantstats-lumi` fork:** Maintained but a library swap is out of scope for this PR. Revisit when QuantStats' open-PR list becomes a blocker.
- **NautilusTrader 1.225 upgrade:** Affects backtest_runner. **Defer the decision to the design phase** — either upgrade in this PR or keep 1.222 (current pin). Note the PyO3 getter→method changes (1.225) require a small audit.
- **Tremor / ECharts / Plotly.js bundle decision:** Postpone until the design phase picks the optimization-trace viz. Bundle budget question, not a research question.
- **MSAL / PyJWT / FastAPI / SQLAlchemy / asyncpg / pydantic / redis:** Not touched by this feature beyond established patterns.

---

## Open Risks (the top 5 the design phase must resolve before plan-writing)

1. **Per-strategy P&L attribution accuracy under shared-equity model.** Nautilus's shared `Cache` makes per-strategy realized P&L easy to compute by `strategy_id` filtering — but **per-strategy equity curves and per-strategy drawdowns depend on a deterministic capital-allocation snapshot at each timestep**. With dynamic allocators (inverse-vol, vol-target, monthly rebalance) the design must define exactly **when** to snapshot the per-strategy equity. Spec the algorithm before coding.

2. **NautilusTrader 1.222 → 1.225 upgrade scope.** 1.225 changes `Position.events()` and other PyO3 getters from attributes to methods. `backtest_runner.py`, `parity/normalizer.py`, and any test fixture that reads `position.events` will break silently if the upgrade lands without an audit. Either (a) pin to 1.222 for this PR and upgrade separately, or (b) include the audit + migration in scope. **Decide before plan-writing.**

3. **Optuna trial cancellation idempotency.** `Job.abort()` → `CancelledError` in the trial loop must `study.tell(trial, state=TrialState.FAIL)` BEFORE returning, otherwise the journal stores a pending trial that re-runs on resume. Race window is the time between `study.ask()` and the first checkpoint. Design phase decides whether to wrap the trial body in `try/finally + study.tell(FAIL)` or rely on the journal's TTL.

4. **Walk-forward window sizing defaults for portfolio mode.** `build_walk_forward_windows(train_days=, test_days=)` works fine — but the **right defaults for a 3-member daily-bar portfolio are not the same as for a single intraday strategy**. PRD Open Question §7 already flags this. Design must propose `train_days=252, test_days=63, step_days=63` (or similar) with rationale, and the UI must allow override.

5. **Safety-cap enforcement strategy in the optimizer.** PRD says safety caps are **hard bounds** the search space cannot exceed. Two implementation paths:
   - **(a) Clip the search space at construction** (Optuna's `suggest_float(low=..., high=cap)` — clean but doesn't catch derived violations like max-leverage emerging from combined parameter values).
   - **(b) Reject infeasible trials after evaluation** (cheap to implement but wastes 1 trial per infeasible point).
     Decide which (and whether both) in the design phase. This is the council's Scalability-Hawk concern.

---

## Appendix — File Pointers (absolute paths)

- PRD: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/docs/prds/portfolio-backtest.md`
- Decision doc: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/docs/decisions/2026-05-17-portfolio-backtest-deferred.md`
- Walk-forward engine: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/src/msai/services/research_engine.py`
- Backtest runner (subprocess-per-run pattern): `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/src/msai/services/nautilus/backtest_runner.py`
- Nautilus natives audit: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/docs/nautilus-natives-audit.md`
- Nautilus venv Cache source (per-strategy filter): `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/.venv/lib/python3.12/site-packages/nautilus_trader/cache/base.pyx`
- Nautilus venv Portfolio source: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/.venv/lib/python3.12/site-packages/nautilus_trader/portfolio/portfolio.pyx`
- Existing portfolio service (Maintainer's refactor target, 1100 LOC): `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/src/msai/services/live/portfolio_service.py`
- Backend manifest: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/backend/pyproject.toml`
- Frontend manifest: `/Users/pablomarin/Code/msai-v2/.worktrees/portfolio-backtest/frontend/package.json`
