/**
 * UC: Operator clicks Run smoke and sees the run start.
 *
 * Auth: X-API-Key via playwright.config.ts extraHTTPHeaders +
 * NEXT_PUBLIC_E2E_AUTH_BYPASS=1 wired in webServer.env. Browser API calls
 * issued from the React component are NOT intercepted by extraHTTPHeaders
 * (that header only applies to APIRequestContext) — the component reads
 * NEXT_PUBLIC_MSAI_API_KEY from the dev env and passes X-API-Key directly
 * via `apiFetch`. See frontend/src/lib/api.ts.
 *
 * Backend wiring (T7) and runner (T4) live behind
 * `POST /api/v1/portfolios/smoke/runs?config=fast`. To keep this spec
 * deterministic + decoupled from real ingest latency (cold-catalog
 * Databento pulls can take 30s+), we intercept the POST via `page.route`
 * and assert the toast contract. The real backend wiring is covered by:
 *   - tests/integration/test_smoke_runner.py (end-to-end runner)
 *   - tests/e2e/use-cases/backtests/smoke-ui.md (verify-e2e Phase 5.4
 *     against the live stack)
 *
 * Pre-flight: stack up at http://localhost:3300 — backend not required
 * because the POST is intercepted at the browser layer.
 */
import { test, expect } from "@playwright/test";

test.describe("Run smoke button @smoke", () => {
  test("operator clicks Run smoke and sees a success toast with the run id", async ({
    page,
  }) => {
    // Arrange: intercept the smoke endpoint with a deterministic 201.
    await page.route("**/api/v1/portfolios/smoke/runs**", async (route) => {
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: "11111111-2222-3333-4444-555555555555",
          portfolio_id: "00000000-0000-0000-0000-000000000000",
          status: "pending",
          metrics: null,
          series: null,
          allocations: null,
          report_path: null,
          start_date: "2024-01-02",
          end_date: "2024-12-31",
          created_at: "2026-05-26T00:00:00Z",
          completed_at: null,
          mode: "quick",
          optimization_trace: null,
          walk_forward_payload: null,
          is_metric: null,
          oos_metric: null,
        }),
      });
    });

    await page.goto("/backtests");

    // Button is in the page header. Stable data-testid per .claude/rules/testing.md.
    const button = page.getByTestId("run-smoke-button");
    await expect(button).toBeVisible();

    await button.click();

    // Sonner toasts are rendered into a region with role="region" + an
    // aria-label of "Notifications alt+T", and each individual toast
    // exposes role="status". The success path renders title "Smoke run
    // started" and description "Run id: <id>".
    await expect(page.getByText(/smoke run started/i)).toBeVisible({
      timeout: 5_000,
    });
    await expect(
      page.getByText(/Run id: 11111111-2222-3333-4444-555555555555/),
    ).toBeVisible({ timeout: 5_000 });
  });

  test("operator sees an error toast when the smoke run fails @smoke", async ({
    page,
  }) => {
    // Arrange: intercept the smoke endpoint with a 422 carrying a detail.
    await page.route("**/api/v1/portfolios/smoke/runs**", async (route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "smoke_runner_failed: ingest mutex contention",
        }),
      });
    });

    await page.goto("/backtests");
    await page.getByTestId("run-smoke-button").click();

    // The describeApiError helper surfaces the backend `detail` string
    // verbatim in the error toast (see frontend/src/lib/api.ts).
    await expect(
      page.getByText(/smoke_runner_failed: ingest mutex contention/),
    ).toBeVisible({ timeout: 5_000 });
  });
});
