/** Controlled validation responses exercise form readability; this is not real E2E acceptance. */
import { expect, test } from "@playwright/test";

const strategyId = "11111111-1111-4111-8111-111111111111";
const splitMessage = "Requested holdout and purge leave no training range. Reduce holdout or purge days, or extend the training range.";
const privateConfigMarker = "submitted-config-must-not-be-echoed";
const referenceBaseConfig = {
  instrument_id: "AAPL.XNAS",
  bar_type: "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
  trade_size: "1",
  fast_ema_period: 5,
  slow_ema_period: 20,
};
const referenceParameterGrid = { fast_ema_period: [5, 10], slow_ema_period: [20] };
const splitErrorDetail = [{
  type: "value_error", loc: ["body"], msg: `Value error, ${splitMessage}`,
  input: {
    strategy_id: strategyId, instruments: ["AAPL.XNAS"],
    start_date: "2025-01-22", end_date: "2025-01-25",
    holdout_days: 2, purge_days: 2,
    base_config: { operator_note: privateConfigMarker.repeat(15) },
    parameter_grid: { fast_period: [5, 10], slow_period: [20] },
  },
  ctx: { error: {} },
}];

for (const validation of [
  {
    name: "actual Pydantic split error",
    viewportWidth: 375,
    detail: splitErrorDetail,
    expected: splitMessage,
  },
  {
    name: "field validation message",
    viewportWidth: 375,
    detail: [{
      type: "greater_than_equal", loc: ["body", "purge_days"],
      msg: "Input should be greater than or equal to 0", input: privateConfigMarker,
      ctx: { ge: 0 },
    }],
    expected: "Purge Days: Input should be greater than or equal to 0",
  },
  {
    name: "unrecognized validation shape",
    viewportWidth: 375,
    detail: [{ type: "unknown", input: { base_config: privateConfigMarker }, ctx: {} }],
    expected: "Research inputs are invalid. Check the dates, holdout and purge settings, and try again.",
  },
  {
    name: "desktop populated split error",
    viewportWidth: 1440,
    detail: splitErrorDetail,
    expected: splitMessage,
  },
]) {
  test(`${validation.name} is actionable without raw request content`, async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width: validation.viewportWidth, height: 900 });
    await page.route("**/api/v1/strategies/**", async (route) => {
      await route.fulfill({ json: { items: [{ id: strategyId, name: "Validation reference" }], total: 1 } });
    });
    await page.route("**/api/v1/research/jobs**", async (route) => {
      await route.fulfill({ json: { items: [], total: 0 } });
    });
    let submitted = false;
    await page.route("**/api/v1/research/sweeps", async (route) => {
      submitted = true;
      await route.fulfill({ status: 422, json: { detail: validation.detail } });
    });
    await page.goto("/research", { waitUntil: "domcontentloaded" });
    const launchResearch = page.getByRole("button", { name: "Launch Research", exact: true });
    await expect(launchResearch).toBeVisible();
    await launchResearch.click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Instruments", { exact: true }).fill("AAPL.XNAS");
    await dialog.getByLabel("Start Date", { exact: true }).fill("2025-01-22");
    await dialog.getByLabel("End Date", { exact: true }).fill("2025-01-25");
    await dialog.getByLabel("Holdout Days (optional)", { exact: true }).fill("2");
    await dialog.getByLabel("Purge Days", { exact: true }).fill("2");
    await dialog.getByLabel("Base Config (JSON)", { exact: true }).fill(JSON.stringify(referenceBaseConfig));
    await dialog.getByLabel("Parameter Grid (JSON)", { exact: true }).fill(JSON.stringify(referenceParameterGrid));
    await dialog.getByRole("button", { name: "Launch Research", exact: true }).click();

    const alert = dialog.getByRole("alert");
    await expect(alert).toBeVisible();
    expect(await alert.innerText()).toBe(validation.expected);
    expect(submitted, "the server response must drive rejection").toBe(true);
    await expect(alert).not.toContainText(privateConfigMarker);
    await expect(alert).not.toContainText('"input"');
    await expect(alert).not.toContainText('"ctx"');
    expect(await alert.evaluate((element) => element.scrollWidth <= element.clientWidth),
      "validation text must fit the visible alert width").toBe(true);
    await expect(dialog.getByRole("button", { name: "Launch Research", exact: true })).toBeEnabled();

    const geometry = await dialog.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      const outsideControls = Array.from(element.querySelectorAll("input, textarea, button, [role='combobox']"))
        .flatMap((control) => {
          const controlBounds = control.getBoundingClientRect();
          return controlBounds.left < bounds.left || controlBounds.right > bounds.right
            ? [{ id: control.id, tag: control.tagName, left: controlBounds.left, right: controlBounds.right }]
            : [];
        });
      return {
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        scrollLeft: element.scrollLeft,
        left: bounds.left,
        right: bounds.right,
        viewportWidth: window.innerWidth,
        gridTemplateColumns: getComputedStyle(element).gridTemplateColumns,
        children: Array.from(element.children).map((child) => ({
          slot: child.getAttribute("data-slot"),
          width: child.getBoundingClientRect().width,
          minWidth: getComputedStyle(child).minWidth,
        })),
        textareas: Array.from(element.querySelectorAll("textarea")).map((textarea) => ({
          id: textarea.id,
          width: textarea.getBoundingClientRect().width,
          fieldSizing: getComputedStyle(textarea).getPropertyValue("field-sizing"),
        })),
        outsideControls,
      };
    });
    expect(geometry.scrollWidth, JSON.stringify(geometry)).toBeLessThanOrEqual(geometry.clientWidth);
    expect(geometry.scrollLeft, "dialog must not require sideways scrolling").toBe(0);
    expect(geometry.left, "dialog must fit the viewport").toBeGreaterThanOrEqual(0);
    expect(geometry.right, "dialog must fit the viewport").toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.outsideControls, "every form control must fit the visible dialog").toEqual([]);
  });
}
