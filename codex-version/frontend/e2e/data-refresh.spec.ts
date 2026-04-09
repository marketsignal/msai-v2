import { expect, test } from "@playwright/test";

test.describe("data refresh workflow", () => {
  test("loads, edits, and queues the configured daily universe through API-key auth", async ({ page }) => {
    let currentUniverse = [
      {
        asset_class: "equities",
        symbols: ["SPY", "IWM", "DIA", "EFA", "EEM", "GLD"],
        provider: "databento",
        dataset: "ARCX.PILLAR",
        schema: "ohlcv-1m",
      },
      {
        asset_class: "equities",
        symbols: ["QQQ"],
        provider: "databento",
        dataset: "XNAS.ITCH",
        schema: "ohlcv-1m",
      },
      {
        asset_class: "futures",
        symbols: ["ES.v.0", "NQ.v.0", "RTY.v.0", "YM.v.0", "GC.v.0"],
        provider: "databento",
        dataset: "GLBX.MDP3",
        schema: "ohlcv-1m",
      },
    ];
    let savedUniverse: Array<Record<string, unknown>> | null = null;
    let configuredRefreshCalls = 0;

    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const headers = request.headers();
      expect(headers["x-api-key"]).toBe("msai-dev-key");
      expect(headers.authorization).toBeUndefined();

      const url = new URL(request.url());
      const path = url.pathname;

      if (request.method() === "GET" && path === "/api/v1/market-data/status") {
        await route.fulfill({
          json: {
            last_run_at: "2026-04-06T23:59:59Z",
            storage_stats: {
              equities: { bytes: 1000, file_count: 10 },
              futures: { bytes: 2000, file_count: 5 },
            },
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/market-data/symbols") {
        await route.fulfill({
          json: {
            symbols: {
              equities: ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD"],
              futures: ["ES.v.0", "NQ.v.0", "RTY.v.0", "YM.v.0", "GC.v.0"],
            },
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/market-data/daily-universe") {
        await route.fulfill({
          json: {
            requests: currentUniverse,
          },
        });
        return;
      }

      if (request.method() === "PUT" && path === "/api/v1/market-data/daily-universe") {
        const payload = request.postDataJSON() as { requests: Array<Record<string, unknown>> };
        savedUniverse = payload.requests;
        currentUniverse = payload.requests as typeof currentUniverse;
        await route.fulfill({
          json: {
            requests: currentUniverse,
          },
        });
        return;
      }

      if (request.method() === "POST" && path === "/api/v1/market-data/ingest-daily-configured") {
        configuredRefreshCalls += 1;
        await route.fulfill({
          json: {
            status: "queued",
            start: "2026-04-06",
            end: "2026-04-07",
            request_count: currentUniverse.length,
          },
        });
        return;
      }

      if (request.method() === "GET" && path === "/api/v1/alerts/") {
        await route.fulfill({
          json: {
            alerts: [
              {
                type: "alert",
                level: "error",
                title: "Daily ingest failed",
                message: "Databento futures refresh timed out.",
                created_at: "2026-04-07T12:00:00Z",
              },
            ],
          },
        });
        return;
      }

      await route.fulfill({ status: 404, json: { detail: `Unhandled route: ${path}` } });
    });

    await page.goto("/data");

    await expect(page.getByRole("heading", { name: "Ingestion Status" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Daily Universe" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Recent Alerts" })).toBeVisible();
    await expect(page.getByText("Daily ingest failed")).toBeVisible();
    await expect(page.getByText("3 request groups currently configured.")).toBeVisible();

    const textarea = page.locator("textarea");
    await expect(textarea).toContainText('"QQQ"');

    const updatedUniverse = [
      {
        asset_class: "equities",
        symbols: ["SPY", "QQQ"],
        provider: "databento",
        dataset: "EQUS.MINI",
        schema: "ohlcv-1m",
      },
      {
        asset_class: "futures",
        symbols: ["ES.v.0", "NQ.v.0"],
        provider: "databento",
        dataset: "GLBX.MDP3",
        schema: "ohlcv-1m",
      },
    ];
    await textarea.fill(JSON.stringify(updatedUniverse, null, 2));
    await page.getByRole("button", { name: "Save Daily Universe" }).click();

    await expect
      .poll(() => savedUniverse, {
        timeout: 10_000,
      })
      .toEqual(updatedUniverse);

    await expect(page.getByText("Daily universe saved.")).toBeVisible();
    await expect(page.getByText("2 request groups currently configured.")).toBeVisible();

    await page.getByRole("button", { name: "Trigger Download" }).click();

    await expect
      .poll(() => configuredRefreshCalls, {
        timeout: 10_000,
      })
      .toBe(1);

    await expect(page.getByText("Configured daily universe queued.")).toBeVisible();
  });
});
