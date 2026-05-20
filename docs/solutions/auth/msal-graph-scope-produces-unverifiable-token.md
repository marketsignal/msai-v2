# MSAL frontend requests Graph scope; backend (PyJWT) rejects token

## Problem

Every authenticated `/api/v1/*` call in production returns 401 with `Invalid token: Signature verification failed`. The frontend MSAL sign-in succeeds (user lands on /dashboard with name + email shown), but every page that fetches data renders error banners. Network panel shows all requests carry an `Authorization: Bearer eyJ...` header — yet the backend rejects them.

## Root Cause

`frontend/src/lib/msal-config.ts` defined:

```typescript
export const loginRequest = {
  scopes: ["User.Read", "openid", "profile", "email"],
};
```

`User.Read` is a **Microsoft Graph** scope. When `acquireTokenSilent({...loginRequest, account})` runs (in `frontend/src/lib/auth.ts:getToken()`), MSAL returns an access token for the **Microsoft Graph audience** (`00000003-0000-0000-c000-000000000000`), signed with Microsoft's keys, **opaque/encrypted to third parties**.

The backend (`backend/src/msai/core/auth.py:EntraIDValidator.validate_token`) calls `PyJWKClient.get_signing_key_from_jwt(token)` to look up a signing key, then `jwt.decode(..., audience=settings.jwt_client_id, ...)`. Either the JWKS lookup fails (Graph tokens use Microsoft-internal keys, not your tenant's keys) or the `aud` claim doesn't match `JWT_CLIENT_ID`. Either way, PyJWT raises `InvalidSignatureError`, which the auth dependency renders as `Invalid token: Signature verification failed` with HTTP 401.

## Why Tests Didn't Catch It

Every test layer has an auth bypass:

1. **Frontend dev bypass** (`frontend/src/lib/auth.ts:36-42`): `isAuthBypassed()` returns true when `NODE_ENV === "development"`. In dev (`localhost:3300` and Playwright `webServer.env`), `getToken()` is never called — the broken `User.Read` scope is never exercised.
2. **Backend dev bypass**: All unit/integration/E2E tests use `X-API-Key: msai-dev-key`. The MSAL → PyJWT token path is never exercised in any backend test.
3. **verify-e2e agent**: Uses the API key for UI ARRANGE and VERIFY.
4. **Deploy.yml post-deploy probes**: `GET /health` (unauthenticated) + `GET /api/v1/live/status` with the API key. Never probes the MSAL path.

Result: the bug was latent from the day MSAL was first wired into the frontend, surfacing only when a real user signed in fresh in prod with a real MSAL token (caught 2026-05-20 by a manual UI smoke run on `https://platform.marketsignal.ai` after PR #73's deploy).

## Solution

Change `frontend/src/lib/msal-config.ts` to request a token for the backend audience:

```typescript
export const loginRequest = {
  scopes: [
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || ""}/.default`,
  ],
};
```

Key points:

- `api://<client-id>/.default` requests an access token whose `aud` claim is `<client-id>`, signed by your tenant's keys. PyJWT can decode and verify it.
- `openid` / `profile` / `email` are **OIDC reserved scopes**. MSAL.js automatically strips them when issuing the resource-token request (`acquireTokenSilent`), but they govern what claims land on the **ID token** (`name`, `email`) which the user header displays. Without `profile` + `email`, `account.name` / `account.username` would be undefined post-fix.
- `User.Read` is **removed** — the previous Graph scope is the bug.
- The env-var template substitutes the client ID at build time. `|| ""` matches the existing pattern at line 5.

Single-app-reg setup (this project's case): the SPA frontend and the FastAPI backend share **one** Entra ID app registration. Backend's `JWT_CLIENT_ID` = SPA's `NEXT_PUBLIC_AZURE_CLIENT_ID`. The `.default` scope works against this shared app reg with no portal changes — it returns whatever scopes are configured under "Expose an API" (empty/default is fine).

Two-app-reg setup (NOT this project): backend has its own app reg; SPA would need `api://<backend-app-reg-id>/.default` and the SPA app reg's "API permissions" would need the backend's API added.

## Prevention

The fundamental gap is "no test exercises the real MSAL → backend token path." Three layered fixes:

1. **Static guard (cheapest):** add a build-time lint rule or CI check that fails if `loginRequest.scopes` contains a Microsoft Graph well-known scope (`User.Read`, `Mail.Read`, etc.) without an `api://` scope alongside.
2. **Integration guard (medium effort):** in CI / a non-dev staging build, run a Playwright spec that signs in via a test Entra ID user (test-tenant service principal) and asserts the dashboard data loads without 401s. Requires the **UI E2E auth setup** deferred from PR #49 — that's exactly the test that catches this bug.
3. **Post-deploy guard (cheap, high-leverage):** extend `.github/workflows/deploy.yml`'s public probes to include a **token-validity check**. Currently the probes use `X-API-Key`; add one probe that signs in as a test user (or uses an Entra ID client-credentials token for a service principal), then calls `GET /api/v1/auth/me` (or any cheap authenticated endpoint) with the Bearer token. A 401 here = roll back.

The bug was latent for ~6+ weeks. The cost of each fix above is small relative to the cost of "prod inaccessible to all users." Implement (1) and (3) immediately; (2) when PR #49's UI E2E auth setup lands.

## References

- PR fixing this: [link added post-merge]
- Bug-discovery session: 2026-05-20 prod smoke after PR #73 portfolio-backtest deploy
- Test-gap memory: `feedback_pre_pr_review_doesnt_substitute_github_codex_bot.md` (broader lesson: pre-merge gates don't substitute for prod-like verification)
- MSAL.js docs on `.default` scope: https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc#the-default-scope
- PyJWT signature-failure mode: `jwt.exceptions.InvalidSignatureError` — raised when JWKS-fetched key doesn't validate the token's RS256 signature; the most common cause is audience mismatch (token signed for resource A, verifier looks up keys for resource B).
