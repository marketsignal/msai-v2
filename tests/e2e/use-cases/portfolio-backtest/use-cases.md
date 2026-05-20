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

**Verify:** `is_metric` + `oos_metric` numeric; `optimization_trace` length matches `n_trials` (2); `walk_forward_payload` includes `windows`, `in_sample_scores`, `out_of_sample_scores`, `per_strategy_equity`, `return_correlation`, `drawdown_correlation`, `drawdown_breakdown`. (Note: at completion the optimizer overwrites `metrics` with the per-trial output keys, so `metrics.n_trials_override` is not present in the final payload — assert via `optimization_trace` length instead.)

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

**Verify:** Toast `"Portfolio promoted. Pick deployment options to start trading."` appears, then the inline `PortfolioStartDialog` opens for deployment-options entry. `GET /api/v1/live-portfolios/` lists a new entry with name `"<portfolio name> (run <run_id_first_8>)"` and description referencing the source run + `account_id=DUTEST123, mode=quick`.

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

## UC-PB-CLI-001: Operator composes a portfolio from existing strategies via CLI

**Interface:** CLI
**Intent:** An operator at a terminal composes a multi-strategy portfolio without opening a browser, then sees it listed back.

**Setup (sanctioned):** ≥2 strategies registered with `default_config` containing `instrument_id` and `bar_type` (the same fixtures UC-PB-API-001 expects).

**Steps:**

1. `msai portfolio create-from-strategies --name "CLI Smoke" --strategy-id <s1> --strategy-id <s2> --base-capital 100000 --max-position-size 0.25 --max-drawdown-halt 0.2 --objective maximize_sharpe --default-mode quick`
2. `msai portfolio list`

**Verify:** Step 1 stdout is a JSON object with the new portfolio's `id`, `name == "CLI Smoke"`, `default_mode == "quick"`, `max_position_size == 0.25`, exit code 0. Step 2 stdout lists a row matching the new id.

**Persistence:** Start a fresh shell and run `msai portfolio list` → the new portfolio is still listed.

---

## UC-PB-CLI-002: Operator runs a Quick backtest end-to-end via CLI

**Interface:** CLI
**Intent:** An operator launches a Quick-mode portfolio backtest from the terminal and inspects results — never touching the UI.

**Setup:** Portfolio from UC-PB-CLI-001 + ingested AAPL daily bars (via `msai ingest stocks AAPL 2024-01-01 2024-12-31`).

**Steps:**

1. `msai portfolio run <portfolio_id> 2024-01-02 2024-12-31 --mode quick` → exit 0; stdout contains the new `run_id`.
2. Loop `msai portfolio run-show <run_id>` until stdout's `status == "completed"` (within 5 min).
3. `msai portfolio run-report <run_id>` → QuantStats HTML report streamed to stdout (exit 0).

**Verify:** Step 2's `run-show` payload exposes numeric `metrics.sharpe`, `metrics.total_return`, `metrics.max_drawdown`. Step 3 emits the HTML report with exit 0 (the report body is the contract of `/api/v1/backtests/{id}/report`; the metrics surface for the operator is `run-show`, not `run-report`).

**Persistence:** New shell, `msai portfolio run-show <run_id>` → still `completed` with the same metrics.

---

## UC-PB-CLI-003: Operator cancels a running Full backtest via CLI

**Interface:** CLI
**Intent:** An operator who launched an expensive Full backtest decides to abort it from the terminal and confirms it stayed canceled.

**Setup:** A separate portfolio with `--default-mode full` + AAPL bars from UC-PB-CLI-002.

**Steps:**

1. `msai portfolio run <portfolio_id> 2024-01-02 2024-12-31 --mode full --n-trials 500` → exit 0; stdout `run_id`. (Use ≥500 trials so the cancel-window stays open long enough on dev/CI stacks — `--n-trials 50` can complete in seconds and race the cancel.)
2. While the next `msai portfolio run-show <run_id>` reports `status` ∈ `{queued, running}`, run `msai portfolio cancel <run_id>` → exit 0; stdout JSON has `status == "canceled"`.
3. `msai portfolio run-show <run_id>` → `status == "canceled"`.

**Verify:** Cancel command exits 0 and prints `"status": "canceled"`. (This is exactly the path iter-15 hardened via `_execute_candidate_backtests` cancel_check — terminal-state guard refuses to lift the run back to running.)

**Persistence:** Open a new shell, `msai portfolio run-show <run_id>` → still `canceled`.

---

## UC-PB-CLI-004: Operator promotes a completed run to a paper live portfolio via CLI

**Interface:** CLI
**Intent:** An operator promotes a finished Quick run to a paper LivePortfolio via the terminal, mirroring the UI promote flow.

**Setup:** Completed Quick run from UC-PB-CLI-002.

**Steps:**

1. `msai portfolio promote-to-live <run_id> --account-id DUTEST123` → exit 0; stdout is a JSON object representing the new LivePortfolio.
2. `msai live-portfolio list` (or the equivalent listing command shipped in the project's `live-portfolio` sub-app).

**Verify:** Step 1 stdout has `name` ending in `"(run <run_id_first_8>)"`, `description` referencing the source run id + `account_id=DUTEST123, mode=quick`; exit 0. Step 2 stdout lists the new LivePortfolio.

**Negative path (same command, real-money id):** `msai portfolio promote-to-live <run_id> --account-id U1234567` exits non-zero, stderr contains `PAPER_ONLY_ENFORCED` or HTTP 422 with that error code in the body.

**Persistence:** New shell, list live portfolios → the new entry is still listed.

---

## Smoke vs full coverage

- **Smoke set** (~2 min wall clock; OK for PR CI): UC-PB-API-001, UC-PB-API-004, UC-PB-API-006, UC-PB-UI-004, UC-PB-CLI-001 (no backtest execution; just contract validation).
- **Full set** (~20 min wall clock; OK for nightly): all 15 use cases including the Full-mode optimization at `n_trials=2` and the CLI cancel/promote paths.

## Cross-references

- PRD: [`docs/prds/portfolio-backtest.md`](../../../docs/prds/portfolio-backtest.md)
- Plan: [`docs/plans/portfolio-backtest.md`](../../../docs/plans/portfolio-backtest.md)
- verify-e2e iter-3 report: [`tests/e2e/reports/2026-05-19-portfolio-backtest-iter3.md`](../../reports/2026-05-19-portfolio-backtest-iter3.md)
- Predecessor decision: [`docs/decisions/2026-05-17-portfolio-backtest-deferred.md`](../../../docs/decisions/2026-05-17-portfolio-backtest-deferred.md)
