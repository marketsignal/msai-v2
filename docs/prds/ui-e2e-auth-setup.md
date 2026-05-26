# PRD: Auth Regression Gate — Static Lint + Synthetic JWT Contract

**Version:** 2.0 (pivoted from v1.0 ROPC design)
**Status:** Draft
**Author:** Claude + Pablo (consultations: Codex twice — initial design, post-Microsoft-docs pivot)
**Created:** 2026-05-25
**Last Updated:** 2026-05-25 (pivot)

---

## 1. Overview

MSAI v2 has two deliberate authentication paths: **OAuth (MSAL → PyJWT)** for browser users, and **`X-API-Key`** for non-human callers (CLI, AI agents, API integrators). On 2026-05-20 a production outage broke every authenticated `/api/v1/*` request for ~6 weeks of undetected drift: the SPA's MSAL config requested `User.Read` (a Microsoft Graph scope), Entra issued tokens with `aud=https://graph.microsoft.com`, and the backend's PyJWT audience check rejected every request. The outage stayed hidden because every existing test layer used the `X-API-Key` path and never exercised the real OAuth flow.

This feature adds three deterministic PR-time gates that **catch the May 20 bug class without any live Entra dependency**:

1. **AST/typed lint of MSAL scope config** — catches the literal `User.Read` (and similar) regression.
2. **Backend auth middleware contract test with synthetic JWT and patched JWKS** — catches every audience/issuer/scp/ver/kid/signature validation regression.
3. **Single-source-of-truth identity contract** (committed `identity-contract.json`) — catches tenant-ID / client-ID drift across the frontend, backend, compose files, and deploy workflow.

> **Design history.** v1.0 of this PRD proposed a Resource Owner Password Credentials (ROPC) live-Entra contract test plus a static lint. During Entra setup we discovered that Microsoft's mandatory-MFA enforcement and Identity Protection's MFA Registration Policy now make the no-MFA-service-user pattern hostile to maintain, AND ROPC is deprecated across every MSAL SDK in 2025 ([MSAL.NET 4.74.0](https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/blob/main/CHANGELOG.md), MSAL Node 3.2.3, MSAL Python 1.35.0, MSAL Java 1.24.0, MSAL Go 1.6.0). A second Codex consultation confirmed: "ROPC is no longer a defensible PR gate. A PR-time ROPC check is now a liability." Static lint + synthetic JWT are a **better** PR gate — deterministic, fast, durable. Live integration with Entra moves to the deferred post-deploy probe queued separately in `state.md`. Full pivot rationale: see `docs/prds/ui-e2e-auth-setup-discussion.md` Q7.

## 2. Goals & Success Metrics

### Goals

- **Primary:** Block any PR to `main` that reintroduces the May-20-class regression (wrong MSAL scope, backend audience misconfig, env-var drift across layers).
- **Secondary:** Run in <5 seconds total at PR time, with no network dependency, so the gate cannot be flaked by external service degradation.
- **Tertiary:** Establish `identity-contract.json` as the project's single source of truth for Entra identity wiring, eliminating future cross-file drift bugs.

### Success Metrics

| Metric                                          | Target                                                                                                               | How Measured                                                      |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Gate runs on every PR to `main`                 | 100%                                                                                                                 | GitHub Actions workflow report                                    |
| Gate catches deliberate May-20-style regression | PR includes negative-control commit showing Gate 1 fails on `User.Read` reintroduction, then revert shows it passing | Reviewer reads PR commit log + the failing-then-passing CI runs   |
| Gate wall-clock time                            | ≤ 5 seconds                                                                                                          | GH Actions run duration                                           |
| Gate runs entirely without network              | No outbound calls during gate execution                                                                              | Network-mock assertion in test setup; OR offline-run verification |
| Backend middleware contract negatives coverage  | At least 9 distinct negative cases pass (see US-002)                                                                 | Test enumeration in `tests/integration/test_auth_contract.py`     |
| Cross-file identity-config consistency          | 100% (no file references a stale tenant/client ID)                                                                   | Gate 3 lint passes on every PR                                    |

### Non-Goals (Explicitly Out of Scope)

- ❌ Any live-Entra dependency at PR time (ROPC, headless Playwright, WIF + Graph). Dropped per the pivot — the deferred post-deploy probe handles operational drift.
- ❌ Dedicated Entra test user, Conditional Access exclusions, app-reg toggles, or related runbook.
- ❌ Removing, hardening, or restricting the `X-API-Key` path (deliberate dual-auth product design).
- ❌ Post-deploy Bearer probe in `deploy.yml` (queued as separate ~30 min PR in `state.md ## Next`).
- ❌ WIF-backed live Entra metadata check via Graph API (Codex flagged this as a useful future addition for config-shape validation, but NOT for delegated-flow testing — deferred to a future PR).
- ❌ End-to-end ingest→backtest smoke test (separate ~2-3h PR, queued in `state.md ## Next`).
- ❌ Frontend or backend code changes BEYOND what's needed to enable the gates (no MSAL refactor, no PyJWT refactor — only what Gates 1/2/3 need to inspect cleanly).
- ❌ Migrating to a different OAuth flow or different auth provider (this PRD is about adding regression coverage, not changing the auth design).

## 3. User Personas

### MSAI Engineer (Pablo, primary)

- **Role:** Sole engineer / operator of MSAI v2. Opens PRs, reviews CI output, fixes auth regressions when the gates fire.
- **Permissions:** Owns repo, GH Actions, Entra tenant (but doesn't need to touch Entra for this PR).
- **Goals:** Never ship another May-20-class outage. Get fast, unambiguous CI feedback when something in the auth chain drifts. Avoid operational burden from new test infrastructure.

### Future Contributor / Reviewer

- **Role:** Anyone reading a future PR that the gates flagged, including Pablo months later when memory has decayed.
- **Permissions:** PR author / reviewer.
- **Goals:** Understand from the gate's failure output **what layer broke** without deep Entra/MSAL/PyJWT expertise. Output should name the file and the wrong value.

### AI Agent (out-of-gate-scope, but relevant to framing)

- **Role:** External AI/CLI caller of `/api/v1/*` endpoints using `X-API-Key`.
- **Permissions:** Whatever the API key grants.
- **Goals:** Not blocked by the human auth path. **Explicitly not covered by this gate** — the gate exists for the OAuth path that AI agents bypass by design.

## 4. User Stories

---

### US-001: MSAL scope-config regression is caught at PR time

**As an** MSAI engineer
**I want** every PR to `main` to be blocked if the MSAL scope config reintroduces a Microsoft Graph scope (or any non-API scope) anywhere it gets sent to Entra
**So that** the May 20 outage class cannot ship undetected.

**Scenario:**

```gherkin
Given a PR is opened against main
And the PR modifies frontend/src/lib/msal-config.ts (or similar)
And the modification reintroduces a scope containing "User.Read" or "graph.microsoft.com"
  OR sets a scope that does not match the pattern api://<client-id>/*
When GitHub Actions runs Gate 1 (MSAL scope lint)
Then the lint inspects exported scope arrays, protectedResourceMap entries, and MSAL request objects
And the lint identifies the offending file, line, and scope literal
And the gate exits non-zero with a message naming the file:line and the forbidden value
And the PR's required-check status is RED
```

**Acceptance Criteria:**

- [ ] Gate 1 is implemented as **AST/typed inspection**, not regex grep, so it can't be fooled by e.g. comments, string concatenation, or template literals.
- [ ] Gate 1 inspects: exported scope arrays in `frontend/src/lib/msal-config.ts`, `protectedResourceMap` entries (if any), and the `scopes` arrays on all `RedirectRequest` / `SilentRequest` / `PopupRequest` / `acquireTokenSilent` / `acquireTokenPopup` / `acquireTokenRedirect` call sites.
- [ ] Gate rejects: `User.Read`, `https://graph.microsoft.com/*`, mixed-resource scope arrays (scopes mixing Graph and the API in one request), any fallback/default scope that isn't the API scope, AND any literal that doesn't match the pattern `api://<expected-client-id>/<scope-name>` or `<expected-client-id>/<scope-name>`.
- [ ] Output format: `<file>:<line>: forbidden scope '<value>' in <symbol>. Expected: api://<client-id>/access_as_user`
- [ ] Gate exits 0 (success) ONLY if every inspected scope reference matches the allow-list pattern.

**Edge Cases:**

| Condition                                                                       | Expected Behavior                                                                                                                                                                      |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scope built from dynamic string concatenation (`api://${id}/...`)               | Lint resolves the template against `identity-contract.json` and validates the result. If unresolvable, fail closed with `cannot statically resolve scope expression at <file>:<line>`. |
| Scope array contains BOTH the API scope AND an unrelated scope (e.g., `openid`) | `openid`, `profile`, `email`, `offline_access` are allowed as STANDARD OAuth scopes. Any OTHER non-API scope (especially anything containing "graph") is rejected.                     |
| File has no MSAL imports (lint shouldn't apply)                                 | Gate skips file silently.                                                                                                                                                              |
| New MSAL call site added in a previously-clean file                             | Lint detects new call site and validates its scopes like any other.                                                                                                                    |
| Frontend codebase grows new MSAL.js wrapper not in `lib/msal-config.ts`         | Lint discovers via the import graph (`@azure/msal-browser`, `@azure/msal-node`) — not by hardcoded file list.                                                                          |

**Priority:** Must Have

---

### US-002: Backend auth middleware regressions are caught at PR time

**As an** MSAI engineer
**I want** every PR to `main` to be blocked if the backend's PyJWT validation chain accepts a malformed token OR rejects a valid one
**So that** silent audience/issuer/scp validation drift cannot ship.

**Scenario:**

```gherkin
Given a PR is opened against main
And the PR modifies backend/src/msai/core/auth.py (or its callers)
And the modification breaks one of the negative cases (e.g., now accepts a Graph-audience token)
When GitHub Actions runs Gate 2 (auth middleware contract test)
Then the test sets up a local JWKS with a test public key
And the test patches the backend's JWKS-fetch to point at the test JWKS
And the test mints a synthetic JWT signed with the corresponding test private key
And the test calls the real backend auth middleware (not a helper function in isolation)
And the test asserts the middleware accepts the well-formed positive token
And the test asserts the middleware rejects each negative-case token with the right exception class
And any drift in this contract fails the PR
```

**Acceptance Criteria:**

- [ ] Gate 2 hits the **real backend auth middleware** (the function or dependency PyJWT runs in `/api/v1/*` request handling). Not a separate helper function. If the middleware is currently entangled with FastAPI's request lifecycle, the test uses `ASGITransport` + httpx to drive the full request path.
- [ ] Gate 2 patches the JWKS endpoint to return the test public key (no network call to Microsoft).
- [ ] **Positive case (must accept):**
  - Token with `aud=<expected-client-id>`, `iss=https://login.microsoftonline.com/<expected-tenant-id>/v2.0`, `ver=2.0`, `scp` includes `access_as_user`, valid expiry, signed by the test JWKS private key, correct `kid` header.
- [ ] **Negative cases (each must reject — at least 9 distinct cases):**
  1. Graph audience (`aud=https://graph.microsoft.com`) — the literal May 20 bug
  2. Wrong tenant in `iss`
  3. Missing `scp` claim entirely (would represent an app-only token leaking into the user path)
  4. `scp=User.Read` (right shape, wrong content)
  5. Expired token (`exp` in the past)
  6. Bad `kid` (JWKS lookup miss)
  7. Wrong signature (signed with a different key)
  8. App-only token with `roles` claim but no `scp` claim
  9. v1.0 issuer (`https://sts.windows.net/<tenant>/`) when backend expects v2.0
- [ ] Each negative case asserts the SPECIFIC exception or HTTP status the middleware returns (not just "raised any exception"). Output names the failing claim and the actual-vs-expected values.
- [ ] No network calls during the gate run. Gate verifies this by patching `urllib`/`httpx`/whatever the JWKS fetch uses to raise if called outside the test fixture.

**Edge Cases:**

| Condition                                                             | Expected Behavior                                                                                                                   |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Backend code adds a new claim requirement (e.g., `groups`)            | Test fails until the contract test is updated. This is a feature — forces explicit acknowledgment of the contract change in the PR. |
| Backend code removes a claim requirement (e.g., stops checking `scp`) | Negative case for "missing scp" now passes when it shouldn't. Test fails.                                                           |
| JWKS rotation logic changes                                           | The patched JWKS-fetch tests verify the rotation interface still works; specific rotation behavior is a separate concern.           |

**Priority:** Must Have

---

### US-003: Single source of truth for Entra identity config — no cross-file drift

**As an** MSAI engineer
**I want** the tenant ID, client ID, app ID URI, issuer, and scope name to all live in **one committed file** (`identity-contract.json`), and to be enforced consistent across every place they appear in the codebase
**So that** a future refactor (multi-account fleet, separate test tenant, app-reg rotation) cannot leave stale values in one of seven different files.

**Scenario:**

```gherkin
Given a PR is opened against main
And the PR changes the tenant ID in frontend/.env.example but not in backend/.env.example
When GitHub Actions runs Gate 3 (identity contract lint)
Then the lint reads the canonical identity-contract.json
And the lint scans every other config file: frontend/.env.example, backend/.env.example,
    docker-compose.dev.yml, docker-compose.prod.yml, frontend/src/lib/msal-config.ts,
    backend/src/msai/core/config.py (or wherever AZURE_TENANT_ID/CLIENT_ID is read),
    .github/workflows/deploy.yml, and any other file that hardcodes these values
And the lint identifies any file whose value diverges from identity-contract.json
And the gate exits non-zero naming the divergent file and the actual vs expected values
```

**Acceptance Criteria:**

- [ ] A non-secret `identity-contract.json` is committed at the repo root (or `config/identity-contract.json` — wherever the design phase decides).
- [ ] Contract fields: `tenant_id`, `client_id`, `app_id_uri` (e.g. `api://<client-id>`), `issuer` (e.g. `https://login.microsoftonline.com/<tenant-id>/v2.0`), `scope_name` (e.g. `access_as_user`), `token_version` (e.g. `2.0`).
- [ ] Gate 3 enforces consistency across all files listed in the Scenario. The list of files is **automatically discovered** by scanning for the canonical env-var names (e.g., `AZURE_TENANT_ID`, `NEXT_PUBLIC_AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `NEXT_PUBLIC_AZURE_CLIENT_ID`) — not a static hardcoded file list, so new files added in the future are automatically included.
- [ ] **`identity-contract.json` contains no secrets.** Tenant ID and client ID are not secrets (they're public-class identifiers). Passwords, secrets, JWT signing keys, and the like NEVER go in this file.
- [ ] `.env` and `.env.local` files (which DO contain real values) are gitignored — the contract enforcement applies only to `.env.example` files and committed configuration.

**Edge Cases:**

| Condition                                                                  | Expected Behavior                                                                                                                                                            |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A new config file is added with `AZURE_TENANT_ID` referenced               | Lint discovers it on the next run and enforces consistency.                                                                                                                  |
| `identity-contract.json` itself is modified                                | The lint runs against the NEW values from the contract — so a coordinated update across all files passes. A drifted update (only contract updated, not the env files) fails. |
| Frontend uses `NEXT_PUBLIC_*` prefix variants                              | Lint understands the prefix mapping (`NEXT_PUBLIC_AZURE_TENANT_ID` is the same identity as `AZURE_TENANT_ID`).                                                               |
| Compose file uses `${AZURE_TENANT_ID}` interpolation rather than a literal | Lint treats interpolated env vars as "passthrough" (verifies the variable name is the canonical one, doesn't fail on the absence of a literal value).                        |

**Priority:** Must Have

---

### US-004: Negative-control commit demonstrates Gate 1 catches the May 20 regression

**As an** MSAI engineer
**I want** this PR's commit history to contain durable proof that Gate 1 fails when MSAL scope is reverted to `User.Read`
**So that** future reviewers (including me) trust the gate isn't just a passing tautology.

**Scenario:**

```gherkin
Given this feature PR
When a reviewer scrolls the commit log
Then they see a commit sequence:
  1. "feat(auth-gate): add MSAL scope lint + middleware contract + identity contract"
     (Gate 1 passes, Gate 2 passes, Gate 3 passes)
  2. "demo: revert MSAL scope to User.Read — gate must fail"
     (Gate 1 fails with the structured "forbidden scope 'User.Read'" diagnostic)
  3. "revert: restore correct MSAL scope"
     (Gates pass again)
And the PR body links to the failing GH Actions run from step 2 as durable evidence
And the PR description explains the negative-control pattern
```

**Acceptance Criteria:**

- [ ] PR includes the negative-control commit + revert pair before merge.
- [ ] PR body links the failing run from step 2 as a GH Actions permalink.
- [ ] The negative-control commit message explains intent so future readers don't think it's a real regression.

**Priority:** Must Have

---

## 5. Constraints & Policies

> Outcome-level. Hard limits the product must respect.

### Business / Compliance Constraints

- **Solo-operator platform.** No ongoing operational burden may be introduced by this PR — no test user to manage, no Entra config to maintain, no quarterly rotation.
- **Hedge-fund operational rigor.** Per `state.md` memory `feedback_dont_optimize_for_cost.md`: spend on rigor, not on cost optimization. Acceptable to add CI seconds and code complexity if it eliminates an outage class.

### Platform / Operational Constraints

- **Must run on GitHub Actions hosted runners** with no Azure/Entra/Key Vault dependency.
- **Must complete in ≤ 5 seconds total wall-clock** (PR feedback latency).
- **Must work alongside existing CI** (does not replace existing pytest / lint / type-check jobs; can run as a sibling job).
- **Zero network calls during gate execution.** All gates run offline.
- **Tests must not depend on environment-specific values** — `identity-contract.json` is the only acceptable source for tenant/client IDs in tests; tests must NOT pull from `.env`.

### Dependencies & Required Integrations

- **Requires:** Nothing external. Everything is in-repo.
- **Blocked by:** Nothing. Can ship immediately.
- **Named integrations (scope, not mechanism):**
  - Must speak the JWT validation contract that Entra's token shape requires (audience, issuer, expiry, signature, version, scope claim).
  - Must understand MSAL.js call-site syntax to lint scope arguments (AST-level).

## 6. Security Outcomes Required

> WHAT must be protected.

- **Who can access what:** No new identities or grants. The `X-API-Key` and OAuth paths are unchanged.
- **What must never leak:**
  - `identity-contract.json` contains ONLY non-secret identifiers (tenant ID, client ID, app ID URI, issuer URL, scope name). NO passwords, NO signing keys, NO refresh tokens, NO secrets.
  - JWT private keys used in Gate 2's contract test live in the test fixture and are NEVER reused outside the test (generated fresh per test run or pinned as a known-test-only key).
- **What must be auditable:**
  - Each gate's failure output must point at the specific file/line/value that caused the failure. No black-box errors.
- **What legal/regulatory outcomes apply:**
  - None — no PII, no PHI, no real user data.
- **Required auth capabilities the gates must validate (interoperability scope):**
  - OAuth 2.0 v2.0 issuer format (`https://login.microsoftonline.com/<tenant>/v2.0`).
  - JWT validation per RFC 7519 (audience, issuer, expiry, signature, `kid` header).
  - Delegated-token shape (`scp` claim, NOT `roles`) for the human auth path.

## 7. Open Questions

> Design-phase open questions for `/new-feature` Phase 2/3.

- [ ] **Implementation language for Gate 1's AST lint.** TypeScript itself (via TS Compiler API or `ts-morph`), ESLint custom rule, or a Python AST walker that parses TS via tree-sitter? Likely answer: ts-morph (or a custom ESLint rule) keeps the lint co-located with the frontend toolchain.
- [ ] **Backend auth middleware test scaffolding.** Where exactly in `backend/src/msai/core/auth.py` does the test attach? Via FastAPI's `ASGITransport` + `httpx.AsyncClient`? Or via direct dependency-resolution call? Phase 2 reads the existing auth middleware code to decide.
- [ ] **JWKS patching mechanism.** Monkeypatch `urllib.request`/`httpx.AsyncClient.get`? Use a fixture that injects a JWKS provider into the auth chain? Existing codebase patterns govern.
- [ ] **`identity-contract.json` location.** Repo root, `config/`, `backend/config/`, or `.claude/`? Phase 3 decides.
- [ ] **Should `identity-contract.json` ALSO be consumed at runtime** (so the frontend and backend literally read it instead of duplicating values in env)? Or is it only a lint-time source of truth? Considerable design implications either way.
- [ ] **Token-version (`ver`) handling.** Confirm backend currently expects v2.0 only. If v1.0 was ever accepted (some Entra apps emit v1.0 for backward-compat), the contract test needs to reflect that.
- [ ] **Cross-platform compatibility.** GH Actions runs on Linux; Pablo develops on macOS. Gates must work on both.

## 8. References

- **Discussion Log:** `docs/prds/ui-e2e-auth-setup-discussion.md` (Q1-Q6 + the Q7 pivot)
- **May 20 outage context:**
  - Memory: `~/.claude/projects/-Users-pablomarin-Code-msai-v2/memory/feedback_msal_scope_must_target_backend_audience.md`
  - PRs #74 (initial fix) + #75 (`.default` → `access_as_user` correction)
- **Microsoft Learn docs (for the pivot rationale):**
  - [Plan for mandatory Microsoft Entra MFA](https://learn.microsoft.com/en-us/entra/identity/authentication/concept-mandatory-multifactor-authentication) — confirms custom-app endpoints are out of scope BUT ROPC is incompatible with MFA-enabled tenants
  - [OAuth 2.0 ROPC reference](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth-ropc) — official "don't use this" guidance
  - [Microsoft identity claims validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation) — informs Gate 2's negative-case enumeration
- **Codex consultations:** Inline in conversation 2026-05-25 (initial ROPC recommendation → second consultation post-Microsoft-docs review confirms pivot).
- **Related deferred work (in `state.md ## Next`):**
  - `deploy.yml` post-deploy Bearer probe (~30 min) — Codex flagged that if it uses an exempt CI user it won't catch CA blocks; needs careful design.
  - End-to-end ingest→backtest smoke test (~2-3h)
  - WIF-backed live Entra metadata check (NEW — future PR, Codex's suggestion).

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------- | ---------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1.0     | 2026-05-25 | Claude + Pablo | Initial PRD. Design: ROPC delegated-token gate + scope-config static lint + test user + CA exclusion + runbook. Codex-validated at the time.                                                                                                                                                                                                                                                                                               |
| 2.0     | 2026-05-25 | Claude + Pablo | **Pivot.** Dropped ROPC entirely after Microsoft Learn review surfaced ROPC deprecation across all MSAL SDKs and active hostility from Entra's MFA enforcement layers. New design: 3 deterministic offline gates (AST lint of MSAL scope, synthetic-JWT middleware contract, identity-contract.json single source of truth). Codex re-validated. Live Entra dependency dropped from PR-time path; deferred to scheduled post-deploy probe. |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design (`/new-feature` Phase 2 research)
