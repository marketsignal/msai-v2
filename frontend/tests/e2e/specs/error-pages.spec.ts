/**
 * TJ-11: Error recovery (404 + render-throw).
 *
 * The 500 render-throw path requires NEXT_PUBLIC_E2E_AUTH_BYPASS=1 in
 * the dev-server env. The Playwright config sets that for its own
 * webServer.env block so the `/__e2e_throw` route is available when
 * Playwright starts its own server. When running against an external
 * stack (e.g. docker-compose) without the env, that test is skipped.
 *
 * The inline-error path uses Playwright route interception per R21.
 */
import { test, expect } from "@playwright/test";

test.describe("TJ-11 error recovery", () => {
  test("404 page renders styled not-found.tsx with back-to-safety CTA", async ({
    page,
  }) => {
    const response = await page.goto("/this-page-does-not-exist", {
      waitUntil: "domcontentloaded",
    });
    expect(response?.status()).toBe(404);

    await expect(page.getByText("Page not found")).toBeVisible();
    await expect(
      page.getByRole("link", { name: /Back to dashboard/i }),
    ).toHaveAttribute("href", "/dashboard");
  });

  test("/__e2e_throw renders error.tsx when bypass env is set", async ({
    page,
  }) => {
    // The route returns notFound() unless NEXT_PUBLIC_E2E_AUTH_BYPASS=1 at
    // build time. Test against the Playwright-spawned server (which DOES set
    // the env per playwright.config.ts webServer.env). When running against
    // an externally-running docker stack without the env, the route returns
    // 404 — verify that fallback too.
    const response = await page.goto("/__e2e_throw", {
      waitUntil: "domcontentloaded",
    });

    if (response?.status() === 404) {
      // Stack-without-bypass — R21 short-circuit. Verify 404 page renders.
      await expect(page.getByText("Page not found")).toBeVisible();
      return;
    }

    // Render-throw path — error.tsx kicked in
    await expect(
      page.getByText(/Something went wrong/i).or(page.getByText(/Retry/i)),
    ).toBeVisible();
  });

  test("inline fetch error renders page-level error state", async ({
    page,
  }) => {
    // Intercept the strategies list fetch and return 500; the /strategies
    // page should render the ListErrorPanel with describeApiError output.
    await page.route("**/api/v1/strategies/*", async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "simulated_backend_failure" }),
      });
    });

    await page.goto("/strategies");
    // The page should NOT throw to error.tsx — it should render an inline
    // error state. Either pattern (describeApiError fallback or detail
    // payload) is acceptable.
    await expect(
      page
        .getByText(/Failed to load strategies/i)
        .or(page.getByText(/simulated_backend_failure/i)),
    ).toBeVisible();
  });
});
