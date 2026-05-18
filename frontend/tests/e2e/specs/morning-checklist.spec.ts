/**
 * TJ-1: Morning checklist (smoke) — Pablo opens MSAI in the morning to
 * scan overnight state.
 *
 * Selectors recorded during verify-e2e iter-3 (see
 * tests/e2e/reports/2026-05-17-ui-completeness-iter3-convergence.md).
 *
 * Auth: X-API-Key via playwright.config.ts extraHTTPHeaders +
 * NEXT_PUBLIC_E2E_AUTH_BYPASS=1 wired in webServer.env.
 *
 * Pre-flight: stack up at http://localhost:3300 + http://localhost:8800.
 * Per CLAUDE.md E2E config, no raw DB writes / no Redis pushes for
 * ARRANGE — this spec uses the public API surface only.
 */
import { test, expect } from "@playwright/test";

test.describe("TJ-1 morning checklist @smoke", () => {
  test("dashboard renders portfolio summary + alerts feed without lies", async ({
    page,
  }) => {
    await page.goto("/dashboard");

    // Portfolio summary cards — use .first() because "Active Strategies"
    // appears in both PortfolioSummary card title AND the ActiveStrategies
    // component card title; strict mode would fail without a scope.
    await expect(page.getByText("Total Value")).toBeVisible();
    await expect(page.getByText("Daily P&L")).toBeVisible();
    await expect(page.getByText("Active Strategies").first()).toBeVisible();

    // Alerts feed renders (or shows empty state honestly). Wait up to 5s
    // for the TanStack-Query feed to settle past the skeleton.
    await expect(
      page
        .getByTestId("alerts-feed-list")
        .or(page.getByText(/All quiet|no recent alerts/i)),
    ).toBeVisible({ timeout: 5_000 });
  });

  test("alerts feed → detail sheet opens + closes", async ({ page }) => {
    await page.goto("/dashboard");
    const firstRow = page.getByTestId("alerts-feed-row").first();
    // Only run if alerts exist; otherwise skip gracefully
    const count = await page.getByTestId("alerts-feed-row").count();
    test.skip(count === 0, "No alerts seeded — skipping detail sheet check");

    await firstRow.click();
    // Detail panel renders (Sheet or page navigation per R19/R22)
    await expect(page.getByRole("dialog").or(page.locator("h1"))).toBeVisible();
  });

  test("live-trading audit drawer renders with BUY/SELL side labels", async ({
    page,
  }) => {
    await page.goto("/live-trading");
    const triggerCount = await page.getByTestId("audit-log-trigger").count();
    test.skip(
      triggerCount === 0,
      "No deployments seeded — skipping audit drawer check",
    );

    await page.getByTestId("audit-log-trigger").first().click();
    await expect(page.getByText("Latest 50 order attempts")).toBeVisible();
    // Either an empty state OR rows. If rows, ensure BUY/SELL not raw 1/2.
    const drawer = page.getByRole("dialog");
    const drawerText = await drawer.textContent();
    expect(drawerText).not.toMatch(/Side\s*1[^0-9]/);
    expect(drawerText).not.toMatch(/Side\s*2[^0-9]/);
  });

  test("dashboard persists across reload", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText("Total Value")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Total Value")).toBeVisible();
  });
});
