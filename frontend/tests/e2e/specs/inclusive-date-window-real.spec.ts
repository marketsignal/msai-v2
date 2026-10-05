import { expect, test, type Page } from "@playwright/test";

// Graduated from UC-IDW-UI after native computer-use acceptance against real services.
// Opt in only on the guarded, seeded local research stack. No response interception.
test.skip(
  process.env.MSAI_REAL_DATE_WINDOW_E2E !== "1",
  "Requires the guarded local runtime and existing December 2024 AAPL data",
);

const legacyId = "29e190ef-8b42-4115-90bc-34c100742597";

async function assertCompletedDay(page: Page) {
  await expect(page.getByText("Bars processed", { exact: true })).toBeVisible();
  await expect(page.getByText("completed", { exact: true })).toBeVisible();
  await expect(page.getByText("502", { exact: true })).toBeVisible();
  await expect(page.getByText("32", { exact: true })).toBeVisible();
  // The summary card precedes chart ticks that can repeat the same return.
  await expect(page.getByText("+0.000113%", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("32 fills · Page 1 of 1", { exact: true })).toBeVisible();
}

test("correct a reversed range, complete the whole day and reopen saved results", async ({ page }) => {
  test.setTimeout(300_000);
  await page.goto("/backtests");
  await page.getByRole("button", { name: "Run Backtest", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Run New Backtest" })).toBeVisible();
  await dialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "example.ema_cross", exact: true }).click();
  // Wait for the selected strategy's schema, rather than filling the previous schema.
  await expect(dialog.getByLabel("Fast Ema Period", { exact: true })).toBeVisible();
  await dialog.getByPlaceholder("AAPL, MSFT, SPY").fill("AAPL.NASDAQ");
  await dialog.getByLabel("Trade Size", { exact: true }).fill("1");
  await dialog.getByLabel("Fast Ema Period", { exact: true }).fill("5");
  await dialog.getByLabel("Slow Ema Period", { exact: true }).fill("20");
  await dialog.getByLabel("Start Date", { exact: true }).fill("2024-12-04");
  await dialog.getByLabel("End Date", { exact: true }).fill("2024-12-03");
  await expect(dialog.getByText("Includes both the start and end days in UTC.", { exact: true })).toBeVisible();

  const invalidResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === "/api/v1/backtests/run",
  );
  await dialog.getByRole("button", { name: "Run Backtest", exact: true }).click();
  expect((await invalidResponse).status()).toBe(422);
  await expect(dialog.getByText("End date must be on or after start date", { exact: true })).toBeVisible();

  await dialog.getByLabel("Start Date", { exact: true }).fill("2024-12-03");
  const submittedResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === "/api/v1/backtests/run",
  );
  await dialog.getByRole("button", { name: "Run Backtest", exact: true }).click();
  const submitted = await submittedResponse;
  expect(submitted.status()).toBe(201);
  const { id } = await submitted.json();
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(dialog).not.toBeVisible();
  const resultLink = page.locator(`a[href="/backtests/${id}"]`);
  // Use the history's supported refresh action while the simulation is queued.
  await expect.poll(async () => {
    if (await resultLink.isVisible()) return true;
    await page.getByRole("button", { name: "Refresh history", exact: true }).click();
    return resultLink.isVisible();
  }, { timeout: 180_000, intervals: [1_000, 3_000, 5_000] }).toBe(true);
  await resultLink.click();
  await expect(page.getByText("502", { exact: true })).toBeVisible({ timeout: 180_000 });
  await assertCompletedDay(page);

  await page.getByRole("tab", { name: "Full report", exact: true }).click();
  const report = page.frameLocator("iframe");
  await expect(report.getByText("3 Dec, 2024 - 3 Dec, 2024", { exact: true })).toBeVisible();
  await expect(report.getByText("0.000113%", { exact: true }).first()).toBeVisible();

  await page.reload();
  await assertCompletedDay(page);
  await page.getByRole("link", { name: "Backtests", exact: true }).click();
  await page.locator(`a[href="/backtests/${id}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/backtests/${id}$`));
  await assertCompletedDay(page);

  // Open the existing saved experiment without constructing or changing a legacy row.
  await page.goto(`/backtests/${legacyId}`);
  await expect(page.getByText("Not recorded", { exact: true })).toBeVisible();
  await expect(page.getByText("100", { exact: true })).toBeVisible();
  await expect(page.getByText("-0.0000530%", { exact: true }).first()).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/backtests/${id}$`));
  await assertCompletedDay(page);
});
