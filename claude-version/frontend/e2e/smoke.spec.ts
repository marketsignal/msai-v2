import { expect, test } from "@playwright/test";

/**
 * Smoke tests — verify the Playwright harness works end-to-end.
 *
 * These do not require the backend to be running: they mock the `/api/v1`
 * surface so the dashboard/strategies shells render without a live API.
 * The goal is to prove that: (a) dev-mode auth bypass lets us reach the
 * app without Entra login, (b) the navigation shell renders, (c) the
 * shadcn/ui primitives load without runtime errors.
 */

test.describe("smoke — harness", () => {
  test.beforeEach(async ({ page }) => {
    // Empty defaults for every API call so the app renders without a
    // backend.  Individual feature specs can override in beforeEach.
    // Default: return a paginated empty list for list-style endpoints
    // and a minimal stub for auth.  Individual feature specs override.
    // The `{items: [], total: 0}` shape matches every list response in
    // the FastAPI schemas, so pages that call `setX(data.items)` don't
    // crash on `undefined.map(...)`.
    await page.route(/\/api\/v1\/.*/, async (route) => {
      const url = route.request().url();
      const body = url.includes("/auth/me")
        ? { sub: "e2e-user", preferred_username: "e2e@msai.local" }
        : { items: [], total: 0 };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    });
  });

  test("dashboard renders with auth bypass + API mocks", async ({ page }) => {
    await page.goto("/");
    // The app shell redirects "/" → "/dashboard" in some builds; accept either.
    await expect(page).toHaveURL(/\/(dashboard)?$/);
    await expect(page.locator("body")).toBeVisible();
    // MSAI branding in the header/sidebar — stable selector across
    // future refactors because the name is the brand.
    await expect(page.getByText(/MSAI|MarketSignal/i).first()).toBeVisible();
  });

  test("navigates to strategies page", async ({ page }) => {
    await page.goto("/strategies");
    await expect(page).toHaveURL(/\/strategies/);
    const heading = page.getByRole("heading", { name: /strateg/i }).first();
    await expect(heading).toBeVisible({ timeout: 15_000 });
  });

  test("navigates to backtests page", async ({ page }) => {
    await page.goto("/backtests");
    await expect(page).toHaveURL(/\/backtests/);
    await expect(
      page.getByRole("heading", { name: /backtest/i }).first(),
    ).toBeVisible({
      timeout: 15_000,
    });
  });

  test("navigates to portfolio page", async ({ page }) => {
    await page.goto("/portfolio");
    await expect(page).toHaveURL(/\/portfolio/);
    await expect(
      page.getByRole("heading", { name: /portfolio/i }).first(),
    ).toBeVisible({
      timeout: 15_000,
    });
  });
});
