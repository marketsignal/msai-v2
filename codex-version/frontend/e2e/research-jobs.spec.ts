import { expect, test } from "@playwright/test";

test.describe("research job launcher", () => {
  test("queues a parameter sweep through API-key auth", async ({ page }) => {
    const seenHeaders: string[] = [];
    let jobCreated = false;

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const apiKey = request.headers()["x-api-key"];
      if (apiKey) {
        seenHeaders.push(apiKey);
      }

      if (request.method() === "GET" && path === "/api/v1/research/reports") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/jobs") {
        await route.fulfill({
          json: jobCreated
            ? [
                {
                  id: "job-1",
                  job_type: "parameter_sweep",
                  status: "pending",
                  progress: 0,
                  created_at: "2026-04-07T20:00:00Z",
                  strategy_id: "strategy-mean",
                  strategy_name: "example.mean_reversion",
                  strategy_path: "/repo/strategies/example/mean_reversion.py",
                  instruments: ["SPY.EQUS"],
                  objective: "sharpe",
                },
              ]
            : [],
        });
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
            default_config: { lookback: 20, zscore_threshold: 1.5 },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/research/sweeps") {
        const body = request.postDataJSON() as Record<string, unknown>;
        expect(body.strategy_id).toBe("strategy-mean");
        expect(body.instruments).toEqual(["SPY.EQUS"]);
        expect(body.max_parallelism).toBe(2);
        jobCreated = true;
        await route.fulfill({
          json: {
            job_id: "job-1",
            status: "pending",
          },
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/research");

    await expect(page.getByRole("heading", { name: "Research Console" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Launch Research Jobs" })).toBeVisible();
    await page.getByRole("button", { name: "Queue Research Job" }).click();

    await expect(page.getByText("Queued parameter sweep job job-1")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Research Job Queue" })).toBeVisible();
    await expect(page.getByText("pending")).toBeVisible();
    expect(seenHeaders.length).toBeGreaterThan(0);
  });

  test("queues a walk-forward run through API-key auth", async ({ page }) => {
    let jobCreated = false;

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;

      expect(request.headers()["x-api-key"]).toBe("msai-dev-key");

      if (request.method() === "GET" && path === "/api/v1/research/reports") {
        await route.fulfill({ json: [] });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/research/jobs") {
        await route.fulfill({
          json: jobCreated
            ? [
                {
                  id: "job-wf-1",
                  job_type: "walk_forward",
                  status: "pending",
                  progress: 0,
                  created_at: "2026-04-07T20:05:00Z",
                  strategy_id: "strategy-mean",
                  strategy_name: "example.mean_reversion",
                  strategy_path: "/repo/strategies/example/mean_reversion.py",
                  instruments: ["SPY.EQUS"],
                  objective: "sharpe",
                },
              ]
            : [],
        });
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
            default_config: { lookback: 20, zscore_threshold: 1.5 },
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/research/walk-forward") {
        const body = request.postDataJSON() as Record<string, unknown>;
        expect(body.strategy_id).toBe("strategy-mean");
        expect(body.instruments).toEqual(["SPY.EQUS"]);
        expect(body.train_days).toBe(2);
        expect(body.test_days).toBe(1);
        expect(body.step_days).toBe(1);
        expect(body.max_parallelism).toBe(2);
        expect(body.mode).toBe("rolling");
        jobCreated = true;
        await route.fulfill({
          json: {
            job_id: "job-wf-1",
            status: "pending",
          },
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/research");

    await page.getByLabel("Mode").selectOption("walk_forward");
    await page.getByLabel("Train Days").fill("2");
    await page.getByLabel("Test Days").fill("1");
    await page.getByLabel("Step Days").fill("1");
    await page.getByRole("button", { name: "Queue Research Job" }).click();

    await expect(page.getByText("Queued walk forward job job-wf-1")).toBeVisible();
    await expect(page.getByRole("button", { name: "Open Saved Report" })).toHaveCount(0);
    await expect(page.getByText("job-wf-1")).toBeVisible();
  });
});
