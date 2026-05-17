/**
 * Auth fixture — X-API-Key + E2E AppShell bypass (R8 / R13).
 *
 * Background: MSAL `storageState` is documented-broken
 * (microsoft/playwright#17328) — Playwright cannot reliably persist Entra
 * ID browser auth between specs. So for MSAI v2 E2E, we authenticate API
 * calls via the backend's `X-API-Key` header (set in
 * `playwright.config.ts` as `extraHTTPHeaders`), and we bypass the UI's
 * MSAL route guard via `NEXT_PUBLIC_E2E_AUTH_BYPASS=1` (also set in
 * `playwright.config.ts` as `webServer.env`).
 *
 * That means **no explicit login fixture is required** — every spec
 * already has X-API-Key on its requests AND the AppShell skips the
 * /login redirect. This file is kept only to document the contract for
 * future readers and to surface a clear startup error when the required
 * env vars are missing.
 *
 * If a future feature needs *user-specific* authentication (per-user
 * permissions, role-gated UI by a non-viewer role), revisit this file
 * and either (a) seed the user via the backend signup/admin API, or (b)
 * extend the X-API-Key user mapping to choose which user the key
 * represents per spec.
 *
 * Required env vars at Playwright run time:
 *   TEST_API_KEY                  — backend X-API-Key value (matches MSAI_API_KEY)
 *   NEXT_PUBLIC_E2E_AUTH_BYPASS=1 — wired automatically via playwright.config.ts webServer.env
 *
 * Security note: `TEST_API_KEY` is a long-lived shared secret. In CI it
 * should be a dedicated test key with read-mostly permissions — NOT a
 * production key. See `docs/ci-templates/e2e.yml` for the GH secret
 * wiring.
 */
import { test as setup } from "@playwright/test";

setup("verify-e2e-env", async (): Promise<void> => {
  const apiKey = process.env.TEST_API_KEY;
  if (!apiKey) {
    throw new Error(
      "[auth] Missing TEST_API_KEY env var. Playwright cannot authenticate " +
        "API calls without it. Set TEST_API_KEY to the value of MSAI_API_KEY " +
        "from the backend (or your CI's dedicated test key). See " +
        "frontend/tests/e2e/fixtures/auth.ts for the full contract.",
    );
  }

  console.log(
    "[auth] X-API-Key configured via playwright.config.ts extraHTTPHeaders. " +
      "AppShell MSAL bypass active (NEXT_PUBLIC_E2E_AUTH_BYPASS=1).",
  );
});
