# E2E Use Cases — Backtest Results Charts & Trade Log

**Feature:** Render a Pyfolio-style tear sheet for every completed backtest (equity curve, drawdown, monthly returns heatmap, paginated trade log + in-app QuantStats iframe + downloadable HTML). Preserves the existing `/api/v1/backtests/{id}/report` download flow.

**Interfaces:** API (primary) + UI (secondary).

**Prerequisites:** Dev stack at `http://localhost:8800` + `http://localhost:3300`. A completed backtest in the DB with `series_status="ready"` + non-empty `series.daily`, plus one legacy row with `series_status="not_materialized"` (migration default populates this automatically for pre-PR rows).

---

## UC-BRC-001 — Native charts populated after a fresh backtest

**Intent:** A user submits a backtest; when it completes, the detail page shows populated equity / drawdown / monthly-returns charts plus paginated fills.

**Interface:** API + UI.

**Setup (ARRANGE):** Register the EMA Cross strategy on disk (committed to `strategies/`). Discover via `GET /api/v1/strategies/`.

**Steps (API):**

1. `POST /api/v1/backtests/run` body: `{strategy_id, instruments=["SPY.XNAS"], start_date="2024-01-02", end_date="2024-01-31", config={instrument_id, bar_type, fast_ema_period: 10, slow_ema_period: 30, trade_size: "1"}}`.
2. Poll `GET /api/v1/backtests/{id}/status` until `status=completed` (auto-heal handles missing data).
3. `GET /api/v1/backtests/{id}/results` → assert `series_status="ready"`, `series.daily` is non-empty (equity > 0 on first row, `drawdown <= 0` for all), `series.monthly_returns` has at least one entry matching `^\d{4}-(0[1-9]|1[0-2])$`, `has_report=true`, `trade_count > 0`.
4. `GET /api/v1/backtests/{id}/trades?page=1&page_size=100` → `total` matches `trade_count`, items strictly ascending by `(executed_at, id)`.

**Steps (UI):** 5. Navigate to `/backtests/{id}`. Assert Sharpe / Sortino / Max DD / Total Return / Win Rate / Total Trades metric cards are populated (no `NaN%`, no `0`s across the board). 6. Assert "Native view" tab is selected by default; Equity Curve, Drawdown, Monthly Returns, and Trade Log cards are visible. 7. Click the "Full report" tab (`getByTestId("tab-full-report")`). The iframe src must contain `/api/v1/backtests/{id}/report?token=` (signed URL). Iframe body contains "QuantStats" and "Performance" text. 8. Reload the page. Tab state resets to "Native view" (default); native charts + Trade Log re-hydrate.

**Verification:** All assertions pass. Screenshot at `.playwright-mcp/uc-001-full-report-tab.png` (in the verify-e2e report) shows QuantStats tearsheet rendered inside the iframe.

**Classification on failure:** FAIL_BUG if charts empty on a completed backtest. FAIL_BUG if iframe shows "download" prompt instead of rendering — `Content-Disposition` must be `inline`.

---

## UC-BRC-002 — Legacy backtest renders gracefully

**Intent:** A backtest completed before this feature shipped (series_status="not_materialized", series=null) loads without a 500, shows aggregate metrics, and the empty-state panel explains that charts aren't available.

**Interface:** API + UI.

**Setup (ARRANGE):** Any legacy `Backtest` row — the migration default populates pre-PR rows with `series_status="not_materialized"` and `series=null` automatically.

**Steps (API):**

1. `GET /api/v1/backtests/{legacy_id}/results` → `series=null`, `series_status="not_materialized"`, aggregate metrics present, `trade_count > 0`, `has_report` reflects whether the on-disk report file still exists (may be `false` after housekeeping/DR).

**Steps (UI):** 2. Navigate to `/backtests/{legacy_id}`. Assert all 6 metric cards populated (pre-materialization aggregates). 3. Assert Equity Curve / Drawdown / Monthly Returns cards each show the `<SeriesStatusIndicator>` empty-state panel — `getByTestId("series-status-not-materialized")` visible with text "Analytics unavailable for this backtest." + "Re-run the backtest to populate charts.". 4. If `has_report=false`, the "Full report" tab is visually disabled. 5. If `has_report=true`, clicking "Full report" loads the iframe successfully.

**Verification:** Page is stable (no 500, no crash), the user sees the empty-state instead of silently blank chart cards.

**Classification on failure:** FAIL_BUG if the detail page 500s. FAIL_BUG if chart cards are blank (no indicator).

---

## UC-BRC-003 — Compute-failed backtest disambiguates from not-materialized

**Intent:** A backtest whose **analytics** failed (but run itself completed) shows a distinct failure state so the user knows the backtest worked but charts can't be computed.

**Interface:** API + UI.

**Setup (ARRANGE):** Requires a row with `series_status="failed"`. This state requires the worker to reach `_materialize_series_payload` and throw on `build_series_payload`. Not reachable through sanctioned public-API inputs — no way to cause a pathological `account_df` from the outside.

**Steps (API):** N/A — state unreachable.

**Fallback:** Backend unit tests cover the contract:

- `tests/unit/test_backtest_job.py::test_materialize_series_payload_failure_returns_failed` pins the worker's fail-soft behavior (returns `(None, "failed")` + logs WARNING with `nautilus_version`).
- `tests/unit/test_backtest_schemas.py::test_results_response_rejects_non_ready_with_series` pins the invariant that `series_status="failed"` must not carry a payload.
- `tests/unit/test_backtest_schemas.py::test_series_daily_point_rejects_negative_equity` pins the shape guard.

**Classification:** SKIPPED_INFRA. Unit coverage is sufficient for the distinct failure-state contract.

---

## UC-BRC-004 — Paginated trade log

**Intent:** The trade log is paginated — users can navigate large fill sets without blocking on a single huge payload.

**Interface:** API + UI.

**Setup (ARRANGE):** Any completed backtest with `trade_count > 100`.

**Steps (API):**

1. `GET /api/v1/backtests/{id}/trades?page=1&page_size=100` → `total = trade_count`, `page=1`, 100 items.
2. `GET /api/v1/backtests/{id}/trades?page=2&page_size=100` → subsequent items (first item's `executed_at` strictly after page-1 last). No overlap.
3. `GET /api/v1/backtests/{id}/trades?page=99&page_size=100` → 200 OK with `items=[]` (no 404 on over-page).
4. `GET /api/v1/backtests/{id}/trades?page_size=1000` → server clamps to `page_size=500`.
5. `GET /api/v1/backtests/{id}/trades?page=0` → 422 (Query `ge=1`).
6. `GET /api/v1/backtests/{id}/trades?page_size=0` → 422.

**Steps (UI):** 7. On `/backtests/{id}` Native view, TradeLog card shows "{N} fills · Page 1 of {⌈N/100⌉}". Previous button disabled on page 1. 8. Click Next. Page counter advances to "Page 2 of M". Per-row columns render (Timestamp / Instrument / Side badge / Quantity / Price / P&L / Commission).

**Verification:** Server-side sort deterministic on `(executed_at, id) ASC`. No duplicates across page boundaries.

**Classification on failure:** FAIL_BUG if duplicates or missing rows across pages (secondary-sort regression).

---

## UC-BRC-005 — Signed-URL iframe auth boundary

**Intent:** The iframe renders an authenticated QuantStats report without leaking long-lived credentials. The signed URL expires quickly and cannot be replayed.

**Interface:** API.

**Steps:**

1. `GET /api/v1/backtests/{id}/report` with no auth, no token → 401 `UNAUTHENTICATED`.
2. `GET /report?token=not-a-real-token` → 401 `INVALID_TOKEN` ("malformed token").
3. `POST /api/v1/backtests/{id}/report-token` with valid `X-API-Key` → `{signed_url: "/api/v1/backtests/{id}/report?token=...", expires_at}`. `expires_at ≈ now + settings.report_token_ttl_seconds` (default 60s).
4. `GET <signed_url>` with NO auth header → 200 OK, `Content-Type: text/html`, `Content-Disposition: inline; filename="backtest_{id}_report.html"`, body is a valid QS HTML file (contains "QuantStats" + "Performance").
5. Wait more than TTL (e.g. 70s), retry Step 4 → 401 `INVALID_TOKEN` ("token expired").
6. Mint a token for backtest A, try to use it against backtest B → 401 `INVALID_TOKEN` ("backtest_id mismatch").
7. With a valid session (`X-API-Key` present) AND a token minted for a different user → 403 `TOKEN_SUB_MISMATCH` (cross-user replay guard).

**Verification:** No code path serves the report with zero authentication. All error shapes use the structured `{"error":{"code","message"}}` envelope (no FastAPI `detail` wrapping).

**Classification on failure:** FAIL_BUG if Step 1 returns 200 (auth bypass regression — this is the iter-9 plan-review guard).

---

## UC-BRC-006 — Full-report iframe rendering + download

**Intent:** The iframe must render the QuantStats tearsheet inline (not download). The separate "Download Report" button still downloads.

**Interface:** UI.

**Steps (UI):**

1. Navigate to `/backtests/{id}` where `has_report=true`.
2. Click "Full report" tab. The iframe loads and displays the QuantStats HTML inline (headings, charts, tables all visible inside the iframe).
3. The page's "Download Report" button (top-right of header) downloads the same HTML as a file.

**Verification:** Per UC-005 Step 4, `Content-Disposition: inline` on the `/report` endpoint — iframe renders, browser does not auto-download. The "Download Report" button triggers a separate fetch with a different disposition intent (attachment is the browser-default for anchor downloads).

**Classification on failure:** FAIL_BUG if the iframe triggers a download instead of rendering (`content_disposition_type` regression — iter-5 Codex-caught bug).
