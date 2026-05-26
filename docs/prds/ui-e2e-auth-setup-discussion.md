# PRD Discussion: UI E2E Auth Setup (non-bypass MSAL → PyJWT gate)

**Status:** In Progress
**Started:** 2026-05-25
**Participants:** Pablo, Claude, Codex (consulted for design steer)

## Original User Stories (inferred from session context + May 20 outage post-mortem)

- **US-DRAFT-1:** As an MSAI engineer, I want every PR to `main` to be blocked if it breaks the real MSAL → PyJWT auth path, so that prod outages like 2026-05-20 cannot ship undetected.
- **US-DRAFT-2:** As an MSAI engineer, when the auth gate fails I want a clear diagnostic of which layer broke (scope config, audience, JWKS, tenant), so I can fix the regression fast without ripping into Entra debugging from scratch.
- **US-DRAFT-3:** As the operator of this hedge-fund platform, I want the gate to use realistic auth (not mocks, not the dev-only `X-API-Key` bypass), so it cannot be defeated by silent drift in test scaffolding.

## Settled context (no need to re-ask)

These were locked in earlier in the session and via Codex consultation:

- **Goal:** PR gate, runs on every PR to `main`, blocks merge on auth regression.
- **Where it runs:** GitHub Actions, against an ephemeral `docker-compose.dev.yml` stack spun up in CI.
- **Test identity strategy:** ROPC (Resource Owner Password Credentials) delegated-token contract test + static scope-config lint. Full-browser Playwright deferred to nightly canary or out-of-scope for this PR.
- **Tenant strategy:** Production Entra tenant + narrow Conditional Access exclusion for a single test user. (Deferred: dedicated test tenant — bigger Entra ops scope.)
- **Companion follow-ups (out of scope for this PR):**
  - `deploy.yml` Bearer-token public probe (~30 min separate PR)
  - End-to-end ingest→backtest smoke test (~2–3h separate PR)

## Discussion Log

### Q1 — Coverage (what auth behaviors does the gate exercise?)

**A:** All four:

- Login → `/auth/me` succeeds with delegated token (the May 20 class)
- Token refresh (`acquireTokenSilent`) → backend still accepts
- Missing-token 401 (negative path — proves auth dependency is wired)
- Bad-audience token 401 (negative path — proves rejection logic itself works)

### Q2 — Failure UX

**A:** Structured layered diagnostic. When the gate fails, output classifies the failure layer: `ENTRA_TOKEN_NOT_ISSUED` / `BACKEND_REJECTED_TOKEN` (with JWT claim mismatch detail) / `BACKEND_5XX` / `NETWORK` / `CONFIG_LINT_FAIL`. No re-reading logs to figure out which layer broke.

### Q3 — X-API-Key bypass

**A:** Keep it untouched. Important framing correction:

- OAuth (MSAL → PyJWT) is the **human auth path** (browser UI).
- `X-API-Key` is the **non-human auth path** (CLI, AI agents, external API integrators).
- Both are first-class, not a "bypass" of each other.
- May 20 outage was specifically the human path breaking — AI agents kept working through API-key path.

**Implication for the gate:** the ROPC contract test must explicitly **assert it's exercising the human path** — Bearer token sent, `X-API-Key` header NOT sent — so a future drift where a test accidentally falls back to the API-key path can't silently mask a Bearer-path regression.

### Q4 — Test user operational ownership

**A:** Manual ownership by Pablo. Create the test user once, configure narrow CA exclusion once, manual quarterly password rotation. No Key Vault automation, no scheduled rotation job. Acceptable risk for single-developer hedge-fund platform. Runbook documents the procedure.

### Q5 — Acceptance proof (does the gate actually catch May 20?)

**A:** Negative-control commit on this branch. The PR will include — as durable history, not just docs — a commit sequence:

1. Add the gate (passes).
2. Deliberately revert MSAL scope to `User.Read` → gate FAILS with `BACKEND_REJECTED_TOKEN: ENTRA_AUDIENCE_MISMATCH` diagnostic. Screenshot/log in PR body.
3. Revert step 2 (gate passes again).

When Pablo or any reviewer reads the PR, they see live proof the gate catches the regression class.

### Q6 — Auth-dependency coverage

**A:** `/auth/me` + one other authenticated endpoint (e.g., `GET /api/v1/strategies/` or `GET /api/v1/live/status`). Catches the "new router shipped without auth dependency" regression class as a bonus, marginal cost.

---

## Refined Understanding

### Personas

- **MSAI engineer (Pablo)** — opens PRs, reviews CI output, fixes auth regressions when the gate fires.
- **Future contributor / fork PR author** — needs to understand gate output without deep Entra knowledge (structured diagnostic serves this).
- **AI agent (out-of-scope for gate, but tied to product framing)** — uses `X-API-Key`, never touches the MSAL flow. The gate explicitly does NOT cover this path; it covers the human path.

### User Stories (Refined)

- **US-1:** As an MSAI engineer, I want every PR to `main` to be blocked if it breaks the human (MSAL → PyJWT) auth path, so prod outages like 2026-05-20 cannot ship undetected.
- **US-2:** As an MSAI engineer, when the gate fails I want a **layered diagnostic** (`ENTRA_TOKEN_NOT_ISSUED` / `BACKEND_REJECTED_TOKEN` / `BACKEND_5XX` / `NETWORK` / `CONFIG_LINT_FAIL`) so I can identify the broken layer without grepping Entra/backend logs.
- **US-3:** As an MSAI engineer, I want **proof** in this PR's commit history that the gate actually catches the May 20 regression — not just an assertion that it would.
- **US-4:** As the platform operator, I want the gate to provably exercise the **human (Bearer)** auth path — never accidentally falling back to the `X-API-Key` (non-human) path — so the gate cannot be silently defeated by test drift.
- **US-5:** As the platform operator, I want a runbook covering test-user lifecycle (creation, CA exclusion, password rotation, sign-in monitoring) so the operational dependency is documented.

### Non-Goals (explicitly OUT of scope for THIS PR)

- Full headless-Playwright real-browser MSAL flow (deferred — nightly canary, future PR).
- Removing or hardening the `X-API-Key` path (deliberate dual-auth product design, not a bypass).
- `deploy.yml` post-deploy Bearer probe (separate ~30 min PR, queued in `## Next`).
- End-to-end ingest→backtest smoke test (separate ~2–3h PR, queued in `## Next`).
- Dedicated test-tenant in Entra (using production tenant + narrow CA exclusion for v1; revisit when multi-account fleet starts).
- Automated test-user password rotation (manual quarterly is acceptable initially).
- Test coverage for `acquireTokenSilent` cache-eviction edge cases beyond happy path (a single refresh assertion is the bar).

### Key Decisions

- **D1 — Test identity:** ROPC delegated-token contract test (MSAL-Node or MSAL-Python in CI) using the **same scope config the SPA uses at runtime**. Codex-validated as the right balance of speed vs. catching the May 20 class.
- **D2 — Tenant strategy:** Production Entra tenant. Single test user with narrow Conditional Access exclusion (MFA bypass for this user + this app only). Microsoft's documented integration-test pattern.
- **D3 — Acceptance proof:** Negative-control commit on this branch demonstrates the gate fires when MSAL scope regresses. Durable artifact in PR history.
- **D4 — Scope of auth-dependency coverage:** Hit `/auth/me` AND one other authenticated endpoint to validate the auth dependency is correctly applied across routers.
- **D5 — Path-exclusivity assertion:** Test MUST assert the Bearer header is sent AND the `X-API-Key` header is NOT sent. Prevents future test-drift that masks Bearer-path regressions.
- **D6 — Failure classification:** Structured diagnostic with at least 5 failure modes — `ENTRA_TOKEN_NOT_ISSUED`, `BACKEND_REJECTED_TOKEN` (+ which claim mismatched), `BACKEND_5XX`, `NETWORK`, `CONFIG_LINT_FAIL`.

### Open Questions (Remaining — for design phase, NOT requirements)

- [ ] **Implementation language for the ROPC runner**: MSAL-Node (matches frontend tech) vs MSAL-Python (matches backend tech, can share Pydantic-validated config). Design Phase 3.
- [ ] **How the test imports SPA scope config**: read `frontend/src/lib/msal-config.ts` directly via filesystem? export to a shared JSON? Design Phase 3.
- [ ] **Static scope-config lint shape**: runtime assertion inside the ROPC test vs separate pytest/regex lint vs both. Design Phase 3.
- [ ] **GitHub Actions secret management**: GH Actions environment secrets vs. OIDC + Key Vault federation. Operational design; Phase 3.
- [ ] **Entra app reg `Allow public client flows` toggle**: ROPC requires this. Need to confirm current state and document the portal change in the runbook. Discovery question, Phase 3.

---

**Status: Ready for `/prd:create ui-e2e-auth-setup`**

---

## Pivot — Q7 (added 2026-05-25 after attempted Entra setup)

### What happened

While Pablo was doing the Entra prerequisite setup (create test user, CA exclusion), we hit a wall: even with the test user excluded from the "Require MFA for internal users" CA policy, signing in as `e2e-test@marketsignal.ai` STILL triggered "Let's keep your account secure" MFA registration prompts. This is a separate enforcement layer (Identity Protection MFA registration policy / Authentication Methods Registration Campaign) that Microsoft has been making increasingly hostile to bypass since 2024.

### Research review

We pulled current Microsoft Learn docs:

- **Mandatory MFA scope** (`concept-mandatory-multifactor-authentication`, updated 2026-04-23): Phase 1 (Oct 2024–Feb 2025) and Phase 2 (Oct 2025) enforcement target ONLY Microsoft management surfaces (Azure portal, Entra/Intune/M365 admin, CLI/PowerShell/SDK/IaC, REST to `management.azure.com`). Custom apps like MSAI's `/api/v1/*` are explicitly **out of scope**.
- **BUT** the same doc states: "The OAuth 2.0 Resource Owner Password Credentials (ROPC) token grant flow is incompatible with MFA. After MFA is enabled in your Microsoft Entra tenant, ROPC-based APIs used in your applications throw exceptions."
- **ROPC is now deprecated** across all MSAL SDKs in 2025: MSAL.NET 4.74.0, MSAL Node 3.2.3, MSAL Python 1.35.0, MSAL Java 1.24.0, MSAL Go 1.6.0. Azure SDK `UsernamePasswordCredential` deprecated in every language stack.
- Microsoft is actively making the no-MFA service-user pattern harder, not easier.

### Codex follow-up consultation (verbatim summary)

> "Pivot. ROPC is no longer a defensible PR gate. A PR-time ROPC check is now a liability. It will fail for reasons unrelated to the May 20 bug, and when it fails people will either rerun it, exempt accounts, or weaken tenant policy. That is worse than not having it."
>
> "Static lint + synthetic JWT are not a full substitute for a live integration test. They are a **better PR gate.** The right split is deterministic contract checks before merge, live identity smoke after deploy or on schedule."

Codex agreed with the pivot and added refinements (see new design in Refined Understanding v2 below).

### New design — pivoted away from live Entra

Three deterministic gates at PR time, all running in-code with no network dependency:

1. **Gate 1 — AST/typed lint of MSAL scope config.** Inspect exported scope arrays / `protectedResourceMap` / MSAL request objects (NOT regex grep). Reject `User.Read`, `graph.microsoft.com`, mixed-resource scope arrays, fallback/default scopes that don't match `api://<client-id>/*`.
2. **Gate 2 — Backend auth middleware contract test with patched JWKS.** Use a synthetic JWT signed with a test private key, swap JWKS to the test public key during the test, hit the real middleware. Positive: `aud=<client-id>` + `iss=login.microsoftonline.com/<tenant>/v2.0` + `ver=2.0` + `scp` contains `access_as_user` → accepted. Negatives: Graph audience rejected, wrong tenant rejected, missing `scp` rejected, expired token rejected, bad `kid` rejected, wrong signature rejected, `scp=User.Read` rejected, app-only `roles` (no `scp`) rejected, wrong `iss` rejected, v1 token rejected (if unsupported).
3. **Gate 3 — Committed non-secret `identity-contract.json`** as single source of truth (tenant ID, client ID, app ID URI, issuer, scope name). Lint that all env examples (`frontend/.env.example`, `backend/.env.example`), compose files (`docker-compose.dev.yml`, `docker-compose.prod.yml`), frontend config (`frontend/src/lib/msal-config.ts`), backend config (PyJWT call sites), and deploy workflow (`deploy.yml`) all reference the same values from this contract file.

### What was discarded

- ❌ Dedicated Entra test user (deleted from Entra 2026-05-25; nothing else depends on it).
- ❌ Narrow Conditional Access exclusion (reverted from "Require MFA for internal users" policy 2026-05-25).
- ❌ App registration "Allow public client flows" toggle (never enabled — wasn't reached).
- ❌ GH Actions secrets for test user (never added — wasn't reached).
- ❌ Test-user lifecycle runbook (no test user to manage anymore).
- ❌ Negative-control commit specifically for ROPC scope-config breakage — the lint version of this proof still happens (commit `User.Read` regression → Gate 1 fails → revert), so US-3 survives in modified form.
- ❌ ROPC implementation language choice (MSAL-Node vs MSAL-Python) — moot.
- ❌ Entra app-reg confirmation question — moot.

### Refined Understanding v2 — supersedes original

The original Refined Understanding section (above) is preserved as historical context but **no longer reflects the design**. The new Refined Understanding is captured in the **rewritten PRD `docs/prds/ui-e2e-auth-setup.md` v2.0**, not duplicated here.

**Status: PRD rewritten v2.0 — ready for Phase 2 (Research)**
