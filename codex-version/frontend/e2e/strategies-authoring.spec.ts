import { expect, test } from "@playwright/test";

test.describe("strategy authoring workflow", () => {
  test("creates a scaffolded strategy and syncs it into the registry", async ({ page }) => {
    const registry = [
      {
        id: "strategy-existing",
        name: "example.mean_reversion",
        description: "Existing registry baseline",
        strategy_class: "MeanReversionZScoreStrategy",
        file_path: "example/mean_reversion.py",
      },
    ];

    const templates = [
      {
        id: "mean_reversion_zscore",
        label: "Mean Reversion Z-Score",
        description: "Intraday z-score reversion baseline.",
        default_config: {
          lookback: 20,
          entry_zscore: 1.5,
          exit_zscore: 0.25,
        },
      },
      {
        id: "ema_cross",
        label: "EMA Cross",
        description: "Trend-following crossover template.",
        default_config: {
          fast_ema_period: 10,
          slow_ema_period: 30,
          trade_size: "1",
        },
      },
    ];

    let scaffoldPayload: Record<string, unknown> | null = null;
    let syncCalls = 0;

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const headers = request.headers();
      expect(headers["x-api-key"]).toBe("msai-dev-key");
      expect(headers.authorization).toBeUndefined();

      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "GET" && path === "/api/v1/strategies/") {
        await route.fulfill({ json: registry });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/strategy-templates") {
        await route.fulfill({ json: templates });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/strategy-templates/scaffold") {
        scaffoldPayload = request.postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          json: {
            strategy_id: "strategy-new",
            template_id: "mean_reversion_zscore",
            name: "user.my_new_strategy",
            description: "Scaffolded from browser",
            file_path: "user/my_new_strategy.py",
            strategy_class: "MyNewStrategyStrategy",
            config_schema: { title: "MyNewStrategyConfig" },
            default_config: { lookback: 20, entry_zscore: 1.5, exit_zscore: 0.25 },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/strategies/sync") {
        syncCalls += 1;
        await route.fulfill({
          json: [
            ...registry,
            {
              id: "strategy-new",
              name: "user.my_new_strategy",
              description: "Scaffolded from browser",
              strategy_class: "MyNewStrategyStrategy",
              file_path: "user/my_new_strategy.py",
            },
          ],
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/strategies");

    await expect(page.getByRole("heading", { name: "Strategy Registry" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Create real Nautilus strategy modules without leaving the product surface." })).toBeVisible();

    await page.getByLabel("Module Name").fill("user.my_new_strategy");
    await page.getByLabel("Description").fill("Scaffolded from browser");
    await page.getByRole("button", { name: "Create Strategy" }).click();

    await expect(page.getByText("Created user.my_new_strategy from mean_reversion_zscore and synced it into the registry.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "user.my_new_strategy" })).toBeVisible();

    expect(scaffoldPayload).toEqual({
      template_id: "mean_reversion_zscore",
      module_name: "user.my_new_strategy",
      description: "Scaffolded from browser",
      force: false,
    });
    expect(syncCalls).toBe(1);
  });
});
