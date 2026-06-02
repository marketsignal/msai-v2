# UC-BA-API-2 — Rotating credentials advances to a new pinned version without half-writing

**Interface:** API
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run)

**Actor:** API integrator rotating a compromised login.

**Scenario:** A login was leaked; the integrator rotates credentials via the API. They must
confirm the rotation produced a new pinned version and that a rotation is safe (the row never
ends up pointing at a version that doesn't exist).

**Intent:** The integrator rotates an account's credentials and confirms the row advances to a
new pinned version without exposing the secret.

**Setup:** Create an account via `POST /api/v1/broker-accounts` (sanctioned) to rotate.
(X-API-Key dev path; no DB writes.)

**Steps:**
1. `GET` the account; note `credentials_secret_version` (v1) + `credentials_updated_at`.
2. `POST /api/v1/broker-accounts/{id}/rotate-credentials` with `{tws_userid, tws_password}`.
3. `GET` the account again.

**Verification:** Rotate returns **200**; the follow-up `GET` shows `credentials_secret_version`
**CHANGED** (v2 ≠ v1) and `credentials_updated_at` advanced, with NO secret in the body.
(Half-rotation-on-store-failure safety is covered by unit tests, not this UC.)

**Persistence:** Re-`GET` after a delay — still v2 (the rotation stuck), still no secret.
