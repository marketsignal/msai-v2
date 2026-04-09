import { expect, test } from "@playwright/test";

test.describe("research console", () => {
  test("compares reports and creates a paper deployment draft", async ({ page }) => {
    const reports = [
      {
        id: "mean-reversion-sweep",
        mode: "parameter_sweep",
        strategy_name: "example.mean_reversion",
        instruments: ["AAPL.EQUS"],
        summary: { total_runs: 4, successful_runs: 4 },
        best_metrics: { sharpe: 1.82, total_return: 0.14, win_rate: 0.61 },
        candidate_count: 4,
      },
      {
        id: "breakout-wf",
        mode: "walk_forward",
        strategy_name: "example.donchian_breakout",
        instruments: ["ESM6.GLBX"],
        summary: { window_count: 3, successful_test_windows: 3 },
        best_metrics: { sharpe: 1.21, total_return: 0.09, win_rate: 0.54 },
        candidate_count: 3,
      },
    ];

    let lastPromotionPayload: Record<string, unknown> | null = null;

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;

      expect(request.headers()["x-api-key"]).toBe("msai-dev-key");

      if (request.method() === "GET" && path === "/api/v1/research/reports") {
        await route.fulfill({ json: reports });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/reports/mean-reversion-sweep") {
        await route.fulfill({
          json: {
            summary: reports[0],
            report: {
              mode: "parameter_sweep",
              instruments: ["AAPL.EQUS"],
              results: [
                { config: { lookback: 20, entry_zscore: 1.5 }, metrics: { sharpe: 1.82, total_return: 0.14, win_rate: 0.61 } },
                { config: { lookback: 10, entry_zscore: 1.0 }, metrics: { sharpe: 1.2, total_return: 0.08, win_rate: 0.52 } },
              ],
            },
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/reports/breakout-wf") {
        await route.fulfill({
          json: {
            summary: reports[1],
            report: {
              mode: "walk_forward",
              instruments: ["ESM6.GLBX"],
              windows: [
                {
                  train_start: "2026-01-01",
                  train_end: "2026-02-01",
                  test_start: "2026-02-02",
                  test_end: "2026-02-10",
                  best_train_result: { config: { channel_period: 20 } },
                  test_result: { metrics: { sharpe: 1.21 } },
                },
              ],
            },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/research/compare") {
        await route.fulfill({
          json: {
            reports: [
              {
                summary: reports[0],
                report: { mode: "parameter_sweep", instruments: ["AAPL.EQUS"], results: [] },
              },
              {
                summary: reports[1],
                report: { mode: "walk_forward", instruments: ["ESM6.GLBX"], windows: [] },
              },
            ],
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/research/promotions") {
        lastPromotionPayload = request.postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          json: {
            id: "promotion-1",
            report_id: "mean-reversion-sweep",
            strategy_id: "strategy-mean-reversion",
            strategy_name: "example.mean_reversion",
            instruments: ["AAPL.EQUS"],
            config: { lookback: 20, entry_zscore: 1.5 },
            created_at: "2026-04-07T18:00:00Z",
            live_url: "/live?promotion_id=promotion-1",
            selection: { kind: "parameter_sweep", result_index: 0 },
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
            strategy_id: "strategy-mean-reversion",
            strategy_name: "example.mean_reversion",
            strategy_path: "/repo/strategies/example/mean_reversion.py",
            instruments: ["AAPL.EQUS"],
            config: { lookback: 20, entry_zscore: 1.5 },
            created_at: "2026-04-07T18:00:30Z",
            updated_at: "2026-04-07T18:00:30Z",
            stage: "paper_candidate",
            paper_trading: true,
            selection: { kind: "parameter_sweep", result_index: 0, metrics: { sharpe: 1.82, total_return: 0.14 } },
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
            strategy_id: "strategy-mean-reversion",
            strategy_name: "example.mean_reversion",
            instruments: ["AAPL.EQUS"],
            config: { lookback: 20, entry_zscore: 1.5 },
            created_at: "2026-04-07T18:00:00Z",
            live_url: "/live?promotion_id=promotion-1",
            selection: { kind: "parameter_sweep", result_index: 0 },
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
          json: [{ id: "strategy-mean-reversion", name: "example.mean_reversion" }],
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategies/strategy-mean-reversion") {
        await route.fulfill({
          json: {
            id: "strategy-mean-reversion",
            name: "example.mean_reversion",
            default_config: { lookback: 10, entry_zscore: 1.0 },
          },
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/research");

    await expect(page.getByRole("heading", { name: "Research Console" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Promote Top Config" })).toBeVisible();

    await page.getByRole("checkbox").nth(0).check();
    await page.getByRole("checkbox").nth(1).check();
    await expect(page.getByRole("heading", { name: "Side-by-Side Comparison" })).toBeVisible();

    await page.getByRole("button", { name: "Promote Top Config" }).click();
    await expect(page.getByText("Graduation candidate created")).toBeVisible();
    expect(lastPromotionPayload).toEqual({
      report_id: "mean-reversion-sweep",
      result_index: 0,
      window_index: null,
      paper_trading: true,
    });

    await page.getByRole("link", { name: "Open in Live Trading" }).click();

    await expect(page.getByText("Paper promotion loaded")).toBeVisible();
    await expect(page.getByLabel("Instruments")).toHaveValue("AAPL.EQUS");
    await expect(page.locator("textarea")).toContainText('"lookback": 20');
  });
});
