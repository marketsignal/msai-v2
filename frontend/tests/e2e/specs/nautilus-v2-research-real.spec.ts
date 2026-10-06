import { expect, test, type Page } from "@playwright/test";

// Graduated from UC-V2-003 after actual preliminary browser acceptance.
// Requires the sanctioned isolated fixture/runtime; never intercept responses.
test.skip(
  process.env.MSAI_REAL_V2_RESEARCH_E2E !== "1" ||
    process.env.PLAYWRIGHT_BASE_URL !== "http://localhost:13300",
  "Requires explicit opt-in and the isolated V2 research UI on localhost:13300",
);

const baselineId = "81766b6e-9da8-4a67-814b-ef7a73e8fb37";

async function assertNativeResult(page: Page) {
  await expect(page.getByRole("heading", { name: /^Backtest [0-9a-f]{8}$/ })).toBeVisible();
  await expect(page.getByText("completed", { exact: true })).toBeVisible();
  await expect(page.getByText("390", { exact: true })).toBeVisible();
  await expect(page.getByText("+0.000210%", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("16 fills · Page 1 of 1", { exact: true })).toBeVisible();
  await expect(page.getByTestId("trade-log").locator("tbody tr")).toHaveCount(16);
  const scope = page.getByTestId("backtest-accounting-scope");
  await expect(scope.getByRole("heading", { name: "Research simulation" })).toBeVisible();
  await expect(scope).toContainText("Opening capital: 1,000,000 USD");
  await expect(scope).toContainText("excluding unrealized gains and losses");
  const assumptions = page.getByTestId("backtest-simulation-assumptions");
  for (const value of ["2.0.0rc6", "1×", "FixedFeeModel", "0.00 USD", "DefaultFillModel", "42", "0 % probability", "Synthetic fixture"]) {
    await expect(assumptions.getByText(value, { exact: true })).toBeVisible();
  }
  await expect(scope).toContainText("exhausted displayed L1 size");
  await expect(page.getByTestId("backtest-synthetic-scope")).toContainText(
    "Generated minute data uses UTC weekdays, including holidays.",
  );
}

test("correct invalid size, inspect native assumptions and revisit unchanged results", async ({ page }) => {
  test.setTimeout(300_000);
  await page.goto("/backtests");
  await page.getByRole("button", { name: "Run Backtest", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox").click();
  await page.getByRole("option", { name: "example.ema_cross", exact: true }).click();
  await expect(dialog.getByLabel("Fast Ema Period", { exact: true })).toBeVisible();
  await dialog.getByPlaceholder("AAPL, MSFT, SPY").fill("AAPL.NASDAQ");
  await dialog.getByLabel("Fast Ema Period", { exact: true }).fill("10");
  await dialog.getByLabel("Slow Ema Period", { exact: true }).fill("30");
  await dialog.getByLabel("Start Date", { exact: true }).fill("2025-01-02");
  await dialog.getByLabel("End Date", { exact: true }).fill("2025-01-02");
  await expect(dialog.getByLabel("Start Date", { exact: true })).toHaveValue("2025-01-02");
  await expect(dialog.getByLabel("End Date", { exact: true })).toHaveValue("2025-01-02");
  await dialog.getByLabel("Trade Size", { exact: true }).fill("-1");

  const invalidResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === "/api/v1/backtests/run",
  );
  await dialog.getByRole("button", { name: "Run Backtest", exact: true }).click();
  expect((await invalidResponse).status()).toBe(422);
  await expect(dialog.getByText("Input should be greater than 0", { exact: true })).toBeVisible();
  await dialog.getByLabel("Trade Size", { exact: true }).fill("1");

  const submittedResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === "/api/v1/backtests/run",
  );
  await dialog.getByRole("button", { name: "Run Backtest", exact: true }).click();
  const submitted = await submittedResponse;
  expect(submitted.status()).toBe(201);
  expect(submitted.request().postDataJSON()).toMatchObject({
    start_date: "2025-01-02", end_date: "2025-01-02",
  });
  const { id } = await submitted.json();
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  await expect(dialog).not.toBeVisible();
  const resultLink = page.locator(`a[href="/backtests/${id}"]`);
  await expect.poll(async () => {
    if (await resultLink.isVisible()) return true;
    await page.getByRole("button", { name: "Refresh history", exact: true }).click();
    return resultLink.isVisible();
  }, { timeout: 180_000, intervals: [1_000, 3_000, 5_000] }).toBe(true);
  await resultLink.click();
  await expect(page).toHaveURL(new RegExp(`/backtests/${id}$`));
  await expect(page.getByText("390", { exact: true })).toBeVisible({ timeout: 180_000 });
  await assertNativeResult(page);

  await page.getByRole("tab", { name: "Full report", exact: true }).click();
  const report = page.frameLocator("iframe");
  await expect(report.getByRole("heading", { name: "Recorded run assumptions" })).toBeVisible();
  await expect(report.getByText(/Engine: 2\.0\.0rc6; Leverage: 1\.0;/)).toBeVisible();
  await expect(report.getByText(/Synthetic minute-equity fixture using UTC weekdays/)).toBeVisible();
  await expect(report.getByText(/Total return: 0\.000210%/)).toBeVisible();
  await page.reload();
  await assertNativeResult(page);
  await page.getByRole("link", { name: "Backtests", exact: true }).click();
  await page.locator(`a[href="/backtests/${id}"]`).click();
  await assertNativeResult(page);
  await page.getByRole("tab", { name: "Full report", exact: true }).click();
  await expect(report.getByRole("heading", { name: "Recorded run assumptions" })).toBeVisible();
  await expect(report.getByText(/Total return: 0\.000210%/)).toBeVisible();

  await page.goto(`/backtests/${baselineId}`);
  await expect(page.getByText("390", { exact: true })).toBeVisible();
  await expect(page.getByTestId("backtest-simulation-assumptions")
    .getByText("Not recorded", { exact: true })).toHaveCount(8);
  await expect(page.getByTestId("backtest-synthetic-scope")).not.toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/backtests/${id}$`));
  await assertNativeResult(page);
});
