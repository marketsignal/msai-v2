# UC-BA-UI-1 — Operator adds a broker account through the Settings wizard and it survives reload

**Interface:** UI
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run)

**Actor:** Signed-in operator on the Broker Accounts settings page.

**Scenario:** They just got a new IB login and want to add it through the UI wizard, confirm it
appears in the fleet list, and trust it's still there tomorrow — and never see the password
echoed back.

**Intent:** The operator completes the add-account wizard and sees the new account in the list
with credential metadata only, surviving a reload.

**Setup:** Authenticate via the documented dev path (the frontend's `NEXT_PUBLIC_E2E_AUTH_BYPASS`
fixture + X-API-Key; see `frontend/tests/e2e/fixtures/auth.ts`) at origin `http://localhost:3300`
(the only CORS-allowed dev origin). Navigate to `/settings/broker-accounts`. Do NOT pre-create.

**Steps:**

1. Click "Add account" (`getByTestId('broker-accounts-add')`).
2. Wizard step 1 — enter `broker-account-ib-account-id`, `broker-account-ib-login-key`, select `broker-account-trading-mode` (default Paper) → `broker-account-wizard-next`.
3. Wizard step 2 — enter `broker-account-tws-userid` + `broker-account-tws-password` (masked `type="password"`) → next.
4. Step 3 review → Create (`getByRole('button', {name:/create/i})`).

**Verification:** The new row appears in `getByTestId('broker-accounts-table')`
(`[data-testid^="broker-account-row-"]`) with status + slot; opening the detail sheet shows
credential METADATA only (secret ref + pinned version + updated-at) — the password is masked/
absent, never cleartext. (A success toast also fires; the durable row + reload is the
load-bearing assertion.)

**Persistence:** Reload `/settings/broker-accounts` — the new account is still in the table; the
detail still shows metadata only. (API cross-check confirms persistence with no `tws_*` fields.)

**Observed selectors** (for the Playwright spec): table `broker-accounts-table`; add `broker-accounts-add`; wizard `broker-account-wizard`; identity inputs `broker-account-ib-account-id`/`-ib-login-key`/`-trading-mode`/`-gateway-slot`; next `broker-account-wizard-next`; credential inputs `broker-account-tws-userid`/`-tws-password`; create button by role `/create/i`; rows `[data-testid^="broker-account-row-"]`.
