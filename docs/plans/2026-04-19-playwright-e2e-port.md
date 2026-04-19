# Plan — Port 9 Playwright e2e specs from codex-version to claude-version

**Date:** 2026-04-19
**Branch:** `feat/playwright-e2e-port`
**Council verdict:** `docs/decisions/which-version-to-keep.md` port #1 (blocking codex deletion)
**Research brief:** `docs/research/2026-04-19-playwright-e2e-port.md`

---

## Scope

Port all 9 specs from `codex-version/frontend/e2e/` (2,021 LOC) into the repo-root Playwright scaffold at `tests/e2e/specs/`, running against `http://localhost:3300` (claude UI). This requires non-trivial claude-version frontend wiring to add an "API Key Test Mode" auth bypass (codex has it, claude doesn't) — without it, specs cannot reach any page past `/login`.

**Out of scope (per council ratification):**

- Port `LiveStateController` — council SKIP
- Add `daily-scheduler` container — council OPTIONAL / SKIP

## Decisions made upfront

### D1 — Auth bypass strategy: port codex's `ApiKeyAuthProvider` pattern verbatim

**Rejected alternative:** `storageState` + localStorage JWT injection. Reason: claude's `useAuth()` uses MSAL's `acquireTokenSilent()`, not localStorage reads, so injection wouldn't persist auth state. Refactoring MSAL to read from localStorage would be MORE invasive than adding the api-key branch.

**Chosen:** Add `auth-mode.ts` + `ApiKeyAuthProvider` branch in `components/providers.tsx`. Existing backend already accepts `X-API-Key` header (`claude-version/backend/src/msai/core/auth.py:70-81`) — no backend changes needed.

### D2 — api-key mode is OFF by default in dev compose

Only enabled via `NEXT_PUBLIC_AUTH_MODE=api-key NEXT_PUBLIC_E2E_API_KEY=msai-dev-key` overrides when Playwright needs it. Default dev UX stays MSAL. Prevents accidentally shipping a bypass in production.

### D3 — Specs mock all API calls; no real backend needed for spec runs

Confirmed from exploration: every codex spec uses `page.route("**/api/v1/**", ...)` to mock responses. Specs never hit a real backend. This means:

- Playwright runs require `docker compose -f claude-version/docker-compose.dev.yml up frontend` (only) with api-key env vars.
- No postgres/redis/backend dependency for spec runs.
- `webServer` in `playwright.config.ts` can optionally auto-start the frontend in api-key mode.

### D4 — Route delta: codex `/live` → claude `/live-trading`

Every spec that navigates to codex's `/live` must be rewritten to `/live-trading`. No reverse rename on claude side — `/live-trading` is the current product route.

### D5 — data-testid policy: add ONLY where specs assert fragile values

Most spec assertions already pass on claude because both versions derived from the same PRD and use shadcn/Radix primitives with correct ARIA roles. Only 8 spots need `data-testid` per exploration — all on value-sensitive cells (Sharpe ratios, deployment ids, etc.).

---

## Files to modify / create

### A. Claude-version frontend (auth wiring — prerequisite for any spec to pass)

| #   | File                                                   | Action                                                                                                                                                                                                                 | ~LOC      |
| --- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| A1  | `claude-version/frontend/src/lib/auth-mode.ts`         | **CREATE** — port verbatim from codex (`export type AuthMode`, `getAuthMode`, `isApiKeyAuthMode`, `getApiKeyCredential`)                                                                                               | 20        |
| A2  | `claude-version/frontend/src/components/providers.tsx` | **MODIFY** — branch `AuthProvider` into `ApiKeyAuthProvider` vs `MsalAuthProvider` based on `isApiKeyAuthMode()`                                                                                                       | +30 / -5  |
| A3  | `claude-version/frontend/src/lib/auth.ts`              | **MODIFY** — read auth mode from context (provided by A2) instead of calling MSAL hooks directly; keep public `useAuth()` signature identical                                                                          | +25 / -10 |
| A4  | `claude-version/frontend/src/app/login/page.tsx`       | **MODIFY** — at top of component: `if (isApiKeyAuthMode()) return <ApiKeyTestModeCard />;` — render a shadcn Card with heading "API Key Test Mode" and token status. Existing MSAL login card unchanged in entra mode. | +30       |
| A5  | `claude-version/frontend/src/lib/api.ts`               | **MODIFY** — when `isApiKeyAuthMode()`, send `x-api-key: <NEXT_PUBLIC_E2E_API_KEY>` header instead of `Authorization: Bearer <jwt>`. Backend already accepts this (verified in `core/auth.py:75`).                     | +10       |

### B. Playwright scaffold (repo root)

| #   | File                               | Action                                                                                                                                                                                                                                      | ~LOC    |
| --- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| B1  | `playwright.config.ts`             | **MODIFY** — change default `baseURL` from `:3000` to `:3300` (claude). Keep `PLAYWRIGHT_BASE_URL` env override.                                                                                                                            | +1 / -1 |
| B2  | `tests/e2e/fixtures/auth.ts`       | **REWRITE** — remove email/password flow (targets `/api/auth/login` which does not exist in MSAI). Replace with a no-op `setup` project that confirms `NEXT_PUBLIC_AUTH_MODE=api-key` is set, or simply delete if unused (specs self-mock). | ±60     |
| B3  | `playwright.config.ts` (webServer) | **MODIFY (optional)** — add `webServer` block to auto-start claude frontend with api-key env vars on spec runs. Could defer.                                                                                                                | +8      |

### C. Specs (port 9 files, TDD order)

All specs ported from `codex-version/frontend/e2e/<name>.spec.ts` → `tests/e2e/specs/<name>.spec.ts`. Shared edits per spec:

- Replace any `page.goto("/live")` with `page.goto("/live-trading")`
- Verify heading/button/label assertions against claude's actual copy (most already match per exploration)
- Add any required `data-testid` selectors (per Section D below)

| #   | Spec                           | LOC | Tests | Order rationale                                                                                                                           |
| --- | ------------------------------ | --- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| C1  | `strategies-authoring.spec.ts` | 117 | 1     | Smallest, isolated — quickest signal that the infrastructure works                                                                        |
| C2  | `data-refresh.spec.ts`         | 168 | 1     | Isolated to `/data-management`                                                                                                            |
| C3  | `live-paper.spec.ts`           | 173 | 1     | Exercises `/live-trading` route rename + first deployment cell assertion                                                                  |
| C4  | `research-jobs.spec.ts`        | 181 | 2     | Research page form interactions                                                                                                           |
| C5  | `graduation-portfolio.spec.ts` | 197 | 1     | Cross-page flow (graduation → portfolio)                                                                                                  |
| C6  | `research-promotion.spec.ts`   | 206 | 1     | Research + live flow                                                                                                                      |
| C7  | `research-console.spec.ts`     | 226 | 1     | Compare + promotion flow                                                                                                                  |
| C8  | `operator-journey.spec.ts`     | 731 | 2     | Master integration spec — run LAST, consolidates everything                                                                               |
| C9  | `live-paper-real.spec.ts`      | 22  | 1     | Real-backend spec (no mocks). Requires docker stack up. Run last; may be deferred if it depends on seed data claude-version doesn't have. |

### D. `data-testid` additions to claude-version frontend

| Component file                                             | Element                                | Testid                    | Spec that needs it               |
| ---------------------------------------------------------- | -------------------------------------- | ------------------------- | -------------------------------- |
| `claude-version/frontend/src/app/live-trading/page.tsx`    | IB status section `<h2>`               | `ib-status-heading`       | operator-journey.spec.ts:712     |
| `claude-version/frontend/src/app/live-trading/page.tsx`    | Deployment status badge                | `deployment-status-badge` | operator-journey.spec.ts:718     |
| `claude-version/frontend/src/app/live-trading/page.tsx`    | Broker truth pane (if exists — verify) | `broker-truth-pane`       | operator-journey.spec.ts:719     |
| `claude-version/frontend/src/app/portfolio/page.tsx`       | Portfolio run detail card              | `portfolio-run-detail`    | graduation-portfolio.spec.ts:193 |
| `claude-version/frontend/src/app/portfolio/page.tsx`       | Sharpe metric value cell               | `portfolio-run-sharpe`    | graduation-portfolio.spec.ts:194 |
| `claude-version/frontend/src/app/research/page.tsx`        | Job queue section `<section>`          | `research-job-queue`      | research-jobs.spec.ts:87         |
| `claude-version/frontend/src/app/data-management/page.tsx` | Alerts list `<ul>`                     | `alerts-list`             | data-refresh.spec.ts:124         |
| `claude-version/frontend/src/app/login/page.tsx`           | API Key Test Mode card                 | `api-key-test-mode-card`  | operator-journey.spec.ts:668     |

If any of the above UI elements do not exist in claude (e.g., "Broker Truth" pane), flag as a UI gap in the plan-review loop and decide: (a) skip that assertion in the ported spec, (b) add the missing element to claude, or (c) redesign the spec.

### E. Graduate to use cases

After all 9 specs are green:

- Write `tests/e2e/use-cases/playwright-e2e-port.md` — one markdown UC per spec with Intent / Steps / Verification / Persistence (per `.claude/rules/testing.md`)

---

## Execution order (TDD — each spec green before next)

### Phase 0 — Auth infrastructure (MUST be first; blocks everything else)

1. A1: Create `auth-mode.ts`
2. A2: Refactor `components/providers.tsx` to branch providers
3. A3: Update `lib/auth.ts` to use context from A2
4. A4: Add API Key Test Mode card to `login/page.tsx` with `data-testid="api-key-test-mode-card"`
5. A5: Update `lib/api.ts` to send `x-api-key` header in api-key mode
6. B1: Retarget `playwright.config.ts` baseURL to `:3300`
7. B2: Rewrite `tests/e2e/fixtures/auth.ts` (or replace with no-op)
8. **Smoke test:** start claude frontend with `NEXT_PUBLIC_AUTH_MODE=api-key NEXT_PUBLIC_E2E_API_KEY=msai-dev-key pnpm dev`; manually `curl http://localhost:3300/login` and inspect for "API Key Test Mode" heading. Also navigate to `/backtests` and confirm no redirect to `/login`.
9. **Commit:** `feat(frontend): add API Key Test Mode auth bypass for E2E`

### Phase 1 — Port specs (one at a time, TDD)

For each spec C1…C9 (in that order):

1. Copy spec from `codex-version/frontend/e2e/<name>.spec.ts` to `tests/e2e/specs/<name>.spec.ts`
2. Apply per-spec edits: `/live` → `/live-trading`, any copy-string fixes identified
3. Add any data-testids from Section D that this spec requires
4. Run: `PLAYWRIGHT_BASE_URL=http://localhost:3300 pnpm exec playwright test <name>.spec.ts`
5. If FAIL: diagnose → fix selector OR claude UI gap → rerun. If structural gap (missing page, missing component), document and decide: defer, skip, or build.
6. If PASS: commit `test(e2e): port <name>.spec.ts to claude-version`

### Phase 2 — Consolidate & verify

- Run full suite: `PLAYWRIGHT_BASE_URL=http://localhost:3300 pnpm exec playwright test`
- Review HTML report: `pnpm exec playwright show-report`
- If anything flakes, fix root cause (no retries added just to hide flakiness)
- **Simplify pass:** `/simplify` on modified files (Phase 5.2 of workflow)
- **Verify pass:** run claude backend + frontend unit tests via `verify-app` subagent — ensure no regressions from auth refactor

### Phase 3 — Graduate

- Write `tests/e2e/use-cases/playwright-e2e-port.md` with 9 markdown UCs
- Update CONTINUITY.md: check all boxes
- Commit, push, PR (ask user first)

---

## Risks & mitigations

| Risk                                                                                      | Likelihood  | Mitigation                                                                                                                                                                                                            |
| ----------------------------------------------------------------------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude UI copy differs from codex (heading text drift)                                    | Medium      | Adjust `getByText` / `getByRole` assertions to match claude copy. If the copy is user-facing and claude's is less clear, this becomes a product fix, not a test fix.                                                  |
| "Broker Truth" / "Walk-Forward Windows" sections don't exist in claude                    | Medium-High | Verify during Phase 1. If absent, either build them (if PRD-aligned) or remove assertion from ported spec with a `// PORT-NOTE: section missing in claude — tracked as follow-up` comment and Next-section tracker.   |
| Responsive viewport test (iPhone SE 430×932 in operator-journey line 722) fails on claude | Medium      | Check if claude's shadcn shell handles 430px width. If broken, file a follow-up UI issue; do NOT block the port.                                                                                                      |
| Auth refactor breaks existing MSAL flow                                                   | Low-Medium  | Smoke test entra mode (default) after refactor: start dev compose WITHOUT `NEXT_PUBLIC_AUTH_MODE`; confirm login button still redirects to Azure. Covered in Phase 0 step 8 as a parallel smoke.                      |
| Specs depend on codex-specific API response shapes that claude doesn't match              | Low         | `page.route` mocks are DEFINED in each spec, not read from real backend — so the spec controls response shape. No claude API contract dependency. Verify if any `page.goto()` triggers a real page-level fetch (SSR). |
| `playwright.config.ts` `webServer` auto-start conflicts with docker-compose frontend      | Low         | Skip `webServer` in B3; require operator to start compose manually before spec runs. Documented in CONTINUITY.                                                                                                        |

---

## Success criteria

- [ ] All 9 specs green on `PLAYWRIGHT_BASE_URL=http://localhost:3300 pnpm exec playwright test`
- [ ] Entra MSAL login still works in default dev compose (no `NEXT_PUBLIC_AUTH_MODE` set)
- [ ] Claude backend unit/integration tests still pass (no backend changes, but confirm)
- [ ] Frontend `pnpm build` clean
- [ ] Playwright HTML report produced at `tests/e2e/reports/` with 0 failures
- [ ] Use cases graduated to `tests/e2e/use-cases/playwright-e2e-port.md`
- [ ] PR opens clean with `gh pr create --base main`

---

## Non-goals (to avoid scope creep)

- Adding `data-testid` to components that specs don't need
- Refactoring claude frontend for "cleaner auth architecture" beyond what the port requires
- Replacing shadcn primitives
- Changing any claude backend code (no backend edits in scope)
- Running specs against codex-version (it's being deleted; no point verifying against both)
- Porting the `daily-scheduler` container (council SKIP)
- Porting `LiveStateController` (council SKIP)
