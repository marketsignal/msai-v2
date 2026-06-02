/**
 * Deep-link / refresh auth-guard race — graduated from
 * tests/e2e/use-cases/auth/deeplink-auth-race.md (UC-DLA-1 + UC-DLA-2).
 *
 * Subject of the fix:
 *   - frontend/src/components/layout/app-shell.tsx — gate the auth redirect on MSAL
 *     having SETTLED (inProgress === InteractionStatus.None) so a full-page load of
 *     a protected route doesn't bounce through /login to /dashboard.
 *   - frontend/src/components/layout/sidebar.tsx — add a "Broker Accounts" nav item
 *     and a longest-prefix activeHref so /settings/broker-accounts highlights only
 *     that item (not also "Settings").
 *
 * Auth: dev bypass (NODE_ENV=development / NEXT_PUBLIC_E2E_AUTH_BYPASS=1 / X-API-Key),
 * same pattern as the other graduated specs. Run against the already-serving dev
 * frontend on :3300 (reuseExistingServer).
 *
 * VERIFICATION CAVEAT: the dev bypass forces isAuthenticated=true, so dev CANNOT
 * reproduce the MSAL-init race that caused the prod bounce. These specs prove the
 * sidebar-reachability + active-state half AND that the new msalSettled spinner-gate
 * does not regress the dev render/persist path. The race fix itself is proven by the
 * post-deploy prod re-test (see the use-case file's verification note).
 *
 * Known harness issue (pre-existing, shared by all specs): playwright.config.ts's
 * webServer command (`pnpm dev -- --port 3300`) is misparsed by Next when Playwright
 * has to launch its own server; run against the already-running dev stack instead.
 */
import { test, expect } from "@playwright/test";

const BROKER_ACCOUNTS_PATH = "/settings/broker-accounts";

test.describe("Feature: deeplink-auth-race", () => {
  test("UC-DLA-2: reach Broker Accounts from the sidebar; only that item is active @smoke", async ({
    page,
  }) => {
    await page.goto("/dashboard");

    // The sidebar now exposes a "Broker Accounts" entry.
    const brokerLink = page
      .getByRole("link", { name: "Broker Accounts" })
      .first();
    await expect(brokerLink).toBeVisible();

    // In-app SPA navigation to the page.
    await brokerLink.click();
    await expect(page).toHaveURL(new RegExp(`${BROKER_ACCOUNTS_PATH}$`));
    await expect(
      page.getByRole("heading", { level: 1, name: /broker accounts/i }),
    ).toBeVisible();

    // Longest-prefix active-state: "Broker Accounts" is active (bg-accent),
    // "Settings" is NOT — proving no double-highlight on the shared /settings prefix.
    // Match the standalone `bg-accent` token, NOT the `hover:bg-accent` /
    // `dark:hover:bg-accent/50` variants that every nav item carries (a bare
    // /bg-accent/ substring would match those too and the negative assert would fail).
    const ACTIVE_BG = /(^|\s)bg-accent(\s|$)/;
    await expect(
      page.getByRole("link", { name: "Broker Accounts" }).first(),
    ).toHaveClass(ACTIVE_BG);
    await expect(
      page.getByRole("link", { name: "Settings", exact: true }).first(),
    ).not.toHaveClass(ACTIVE_BG);
  });

  test("UC-DLA-1: deep-link / reload of Broker Accounts lands there, no bounce", async ({
    page,
  }) => {
    // Full-page load straight to the protected route (the bookmark / address-bar case).
    await page.goto(BROKER_ACCOUNTS_PATH);

    // Give any client-side redirect a chance to fire, then assert we did NOT bounce.
    await page.waitForTimeout(3000);
    await expect(page).toHaveURL(new RegExp(`${BROKER_ACCOUNTS_PATH}$`));
    await expect(page).not.toHaveURL(/\/dashboard$/);
    await expect(page).not.toHaveURL(/\/login$/);
    await expect(
      page.getByRole("heading", { level: 1, name: /broker accounts/i }),
    ).toBeVisible();

    // Persistence — a reload (another full load) also stays put.
    await page.reload();
    await page.waitForTimeout(3000);
    await expect(page).toHaveURL(new RegExp(`${BROKER_ACCOUNTS_PATH}$`));
    await expect(
      page.getByRole("heading", { level: 1, name: /broker accounts/i }),
    ).toBeVisible();
  });
});
