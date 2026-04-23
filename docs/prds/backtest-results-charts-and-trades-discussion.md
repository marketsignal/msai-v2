# PRD Discussion: Backtest Results — Charts & Trade Log

**Status:** Complete — ratified via council 2026-04-21
**Started:** 2026-04-21
**Participants:** Pablo, Claude

## Original User Stories (inferred from CONTINUITY item #7 + UI-RESULTS-01 + PR #40 live-demo flag)

- **As a user** running a backtest via the UI, I want to see a visual equity curve over the backtest window so I can judge strategy performance at a glance — not just a single Total Return number.
- **As a user**, I want to see the drawdown series alongside the equity curve so I can assess risk, not just returns.
- **As a user**, I want to see monthly returns in a heatmap so I can spot seasonality and regime changes.
- **As a user**, I want to see every trade the strategy made (entry/exit/P&L) in a sortable table so I can audit individual decisions.
- **As a user (existing)**, I still want the one-click QuantStats HTML report download I have today — that stays.

**Discovered gap (triggering this PRD):** The React detail page at `frontend/src/app/backtests/[id]/page.tsx:203,309` hardcodes `equityCurve: []` and `<TradeLog trades={[]} />` with a comment saying "backend doesn't return these yet". The backend DOES compute all this data via QuantStats and persists individual trade fills to Postgres — the wiring between backend and UI is just missing.

## Code Ground Map (from Phase 0 recon 2026-04-21)

### Backend

| Component                     | File                                                                                                          | State                                                                                                                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/results` handler            | `backend/src/msai/api/backtests.py:410–454`                                                                   | Fetches trades from DB but `BacktestResultsResponse` Pydantic schema doesn't include `trades` field. No timeseries fields at all.                                                                       |
| `BacktestResultsResponse`     | `backend/src/msai/schemas/backtest.py`                                                                        | `id, metrics, trade_count, trades: list[dict]=[]` — `trades` listed but missing from TS client type too.                                                                                                |
| QuantStats integration        | `backend/src/msai/workers/backtest_job.py:311–322` + `services/report_generator.py:79–137`                    | Produces HTML only. Source DataFrame (`result.account_df`) is in subprocess memory at generation time but NOT persisted anywhere after worker exits.                                                    |
| `account_df` → returns series | `services/analytics_math.py:36–55` (`build_series_from_returns`) + `:182–195` (`dataframe_to_series_payload`) | Already exists. Takes a returns Series → computes equity curve + drawdown series. Reusable as-is.                                                                                                       |
| Trade persistence             | `workers/backtest_job.py:485–495` + `models/trade.py`                                                         | Individual fills written to `trades` table. Columns: `instrument, side, quantity, price, pnl, commission, executed_at`. **No entry/exit pairing**. One fill = one row.                                  |
| Nautilus source               | `backtest_runner.py:106–118`                                                                                  | Result has `orders_df, positions_df, account_df` DataFrames — post-run pickled back from subprocess. Only `account_df.returns` makes it into QuantStats; all three are lost after `_finalize_backtest`. |

### Frontend

| Component                 | File                                                   | State                                                                                                                             |
| ------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Detail page               | `frontend/src/app/backtests/[id]/page.tsx:207,309`     | `const equityCurve = [];` hardcoded. `<TradeLog trades={[]} />` hardcoded.                                                        |
| `<ResultsCharts>`         | `frontend/src/components/backtests/results-charts.tsx` | Accepts `equityCurve: EquityPoint[]`. MonthlyReturnsHeatmap is a no-data placeholder.                                             |
| `<TradeLog>`              | `frontend/src/components/backtests/trade-log.tsx`      | Table with columns: Timestamp, Instrument, Side, Qty, **Entry, Exit**, P&L, Duration. **Shape mismatch with what backend sends.** |
| `BacktestMetrics` TS type | `frontend/src/lib/api.ts:199–214`                      | `trades` field missing from `BacktestResultsResponse`. No `BacktestTradeItem` type exists.                                        |

## Discussion Log

### Round 1 questions (asked 2026-04-21)

10 questions covering: trade-pairing semantics, timeseries derivation source, endpoint shape, payload size/downsampling, benchmark overlay, MVP scope trimming, persistence strategy (recompute-vs-store), trade log columns, schema backward-compat, and equity-curve x-axis behavior.

### Pablo's response (2026-04-21)

Instead of answering the 10 questions individually, Pablo ratified the broader intent via council: "ultimately this is what i need: I want to be able to see, for every backtest that ends, the whole tear sheet with all the risk metrics and all the charts. For example, Quantopian gives you this huge report, Pyfolio, which is my favorite..."

### Round 2 — `/council` verdict (2026-04-21)

Dispatched 5 advisors + Codex chairman. **Verdict: `A.3 + B.1 + C.2` with must-do constraints from all advisors.** Full decision record ratified at `docs/decisions/backtest-results-charts-and-trades.md`.

Advisor tally:

- Simplifier: APPROVE A.1+B.1+C.2
- Scalability Hawk: CONDITIONAL A.3+B.1+C.2 (daily-compound, trade pagination, payload observability)
- Pragmatist: APPROVE A.3+B.1+C.2
- Contrarian: **OBJECT** — iframe auth flaw + pre-digested JSONB drift
- Maintainer: CONDITIONAL — atomic write, `series` naming, single normalization path, explicit availability flag

**Contrarian objections upheld** (iframe proxy + canonical-series persistence are mandatory); **A.2 overruled** (deferred) because Pablo wants to see the tear sheet now.

### Chairman follow-up Q&A (2026-04-21)

**Q: Iframe auth proxy strategy — (a) Next.js server-side route handler, (b) session-cookie auth on backend `/report`, (c) short-lived signed URL?**
A: **(a) Next.js route handler.** Lowest surface-area change, keeps backend auth untouched.

**Q: Will you share backtest detail pages with outside viewers (investors, collaborators, demo)?**
A: Yes, but **HTML downloaded file is fine for v1** — no live-link sharing. So the iframe stays Pablo-only (authenticated), and the desktop-style QS HTML UX is acceptable because outside viewers get the downloaded file, not the React UI.

## Refined Understanding

### Personas

- **Pablo (solo operator)** — runs backtests interactively via UI + overnight batches; wants the full Pyfolio-equivalent tear sheet visible per-backtest without leaving the app.
- **Outside viewer (future, v1 via downloaded file only)** — investor/collaborator/demo audience who gets the QuantStats HTML file sent to them. Doesn't access the React UI in v1.

### User Stories (Refined)

- **US-001 (happy path — native views):** As Pablo, when a backtest completes, I open its detail page and see a populated equity curve, drawdown chart, monthly returns heatmap, and paginated trade log — no empty components.
- **US-002 (full report in-app):** As Pablo, I click "Open Full Report" on the detail page and see the QuantStats tear sheet rendered inside an authenticated iframe (same-origin, via Next.js proxy route). All 60+ stats + 20+ charts are visible without leaving the app.
- **US-003 (download):** As Pablo, I click "Download Report" (existing) and get the QuantStats HTML file to send to an outside viewer or archive.
- **US-004 (trade audit):** As Pablo, I scroll through the trade log and page forward/back (100 rows per page) to audit individual fills. Each row shows timestamp, instrument, side, qty, price, P&L, commission.
- **US-005 (legacy backtests):** As Pablo, when I open a backtest that was created before this feature shipped, the UI shows charts-as-empty with a "data not materialized for historical backtests" message, NOT a broken page. (`series_status = "not_materialized"`.)
- **US-006 (compute-failed backtests):** As Pablo, when the worker successfully completed the backtest but analytics materialization failed, the UI shows the 6 aggregate metrics but flags charts as "unavailable: compute error" with an explicit message. (`series_status = "failed"`.)

### Non-Goals (explicit exclusions)

- Full native React Pyfolio port (C.3) — future PR if iframe UX becomes unacceptable.
- Benchmark overlay (SPY/QQQ comparison) — future PR.
- Entry/exit round-trip pairing in trade log — render individual fills in v1.
- CSV/Excel export of trades — future PR.
- Public-shareable link / signed URL / anonymous backtest view — future PR with its own auth story.
- Rolling metrics (3m/6m/12m Sharpe etc.) beyond what the `series` payload enables — future PR (native `series` persistence enables these cheaply).
- Historical backfill of `series` for pre-PR backtests — left as `series_status = "not_materialized"`.

### Key Decisions (from council)

1. **ONE JSONB column** `Backtest.series` (matches `PortfolioRun.series` precedent) holding canonical daily-normalized returns/equity timeseries. Views derive from this. NOT three separate columns.
2. **Single returns-normalization code path** — dedupe `analytics_math.build_series_from_returns` vs `report_generator._normalize_report_returns`. The canonical normalization feeds BOTH QuantStats HTML AND the persisted `series`.
3. **Atomic worker write** in `_finalize_backtest()` alongside `metrics`, `report_path`, and terminal status. No second-phase writes.
4. **Explicit `series_status: Literal["ready", "not_materialized", "failed"]`** so API callers disambiguate old rows from compute failures.
5. **Paginated `GET /api/v1/backtests/{id}/trades?page=N&page_size=100`** sibling endpoint. `/results` stops returning trades inline.
6. **Next.js server-side proxy route** `/api/backtests/[id]/report` (frontend) forwards auth-header to backend's `/report` and streams HTML to the iframe.
7. **Payload-size observability:** `msai_backtest_results_payload_bytes` histogram + WARN log when `series` JSONB >1 MB.
8. **Parity contract** (documented in decision doc): `metrics` wins for aggregates; `series` wins for chart timeseries; `trades` table wins for fills; QS HTML is viewer-only snapshot.

### Open Questions (remaining — addressed in Phase 2 research or Phase 4 TDD)

- [ ] QuantStats HTML structure stability across minor versions (2–3-version sanity test)
- [ ] Does 3–5 MB QuantStats HTML iframe render cleanly without memory/UX issues? (~30-min spike)
- [ ] Wall-clock + payload-size cost of `series` build on 5-year minute-bar `account_df` (benchmark during TDD)

**Status:** READY FOR `/prd:create`
