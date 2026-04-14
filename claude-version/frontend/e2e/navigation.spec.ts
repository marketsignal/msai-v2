import { expect, test } from "@playwright/test";
import { registerApiMocks } from "./fixtures";

/**
 * Navigation spec — verifies each top-level route renders its heading
 * without a backend.  This is the regression net that catches silent
 * routing breakages (e.g. a renamed page file breaks the sidebar link,
 * or a new layout swallows the h1).  Cheap to run, high signal.
 */

test.describe("navigation — all top-level routes render", () => {
  test.beforeEach(async ({ page }) => {
    await registerApiMocks(page);
  });

  const routes: { path: string; headingRe: RegExp }[] = [
    { path: "/dashboard", headingRe: /dashboard|msai|portfolio/i },
    { path: "/strategies", headingRe: /strateg/i },
    { path: "/backtests", headingRe: /backtest/i },
    { path: "/portfolio", headingRe: /portfolio/i },
    { path: "/live-trading", headingRe: /live/i },
    { path: "/market-data", headingRe: /market|data/i },
    { path: "/data-management", headingRe: /data|ingest/i },
    { path: "/research", headingRe: /research/i },
    { path: "/graduation", headingRe: /graduat/i },
    { path: "/settings", headingRe: /setting/i },
  ];

  for (const { path, headingRe } of routes) {
    test(`${path} renders`, async ({ page }) => {
      await page.goto(path);
      await expect(page).toHaveURL(new RegExp(path.replace("/", "\\/")));
      // First heading on the page — covers h1/h2/h3 and any role=heading.
      // If the page failed to render entirely (error boundary, infinite
      // loader) this will time out with a screenshot so the operator
      // can diagnose.
      await expect(
        page.getByRole("heading", { name: headingRe }).first(),
      ).toBeVisible({
        timeout: 15_000,
      });
    });
  }

  test("sidebar exposes expected nav links", async ({ page }) => {
    // Verify the sidebar has links to every top-level route.  We don't
    // click — the sidebar rerenders on auth/data settle in dev mode,
    // which detaches the `<a>` mid-click.  Asserting the `href` is
    // equivalent: if the href is correct, the route is navigable, and
    // `/strategies renders` above proves the destination works.
    await page.goto("/dashboard");
    // Wait for React hydration — `networkidle` alone isn't enough on
    // `next dev`; the initial HTML is just the hydration payload.  Wait
    // for the h1 (which only exists post-hydration).
    await page.waitForFunction(
      () => !!document.querySelector("h1"),
      undefined,
      {
        timeout: 30_000,
      },
    );
    for (const route of [
      "/strategies",
      "/backtests",
      "/portfolio",
      "/live-trading",
      "/market-data",
      "/data-management",
      "/research",
      "/graduation",
      "/settings",
    ]) {
      await expect(
        page.locator(`aside a[href="${route}"]`).first(),
      ).toBeVisible();
    }
  });
});
