import type { Route } from "@playwright/test";
import { test as base, expect } from "@playwright/test";

/**
 * Per-URL override lookup.  Specs register a partial-URL match → body
 * pair; any non-matching route falls through to the catch-all empty
 * list/stub response so pages render without a backend.
 */
export type ApiMockOverrides = Record<string, unknown>;

/**
 * Shared mock infrastructure for specs that don't want to stand up a
 * real backend.  Three layers:
 *
 * 1. `/auth/me` → stub user so the auth provider settles.
 * 2. Registered overrides (URL-substring → JSON body).
 * 3. Catch-all: `{items: [], total: 0}` — matches every FastAPI list
 *    response shape so pages doing `setX(data.items)` don't crash.
 *
 * Feature specs call `registerApiMocks(page, { "/portfolios": { items,
 * total } })` in `beforeEach` to shape the fixture.
 */
export async function registerApiMocks(
  page: import("@playwright/test").Page,
  overrides: ApiMockOverrides = {},
): Promise<void> {
  await page.route(/\/api\/v1\/.*/, async (route: Route) => {
    const url = route.request().url();

    // User-supplied overrides win.  Longest match first so
    // "/portfolios/runs" doesn't accidentally pick up "/portfolios".
    const hit = Object.keys(overrides)
      .sort((a, b) => b.length - a.length)
      .find((needle) => url.includes(needle));
    if (hit) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(overrides[hit]),
      });
      return;
    }

    // Default shapes.  Keep the catch-all simple: match the response
    // shape each endpoint expects so pages don't crash on
    // `undefined.map(...)` or `Object.entries(undefined)`.
    let body: unknown;
    if (url.includes("/auth/me")) {
      body = { sub: "e2e-user", preferred_username: "e2e@msai.local" };
    } else if (url.includes("/market-data/symbols")) {
      body = { symbols: {} };
    } else if (url.includes("/market-data/status")) {
      body = { asset_classes: {}, total_files: 0, total_bytes: 0 };
    } else if (url.includes("/account/summary")) {
      body = { cash: 0, net_liquidation: 0, buying_power: 0 };
    } else if (url.includes("/live/status")) {
      body = { deployments: [] };
    } else {
      // Paginated list default — matches every FastAPI list response.
      body = { items: [], total: 0 };
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

export const test = base;
export { expect };
