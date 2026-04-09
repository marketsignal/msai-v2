import { expect, test } from "@playwright/test";

type Deployment = {
  id: string;
  strategy: string;
  status: string;
  started_at?: string;
  daily_pnl?: number;
};

type Position = {
  instrument: string;
  quantity: number;
  avg_price: number;
  current_price?: number;
  unrealized_pnl: number;
  market_value: number;
};

test.describe("live control surface", () => {
  test("uses API key auth and drives the live deployment controls", async ({ page }) => {
    let deployments: Deployment[] = [
      {
        id: "dep-running",
        strategy: "example.ema_cross",
        status: "running",
        started_at: "2026-04-07T12:00:00Z",
        daily_pnl: 42.5,
      },
    ];
    let positions: Position[] = [
      {
        instrument: "AAPL.XNAS",
        quantity: 10,
        avg_price: 210.5,
        current_price: 212.1,
        unrealized_pnl: 16,
        market_value: 2121,
      },
    ];
    let lastStartPayload: Record<string, unknown> | null = null;
    const seenHeaders: string[] = [];

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const headers = request.headers();
      seenHeaders.push(headers["x-api-key"] ?? "");
      expect(headers["x-api-key"]).toBe("msai-dev-key");
      expect(headers.authorization).toBeUndefined();

      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "GET" && path === "/api/v1/live/status") {
        await route.fulfill({ json: deployments });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/live/positions") {
        await route.fulfill({ json: positions });
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
        await route.fulfill({
          json: {
            halted: false,
            current_pnl: 42.5,
            notional_exposure: 2121,
            portfolio_value: 1250000,
            margin_used: 150000,
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategies/") {
        await route.fulfill({
          json: [{ id: "strategy-ema", name: "example.ema_cross" }],
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategies/strategy-ema") {
        await route.fulfill({
          json: {
            id: "strategy-ema",
            name: "example.ema_cross",
            default_config: {
              fast_ema_period: 10,
              slow_ema_period: 30,
              trade_size: "1",
            },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/live/start") {
        lastStartPayload = request.postDataJSON() as Record<string, unknown>;
        deployments = [
          {
            id: "dep-new",
            strategy: "example.ema_cross",
            status: "starting",
            started_at: "2026-04-07T12:05:00Z",
            daily_pnl: 0,
          },
        ];
        positions = [];
        await route.fulfill({ json: { deployment_id: "dep-new" } });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/live/stop") {
        deployments = [];
        positions = [];
        await route.fulfill({ json: { status: "stopped" } });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/live/kill-all") {
        deployments = [];
        positions = [];
        await route.fulfill({ json: { stopped: 1 } });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    page.on("dialog", (dialog) => dialog.accept());

    await page.goto("/live");

    await expect(page.getByText("API key mode")).toBeVisible();
    await expect(page.getByText("Stream status: disabled for tests")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Active Strategies" })).toBeVisible();
    await expect(page.getByText("dep-running")).toBeVisible();
    await expect(page.getByText("AAPL.XNAS")).toBeVisible();

    await page.getByLabel("Instruments").fill("MSFT.XNAS,NVDA.XNAS");
    await page.getByRole("button", { name: "Start" }).click();

    await expect(page.getByText("dep-new")).toBeVisible();
    expect(lastStartPayload).toEqual({
      strategy_id: "strategy-ema",
      config: {
        fast_ema_period: 10,
        slow_ema_period: 30,
        trade_size: "1",
      },
      instruments: ["MSFT.XNAS", "NVDA.XNAS"],
      paper_trading: true,
    });

    await page.getByRole("button", { name: "Stop", exact: true }).click();
    await expect(page.getByText("dep-new")).not.toBeVisible();

    await page.getByRole("button", { name: "STOP ALL" }).click();
    expect(seenHeaders.length).toBeGreaterThan(0);
  });
});
