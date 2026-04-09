import { expect, test } from "@playwright/test";

const runRealBackend = process.env.PW_REAL_BACKEND === "1";

test.describe("live control surface (real backend)", () => {
  test.skip(!runRealBackend, "set PW_REAL_BACKEND=1 to run against the live backend");

  test("loads the live dashboard against the running paper stack", async ({ page }) => {
    await page.goto("/live");

    await expect(page.getByText("API key mode")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Deploy Strategy" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Active Strategies" })).toBeVisible();
    await expect(page.locator("select")).toBeVisible();

    await expect
      .poll(async () => page.locator("select option").count(), {
        timeout: 30_000,
      })
      .toBeGreaterThan(0);
  });
});
