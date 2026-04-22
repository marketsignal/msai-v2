# PRD: Backtest Results — Charts & Trade Log

**Version:** 1.0
**Status:** Draft (ready for `/superpowers:writing-plans`)
**Author:** Claude + Pablo
**Created:** 2026-04-21
**Last Updated:** 2026-04-21

---

## 1. Overview

Today, every backtest that completes on msai-v2 silently throws away its richest analytics: the backend runs QuantStats (Pyfolio's maintained successor) and produces a full tear sheet with 60+ risk stats and 20+ charts, writes it to an HTML file, and exposes it as a downloadable artifact — but the React detail page hardcodes empty arrays for Equity Curve, Drawdown, Monthly Returns, and Trade Log, displaying blank components. This PRD fixes that gap by persisting a canonical normalized daily returns/equity series alongside the existing metrics and trades, wiring four already-scaffolded React components to real data, and embedding the full QuantStats HTML report inside the detail page via an authenticated Next.js proxy so every completed backtest gets a Pablo-accessible Pyfolio-equivalent view with one-click download preserved.

## 2. Goals & Success Metrics

### Goals

- **Primary:** Every completed backtest renders populated charts + trade log in the React detail page, plus an "Open Full Report" tab showing the full QuantStats tear sheet inside the app.
- **Secondary:** Establish canonical-series persistence in `Backtest.series` JSONB so future analytics features (benchmark overlay, rolling metrics, yearly tables) become view-layer transforms rather than new schema writes.
- **Secondary:** Offload the trade log to a paginated sibling endpoint so high-trade-count backtests (100k+ fills) don't crash the browser.

### Success Metrics

| Metric                                                             | Target              | How Measured                                                               |
| ------------------------------------------------------------------ | ------------------- | -------------------------------------------------------------------------- |
| % of completed backtests rendering non-empty charts in detail page | 100% (post-PR)      | E2E UC: run new backtest, visit detail page, assert 4 components populated |
| `Backtest.series` JSONB payload size per backtest                  | < 1 MB (typical)    | `msai_backtest_results_payload_bytes` Prometheus histogram                 |
| `/results` endpoint p99 latency (post-pagination)                  | < 200ms             | Existing latency histogram (FastAPI middleware)                            |
| Equity chart render time on detail page open                       | < 500ms interactive | Browser DevTools perf trace on 1yr-daily + 1yr-minute backtests            |
| Legacy backtests (pre-PR) don't 500 on detail page open            | 100%                | E2E UC: open a backtest created before migration, assert graceful fallback |

### Non-Goals (Explicitly Out of Scope)

- ❌ Full native React port of QuantStats (C.3 — rejected by council; future PR if iframe UX hurts)
- ❌ Benchmark overlay (SPY/QQQ comparison on equity chart) — future PR, enabled cheaply by canonical `series`
- ❌ Rolling metrics (3m/6m/12m Sharpe, rolling volatility) — future PR, same rationale
- ❌ Entry/exit round-trip pairing in trade log (render individual fills in v1)
- ❌ CSV/Excel export of trades
- ❌ Public-shareable link / signed URL / anonymous backtest view (outside viewers get the downloaded HTML file instead in v1)
- ❌ Historical backfill of `series` for pre-PR backtests (left as `series_status = "not_materialized"`)
- ❌ Parse-QuantStats-HTML-on-GET (B.2) — rejected by all 5 advisors
- ❌ Changes to how QuantStats is invoked in the worker (its output remains ground truth for downloadable HTML)

## 3. User Personas

### Pablo (solo operator)

- **Role:** Sole user of the msai-v2 platform; runs backtests interactively via UI and overnight batches.
- **Permissions:** Full access to all backtests (single-tenant app).
- **Goals:** See a Pyfolio-equivalent tear sheet inside the detail page for every completed backtest without leaving the app; retain the option to download the HTML file when sharing.

### Outside viewer (future, v1 via downloaded file only)

- **Role:** Investor, collaborator, or demo audience who receives a backtest report from Pablo.
- **Permissions:** None in-app (no account). Receives an emailed/attached QuantStats HTML file.
- **Goals:** Read the full tear sheet offline, independently of the msai-v2 stack.
- **v1 interaction with this feature:** zero — outside viewers consume the HTML file Pablo downloads via the existing `/report` endpoint. The in-app iframe remains Pablo-only (authenticated).

## 4. User Stories

### US-001: Populated detail page on backtest completion

**As** Pablo
**I want** every completed backtest's detail page to show an equity curve, drawdown chart, monthly returns heatmap, and paginated trade log
**So that** I can judge strategy performance visually without downloading any file

**Scenario:**

```gherkin
Given a strategy is registered
When I submit a backtest via POST /api/v1/backtests/run with valid symbols + window
And the backtest completes successfully (status=completed, series_status=ready)
Then GET /api/v1/backtests/{id}/results returns {metrics, series_status: "ready", series: {...}}
And navigating to /backtests/{id} in the UI displays non-empty Equity Curve, Drawdown, and Monthly Returns charts
And the Trade Log shows the first 100 trades (or fewer if <100)
```

**Acceptance Criteria:**

- [ ] `Backtest.series` JSONB column populated in `_finalize_backtest()` atomically with `metrics` and `report_path`
- [ ] `series` payload contains daily-compounded entries with fields: `date (ISO)`, `equity (float)`, `drawdown (float)`, `daily_return (float)`
- [ ] `series.monthly_returns` sub-payload with `[{month: "YYYY-MM", pct: float}]`
- [ ] `GET /api/v1/backtests/{id}/results` response includes `series` and `series_status` when `series_status = "ready"`
- [ ] Frontend `<EquityCurveChart>`, `<DrawdownChart>`, `<MonthlyReturnsHeatmap>`, `<TradeLog>` render real data (no hardcoded `[]`)
- [ ] Reloading the detail page shows the same data (persistence works)

**Edge Cases:**

| Condition                                                          | Expected Behavior                                                                                                                                                 |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backtest produces zero trades (e.g., strategy never fires)         | Charts still render from `series` (equity stays flat at initial capital); Trade Log shows "No trades" empty state                                                 |
| `series_status = "failed"` (compute error, status still completed) | API returns 200 with `series_status = "failed"`, metrics still populated, UI shows "Charts unavailable: compute error" banner; 6 metric cards still render        |
| Backtest still running (status ≠ completed)                        | `/results` returns 202 or existing error envelope (unchanged behavior from current `/results` pre-completion race — already handled via polling loop from PR #40) |
| Payload exceeds 1 MB JSONB                                         | Worker still writes, but logs WARN with `msai_backtest_results_payload_bytes` histogram reading; ops investigate                                                  |

**Priority:** Must Have

---

### US-002: Full QuantStats report embedded in-app

**As** Pablo
**I want** to click "Open Full Report" on the detail page and see the QuantStats tear sheet rendered inside the app
**So that** I get access to all 60+ stats and 20+ charts without downloading

**Scenario:**

```gherkin
Given a backtest has completed and its QuantStats HTML is persisted at Backtest.report_path
When I click "Open Full Report" on /backtests/{id}
Then the detail page shows a tab or expanded section with an iframe
And the iframe src points to a same-origin Next.js route /api/backtests/{id}/report
And that route server-side-authenticates with my session, fetches the backend's /api/v1/backtests/{id}/report, and streams the HTML to the iframe
And the full QuantStats tear sheet renders (all stats + all charts) inside the iframe
```

**Acceptance Criteria:**

- [ ] New Next.js route handler `frontend/src/app/api/backtests/[id]/report/route.ts` (or similar, following existing Next.js 15 App Router conventions)
- [ ] Route handler reads the user's session/token server-side and attaches it to the backend fetch (`Authorization: Bearer <token>` OR `X-API-Key: <key>` depending on auth mode)
- [ ] Route streams the HTML response body to the iframe (sets correct `Content-Type: text/html`; preserves size; no in-memory buffering of the whole file)
- [ ] Iframe `src` in the detail page points at the Next.js proxy route, NOT at the backend `/api/v1/backtests/{id}/report` directly
- [ ] No token appears in the URL query string (chairman's explicit rejection; preserves it out of browser history)
- [ ] Existing "Download Report" button behavior unchanged (still does authenticated fetch → blob download)

**Edge Cases:**

| Condition                                           | Expected Behavior                                                               |
| --------------------------------------------------- | ------------------------------------------------------------------------------- |
| Backtest has no `report_path` (compute failed)      | "Open Full Report" button disabled with tooltip "Report not available"          |
| User's session expired                              | Proxy returns 401 → detail page shows "Please sign in" (existing auth-error UX) |
| Backend `/report` returns 404 (file gone / cleanup) | Proxy returns 404 → iframe displays a generic "Report not found" card           |
| QS HTML is 5MB+ and slow to render                  | Iframe renders progressively (HTML streams); acceptable for v1 solo use         |

**Priority:** Must Have

---

### US-003: Download QuantStats HTML (preserved)

**As** Pablo
**I want** to download the QuantStats HTML file for archive or sharing with an outside viewer
**So that** I can send it by email/attachment or save it offline

**Scenario:**

```gherkin
Given a backtest has completed
When I click "Download Report" on /backtests/{id}
Then I receive a local .html file via the browser download flow
And I can open the file standalone in any browser
And I can forward it to someone outside the msai-v2 platform
```

**Acceptance Criteria:**

- [ ] Existing "Download Report" button continues to work exactly as today
- [ ] Backend `GET /api/v1/backtests/{id}/report` endpoint behavior unchanged (FileResponse, header-auth gated)
- [ ] Frontend fetch + blob-download flow unchanged

**Edge Cases:**

| Condition                   | Expected Behavior       |
| --------------------------- | ----------------------- |
| Report file missing on disk | 404 (existing behavior) |
| User not authenticated      | 401 (existing behavior) |

**Priority:** Must Have (regression protection, not new work)

---

### US-004: Paginated trade audit

**As** Pablo
**I want** to scroll through every trade the strategy made, paginated 100 rows at a time
**So that** I can audit individual fills even when the backtest produced thousands

**Scenario:**

```gherkin
Given a backtest produced 500 individual Nautilus fills
When I scroll to the Trade Log section on /backtests/{id}
Then GET /api/v1/backtests/{id}/trades?page=1&page_size=100 returns the first 100 fills
And the Trade Log shows columns: Timestamp, Instrument, Side, Quantity, Price, P&L, Commission
And a pagination control shows "Page 1 of 5" with Next/Previous buttons
When I click Next
Then GET /api/v1/backtests/{id}/trades?page=2&page_size=100 loads the next 100 fills
```

**Acceptance Criteria:**

- [ ] New `GET /api/v1/backtests/{id}/trades?page=N&page_size=100` endpoint with response shape `{items: [...], total: int, page: int, page_size: int}` per `.claude/rules/api-design.md`
- [ ] `page_size` capped server-side (max 500) to prevent abuse
- [ ] Trades sorted by `executed_at ASC` by default (matches existing `/results` ordering)
- [ ] `/results` endpoint stops returning `trades` inline — removed from `BacktestResultsResponse` Pydantic schema
- [ ] Frontend `<TradeLog>` uses the new paginated endpoint via `apiGet` with page state
- [ ] `BacktestTradeItem` TS type updated to match backend individual-fill shape: `{id, instrument, side, quantity, price, pnl, commission, executed_at}`
- [ ] Entry/Exit/Duration columns removed from the TradeLog table (no round-trip pairing this PR)

**Edge Cases:**

| Condition                                 | Expected Behavior                                                             |
| ----------------------------------------- | ----------------------------------------------------------------------------- |
| Backtest has 0 trades                     | `items: []`, `total: 0`. UI shows "No trades executed" empty state            |
| `page` > max available page               | Returns empty `items`, correct `total`, `page`, `page_size` (no 404)          |
| `page_size` > 500 (client tries to abuse) | Server clamps to 500 and returns accordingly (or 422 — decide in plan review) |
| `page_size` ≤ 0 or `page` ≤ 0             | 422 with validation error                                                     |

**Priority:** Must Have

---

### US-005: Legacy backtests render gracefully

**As** Pablo
**I want** backtests created before this feature shipped to not break the detail page
**So that** my 40+ historical backtests remain browsable

**Scenario:**

```gherkin
Given a backtest was completed before this PR shipped (series is NULL in the DB)
When I navigate to /backtests/{legacy-id}
Then GET /api/v1/backtests/{legacy-id}/results returns metrics, series_status: "not_materialized", no series field
And the UI shows the 6 existing metric cards populated
And the 4 chart components show a "Analytics not available for backtests run before 2026-04-21" empty state
And the "Download Report" button still works (QS HTML was generated pre-PR)
And the "Open Full Report" iframe still works (uses report_path which exists pre-PR)
```

**Acceptance Criteria:**

- [ ] `Backtest.series` column nullable (ALTER TABLE adds column with DEFAULT NULL)
- [ ] `Backtest.series_status` column defaults to `"not_materialized"` for existing rows (migration sets default on historical data)
- [ ] API returns `series_status: "not_materialized"` for pre-PR backtests
- [ ] Frontend distinguishes the three states (`ready`/`not_materialized`/`failed`) and renders a dedicated empty-state message for each

**Edge Cases:**

| Condition                                                           | Expected Behavior                                       |
| ------------------------------------------------------------------- | ------------------------------------------------------- |
| Legacy backtest has valid `report_path` (QS HTML) but NULL `series` | Charts empty w/ message; Full Report iframe still works |
| Legacy backtest missing both `report_path` AND `series` (unusual)   | Charts + Full Report both show "Analytics unavailable"  |

**Priority:** Must Have

---

### US-006: Compute-failed backtests disambiguate from legacy

**As** Pablo
**I want** to distinguish "backtest ran but analytics materialization failed" from "backtest is from before this feature"
**So that** I know whether to retry the compute or accept a historical gap

**Scenario:**

```gherkin
Given a backtest completes but series materialization raises an exception in the worker
When the worker catches the exception
Then _finalize_backtest() writes series_status: "failed" and leaves series NULL
And the backtest row's status remains "completed" (core run succeeded)
And GET /api/v1/backtests/{id}/results returns series_status: "failed"
And the UI shows charts in a red-bordered failure state with message "Analytics computation failed — metrics shown below are still valid; full report download may still work"
And a structured log event backtest_series_materialization_failed is emitted with exc_info
```

**Acceptance Criteria:**

- [ ] Worker wraps the series-materialization step in a try/except; exception → `series_status = "failed"`, log event, continue to commit metrics + report_path normally
- [ ] The series-materialization failure does NOT cascade into marking the backtest as failed (status stays `completed` because the run succeeded)
- [ ] UI renders distinct visual treatment for `series_status = "failed"` vs `"not_materialized"`
- [ ] Structured log `backtest_series_materialization_failed` at WARNING with `exc_info=True`, `backtest_id`, and `nautilus_version` fields

**Edge Cases:**

| Condition                                                              | Expected Behavior                                           |
| ---------------------------------------------------------------------- | ----------------------------------------------------------- |
| `account_df` is empty (no bars processed)                              | `series_status = "failed"` with a distinct reason field     |
| QuantStats HTML generation succeeds but series build fails (edge case) | Full Report still viewable; native charts show failed state |

**Priority:** Must Have

## 5. Technical Constraints

### Known Limitations

- **No historical backfill.** Pre-PR backtests stay at `series_status = "not_materialized"`. A backfill job would require re-reading the Nautilus catalog + re-running analytics for every historical backtest; deferred as a follow-up PR.
- **Daily-grain charts only.** Minute-bar strategies get a daily-compounded equity curve (to keep JSONB payload < 1 MB). Intraday-resolution equity is accessible only via the QuantStats HTML iframe/download. Council accepted this trade-off.
- **Individual-fill trade log.** Trade rows render as individual Nautilus fills, not entry/exit round-trips. Multi-leg strategies will see each leg as a separate row.
- **Iframe desktop-only UX.** QuantStats HTML is not mobile-responsive and not dark-mode. Acceptable for v1 because outside viewers receive the downloaded file (US-003 pattern), not a live link.
- **No CSV export of trades.** `/trades` endpoint is paginated JSON only.

### Dependencies

- **Requires:** `Backtest.metrics` JSONB (exists), `Backtest.report_path` (exists), `trades` table (exists), `analytics_math.build_series_from_returns()` (exists), `report_generator._normalize_report_returns()` (exists — will be deduped).
- **Blocked by:** Nothing.

### Integration Points

- **NautilusTrader:** `BacktestResult.account_df` (returns timeseries) must be passed from `backtest_runner._extract_returns_series` into `_finalize_backtest` for series materialization. Already in subprocess memory at QuantStats invocation time — we just stop discarding it.
- **QuantStats:** unchanged invocation. The persisted `series` uses the same normalization that feeds QuantStats (dedupe `build_series_from_returns` vs `_normalize_report_returns` into one path).
- **Next.js App Router:** new route handler at `frontend/src/app/api/backtests/[id]/report/route.ts` for the iframe proxy. Follows Next.js 15 server-side route conventions.
- **Postgres 16:** add two columns to `backtests`: `series JSONB NULL`, `series_status String(32) NOT NULL DEFAULT 'not_materialized'`. Alembic migration.

## 6. Data Requirements

### New Data Models

- **`Backtest.series` (JSONB, NULL allowed):** canonical daily-normalized analytics payload. Shape:

  ```json
  {
    "daily": [
      {"date": "2024-01-02", "equity": 100000.0, "drawdown": 0.0, "daily_return": 0.0},
      {"date": "2024-01-03", "equity": 100250.5, "drawdown": 0.0, "daily_return": 0.0025},
      ...
    ],
    "monthly_returns": [
      {"month": "2024-01", "pct": 0.0512},
      {"month": "2024-02", "pct": -0.0134},
      ...
    ]
  }
  ```

  Shape is stable for v1. Additional views (yearly table, rolling metrics, benchmark) become derived transforms of `daily` + `monthly_returns` in future PRs without schema changes.

- **`Backtest.series_status` (String(32), NOT NULL, DEFAULT 'not_materialized'):** `Literal["ready", "not_materialized", "failed"]`.
  - `"ready"` — worker successfully persisted `series` payload
  - `"not_materialized"` — pre-PR row (default for historical backtests) OR future backtests where materialization was explicitly skipped
  - `"failed"` — worker attempted materialization but hit an exception; `series` stays NULL, structured log captured

### Data Validation Rules

- `series.daily[].date` must be a valid ISO date (YYYY-MM-DD); the series is strictly ordered ASC
- `series.daily[].equity` must be positive float; `drawdown` is non-positive float ≤ 0
- `series.daily[].daily_return` is a simple return (not log return); reconciles to `equity_t = equity_{t-1} * (1 + daily_return_t)` within floating-point tolerance
- `series.monthly_returns[].month` must be `YYYY-MM` format; no duplicates
- `series_status` enum enforced both in DB (CHECK constraint optional) and in Pydantic `Literal`

### Data Migration

- **Alembic migration:** adds `series JSONB NULL` and `series_status String(32) NOT NULL DEFAULT 'not_materialized'` to `backtests`.
- **No backfill.** Existing rows default to `series_status = "not_materialized"`.
- **Downgrade path:** drop both columns. (Backtests completed post-PR would lose analytics on downgrade, but the downgrade path is only used for dev rollback — production is single-user single-VM, no blue/green.)

## 7. Security Considerations

- **Authentication:** all new endpoints (`/trades`, extended `/results`) continue to require `Depends(get_current_user)` — inherits Azure Entra ID JWT + `X-API-Key` fallback from existing auth.
- **Authorization:** single-tenant app, no RBAC. Pablo sees all backtests.
- **Iframe proxy auth model:**
  - Next.js route handler runs server-side (not client-side) and reads the session token from the incoming request (same Next.js session mechanism existing API calls use).
  - Attaches it as `Authorization: Bearer <token>` header on the upstream fetch to the backend.
  - Streams response back to iframe.
  - **Explicit rejection of token-in-query** (chairman ruling): no URL like `/report?token=abc`. Prevents token leakage to browser history, referrer headers, server logs, or bookmarks.
  - Out of scope for v1: short-lived signed URLs (would allow sharing a single report without login, but creates a separate auth story).
- **Data Protection:**
  - `series` JSONB may contain monetary data (equity in dollars) — same sensitivity class as existing `metrics` JSONB, no new classification needed.
  - Trade log (`/trades` paginated) may contain per-trade P&L — same sensitivity. Existing auth gate is sufficient.
- **Audit:**
  - Worker logs `backtest_series_materialized` with `backtest_id`, `series_daily_rows`, `series_monthly_rows`, `payload_bytes` at INFO.
  - On failure: `backtest_series_materialization_failed` at WARNING with `exc_info`.
  - `msai_backtest_results_payload_bytes` Prometheus histogram on `/results` response size (catches accidental minute-bar leaks or someone removing the daily-compound step).
  - `/trades` endpoint usage counted via `msai_backtest_trades_page_count{page_size}` counter for rough request profiling.

## 8. Open Questions

> Questions that need answers before or during implementation. Each has a proposed path.

- [ ] **QuantStats HTML structure stability across minor versions.** Does `report.html()` produce significantly different HTML between QS versions? (Addressed in Phase 2 research — pin QS version via `pyproject.toml` if drift found, or validate the iframe across 2–3 versions.)
- [ ] **3–5 MB iframe render behavior.** Does the browser render a large QuantStats HTML file in an iframe without memory/UX issues? (Addressed in Phase 4 TDD — 30-min spike on an actual large backtest before committing the iframe work.)
- [ ] **5-year minute-bar `account_df` series-build cost.** How long does `build_series_from_returns()` + daily-compound take on 500k rows? What's the resulting JSONB payload size after daily compound? (Addressed in Phase 4 TDD — benchmark; if >30s or >1 MB, add downsample WARN gate or split into a post-commit arq job.)
- [ ] **`page_size` overrun handling: clamp to 500 or reject with 422?** (Plan review.)
- [ ] **Which Next.js 15 route handler auth pattern to use for the iframe proxy?** Server Actions vs Route Handlers vs middleware. (Plan review — lean Route Handler `frontend/src/app/api/backtests/[id]/report/route.ts` per standard App Router convention.)
- [ ] **Should `series_status = "failed"` backtests be retry-able via a new `POST /api/v1/backtests/{id}/re-materialize` endpoint?** (Out of scope for v1; defer.)

## 9. References

- **Discussion Log:** `docs/prds/backtest-results-charts-and-trades-discussion.md`
- **Decision doc (council verdict):** `docs/decisions/backtest-results-charts-and-trades.md`
- **Related PRDs:**
  - `docs/prds/backtest-auto-ingest-on-missing-data.md` (PR #40, context on existing `/results` endpoint shape)
  - `docs/prds/backtest-failure-surfacing.md` (PR #39, error envelope structure reused in US-006)
- **Competitor Reference:**
  - **Pyfolio** (Quantopian's library) — original 60+ stat tear sheet format Pablo cited as the gold standard. QuantStats is its maintained successor, already integrated in msai-v2.
  - **QuantConnect** Lean research reports — single-page scrollable tear sheet with benchmark overlay. Similar UX model to what this PR delivers via iframe.
- **Codebase anchors** (from Phase 0 recon + council):
  - `backend/src/msai/workers/backtest_job.py::_finalize_backtest()` — atomic write boundary
  - `backend/src/msai/services/analytics_math.py::build_series_from_returns` — source of equity/drawdown math
  - `backend/src/msai/services/report_generator.py::_normalize_report_returns` — daily compounding (dedupe target)
  - `backend/src/msai/models/portfolio_run.py::series` — naming precedent
  - `backend/src/msai/api/backtests.py:410` — current `/results` handler
  - `frontend/src/app/backtests/[id]/page.tsx:203` — hardcoded `equityCurve: []` that this PR kills
  - `frontend/src/components/backtests/{results-charts,trade-log}.tsx` — scaffolded components to wire

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                          |
| ------- | ---------- | -------------- | ---------------------------------------------------------------- |
| 1.0     | 2026-04-21 | Claude + Pablo | Initial PRD; scope locked via 5-advisor council + Codex chairman |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design (Phase 3.2 `/superpowers:writing-plans`)
