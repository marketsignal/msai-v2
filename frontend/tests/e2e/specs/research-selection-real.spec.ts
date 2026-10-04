/** Real research journeys. Requires the explicitly selected guarded, existing-data local target. */
import { expect, test, type Page, type Locator } from "@playwright/test";

const baseConfig = {
  instrument_id: "AAPL.XNAS", bar_type: "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
  trade_size: "1", fast_ema_period: 5, slow_ema_period: 20,
};
const parameterGrid = { fast_ema_period: [5, 10], slow_ema_period: [20] };
const splitMessage = "Requested holdout and purge leave no training range. Reduce holdout or purge days, or extend the training range.";

test.describe("real training selection and exploratory discovery", () => {
  test.describe.configure({ mode: "serial" });
  test.skip(process.env.MSAI_REAL_RESEARCH_E2E !== "1", "Opt in only against guarded existing-data research services.");
  test.setTimeout(180_000);

  async function openForm(page: Page, walkForward = false, invalid = false): Promise<Locator> {
    await page.goto("/research", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("button", { name: "Launch Research", exact: true })).toBeVisible();
    if (await page.getByRole("button", { name: "Retry", exact: true }).isVisible()) {
      await page.getByRole("button", { name: "Retry", exact: true }).click();
    }
    await page.getByRole("button", { name: "Launch Research", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Launch Research Job", exact: true });
    await dialog.getByRole("combobox", { name: "Strategy", exact: true }).click();
    await page.getByRole("option", { name: "example.ema_cross", exact: true }).click();
    if (walkForward) await dialog.getByRole("button", { name: "Walk Forward", exact: true }).click();
    await dialog.getByRole("combobox", { name: "Objective", exact: true }).click();
    await page.getByRole("option", { name: "Total Return", exact: true }).click();
    await dialog.getByLabel("Instruments", { exact: true }).fill("AAPL.XNAS");
    await dialog.getByLabel("Start Date", { exact: true }).fill("2025-01-22");
    await dialog.getByLabel("End Date", { exact: true }).fill("2025-01-25");
    await expect(dialog.getByLabel("Start Date", { exact: true })).toHaveValue("2025-01-22");
    await expect(dialog.getByLabel("End Date", { exact: true })).toHaveValue("2025-01-25");
    await dialog.getByLabel("Holdout Days (optional)", { exact: true }).fill(walkForward ? "" : "2");
    await dialog.getByLabel("Purge Days", { exact: true }).fill("0");
    await dialog.getByLabel("Base Config (JSON)", { exact: true }).fill(JSON.stringify({ ...baseConfig, trade_size: invalid ? "invalid" : "1" }));
    await dialog.getByLabel("Parameter Grid (JSON)", { exact: true }).fill(JSON.stringify(parameterGrid));
    if (walkForward) {
      for (const label of ["Train Days", "Test Days", "Step Days"]) {
        await dialog.getByLabel(label, { exact: true }).fill("2");
      }
    }
    return dialog;
  }

  async function submitAndOpen(page: Page, dialog: Locator, walkForward = false): Promise<string> {
    // Observe the actual submission identity, then follow its visible history link; no response substitution.
    const responsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST" && response.url().endsWith(walkForward ? "/api/v1/research/walk-forward" : "/api/v1/research/sweeps"));
    await dialog.getByRole("button", { name: "Launch Research", exact: true }).click();
    const response = await responsePromise;
    expect(response.ok()).toBe(true);
    const created: { id: string } = await response.json();
    await expect(dialog).not.toBeVisible();
    const historyLink = page.locator(`a[href="/research/${created.id}"]`);
    await expect(historyLink).toBeVisible();
    await historyLink.click();
    await expect(page.getByTestId("research-selection")).toContainText("Training Selection · Exploratory");
    await expect(page.getByText("Completed execution does not establish validated alpha.", { exact: false })).toBeVisible();
    return created.id;
  }

  async function expectSweep(page: Page): Promise<void> {
    await expect(page.getByTestId("research-create-discovery")).toBeEnabled();
    await expect(page.getByText("Selected trial index: 0 (zero-based).", { exact: true })).toBeVisible();
    await expect(page.getByText("Holdout diagnostic · Succeeded", { exact: true })).toBeVisible();
    await expect(page.getByText("Full-period replay diagnostic · Succeeded", { exact: true })).toBeVisible();
    await expect(page.getByText(/Training: 2025-01-22 to 2025-01-23/)).toBeVisible();
    await expect(page.getByText("2025-01-24 to 2025-01-25", { exact: true })).toBeVisible();
  }

  async function openDiscovery(page: Page, jobId: string, policy: string, fast: number, createdText?: string): Promise<string> {
    await expect(page.getByRole("heading", { name: "Graduation Pipeline", exact: true })).toBeVisible();
    const cards = page.getByRole("button", { name: /^example\.ema_cross S:/ });
    await expect(cards.first()).toBeVisible();
    for (let index = 0; index < await cards.count(); index += 1) {
      await cards.nth(index).click();
      const lineage = page.getByRole("link", { name: "View Research Job", exact: true });
      const provenance = page.getByTestId("candidate-selection-provenance");
      if (await lineage.getAttribute("href") !== `/research/${jobId}` || !(await provenance.innerText()).includes(policy)) continue;
      await expect(provenance).toContainText(`${policy} · Exploratory`);
      await expect(page.getByRole("heading", { name: "Training Metrics", exact: true })).toBeVisible();
      await expect(page.locator("pre")).toContainText(`"fast_ema_period": ${fast}`);
      await expect(page.locator("pre")).not.toContainText('"selection"');
      await expect(page.getByText("[object Object]", { exact: false })).toHaveCount(0);
      const creation = page.getByText(/^Created /);
      const observed = await creation.innerText();
      if (createdText && observed !== createdText) continue;
      await expect(creation).toBeVisible();
      return observed;
    }
    throw new Error(`No visible discovery card matched research lineage ${jobId} and ${policy}`);
  }

  async function createAndRevisit(page: Page, jobId: string, policy: string, fast: number, explicit = false): Promise<void> {
    await page.getByRole("button", { name: explicit ? "Create Discovery from trial 1" : "Create Discovery Candidate", exact: true }).click();
    await page.getByRole("link", { name: "View Graduation Pipeline", exact: true }).click();
    const creation = await openDiscovery(page, jobId, policy, fast);
    await page.reload({ waitUntil: "domcontentloaded" });
    await openDiscovery(page, jobId, policy, fast, creation);
  }

  test("sweep retains training choice, separate diagnostics and automatic/manual Discovery", async ({ page }, info) => {
    const jobId = await submitAndOpen(page, await openForm(page));
    await expectSweep(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expectSweep(page);
    await createAndRevisit(page, jobId, "Automatic best training choice", 5);
    await page.getByRole("link", { name: "View Research Job", exact: true }).click();
    await createAndRevisit(page, jobId, "Explicit trial choice", 10, true);
    await page.screenshot({ path: info.outputPath("manual-discovery.png"), fullPage: true });
  });

  test("walk-forward retains latest training window and separate test diagnostic", async ({ page }, info) => {
    const jobId = await submitAndOpen(page, await openForm(page, true), true);
    const assertWindow = async (): Promise<void> => {
      await expect(page.getByTestId("research-create-discovery")).toBeEnabled();
      await expect(page.getByText("Selected window index: 0 (zero-based).", { exact: true })).toBeVisible();
      await expect(page.getByText("Window 0 · Latest training window", { exact: true })).toBeVisible();
      await expect(page.getByText("Test diagnostic · Succeeded", { exact: true })).toBeVisible();
      await expect(page.getByText("Holdout diagnostic · Not requested", { exact: true })).toBeVisible();
    };
    await assertWindow();
    await page.reload({ waitUntil: "domcontentloaded" });
    await assertWindow();
    await createAndRevisit(page, jobId, "Automatic latest window training choice", 5);
    await page.screenshot({ path: info.outputPath("walk-forward-discovery.png"), fullPage: true });
  });

  test("failed training and impossible split refuse clearly, corrected research persists, legacy stays unknown", async ({ page }, info) => {
    await submitAndOpen(page, await openForm(page, false, true));
    const assertInvalid = async (): Promise<void> => {
      await expect(page.getByTestId("research-discovery-refusal")).toContainText("No eligible automatic training result");
      await expect(page.getByTestId("research-create-discovery")).toBeDisabled();
      await expect(page.getByRole("cell", { name: "failed", exact: true })).toHaveCount(2);
      for (const index of [0, 1]) await expect(page.getByRole("button", { name: `Create Discovery from trial ${index}`, exact: true })).toBeDisabled();
      await expect(page.getByRole("cell", { name: "--", exact: true })).toHaveCount(4);
    };
    await assertInvalid();
    await page.reload({ waitUntil: "domcontentloaded" });
    await assertInvalid();
    const dialog = await openForm(page);
    await dialog.getByLabel("Purge Days", { exact: true }).fill("2");
    await dialog.getByRole("button", { name: "Launch Research", exact: true }).click();
    await expect(dialog.getByRole("alert")).toHaveText(splitMessage);
    await expect(dialog.getByRole("alert")).not.toContainText('"input"');
    await expect(dialog.getByRole("alert")).not.toContainText('"ctx"');
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth && element.scrollLeft === 0)).toBe(true);
    await expect(dialog.getByRole("button", { name: "Launch Research", exact: true })).toBeEnabled();
    await page.screenshot({ path: info.outputPath("real-split-guidance.png") });
    await dialog.getByLabel("Purge Days", { exact: true }).fill("0");
    await submitAndOpen(page, dialog);
    await expectSweep(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expectSweep(page);
    const legacyId = process.env.MSAI_RESEARCH_LEGACY_JOB_ID ?? "4f39f4ff-3c54-44e1-a190-b0123feba503";
    await page.goto(`/research/${legacyId}`, { waitUntil: "domcontentloaded" });
    const assertLegacy = async (): Promise<void> => {
      await expect(page.getByTestId("research-selection")).toContainText("Unknown Selection · Legacy");
      await expect(page.getByTestId("research-discovery-refusal")).toContainText(/rerun/i);
      await expect(page.getByTestId("research-create-discovery")).toBeDisabled();
    };
    await assertLegacy();
    await page.reload({ waitUntil: "domcontentloaded" });
    await assertLegacy();
  });
});

