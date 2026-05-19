# PRD: Portfolio Backtest

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-18
**Last Updated:** 2026-05-18

---

## 1. Overview

Today the trader composes a live portfolio by picking one strategy at a time from a dropdown, **hand-writing per-strategy JSON config in a `<Textarea>`**, typing comma-separated instruments, and entering a raw `weight: 0–1` per member. This UX is rejected by the operator and the route at `/live-trading/portfolio` is currently hard-disabled (404) pending this redesign — see [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../decisions/2026-05-17-portfolio-backtest-deferred.md).

Portfolio Backtest replaces that flow with a form-based portfolio composer and a portfolio-level backtest engine that runs in two modes: **Quick** (single-shot backtest, conservative default risk policy, ~3–5 min) and **Full** (optimization over a per-strategy risk-policy parameter space against a trader-chosen objective function — return / Sharpe / Sortino / Calmar / max drawdown — with walk-forward cross-validation, up to 8h). A backtested portfolio revision can be promoted to a live deployment in one click. The feature reuses the existing Nautilus multi-strategy `BacktestNode` for execution and the existing `services/research_engine.py` walk-forward / IS-OOS harness for optimization.

## 2. Goals & Success Metrics

### Goals

- **Eliminate JSON from portfolio compose.** Trader composes through form widgets only (multi-select strategies, allocator dropdown, objective dropdown, safety-cap inputs).
- **Backtest the combined portfolio**, not just standalone strategies, so the trader can see how the _combination_ behaves before committing capital.
- **Make optimization a first-class workflow.** Trader picks an objective function; system searches the risk-policy parameter space and reports the winning config + IS/OOS performance.
- **Defend against overfitting** by default — Full mode uses walk-forward cross-validation; results page shows IS/OOS gap.
- **Promote winning portfolios to live** without recreating the form in `/live-trading`.
- **Surface drawdown correlation** (not just return correlation) so the trader sees _real_ diversification.

### Success Metrics

| Metric                                                  | Target                           | How Measured                                                                            |
| ------------------------------------------------------- | -------------------------------- | --------------------------------------------------------------------------------------- | -------------------------- |
| Time from "New Portfolio" click to "Run Quick" click    | < 2 min for 3-strategy portfolio | Stopwatch on E2E TJ-4 use case, measured during graduation                              |
| Quick-mode end-to-end latency (p95)                     | < 5 min                          | `portfolio_backtest_runs.completed_at - started_at` over the last 20 runs               |
| IS/OOS gap visibility on Full-mode results page         | 100% of Full runs                | Results page must render both `is_metric` and `oos_metric` plus their delta             |
| Promotion friction (Backtest results → Live deployment) | ≤ 3 clicks from results page     | Click-counted in E2E "promote" use case                                                 |
| Zero JSON in compose UX                                 | 0 raw-text config inputs         | Static check: no `<Textarea>` for config in `PortfolioCompose` component                |
| Member-strategy failures abort + surface per-strategy   | 100% of failures                 | Unit test: any member raise → run fails with per-strategy attribution in error response |
| `/backtests` history unified                            | 1 list view, type-filterable     | UI inspection: list shows badge column (Single                                          | Portfolio), filter present |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Allocators beyond v1 set** — HRP, risk-parity, mean-variance optimization, Black-Litterman are deferred to v2.
- ❌ **Manual JSON risk-policy compose** — the optimizer-driven flow REPLACES per-strategy risk-policy form fields. No "Edit Raw Config" mode in v1.
- ❌ **Manual per-strategy capital weights** — the allocator computes weights; the trader sets the allocator, not the numbers.
- ❌ **Per-strategy explicit instrument override** — universe auto-derived from strategy union; if the trader needs a subset they create a narrower strategy.
- ❌ **Custom blended objective functions** (e.g., `0.7 × Sharpe + 0.3 × −maxDD`) — v1 ships the 5 standard objectives; user-defined blends defer to v2.
- ❌ **Per-strategy hard caps** — v1 ships portfolio-level safety ceilings only (max leverage, max position size, max drawdown halt apply to the _portfolio_, not per member).
- ❌ **Stress tests, Monte Carlo, rolling correlation, allocation-drift charts** on the results page — deferred to v2.
- ❌ **Adding walk-forward to single-strategy `/backtests`** — this PR does not change single-strategy backtest behavior; `/backtests` stays in-sample-full-period as today.
- ❌ **Cross-regime stability scoring / regime detection** — deferred to v2.
- ❌ **Anonymous / multi-tenant access** — MSAI is single-user; auth model unchanged.

## 3. User Personas

### The Trader (Pablo)

- **Role:** Sole operator of MSAI v2. Owns strategies, composes portfolios, runs backtests, deploys live, monitors P&L.
- **Permissions:** Full — strategies registry, backtests, portfolios, live deploy, kill-all. Authenticated via Azure Entra ID.
- **Goals:** Build diversified multi-strategy portfolios, find configurations that hold up out-of-sample, and ship them to live trading without re-typing the spec.
- **Context:** This is a personal-hedge-fund platform; no secondary personas. RBAC granularity is not in scope.

## 4. User Stories

### US-001: Compose a portfolio without JSON

**As a** trader
**I want** to multi-select strategies and configure them as a portfolio (allocator + rebalance cadence + objective function + safety caps) through form widgets
**So that** I can construct a portfolio in minutes without hand-writing config text

**Scenario:**

```gherkin
Given I am authenticated and at /portfolios/new
When I multi-select 3 strategies from the registry
And I choose allocator = "inverse-volatility"
And I choose rebalance cadence = "monthly"
And I choose objective = "Sharpe"
And I set safety caps: max_leverage = 2.0, max_position_size = 0.25, max_drawdown_halt = 0.20
And I enter date range = last 2 years and initial capital = $100,000
And I click "Save Composition"
Then a new LivePortfolio is created with a frozen LivePortfolioRevision
And I land on /portfolios/<id> showing the composition summary
And no `<Textarea>` for JSON config appears anywhere on the page
```

**Acceptance Criteria:**

- [ ] Strategies are selected via a searchable multi-select; selected strategies render as removable chips.
- [ ] Allocator is a `<Select>` with options: equal-weight, fixed-weight, inverse-volatility, vol-targeted. Default: equal-weight.
- [ ] Rebalance cadence is a `<Select>` with options: none, daily, weekly, monthly. Default: monthly.
- [ ] Objective function is a `<Select>` with options: total return, Sharpe, Sortino, Calmar, max drawdown. Default: Sharpe.
- [ ] Safety caps are three numeric inputs with validation (positive, sensible ranges) and inline help text.
- [ ] Date range uses a date-range picker; initial capital is a numeric input with currency formatting.
- [ ] When fixed-weight allocator is chosen, a per-strategy weight field appears (only that allocator surfaces weights).
- [ ] No `<Textarea>` for raw config appears in compose UX.
- [ ] Saving the composition creates a `LivePortfolio` + frozen `LivePortfolioRevision` via the existing live-portfolio chain.
- [ ] The composition page reloads cleanly; persistence verified.

**Edge Cases:**

| Condition                                                        | Expected Behavior                                                                                                          |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Trader selects 1 strategy with inverse-volatility allocator      | Allocator works (single strategy gets 100% weight); show inline note "single-strategy portfolios collapse to fixed-weight" |
| Trader selects 0 strategies                                      | "Save Composition" button disabled; inline message "select at least one strategy"                                          |
| Trader selects a strategy that targets instruments we don't have | Compose succeeds; warning chip on the strategy: "instrument <X> data missing"; backtest will fail until ingest runs        |
| Trader edits a saved composition (adds/removes a strategy)       | New `LivePortfolioRevision` is created; previous revision and its backtests remain queryable                               |
| Two compositions share the same set of strategies and allocator  | They are separate entities; no auto-dedup (intentional — trader may want parallel experiments)                             |

**Priority:** Must Have

---

### US-002a: Run a Quick portfolio backtest

**As a** trader
**I want** a Quick mode that runs a single-shot backtest of the composition with default risk policy in 3–5 minutes
**So that** I can iterate on portfolio composition without paying the full optimization cost

**Scenario:**

```gherkin
Given I am at /portfolios/<id> with a saved composition
When I click "Run Backtest"
And I select Mode = "Quick"
And I confirm the run dialog
Then a PortfolioBacktestRun is created with mode = "quick"
And the job runs to completion within ~5 minutes for a 3-strategy 2-year backtest
And I am redirected to /portfolio-backtests/<run_id> showing the results
And the results page shows combined equity, per-strategy contribution, return + drawdown correlation matrices
```

**Acceptance Criteria:**

- [ ] Run dialog has a Mode toggle (Quick | Full); Quick is the default for first-time users.
- [ ] Quick mode runs a single backtest through Nautilus multi-strategy `BacktestNode` with a conservative default risk policy (no parameter search).
- [ ] Quick mode uses full-period in-sample (no walk-forward); matches single-strategy `/backtests` semantics on training-window choice.
- [ ] Quick mode end-to-end latency ≤ 5 min (p95) for a 3-strategy, 2-year, daily-bar backtest.
- [ ] Run status is pollable via `GET /api/v1/portfolio-backtests/<run_id>/status` (parallels existing single-backtest status endpoint).
- [ ] On completion, the run's results page renders.
- [ ] On failure, the run's results page surfaces the per-strategy error attribution.

**Edge Cases:**

| Condition                                              | Expected Behavior                                                                                                          |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Member strategy raises an exception during backtest    | The entire run fails. Results page shows `FAILED` status + per-strategy error block identifying which member raised.       |
| Member strategy has missing instrument data            | Run fails fast at startup with `MISSING_DATA` status + per-strategy data-gap report. Suggest `msai ingest …` in the error. |
| Backtest exceeds the Quick mode timeout (e.g., 15 min) | Job is canceled, run marked `TIMEOUT`. Suggest Full mode for compute-heavy compositions.                                   |
| Trader runs Quick on the same revision repeatedly      | Each run is a separate `PortfolioBacktestRun`. No dedup (intentional — useful for measuring run-to-run determinism).       |

**Priority:** Must Have

---

### US-002b: Run a Full optimization with walk-forward

**As a** trader
**I want** a Full mode that searches the per-strategy risk-policy parameter space against my chosen objective function using walk-forward cross-validation
**So that** I get a config that's robust out-of-sample, not overfit to history

**Scenario:**

```gherkin
Given I am at /portfolios/<id> with a saved composition
And the composition has objective = "Sharpe" and safety caps set
When I click "Run Backtest" and select Mode = "Full"
And I confirm the run dialog with time budget = 8h
Then a PortfolioBacktestRun is created with mode = "full"
And the job runs walk-forward over the date range, optimizing per window on the in-sample portion
And evaluating the chosen config on the out-of-sample portion
And on completion I see the winning risk-policy config, its IS metric, its OOS metric, and the IS-OOS gap
And the optimization trace is browsable (parameter samples + objective value per trial)
```

**Acceptance Criteria:**

- [ ] Run dialog Mode = "Full" exposes time-budget input (default 8h, capped at 8h in v1).
- [ ] Full mode reuses `services/research_engine.py` for walk-forward window generation, IS/OOS scoring, and the optimizer harness.
- [ ] Optimizer respects the portfolio-level safety caps (max leverage, max position size, max drawdown halt) as hard bounds — search space cannot exceed them.
- [ ] Results page renders **both** IS metric and OOS metric for the chosen objective, plus their delta; large gap is visually flagged.
- [ ] Results page renders the optimization trace (parameter samples + objective value per trial) — exact visualization deferred to design phase.
- [ ] Run is cancelable mid-flight via a Cancel button; cancellation completes within 30s and marks the run `CANCELED` with whatever trials completed preserved.
- [ ] Cost telemetry: each Full run logs trial count, wall-clock time, and per-trial CPU-seconds for capacity planning.

**Edge Cases:**

| Condition                                                               | Expected Behavior                                                                                                    |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Walk-forward window doesn't fit (date range < 2 windows)                | Run fails fast at validation with `INSUFFICIENT_DATE_RANGE`; suggest extending the range.                            |
| Optimizer finds no config satisfying safety caps                        | Run completes with status `NO_FEASIBLE_CONFIG`; results page explains which cap was the binding constraint.          |
| All trials produce identical objective values (objective is degenerate) | Run completes; results page flags "objective insensitive to parameters in search space — try a different objective". |
| Wall-clock budget exhausted before convergence                          | Run completes with status `BUDGET_EXHAUSTED`; best-so-far config + its IS/OOS metrics still rendered.                |
| Member strategy raises during a trial                                   | The trial is marked failed; optimizer continues with remaining trials. If >X% of trials fail, run fails.             |
| Trader navigates away mid-run                                           | Run continues in the background. Reload `/portfolio-backtests/<run_id>` to see progress; status banner shows ETA.    |

**Priority:** Must Have

---

### US-003: Analyze portfolio backtest results

**As a** trader
**I want** a portfolio-level results page — combined equity curve, per-strategy contribution attribution, return correlation matrix, drawdown correlation matrix (heatmap + sortable table), drawdown breakdown by strategy
**So that** I can evaluate diversification and overfit risk, not just standalone returns

**Scenario:**

```gherkin
Given I am at /portfolio-backtests/<run_id> after a completed run
Then I see the combined equity curve (portfolio-level)
And I see per-strategy contribution stacked underneath
And I see two correlation matrices: return correlation and drawdown correlation
And each correlation matrix shows as both a heatmap AND a sortable table side-by-side
And I see a drawdown breakdown table by strategy (max DD, drawdown duration, recovery)
And (Full mode only) I see IS metric, OOS metric, IS-OOS gap, and the optimization trace
And the page reloads identically (persisted run, not derived in-memory)
```

**Acceptance Criteria:**

- [ ] Combined equity curve renders with TradingView Lightweight Charts (existing chart library).
- [ ] Per-strategy contribution renders as a stacked area chart or equivalent contribution attribution viz.
- [ ] Both correlation matrices (return + drawdown) render heatmap + table side-by-side, sortable and exportable from the table.
- [ ] Drawdown breakdown table lists per-strategy: max DD, drawdown duration, recovery time.
- [ ] Full-mode runs additionally render IS metric, OOS metric, IS-OOS gap (visually emphasized when gap is large).
- [ ] Full-mode runs additionally render the optimization trace (visualization style chosen in design phase).
- [ ] Run is downloadable as a QuantStats HTML report (parallels existing `/api/v1/backtests/<id>/report`).
- [ ] Results data persists in `portfolio_backtest_runs` table (additive migration); page reloads pull from DB.

**Edge Cases:**

| Condition                                                             | Expected Behavior                                                                                             |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Run is still in flight                                                | Page shows progress banner with ETA + completed/total trials (Full mode) or "running" indicator (Quick mode). |
| Run failed                                                            | Page shows `FAILED` banner with per-strategy error attribution; no charts render.                             |
| All strategies share the same instrument universe (correlation = 1.0) | Heatmap renders the full-red matrix; inline note "low diversification — correlations near 1.0".               |
| Only 1 member strategy in the portfolio                               | Correlation matrices and per-strategy contribution viz collapse to "N/A — single-strategy portfolio".         |
| Run is older than the latest revision of the portfolio                | Results page shows "this backtest is of an earlier revision" badge with link to the revision.                 |

**Priority:** Must Have

---

### US-004: Promote a backtested portfolio to live

**As a** trader
**I want** a "Deploy as Live Portfolio" button on the results page that creates a live deployment with the same composition + winning config
**So that** I don't have to recreate the form in `/live-trading`

**Scenario:**

```gherkin
Given I am at /portfolio-backtests/<run_id> for a successful run
When I click "Deploy as Live Portfolio"
And I confirm the existing risk-engine validation gate
Then a LiveDeployment is created for the same LivePortfolioRevision
And (Full mode only) the winning risk-policy config is materialized into the deployment
And I land on /live-trading/<deployment_id> for review and Start
And the deployment defaults to a paper IB account (DU…)
```

**Acceptance Criteria:**

- [ ] "Deploy as Live Portfolio" button appears on every successful results page.
- [ ] Promotion uses the existing `LivePortfolio → LivePortfolioRevision → LiveDeployment` chain — no new live-deploy paths.
- [ ] Promotion routes through the existing risk-engine validation gate (no bypass — even from a backtested-and-approved revision).
- [ ] Promotion from a Full-mode run materializes the winning risk-policy config into the deployment; promotion from a Quick-mode run uses the conservative default.
- [ ] Live deployment defaults to paper IB account (per project safety rail in `CLAUDE.md` / `nautilus.md` gotcha #6).
- [ ] Promotion completes in ≤ 3 clicks from results page (button → confirm dialog → Start).

**Edge Cases:**

| Condition                                                    | Expected Behavior                                                                                                    |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| Risk-engine validation fails (e.g., over-leverage)           | Deploy is blocked; trader sees the validation error inline. Existing risk-engine 422 response surfaces verbatim.     |
| Trader promotes a Failed or Canceled run                     | "Deploy as Live" button is hidden; tooltip explains why.                                                             |
| Trader promotes a run whose revision is no longer the latest | Confirmation dialog warns: "This is an older revision; current revision is <N>. Continue with the older revision?"   |
| Trader has an existing live deployment for this portfolio    | Confirmation dialog warns: "Existing live deployment <id> is running. Stop it first or create a separate portfolio." |

**Priority:** Must Have

---

## 5. Constraints & Policies

> Outcome-level. Hard limits the product must respect.

### Business / Compliance Constraints

- **Optimization compute envelope:** up to 8h per Full run is acceptable per trader directive (2026-05-18). Wall-clock cost is not a constraint per [`feedback_dont_optimize_for_cost.md`](../../memory/feedback_dont_optimize_for_cost.md) — rigor wins over compute spend.
- **Real-money safety:** every live promotion defaults to a paper IB account (`DU…`). Real-account promotion requires explicit operator override in the run dialog and matches the project's standing "default to paper" policy (`CLAUDE.md` § Live-trading safety rails).
- **Reproducibility:** every `PortfolioBacktestRun` must persist `strategy_code_hash` (per member), `git_sha`, `nautilus_version`, `python_version`, `data_snapshot` (matching the existing single-backtest data-lineage pattern in `CLAUDE.md` § Key Design Decisions).

### Platform / Operational Constraints

- **Engine choice is fixed.** Portfolio execution must run through Nautilus multi-strategy `BacktestNode` (`TradingNodeConfig.strategies=[N ImportableStrategyConfig]`). Hand-rolled aggregators are not acceptable (per `nautilus.md` Architectural Rule #1 + decision doc).
- **Walk-forward harness reuse.** Full mode must reuse `services/research_engine.py` (`build_walk_forward_windows`, IS/OOS scoring, optimizer harness). No parallel walk-forward implementation.
- **Refactor ordering.** `portfolio_service.py` (1100 LOC, flagged by Maintainer council advisor) must be split in the same PR, with refactor commits ordered BEFORE new portfolio-backtest modules. This is the Maintainer's structural prerequisite from the decision doc.
- **No JSON in compose UX.** Static check: zero `<Textarea>` widgets for config in the compose page. This is the operator-facing acceptance criterion that motivated the redesign.
- **Single-strategy `/backtests` is unchanged.** This PR does not modify single-strategy backtest behavior, schemas, or endpoints. Unified `/backtests` history view is a new presentation layer over both run types.

### Dependencies & Required Integrations

- **Requires:** `services/research_engine.py` (existing, holds the walk-forward + IS/OOS machinery).
- **Requires:** `LivePortfolio` / `LivePortfolioRevision` / `LiveDeployment` chain (existing, holds the composition + deployment lifecycle).
- **Requires:** Nautilus multi-strategy `BacktestNode` (existing in vendor library).
- **Requires:** Single-strategy backtest infrastructure (`/api/v1/backtests/*`, `BacktestRunner`, QuantStats reporter) for pattern reuse and the unified history view.
- **Blocked by:** None.
- **Named integrations:** None new — no new external systems. Existing IB Gateway integration is only relevant on the live-promotion path and uses existing infrastructure unchanged.

## 6. Security Outcomes Required

- **Access:** Only authenticated trader (Pablo, via Azure Entra ID) can compose portfolios, run backtests, or promote to live. Existing JWT enforcement on `/api/v1/*` covers this — no new auth surface.
- **Audit:** Every `PortfolioBacktestRun` is auditable: who triggered it (JWT `sub`), when (timestamps), what code ran (`strategy_code_hash` + `git_sha`), and (Full mode) full optimization trace.
- **Reproducibility:** A run can be re-executed and produce identical results given the same `LivePortfolioRevision` + `data_snapshot` + `nautilus_version`. Required for incident forensics and for promotion review.
- **Live-deploy gate:** Promotion from results page must traverse the existing risk-engine validation gate. No bypass, even for backtested-and-approved revisions. This is the project's standing safety rail; not negotiable.
- **No new secret-handling:** Feature does not introduce new secrets, API keys, or external credentials.
- **Compute-cost telemetry is non-sensitive:** Full-mode trial counts, CPU-seconds, and wall-clock can be logged at INFO level without scrubbing.

## 7. Open Questions

Carried forward from discussion; deferred to design / research / plan-review:

- [ ] **Search algorithm choice** — Bayesian (e.g., optuna), evolutionary (e.g., cmaes / DEAP), or grid? Defer to design phase; depends on the parameter-space shape that falls out of the risk-policy schema.
- [ ] **Walk-forward window sizing defaults** — `services/research_engine.py` exposes `build_walk_forward_windows(start_date=, ...)` but the default window/step needs portfolio-backtest-appropriate defaults. Defer to design.
- [ ] **Optimization trace visualization** — parameter scatter, parallel coordinates, just best-result table, or something else? Defer to design.
- [ ] **Concurrent Full-mode budget** — council Scalability Hawk flagged `portfolio_service.py` lease accounting at 10× concurrent. Pablo is a single user, but a single trader can launch multiple Full runs in parallel. Pin a concurrency cap in plan-review.
- [ ] **Real-money override UX** — paper account is the default for promotion; the override surface needs design (modal, checkbox, separate flow?). Defer to design.

## 8. References

- **Discussion Log:** [`docs/prds/portfolio-backtest-discussion.md`](./portfolio-backtest-discussion.md)
- **Predecessor Decision (5/5 council + Codex research):** [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../decisions/2026-05-17-portfolio-backtest-deferred.md)
- **Nautilus Engine Constraints:** [`docs/nautilus-reference.md`](../nautilus-reference.md) + `.claude/rules/nautilus.md` gotchas #1, #6, #18
- **Walk-forward harness (to be reused):** `backend/src/msai/services/research_engine.py`
- **Live deploy chain (to be reused):** `backend/src/msai/services/live/portfolio_service.py` (to be split first), `backend/src/msai/models/live_portfolio.py`
- **Memory references:** [`feedback_always_use_nautilus_api_first.md`](../../memory/feedback_always_use_nautilus_api_first.md), [`feedback_dont_optimize_for_cost.md`](../../memory/feedback_dont_optimize_for_cost.md), [`feedback_skip_phase3_brainstorm_when_council_predone.md`](../../memory/feedback_skip_phase3_brainstorm_when_council_predone.md)
- **Competitor reference (synthesized from discussion):** Composer.trade (visual symphony builder), QuantConnect (Backtest vs Optimization split), Build Alpha (walk-forward + objective optimization), RealTest / AlgoTest (drawdown correlation), López de Prado HRP (v2 allocator).

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes     |
| ------- | ---------- | -------------- | ----------- |
| 1.0     | 2026-05-18 | Claude + Pablo | Initial PRD |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design
