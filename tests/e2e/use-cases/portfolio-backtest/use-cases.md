# Portfolio Backtest — E2E Use Cases (graduated 2026-05-19)

> Graduated from `docs/plans/portfolio-backtest.md` Phase 3.2b after iter-3 verify-e2e PASS (`tests/e2e/reports/2026-05-19-portfolio-backtest-iter3.md`). 11/11 use cases pass; this file is the permanent regression target for the feature.
>
> **Updated from the plan-shipped UCs to reflect implementation reality:**
>
> - UC-PB-API-001: `strategy_ids` field replaces `allocations` (per F1c bridge).
> - UC-PB-UI-001 / UC-PB-UI-002: post-save lands on `/portfolio/[id]`; results live at `/portfolio/runs/[runId]` (not `/portfolio/{id}/results?run=`).
> - UC-PB-UI-003: post-promote redirect target is `/live-trading?revision=<id>` (the deployments list does not yet highlight the revision — v1.1 polish).
> - UC-PB-API-003: takes a `n_trials` body field for test-mode override.

**Project type:** fullstack — API-first ordering per CLAUDE.md.

---

## UC-PB-API-001: Create a portfolio via the strategy_ids bridge

**Interface:** API
**Intent:** Trader creates a multi-strategy portfolio by referencing strategy IDs; the backend auto-creates default GraduationCandidates per strategy.

**Setup (sanctioned):** ≥2 strategies registered with `default_config` containing `instrument_id` and `bar_type`. Re-seed via `PATCH /api/v1/strategies/{id}` if the registry walker stripped them on backend restart.

**Steps:**

1. `POST /api/v1/portfolios` body:
   ```json
   {
     "name": "...",
     "objective": "maximize_sharpe",
     "base_capital": 100000,
     "max_position_size": 0.25,
     "max_drawdown_halt": 0.2,
     "default_mode": "quick",
     "allocator_name": "equal_weight",
     "strategy_ids": ["<strategy_id_1>", "<strategy_id_2>"]
   }
   ```

**Verify:** 201 + `Location: /api/v1/portfolios/{id}` header; response body has `id`, `default_mode == "quick"`, `max_position_size == 0.25` echoed.

**Persistence:** `GET /api/v1/portfolios/{id}` returns the same payload. `GET /api/v1/portfolios/{id}/allocations` returns 2 allocations with auto-generated `candidate_id`s.

---

## UC-PB-API-002: Run a Quick backtest and inspect results

**Interface:** API
**Intent:** Trader runs a single-shot multi-strategy backtest in Quick mode and gets combined metrics back.

**Setup:** Portfolio from UC-PB-API-001 (2 strategies) + ingested AAPL daily bars covering the date range (`msai ingest stocks AAPL 2024-01-01 2024-12-31`).

**Steps:**

1. `POST /api/v1/portfolios/{portfolio_id}/runs` body `{"start_date": "2024-01-02", "end_date": "2024-12-31", "mode": "quick"}` → 201.
2. Poll `GET /api/v1/portfolios/runs/{run_id}` until `status == "completed"` (within 5 min).

**Verify:** Final response has `metrics` (sharpe/sortino/total_return/max_drawdown), `series` (combined equity timeline), `allocations` (resolved per-strategy entries with weights), `mode == "quick"`, `walk_forward_payload.per_strategy_equity` + correlation matrices populated.

**Persistence:** Reload `GET .../runs/{run_id}`; same payload returned.

---

## UC-PB-API-003: Run a Full optimization with walk-forward (smoke)

**Interface:** API
**Intent:** Trader runs the optimizer-driven mode against a small trial budget to validate the path.

**Setup:** Single-strategy portfolio with `default_mode: "full"`.

**Steps:**

1. `POST .../{id}/runs` body `{"start_date": "2024-01-02", "end_date": "2024-12-31", "mode": "full", "n_trials": 2}` → 201.
2. Poll until completed (within ~5 min for 2 trials; cache warmup runs each strategy once before optimization).

**Verify:** `is_metric` + `oos_metric` numeric; `optimization_trace` length matches `n_trials` (2); `walk_forward_payload` includes `windows`, `in_sample_scores`, `out_of_sample_scores`, `per_strategy_equity`, `return_correlation`, `drawdown_correlation`, `drawdown_breakdown`. `metrics.n_trials_override == 2`.

**Persistence:** Reload; same payload.

---

## UC-PB-API-004: Cancel a running portfolio run

**Interface:** API
**Intent:** Trader stops a long-running backtest mid-flight.

**Setup:** Long-running Full-mode run from UC-PB-API-003 (without the `n_trials` cap, so it runs minutes).

**Steps:**

1. `POST /api/v1/portfolios/runs/{run_id}/cancel`.

**Verify:** 200 + `status == "canceled"`. Subsequent `GET` confirms persisted state.

**Persistence:** Refresh; status remains `canceled`. (Tests both the API and the DB CHECK constraint extension — `'canceled'` is in the allowed enum after migration `b063ef2dd543`.)

---

## UC-PB-API-005: Promote a backtested portfolio to live (paper)

**Interface:** API
**Intent:** Trader takes a completed portfolio backtest result and materializes it as a LivePortfolio spec on a paper IB account.

**Setup:** Completed Quick-mode run from UC-PB-API-002.

**Steps:**

1. `POST .../runs/{run_id}/promote-to-live` body `{"account_id": "DUTEST123"}`.

**Verify:** 201 + `{"live_portfolio_id": ..., "live_portfolio_revision_id": ...}`. `GET /api/v1/live-portfolios/{live_portfolio_id}` returns the new spec with traceable name (`"<original> (run <run_id_first_8>)"`) and description (`"Promoted from PortfolioRun <run_id> (account_id=DUTEST123, mode=quick)"`).

**Persistence:** Reload `/live-portfolios/{id}`; same payload.

---

## UC-PB-API-006: Risk-engine blocks an over-leverage promotion

**Interface:** API
**Intent:** Real-money safety rail rejects a promotion that violates risk caps.

**Setup:** Completed Quick-mode run from a portfolio with `requested_leverage: 10.0` × `base_capital: 100000` (notional 1_000_000 > risk-engine cap 500_000).

**Steps:**

1. `POST .../runs/{run_id}/promote-to-live` body `{"account_id": "DUTEST123"}`.

**Verify:** 422 + `error.code == "RISK_VALIDATION_FAILED"` + error.message references the violated cap (leverage 10.0× / notional $1,000,000 > max $500,000).

---

## UC-PB-UI-001: Compose a portfolio via the form (no JSON)

**Interface:** UI
**Intent:** Trader composes a portfolio through form widgets with zero JSON typing.

**Setup:** Authenticated session (E2E auth bypass or persisted Entra ID storage state) + ≥2 strategies registered with runnable `default_config`.

**Steps:**

1. Navigate to `/portfolio/new`.
2. Fill `[data-testid="portfolio-name"]` with a name.
3. Open `[data-testid="strategy-multi-select"]` combobox; click 2 strategy options.
4. (Optional) Change Allocator / Objective via their `<Select>` controls.
5. (Optional) Adjust safety caps (max leverage, max position size, max drawdown halt) via their numeric inputs.
6. Click "Save Composition".

**Verify:** No `<textarea>` element on the page (HARD PRD requirement). Save lands on `/portfolio/{id}` showing the composition summary (Composition card with Objective / Allocator / Default mode / Initial capital / Requested leverage / Max position size / Max drawdown halt / Created timestamp).

**Persistence:** Reload `/portfolio/{id}`; same composition.

---

## UC-PB-UI-002: Run Quick mode from UI; see results page

**Interface:** UI
**Intent:** Trader triggers a backtest from the portfolio detail page and reviews the results visually.

**Setup:** Portfolio from UC-PB-UI-001.

**Steps:**

1. From `/portfolio/{id}`, click `[data-testid="run-backtest-button"]`.
2. In the dialog, pick Mode = Quick (default), confirm date range (defaults 2024-01-02 → 2024-12-31), click `[data-testid="run-submit"]`.
3. Wait for redirect to `/portfolio/runs/{run_id}`.
4. Wait for auto-poll to surface `status == "completed"`.

**Verify:** Results page renders (1) Combined Equity chart, (2) Per-Strategy Contribution stacked area chart with legend entries for each member strategy, (3) Return Correlation heatmap (`@nivo/heatmap`) + sortable companion table side-by-side, (4) Drawdown Correlation heatmap + sortable table side-by-side, (5) Drawdown Breakdown table with per-strategy max DD / duration / recovery. "Deploy as Live Portfolio" button visible.

**Persistence:** Navigate away and back to `/portfolio/runs/{run_id}`; same payload renders.

---

## UC-PB-UI-003: Promote backtested portfolio to live (paper)

**Interface:** UI
**Intent:** Trader takes a completed backtest result and creates a paper-money LivePortfolio spec from it.

**Setup:** Completed Quick-mode run from UC-PB-UI-002.

**Steps:**

1. From `/portfolio/runs/{run_id}`, click "Deploy as Live Portfolio".
2. In the "Promote to Live (Paper)" modal, leave the default `DUTEST123` in `[data-testid="promote-account-input"]`.
3. Click "Promote".

**Verify:** Toast `"Portfolio promoted to live..."` appears. `GET /api/v1/live-portfolios/` lists a new entry with name `"<portfolio name> (run <run_id_first_8>)"` and description referencing the source run + `account_id=DUTEST123, mode=quick`.

**Known v1.1 hygiene gap:** post-promote `router.push('/live-trading?revision=<revision_id>')` doesn't highlight the new entity on the deployments list page (the list shows `LiveDeployment` runtime entities, not `LivePortfolio` specs). LivePortfolio is correctly created; deploy flow continues via `/live-trading/portfolios/<id>` (TBD).

---

## UC-PB-UI-004: Unified backtests history with type filter

**Interface:** UI
**Intent:** Trader views single-strategy and portfolio backtests in one list, filterable by type.

**Setup:** ≥1 single-strategy backtest + ≥1 portfolio backtest in the DB (UC-PB-API-002 + any prior single-strategy backtest).

**Steps:**

1. Navigate to `/backtests`.
2. Click `[data-testid="backtest-type-filter"]`; choose "Portfolio".

**Verify:** Table narrows to portfolio-type rows only. Each row carries a "Portfolio" badge in the type column. Switching to "Single" narrows to single-strategy rows with "Single" badge. "All" shows both.

**Persistence:** Reload `/backtests?type=portfolio`; same filter state.

---

## UC-PB-NEG-001: Member-strategy failure aborts the run

**Interface:** API
**Intent:** A strategy that raises during execution surfaces per-strategy error attribution (not a silent failure).

**Setup:** Portfolio with `intentionally_failing_strategy` (id `a8825977-64a3-4768-8bbe-302a399f9c51`) as a member.

**Steps:**

1. `POST /api/v1/portfolios` with `strategy_ids: ["a8825977-..."]`.
2. `POST /api/v1/portfolios/{id}/runs` with `mode: "quick"`.
3. Poll until terminal.

**Verify:** `status == "failed"`; `error_message` names the failing strategy (`"1 strategy failed: intentionally_failing_strategy"`); `metrics.per_strategy_errors[0]` has `strategy_id`, `strategy_name`, `error_type == "RuntimeError"`, `message` containing the strategy's intentional-failure traceback. (Satisfies PRD US-002a edge case "Member strategy raises an exception during backtest → per-strategy error block identifying which member raised.")

---

## Smoke vs full coverage

- **Smoke set** (~2 min wall clock; OK for PR CI): UC-PB-API-001, UC-PB-API-004, UC-PB-API-006, UC-PB-UI-004 (no backtest execution; just contract validation).
- **Full set** (~15 min wall clock; OK for nightly): all 11 use cases including the Full-mode optimization at `n_trials=2`.

## Cross-references

- PRD: [`docs/prds/portfolio-backtest.md`](../../../docs/prds/portfolio-backtest.md)
- Plan: [`docs/plans/portfolio-backtest.md`](../../../docs/plans/portfolio-backtest.md)
- verify-e2e iter-3 report: [`tests/e2e/reports/2026-05-19-portfolio-backtest-iter3.md`](../../reports/2026-05-19-portfolio-backtest-iter3.md)
- Predecessor decision: [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../../../docs/decisions/2026-05-17-portfolio-backtest-deferred.md)
