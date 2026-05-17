/**
 * TJ-9: Honest settings page — no fakes; role-agnostic per iter-5 Issue B.
 *
 * Selectors recorded during verify-e2e iter-2/iter-3 (see
 * tests/e2e/reports/2026-05-17-ui-completeness-iter2.md "Selectors Observed").
 *
 * Auth: dev API-key user resolves to role=admin. The role badge MUST
 * match auth/me.role verbatim — the assertion checks "no HARDCODED
 * 'Admin' badge that contradicts the backend" rather than "no role
 * badge at all" (per iter-5 Issue B rewrite of TJ-9).
 */
import { test, expect } from "@playwright/test";

test.describe("TJ-9 honest settings page", () => {
  test("renders real profile; no fakes", async ({ page, request }) => {
    // Fetch the authoritative role from the backend so the assertion is
    // role-agnostic (dev API-key user is admin; production viewers would
    // see "viewer" — both valid as long as the UI matches backend).
    // Absolute API URL — Playwright's `request` fixture defaults to the
    // UI baseURL (port 3300) which would hit the Next.js page tree, not
    // the backend. The backend lives on a separate origin (port 8800).
    const apiBase = process.env.MSAI_API_BASE ?? "http://localhost:8800";
    const meRes = await request.get(`${apiBase}/api/v1/auth/me`);
    expect(meRes.ok()).toBe(true);
    const me = await meRes.json();

    await page.goto("/settings");

    // Positive space — profile fields match backend
    await expect(page.getByTestId("profile-display-name")).toHaveValue(
      me.display_name,
    );
    await expect(page.getByTestId("profile-email")).toHaveValue(me.email);
    await expect(page.getByTestId("profile-role")).toHaveText(me.role);

    // Negative space — the 8 fakes removed per audit F-1..F-5 must stay removed
    const body = await page.locator("body").textContent();
    expect(body).not.toContain("Save Preferences");
    expect(body).not.toContain("Trade Execution Alerts");
    expect(body).not.toContain("Strategy Error Alerts");
    expect(body).not.toContain("Daily Summary");
    expect(body).not.toContain("Clear All Data");
    expect(body).not.toContain("System Information");
    expect(body).not.toContain("Demo User");
  });

  test("profile persists across reload", async ({ page, request }) => {
    // Absolute API URL — Playwright's `request` fixture defaults to the
    // UI baseURL (port 3300) which would hit the Next.js page tree, not
    // the backend. The backend lives on a separate origin (port 8800).
    const apiBase = process.env.MSAI_API_BASE ?? "http://localhost:8800";
    const meRes = await request.get(`${apiBase}/api/v1/auth/me`);
    const me = await meRes.json();

    await page.goto("/settings");
    await expect(page.getByTestId("profile-display-name")).toHaveValue(
      me.display_name,
    );

    await page.reload();
    await expect(page.getByTestId("profile-display-name")).toHaveValue(
      me.display_name,
    );
  });
});
