# PRD Discussion: Portfolio Backtest

**Status:** Complete — ready for `/prd:create portfolio-backtest`
**Started:** 2026-05-18
**Participants:** Pablo, Claude
**Predecessor decision:** [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../decisions/2026-05-17-portfolio-backtest-deferred.md) (5/5 council + Codex research, ratified Option B)

## Original User Stories

Pablo's product directive (verbatim, from `feat/ui-completeness` iter-3 walkthrough at `/live-trading/portfolio`):

> "users dont need to deal with json, they select the strategies to go
> in the porfolio, then at portfolio level they pick the risk,
> allocation methods to each strategy, etc, then the user should be
> able to backtest the portfolio to see how it woudl behave in the
> past"

This decomposes to three stories:

- **US-1: Compose** — As a trader, I want to multi-select strategies and configure them at the portfolio level (allocation method, per-strategy risk policy) without writing JSON, so I can construct a portfolio in minutes instead of hand-editing config.
- **US-2: Backtest** — As a trader, I want to backtest the composed portfolio against historical data, so I can see how the _combined_ strategy set would have behaved before risking capital.
- **US-3: Analyze** — As a trader, I want a portfolio-level results view — combined equity, per-strategy attribution, correlation matrix (return AND drawdown), drawdown breakdown — so I can evaluate diversification, not just standalone returns.

## Pre-confirmed Scope (from council + scoping turn 2026-05-18)

| Decision                          | Value                                                                                                                                                                            |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **v1 allocators**                 | equal-weight, fixed-weight, inverse-volatility, vol-targeted                                                                                                                     |
| **v2 allocators (defer)**         | HRP, risk-parity, mean-variance optimization, Black-Litterman                                                                                                                    |
| **v1 risk policy fields**         | per-strategy: max position, max daily loss, stop-out, leverage cap, concentration limit, vol band, correlation cap (final list to be confirmed in discussion)                    |
| **v1 results**                    | combined equity curve, per-strategy contribution attribution, return correlation matrix, **drawdown correlation matrix**, drawdown breakdown by strategy                         |
| **v2 results (defer)**            | stress tests, Monte Carlo, rolling correlation, allocation drift                                                                                                                 |
| **Engine**                        | reuse Nautilus multi-strategy `BacktestNode` (`TradingNodeConfig.strategies=[N ImportableStrategyConfig]`), NOT a hand-rolled aggregator (per nautilus gotcha #1 + decision doc) |
| **portfolio_service.py refactor** | Same PR, refactor commits FIRST, then build new modules on top                                                                                                                   |
| **Backend additions**             | `portfolio_backtest_runs` table (additive), `services/portfolio_backtest/{allocators,risk,results}.py`, materialized cache for correlation matrices                              |
| **Frontend additions**            | form-based multi-select compose, allocation-method `<Select>`, per-strategy risk-config drawer, portfolio-backtest `<RunDialog>`, results page                                   |
| **E2E coverage**                  | re-enable TJ-4 (was DEFERRED in PR #70) with the new UX                                                                                                                          |

## Discussion Log

### Turn 1 — Composition + execution mechanics (4 questions, all default-recommended accepted)

**Q1: Portfolio lifecycle — revisioned, mutable, or immutable?**
A: **Revisioned, matching LivePortfolio.** Edit creates a new revision; backtests attach to a specific revision. Mirrors existing pattern; enables reproducibility and "compare same portfolio over time".

**Q2: When does the allocator recompute weights during backtest?**
A: **User picks per portfolio: none / daily / weekly / monthly.** Default monthly (industry norm). 'None' = set-once, weights drift. Allows trader to test rebalance impact for dynamic allocators (inverse-vol, vol-target).

**Q3: Backtest input requirements?**
A: **User picks date range + initial capital; instruments inferred from member strategies (union).** Date-range default last 2 years, capital default $100k matching single-backtest. No manual JSON or instrument override in v1.

**Q4: Member strategy failure handling?**
A: **Abort the entire portfolio backtest, surface per-strategy failure.** Strict semantics; partial results would mislead. Matches Nautilus single-strategy semantics.

### Turn 2 — Outputs, promotion, edge cases (4 questions; 3 default-accepted, 1 redirected)

**Q5: Required per-strategy risk policy fields in v1?**
A: **REDIRECT — Pablo: "this should be automated, meaning that every backtest should inform what is the right combination. What I want as a user to pick is, what am I optimizing for: return, sharpe, sortino? something else?"**

This is a substantial scope change vs council's static-risk-policy scope. The feature becomes **portfolio optimization on top of portfolio backtesting**: trader picks an objective function, system searches the risk-policy parameter space, reports best config + its performance. Turn 3 below pins down the optimization scope.

**Q6: Backtest → Live promotion path?**
A: **'Deploy as live portfolio' button on results page.** Click creates a LivePortfolio with the same composition; lands on `/live-trading/<id>` for review and Start.

**Q7: Drawdown correlation display?**
A: **Heatmap + sortable table side-by-side.** Matches RealTest / Portfolio Visualizer. Same UX for return correlation.

**Q8: Portfolio backtest history location?**
A: **Unified `/backtests` with type filter** (All | Single-strategy | Portfolio). List shows Type badge. Detail routes diverge (`/backtests/<id>` for single, `/portfolio-backtests/<id>` for portfolio detail).

### Turn 3 — Optimization scope (4 questions, all defaults accepted)

**Q9: Replace, augment, or progressive optimization?**
A: **REPLACE — optimizer picks risk policy from objective; no manual risk-policy fields.** User input shrinks to: strategies + allocator + objective + date range + capital + hard caps. Cleanest UX.

**Q10: Objective functions in v1?**
A: **5 standard: total return, Sharpe, Sortino, Calmar, max drawdown.** Covers risk-adjusted, drawdown-aware, and raw. Matches Build Alpha / QuantConnect / Portfolio Visualizer baseline.

**Q11: Hard caps the optimizer cannot exceed?**
A: **Small fixed set of safety ceilings (recommended: max leverage, max position size, max drawdown halt).** Optimizer searches WITHIN these. Stops the "optimizer chose suicidal leverage to win Sharpe by 0.05" failure mode.

**Q12: Time budget per optimization run?**
A: **Overnight (up to 8h) acceptable.** Permits deep search — Bayesian/evolutionary methods over 100s–1000s of trials, or grid search over many fields. Compute cost not the constraint (per `feedback_dont_optimize_for_cost.md`).

### Turn 4 — Walk-forward + mode toggle

**Q13: Overfitting defense — how does the optimization handle in-sample vs out-of-sample?**
A: **REUSE existing research engine.** Pablo asked "Same as in individual backtesting, we do a walk-forward cross-validation. What do you think?" — verified: single-strategy `/backtests` does NOT do walk-forward (it's QuantStats full-period in-sample), but `/research` (via `services/research_engine.py`) DOES — `build_walk_forward_windows()`, IS/OOS gap calculator, `in_sample_results` / `out_of_sample_results`. **Decision: portfolio-backtest's Full (optimization) mode reuses the research engine's walk-forward machinery; do not reinvent.** Side benefit: surfaces the research engine's value-prop into a UI-facing feature.

**Q14: Quick (single-shot) mode alongside Full (optimization) mode?**
A: **Two modes — Quick + Full.** Quick = single-shot backtest with conservative default risk policy, ~3–5 min, no optimization, full-period in-sample (matches single-strategy `/backtests` semantics). Full = optimization with walk-forward, up to 8h. Run dialog has a Mode toggle. Matches QuantConnect's "Backtest vs Optimization" split. Quick mode is the "explore composition feel" workflow; Full is the "commit a config" workflow.

---

## Refined Understanding

### Personas

- **The Trader (Pablo, single user)** — owns strategies, composes portfolios, runs backtests/optimizations, deploys to live. MSAI v2 is a personal-hedge-fund platform; no secondary personas.

### Refined User Stories

- **US-1 (Compose):** As a trader, I want to multi-select strategies and configure them as a portfolio (allocator + rebalance cadence + safety hard-caps + objective function) without writing JSON, so I can construct a portfolio in minutes via a form. Portfolios are revisioned, mirroring LivePortfolio.
- **US-2a (Quick Backtest):** As a trader, I want a Quick mode that runs a single-shot backtest of the composition with default risk policy in 3–5 minutes, so I can iterate on composition without paying optimization cost.
- **US-2b (Full Optimization):** As a trader, I want a Full mode that searches the risk-policy parameter space against an objective function (return / Sharpe / Sortino / Calmar / max DD) using walk-forward cross-validation, so I get a config that is robust out-of-sample rather than overfit to history. Budget up to 8h per run.
- **US-3 (Analyze):** As a trader, I want a portfolio-level results page — combined equity curve, per-strategy contribution attribution, return correlation matrix, **drawdown correlation matrix** (heatmap + sortable table), drawdown breakdown by strategy, and (Full mode only) IS/OOS gap + optimizer trace — so I can evaluate diversification and overfit risk, not just standalone returns.
- **US-4 (Promote):** As a trader, I want a "Deploy as live portfolio" button on the results page that materializes the composition + winning config as a LivePortfolio + LivePortfolioRevision, so I don't recreate the form in `/live-trading`.

### Non-Goals (v1)

- HRP, risk-parity, mean-variance optimization, Black-Litterman allocators (deferred to v2)
- Stress tests, Monte Carlo, rolling correlation, allocation drift charts (deferred to v2)
- Manual JSON risk-policy compose (REPLACED by objective + optimizer)
- Manual per-strategy capital weights (allocator computes these)
- Per-strategy explicit instrument override (auto-derived from strategy union)
- Custom blended objective functions (defer to v2)
- Per-strategy hard caps in v1 (only portfolio-level safety caps)
- Cross-validation across regimes / regime-stability scoring (defer to v2)
- Walk-forward in `/backtests` single-strategy (this PR does not change single-strategy behavior)

### Key Decisions

1. **Optimization, not manual risk-policy fields** — the largest scope shift from council's 2026-05-17 verdict. Council ratified DEFERRAL; the v1 contract IS new and needs a council ratification this branch (Phase 3 council should run).
2. **Reuse `services/research_engine.py`** for walk-forward windows, IS/OOS scoring, optimizer harness. Don't reinvent.
3. **Two modes, one results page** — Quick (no optimization, single equity curve) and Full (optimization, equity + trace + IS/OOS).
4. **Revisioned portfolio entity** — mirrors `LivePortfolio` → `LivePortfolioRevision` → `LiveDeployment` pattern. New parallel: `LivePortfolioRevision` → `PortfolioBacktestRun` (additive table). Both reference the same `LivePortfolioRevision` so a backtested revision can be promoted to live with one click.
5. **Nautilus multi-strategy `BacktestNode` underneath** — never a hand-rolled aggregator (council + nautilus gotcha #1).
6. **Engine ordering in this PR**: refactor commits split `portfolio_service.py` FIRST, then portfolio-backtest builds on the refactored foundation.
7. **`/backtests` is the unified history** with a Type filter; detail routes diverge.

### Open Questions (Remaining)

- [ ] **Search algorithm choice** — Bayesian (optuna), evolutionary (DEAP / cmaes), or grid? Defer to design/research phase; this is a HOW question, not a WHAT.
- [ ] **Walk-forward window sizing defaults** — research engine has `build_walk_forward_windows(start_date=, ...)` but the default window/step needs to be chosen for portfolio-backtest semantics. Defer to design.
- [ ] **Optimizer trace visualization** — parameter scatter, parallel coordinates, just-best-result table? Mentioned as a design-phase concern in US-3.
- [ ] **Concurrent Full-mode budget** — council's Scalability Hawk flagged 10× concurrent at the `portfolio_service.py` layer. Today single-trader has Pablo only; Full optimization at 8h × N concurrent has real compute envelope implications. Pin in plan-review phase.
