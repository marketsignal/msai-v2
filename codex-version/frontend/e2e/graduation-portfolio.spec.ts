import { expect, test } from "@playwright/test";

type PortfolioDefinition = {
  id: string;
  name: string;
  description: string;
  objective: string;
  base_capital: number;
  requested_leverage: number;
  downside_target: number | null;
  benchmark_symbol: string;
  allocations: Array<{
    candidate_id: string;
    strategy_name: string;
    instruments: string[];
    weight: number;
  }>;
};

type PortfolioRun = {
  id: string;
  portfolio_id: string;
  portfolio_name: string;
  status: string;
  start_date: string;
  end_date: string;
  max_parallelism: number;
  metrics: Record<string, number>;
  series: Array<{ timestamp: string; equity: number; drawdown: number; returns: number }>;
  allocations: Array<{
    candidate_id: string;
    strategy_name: string;
    instruments: string[];
    weight: number;
    metrics: Record<string, number>;
  }>;
  report_path: string;
};

test.describe("graduation and portfolio workflow", () => {
  test("moves a candidate through graduation and queues a portfolio run", async ({ page }) => {
    let candidate = {
      id: "candidate-1",
      promotion_id: "promotion-1",
      report_id: "report-1",
      created_at: "2026-04-07T18:00:00Z",
      updated_at: "2026-04-07T18:00:00Z",
      stage: "paper_candidate",
      notes: "Initial promotion",
      strategy_id: "strategy-mean",
      strategy_name: "example.mean_reversion",
      strategy_path: "/repo/strategies/example/mean_reversion.py",
      instruments: ["SPY.EQUS"],
      config: { lookback: 20, zscore_threshold: 1.5 },
      selection: {
        kind: "parameter_sweep",
        metrics: { sharpe: 1.8, sortino: 2.2, total_return: 0.12, win_rate: 0.58 },
      },
      paper_trading: true,
      live_url: "/live?candidate_id=candidate-1",
      portfolio_url: "/portfolio?candidate_id=candidate-1",
    };

    let portfolios: PortfolioDefinition[] = [];
    let runs: PortfolioRun[] = [];

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const headers = request.headers();
      expect(headers["x-api-key"]).toBe("msai-dev-key");

      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "GET" && path === "/api/v1/graduation/candidates") {
        await route.fulfill({ json: [candidate] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/graduation/candidates/candidate-1") {
        await route.fulfill({ json: candidate });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/graduation/candidates/candidate-1/stage") {
        const payload = request.postDataJSON() as { stage: string; notes?: string | null };
        candidate = {
          ...candidate,
          stage: payload.stage,
          notes: payload.notes ?? null,
          updated_at: "2026-04-07T18:05:00Z",
        };
        await route.fulfill({ json: candidate });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/portfolios") {
        await route.fulfill({ json: portfolios });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/portfolios") {
        portfolios = [
          {
            id: "portfolio-1",
            name: "Core Portfolio",
            description: "Blended sleeve of graduated strategies",
            objective: "maximize_sharpe",
            base_capital: 1000000,
            requested_leverage: 1,
            downside_target: null,
            benchmark_symbol: "SPY.EQUS",
            allocations: [
              {
                candidate_id: "candidate-1",
                strategy_name: "example.mean_reversion",
                instruments: ["SPY.EQUS"],
                weight: 1.0,
              },
            ],
          },
        ];
        await route.fulfill({ json: portfolios[0] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/portfolios/runs") {
        await route.fulfill({ json: runs });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/portfolios/portfolio-1/runs") {
        runs = [
          {
            id: "run-1",
            portfolio_id: "portfolio-1",
            portfolio_name: "Core Portfolio",
            status: "completed",
            start_date: "2026-03-31",
            end_date: "2026-04-03",
            max_parallelism: 2,
            metrics: {
              sharpe: 1.7,
              sortino: 2.4,
              alpha: 0.11,
              beta: 0.82,
              total_return: 0.14,
              max_drawdown: 0.05,
              effective_leverage: 1.0,
            },
            series: [
              { timestamp: "2026-03-31T14:30:00Z", equity: 1000000, drawdown: 0, returns: 0 },
              { timestamp: "2026-04-01T14:30:00Z", equity: 1012000, drawdown: -0.01, returns: 0.012 },
              { timestamp: "2026-04-02T14:30:00Z", equity: 1025000, drawdown: -0.008, returns: 0.013 },
            ],
            allocations: [
              {
                candidate_id: "candidate-1",
                strategy_name: "example.mean_reversion",
                instruments: ["SPY.EQUS"],
                weight: 1.0,
                metrics: { sharpe: 1.8, total_return: 0.12 },
              },
            ],
            report_path: "/tmp/report.html",
          },
        ];
        await route.fulfill({ json: runs[0] });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/graduation?candidate_id=candidate-1");

    await expect(page.getByRole("heading", { name: /Turn research winners into paper and live sleeves/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /example\.mean_reversion/i }).first()).toBeVisible();

    await page.getByLabel("Stage").selectOption("paper_review");
    await page.getByRole("button", { name: "Apply Stage Update" }).click();
    await expect(page.getByText("Candidate moved to Paper Review.")).toBeVisible();

    await page.getByRole("link", { name: "Open Portfolio Lab" }).click();
    await expect(page).toHaveURL(/candidate_id=candidate-1/);
    await expect(page.getByRole("heading", { name: /Model portfolio allocation/i })).toBeVisible();

    await page.getByRole("button", { name: "Create Portfolio" }).click();
    await expect(page.getByText("Portfolio Core Portfolio created.")).toBeVisible();

    await page.getByRole("button", { name: "Run Portfolio" }).click();
    await expect(page.getByText("Portfolio run run-1 queued.")).toBeVisible();
    await expect(page.getByText("Selected Run Detail")).toBeVisible();
    await expect(page.getByText("1.70").nth(1)).toBeVisible();
    await expect(page.getByRole("link", { name: "Open HTML Report" })).toBeVisible();
  });
});
