/** Isolated layout regressions; controlled responses do not certify the real journey. */
import { expect, test, type Route } from "@playwright/test";
import type { BacktestAccounting, BacktestResultsResponse } from "../../../src/lib/api";

const accounting: BacktestAccounting = {
  version: 1, basis: "realized_account_balance", initial_capital: 1_000_000,
  currency: "USD", costs: "engine_recorded",
};
const results: BacktestResultsResponse = {
  id: "readability-reference", trade_count: 0, accounting, has_report: false,
  metrics: {
    sharpe_ratio: -5.4, sortino_ratio: -5.58, max_drawdown: -0.00000087,
    total_return: -0.00000053, win_rate: 0.36, num_trades: 0, num_fills: 0,
  },
  series_status: "ready",
  series: {
    accounting,
    daily: [
      { date: "2025-01-22", equity: 999999.96, daily_return: -0.00000004, drawdown: -0.00000004 },
      { date: "2025-01-23", equity: 1000000.34, daily_return: 0.00000038, drawdown: 0 },
      { date: "2025-01-24", equity: 999999.47, daily_return: -0.00000087, drawdown: -0.00000087 },
    ],
    monthly_returns: [{ month: "2025-01", pct: -0.00000053 }],
  },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/backtests/readability-reference/status", async (route) => {
    await route.fulfill({ json: {
      id: results.id, status: "completed", progress: 1,
      started_at: "2026-10-03T12:00:00Z", completed_at: "2026-10-03T12:01:00Z",
    } });
  });
  await page.route("**/api/v1/backtests/readability-reference/results", async (route) => {
    await route.fulfill({ json: results });
  });
  await page.route("**/api/v1/backtests/readability-reference/trades?**", async (route) => {
    await route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 100, accounting } });
  });
});

for (const width of [1024, 1280, 1440]) {
  test(`small-return metrics and axis labels fit at ${width}px`, async ({ page }) => {
    test.setTimeout(90_000); // The shared development server may compile this route first.
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/backtests/${results.id}`);

    // Losing the final digits or % makes a financially correct value misleading.
    for (const value of ["-0.0000530%", "-0.0000870%", "-5.40", "-5.58", "36.0%"]) {
      const metric = page.getByText(value, { exact: true }).and(
        page.locator('[data-slot="card-content"] > div'),
      );
      await expect(metric).toBeVisible();
      const fit = await metric.evaluate((element) => {
        const range = document.createRange();
        range.selectNodeContents(element);
        const text = range.getBoundingClientRect();
        const content = element.closest('[data-slot="card-content"]')!.getBoundingClientRect();
        return { left: text.left - content.left, right: content.right - text.right };
      });
      expect.soft(fit.left, `${value} left edge`).toBeGreaterThanOrEqual(0);
      expect.soft(fit.right, `${value} right edge`).toBeGreaterThanOrEqual(0);
    }

    for (const chartId of ["equity-curve-chart", "drawdown-chart"]) {
      const chart = page.getByTestId(chartId);
      const ticks = chart.locator("svg text").filter({ hasText: "%" });
      await expect(ticks.first()).toBeAttached();
      const clipped = await ticks.evaluateAll((elements) => elements.filter((element) => {
        const text = element.getBoundingClientRect();
        const viewport = element.closest("svg")!.getBoundingClientRect();
        return text.left < viewport.left || text.right > viewport.right;
      }).map((element) => element.textContent));
      expect.soft(clipped, `${chartId} clipped labels`).toEqual([]);
    }
  });
}

for (const versioned of [true, false]) {
  test(`${versioned ? "fill" : "legacy"} log does not invent counts while unavailable and retries the current page`, async ({ page }) => {
    test.setTimeout(90_000);
    let receiveFirst!: () => void;
    const firstRequest = new Promise<void>((resolve) => { receiveFirst = resolve; });
    const held: Route[] = [];
    const requests: string[] = [];
    let loading = true;
    let fails = true;
    const metadata = versioned ? accounting : null;
    await page.route("**/api/v1/backtests/readability-reference/results", async (route) => {
      await route.fulfill({ json: { ...results, accounting: metadata } });
    });
    await page.route("**/api/v1/backtests/readability-reference/trades?**", async (route) => {
      const pageNumber = new URL(route.request().url()).searchParams.get("page")!;
      requests.push(pageNumber);
      if (loading) {
        held.push(route);
        receiveFirst();
      } else if (fails) {
        await route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } });
      } else {
        await route.fulfill({ json: {
          accounting: metadata, total: 101, page: Number(pageNumber), page_size: 100,
          items: [{
            id: `execution-${pageNumber}`, instrument: "AAPL.XNAS", side: "BUY",
            quantity: 1, price: 200, pnl: null, commission: versioned ? 0 : null,
            executed_at: "2025-01-22T15:00:00Z",
          }],
        } });
      }
    });
    await page.goto(`/backtests/${results.id}`);
    await firstRequest;
    const log = page.getByTestId("trade-log");
    await expect(log.getByRole("status", { name: "Loading trades" })).toBeVisible();
    await expect.soft(log).not.toContainText(/\d+ (?:fills|records) · Page/);
    loading = false;
    await Promise.all(held.map((route) => route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } })));
    await expect(log.getByText(/Unable to load trades:/)).toBeVisible();
    await expect.soft(log).not.toContainText(/\d+ (?:fills|records) · Page/);
    await expect(log.getByRole("button", { name: "Retry", exact: true })).toBeVisible();

    fails = false;
    const retryStart = requests.length;
    await log.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(log.getByTestId("trade-row-execution-1")).toBeVisible();
    await expect(log).toContainText(versioned ? "101 fills · Page 1 of 2" : "101 records · Page 1 of 2");
    await expect(log.getByText(versioned ? "Fill Log" : "Legacy Order Records", { exact: true })).toBeVisible();
    await expect(log.getByRole("columnheader", { name: versioned ? "Filled quantity" : "Reported quantity" })).toBeVisible();

    fails = true;
    await log.getByRole("button", { name: "Next page" }).click();
    await expect(log.getByText(/Unable to load trades:/)).toBeVisible();
    await expect(log).not.toContainText(/\d+ (?:fills|records) · Page/);
    fails = false;
    await log.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(log.getByTestId("trade-row-execution-2")).toBeVisible();
    await expect(log).toContainText("Page 2 of 2");
    expect(requests.slice(retryStart)).toEqual(["1", "2", "2"]);
  });
}
