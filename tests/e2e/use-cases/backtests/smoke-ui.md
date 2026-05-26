# E2E Use Case — "Run smoke" button on `/backtests` (UI, smoke:fast)

**Feature:** Operational workflow smoke (PRD `docs/prds/ingest-backtest-smoke-test.md` v1.3).
**Maps to:** PRD US-002.
**Interface:** UI.

---

## UC-SMK-002 — Authenticated dashboard user clicks "Run smoke" and reads metrics in the backtest details view

**Actor:** Any Entra-authenticated user on the MSAI v2 dashboard (today: Pablo; future: small team) reaching the existing `/backtests` page from the main nav.

**Scenario:** The user has just logged into the dashboard and wants to confirm the platform's end-to-end backtest pipeline is healthy on real AAPL + SPY data before they review yesterday's strategy results. They prefer the dashboard over the CLI because they're already there and want to see the structured risk metrics rendered prominently inside the existing backtest details view, not in a downloaded HTML.

**Interface:** UI

**Intent:** The user fires the canonical smoke from a single button click and watches the metrics block appear inline in the existing backtest details view, without leaving the dashboard.

**Setup:**

1. The dev stack is running (`docker compose -f docker-compose.dev.yml up -d` from the worktree path).
2. The Alembic smoke migration has run (four canonical `__smoke__/...` Strategy rows are seeded).
3. The user authenticates via the documented MSAL login flow at `http://localhost:3300` (or via the dev-mode `NEXT_PUBLIC_MSAI_API_KEY` bypass if the agent's auth fixture is configured for it — never by forging a JWT or reading secrets from disk).
4. The user is on the `/backtests` page; the existing history list renders (possibly empty on a fresh stack, possibly with prior rows).
5. (Warm path) The Parquet files for AAPL + SPY 2024-12 are already on disk so the run completes inside the 3-min p95 warm budget; cold path adds Databento ingest (≤ 10 min p95).

**Steps:**

1. The user opens `/backtests` in the browser.
2. The user clicks the button with `data-testid="run-smoke-button"` (text `Run smoke`) in the page header.
3. The user observes the new run row appear in the existing backtests history list, tagged `smoke:fast`.
4. The user waits for the row's status badge to transition live (without manual refresh) from `pending` / `running` to a terminal state (`passed` or `failed`).
5. The user clicks the row's link to open the existing backtest details view at `/portfolio/runs/<runId>`.

**Verification:**

- After step 2, the user sees a non-blocking toast (`data-testid="run-smoke-toast"`) confirming submission within 2 seconds. The toast text names the new run id.
- After step 3, the new row is visible at the top of the history list within 2 seconds, with `data-testid="backtest-row-<runId>"`, a `smoke:fast` chip (`data-testid="backtest-row-smoke-tag"`), and a non-terminal status badge (`data-testid="backtest-row-status"` reading `pending` or `running`).
- During step 4, the status badge updates live (polling, no manual reload) — the user sees the page transition `running → completed` without a page reload.
- After step 5, the backtest details page renders the structured risk-metrics block (`data-testid="metrics-block"`) prominently above the report-iframe. The block contains labeled rows for Total Return, P&L, Sharpe, Sortino, Alpha vs SPY, Beta vs SPY, Max Drawdown, and a Trade Count breakdown listing each strategy name with its count. The QuantStats HTML link is offered as a secondary `data-testid="open-full-report-link"` affordance below the metrics block — not as the primary content.
- The benchmark line in the metrics block reads `Benchmark: SPY`.
- On a failed run, the details view shows a failing-stage indicator (`data-testid="backtest-failure-stage"`) and a link to the persisted Nautilus logs (`data-testid="backtest-failure-logs-link"`) — instead of the metrics block.

**Persistence:** The user reloads `/portfolio/runs/<runId>` — the metrics block re-renders with the same values (same total return, same trade-count breakdown, same Backtest id in the URL). The user then navigates back to `/backtests`; the run row is still in the history list at its position (sorted by created_at desc) with the same `smoke:fast` chip and `completed` status. The user filters by the smoke tag family (`data-testid="backtest-filter-smoke"` chip) and confirms only smoke runs (including this one) appear. A second browser window logged in as the same user, opened fresh against `/backtests`, also shows the same run in the list with the same terminal state.

**Expected failure modes:**

- Backend unreachable on click → non-blocking error toast (`data-testid="run-smoke-error-toast"`) names the failure; no row appears in history; the button re-enables for retry.
- User clicks "Run smoke" twice rapidly → two distinct rows appear in history with distinct ids (no idempotency in v1); the second submission's pre-ingest stage waits behind the first via the Redis ingest mutex.
- User navigates away mid-run → the run continues server-side; when the user returns to `/backtests` later, the row shows the terminal state.
- User is not authenticated → the existing auth redirect fires before reaching `/backtests`; the button is never reachable.

**Notes for verify-e2e:**

- Use Playwright MCP — driven directly by the agent — for the UI navigation, click, and DOM assertions; do not defer UI verification to the operator.
- All selectors use `data-testid` per the project's E2E rule (`.claude/rules/testing.md`); never use fragile CSS class selectors.
- Use the warm-path setup unless explicitly testing cold ingest — the cold path adds Databento spend.

---
