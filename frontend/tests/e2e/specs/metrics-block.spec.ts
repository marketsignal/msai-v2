/**
 * UC: Operator opens a completed portfolio-run details page and sees the
 *     G5 risk-metrics block FIRST — Sharpe, Sortino, Alpha, Beta, and
 *     Max drawdown surface above the equity chart and any report link.
 *
 * Plan ref: `docs/plans/2026-05-26-ingest-backtest-smoke-test.md` Task 11.
 *
 * Auth: X-API-Key via playwright.config.ts extraHTTPHeaders +
 * NEXT_PUBLIC_E2E_AUTH_BYPASS=1 wired in webServer.env (see
 * `frontend/src/lib/auth.ts::isAuthBypassed`). Component is render-only —
 * no auth tokens reach the browser side of the request.
 *
 * Backend wiring lives behind `GET /api/v1/portfolios/runs/{run_id}`. To
 * keep this spec deterministic + decoupled from real worker latency we
 * intercept the GET via `page.route` and return a canonical G5-shaped
 * payload. The real wiring is covered by:
 *   - tests/integration/test_smoke_runner.py (end-to-end runner)
 *   - tests/e2e/use-cases/backtests/smoke-ui.md (verify-e2e Phase 5.4
 *     against the live stack)
 *
 * Pre-flight: stack up at http://localhost:3300 — backend not required
 * because the GET is intercepted at the browser layer.
 */
import { expect, test } from "@playwright/test";

const RUN_ID = "11111111-2222-3333-4444-555555555555";

const STUB_RUN = {
  id: RUN_ID,
  portfolio_id: "00000000-0000-0000-0000-000000000000",
  status: "completed",
  metrics: {
    // SeriesMetrics.as_dict() output
    total_return: 0.1234,
    sharpe: 1.45,
    sortino: 1.88,
    max_drawdown: -0.0567,
    alpha: 0.0312,
    beta: 0.82,
    // G5 enrichment (orchestration._enrich_smoke_metrics)
    pnl: 12340.0,
    benchmark_symbol: "SPY",
    smoke_config: "fast",
    trade_count_total: 17,
    trade_count_by_strategy: {
      "__smoke__/ema_cross/AAPL": 9,
      "__smoke__/ema_cross/SPY": 8,
    },
  },
  series: [],
  allocations: null,
  report_path: null,
  start_date: "2024-01-02",
  end_date: "2024-12-31",
  created_at: "2026-05-26T00:00:00Z",
  completed_at: "2026-05-26T00:15:00Z",
  mode: "quick",
  optimization_trace: null,
  walk_forward_payload: null,
  is_metric: null,
  oos_metric: null,
};

test.describe("Portfolio-run metrics block @smoke", () => {
  test("operator opens a completed run and sees Sharpe/Sortino/Alpha/Beta/Max-drawdown above the equity chart", async ({
    page,
  }) => {
    // Arrange: intercept the run-details GET with a G5-shaped completed run.
    await page.route(`**/api/v1/portfolios/runs/${RUN_ID}**`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(STUB_RUN),
      });
    });

    await page.goto(`/portfolio/runs/${RUN_ID}`);

    // The block itself is visible with the benchmark-labelled title.
    const block = page.getByTestId("metrics-block");
    await expect(block).toBeVisible();
    await expect(block).toContainText(/Risk metrics \(benchmark: SPY\)/);

    // Headline G5 fields surface with their formatted values.
    await expect(block).toContainText(/Sharpe/i);
    await expect(block.getByTestId("metric-sharpe")).toContainText("1.45");

    await expect(block).toContainText(/Sortino/i);
    await expect(block.getByTestId("metric-sortino")).toContainText("1.88");

    await expect(block).toContainText(/Alpha/i);
    await expect(block.getByTestId("metric-alpha")).toContainText("0.03");

    await expect(block).toContainText(/Beta/i);
    await expect(block.getByTestId("metric-beta")).toContainText("0.82");

    await expect(block).toContainText(/Max drawdown/i);
    await expect(block.getByTestId("metric-max-drawdown")).toContainText(
      "-5.67%",
    );

    // Total return + P&L + trades total are part of the same primary grid.
    await expect(block.getByTestId("metric-total-return")).toContainText(
      "12.34%",
    );
    await expect(block.getByTestId("metric-pnl")).toContainText("$12,340.00");
    await expect(block.getByTestId("metric-trades-total")).toContainText("17");

    // Per-strategy breakdown renders below the grid.
    const breakdown = block.getByTestId("metric-trade-counts");
    await expect(breakdown).toContainText("__smoke__/ema_cross/AAPL");
    await expect(breakdown).toContainText("__smoke__/ema_cross/SPY");

    // Primary-content ordering: the metrics block must appear BEFORE the
    // existing exploratory content (the "Drawdown Breakdown" card is the
    // canonical landmark that lived on the page pre-Task-11).
    const drawdownCard = page.getByTestId("drawdown-breakdown");
    await expect(drawdownCard).toBeVisible();
    const blockBox = await block.boundingBox();
    const drawdownBox = await drawdownCard.boundingBox();
    expect(blockBox).not.toBeNull();
    expect(drawdownBox).not.toBeNull();
    if (blockBox && drawdownBox) {
      expect(blockBox.y).toBeLessThan(drawdownBox.y);
    }
  });

  test("operator opens a pending run and sees the empty-state fallback @smoke", async ({
    page,
  }) => {
    // Arrange: pending run with no metrics yet — the block must still
    // render with the documented "No metrics available" fallback line so
    // the page never shows a blank header before the worker finishes.
    await page.route(`**/api/v1/portfolios/runs/${RUN_ID}**`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...STUB_RUN,
          status: "pending",
          metrics: null,
          completed_at: null,
        }),
      });
    });

    await page.goto(`/portfolio/runs/${RUN_ID}`);

    const block = page.getByTestId("metrics-block");
    await expect(block).toBeVisible();
    await expect(block).toContainText(/No metrics available for this run\./i);
  });
});
