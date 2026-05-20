# Fix Plan: MSAL frontend scope requests Graph token, backend rejects it (PROD OUTAGE)

> **Workflow:** `/fix-bug msal-backend-scope` — compressed Phase 3 path (per Pablo, 2026-05-20).
> **Severity:** P0 — all authenticated endpoints return 401 in production; no one can log in.
> **Branch:** `fix/msal-backend-scope` (worktree at `.worktrees/msal-backend-scope`).
> **Base:** `f7d15c0` (origin/main).

## Goal

Make the frontend request an access token whose `aud` claim matches the backend's expected audience (`JWT_CLIENT_ID = 24a60bec-57f4-4e19-aec0-1b90d75b4c31`) so PyJWT can verify the signature against the Entra ID JWKS for the right tenant + audience.

## Architecture (current vs target)

**Current (broken):**

1. Frontend MSAL: `loginRequest.scopes = ["User.Read", "openid", "profile", "email"]` (msal-config.ts:20).
2. `acquireTokenSilent({...loginRequest, account})` returns an **access token for Microsoft Graph** (audience `00000003-0000-0000-c000-000000000000`).
3. Frontend sends `Authorization: Bearer <graph-token>` to MSAI API.
4. Backend `EntraIDValidator.validate_token` calls `PyJWKClient.get_signing_key_from_jwt(token)` then `jwt.decode(..., audience=settings.jwt_client_id, ...)`. The Graph token's `aud` doesn't match `JWT_CLIENT_ID`, AND/OR Graph tokens are encrypted/opaque (not parseable by third parties at all).
5. Result: `Invalid token: Signature verification failed` → 401 on every `/api/v1/*` request.

**Target (fixed):**

1. Frontend MSAL: `loginRequest.scopes = ["api://24a60bec-57f4-4e19-aec0-1b90d75b4c31/.default"]`.
2. `acquireTokenSilent` returns an **access token for the MSAI backend audience** (audience = `24a60bec...`).
3. Frontend sends that token.
4. Backend validates: `aud` matches `JWT_CLIENT_ID`, signature verifies against the tenant JWKS. Pass.
5. Result: 200 on `/api/v1/*` requests.

## Tech Stack

- `@azure/msal-browser` (frontend)
- `PyJWT` + `PyJWKClient` (backend, `backend/src/msai/core/auth.py`)
- Single Entra ID app registration `24a60bec-57f4-4e19-aec0-1b90d75b4c31` shared by SPA frontend and API backend (confirmed by Pablo 2026-05-20).

## Approach Comparison

| Axis                  | A: `access_as_user`                                                              | **B: `.default` (CHOSEN)**                                                                                                                 |
| --------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Complexity            | Same 1-line change                                                               | Same 1-line change                                                                                                                         |
| Blast radius          | 1 file (msal-config.ts)                                                          | 1 file (msal-config.ts)                                                                                                                    |
| Reversibility         | Trivial (revert + redeploy)                                                      | Trivial (revert + redeploy)                                                                                                                |
| Time to validate      | 1 min code, ~15 min deploy + manual prod browser smoke                           | 1 min code, ~15 min deploy + manual prod browser smoke                                                                                     |
| User/correctness risk | Requires `access_as_user` scope defined in Expose-an-API; AADSTS65005 if missing | Works with any Expose-an-API config (even empty); over-shares if many scopes are later added without thinking through `.default` semantics |
| Operator dependency   | Pablo must verify/add `access_as_user` scope in portal                           | None — works with current app reg state                                                                                                    |

### Chosen Default: B — `.default`

**Why B:** prod is down; Option A has an operator dependency (portal scope must exist) that adds latency to recovery. Option B is symmetric in fix-quality but doesn't gate recovery on a portal config check.

**Trade-off accepted:** if Pablo later adds fine-grained scopes (e.g., `read:portfolio`, `write:portfolio`) under Expose-an-API, the `.default` scope will request consent for ALL of them at sign-in time. Either (a) keep using `.default` and accept the broader consent prompt, or (b) re-tighten to a specific scope literal at that future point. Both are reversible.

## Contrarian Verdict

**SKIPPED per Pablo's explicit choice** (compressed Phase 3, 2026-05-20). The alternative (Option A) was considered explicitly in the comparison table above; Option B chosen on recovery-latency grounds. No council triggered — single dimension of disagreement (operator-step required Y/N), no architectural ambiguity.

## Plan

### Change

**File:** `frontend/src/lib/msal-config.ts`

**Before:**

```typescript
export const loginRequest = {
  scopes: ["User.Read", "openid", "profile", "email"],
};
```

**After:**

```typescript
// Request an access token for the MSAI backend audience, not Microsoft Graph.
// The single Entra ID app registration `NEXT_PUBLIC_AZURE_CLIENT_ID` is shared
// by this SPA AND the FastAPI backend. `.default` resolves to all configured
// scopes under "Expose an API" (currently empty/defaults), producing an access
// token with aud=<client-id> that PyJWT validates against JWT_CLIENT_ID.
//
// Why not `User.Read`: that returns a Microsoft Graph access token (aud=
// 00000003-0000-0000-c000-000000000000) which is opaque to third parties —
// PyJWT can't decode it → "Invalid token: Signature verification failed".
//
// `openid` + `profile` + `email` are OIDC reserved scopes — they govern what
// claims land on the ID TOKEN (name, email shown in the user header).
// MSAL.js automatically strips them before issuing the access-token request,
// so listing them alongside the API scope is the correct shape for both
// loginRedirect and acquireTokenSilent: login gets all 4 → ID token gets
// name/email claims; silent token fetch ignores OIDC scopes and returns
// an access token for the api:// resource only.
export const loginRequest = {
  scopes: [
    "openid",
    "profile",
    "email",
    `api://${process.env.NEXT_PUBLIC_AZURE_CLIENT_ID}/.default`,
  ],
};
```

**Why env-var the client ID:** the bundle already reads `NEXT_PUBLIC_AZURE_CLIENT_ID` at build time (line 5). Hardcoding the literal `24a60bec...` would duplicate the value and drift if the app reg ever changes. The build-time substitution puts the right value in the bundle without rebuild logic.

**Why KEEP `openid`/`profile`/`email`:** Plan review P1 catch — these are OIDC reserved scopes that govern ID-token claims. Without `profile` + `email` in the login request, the cached ID token won't have `name`/`email` claims, breaking the `<header>` user display (which reads `account.name` and `account.username`). MSAL.js strips reserved scopes automatically when issuing the resource-token request via `acquireTokenSilent`, so including them is safe and correct.

### TDD Discipline — DELIBERATE DEVIATION

**Decision: skip the unit test.** Document the rationale.

The frontend has no test framework installed (no `vitest.config.ts`, no `jest.config.ts`; only `@playwright/test`). Adding vitest just for one regression-guard test on a 4-element static array violates the project memory `feedback_drop_vitest_for_one_off_pure_helpers.md`: "Don't add vitest/jest just to unit-test a single small pure helper when Playwright/integration already exercises the user-visible behavior. Revisit at N≥2."

For this fix:

- The change is one literal value swap; type-checking + ESLint catch any syntactically broken code at build time.
- The real verification (does MSAL produce a backend-audience token PyJWT can decode) cannot be unit-tested without mocking MSAL itself — which would test the mock, not the bug.
- The actionable test is a live MSAL sign-in against prod, which is exactly what Phase 5.4 / 6 manual smoke covers.

**Future regression guard:** when UI E2E auth setup lands (PR #49 follow-up), the auth fixture will exercise this path on every CI run — that's where the regression guard belongs, not a fragile array-equality unit test.

**The TDD rule in `critical-rules.md` says "TDD MANDATORY".** This deviation is consciously taken with the rationale above and surfaced for plan review.

### E2E Use Cases

**Surface coverage:** Project type fullstack (UI + API), per `CLAUDE.md ## E2E Configuration`. The bug surface is the **UI login flow** — user signs in via Entra ID, lands at /dashboard, and the page successfully loads data without "Invalid token" errors. CLI: N/A — the CLI uses `X-API-Key` auth, never MSAL.

- **UC-MSAL-UI-001 (regression reproducer, MUST PASS POST-FIX):** Signed-out user signs in via Entra ID, lands on /dashboard, and sees actual data load successfully (no "Invalid token" banners, no 401s in the network panel).
- **UC-MSAL-API-001 (negative test for the broken state, OPTIONAL):** Send a Microsoft Graph token to `/api/v1/strategies/`; expect a 401 with the right error code. Verifies the backend keeps rejecting non-backend tokens (no regression in the other direction).

**Can't run locally (the test gap that allowed this bug to ship):** `isAuthBypassed()` in `auth.ts` returns `true` in dev mode (`NODE_ENV === "development"`), so the real MSAL flow is never exercised against the local dev stack. Verification has to happen in prod after deploy. **This is the known limitation and the reason for the post-mortem follow-up to PR #49's "UI E2E auth setup."**

### Risk Assessment

| Risk                                              | Likelihood | Mitigation                                                                          |
| ------------------------------------------------- | ---------- | ----------------------------------------------------------------------------------- |
| `.default` returns wrong audience for some reason | Low        | Manual prod browser smoke catches it within minutes — revert if so                  |
| Old session tokens cached client-side             | Medium     | Pablo signs out + back in after deploy; storage is `sessionStorage` so closes flush |
| Backend's `JWT_CLIENT_ID` ≠ `24a60bec...` in prod | Low        | If wrong, the post-deploy smoke catches it immediately; same revert path applies    |
| Hardcoded backend URL drift                       | N/A        | Env-var substitution handles this                                                   |
| Test gap masks future regressions                 | High       | Post-mortem follow-up: enable a non-bypass auth path in CI / staging                |

## Out of scope (post-mortem follow-up, NOT this PR)

- Building a non-bypass auth test path in dev / CI / staging — the test gap that allowed this bug to ship is real, but addressing it properly is a separate PR (likely PR #49's "UI E2E auth setup" follow-up).
- Reviewing whether the dashboard error UI ("Invalid token: Signature verification failed") leaks too much detail to logged-in users; the backend currently echoes PyJWT's exception text. Tracking as a deferred polish item.
