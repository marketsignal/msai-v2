# UC-BA-API-1 — Integrator provisions a broker account and confirms it without ever seeing the secret

**Interface:** API
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run)

**Actor:** API integrator scripting fleet setup against the broker-accounts service.

**Scenario:** They are onboarding a new IB account for the fleet and need to register it
programmatically, then confirm it persisted so the next provisioning step can bind a
portfolio to it — and they must be sure the login secret never comes back in any response.

**Intent:** The integrator registers a broker account on behalf of the operator and
retrieves it back from the account list, confirming credentials are stored but never readable.

**Setup:** Obtain the dev API key via the documented X-API-Key path
(`docker exec msai-claude-backend printenv MSAI_API_KEY`). Do NOT pre-create the account.
(No raw DB writes / no token forging.)

**Steps:**

1. `POST /api/v1/broker-accounts` with `{ib_account_id:"DU<digits>", ib_login_key, trading_mode:"paper", tws_userid, tws_password}` + `X-API-Key`.
2. `GET` the `Location` header URL.
3. `GET /api/v1/broker-accounts` (list).

**Verification:** Receives **201 + a `Location` header**; following the link returns the
new account with the same `ib_account_id`, `status` active, an auto-allocated `gateway_slot`,
and credential METADATA only (`credentials_secret_ref`, `credentials_secret_version`,
`credentials_updated_at/by`, `credentials_last_accessed`). The list response is a **bare JSON
array** that includes the new account. **No `tws_userid`/`tws_password`/cleartext appears in
ANY response body.**

**Persistence:** Re-`GET` the account after a short delay — still present with the same id +
metadata, still no secret.

**Note:** account-prefix rule enforced at the API — paper accounts must be `DU`/`DF`-prefixed,
live accounts `U`-prefixed (mismatch → 422).
