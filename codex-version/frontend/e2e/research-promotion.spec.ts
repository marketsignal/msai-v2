import { expect, test } from "@playwright/test";

test.describe("research promotion workflow", () => {
  test("promotes a saved research result into the live deployment form", async ({ page }) => {
    const seenHeaders: string[] = [];

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const headers = request.headers();
      seenHeaders.push(headers["x-api-key"] ?? "");
      expect(headers["x-api-key"]).toBe("msai-dev-key");
      expect(headers.authorization).toBeUndefined();

      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "GET" && path === "/api/v1/research/reports") {
        await route.fulfill({
          json: [
            {
              id: "mean-reversion-sweep",
              mode: "parameter_sweep",
              generated_at: "2026-04-07T15:00:00Z",
              strategy_name: "example.mean_reversion",
              instruments: ["SPY.EQUS"],
              objective: "sharpe",
              summary: { total_runs: 2, successful_runs: 2 },
              best_config: { lookback: 20, zscore_threshold: 1.5 },
              best_metrics: { sharpe: 1.8, total_return: 0.12 },
              candidate_count: 2,
            },
          ],
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/reports/mean-reversion-sweep") {
        await route.fulfill({
          json: {
            summary: {
              id: "mean-reversion-sweep",
              mode: "parameter_sweep",
              generated_at: "2026-04-07T15:00:00Z",
              strategy_name: "example.mean_reversion",
              instruments: ["SPY.EQUS"],
              objective: "sharpe",
              summary: { total_runs: 2, successful_runs: 2 },
              best_config: { lookback: 20, zscore_threshold: 1.5 },
              best_metrics: { sharpe: 1.8, total_return: 0.12 },
              candidate_count: 2,
            },
            report: {
              mode: "parameter_sweep",
              generated_at: "2026-04-07T15:00:00Z",
              objective: "sharpe",
              instruments: ["SPY.EQUS"],
              results: [
                {
                  config: { lookback: 20, zscore_threshold: 1.5 },
                  metrics: { sharpe: 1.8, total_return: 0.12, win_rate: 0.58 },
                  error: null,
                },
                {
                  config: { lookback: 10, zscore_threshold: 2.0 },
                  metrics: { sharpe: 1.2, total_return: 0.08, win_rate: 0.52 },
                  error: null,
                },
              ],
            },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/research/promotions") {
        await route.fulfill({
          json: {
            id: "promotion-1",
            report_id: "mean-reversion-sweep",
            created_at: "2026-04-07T15:05:00Z",
            created_by: "user-1",
            paper_trading: true,
            strategy_id: "strategy-mean",
            strategy_name: "example.mean_reversion",
            instruments: ["SPY.EQUS"],
            config: { lookback: 20, zscore_threshold: 1.5 },
            selection: {
              kind: "parameter_sweep",
              result_index: 0,
              window_index: null,
            },
            live_url: "/live?promotion_id=promotion-1",
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/graduation/candidates") {
        await route.fulfill({
          json: {
            id: "candidate-1",
            promotion_id: "promotion-1",
            report_id: "mean-reversion-sweep",
            created_at: "2026-04-07T15:06:00Z",
            updated_at: "2026-04-07T15:06:00Z",
            stage: "paper_candidate",
            strategy_id: "strategy-mean",
            strategy_name: "example.mean_reversion",
            strategy_path: "/repo/strategies/example/mean_reversion.py",
            instruments: ["SPY.EQUS"],
            config: { lookback: 20, zscore_threshold: 1.5 },
            selection: { kind: "parameter_sweep", result_index: 0, metrics: { sharpe: 1.8 } },
            paper_trading: true,
            live_url: "/live?candidate_id=candidate-1",
            portfolio_url: "/portfolio?candidate_id=candidate-1",
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/promotions/promotion-1") {
        await route.fulfill({
          json: {
            id: "promotion-1",
            report_id: "mean-reversion-sweep",
            created_at: "2026-04-07T15:05:00Z",
            created_by: "user-1",
            paper_trading: true,
            strategy_id: "strategy-mean",
            strategy_name: "example.mean_reversion",
            instruments: ["SPY.EQUS"],
            config: { lookback: 20, zscore_threshold: 1.5 },
            selection: {
              kind: "parameter_sweep",
              result_index: 0,
              window_index: null,
            },
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/status") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/positions") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/orders") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/trades") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/risk-status") {
        await route.fulfill({ json: { halted: false, current_pnl: 0, notional_exposure: 0 } });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategies/") {
        await route.fulfill({
          json: [{ id: "strategy-mean", name: "example.mean_reversion" }],
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategies/strategy-mean") {
        await route.fulfill({
          json: {
            id: "strategy-mean",
            name: "example.mean_reversion",
            default_config: { lookback: 5, zscore_threshold: 2.5 },
          },
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/research");

    await expect(page.getByRole("heading", { name: "Research Console" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Promote Top Config" })).toBeVisible();

    await page.getByRole("button", { name: "Promote Top Config" }).click();

    await expect(page.getByText("Graduation candidate created")).toBeVisible();
    await page.getByRole("link", { name: "Open in Live Trading" }).click();

    await expect(page).toHaveURL(/promotion_id=promotion-1/);
    await expect(page.getByText("Paper promotion loaded")).toBeVisible();
    await expect(page.getByLabel("Instruments")).toHaveValue("SPY.EQUS");
    await expect(page.getByLabel("Config JSON")).toContainText('"lookback": 20');
    await expect(page.getByLabel("Config JSON")).toContainText('"zscore_threshold": 1.5');
    expect(seenHeaders.length).toBeGreaterThan(0);
  });
});
