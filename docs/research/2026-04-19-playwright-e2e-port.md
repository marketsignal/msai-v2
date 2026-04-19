# Research brief — playwright-e2e-port

**Date:** 2026-04-19
**Feature:** Port 9 Playwright e2e specs from codex-version to claude-version
**Status:** N/A-minimal (no new external libraries being adopted)

---

## External libraries touched

| Library                        | Version                          | Already in project? | Research needed?                                                                    |
| ------------------------------ | -------------------------------- | ------------------- | ----------------------------------------------------------------------------------- |
| `@playwright/test`             | installed via repo-root scaffold | Yes                 | No — existing `tests/e2e/fixtures/auth.ts` + `playwright.config.ts` already present |
| `next` (Next.js 15 App Router) | already used                     | Yes                 | No                                                                                  |
| `@radix-ui/*` + shadcn/ui      | already used                     | Yes                 | No                                                                                  |
| `@azure/msal-browser` (MSAL)   | already used by claude frontend  | Yes                 | No                                                                                  |

No new external dependencies are being adopted. Playwright, shadcn, Next.js, MSAL are all pre-existing in claude-version. This passes the research gate as N/A.

## Design impact

**Auth bypass is the real design question**, not a library choice. Codex specs use `page.goto("/login")` and expect an **"API Key Test Mode" heading** to be visible — a dev-mode UI affordance that claude-version's `/login` page does NOT currently have. The port cannot proceed without resolving this.

Two candidate approaches (to be decided in the plan — Phase 3 of this workflow):

1. **Port codex's "API Key Test Mode" UI** onto claude's `/login` page (dev-mode only, gated on `NODE_ENV !== "production"` + `NEXT_PUBLIC_MSAI_API_KEY` env). Matches codex specs verbatim. Requires code change in `claude-version/frontend/src/app/login/page.tsx`.
2. **Bypass `/login` entirely in specs** via `storageState` injection in `tests/e2e/fixtures/auth.ts`. Requires modifying every ported spec that hits `/login`.

Option 1 preserves spec fidelity; option 2 is more invasive per spec but leaves production login untouched. The exploration task (#9) will inventory which specs actually assert on the login UI vs just use it as a transitional step — if the latter, option 2 is cheap.

## Test implication

- All 9 specs use `page.route("**/api/v1/**", ...)` for API mocking — no real claude backend required for spec runs.
- Specs assert on **role-based selectors** (`getByRole("heading")`, `getByRole("cell")`, `getByRole("link")`) and **visible text** (`getByText`). They rarely use CSS classes. Good news: claude's shadcn primitives use correct ARIA roles by default.
- Where specs use codex-specific copy strings (e.g., "Backtest Runner" heading), claude must either match the copy or the spec must be adjusted.
- Specs expect specific URL patterns (`/backtests/{id}`, `/research`, `/graduation`, `/portfolio`). All these routes exist in claude-version (confirmed via `ls claude-version/frontend/src/app/`).

## Open risks

- **MSAL redirect-in-test**: if option 2 is chosen and the MSAL `useAuth` hook redirects to Azure login when no token is present, specs will break. Need to confirm claude's auth hook behaviour when `storageState` pre-injects a token.
- **Copy string drift**: codex heading copy ("Backtest Runner", "Research Console", etc.) may not match claude's copy. Inventory needed.
- **Codex-specific components**: codex may use UI components (e.g., Walk-Forward Windows card) that claude doesn't have. Those specs may need to be deferred or reimplemented.

## Fallback path used?

No — main agent wrote this brief directly. Research-first agent not dispatched because no new libraries are being adopted. The N/A pathway from `.claude/commands/new-feature.md` Phase 2.4 applies.
