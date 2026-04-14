import { expect, test } from "@playwright/test";
import { registerApiMocks } from "./fixtures";

/**
 * Portfolio creation flow — exercises the `CreatePortfolioDialog` that
 * PR #6 wired up to the new orchestration schema (heuristic-by-objective
 * weights, `allocations: min_length=1`, etc.).  The spec drives the
 * form through a complete happy-path submission and intercepts the POST
 * to assert the payload shape the backend expects.
 */

test.describe("portfolio creation dialog", () => {
  test.beforeEach(async ({ page }) => {
    await registerApiMocks(page, {
      "/portfolios/runs": { items: [], total: 0 },
      "/portfolios": { items: [], total: 0 },
      // Graduation candidates — the allocation combobox needs something
      // to pick from.  Mock two graduated candidates.
      "/graduation/candidates": {
        items: [
          {
            id: "11111111-1111-1111-1111-111111111111",
            strategy_name: "EMA Cross",
            stage: "promoted",
            metrics: { sharpe: 1.8, sortino: 2.1 },
          },
          {
            id: "22222222-2222-2222-2222-222222222222",
            strategy_name: "Donchian Breakout",
            stage: "promoted",
            metrics: { sharpe: 0.9, sortino: 1.1 },
          },
        ],
        total: 2,
      },
    });
  });

  test("opens, validates required name, submits with heuristic weights", async ({
    page,
  }) => {
    // Capture the POST body so we can assert the schema shape.
    let postedBody: unknown = null;
    await page.route("**/api/v1/portfolios", async (route) => {
      if (route.request().method() === "POST") {
        postedBody = route.request().postDataJSON();
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: "99999999-9999-9999-9999-999999999999",
            name:
              postedBody &&
              typeof postedBody === "object" &&
              "name" in postedBody
                ? (postedBody as { name: string }).name
                : "Test",
            description: null,
            objective: "maximize_sharpe",
            base_capital: 100000.0,
            requested_leverage: 1.0,
            downside_target: null,
            benchmark_symbol: null,
            account_id: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          }),
        });
      } else {
        // GET — return the empty list.
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ items: [], total: 0 }),
        });
      }
    });

    await page.goto("/portfolio");
    await expect(
      page.getByRole("heading", { name: /portfolios/i }).first(),
    ).toBeVisible();

    // Open the Create Portfolio dialog.
    await page.getByRole("button", { name: /create portfolio/i }).click();
    await expect(page.getByRole("dialog")).toBeVisible();

    // Try to submit with an empty name — form should refuse.
    const submitButton = page
      .getByRole("dialog")
      .getByRole("button", { name: /^create(?!.*allocation)/i });
    await submitButton.click();
    // Dialog stays open because validation failed.
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByText(/name is required/i).first()).toBeVisible();

    // Fill the form.  Name, leave objective as default
    // ("maximize_sharpe"), add one allocation, leave weight blank so
    // the backend applies the heuristic.
    await page.getByLabel(/^name$/i).fill("E2E Test Portfolio");
    // Verify the default Objective selection is maximize_sharpe — the
    // component's selected-value text is rendered in the trigger.
    await expect(
      page
        .getByRole("combobox")
        .filter({ hasText: /sharpe/i })
        .first(),
    ).toBeVisible();

    // Add one allocation row.  The allocation row renders two inputs:
    // a candidate-id input (placeholder "UUID") and a weight input.
    // Target by placeholder — stable across class name changes.
    await page.getByRole("button", { name: /add allocation/i }).click();
    await page
      .getByPlaceholder("UUID")
      .first()
      .fill("11111111-1111-1111-1111-111111111111");
    // Leave weight blank — the whole point of the PR #6 contract.

    await submitButton.click();

    // Dialog closes on success.
    await expect(page.getByRole("dialog")).toBeHidden({ timeout: 10_000 });

    // Verify the payload matched the new schema: weight = null for
    // heuristic derivation, single allocation, objective present.
    expect(postedBody).toBeTruthy();
    const body = postedBody as {
      name: string;
      objective: string;
      allocations: { candidate_id: string; weight: number | null }[];
    };
    expect(body.name).toBe("E2E Test Portfolio");
    expect(body.objective).toBe("maximize_sharpe");
    expect(body.allocations).toHaveLength(1);
    expect(body.allocations[0]?.weight).toBeNull();
    expect(body.allocations[0]?.candidate_id).toBe(
      "11111111-1111-1111-1111-111111111111",
    );
  });
});
