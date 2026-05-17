# Decision: Portfolio Compose + Backtest Redesign — Deferred

**Status:** Ratified by council 2026-05-17
**Affects:** `feat/ui-completeness` PR (98 → 100 files, with TJ-4 carved out)
**Owners:** Pablo (product), Claude (implementation)
**Supersedes:** None
**Follow-up:** `/new-feature portfolio-backtest` (next branch)

## Context

Pablo invoked `/loop` to walk the full trader journey through the
UI-completeness branch before PR. During the iter-3 walkthrough at
`/live-trading/portfolio` (live portfolio compose page), he rejected
the shipped UX:

> "users dont need to deal with json, they select the strategies to go
> in the porfolio, then at portfolio level they pick the risk,
> allocation methods to each strategy, etc, then the user should be
> able to backtest the portfolio to see how it woudl behave in the
> past"

The current implementation requires the trader to:

1. Pick one strategy at a time from a dropdown
2. **Hand-write per-strategy Config (JSON)** in a `<Textarea>`
3. Type comma-separated instruments
4. Enter a raw `weight: 0–1`
5. Repeat for each member
6. Click Snapshot → freeze the revision → Start dialog → live deploy

There is no allocation-method picker (equal-weight, inverse-vol,
vol-targeted, risk-parity, HRP, mean-variance), no per-member risk
policy (max position, max daily loss, stop-out, leverage cap,
correlation cap), and no portfolio-level backtest viewer (combined
equity curve, per-strategy contribution, correlation matrix,
drawdown breakdown).

## Research

Claude + Codex parallel web research surveyed Composer, QuantConnect,
Build Alpha, RealTest, AlgoTest, AmiBroker, Portfolio Visualizer, AQR,
plus the canonical literature (Marcos López de Prado HRP, Robert
Carver _Systematic Trading_, Ernie Chan).

**Dominant industry pattern (2026):**

- **Composer.trade**: visual "symphony" builder, no JSON anywhere.
  Allocation dropdown: `custom weight | inverse volatility | market cap
| balance equally`. Backtest combined equity curve + per-strategy
  attribution.
- **QuantConnect**: code-based Portfolio Construction Model framework.
  Supported models: `EqualWeighting`, `InsightWeighting`,
  `ConfidenceWeighted`, `MeanVariance`, `BlackLitterman`, `RiskParity`.
  Risk Management model receives portfolio targets, applies trailing
  stops / drawdown liquidation / sector exposure / hedging.
- **Build Alpha**: form-based with risk profile + fitness function +
  saved strategies. Multi-strategy optimization; equity curve
  comparison + correlation matrix + min-variance + robustness tests.
- **RealTest / AlgoTest**: aggregate equity + correlation matrices.
  Critically: **drawdown correlation** (not just return correlation),
  because drawdowns at different times are what diversify real outage
  risk.
- **AmiBroker**: explicitly models one shared portfolio equity with
  position sizing from portfolio equity, not isolated strategy
  accounts.
- **Carver**: "equal weights are hard to beat" — manual portfolio
  construction + diversification > opaque optimization. Volatility
  targeting at the position-sizing level.
- **López de Prado HRP**: hierarchical clustering on covariance →
  cluster-equal capital. Doesn't need invertible covariance matrix.
  Outperforms Markowitz CLA in Monte Carlo + empirically.

**MSAI today vs. industry:** materially below convention. The
backend's Nautilus integration is correct (`TradingNodeConfig.strategies=[N
ImportableStrategyConfig]` already supports multi-strategy), but the
operator-facing UX leaks raw JSON and weight numbers that no peer
exposes.

## Options Considered

### Option A — Implement portfolio-redesign in current PR

- Adds ~30 files: `portfolio_backtests` router, risk_engine extensions,
  `portfolio-compose-v2` component, allocation-method picker,
  risk-config form, portfolio-backtest-runner, results-aggregator,
  plus tests.
- Pushes total PR size to ~130 files.
- Touches the hardened backend portfolio_run subsystem
  (`portfolio_service.py` 1100 LOC + `portfolio_job.py` 340 LOC +
  `compute_slots.py` semaphore + lease/heartbeat) WITHOUT a load test.

### Option B — Ship UI-completeness PR with portfolio-compose carved out, follow-up PR for redesign

- HARD-DISABLE `/live-trading/portfolio` route (not just hide nav).
- Remove "Deploy New Portfolio" link from `/live-trading`.
- Mark TJ-4 (paper deploy) as DEFERRED in the regression checklist.
- Open dedicated `/new-feature portfolio-backtest` PR with full
  PRD + research + council + plan-review + implementation cycle.

## Council Verdict (5/5 + Codex research)

**5 advisors all recommend Option B.** Codex research independently
recommended Option B.

| Advisor              | Verdict                            | Key reasoning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| -------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The Simplifier       | OBJECT(A), APPROVE(B)              | Current PR is already a mega-PR the council reluctantly accepted (§13). Bolting portfolio redesign on top compounds rollback hazard. "Public defect" is overstated — Pablo hasn't merged anything yet; feature-flagging the page is sufficient.                                                                                                                                                                                                                                                                                 |
| The Scalability Hawk | OBJECT(A), APPROVE(B) + conditions | `portfolio_service.py` is already 1100 LOC with production-grade orchestration. At 10× concurrent backtests, undetected bugs in lease accounting wedge all backtests platform-wide. Option A would push 1500–2500 additional LOC through the same path with one E2E pass. Drawdown correlation is industry-standard and we ship NEITHER. Conditions: load test, Prometheus metrics for allocation-compute + correlation-matrix + slot-wait histograms, drawdown correlation in v1, materialized cache schema designed up front. |
| The Pragmatist       | APPROVE(B), OBJECT(A)              | Current PR is 11,372 / -2,030 LOC across 2 sub-areas. Bolting 30 more files = textbook way to miss Monday. 30-min first step for (B) is trivial; 30-min first step for (A) is impossible (PRD + research + council required by workflow rules). Realistic delivery for portfolio redesign: 2–3 weeks.                                                                                                                                                                                                                           |
| The Contrarian       | CONDITIONAL on (B)                 | "Hide the JSON compose page" is **cosmetic** unless the route is hard-disabled. Direct URL access still loads `PortfolioCompose`. Recovery cost is underplayed — there is NO remove-strategy endpoint; mistaken adds require creating a new portfolio. Don't sweep unstaged compose/chart edits into PR without separate review. Decide cleanly: no composer in this PR, OR minimal equal-weight-only — ambiguity is the fatal flaw.                                                                                            |
| The Maintainer       | CONDITIONAL on (B)                 | Current branch is 98 files / 11,372 insertions. `portfolio_service.py` (1100 LOC) is already near the "too big to onboard" line. Adding allocation/risk/correlation there makes data flow harder to trace. Decision doc must mark this UX as intentionally deferred — don't sweep it in as another R-revision. New portfolio-backtest work must split `portfolio_service.py` responsibilities before adding more algorithms.                                                                                                    |
| Codex research       | Pick B                             | Adding 30 files of allocation methods + risk engine + portfolio backtest is "not a 30-file polish task; it changes product semantics, API contracts, backtest engine behavior, risk policy modeling, and result analytics." Concrete redesign sketch + 6 specific allocation methods to ship in v1 (equal/fixed/inverse-vol/vol-target, then HRP/risk-parity/MVO follow).                                                                                                                                                       |

## Decision

**Adopt Option B with HARD carve-out.** Specifically:

1. `/live-trading/portfolio` route returns 404 (hard-disabled). The
   route file is replaced with a thin `notFound()` guard; the original
   `PortfolioCompose` + `PortfolioStartDialog` wiring is preserved on
   the parent commit and recoverable via git history when the
   redesign lands.
2. "Deploy New Portfolio" link removed from `/live-trading`.
3. `tests/e2e/use-cases/ui-completeness/trader-journeys.md` marks
   TJ-4 as DEFERRED with link to this decision doc.
4. CHANGELOG entry for the carve-out + scope notice.
5. `state.md` checklist updated; PR description (when merging) calls
   out the carve-out + follow-up reference.

## Out of Scope for the Current PR

- Multi-select strategies UI
- Allocation-method picker (equal-weight, inverse-vol, vol-targeted,
  risk-parity, HRP, mean-variance, Black-Litterman, custom)
- Per-strategy risk policy (max position, max daily loss, stop-out,
  leverage cap, concentration limit, vol band, correlation cap)
- Portfolio-level backtest: combined equity curve, per-strategy
  contribution, correlation matrix (return AND drawdown), drawdown
  breakdown by strategy, stress tests, Monte Carlo, rolling
  correlation, allocation drift
- Removal of the manual JSON `Config` Textarea
- `portfolio_service.py` refactor / split (Maintainer's structural
  prerequisite for the follow-up PR)
- Load test plan for the portfolio_run subsystem at 10× / 100× scale

## Follow-up PR Scope (`portfolio-backtest`)

The dedicated PR will go through the full `/new-feature` workflow:

1. **PRD** with the operator narrative (Pablo's quote above is the
   north star).
2. **Research artifact** — re-validate industry patterns against
   current state of Composer / QC / Build Alpha / AlgoTest.
3. **Council ratification** of the v1 contract — which allocation
   methods to ship first, which risk-policy fields, what the portfolio
   backtest engine signature looks like (re-use Nautilus multi-strategy
   `BacktestNode` per gotcha #1, NOT a hand-rolled aggregator).
4. **Plan-review loop**.
5. **Backend**: new `portfolio_backtest_runs` table (additive
   migration), `services/portfolio_backtest/{allocators,risk,results}.py`,
   reuse `services/nautilus/backtest_runner.py` via multi-strategy
   `BacktestEngine`, materialized cache for correlation matrices.
6. **Frontend**: form-based multi-select compose, allocation-method
   `<Select>`, per-strategy risk-config drawer, portfolio-backtest
   `<RunDialog>`, results page with combined equity + attribution +
   correlation heatmap + drawdown-by-strategy.
7. **E2E coverage**: re-enable TJ-4 with the new UX.

## Tradeoffs Accepted

- Users see a degraded "portfolio compose" experience (route 404s) for
  the 2–3 weeks the follow-up PR is in flight. **Acceptable** because
  the current UX is rejected by the operator; surfacing 404 is
  strictly better than teaching users a deprecated mental model.
- The follow-up PR will need its own research/council/plan cycle.
  **That's the cost of doing it right.**
- The current PR's UI-completeness narrative now has an explicit
  exclusion. Documented here + in CHANGELOG + in the PR description.

## Provenance

- Pablo's two product directives, verbatim quoted in Context above.
- Codex industry-research transcript: ran 2026-05-17, gpt-5.5, xhigh
  reasoning, web-search=live, 336,515 tokens. Full output cached in
  session at `/tmp/portfolio-research-codex.txt`.
- 5-advisor council deliberation, dispatched in parallel per
  `.claude/skills/council/SKILL.md`. All 5 verdicts above. Per-advisor
  full transcripts in agent task outputs (session-scoped, not
  persisted).
- The decision-pattern (single-PR override + ratified deferral) mirrors
  the precedent in `docs/decisions/2026-05-16-ui-completeness-scope.md`
  §13.
