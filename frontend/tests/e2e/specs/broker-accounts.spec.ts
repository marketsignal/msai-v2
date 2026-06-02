/**
 * UC-BA-UI-1: Operator adds a broker account through the Settings wizard and it
 * survives a reload — graduated from tests/e2e/use-cases/broker-accounts/add-via-wizard-ui.md.
 *
 * Selectors recorded during the verify-e2e feature run (see
 * tests/e2e/reports/2026-06-02-09-30-broker-account-entity.md "Observed selectors").
 *
 * Auth: dev bypass via the webServer env (NEXT_PUBLIC_E2E_AUTH_BYPASS=1) + X-API-Key,
 * same pattern as the other graduated specs. Backend on :8800 with broker_accounts
 * migrated and at least one free gateway slot.
 *
 * Determinism: a unique ib_account_id per run (DU + timestamp) avoids the
 * duplicate-active-account 409 on repeated local runs; CI runs against a fresh DB.
 * The wizard default trading_mode is "paper", which requires a DU/DF-prefixed id.
 *
 * NOTE (local run): this UI flow was verified PASS end-to-end by the verify-e2e
 * agent (tests/e2e/reports/2026-06-02-09-30-broker-account-entity.md) with these
 * exact selectors. A `pnpm exec playwright test` run that has to START its own
 * webServer currently fails to launch it due to a PRE-EXISTING issue in
 * playwright.config.ts (`command: "pnpm dev -- --port 3300"` — Next misparses
 * `--port` as a project dir); this affects ALL specs, is unrelated to this
 * feature, and is flagged as a separate follow-up. Locally, run against the
 * already-serving dev frontend (reuseExistingServer) once it has the auth bypass.
 */
import { test, expect } from "@playwright/test";

test.describe("Feature: broker-accounts", () => {
  test("UC-BA-UI-1: add account via wizard, survives reload, no cleartext @smoke", async ({
    page,
  }) => {
    const ibAccountId = `DU${Date.now().toString().slice(-9)}`;
    const loginKey = `login-${ibAccountId.toLowerCase()}`;
    const password = "ui-spec-secret-pw"; // never expected to appear on screen

    await page.goto("/settings/broker-accounts");
    // Precondition: wait for the header "Add" button, which is ALWAYS present
    // regardless of whether the list has rows. On a fresh DB (the CI case) the
    // page renders the empty state (broker-accounts-empty), not the table, so a
    // table-only precondition would time out before we can start the add flow.
    const headerAdd = page.getByTestId("broker-accounts-add");
    await expect(headerAdd).toBeVisible();

    // Step 1 — identity
    await headerAdd.click();
    await expect(page.getByTestId("broker-account-wizard")).toBeVisible();
    await page.getByTestId("broker-account-ib-account-id").fill(ibAccountId);
    await page.getByTestId("broker-account-ib-login-key").fill(loginKey);
    // trading_mode defaults to "Paper" (valid for a DU-prefixed account)
    await page.getByTestId("broker-account-wizard-next").click();

    // Step 2 — credentials (masked)
    const pwField = page.getByTestId("broker-account-tws-password");
    await expect(pwField).toHaveAttribute("type", "password");
    await page.getByTestId("broker-account-tws-userid").fill("ui-spec-user");
    await pwField.fill(password);
    await page.getByTestId("broker-account-wizard-next").click();

    // Step 3 — review → create
    await page.getByRole("button", { name: /create/i }).click();

    // The new row appears in the list
    const row = page
      .locator('[data-testid^="broker-account-row-"]')
      .filter({ hasText: ibAccountId });
    await expect(row).toBeVisible();

    // No cleartext password anywhere on the page
    expect(await page.locator("body").textContent()).not.toContain(password);

    // Persistence — survives reload, still no cleartext
    await page.reload();
    await expect(
      page
        .locator('[data-testid^="broker-account-row-"]')
        .filter({ hasText: ibAccountId }),
    ).toBeVisible();
    expect(await page.locator("body").textContent()).not.toContain(password);
  });
});
