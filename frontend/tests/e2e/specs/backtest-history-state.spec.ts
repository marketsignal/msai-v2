/**
 * Isolated history-state regressions using controlled API responses.
 * These exercise rendered failure/recovery and response ordering; they do not
 * certify the real research journey or explain a transport failure.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import type { BacktestHistoryItem } from "../../../src/lib/api";

const single: BacktestHistoryItem = {
  id: "single-reference",
  type: "single",
  strategy_id: "ema-reference",
  status: "completed",
  start_date: "2025-01-22",
  end_date: "2025-01-24",
  created_at: "2026-10-03T12:00:00Z",
};
const portfolio: BacktestHistoryItem = {
  ...single,
  id: "portfolio-reference",
  type: "portfolio",
  strategy_id: null,
  portfolio_id: "portfolio-one",
  portfolio_name: "Reference portfolio",
};

async function history(route: Route, items: BacktestHistoryItem[]) {
  await route.fulfill({ json: { items, total: items.length } });
}

async function unavailable(route: Route) {
  await route.fulfill({ status: 503, json: { detail: "History unavailable" } });
}

async function settleResponse(page: Page, route: Route, complete: () => Promise<void>) {
  const response = page.waitForResponse(route.request().url());
  await complete();
  await (await response).finished();
  // Allow the completed fetch's React update to render before asserting that
  // a stale response did not alter the current view. No arbitrary time sleep.
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/strategies/**", async (route) => {
    await route.fulfill({ json: { items: [], total: 0 } });
  });
});

test("initial failure is unavailable, and Retry can reveal a genuinely empty history", async ({
  page,
}) => {
  let fails = true;
  await page.route("**/api/v1/backtests/history?**", async (route) => {
    if (fails) await unavailable(route);
    else await history(route, []);
  });

  await page.goto("/backtests");
  await expect(page.getByText(/Failed to load backtests/)).toBeVisible();
  await expect(page.getByText(/No backtests yet/)).toHaveCount(0);

  fails = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByText(/No backtests yet/)).toBeVisible();
  await expect(page.getByText(/Failed to load backtests/)).toHaveCount(0);
});

test("failed filter change hides old rows, and Retry retains the selected filter", async ({
  page,
}) => {
  let fails = true;
  const requestedTypes: string[] = [];
  await page.route("**/api/v1/backtests/history?**", async (route) => {
    const type = new URL(route.request().url()).searchParams.get("type") ?? "";
    requestedTypes.push(type);
    if (type === "single") {
      if (fails) await unavailable(route);
      else await history(route, [single]);
    } else {
      await history(route, [portfolio]);
    }
  });

  await page.goto("/backtests");
  await expect(page.getByTestId(`backtest-row-${portfolio.id}`)).toBeVisible();
  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-single").click();
  await expect(page.getByText(/Failed to load backtests/)).toBeVisible();
  await expect(page.getByTestId(`backtest-row-${portfolio.id}`)).toHaveCount(0);
  await expect(page.getByText(/No single backtests yet/)).toHaveCount(0);

  fails = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByTestId(`backtest-row-${single.id}`)).toBeVisible();
  await expect(page.getByTestId(`backtest-row-${portfolio.id}`)).toHaveCount(0);
  expect(requestedTypes.slice(-2)).toEqual(["single", "single"]);
});

test("old success cannot end a newer load and old failure cannot replace newer results", async ({
  page,
}) => {
  let phase: "initial" | "pending" | "newer-success" = "initial";
  let receiveSingle!: (route: Route) => void;
  let receivePortfolio!: (route: Route) => void;
  let receiveAll!: (route: Route) => void;
  const singleRequested = new Promise<Route>((resolve) => { receiveSingle = resolve; });
  const portfolioRequested = new Promise<Route>((resolve) => { receivePortfolio = resolve; });
  const allRequested = new Promise<Route>((resolve) => { receiveAll = resolve; });
  await page.route("**/api/v1/backtests/history?**", async (route) => {
    const type = new URL(route.request().url()).searchParams.get("type");
    if (phase === "initial") await history(route, [portfolio]);
    else if (type === "portfolio") receivePortfolio(route);
    else if (type === "all") receiveAll(route);
    else if (phase === "newer-success") await history(route, [single]);
    else receiveSingle(route);
  });

  await page.goto("/backtests");
  await expect(page.getByTestId(`backtest-row-${portfolio.id}`)).toBeVisible();
  phase = "pending";
  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-single").click();
  const oldSingle = await singleRequested;
  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-portfolio").click();
  const newerPortfolio = await portfolioRequested;

  await settleResponse(page, oldSingle, () => history(oldSingle, [single]));
  await expect(page.getByText("Loading backtests...")).toBeVisible();
  await expect(page.getByTestId(`backtest-row-${single.id}`)).toHaveCount(0);
  await history(newerPortfolio, [portfolio]);
  await expect(page.getByTestId(`backtest-row-${portfolio.id}`)).toBeVisible();

  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-all").click();
  const oldAll = await allRequested;
  phase = "newer-success";
  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-single").click();
  await expect(page.getByTestId(`backtest-row-${single.id}`)).toBeVisible();
  await settleResponse(page, oldAll, () => unavailable(oldAll));
  await expect(page.getByText(/Failed to load backtests/)).toHaveCount(0);
  await expect(page.getByTestId(`backtest-row-${single.id}`)).toBeVisible();
});

test("Refresh history updates a pending run without leaving the selected filter", async ({
  page,
}) => {
  let completed = false;
  const requestedTypes: string[] = [];
  await page.route("**/api/v1/backtests/history?**", async (route) => {
    requestedTypes.push(new URL(route.request().url()).searchParams.get("type") ?? "");
    await history(route, [{ ...single, status: completed ? "completed" : "pending" }]);
  });

  await page.goto("/backtests");
  await page.getByTestId("backtest-type-filter").click();
  await page.getByTestId("filter-option-single").click();
  const row = page.getByTestId(`backtest-row-${single.id}`);
  await expect(row).toContainText("pending");

  completed = true;
  await page.getByRole("button", { name: "Refresh history", exact: true }).click();
  await expect(row).toContainText("completed");
  await expect(page.getByTestId("backtest-type-filter")).toHaveText("Single");
  expect(requestedTypes.slice(-2)).toEqual(["single", "single"]);
});
