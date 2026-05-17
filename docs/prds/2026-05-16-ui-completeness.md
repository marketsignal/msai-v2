# PRD — UI Completeness (MSAI v2)

**Status:** Draft v1
**Date:** 2026-05-16
**Branch:** `feat/ui-completeness`
**Author:** Claude (with Pablo, post-council)
**Mode:** Trust-First (hedge fund, real money, irreversible actions) + Product UI (CRUD/observability density)
**Source audit:** `docs/audits/2026-05-16-ui-surface-audit.md` (12 fakes, 7 missing pages/components, 4 backend safety findings, 1 Phase 1 policy contradiction)
**Council scope decision:** `docs/decisions/2026-05-16-ui-completeness-scope.md` §13 (user overrode staging recommendation → single PR)

---

## 1. Goal

After PR #67 (live workflow UI catch-up) and PR #68 (CLI 100% REST parity), the UI is the laggard surface in MSAI v2 (the project's stated ordering: API-first, CLI-second, UI-third). The audit revealed three distinct problems:

1. **The UI lies in multiple places.** `/settings` ships 8 distinct fake/hardcoded elements; `/dashboard`'s biggest visual element (EquityChart) is permanently empty; `/strategies` shows hardcoded `--` metrics on every card. These are user-visible lies, not "missing features."
2. **CRUD parity is incomplete.** The backend exposes PATCH/DELETE on strategies, GET on alerts, GET on account portfolio/health, POST cancel on research jobs, GET on live audits — none surfaced in UI.
3. **One backend feature contradicts ratified Phase 1 architecture** (strategy templates scaffolder writes Python files to `STRATEGIES_ROOT`, which CLAUDE.md says is git-only). This must be decided BEFORE any templates UI work.

This PR closes all three. **One PR, many focused commits, one decision doc per concern area.**

---

## 2. Stakeholders & Personas

**Primary user:** Pablo — solo operator of a personal hedge fund. Uses the UI daily to monitor live deployments, review backtests, ingest market data, and manage strategies. No team; the UI must be self-explanatory and never lie.

**Secondary users (future):**

- Operator-grade developers reading the codebase 6 months from now (Maintainer council concern)
- Audit / compliance reviewer (Trust-First aesthetic — every status must be color + icon + text, never color alone)

**Non-users:**

- Real-money trading is paper-only in Phase 1. No multi-tenancy. No external access.

---

## 3. Scope — In This PR

### 3.1 P0 — Stop the lies

| ID   | Deliverable                                                                                                                                                                                                                                                                                                                             | Affected files                                                                                                                         |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| P0-A | **`/settings` rewrite**: strip ALL fake content. Keep a Profile card (`/auth/me` real fields: name, email, role, display_name) and nothing else by default. Remove fake Notifications, hardcoded System Information, Clear-All-Data Danger Zone. If we later add real preferences they get a follow-up PR.                              | `frontend/src/app/settings/page.tsx`                                                                                                   |
| P0-B | **`useAuth` extension**: extend `AuthUser` type with `role: string \| null` and `display_name: string \| null`. Fetch `/api/v1/auth/me` on auth state change, merge into context. Backed by TanStack Query for cache + refresh.                                                                                                         | `frontend/src/lib/auth.ts`, `frontend/src/components/providers.tsx`                                                                    |
| P0-C | **Dashboard EquityChart fix**: either ship a new backend endpoint `GET /api/v1/live/equity-curve` (daily aggregate of realized + unrealized PnL across all deployments) OR remove the chart and replace with an Alerts feed card. Decision in Phase 3.2 plan after research-first reads recharts patterns. **No `data={[]}` survives.** | `frontend/src/app/dashboard/page.tsx`, `frontend/src/components/dashboard/equity-chart.tsx`, optionally `backend/src/msai/api/live.py` |
| P0-D | **PortfolioSummary fixes**: drop "Total Return `--`" card OR wire to real source; drive `trend` from value sign on each card. No hardcoded green-up.                                                                                                                                                                                    | `frontend/src/components/dashboard/portfolio-summary.tsx`                                                                              |

### 3.2 P1 — Core missing functionality

| ID           | Deliverable                                                                                                                                                                                                                                                                                                                                                             | Affected files                                                                                                                                   |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| P1-A         | **`/alerts` page**: paginated table reading `GET /api/v1/alerts/`. Trust-First design: severity icon + text + ISO timestamp + structured detail in expandable row. shadcn `Table` + `Sheet` for detail drawer.                                                                                                                                                          | `frontend/src/app/alerts/page.tsx` (NEW), `frontend/src/components/alerts/*` (NEW), `frontend/src/components/layout/sidebar.tsx` (add nav entry) |
| P1-B         | **`/account` page**: tabbed view of `/account/summary` + `/account/portfolio` + `/account/health`. Gated by P1-B-backend. Manual Refresh button + 30s background poll.                                                                                                                                                                                                  | `frontend/src/app/account/page.tsx` (NEW), `frontend/src/components/account/*` (NEW), sidebar nav                                                |
| P1-B-backend | **`ib_account.py` caching fix**: extend the existing `IBProbe` periodic task to also push `/summary` and `/portfolio` data into a TTL cache (15-30s). `account.py` handlers serve from cache. Cap `client_id` counter (`itertools.count(start=900, step=1) % 100` or single shared id).                                                                                 | `backend/src/msai/services/ib_account.py`, `backend/src/msai/services/ib_probe.py`                                                               |
| P1-C         | **Strategy edit form**: replace `/strategies/[id]` "Validate" plain JSON-parse with a real form section that PATCHes `name`, `description`, and `default_config`. Use shadcn primitives (Input + Textarea + Button). On Save: `PATCH /api/v1/strategies/{id}`. On success: success toast + refetch.                                                                     | `frontend/src/app/strategies/[id]/page.tsx`, `frontend/src/lib/api.ts` (add `patchStrategy`)                                                     |
| P1-D         | **Strategy validate (real)**: the existing button should call `POST /api/v1/strategies/{id}/validate` (which actually verifies the strategy LOADS) and display the result. Local JSON-parse is a separate UX concern; the button must do what its label says.                                                                                                           | `frontend/src/app/strategies/[id]/page.tsx`, `frontend/src/lib/api.ts` (add `validateStrategy`)                                                  |
| P1-E         | **Strategy delete**: shadcn `AlertDialog` confirm + explicit "Type strategy name to delete" friction. Calls `DELETE /api/v1/strategies/{id}`. Gated by P1-E-backend. On success: toast + redirect to `/strategies`.                                                                                                                                                     | `frontend/src/app/strategies/[id]/page.tsx`, `frontend/src/lib/api.ts` (add `deleteStrategy`)                                                    |
| P1-E-backend | **Strategy soft-delete migration**: add `deleted_at: datetime \| null` column to `strategies` table. Update `DELETE /api/v1/strategies/{id}` to SET `deleted_at = now()` instead of hard-delete. Update `GET /strategies/` list to filter `deleted_at IS NULL`. Update `GET /strategies/{id}` to still resolve archived rows (for backtest history). Alembic migration. | `backend/src/msai/models/strategy.py`, `backend/src/msai/api/strategies.py`, `backend/alembic/versions/*` (NEW)                                  |
| P1-F         | **`/strategies` list — show real status + metrics**: parent passes `status` from `/live/status` (running/stopped/error) + ideally Sharpe/Return/WinRate from best backtest. If best-backtest metric fetch is too expensive, drop those columns entirely (no more `--` placeholders).                                                                                    | `frontend/src/app/strategies/page.tsx`, `frontend/src/components/strategies/strategy-card.tsx`                                                   |

### 3.3 P1 — Phase 1 policy decision (BLOCKER)

| ID        | Deliverable                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Notes                                                            |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| P1-POLICY | **Decision doc** at `docs/decisions/2026-05-16-strategy-templates-policy.md`. Resolve the contradiction: either (a) AMEND Phase 1 to allow templates scaffolder (UI writes Python files to `strategies/`, then operator commits to git as today), or (b) CUT the backend feature (`api/strategy_templates.py` + `services/strategy_templates.py`). **No templates UI ships in this PR either way** — the decision unblocks Stage 2 ui-completeness follow-ups. | Decision required Phase 3.2; cuts/amendments execute in Phase 4. |

### 3.4 P2 — Polish + observability parity

| ID   | Deliverable                                                                                                                                                                               | Affected files                                                                                                 |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| P2-A | **Live audit-log drawer**: shadcn `Sheet` triggered from each deployment row on `/live-trading`. Reads `GET /api/v1/live/audits/{id}`.                                                    | `frontend/src/app/live-trading/page.tsx`, `frontend/src/lib/api.ts` (add `getLiveAudits`)                      |
| P2-B | **Research job cancel CTA**: button on `/research/[id]` for running jobs. Calls `POST /api/v1/research/jobs/{id}/cancel`. Confirm dialog.                                                 | `frontend/src/app/research/[id]/page.tsx`, `frontend/src/lib/api.ts` (add `cancelResearchJob`)                 |
| P2-C | **Market-data storage status card**: shows file count, bytes, asset-class breakdown. Reads `GET /api/v1/market-data/status`. Lives on `/market-data` page header or as dashboard sidecar. | `frontend/src/app/market-data/page.tsx` (extend) OR `frontend/src/components/dashboard/storage-card.tsx` (NEW) |
| P2-D | **Symbol readiness widget**: pre-trade check on `/live-trading` portfolio compose flow. Reads `GET /api/v1/symbols/readiness`.                                                            | `frontend/src/app/live-trading/portfolio/page.tsx`, `frontend/src/lib/api.ts`                                  |
| P2-E | **Header alerts notification bell**: small badge on the header with unread alert count. Click → `/alerts`. Poll every 30s via TanStack Query.                                             | `frontend/src/components/layout/header.tsx`                                                                    |
| P2-F | **`/strategies` redesign**: skeleton loaders during load, empty-state with "How to add a strategy" helper text (linking to docs), real metrics or no metrics.                             | `frontend/src/app/strategies/page.tsx`, `frontend/src/components/strategies/strategy-card.tsx`                 |
| P2-G | **Global error pages**: `app/not-found.tsx` (404) and `app/error.tsx` (500) styled with shadcn primitives + sad-but-helpful copy + "Back to dashboard" CTA.                               | `frontend/src/app/not-found.tsx` (NEW), `frontend/src/app/error.tsx` (NEW)                                     |
| P2-H | **Dashboard densification**: add Alerts feed card (last 5 alerts) replacing or alongside the EquityChart slot.                                                                            | `frontend/src/app/dashboard/page.tsx`, `frontend/src/components/dashboard/alerts-feed.tsx` (NEW)               |

### 3.5 P2 — Verification scaffolding

| ID    | Deliverable                                                                                                                                                                                                                                     | Affected files                             |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| P2-V1 | **Playwright spec authoring**: create `frontend/tests/e2e/specs/` directory. Author one `.spec.ts` per shipped surface using observed selectors from verify-e2e Phase 5.4 reports. Selectors prefer `getByRole` / `getByLabel` / `getByTestId`. | `frontend/tests/e2e/specs/*.spec.ts` (NEW) |
| P2-V2 | **CI workflow activation**: copy `docs/ci-templates/e2e.yml` to `.github/workflows/e2e.yml`. Run smoke specs on PR, full suite nightly.                                                                                                         | `.github/workflows/e2e.yml` (NEW)          |

---

## 4. Out of Scope (Deferred with Explicit Reason)

| Item                                        | Reason                                                                                                                                                                |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Strategy templates scaffolder UI            | Blocked on P1-POLICY decision. If policy amends, follow-up `/new-feature templates-scaffolder`. If policy cuts, never built.                                          |
| Market-data bars browser (direct ingest UI) | The chart at `/market-data/chart` + the inventory at `/market-data` already cover the user-visible needs. Direct ingest is CLI-only by design. Revisit if Pablo asks. |
| Custom notification preferences backend     | No backend endpoint exists. Building one is its own feature, not "completeness."                                                                                      |
| Multi-tenant role/permission system         | Phase 1 is single-user (Pablo only). Role exists in JWT but maps to view-only today.                                                                                  |
| Backtest equity timeseries on dashboard     | Backtest-style equity timeseries is a different concept than live equity curve. If we ship P0-C as a backend endpoint, it's live-only.                                |

---

## 5. Acceptance Criteria (per surface)

### `/settings`

- [ ] No element on the page is hardcoded. Every value either comes from an API call or is omitted.
- [ ] Profile card shows `name`, `email`, `role`, `display_name` from `/auth/me` — never "Demo User", never hardcoded "Admin".
- [ ] No "Save Preferences" button that doesn't save.
- [ ] No "Clear All Data" Danger Zone (the endpoint doesn't exist; the button is the lie).
- [ ] Page passes Playwright spec asserting no `text=Demo User`, no `text=v0.1.0`, no `text=5d 14h`, no `[data-fake]`.

### `/dashboard`

- [ ] No card renders `data={[]}` as a permanent state.
- [ ] No StatCard returns `value="--"` regardless of state.
- [ ] Trend arrows reflect actual sign of value.
- [ ] Alerts feed card present (P2-H) showing last 5 alerts from `GET /api/v1/alerts/`.

### `/alerts`

- [ ] Table reads from `GET /api/v1/alerts/`. Each row: severity icon + ISO timestamp + alert code + plain-language message + (expandable) raw payload.
- [ ] Empty state: "All quiet — no recent alerts" + last-checked timestamp.
- [ ] Reload preserves selected severity filter (if any).
- [ ] Loading state uses shadcn Skeleton (install if needed).
- [ ] Sidebar nav has Alerts entry.

### `/account`

- [ ] Three tabs (Summary, Portfolio, Health) reading the respective `/api/v1/account/*` endpoints.
- [ ] Manual Refresh button + automatic refresh every 30s via TanStack Query (no aggressive sub-30s polling).
- [ ] Loading state honors `prefers-reduced-motion` (no infinite spinners; skeleton fade-in).
- [ ] Backend caching fix (P1-B-backend) verified by integration test: 10 concurrent UI requests open ≤1 IB connection over a 30s window.
- [ ] Sidebar nav has Account entry.

### `/strategies` + `/strategies/[id]`

- [ ] List page: no card shows `--` for status (real status from `/live/status` OR no status column at all).
- [ ] Detail page Validate button: calls `POST /api/v1/strategies/{id}/validate`; result shown in dialog.
- [ ] Detail page Edit: separate Save button on the form; PATCHes `name`/`description`/`default_config`. Reload preserves changes.
- [ ] Detail page Delete: AlertDialog with "type strategy name" friction. After confirm: DELETE → toast → redirect to `/strategies`. Reload still shows the strategy archived (soft-delete) but not in the active list.
- [ ] Backend soft-delete migration applied + existing backtests still resolve their strategy_id.

### Header

- [ ] Alerts bell with unread badge count. Polls every 30s. Click → `/alerts`.

### Global error pages

- [ ] `not-found.tsx`: dark theme, MSAI logo, "Page not found" + back-to-dashboard CTA.
- [ ] `error.tsx`: same shape, "Something went wrong" + retry CTA + minimal error detail.

### Playwright

- [ ] At least one `.spec.ts` per surface that ships in this PR.
- [ ] Auth fixture works via `TEST_API_KEY` (X-API-Key fallback) per existing fixture template.
- [ ] CI workflow `.github/workflows/e2e.yml` runs smoke-tagged specs on PR.

---

## 6. Backend Changes (must land in this PR) — research-validated

Research brief: `docs/research/2026-05-16-ui-completeness.md`.

1. **`services/ib_account.py` → Singleton `IBAccountSnapshot` pattern** (research finding 2):
   - Replace per-request `IB.connectAsync` + unbounded `itertools.count(start=900)` with **one shared `IB()` instance**, static `client_id=900`, 30-second background refresh.
   - Align refresh cadence with `_PROBE_INTERVAL_S` so both `/account/summary` and `/account/portfolio` serve from the same background-refreshed snapshot.
   - Wire to FastAPI lifespan (the existing pattern for `IBProbe`). **NO new dependency** (`fastapi-cache2`, `cachetools` were explicitly rejected).
   - Cap `client_id` to a single static value rather than counter.

2. **`models/strategy.py` → `deleted_at: Mapped[datetime | None]`** (research finding 3):
   - Additive Alembic migration: add `deleted_at: TIMESTAMP NULL` + partial index `WHERE deleted_at IS NULL` for query performance.
   - Global SQLAlchemy event listener `do_orm_execute` + `with_loader_criteria` to filter archived rows from all `select(Strategy)` queries.
   - Opt-out: backtest history fetch uses `execution_options(include_deleted=True)` to resolve archived strategies for old backtests.

3. **`api/strategies.py`** — DELETE handler sets `deleted_at = now()` instead of `db.delete()`. List + detail handlers inherit the global filter; detail handler opts in for include-archived when called from backtest detail flow.

4. **`api/live.py`** — **no new `/equity-curve` endpoint** (research finding 1). The data doesn't exist (Phase 1 has no per-deployment equity timeseries). Dashboard card gets dropped or replaced with Alerts feed (P2-H).

5. **(NEW from P1-G + N-8) `backend/src/msai/api/system.py`** — new GET `/api/v1/system/health` endpoint aggregating: API status, DB ping, Redis ping, IB Gateway probe (from `IBProbe`), worker queue depth (arq), parquet storage stats (count + bytes), version + commit SHA + uptime. Polled by `/system` page every 30s.

## 6b. Frontend Dependencies (NEW installs)

Per research finding 4, the following dependencies must be installed in commit 1 (so the rest of the work can use them):

```bash
cd frontend && pnpm add react-hook-form @hookform/resolvers zod
cd frontend && pnpm exec shadcn@latest add form skeleton pagination
```

- **`react-hook-form` + `@hookform/resolvers` + `zod`** — for the strategy edit form (P1-C), settings profile form (P0-A), and any future forms. Validation pattern: `zod` schema → `zodResolver` → `useForm`.
- **shadcn `form`** — wraps react-hook-form fields with shadcn primitives (Input, Label, FormMessage).
- **shadcn `skeleton`** — loading states across all new pages (alerts, account, system).
- **shadcn `pagination`** — alerts list pagination control.

**Risk (research finding 4):** Tailwind 4 + shadcn registry has "last-mile" install risk. Commit 1 in Phase 4 MUST be the dep install + smoke build (`pnpm build`) before any feature work starts. If install fails, fix it as a P0 before continuing.

---

## 7. Decision Docs Required

1. `docs/decisions/2026-05-16-strategy-templates-policy.md` (P1-POLICY) — amend or cut.
2. `docs/decisions/2026-05-16-dashboard-equity-chart.md` (P0-C) — build endpoint vs replace card. Drafted in Phase 3.2 plan; ratified there.

---

## 8. Verification Plan

- **Unit tests:** new backend handlers (soft-delete, account cache, optional equity-curve). Test count target: existing ~1933 → ~2000+.
- **Integration tests:** account-cache test asserts ≤1 IB connection per 30s under 10-concurrent-request load. Strategy soft-delete test asserts list excludes archived, detail includes archived, FK from backtest still resolves.
- **Lint + types:** `ruff check src/` + `mypy src/ --strict` clean.
- **Frontend lint:** `pnpm lint` + `tsc --noEmit` clean.
- **E2E (verify-e2e + Playwright MCP):** one use case per shipped surface, executed via the verify-e2e agent in Phase 5.4. Reports persisted to `tests/e2e/reports/`.
- **Playwright specs (CI-graduated):** authored in Phase 6.2c using selectors observed during verify-e2e. Run locally with `cd frontend && pnpm exec playwright test`.

---

## 9. Open Questions — RESOLVED via research

All seven open questions are now answered based on the research brief (`docs/research/2026-05-16-ui-completeness.md`).

1. **Dashboard equity chart fate (P0-C):** ✅ **RESOLVED — Drop the card entirely.** Per research finding 1, do NOT ship a fake `/live/equity-curve` endpoint. The data doesn't exist in Phase 1 (no per-deployment equity timeseries; backtest has one, live doesn't). Replace the dashboard's EquityChart slot with an Alerts feed card (P2-H). This is the honest answer.

2. **Strategy soft-delete: `deleted_at` vs `is_archived`:** ✅ **RESOLVED — `deleted_at: Mapped[datetime | None]`.** Per research finding 3. Use SQLAlchemy 2.0 global event listener `do_orm_execute` + `with_loader_criteria` for default filtering. Opt-out via `execution_options(include_deleted=True)` for backtest history. Additive migration + partial index `WHERE deleted_at IS NULL`.
3. **Notification preferences in `/settings`:** ✅ **RESOLVED — Drop entirely.** No backend exists. Faking was the original sin. Re-evaluate in a future PR when alerts mature.

4. **`/strategies` metrics columns:** ✅ **RESOLVED — Drop metric columns, replace with status + "Run backtest" CTA.** Real metrics live on the detail page derived from backtest history. Avoids N+1 fetch on list page + the "permanently --" lie.

5. **Strategy templates policy (P1-POLICY):** ✅ **RESOLVED — CUT backend.** Cut `api/strategy_templates.py` + `services/strategy_templates.py`. Phase 1 git-only is intentional. Pablo's workflow (`cp examples/<x>.py strategies/<new>.py && git add`) is not friction worth fixing. Dead-code liability removed. Re-evaluate when there are 20+ strategies + a team.

6. **Header notification poll cadence:** ✅ **RESOLVED — 60s.** Per research finding 6, polling cadences: 30s for IB account surfaces (aligned to probe), **60s for alerts**, 10-15s for live status when deployments are active. Header alerts badge uses 60s (matches alerts surface cadence).

7. **`research walk-forward` dialog mode (from CLI cross-check Q-7):** ✅ **RESOLVED — verify in Phase 4.** Implementer must check `/research` launch dialog for walk-forward support. If absent, add as part of P2 (small dialog extension). If present, no work needed.

## 9b. Additional Research-Derived Resolutions

These are NEW resolutions surfaced by the research brief and not in the original Open Questions:

A. **`useAuth` extension pattern (P0-B):** ✅ **Source-of-truth = backend `/api/v1/auth/me`**, NOT MSAL `account.idTokenClaims`. Per research finding 7. Implementation: a new `useUserProfile()` TanStack Query hook that fetches `/auth/me` after MSAL auth completes. The existing `useAuth()` hook stays MSAL-focused; the new hook joins with backend claims projection. Settings + header consume `useUserProfile()`.

B. **Playwright + MSAL `storageState`:** ✅ **Use `extraHTTPHeaders: { 'X-API-Key': process.env.TEST_API_KEY }` in `playwright.config.ts`.** Per research finding 5, MSAL `storageState` is documented-broken (microsoft/playwright#17328). Backend already accepts `X-API-Key` as Bearer alternative (per CLAUDE.md). CI activation requires a new GH secret `TEST_API_KEY` mapped to `MSAI_API_KEY`.

C. **TanStack Query mutation patterns:**

- Strategy config PATCH (P1-C): **optimistic** — `onMutate` updates cache; `onError` rolls back.
- Strategy delete (P1-E): **non-optimistic** — wait for 200, then invalidate. Reason: a 422 on delete (e.g., active deployment references the strategy) shouldn't leave the UI in an empty-state flash.
- Per research finding 6.

D. **Polling cadence cheat-sheet:**
| Surface | Cadence | Why |
| --- | --- | --- |
| `/api/v1/account/summary` + `/portfolio` + `/health` | 30s | Matches `_PROBE_INTERVAL_S` background refresh |
| `/api/v1/alerts/` | 60s | Read-mostly, no urgent reactivity |
| `/api/v1/live/status` | 10-15s | Only when ≥1 active deployment |
| `/api/v1/live/positions` + `/trades` | already WebSocket-streamed | unchanged |
| `/api/v1/system/health` (NEW) | 30s | Operator dashboard, parity with account |

---

## 10. Risks & Mitigations

| Risk                                                             | Likelihood                     | Mitigation                                                                                                                                                        |
| ---------------------------------------------------------------- | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Single-PR rollback re-introduces all 12 fakes if anything breaks | Medium                         | Mitigated by per-surface Playwright specs in Phase 6.2c. Each surface ships with its own E2E gate.                                                                |
| Codex review loop takes 8-10 iterations on a wide surface        | Medium                         | Per `feedback_dont_propose_time_based_stops`, accept the iteration cost. Memory note `code_review_iteration_discipline` says re-run reviewers on each fix commit. |
| Backend caching change causes regression in live trading         | Low — but high blast radius    | Council's binding ib_account caching constraint is in-scope here. Paper-IB drill in Phase 5.4 catches regressions. Operator (Pablo) drives the drill.             |
| Strategy soft-delete migration fails on existing data            | Low (no existing soft-deletes) | Migration is additive (NULL default); existing rows unaffected.                                                                                                   |
| Phase 1 templates policy decision delayed                        | Low                            | Decision doc is a Phase 3.2 deliverable, not a Phase 4 deliverable. Cuts ship cleanly with the rest of the PR.                                                    |

---

## 11. Success Metric

After this PR ships and auto-deploys to the Azure VM:

- **Zero hardcoded lies** in any UI element. Every visible data point traces to an API response.
- **Every API endpoint** in the audit's §3 has at least one UI consumer (or is explicitly CLI-only with a rationale).
- **Playwright CI** runs at least one spec per shipped UI route on every PR.
- **Pablo can use the UI for a full day without finding a lie.** Subjective but the operative test.

---

## 12.5 CLI → UI Cross-Check (Pablo directive 2026-05-16)

Per the user's "everything that can be done in API and CLI can be done in UI" requirement, I walked every `msai` CLI command and mapped to UI counterpart. Inventory: 13 sub-apps, 50+ commands.

| Sub-app     | Command                                                                                                        | UI Counterpart                                                  | Status                                         |
| ----------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ---------------------------------------------- |
| top-level   | `msai health`                                                                                                  | none                                                            | CLI-only — covered by N-8 system page below    |
| top-level   | `msai ingest`                                                                                                  | `/market-data` onboard flow                                     | ✅                                             |
| top-level   | `msai ingest-daily`                                                                                            | none                                                            | CLI-only by design (cron)                      |
| top-level   | `msai data-status`                                                                                             | none                                                            | **Gap — see P2-C** (market-data status card)   |
| top-level   | `msai whoami`                                                                                                  | header avatar                                                   | ✅ (partial — see P0-B for full role plumbing) |
| strategy    | `list`, `show`, `validate`, `edit`, `delete`                                                                   | `/strategies`, `/strategies/[id]`                               | ✅ after P1-C/D/E                              |
| backtest    | `run`, `history`, `show`, `report`, `trades`                                                                   | `/backtests`, `/backtests/[id]`                                 | ✅                                             |
| research    | `list`, `show`, `sweep`, `promote`                                                                             | `/research`, `/research/[id]`                                   | ✅                                             |
| research    | `cancel`                                                                                                       | none                                                            | **Gap — P2-B**                                 |
| research    | `walk-forward`                                                                                                 | TBD — does `/research` launch dialog support walk-forward mode? | **Open Q-7** (verify in Phase 3.2)             |
| live        | `start` (410), `stop`, `status`, `kill-all`, `resume`, `positions`, `trades`, `start-portfolio`, `portfolio-*` | `/live-trading`, `/live-trading/portfolio`                      | ✅                                             |
| live        | `audits`                                                                                                       | none                                                            | **Gap — P2-A** (audit drawer)                  |
| live        | `status-show <id>`                                                                                             | row-expand / drawer on `/live-trading`                          | likely covered; verify in Phase 3.2            |
| live        | `portfolio-draft-members`                                                                                      | `/live-trading/portfolio` compose flow                          | likely covered; verify                         |
| graduation  | `list`, `show`, `create`, `stage`                                                                              | `/graduation`                                                   | ✅                                             |
| portfolio   | `list`, `runs`, `show`, `run`, `create`, `run-show`, `run-report`                                              | `/portfolio`                                                    | ✅                                             |
| account     | `summary`                                                                                                      | dashboard card (partial) → `/account` (P1-B)                    | ✅ after P1-B                                  |
| account     | `positions`                                                                                                    | none                                                            | **Gap — P1-B Portfolio tab**                   |
| account     | `health`                                                                                                       | none                                                            | **Gap — P1-B Health tab + N-8 system page**    |
| system      | `health`                                                                                                       | none. `/settings` System Info card was fake (F-5).              | **NEW GAP N-8 — see below**                    |
| instruments | `refresh`, `bootstrap`                                                                                         | none                                                            | CLI-only by design (rare operator task; ok)    |
| alerts      | `list`                                                                                                         | none                                                            | **Gap — P1-A `/alerts` page**                  |
| auth        | `me`, `logout`                                                                                                 | header + settings                                               | ✅ after P0-A/B                                |
| market-data | `bars`, `symbols`, `ingest`                                                                                    | `/market-data`, `/market-data/chart`                            | ✅                                             |
| market-data | `status`                                                                                                       | none                                                            | **Gap — P2-C**                                 |
| template    | `list`, `scaffold`                                                                                             | none                                                            | **Blocked on P1-POLICY**                       |
| symbols     | (cli_symbols 6 cmds)                                                                                           | `/market-data` inventory + onboard                              | ✅                                             |

### NEW GAP N-8: System Health Page

The CLI `msai system health` returns API status, DB status, Redis status, IB Gateway status, worker queue depth, parquet storage health, version, commit SHA, uptime. The UI has **NO real system-health surface today** — only the fake settings card (F-5).

**Recommendation: ship a new `/system` page** as part of this PR. Trust-First design: each subsystem listed as a row with color + icon + text status + last-checked timestamp. Polling every 30s via TanStack Query. Replaces F-5's fakery with real telemetry.

Add to scope:

| ID       | Deliverable                                                                                                                                                                                                                                                                      | Affected files                                                                                                                                                                              |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P1-G** | `/system` page reading `GET /api/v1/system/health` (NEW backend endpoint) or `GET /health` extended. Lists subsystems (API, DB, Redis, IB Gateway, Workers, Parquet) with green/red status + last-checked + version metadata. Replaces the fake System Info card in `/settings`. | `frontend/src/app/system/page.tsx` (NEW), `frontend/src/components/system/*` (NEW), `frontend/src/components/layout/sidebar.tsx` (add nav), possibly `backend/src/msai/api/system.py` (NEW) |

### Open Question Q-7 (added)

`research walk-forward` is a distinct CLI command; the existing `/research` launch dialog should support walk-forward parameters (window size, step, train/test split). Verify in Phase 3.2; if missing, add to P1 or P2 depending on cost.

### CLI commands that stay CLI-only (with rationale)

- `msai ingest-daily` — cron-driven, no human invocation surface needed
- `msai instruments refresh` / `bootstrap` — rare operator task, run from terminal
- `msai whoami` — quick diagnostic; UI header already shows logged-in user

---

## 12. References

- Audit: `docs/audits/2026-05-16-ui-surface-audit.md`
- Council scope decision: `docs/decisions/2026-05-16-ui-completeness-scope.md`
- Memory rule (uncached IB connect): `feedback_use_playwright_mcp_for_ui_e2e`
- Memory rule (no time-based stops): `feedback_dont_propose_time_based_stops`
- Memory rule (rigor over cost): `feedback_dont_optimize_for_cost`
- CLAUDE.md "API-first, CLI-second, UI-third" ordering rule
- Nautilus gotchas #3, #6, #11 (IB connection management)
- Trust-First mode guidance from `/ui-design` skill
- shadcn primitives inventory: 20 components installed in `frontend/src/components/ui/`
