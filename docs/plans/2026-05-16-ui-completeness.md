# UI Completeness — Implementation Plan

**Goal:** Close every API/CLI capability gap in the MSAI v2 UI. Strip 12 user-visible lies. Add 4 new pages (`/alerts`, `/account`, `/system`, and one supporting decision-doc-cut). Fix 4 backend safety hazards. Author Playwright specs for every shipped surface. Ship a Trust-First + Product-UI dark dashboard that **works first, then looks great**.

**Architecture:** Single PR per user override (`docs/decisions/2026-05-16-ui-completeness-scope.md` §13). Frontend: extend Next.js 15 app-router + shadcn/ui + TanStack Query 5 + react-hook-form/zod. Backend: replace per-request IB connects with a singleton snapshot pattern aligned to existing `IBProbe` lifespan; add `deleted_at` soft-delete column with SQLAlchemy 2.0 global `do_orm_execute` event listener; cut `strategy_templates` contradictory feature; add new `/api/v1/system/health` aggregator endpoint.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy 2.0 + Alembic + ib_async (existing). Next.js 15 + React + shadcn/ui (20 primitives installed; 3 to add) + Tailwind 4 + TanStack Query 5 + Recharts + Geist font + OKLCH dark theme + Playwright/`@playwright/test`.

---

## Approach Comparison

### Chosen Default

**Single PR, audit-driven, working-phase-first ordering** (P0 stop lies → P1 CRUD parity → P2 polish), with research-validated technical choices (singleton IB snapshot, `deleted_at` event listener, cut templates feature, drop EquityChart card, X-API-Key Playwright fixture, react-hook-form/zod).

### Best Credible Alternative

**Staged PRs (council's recommendation):** Stage 1 = settings cleanup + alerts + strategy edit; Stage 2 = templates / market-data / account/portfolio. Smaller blast radius per PR, easier rollback.

### Scoring (fixed axes)

| Axis                  | Default (single PR) | Alternative (staged) |
| --------------------- | ------------------- | -------------------- |
| Complexity            | H                   | M                    |
| Blast Radius          | H                   | M                    |
| Reversibility         | M                   | H                    |
| Time to Validate      | M                   | M                    |
| User/Correctness Risk | M                   | M                    |

### Cheapest Falsifying Test

**N/A — user-locked direction.** Pablo directly overrode the council's staging recommendation in a follow-up message (audit decision-doc §13). The "single PR" approach is non-negotiable per user authority; the council's binding _technical_ constraints (ib_account caching, soft-delete, Phase 1 policy, Playwright spec graduation) are adopted in-PR rather than deferred. No spike needed because the question isn't "which is better" — the user has chosen and the technical constraints are absorbed.

## Contrarian Verdict

Phase 3.1c **PRE-DONE** per memory rule `feedback_skip_phase3_brainstorm_when_council_predone`. Council standalone-mode verdict: `docs/decisions/2026-05-16-ui-completeness-scope.md`. Chairman's binding technical constraints (verbatim):

- IB account endpoints must NOT reconnect per-request → adopted as B1/B3 below
- Strategy DELETE must move to soft-delete BEFORE UI delete CTA ships → adopted as B2/F11 below
- Templates scaffolder Phase 1 contradiction must be resolved → adopted as B5 (CUT)
- Playwright specs required for every shipped UI → adopted as Phase 6.2c (`P2-V1`)

User override (§13) of council staging adopted; single-PR shape locked.

---

## Files

### NEW (created)

**Backend (Python):**

- `backend/src/msai/api/system.py` — new router `/api/v1/system/health` aggregating subsystem statuses
- `backend/src/msai/services/ib_account_snapshot.py` — new singleton `IBAccountSnapshot` class wrapping a long-lived `IB()` connection + 30s refresh loop
- `backend/src/msai/services/ib_account_snapshot.py` exports `get_account_snapshot()` dependency for FastAPI handlers
- `backend/alembic/versions/<sha>_add_strategy_deleted_at.py` — additive migration: `ALTER TABLE strategies ADD COLUMN deleted_at TIMESTAMP NULL; CREATE INDEX ix_strategies_active ON strategies(id) WHERE deleted_at IS NULL;`
- `backend/src/msai/core/soft_delete.py` — new SQLAlchemy event listener module: `do_orm_execute` + `with_loader_criteria` for `Strategy` filtering

**Backend (cuts — deletions):**

- `backend/src/msai/api/strategy_templates.py` — DELETE (P1-POLICY decision: cut)
- `backend/src/msai/services/strategy_templates.py` — DELETE (same)
- `backend/src/msai/schemas/strategy_template.py` (if exists) — DELETE
- `backend/src/msai/cli.py` — remove `template_app` sub-app + `app.add_typer(template_app, ...)` (CLI parity with cut backend)

**Frontend pages (Next.js):**

- `frontend/src/app/alerts/page.tsx` — alerts list table
- `frontend/src/app/account/page.tsx` — tabbed Summary/Portfolio/Health
- `frontend/src/app/system/page.tsx` — system subsystem status grid
- `frontend/src/app/not-found.tsx` — global 404
- `frontend/src/app/error.tsx` — global 500 error boundary

**Frontend components:**

- `frontend/src/components/alerts/alerts-table.tsx`
- `frontend/src/components/alerts/alert-detail-sheet.tsx`
- `frontend/src/components/account/account-summary-card.tsx`
- `frontend/src/components/account/account-portfolio-table.tsx`
- `frontend/src/components/account/account-health-card.tsx`
- `frontend/src/components/system/subsystem-row.tsx`
- `frontend/src/components/system/version-info-card.tsx`
- `frontend/src/components/dashboard/alerts-feed.tsx` — replaces dashboard EquityChart slot
- `frontend/src/components/dashboard/storage-stats-card.tsx` — P2-C: market-data status card
- `frontend/src/components/strategies/strategy-edit-form.tsx`
- `frontend/src/components/strategies/strategy-delete-dialog.tsx`
- `frontend/src/components/live/audit-log-sheet.tsx` — P2-A: per-deployment audit drawer
- `frontend/src/components/layout/notifications-bell.tsx` — P2-E: header alerts badge
- `frontend/src/lib/hooks/use-user-profile.ts` — TanStack hook for `/api/v1/auth/me`
- `frontend/src/lib/hooks/use-alerts.ts` — TanStack hook for `/api/v1/alerts/`
- `frontend/src/lib/hooks/use-account.ts` — three hooks: `useAccountSummary`, `useAccountPortfolio`, `useAccountHealth`
- `frontend/src/lib/hooks/use-system-health.ts` — TanStack hook for `/api/v1/system/health`

**Frontend shadcn primitive installs:**

- `frontend/src/components/ui/form.tsx` — from shadcn registry
- `frontend/src/components/ui/skeleton.tsx` — from shadcn registry
- `frontend/src/components/ui/pagination.tsx` — from shadcn registry

**Test files:**

- `backend/tests/integration/test_ib_account_snapshot.py` — asserts ≤1 IB connection per 30s under 10-concurrent-request load
- `backend/tests/integration/test_strategy_soft_delete.py` — list excludes archived, detail includes archived, backtest FK still resolves
- `backend/tests/integration/test_system_health.py` — aggregator endpoint returns all subsystem statuses
- `backend/tests/unit/test_soft_delete_listener.py` — event listener filters correctly + opt-out works
- `frontend/tests/e2e/specs/alerts.spec.ts`
- `frontend/tests/e2e/specs/account.spec.ts`
- `frontend/tests/e2e/specs/system.spec.ts`
- `frontend/tests/e2e/specs/settings.spec.ts`
- `frontend/tests/e2e/specs/strategies-crud.spec.ts`
- `frontend/tests/e2e/specs/dashboard.spec.ts`
- `frontend/tests/e2e/specs/live-audit-drawer.spec.ts`
- `frontend/tests/e2e/specs/research-cancel.spec.ts`
- `frontend/tests/e2e/specs/error-pages.spec.ts`

**CI:**

- `.github/workflows/e2e.yml` — copy from `docs/ci-templates/e2e.yml` + Tailwind 4 / shadcn registry build smoke

**Decision docs:**

- `docs/decisions/2026-05-16-strategy-templates-policy.md` — CUT verdict
- `docs/decisions/2026-05-16-dashboard-equity-chart.md` — DROP verdict (no fake endpoint)

### MODIFIED

**Backend:**

- `backend/src/msai/api/account.py` — handlers serve from `IBAccountSnapshot` cache; remove direct `_ib_service` connect calls
- `backend/src/msai/services/ib_account.py` — delete `_make_client_id_counter` + per-request `IB.connectAsync`; or wrap behind snapshot. Verify in commit that no other module imports the old API.
- `backend/src/msai/api/strategies.py` — DELETE handler sets `deleted_at`; list filter inherits from global listener; detail opts in for include-archived when called via backtest detail path
- `backend/src/msai/models/strategy.py` — add `deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=False)` + register with global filter
- `backend/src/msai/main.py` — register new `system_router`; deregister `strategy_templates_router`; register `soft_delete` event listener at app startup
- `backend/src/msai/cli.py` — remove `template_app` declarations + `add_typer` line

**Frontend:**

- `frontend/src/app/settings/page.tsx` — **near-total rewrite**. Strip System Information card, Notifications card, Danger Zone card. Keep Profile card wired to `useUserProfile()` (real `role`, `display_name`, `email`, `name`). No fakes survive.
- `frontend/src/app/dashboard/page.tsx` — replace `<EquityChart data={[]} />` with `<AlertsFeed />`; pass `accountData` from snapshot-backed `useAccountSummary()`
- `frontend/src/app/strategies/page.tsx` — pass real `status` from `/live/status` join; drop Sharpe/Return/WinRate metric columns from `StrategyCard`
- `frontend/src/app/strategies/[id]/page.tsx` — wire `Validate` to real `POST /validate` endpoint; replace plain textarea with react-hook-form + zod form; add `<StrategyDeleteDialog>` (P1-E)
- `frontend/src/app/live-trading/page.tsx` — add `<AuditLogSheet>` trigger per deployment row
- `frontend/src/app/research/[id]/page.tsx` — add Cancel CTA for running jobs (`POST /research/jobs/{id}/cancel`)
- `frontend/src/app/market-data/page.tsx` — add `<StorageStatsCard>` to page header
- `frontend/src/components/strategies/strategy-card.tsx` — drop metric columns; add status badge + "Last run" (from backtest history) + "Run Backtest" CTA
- `frontend/src/components/dashboard/portfolio-summary.tsx` — drop "Total Return `--`" StatCard; drive `trend` from value sign on remaining cards
- `frontend/src/components/layout/sidebar.tsx` — add `Alerts`, `Account`, `System` nav entries (3 new)
- `frontend/src/components/layout/header.tsx` — add `<NotificationsBell />` with TanStack 60s polling
- `frontend/src/components/providers.tsx` — wrap children with `<UserProfileProvider>` (lifts `useUserProfile` result to context)
- `frontend/src/lib/auth.ts` — keep MSAL-focused; do NOT extend `AuthUser` (research-validated: use separate `useUserProfile()` hook instead)
- `frontend/src/lib/api.ts` — add typed fetchers: `getAlerts`, `getAccountPortfolio`, `getAccountHealth`, `getSystemHealth`, `patchStrategy`, `validateStrategy`, `deleteStrategy`, `getLiveAudits`, `cancelResearchJob`, `getUserProfile`
- `frontend/playwright.config.ts` — add `extraHTTPHeaders: { 'X-API-Key': process.env.TEST_API_KEY }` to `use` block (research finding 5)
- `frontend/tests/e2e/fixtures/auth.ts` — switch from MSAL storageState to X-API-Key header pattern
- `frontend/package.json` — add deps `react-hook-form`, `@hookform/resolvers`, `zod`

---

## Tasks

Tasks are numbered T1..T26. Backend safety + dep-install foundation work serializes; UI per-surface tasks parallelize where Writes paths are disjoint.

### Foundation (commit 1)

**T1 — Dep install + smoke build** (`pnpm add react-hook-form @hookform/resolvers zod`; `pnpm exec shadcn@latest add form skeleton pagination`; `pnpm build`). Verifies Tailwind 4 + shadcn registry compatibility before any feature work. Writes: `frontend/package.json`, `frontend/pnpm-lock.yaml`, `frontend/src/components/ui/form.tsx`, `frontend/src/components/ui/skeleton.tsx`, `frontend/src/components/ui/pagination.tsx`. Depends on: —.

### Backend safety (commits 2-4)

**T2 — IB account singleton snapshot.** Implement `services/ib_account_snapshot.py` with one `IB()`, static `client_id=900`, 30s refresh loop. Wire into FastAPI lifespan (alongside existing `IBProbe`). Update `api/account.py` handlers to serve from snapshot. Delete unused per-request connect path in `services/ib_account.py`. Writes: `backend/src/msai/services/ib_account_snapshot.py` (NEW), `backend/src/msai/services/ib_account.py`, `backend/src/msai/api/account.py`, `backend/src/msai/main.py`. Tests: `backend/tests/integration/test_ib_account_snapshot.py`. Depends on: —.

**T3 — Soft-delete migration + event listener.** Add `deleted_at` column via Alembic. Implement `core/soft_delete.py` with `do_orm_execute` listener + `with_loader_criteria(Strategy, lambda s: s.deleted_at.is_(None))`. Register listener at startup. Update `api/strategies.py` DELETE handler. Backtest detail path uses `execution_options(include_deleted=True)`. Writes: `backend/alembic/versions/<sha>_add_strategy_deleted_at.py` (NEW), `backend/src/msai/core/soft_delete.py` (NEW), `backend/src/msai/models/strategy.py`, `backend/src/msai/api/strategies.py`, `backend/src/msai/main.py`. Tests: `backend/tests/integration/test_strategy_soft_delete.py`, `backend/tests/unit/test_soft_delete_listener.py`. Depends on: —.

**T4 — System health aggregator + cut templates.** Implement `api/system.py` aggregating subsystem statuses (DB ping, Redis ping, IB Gateway probe state, worker queue depth, parquet storage stats, version + commit SHA + uptime). Register in `main.py`. **Cut `api/strategy_templates.py` + `services/strategy_templates.py`** + corresponding CLI sub-app in `cli.py`. Write decision-doc `docs/decisions/2026-05-16-strategy-templates-policy.md` recording the CUT. Writes: `backend/src/msai/api/system.py` (NEW), `backend/src/msai/main.py`, `backend/src/msai/api/strategy_templates.py` (DELETE), `backend/src/msai/services/strategy_templates.py` (DELETE), `backend/src/msai/cli.py`, `docs/decisions/2026-05-16-strategy-templates-policy.md` (NEW). Tests: `backend/tests/integration/test_system_health.py`. Depends on: —.

### Frontend foundation (commits 5-6)

**T5 — Typed API client extensions.** Add typed fetchers in `frontend/src/lib/api.ts`: `getAlerts`, `getAccountPortfolio`, `getAccountHealth`, `getSystemHealth`, `patchStrategy`, `validateStrategy`, `deleteStrategy`, `getLiveAudits`, `cancelResearchJob`, `getUserProfile`. Plus mirror types from new backend response schemas. Writes: `frontend/src/lib/api.ts`. Depends on: T1, T2, T3, T4 (need backend shapes to mirror).

**T6 — `useUserProfile()` TanStack hook + provider.** Implement `lib/hooks/use-user-profile.ts` fetching `/api/v1/auth/me`. Wrap providers tree with `<UserProfileProvider>`. Keep MSAL `useAuth()` untouched. Writes: `frontend/src/lib/hooks/use-user-profile.ts` (NEW), `frontend/src/components/providers.tsx`. Depends on: T5.

### P0 — Stop the lies (commits 7-9)

**T7 — `/settings` rewrite (P0-A).** Delete System Information card, Notifications card, Danger Zone card. Keep only Profile card wired to `useUserProfile()` showing real `name`, `email`, `role`, `display_name`. Add aria-busy skeleton during load. Writes: `frontend/src/app/settings/page.tsx`. Depends on: T6.

**T8 — Dashboard EquityChart removal + AlertsFeed (P0-C, P0-D, P2-H combined).** Drop `<EquityChart>` slot entirely. Add `<AlertsFeed limit={5} />` showing last 5 alerts from `useAlerts()`. Drop "Total Return `--`" StatCard from `<PortfolioSummary>`. Drive `trend` from value sign on remaining cards. Writes: `frontend/src/app/dashboard/page.tsx`, `frontend/src/components/dashboard/portfolio-summary.tsx`, `frontend/src/components/dashboard/alerts-feed.tsx` (NEW), `docs/decisions/2026-05-16-dashboard-equity-chart.md` (NEW). Depends on: T1, T5, T6, T14 (needs `useAlerts` from T14).

**T9 — Sidebar nav additions.** Add `Alerts`, `Account`, `System` entries to sidebar nav. Maintain icon + label + isActive pattern. Writes: `frontend/src/components/layout/sidebar.tsx`. Depends on: —.

### P1 — CRUD parity (commits 10-15)

**T10 — `/alerts` page + table + detail sheet (P1-A).** New route. shadcn `Table` with severity icon (color + icon + text per Trust-First), ISO timestamp, alert code, expandable detail (shadcn `Sheet`). Empty state ("All quiet — no recent alerts" + last-checked). Pagination via shadcn `pagination`. 60s TanStack polling. Writes: `frontend/src/app/alerts/page.tsx` (NEW), `frontend/src/components/alerts/alerts-table.tsx` (NEW), `frontend/src/components/alerts/alert-detail-sheet.tsx` (NEW), `frontend/src/lib/hooks/use-alerts.ts` (NEW). Depends on: T5.

**T11 — `/account` page + Summary/Portfolio/Health tabs (P1-B).** New route. shadcn `Tabs`. Summary tab uses existing `useAccountSummary` (now snapshot-backed via T2). Portfolio tab consumes `getAccountPortfolio()`. Health tab consumes `getAccountHealth()` (reads cached probe state). Manual Refresh button + 30s background poll. Writes: `frontend/src/app/account/page.tsx` (NEW), `frontend/src/components/account/account-summary-card.tsx` (NEW), `frontend/src/components/account/account-portfolio-table.tsx` (NEW), `frontend/src/components/account/account-health-card.tsx` (NEW), `frontend/src/lib/hooks/use-account.ts` (NEW). Depends on: T2 (backend snapshot must exist), T5.

**T12 — `/system` page (P1-G / N-8).** New route. Subsystem grid (DB, Redis, IB Gateway, Workers, Parquet) + version card (version + commit SHA + uptime). 30s polling. Replaces fakery from former `/settings` System Information card. Writes: `frontend/src/app/system/page.tsx` (NEW), `frontend/src/components/system/subsystem-row.tsx` (NEW), `frontend/src/components/system/version-info-card.tsx` (NEW), `frontend/src/lib/hooks/use-system-health.ts` (NEW). Depends on: T4 (backend endpoint), T5.

**T13 — Strategy edit form (P1-C, F-7 fix).** Add react-hook-form + zod schema for `{name, description, default_config}`. Replace plain Textarea with `<StrategyEditForm>`. Optimistic mutation via TanStack `useMutation` with `onMutate` cache update + `onError` rollback. Success toast. Writes: `frontend/src/components/strategies/strategy-edit-form.tsx` (NEW), `frontend/src/app/strategies/[id]/page.tsx`. Depends on: T1, T5.

**T14 — Strategy validate fix (P1-D, F-6 fix).** Replace local `JSON.parse()` Validate button with real `POST /api/v1/strategies/{id}/validate` call. Show backend response (success message OR import error) in dialog. Writes: `frontend/src/app/strategies/[id]/page.tsx`. Depends on: T5, T13 (same file; serialize).

**T15 — Strategy delete dialog (P1-E).** Add `<StrategyDeleteDialog>` using shadcn `AlertDialog`. "Type strategy name to confirm" friction input. Non-optimistic mutation: wait for 200, then invalidate `strategies` cache + toast + redirect. Writes: `frontend/src/components/strategies/strategy-delete-dialog.tsx` (NEW), `frontend/src/app/strategies/[id]/page.tsx`. Depends on: T3 (backend soft-delete must ship first), T5, T14 (same page file; serialize).

### P2 — Polish + observability (commits 16-22)

**T16 — Live audit log drawer (P2-A, M-4).** Add `<AuditLogSheet>` trigger button per deployment row on `/live-trading`. Reads `GET /api/v1/live/audits/{id}`. Writes: `frontend/src/components/live/audit-log-sheet.tsx` (NEW), `frontend/src/app/live-trading/page.tsx`. Depends on: T5.

**T17 — Research cancel CTA (P2-B, M-5).** Add Cancel button on `/research/[id]` for running jobs. shadcn `AlertDialog` confirm. POSTs to `/api/v1/research/jobs/{id}/cancel`. Writes: `frontend/src/app/research/[id]/page.tsx`. Depends on: T5.

**T18 — Market-data storage stats card (P2-C, M-6).** Add `<StorageStatsCard>` to `/market-data` page header showing file count + bytes + asset-class breakdown from `GET /api/v1/market-data/status`. Writes: `frontend/src/components/dashboard/storage-stats-card.tsx` (NEW), `frontend/src/app/market-data/page.tsx`. Depends on: T5.

**T19 — Header notifications bell (P2-E).** Add `<NotificationsBell />` to header showing unread alert count from `useAlerts()` (filter by `read_at IS NULL` if backend supports; else show top-N count). Click → `/alerts`. 60s polling. Writes: `frontend/src/components/layout/notifications-bell.tsx` (NEW), `frontend/src/components/layout/header.tsx`. Depends on: T10 (needs `useAlerts`).

**T20 — `/strategies` redesign (P2-F + F-12 fix).** Drop metric columns from `StrategyCard`. Replace with: real status badge from `/live/status` join, "Last run" timestamp from backtest history (cheapest: latest backtest per strategy via single-page fetch + group-by), "Run Backtest" CTA. Skeleton loaders on page load. Helpful empty state with "How to add a strategy" link. Writes: `frontend/src/app/strategies/page.tsx`, `frontend/src/components/strategies/strategy-card.tsx`. Depends on: T1 (skeleton primitive).

**T21 — Global error pages (P2-G).** `app/not-found.tsx` (404 styled — MSAI logo + "Page not found" + back-to-dashboard) and `app/error.tsx` (500 error boundary — "Something went wrong" + retry CTA + minimal error detail). Writes: `frontend/src/app/not-found.tsx` (NEW), `frontend/src/app/error.tsx` (NEW). Depends on: —.

**T22 — Symbol readiness widget (P2-D, M-7).** Pre-trade readiness check on `/live-trading/portfolio` compose flow. Calls `GET /api/v1/symbols/readiness`. Shows per-instrument status before allowing start-portfolio. Writes: `frontend/src/app/live-trading/portfolio/page.tsx`, possibly `frontend/src/components/live-portfolio/readiness-check.tsx` (NEW). Depends on: T5.

### Verification scaffolding (commits 23-25)

**T23 — Playwright config + auth fixture switch.** Switch `playwright.config.ts` to use `extraHTTPHeaders: { 'X-API-Key': process.env.TEST_API_KEY }`. Update `tests/e2e/fixtures/auth.ts` to no longer rely on storageState. Document `TEST_API_KEY` env var requirement. Writes: `frontend/playwright.config.ts`, `frontend/tests/e2e/fixtures/auth.ts`. Depends on: —.

**T24 — Playwright specs.** Author one `.spec.ts` per shipped surface using observed selectors from Phase 5.4 verify-e2e reports. Tag smoke paths with `@smoke`. Writes: `frontend/tests/e2e/specs/{alerts,account,system,settings,strategies-crud,dashboard,live-audit-drawer,research-cancel,error-pages}.spec.ts` (9 NEW). Depends on: T23 + all P0/P1/P2 surface tasks (T7-T22).

**T25 — CI workflow activation.** Copy `docs/ci-templates/e2e.yml` → `.github/workflows/e2e.yml`. Add `TEST_API_KEY` to required secrets. Smoke specs on PR, full suite nightly. Writes: `.github/workflows/e2e.yml` (NEW). Depends on: T24.

### Polish + memory

**T26 — Memory + CHANGELOG updates.** Update `docs/CHANGELOG.md` "Unreleased" with summary. Save 1-2 memory entries: (a) the council-override-to-single-PR pattern, (b) the singleton IB snapshot pattern (general reusable IB connection-management lesson). Writes: `docs/CHANGELOG.md`, memory files. Depends on: T1-T25.

---

## Dispatch Plan

Per Phase 4.0 file-disjoint rule. Concurrency cap 3.

| Task | Depends on    | Writes (concrete paths)                                                                                                                                                                                                                                        |
| ---- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1   | —             | `frontend/package.json`, `frontend/pnpm-lock.yaml`, `frontend/src/components/ui/{form,skeleton,pagination}.tsx`                                                                                                                                                |
| T2   | —             | `backend/src/msai/services/{ib_account_snapshot,ib_account}.py`, `backend/src/msai/api/account.py`, `backend/src/msai/main.py`                                                                                                                                 |
| T3   | —             | `backend/alembic/versions/<sha>_add_strategy_deleted_at.py`, `backend/src/msai/core/soft_delete.py`, `backend/src/msai/models/strategy.py`, `backend/src/msai/api/strategies.py`, `backend/src/msai/main.py`                                                   |
| T4   | —             | `backend/src/msai/api/system.py`, `backend/src/msai/api/strategy_templates.py` (DEL), `backend/src/msai/services/strategy_templates.py` (DEL), `backend/src/msai/cli.py`, `backend/src/msai/main.py`, `docs/decisions/2026-05-16-strategy-templates-policy.md` |
| T5   | T1,T2,T3,T4   | `frontend/src/lib/api.ts`                                                                                                                                                                                                                                      |
| T6   | T5            | `frontend/src/lib/hooks/use-user-profile.ts`, `frontend/src/components/providers.tsx`                                                                                                                                                                          |
| T7   | T6            | `frontend/src/app/settings/page.tsx`                                                                                                                                                                                                                           |
| T9   | —             | `frontend/src/components/layout/sidebar.tsx`                                                                                                                                                                                                                   |
| T10  | T5            | `frontend/src/app/alerts/page.tsx`, `frontend/src/components/alerts/{alerts-table,alert-detail-sheet}.tsx`, `frontend/src/lib/hooks/use-alerts.ts`                                                                                                             |
| T11  | T2,T5         | `frontend/src/app/account/page.tsx`, `frontend/src/components/account/{account-summary-card,account-portfolio-table,account-health-card}.tsx`, `frontend/src/lib/hooks/use-account.ts`                                                                         |
| T12  | T4,T5         | `frontend/src/app/system/page.tsx`, `frontend/src/components/system/{subsystem-row,version-info-card}.tsx`, `frontend/src/lib/hooks/use-system-health.ts`                                                                                                      |
| T8   | T1,T5,T6,T14¹ | `frontend/src/app/dashboard/page.tsx`, `frontend/src/components/dashboard/{portfolio-summary,alerts-feed}.tsx`, `docs/decisions/2026-05-16-dashboard-equity-chart.md`                                                                                          |
| T13  | T1,T5         | `frontend/src/components/strategies/strategy-edit-form.tsx`, `frontend/src/app/strategies/[id]/page.tsx`                                                                                                                                                       |
| T14  | T5,T13        | `frontend/src/app/strategies/[id]/page.tsx` (same file as T13 — serialized)                                                                                                                                                                                    |
| T15  | T3,T5,T14     | `frontend/src/components/strategies/strategy-delete-dialog.tsx`, `frontend/src/app/strategies/[id]/page.tsx` (same — serialized)                                                                                                                               |
| T16  | T5            | `frontend/src/components/live/audit-log-sheet.tsx`, `frontend/src/app/live-trading/page.tsx`                                                                                                                                                                   |
| T17  | T5            | `frontend/src/app/research/[id]/page.tsx`                                                                                                                                                                                                                      |
| T18  | T5            | `frontend/src/components/dashboard/storage-stats-card.tsx`, `frontend/src/app/market-data/page.tsx`                                                                                                                                                            |
| T19  | T10           | `frontend/src/components/layout/notifications-bell.tsx`, `frontend/src/components/layout/header.tsx`                                                                                                                                                           |
| T20  | T1            | `frontend/src/app/strategies/page.tsx`, `frontend/src/components/strategies/strategy-card.tsx`                                                                                                                                                                 |
| T21  | —             | `frontend/src/app/{not-found,error}.tsx`                                                                                                                                                                                                                       |
| T22  | T5            | `frontend/src/app/live-trading/portfolio/page.tsx`, `frontend/src/components/live-portfolio/readiness-check.tsx`                                                                                                                                               |
| T23  | —             | `frontend/playwright.config.ts`, `frontend/tests/e2e/fixtures/auth.ts`                                                                                                                                                                                         |
| T24  | T23,T7..T22   | `frontend/tests/e2e/specs/{alerts,account,system,settings,strategies-crud,dashboard,live-audit-drawer,research-cancel,error-pages}.spec.ts`                                                                                                                    |
| T25  | T24           | `.github/workflows/e2e.yml`                                                                                                                                                                                                                                    |
| T26  | T1-T25        | `docs/CHANGELOG.md`, memory files                                                                                                                                                                                                                              |

¹ T8 depends on T14 only for the `<AlertsFeed>` reusing the same `useAlerts` hook that T10 builds. Re-stated: T8 depends on T10 (NOT T14). Correcting:

**T8 corrected dependency:** T1, T5, T6, T10.

### Scheduling

- **Wave 1 (parallel):** T1, T2, T3, T4 — all foundation tasks have disjoint Writes.
- **Wave 2 (serial):** T5 — depends on all of Wave 1.
- **Wave 3 (parallel):** T6, T9, T21, T23 — disjoint writes after foundation.
- **Wave 4 (parallel up to 3 concurrent):** T7 (after T6), T10, T11 (after T2), T12, T16, T17, T18, T20, T22.
- **Wave 5 (serial on `strategies/[id]/page.tsx`):** T13 → T14 → T15.
- **Wave 6:** T8 (after T10), T19 (after T10).
- **Wave 7 (after all surfaces complete):** verify-e2e in Phase 5.4 → T24 (using observed selectors) → T25.
- **Wave 8:** T26 polish.

**Sequential override:** No — most tasks have disjoint writes. The `strategies/[id]/page.tsx` cluster (T13-T15) is the only serialization point. T8 depends on T10's hook; T19 depends on T10's hook; both serialize behind T10.

---

## Implementation Notes

### Trust-First mode (UI design skill)

Every new screen MUST:

- Use shadcn primitives only — NO raw HTML buttons/inputs/dialogs.
- Color + icon + text for all status (never color alone). E.g., subsystem status row: `🟢 Healthy` (text), green dot, "Last checked 12s ago".
- Plain language status. No "ERR_502_UPSTREAM_TIMEOUT" — translate to "API unreachable. Last seen 2 minutes ago."
- Loading states: shadcn `Skeleton` (installed in T1) with `aria-busy`.
- Empty states: helpful copy + a primary action ("All quiet — no recent alerts. Check back in a few minutes.")
- Error states: explicit, retry-able. Per-component error UI inline; global `error.tsx` for crashes.
- Confirmation friction on destructive actions (strategy delete = type-name-to-confirm).
- WCAG AA contrast minimum; AAA on critical paths (account balances, strategy delete confirms).
- `prefers-reduced-motion` respected on every transition.

### Product-UI density

- Dense but scannable: shadcn `Table` for alerts + account portfolio with sticky headers + sortable columns.
- Sidebar nav consistent across all surfaces — add 3 entries (Alerts, Account, System), keep existing 9.
- Breadcrumbs not required (sidebar already shows location).
- Tabs for multi-view (`/account` Summary/Portfolio/Health).
- Drawers (shadcn `Sheet`) for detail views (alert detail, live audit log).
- Keyboard navigability: shadcn primitives handle this out of the box (focus rings, Tab order).

### Backend safety pattern (research finding 2)

```python
# services/ib_account_snapshot.py — pattern outline
class IBAccountSnapshot:
    def __init__(self):
        self._ib: IB = IB()
        self._client_id = 900  # STATIC. No counter.
        self._summary: AccountSummary | None = None
        self._portfolio: list[Position] | None = None
        self._refresh_task: asyncio.Task | None = None

    async def start(self):
        await self._ib.connectAsync(host=..., port=..., clientId=self._client_id)
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        while True:
            try:
                self._summary = await self._fetch_summary()
                self._portfolio = await self._fetch_portfolio()
            except Exception as e:
                log.warning("snapshot_refresh_failed", error=str(e))
            await asyncio.sleep(_PROBE_INTERVAL_S)  # 30s, aligned

    async def stop(self):
        if self._refresh_task: self._refresh_task.cancel()
        if self._ib.isConnected(): self._ib.disconnect()

# Wire to FastAPI lifespan:
@asynccontextmanager
async def lifespan(app: FastAPI):
    snapshot = IBAccountSnapshot()
    await snapshot.start()
    app.state.ib_account_snapshot = snapshot
    yield
    await snapshot.stop()

# Use in handler:
@router.get("/summary")
async def summary(snapshot: IBAccountSnapshot = Depends(get_snapshot)):
    return snapshot._summary or AccountSummaryZero()  # graceful degrade
```

Test asserts: 10 concurrent `/account/summary` requests → exactly 1 IB connection in `IBProbe`-style logs.

### Soft-delete pattern (research finding 3)

```python
# core/soft_delete.py
from sqlalchemy import event, with_loader_criteria
from msai.models.strategy import Strategy

def _filter_soft_deleted(execute_state):
    if execute_state.is_select and not execute_state.execution_options.get("include_deleted", False):
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(Strategy, lambda s: s.deleted_at.is_(None))
        )

def register_listeners(engine):
    event.listen(SessionEvents, "do_orm_execute", _filter_soft_deleted)
```

Backtest detail handler opts in: `session.execute(stmt, execution_options={"include_deleted": True})`.

### Dashboard `EquityChart` cut rationale (decision-doc P0-C)

Phase 1 has no per-deployment equity timeseries. Live trades arrive via WebSocket and are persisted to `live_trades` but no daily aggregation job exists. Building a fake `/live/equity-curve` endpoint that sums realized PnL from `live_trades` would:

- Show a partial chart (unrealized PnL not included → misleading curves).
- Require a backfill for any pre-existing trades (operator burden).
- Look identical to the backtest equity curve at a glance, but with different data semantics → user confusion.

**Verdict:** Drop the dashboard chart entirely; replace the slot with the Alerts feed (P2-H). Revisit live equity tracking as a standalone feature once daily-aggregation is required.

### Phase 1 templates policy cut rationale (decision-doc P1-POLICY)

CLAUDE.md "Key Design Decisions" line: _strategies are Python files in `strategies/` dir (no UI uploads in Phase 1 — git-only)_. The existing `services/strategy_templates.py` violates this silently — no UI consumer means no one has noticed.

**Verdict:** CUT the backend feature. Pablo's workflow (`cp examples/<x>.py strategies/<new>.py && git add`) is not friction worth fixing in Phase 1. Re-evaluate when there are 20+ strategies + a team. Dead code is a maintenance liability.

Affected: delete `api/strategy_templates.py`, `services/strategy_templates.py`, `schemas/strategy_template.py` (if exists), `template_app` sub-app in CLI, any `strategy_templates_router` reference in `main.py`.

### Playwright + MSAL workaround (research finding 5)

```ts
// playwright.config.ts
export default defineConfig({
  use: {
    extraHTTPHeaders: {
      "X-API-Key": process.env.TEST_API_KEY ?? "",
    },
  },
});
```

Backend already accepts `X-API-Key` (`MSAI_API_KEY`) as Bearer alternative per CLAUDE.md. CI requires new GH secret `TEST_API_KEY`.

---

## E2E Use Cases

Mode: **fullstack** (API + UI). API-first ordering per `CLAUDE.md` `## E2E Configuration`.

### UC-1: User views their real role and email in Settings (P0-A, P0-B, F-2 fix)

**Interface:** UI (Playwright MCP via verify-e2e)
**Setup:** ARRANGE — sign in via X-API-Key fixture; the test user already exists in DB with `role="viewer"`.
**Steps:**

1. Navigate to `/settings`
2. Wait for profile card to load (skeleton resolves)

**Verification:**

1. Role badge shows `viewer` (NOT `Admin`)
2. Email shows the test user's email (NOT `demo@msai.dev`)
3. No text "Admin" anywhere on the page
4. No "System Information" card visible
5. No "Notifications" card visible
6. No "Danger Zone" / "Clear All Data" button visible

**Persistence:** Reload page. Same role + email visible.

### UC-2: User views the alerts list and detail (P1-A, M-1)

**Interface:** API+UI
**Setup:** ARRANGE via API: `GET /api/v1/alerts/` to confirm endpoint returns; no alert creation API (alerts are emitted by backend); use whatever alerts exist OR seed via a documented backend trigger.
**Steps:**

1. Navigate to `/alerts`
2. Click on the first alert row

**Verification:**

1. Table renders with severity icon + ISO timestamp + alert code + message
2. Detail sheet opens with full payload
3. Severity color matches icon AND text label
4. Empty state if no alerts: "All quiet — no recent alerts" + last-checked timestamp

**Persistence:** Reload. Same alerts visible (or same empty state).

### UC-3: User sees real account data on the Account page (P1-B, M-2, M-3)

**Interface:** API+UI
**Setup:** Paper IB account `DU...` is reachable on port 4004 (per `CLAUDE.md` IB Gateway config).
**Steps:**

1. Navigate to `/account`
2. Verify Summary tab is active
3. Click Portfolio tab
4. Click Health tab
5. Click Refresh button

**Verification:**

1. Summary shows real `net_liquidation`, `buying_power`, `available_funds`, `margin_used`, `unrealized_pnl`, `realized_pnl` from `IBAccountSnapshot`.
2. Portfolio tab shows positions table.
3. Health tab shows IB Gateway connection state (color + icon + "Connected" / "Disconnected" + last-checked).
4. Refresh button triggers a refetch; new timestamp visible.

**Persistence:** Reload. Same data.

### UC-4: System health page reflects real subsystem statuses (P1-G, N-8)

**Interface:** API+UI
**Setup:** Stack running (`docker compose -f docker-compose.dev.yml up -d`).
**Steps:**

1. Navigate to `/system`
2. Read each subsystem row

**Verification:**

1. Each subsystem row has color + icon + plain-language status text + last-checked timestamp.
2. Version + commit SHA + uptime card shows real values (not "v0.1.0" / "5d 14h 32m").

**Persistence:** Reload. Same subsystem states.

### UC-5: User edits a strategy config and persists (P1-C, F-7 fix)

**Interface:** API+UI
**Setup:** A strategy exists in the registry (any of the 5 git-only strategies).
**Steps:**

1. Navigate to `/strategies/<id>`
2. Click into the config editor field
3. Modify a JSON config value (e.g., `slow_ema_period: 50` → `slow_ema_period: 60`)
4. Click Save

**Verification:**

1. Success toast "Strategy updated" appears.
2. Backend PATCH `/api/v1/strategies/{id}` returns 200.
3. Reload page → modified value persists.

**Persistence (re-verify):** Reload again. Modified value still present.

### UC-6: User validates a strategy (P1-D, F-6 fix)

**Interface:** API+UI
**Setup:** Strategy exists.
**Steps:**

1. Navigate to `/strategies/<id>`
2. Click Validate button

**Verification:**

1. Dialog opens showing real validation result from `POST /api/v1/strategies/{id}/validate` (NOT a local JSON.parse).
2. Dialog text reflects backend response: "Strategy validated successfully" OR "Validation failed: <import error>".

### UC-7: User deletes a strategy (P1-E)

**Interface:** API+UI
**Setup:** A test strategy exists (one not referenced by any active deployment).
**Steps:**

1. Navigate to `/strategies/<test-id>`
2. Click Delete button
3. AlertDialog opens; type strategy name in confirm input
4. Click "Delete Strategy" confirm button

**Verification:**

1. Backend DELETE `/api/v1/strategies/{id}` returns 200.
2. Toast "Strategy archived" appears.
3. Redirect to `/strategies`.
4. Strategy is no longer in the list.

**Persistence:** Reload `/strategies`. Strategy still absent. (But: GET `/api/v1/strategies/{id}` directly STILL returns the row for historical reference.)

### UC-8: Dashboard EquityChart is gone; Alerts feed visible (P0-C, P2-H)

**Interface:** UI
**Setup:** Authenticated.
**Steps:**

1. Navigate to `/dashboard`

**Verification:**

1. No EquityChart card present (no "Portfolio Performance" title).
2. AlertsFeed card present showing last 5 alerts (or empty state).
3. PortfolioSummary has 3 StatCards (Total Value, Daily P&L, Active Strategies) — NOT 4. No "Total Return `--`" card.
4. Trend arrows on cards reflect actual value signs.

### UC-9: Live audit drawer (P2-A, M-4)

**Interface:** API+UI
**Setup:** At least one deployment exists in DB (can be `stopped` status).
**Steps:**

1. Navigate to `/live-trading`
2. Click "Audit Log" button on a deployment row

**Verification:**

1. Sheet opens.
2. Audit events render with timestamps + event type + payload preview.

### UC-10: Research job cancel (P2-B, M-5)

**Interface:** API+UI
**Setup:** A running research job (sweep started but not completed).
**Steps:**

1. Navigate to `/research/<job-id>`
2. Click Cancel button
3. Confirm in AlertDialog

**Verification:**

1. POST `/api/v1/research/jobs/{id}/cancel` returns 200.
2. Job status updates to `cancelled` in the UI within 5s.
3. Reload → still cancelled.

### UC-11: Header notifications bell shows unread count (P2-E)

**Interface:** UI
**Setup:** Alerts exist (unread).
**Steps:**

1. Navigate anywhere logged-in.

**Verification:**

1. Bell icon visible in header.
2. Badge shows numeric count of unread alerts.
3. Click → navigates to `/alerts`.

### UC-12: Global error pages render (P2-G)

**Interface:** UI
**Setup:** Authenticated.
**Steps:**

1. Navigate to `/this-page-does-not-exist`.
2. Trigger a 500 (TBD — pick a deterministic trigger or stub).

**Verification:**

1. 404 page shows styled MSAI logo + "Page not found" + back-to-dashboard CTA.
2. 500 page shows styled "Something went wrong" + retry CTA.

### UC-13: Soft-delete preserves backtest history (P1-E + P1-E-backend)

**Interface:** API
**Setup:** A strategy with at least one completed backtest exists.
**Steps:**

1. DELETE the strategy via `/api/v1/strategies/{id}`.
2. GET `/api/v1/strategies/` — confirm strategy is NOT in the list.
3. GET `/api/v1/strategies/{id}` — confirm strategy IS still resolvable (for history).
4. GET `/api/v1/backtests/<existing-backtest-id>/results` — confirm the backtest still resolves its `strategy_id`.

**Verification:**

1. List query excludes archived.
2. Detail query returns the archived row.
3. Backtest history still resolves.

### UC-14: IB account snapshot caching (P1-B-backend)

**Interface:** API
**Setup:** Stack running with `IBAccountSnapshot` enabled.
**Steps:**

1. Concurrently GET `/api/v1/account/summary` 10 times.
2. Inspect server logs (or use a test that intercepts `IB.connectAsync`).

**Verification:**

1. Exactly ONE IB connection observed in the test window.
2. All 10 responses return within reasonable time (cached) without IB pacing errors.

### UC-15: Templates feature is cut (cleanup verification)

**Interface:** API+CLI
**Setup:** Stack running.
**Steps:**

1. GET `/api/v1/strategy-templates/` → expect 404 (router not registered).
2. POST `/api/v1/strategy-templates/scaffold` → expect 404.
3. CLI: `msai template list` → expect "No such command" error.

**Verification:** All three calls return as expected; no backend feature remains.

---

## Risks & Mitigations

| Risk                                                                                   | Likelihood       | Mitigation                                                                                                                                                                     |
| -------------------------------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Tailwind 4 + shadcn registry install fails                                             | Medium           | T1 is the first commit; if it fails, fix as P0 before continuing                                                                                                               |
| `IBAccountSnapshot` singleton crashes the FastAPI app at startup if IB Gateway is down | Medium           | Graceful degradation: snapshot starts unconnected, returns `AccountSummaryZero()` until first successful connect. Handler never raises; UI shows "No account connected" state. |
| Soft-delete event listener affects unintended queries                                  | Low              | Unit tests assert listener filters Strategy only; opt-out works for backtest detail.                                                                                           |
| Playwright X-API-Key fixture doesn't authenticate against backend                      | Low              | Backend already supports `X-API-Key`; the test env just needs the secret. Smoke test in T23.                                                                                   |
| Dashboard AlertsFeed creates dependency loop (T8 needs T10)                            | Low              | Encoded in dispatch plan; T10 fires before T8.                                                                                                                                 |
| Strategy edit/delete cluster on same file blocks parallelism                           | Accepted         | T13-T14-T15 serialized on `strategies/[id]/page.tsx`. Other parallel work continues.                                                                                           |
| `research walk-forward` UI gap discovered at T17 implementation                        | Low              | Q-7 explicitly noted; T17 implementer verifies + adds dialog mode if missing.                                                                                                  |
| Header bell uses `useAlerts` mid-fetch on page load → flicker                          | Low              | TanStack `placeholderData: previousData` smooths transitions; skeleton-style "—" while first fetch.                                                                            |
| Existing live-trading drill regressions from snapshot pattern                          | Low (high blast) | Paper IB drill in Phase 5.4 catches regressions before merge. Pablo runs the drill.                                                                                            |
| Global event listener breaks during Alembic migration on existing data                 | Low              | Migration is additive (`deleted_at NULL DEFAULT`). No data backfill required.                                                                                                  |

---

## Acceptance

- All P0 lies gone (UC-1 through UC-8 PASS).
- All P1 CRUD working (UC-5, UC-6, UC-7, UC-13 PASS).
- All P2 polish + observability surfaces shipped (UC-9, UC-10, UC-11 PASS).
- Backend safety holds (UC-14 PASS).
- Cut feature confirmed (UC-15 PASS).
- Playwright spec count ≥ 9 (T24 acceptance).
- CI workflow active and green on push (T25 acceptance).
- `pytest`, `ruff`, `mypy --strict`, `pnpm lint`, `tsc --noEmit` all clean.
- Pablo can use the UI for a full day without finding a lie.

---

## References

- Audit: `docs/audits/2026-05-16-ui-surface-audit.md`
- PRD: `docs/prds/2026-05-16-ui-completeness.md`
- Council scope + user override: `docs/decisions/2026-05-16-ui-completeness-scope.md`
- Research brief: `docs/research/2026-05-16-ui-completeness.md`
- CLAUDE.md (API-first/CLI-second/UI-third ordering, file structure)
- Nautilus gotchas #3 (client_id), #6 (port/account_id consistency), #11 (no dynamic instrument loading)
- Memory rules:
  - `feedback_use_playwright_mcp_for_ui_e2e` — drive UI E2E autonomously
  - `feedback_dont_optimize_for_cost` — rigor > velocity
  - `feedback_dont_propose_time_based_stops` — finish full scope
  - `feedback_skip_phase3_brainstorm_when_council_predone` — 3.1/3.1b/3.1c PRE-DONE
  - `feedback_code_review_iteration_discipline` — re-run reviewers on each fix commit

---

## Plan Revisions — Iter 1 (post-Codex review)

**Plan-review iter 1 verdict:** 0 P0, 4 P1, 7 P2, 1 P3 — NOT ready until these supersessions apply. Sections below SUPERSEDE the original task text where they conflict. Original Task definitions earlier in the document are preserved for historical traceability; **the supersessions below are authoritative for execution.**

### Revision R1 (supersedes T2) — IBAccountSnapshot startup must not block on IB connect (Codex F1)

T2's original `start()` outline awaits `connectAsync()` before spawning the refresh task. If IB Gateway is down at FastAPI startup, the app fails to boot. Mirror the existing `IBProbe.run_periodic` pattern (`backend/src/msai/services/ib_probe.py:54-90`) which does I/O **inside** the loop body, never at task creation.

**Revised pattern:**

```python
class IBAccountSnapshot:
    def __init__(self):
        self._ib = IB() if IB else None
        self._client_id = 900
        self._summary: AccountSummary = AccountSummaryZero()   # ← always usable
        self._portfolio: list[Position] = []                    # ← always usable
        self._connected = False
        self._refresh_task: asyncio.Task | None = None

    def start(self):
        # NO await — just spawn the loop. Connection happens inside.
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        while True:
            try:
                if not self._connected:
                    await asyncio.wait_for(
                        self._ib.connectAsync(host=..., port=..., clientId=self._client_id),
                        timeout=5.0,
                    )
                    self._connected = True
                self._summary = await self._fetch_summary()
                self._portfolio = await self._fetch_portfolio()
            except Exception as e:
                self._connected = False  # mark for reconnect next tick
                log.warning("snapshot_refresh_failed", error=str(e))
            await asyncio.sleep(_PROBE_INTERVAL_S)
```

Lifespan calls `snapshot.start()` (synchronous task spawn), not `await snapshot.start()`. Handlers serve `snapshot._summary` which is always non-None.

**Add integration test:** `backend/tests/integration/test_ib_account_snapshot_boot.py` — boot the FastAPI app with IB Gateway unreachable; assert `/health` returns 200 and `/api/v1/account/summary` returns the zero-summary fallback.

### Revision R2 (supersedes T3 + T15) — Soft-delete must coexist with `sync_strategies_to_db` registry sync (Codex F2)

`backend/src/msai/api/strategies.py:50-53, 92-96` call `sync_strategies_to_db()` before every list/detail read. `sync` queries `select(Strategy)` and creates new rows for any discovered file path that has no existing row (`services/strategy_registry.py:520-545`). With a default `deleted_at IS NULL` filter, an archived row becomes invisible to sync → sync creates a NEW active row for the same file path → archive is silently un-archived on next GET.

**Revised T3 sub-task list:**

1. Alembic migration adds `deleted_at: TIMESTAMP NULL` + partial index.
2. `core/soft_delete.py` adds the `do_orm_execute` + `with_loader_criteria(Strategy, lambda s: s.deleted_at.is_(None))` listener.
3. **`services/strategy_registry.py:520` MUST query with `include_deleted=True`** so sync sees archived rows. Sync logic updated:
   - If discovered file matches an existing row (active or archived) → reuse that row. If archived, **leave archived** (do not silently un-archive). Skip from the returned `paired` list so list view continues to hide it.
   - Add a separate `restore_strategy(strategy_id)` path (NOT in this PR) for explicit operator restoration.
   - Pruning (`if prune_missing:` block, line ~565): when a strategy file disappears from disk, set `deleted_at` on the row (soft-prune) instead of `session.delete()` (hard-prune).
4. `api/strategies.py` DELETE handler sets `deleted_at = now()`. UC-13's "GET /strategies/{id} returns archived" requires `execution_options(include_deleted=True)` on that handler — confirmed.
5. Backtest history detail handler also uses `include_deleted=True` for strategy resolution (already in original plan).

**Add unit test:** `backend/tests/unit/test_soft_delete_sync_interaction.py` — soft-delete a strategy → call `sync_strategies_to_db()` (which discovers the same file on disk) → assert the archived row stays archived (not un-archived) and no new row is created.

### Revision R3 (supersedes T13) — Strategy edit form scope: `description` + `default_config` ONLY (Codex F3 + Claude)

`StrategyUpdate` schema (`backend/src/msai/schemas/strategy.py:35-39`) accepts ONLY `default_config` and `description`. Sync (`strategy_registry.py:547-554`) overwrites `row.name` from disk on every list call. Editing `name` from the UI would be silently clobbered on next list.

**Revised T13 form schema:** `z.object({ description: z.string().nullable(), default_config: z.string() })` — no `name`. The detail page H1 still renders `strategy.name` (derived from class) but it's NOT editable. Add an explanatory tooltip "Name is set by the strategy class registration and cannot be edited."

### Revision R4 (supersedes T4) — Templates cut must update test files (Codex F4 + Claude)

T4's original Writes list missed test files. Updated Writes:

**ADD:**

- `backend/tests/unit/test_strategy_templates.py` → **DELETE entire file** (it tests the now-cut service).
- `backend/tests/unit/test_cli_completeness.py` → **EDIT** to remove the `test_gets_strategy_templates_endpoint` test (line 179) + remove `template_list` / `template_scaffold` assertions (lines 186, 210, 1011-1038 per Codex citation).

### Revision R5 (supersedes Dispatch Plan Wave 1) — Wave 1 is NOT file-disjoint (Codex F5)

T2, T3, and T4 all write `backend/src/msai/main.py`:

- T2 wires `IBAccountSnapshot` into `lifespan`
- T3 registers the `soft_delete` event listener at app startup
- T4 registers new `system_router` and de-registers `strategy_templates_router`

This violates the file-disjoint dispatch rule. Two fixes:

**Option A (chosen):** Split the `main.py` wiring into a NEW task T4b that depends on T2 + T3 + T4 backend logic. T2/T3/T4 write their respective new modules; T4b is the single serial integration commit.

**Revised T4b — Main app wiring (NEW):**

- Writes: `backend/src/msai/main.py` (sole consumer).
- Depends on: T2 + T3 + T4.
- Content: import `IBAccountSnapshot`, register listener, wire `system_router`, de-register `strategy_templates_router`.

**Revised Wave 1:** T1 (frontend deps) || T2 (snapshot module only — NO main.py touch) || T3 (model + soft_delete + migration — NO main.py touch) || T4 (system.py + cuts — NO main.py touch). All four parallel.

**Revised Wave 1.5 (NEW serial):** T4b — main.py integration. Serial.

**Revised Wave 2:** T5 (api.ts) after T4b.

### Revision R6 (supersedes T10, T19, UC-2, UC-11) — Alert fields are minimal; revise UI expectations (Codex F6)

`AlertRecord` (`backend/src/msai/schemas/alert.py`) has ONLY: `type`, `level`, `title`, `message`, `created_at`. There is no `code`, `payload`, `read_at`, or read/unread state.

**Revised T10 (alerts page):** Table columns are `level` (severity icon + text color), `created_at` (ISO timestamp), `type` (short tag), `title` (bold), `message` (truncated). Detail sheet shows the same fields fully expanded. No "expandable raw payload" — that doesn't exist.

**Revised T19 (header notifications bell):** Badge shows **count of alerts created in the last 24h** (client-side filter from `created_at`). Tooltip: "X new alerts in the last 24 hours." No "unread" semantics until backend supports them.

**Revised UC-2 verification:**

- Table renders with severity icon (color + icon + text label for `level`) + ISO timestamp + type tag + title + message.
- Detail sheet shows all five fields.
- No assertion about "alert code" or "raw payload."

**Revised UC-11 verification:**

- Bell shows count of last-24h alerts (NOT unread count).
- Tooltip text matches the count rule.

**Optional backend extension** (NOT in this PR — defer): if Pablo wants real read/unread tracking, ship `POST /api/v1/alerts/{id}/ack` + `read_at: datetime | null` in a future PR.

### Revision R7 (supersedes T22 + UC) — Symbol readiness needs (symbol, asset_class) tuples; portfolio compose only has comma-separated text (Codex F7)

`GET /api/v1/symbols/readiness` requires `symbol` AND `asset_class` query params (`api/symbol_onboarding.py:624`). Portfolio compose UI parses comma-separated instrument strings only (`components/live/portfolio-compose.tsx:81-89`).

**Revised T22:** Add an inventory-lookup step BEFORE readiness calls. The compose flow can leverage the existing `useInventoryQuery()` hook to resolve instrument string → `(symbol, asset_class)` tuple. For each instrument the user enters, look up its row from inventory; if not found, surface a "Symbol not in registry" warning inline. For found rows, fire one readiness request per (symbol, asset_class) pair.

Add to T22 Writes:

- `frontend/src/components/live-portfolio/readiness-check.tsx` (NEW) consumes inventory + readiness hooks; renders inline status per instrument.

**Edge case:** If multiple inventory rows match the same symbol across asset classes, surface the conflict ("AAPL has rows for equity AND option — clarify"). Block portfolio start until resolved.

### Revision R8 (supersedes T23) — Playwright auth bypass requires UI-level config, not just X-API-Key (Codex F8)

Backend `X-API-Key` (`core/auth.py:96-102`) is correct for API calls, but `frontend/src/components/layout/app-shell.tsx:16-26` redirects unauthenticated users to `/login` when `NODE_ENV !== "development"`. CI builds run `pnpm build` (production mode) by default, so Playwright would hit the login redirect.

**Revised T23 — three sub-steps:**

1. **`playwright.config.ts`** — set `use.extraHTTPHeaders: { 'X-API-Key': process.env.TEST_API_KEY }` for fetch-mocked API calls.
2. **`frontend/src/components/layout/app-shell.tsx`** — extend the dev-bypass to also bypass when `process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"`. New env var, default off.
3. **Playwright `webServer` config** — start the Next.js dev server (`pnpm dev`) for E2E rather than `pnpm build && pnpm start`. Faster + no auth-redirect issue. CI workflow `e2e.yml` does `pnpm dev` (background) + waits for `localhost:3300` before running specs.

**OR** alternative: set `NEXT_PUBLIC_E2E_AUTH_BYPASS=1` at CI build time + run production. Pick option A (dev server) for simplicity unless prod-mode E2E is required.

**Revised UC-1 setup:** "ARRANGE — Playwright run uses dev-mode server with E2E bypass; `X-API-Key` header set for all fetches via `extraHTTPHeaders`."

### Revision R9 (supersedes T20) — "Last run" requires aggregate endpoint or relabel (Codex F9)

Backtest history is paginated `page_size=20` default, max 100. A single-page fetch + group-by misses strategies whose latest backtest isn't on the first page.

**Revised T20:**

Option A (chosen — cheaper): **Drop the "Last run" timestamp column entirely.** Replace with a "Has backtests" badge (boolean: query `GET /api/v1/backtests/history?strategy_id=<id>&page_size=1`). The detail page already shows full backtest history.

Option B (deferred): Add `GET /api/v1/strategies/{id}/last-backtest` backend endpoint. NOT in this PR — would expand scope.

Revised T20 acceptance: card shows strategy name + status badge + "View Backtests" CTA + "Run Backtest" CTA. No "Last run" timestamp.

### Revision R10 (supersedes T16 + UC-9) — Live audit drawer shows order-attempt fields, not generic events (Codex F10)

`GET /api/v1/live/audits/{deployment_id}` returns: `client_order_id`, `instrument_id`, `side`, `quantity`, `status`, `strategy_code_hash`, `timestamp` (`api/live.py:2170-2181`).

**Revised T16:** Drawer renders a table with columns: `timestamp`, `side` (badge BUY/SELL), `instrument_id`, `quantity` (formatted decimal), `status` (badge), `client_order_id` (monospace truncated). `strategy_code_hash` shown in row-expand. NOT "event type + payload preview."

**Revised UC-9 verification:**

- Drawer opens with sortable table of order attempts.
- Each row shows timestamp + side + instrument_id + quantity + status + client_order_id.
- Empty state: "No order attempts yet for this deployment."

### Revision R11 (supersedes T24 + UC-12) — E2E coverage gaps + deterministic 500 trigger (Codex F11)

T24 spec list missed T18 and T22. UC-12 has "Trigger a 500 (TBD)" — not testable as-written.

**Revised T24 spec list:**

- `alerts.spec.ts`, `account.spec.ts`, `system.spec.ts`, `settings.spec.ts`, `strategies-crud.spec.ts`, `dashboard.spec.ts`, `live-audit-drawer.spec.ts`, `research-cancel.spec.ts`, `error-pages.spec.ts`, **`market-data-status.spec.ts` (NEW for T18)**, **`readiness-check.spec.ts` (NEW for T22)**.

**Revised UC-12:** Replace "Trigger a 500 (TBD)" with two separate UCs:

UC-12a (404): Navigate to `/this-page-does-not-exist` → assert `not-found.tsx` renders.

UC-12b (500): Component-level test — Playwright route-intercepts `/api/v1/strategies/` to return 500. Navigate to `/strategies`. Page-level error boundary (`error.tsx`) renders OR inline error message renders (depending on `error.tsx` scope). Deterministic + testable.

### Revision R12 (supersedes T7, T18) — Profile fields + reuse existing fetcher (Codex F12, P3)

**T7 settings profile card:** `/auth/me` returns `display_name`, NOT `name`. Render `display_name` (with `email` as fallback if `display_name` is null), `email`, `role`. NOT `name`. Same on header `<Avatar>` (already uses MSAL `account.name` which is correct from MSAL side — leave that alone). The settings page Profile card specifically uses backend claims, so use `display_name`.

**T18 market-data status card:** `getMarketDataStatus()` already exists at `frontend/src/lib/api.ts:438-453`. T5's planned addition for this fetcher is redundant — REMOVE from T5 list. T18 reuses the existing fetcher.

---

## Plan-Review Loop Status

| Iter         | Reviewer           | P0    | P1    | P2    | P3    | Verdict       |
| ------------ | ------------------ | ----- | ----- | ----- | ----- | ------------- |
| 1            | Claude self-review | 0     | 2     | 1     | 1     | NOT ready     |
| 1            | Codex (`xhigh`)    | 0     | 4     | 7     | 1     | NOT ready     |
| **1 merged** | —                  | **0** | **4** | **7** | **1** | **NOT ready** |

Iter-1 fixes embedded in Revisions R1-R12 above. **Iter 2 must re-run reviewers** on the supersession content + verify no new findings emerge.

---

## Plan Revisions — Iter 2 (post-Codex review)

Iter-2 verdict: 0 P0, 1 P1, 4 P2, 1 P3 (improvement from 4 P1 / 7 P2 in iter 1). Codex confirmed clean for R1, R3, R4, R5, R9, R12. Revisions R13-R19 address the new findings.

### Revision R13 (amends R8) — Resolve Playwright port conflict (Codex iter-2 F1, P1)

R8 said "run `pnpm dev` for E2E" — but `pnpm dev` defaults to port 3000 while `playwright.config.ts:33` expects 3300 (Docker-host-exposed). Two servers race for ports.

**Revised R13:**

1. **`playwright.config.ts`** — uncomment the existing `webServer` block (`playwright.config.ts:89-94`) and set:
   ```ts
   webServer: {
     command: 'pnpm dev --port 3300',
     url: 'http://localhost:3300',
     reuseExistingServer: !process.env.CI,
     timeout: 120_000,
   }
   ```
2. **CI workflow `e2e.yml`** — does NOT separately start `pnpm dev`. Playwright's `webServer` config owns the server lifecycle. CI just runs `pnpm exec playwright test`.
3. **Local dev (Docker)** — the Docker compose dev environment already serves on 3300 internally→externally. `reuseExistingServer: !CI` means local runs reuse the Docker-served server; CI starts its own.

This eliminates port mismatch + race. Playwright owns the lifecycle.

### Revision R14 (amends R2) — Extend `include_deleted=True` to live deployment context paths (Claude iter-2 P1)

R2 covered `services/strategy_registry.py` (sync) + `api/strategies.py` detail GET, but missed two more `select(Strategy)` call sites that active live deployments depend on:

- `backend/src/msai/api/live.py:828` — multi-strategy resolution during portfolio operations.
- `backend/src/msai/live_supervisor/__main__.py:231` — supervisor's member resolution during start-portfolio.

If a strategy is soft-deleted while an **active** deployment references it, these two sites with default-filtered queries would fail to resolve the strategy → supervisor crash / 500 on live operations.

**Revised R14:**

1. Both sites get `.execution_options(include_deleted=True)` applied to their queries.
2. **Operational rule:** a soft-deleted strategy is still **resolvable** for read paths (history, audit, live supervisor). Default-filtering applies to:
   - List view (`/strategies/` GET) — to hide from the index
   - Create paths (new backtest, new research job, new portfolio member) — to prevent re-use of archived strategies for NEW operations

3. Updated R2 wording: "Default filter applies to LIST and NEW-OPERATION paths. Include_deleted opt-in for DETAIL, SUPERVISOR, and HISTORY paths."

**Other call sites checked:**

| Site                                               | Path type            | include_deleted?                                             |
| -------------------------------------------------- | -------------------- | ------------------------------------------------------------ |
| `api/strategies.py:95` (`get_strategy` detail)     | DETAIL               | **opt-in** (R15 below replaces R2's misattribution)          |
| `api/strategies.py:127` (PATCH)                    | mutation on existing | default (archived returns 404 — desired idempotency)         |
| `api/strategies.py:171` (validate)                 | mutation             | default (cannot validate archived)                           |
| `api/strategies.py:208` (DELETE)                   | mutation             | default (DELETE of already-archived returns 404, idempotent) |
| `api/research.py:341` (new sweep dispatch)         | NEW-OP               | default (cannot launch on archived)                          |
| `api/backtests.py:296` (new run dispatch)          | NEW-OP               | default (cannot launch on archived)                          |
| `api/live.py:828` (multi-strat resolve in live)    | SUPERVISOR           | **opt-in**                                                   |
| `live_supervisor/__main__.py:231` (member resolve) | SUPERVISOR           | **opt-in**                                                   |
| `services/strategy_registry.py:520` (sync)         | SYNC                 | **opt-in** (per R2)                                          |

**Add integration test:** `backend/tests/integration/test_strategy_soft_delete_live_path.py` — start a live paper deployment using strategy X; soft-delete X; assert deployment status/positions endpoints still resolve correctly (supervisor uses include_deleted).

### Revision R15 (amends R2 wording) — `get_strategy()` is the include_deleted opt-in site, not the DELETE handler (Codex iter-2 F2, P2)

R2 ambiguously tied `include_deleted=True` to the DELETE handler / backtest history path. The correct binding: **`get_strategy()` detail GET at `api/strategies.py:95` opts in.**

DELETE handler at `api/strategies.py:208` keeps the default filter (returns 404 on already-archived → idempotent semantics). Backtest history detail handler in `api/backtests.py` opts in separately (it queries Strategy via `select(Strategy).where(Strategy.id == backtest.strategy_id)` — that select must also use include_deleted, see R14's "DETAIL" classification).

Revised R2/R15 sentence: "Detail GETs across all routers (`/strategies/{id}`, backtest detail strategy join, research detail strategy join) all use `.execution_options(include_deleted=True)`."

### Revision R16 (amends R7) — Compose flow MUST block on unresolved instruments (Codex iter-2 F3, P2)

R7 said "surface an inline warning" for unknown instruments. That allows the compose flow to proceed and persist invalid instruments. The backend `portfolio_service:135` then has to deal with unresolved symbols at start-portfolio time — pushing failure to the worst moment (mid-deployment).

**Revised R16:**

1. The compose flow validates every instrument against inventory BEFORE allowing "Add Member" / "Snapshot" / "Start Portfolio." Block the action if any instrument is:
   - Not in inventory at all → surface "Symbol not in registry" + a CTA button: **"Onboard symbol via Market Data"** (links to `/market-data` add flow).
   - Ambiguous (same symbol in multiple asset classes) → "Symbol matches multiple asset classes" + radio picker.

2. UC-22 acceptance: blocked submit + CTA visible + cannot proceed until resolved.

3. Add unit test for the resolution helper: `frontend/tests/unit/test_instrument_resolver.test.ts`.

### Revision R17 (amends R10) — Audit drawer is "Latest 50 attempts" + no pagination control (Codex iter-2 F4, P2)

`api/live.py:2145-2163` returns the latest 50 rows with NO `total` / `page` / `page_size` shape. Showing a paginated table is misleading.

**Revised R10/R17:**

1. Drawer header text: "Latest 50 order attempts" (explicit truncation notice).
2. No pagination control. No sort UI (rows are returned `ts_attempted DESC` already).
3. If more history is needed in the future, add a separate backend endpoint with pagination — NOT in this PR.

Update UC-9 verification:

- Drawer header reads "Latest 50 order attempts".
- Table renders up to 50 rows sorted newest-first.
- No pagination control visible.

### Revision R18 (amends R11) — UC-12b needs a render-time throw test, not a fetch intercept (Codex iter-2 F5, P2)

R11's UC-12b intercepts `/api/v1/strategies/` to return 500. But `frontend/src/app/strategies/page.tsx:30, 57` catches fetch errors and renders them INLINE via the existing `error` state. `error.tsx` only catches RENDER-time throws. The intercept tests the inline error path, not the route error boundary.

**Revised UC-12 split:**

**UC-12a (404 page):** Navigate to `/this-page-does-not-exist` → assert `not-found.tsx` renders. ✅ as-is.

**UC-12b (route error boundary):** Add a deterministic render-throw test:

- Create a hidden test-only route `/__e2e_throw` (gated by `NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"`) that does `throw new Error("e2e test crash")` at render.
- Navigate to `/__e2e_throw` → assert `error.tsx` renders with "Something went wrong" + retry CTA.

**UC-12c (inline fetch error, separate from boundary):** Keep the original intercept of `/api/v1/strategies/` to 500 → assert page-inline error message renders ("Failed to load strategies (500)"). This tests the inline error path that already exists, complementing UC-12b.

Update T24 spec list: add `e2e-throw.spec.ts` for UC-12b, rename existing UC-12 spec to cover all three (12a/12b/12c).

### Revision R19 (amends R6) — Alert detail Sheet targets by response-index, not synthetic identity (Codex iter-2 F6, P3)

`AlertRecord` has no `id`. The detail Sheet must target by **position in the response list** (index), NOT by `created_at + title` (which is not unique).

**Revised R6/R19:**

```tsx
function AlertsTable({ alerts }: { alerts: AlertRecord[] }) {
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  // ...
  <SheetTrigger asChild>
    <TableRow onClick={() => setSelectedIndex(i)}>...</TableRow>
  </SheetTrigger>
  <AlertDetailSheet
    alert={selectedIndex !== null ? alerts[selectedIndex] : null}
    onClose={() => setSelectedIndex(null)}
  />
  // ...
}
```

No backend identity is synthesized. If the alert list refreshes while the Sheet is open, the index may point to a different alert — the Sheet should close on list refresh (driven by TanStack `isFetching` state) to avoid this.

---

## Plan-Review Loop Status (updated)

| Iter     | Reviewer        | P0    | P1    | P2    | P3    | Verdict   |
| -------- | --------------- | ----- | ----- | ----- | ----- | --------- |
| 1        | Claude          | 0     | 2     | 1     | 1     | NOT ready |
| 1        | Codex (`xhigh`) | 0     | 4     | 7     | 1     | NOT ready |
| 1 merged | —               | **0** | **4** | **7** | **1** | NOT ready |
| 2        | Claude          | 0     | 1     | 0     | 0     | NOT ready |
| 2        | Codex (`xhigh`) | 0     | 1     | 4     | 1     | NOT ready |
| 2 merged | —               | **0** | **1** | **4** | **1** | NOT ready |

Iter-2 fixes embedded in Revisions R13-R19. **Iter 3 must re-run reviewers** to confirm convergence to zero P0/P1/P2.

---

## Plan Revisions — Iter 3 (post-Codex review)

Iter-3 verdict: **0 P0, 0 P1, 0 P2, 3 P3 — PLAN READY TO EXECUTE.** Codex checked clean for R13, R14, R16, R17 (confirmed against actual code). Three P3 polish items applied as R20-R22 for cleanliness (non-blocking).

### Revision R20 (corrects R15 wording, P3)

R15 misclassified `api/backtests.py:296` and `api/research.py:341` as "backtest/research detail strategy joins." They are actually NEW-OP dispatch paths (new backtest run, new research sweep) and should remain **default-filtered** (archived strategies should NOT be able to launch new operations).

**Corrected classification (final):**

| Site                                                          | Path type            | include_deleted?                            |
| ------------------------------------------------------------- | -------------------- | ------------------------------------------- |
| `api/strategies.py:95` (`get_strategy` detail)                | DETAIL               | **opt-in**                                  |
| `api/strategies.py:127, 171, 208` (PATCH / validate / DELETE) | mutation on existing | default                                     |
| `api/research.py:341` (new sweep dispatch)                    | NEW-OP               | **default** (archived blocked from new ops) |
| `api/backtests.py:296` (new run dispatch)                     | NEW-OP               | **default**                                 |
| `api/live.py:828` (multi-strat resolve in live)               | SUPERVISOR           | **opt-in**                                  |
| `live_supervisor/__main__.py:231` (member resolve)            | SUPERVISOR           | **opt-in**                                  |
| `services/strategy_registry.py:520` (sync)                    | SYNC                 | **opt-in**                                  |

Only DETAIL, SUPERVISOR, and SYNC paths opt in. NEW-OP and mutation stay default-filtered.

### Revision R21 (hardens R18, P3)

The `/__e2e_throw` test route under `src/app` is a real Next route. Even with the env gate, ship a `notFound()` short-circuit so the route returns a 404 when not in E2E mode, regardless of any env-var leakage.

```tsx
// frontend/src/app/__e2e_throw/page.tsx
import { notFound } from "next/navigation";

export default function E2EThrow() {
  if (process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS !== "1") {
    notFound();
  }
  throw new Error("E2E render-time test crash");
}
```

Server-component (no `"use client"`) so the env check runs server-side. In prod (`NEXT_PUBLIC_E2E_AUTH_BYPASS` unset), the route returns 404 via `not-found.tsx` instead of throwing.

### Revision R22 (refines R19 UX, P3)

R19 said "close Sheet on TanStack `isFetching`" — but T10's 60s polling cadence would close the Sheet every minute, interrupting the user reading detail. Better UX: snapshot the selected `AlertRecord` into local state at click time; the Sheet renders from the snapshot regardless of subsequent list refreshes.

```tsx
const [selectedAlert, setSelectedAlert] = useState<AlertRecord | null>(null);

<SheetTrigger asChild>
  <TableRow onClick={() => setSelectedAlert(alert)}>...</TableRow>
</SheetTrigger>
<AlertDetailSheet
  alert={selectedAlert}
  onClose={() => setSelectedAlert(null)}
/>
```

Sheet stays open across refreshes. User explicitly closes via X button or backdrop click. No polling-driven interruption.

---

## Plan-Review Loop Status — FINAL

| Iter         | Reviewer        | P0    | P1    | P2    | P3             | Verdict      |
| ------------ | --------------- | ----- | ----- | ----- | -------------- | ------------ |
| 1            | Claude          | 0     | 2     | 1     | 1              | NOT ready    |
| 1            | Codex (`xhigh`) | 0     | 4     | 7     | 1              | NOT ready    |
| 1 merged     | —               | 0     | 4     | 7     | 1              | NOT ready    |
| 2            | Claude          | 0     | 1     | 0     | 0              | NOT ready    |
| 2            | Codex (`xhigh`) | 0     | 1     | 4     | 1              | NOT ready    |
| 2 merged     | —               | 0     | 1     | 4     | 1              | NOT ready    |
| 3            | Claude          | 0     | 0     | 0     | 1 (R13 polish) | READY        |
| 3            | Codex (`xhigh`) | 0     | 0     | 0     | 3              | **READY**    |
| **3 merged** | —               | **0** | **0** | **0** | **3**          | **✅ READY** |

**Plan-review loop CLOSED at iter 3.** Trajectory healthy (P1 count: 4 → 1 → 0; P2 count: 7 → 4 → 0). Per memory rule `feedback_code_review_iteration_discipline`, P3-only findings do not block. All 22 revisions (R1-R22) are authoritative supersessions over the original task definitions earlier in this document.

Proceed to Phase 4 (subagent-driven execution).

---

## Plan Revisions — Iter 3.5 (E2E framing — Pablo directive)

User directive 2026-05-16: "E2E tests should be as a user, as a trader using the system." The original UC-1..UC-15 list was feature-atomic ("user views X element"); this revision recasts them as multi-page trader journeys TJ-1..TJ-11. **R23 SUPERSEDES the entire `## E2E Use Cases` section earlier in this document.** T24's spec file list is also rewritten to match.

### Revision R23 — Trader Journey use cases (SUPERSEDES UC-1..UC-15)

Persona (all journeys unless noted): **Pablo, paper trader / solo operator.** Authenticated via Entra ID in browser, paper IB account `DU...` on port 4004. The trader thinks in goals (deploy a paper portfolio, archive an old strategy, respond to an alert) — not in page elements.

Mode: **fullstack** (API+UI). API-first execution order per `CLAUDE.md ## E2E Configuration`.

All journeys honor ARRANGE/VERIFY boundaries: setup goes through public API / public UI flows / documented CLI. VERIFY goes through the same surface a real trader uses.

---

#### TJ-1: Morning checklist (smoke)

**Spec file:** `frontend/tests/e2e/specs/morning-checklist.spec.ts`
**Tags:** `@smoke`
**Touches:** `/dashboard`, alerts feed, alert detail sheet, `/live-trading`, audit log drawer.

**Goal:** Pablo opens MSAI in the morning to scan overnight state — portfolio P&L, alerts that fired while he slept, active deployments, and last night's order activity.

**ARRANGE:**

- Authenticate via X-API-Key fixture.
- Seed via public API: at least one live deployment (created via `POST /api/v1/live/start-portfolio` against a paper account) AND at least one alert in history (alerts are emitted by backend; trigger by stopping a deployment if no alerts exist, OR run a documented dev-seed command).

**STEPS (trader actions):**

1. Navigate to `/dashboard`.
2. Read the PortfolioSummary cards (Total Value, Daily P&L, Active Strategies).
3. Scan the AlertsFeed card (last 5 alerts).
4. Click the most recent alert → detail Sheet opens.
5. Close the Sheet.
6. Click "Live Trading" in sidebar → `/live-trading`.
7. Click "Audit Log" on the running deployment row → drawer opens with last 50 order attempts.
8. Close the drawer.

**VERIFY:**

- Dashboard renders without any `--` placeholders or empty chart cards (no F-9/F-10/F-11 lies).
- Trend arrow on Total Value reflects the actual sign of the value.
- AlertsFeed shows up to 5 rows with level icon + timestamp + title.
- Alert detail Sheet shows full `type`, `level`, `title`, `message`, `created_at`.
- `/live-trading` lists the seeded deployment in `running` status.
- Audit drawer header reads "Latest 50 order attempts" + table renders with `timestamp`, `side`, `instrument_id`, `quantity`, `status`, `client_order_id`.
- Empty state on audit drawer when no attempts yet: "No order attempts yet for this deployment."

**PERSISTENCE:** Reload `/dashboard` → same data renders consistently.

---

#### TJ-2: Design a strategy (smoke)

**Spec file:** `frontend/tests/e2e/specs/strategy-design.spec.ts`
**Tags:** `@smoke`
**Touches:** `/strategies`, `/strategies/[id]`.

**Goal:** Pablo has registered a new strategy file via git. He wants to validate it loads correctly, tweak its default config, and save the changes for future backtests.

**ARRANGE:**

- An EMA-cross strategy already exists in `strategies/` directory (git-only convention; pre-registered via worker startup sync).

**STEPS:**

1. Navigate to `/strategies`.
2. Read the list — every card shows a real status badge (running/stopped/none), NO `--` metric placeholders.
3. Click "View Details" on the EMA-cross strategy.
4. Click "Validate" button.
5. Read the dialog — backend validation result (success message OR import error).
6. Close the dialog.
7. Edit `default_config` in the form (e.g. change `slow_ema_period: 50` → `60`).
8. Click "Save".
9. See success toast "Strategy updated".

**VERIFY:**

- `/strategies` list has NO `--` placeholders anywhere (F-12 fixed).
- Validate dialog shows real backend response (NOT a local `JSON.parse` result) (F-6 fixed).
- Form has explicit Save button (F-7 fixed — no silent data loss).
- Backend PATCH `/api/v1/strategies/{id}` returns 200 with updated config.
- Toast appears confirming save.
- Strategy `name` field is NOT editable (tooltip explains why — set by class registration).

**PERSISTENCE:** Reload `/strategies/[id]` → modified config still present.

---

#### TJ-3: Backtest + review

**Spec file:** `frontend/tests/e2e/specs/backtest-flow.spec.ts`
**Tags:** (not smoke — existing flow regression)
**Touches:** `/strategies/[id]`, `/backtests`, `/backtests/[id]`.

**Goal:** Pablo wants to backtest the strategy he just configured, then review trades + the QuantStats report.

**ARRANGE:**

- Strategy exists with valid config.
- Market data already ingested for AAPL/SPY 2024-01-01 to 2025-01-01 (via documented `msai ingest` CLI or pre-existing parquet).

**STEPS:**

1. From `/strategies/[id]`, click "Run Backtest" → `/backtests?strategy=<id>`.
2. Run dialog opens pre-filled with the strategy.
3. Set start/end dates + instruments.
4. Click "Start Backtest".
5. Poll status until `completed` (Playwright `expect.poll` or `waitForResponse`).
6. Click into the backtest result `/backtests/[id]`.
7. Read metrics (Sharpe, total return, win rate, max drawdown).
8. Open trade log tab — paginated table of fills.
9. Open Full Report tab — QuantStats iframe renders.

**VERIFY:**

- Run dialog accepts the strategy + dates + instruments.
- Status transitions: `pending → running → completed`.
- Metrics show real values (not placeholders).
- Trade log paginates correctly.
- Report iframe loads (HMAC-signed URL works).

**PERSISTENCE:** Reload `/backtests/[id]` → metrics + trades + report still accessible.

---

#### TJ-4: Deploy a paper portfolio (smoke)

**Spec file:** `frontend/tests/e2e/specs/paper-deploy.spec.ts`
**Tags:** `@smoke`
**Touches:** `/live-trading/portfolio`, `/market-data` (onboard side-trip), back to portfolio.

**Goal:** Pablo composes a multi-strategy portfolio. He hits a readiness check on a symbol not in inventory, side-trips to `/market-data` to onboard it, returns to portfolio, snapshots the composition, and starts the deployment with paper account.

**ARRANGE:**

- A strategy with valid config exists.
- One instrument (say, `MSFT`) is NOT in inventory; another (`AAPL`) IS in inventory.
- Paper IB account `DU...` reachable on port 4004.

**STEPS:**

1. Navigate to `/live-trading/portfolio`.
2. Click "New Portfolio". Fill name + description.
3. Click "Add Member" → fill strategy + instruments `AAPL, MSFT` (comma-separated).
4. See **inline readiness block**: "MSFT — Symbol not in registry" + CTA button "Onboard symbol via Market Data".
5. Click the CTA → navigates to `/market-data` with `?onboard=MSFT` (or opens add drawer).
6. Onboard MSFT via the existing onboard flow.
7. Return to `/live-trading/portfolio`.
8. Re-validate instruments → both AAPL and MSFT now resolve.
9. "Snapshot" button enabled. Click → confirms binding-fingerprint dialog.
10. "Start Portfolio" dialog → required `ib_login_key` field → toggle paper_trading ON → confirm.
11. Deployment created → status `starting`.

**VERIFY:**

- Compose flow BLOCKS Snapshot + Start when unresolved instruments exist (R16 enforced — no "warn and proceed").
- "Symbol not in registry" message + CTA visible.
- After onboard, readiness check resolves cleanly.
- Snapshot triggers binding-fingerprint preview.
- Start dialog has explicit paper-trading toggle + real-money warning + required `ib_login_key`.
- Deployment lands in `/live-trading` list with `paper_trading=true`.

**PERSISTENCE:** Reload `/live-trading` → deployment still present + status visible.

---

#### TJ-5: Respond to an alert (smoke)

**Spec file:** `frontend/tests/e2e/specs/alert-response.spec.ts`
**Tags:** `@smoke`
**Touches:** Header bell, `/alerts`, alert detail Sheet, `/live-trading`, stop dialog.

**Goal:** While reviewing his portfolio, Pablo notices the notifications bell glowing. He clicks through to read the alert, finds it relates to a live deployment, navigates over, stops the deployment, and verifies the flatness report.

**ARRANGE:**

- A running paper deployment exists.
- At least one alert in history (recent, within 24h window so the bell badge counts it).

**STEPS:**

1. Navigate to `/dashboard` (already authenticated).
2. Observe header bell badge with count ≥ 1.
3. Click the bell → `/alerts`.
4. Read the alerts table → click most recent.
5. Detail Sheet opens with full payload.
6. Close Sheet.
7. Navigate to `/live-trading`.
8. Locate the affected deployment.
9. Click "Audit Log" → drawer shows recent order attempts.
10. Close audit drawer.
11. Click "Stop" on the deployment row → confirm dialog.
12. Observe flatness response: `broker_flat: true/false` + remaining positions (if any).

**VERIFY:**

- Bell badge count matches alerts created in last 24h (NOT unread — backend has no unread state per R6).
- Bell click navigates to `/alerts` cleanly.
- Alerts table renders + detail Sheet selects by row index (R19/R22 — snapshot into local state).
- Stop dialog shows `broker_flat` boolean + positions list.
- Deployment status transitions to `stopped` (or `stopping → stopped`).

**PERSISTENCE:** Reload `/live-trading` → deployment shows stopped status.

---

#### TJ-6: Emergency kill-all (smoke)

**Spec file:** `frontend/tests/e2e/specs/kill-all.spec.ts`
**Tags:** `@smoke`
**Touches:** `/live-trading`.

**Goal:** Something looks catastrophically wrong. Pablo invokes the emergency kill-all, reviews per-deployment flatness, then resumes the system after investigation.

**ARRANGE:** ≥2 running paper deployments.

**STEPS:**

1. Navigate to `/live-trading`.
2. Click "Kill All" → confirm dialog with explicit "stop ALL N deployments" warning.
3. Confirm.
4. Observe response: `any_non_flat` boolean + `flatness_reports[]` (one per deployment with `broker_flat`, `remaining_positions`, `stop_nonce`).
5. Verify risk_halt indicator becomes visible.
6. Click "Resume" → confirm dialog.
7. risk_halt clears.

**VERIFY:**

- Kill-all stops every deployment.
- Per-deployment flatness report displays (table or list).
- `any_non_flat=true` highlights affected deployments in red.
- Resume button re-enables the system.

**PERSISTENCE:** Reload `/live-trading` → all deployments still stopped + system unhalted.

---

#### TJ-7: Archive an obsolete strategy

**Spec file:** `frontend/tests/e2e/specs/strategy-archive.spec.ts`
**Tags:** (not smoke — destructive flow, runs nightly)
**Touches:** `/strategies/[id]`, `/backtests/[id]`, `/live-trading`.

**Goal:** Pablo wants to remove an old strategy. The strategy has a completed backtest. He archives it; verifies that historical backtests still resolve; verifies that any active deployment using the strategy keeps running (per soft-delete supervisor opt-in R14).

**ARRANGE:**

- A test strategy `EmaArchiveTest` exists with at least one completed backtest.
- A running paper deployment uses the strategy.

**STEPS:**

1. Navigate to `/strategies/<archive-test-id>`.
2. Click Delete CTA.
3. AlertDialog opens with type-name-to-confirm input.
4. Type the wrong name → confirm button stays disabled.
5. Type the correct name → confirm button enables.
6. Click "Delete Strategy" → toast "Strategy archived".
7. Redirected to `/strategies` → strategy NOT in the list.
8. Navigate to the historical backtest at `/backtests/<existing-id>` → loads normally; strategy reference still resolves.
9. Navigate to `/live-trading` → the deployment using the archived strategy is STILL running (R14 supervisor opt-in proven via UI).

**VERIFY:**

- Type-name-to-confirm friction works (button disabled until match).
- After delete: strategy hidden from `/strategies` list.
- Historical backtest still shows the strategy name + reference (soft-delete preserves).
- Active deployment unaffected.

**PERSISTENCE:** Reload `/strategies` → strategy still archived. Reload `/backtests/<id>` → still resolves.

---

#### TJ-8: Operator daily check

**Spec file:** `frontend/tests/e2e/specs/operator-checkin.spec.ts`
**Tags:** (not smoke — operator workflow, nightly)
**Touches:** `/system`, `/market-data`, `/account`.

**Goal:** Pablo (as operator) does a daily health check: subsystem statuses, market-data freshness, account connection.

**STEPS:**

1. Navigate to `/system`.
2. Read each subsystem row (API, DB, Redis, IB Gateway, Workers, Parquet).
3. Read version + commit SHA + uptime card.
4. Navigate to `/market-data`.
5. Read the storage stats card (file count, bytes, asset-class breakdown).
6. Navigate to `/account`.
7. Click Health tab → IB connection state.

**VERIFY:**

- Every subsystem row has color + icon + plain-language text + last-checked timestamp.
- Version card shows real semver + 7-char SHA + uptime (NOT `v0.1.0` / `5d 14h 32m` from the old fake card).
- Market-data storage card shows real bytes from `/api/v1/market-data/status`.
- Account Health tab shows real IB connection state from snapshot probe (NOT a hardcoded green dot).

**PERSISTENCE:** Refresh `/system` → values update with new last-checked timestamp.

---

#### TJ-9: Honest settings page

**Spec file:** `frontend/tests/e2e/specs/settings-honest.spec.ts`
**Tags:** (not smoke — atomic verification, nightly)
**Touches:** `/settings`.

**Goal:** Pablo opens settings expecting an honest profile page — no fake elements, no broken buttons, real role from backend.

**STEPS:**

1. Navigate to `/settings`.

**VERIFY (negative space — what's NOT there, role-agnostic per iter-5 verify-e2e Issue B):**

- No hardcoded "Admin" badge that contradicts the backend's `role` field. The role badge IS allowed; it must render whatever `/api/v1/auth/me` returns (`admin` for the dev API-key user, `viewer` for production viewer users, etc.).
- No "Save Preferences" button.
- No "Trade Execution Alerts" / "Strategy Error Alerts" / "Daily Summary" toggle tiles.
- No "System Information" card (version, uptime, disk, API status, DB status all gone — moved to `/system`).
- No "Clear All Data" Danger Zone (the nonexistent `/api/v1/admin/clear-data` endpoint).

**VERIFY (positive space — what IS there):**

- Profile card with real `display_name`, `email`, `role` from `GET /api/v1/auth/me`.
- Display name shows real backend value (not "Demo User"). When auth is via dev API-key, displays `"API Key User"` + role `"admin"` (the dev seed). When auth is via real Entra ID JWT, displays the user's actual claims.
- Role badge value matches `auth/me.role` verbatim — no client-side substitution.

**PERSISTENCE:** Reload → same profile, no fakes returned.

---

#### TJ-10: Cancel a stuck research sweep

**Spec file:** `frontend/tests/e2e/specs/research-cancel.spec.ts`
**Tags:** (not smoke — manual operator action, nightly)
**Touches:** `/research`, `/research/[id]`.

**Goal:** Pablo launched a parameter sweep that's running too long. He cancels it.

**ARRANGE:**

- A research job in `running` status (seed via `POST /api/v1/research/sweeps`).

**STEPS:**

1. Navigate to `/research`.
2. Click into the running job → `/research/[id]`.
3. Click Cancel CTA → confirm dialog.
4. Confirm.
5. Status updates to `cancelled` within ~5s.

**VERIFY:**

- Cancel button visible only when status is `running`.
- POST `/api/v1/research/jobs/{id}/cancel` returns 200.
- UI shows `cancelled` status.

**PERSISTENCE:** Reload `/research/[id]` → still cancelled.

---

#### TJ-11: Error recovery (404 + render-throw)

**Spec file:** `frontend/tests/e2e/specs/error-pages.spec.ts`
**Tags:** (not smoke — atomic, nightly)
**Touches:** `/this-page-does-not-exist`, `/__e2e_throw`.

**Goal:** When Pablo mistypes a URL or hits a render-time error, he sees a useful error page with a back-to-safety CTA.

**STEPS (404):**

1. Navigate to `/this-page-does-not-exist`.
2. Read the page.

**VERIFY (404):**

- Styled `not-found.tsx` renders.
- "Back to dashboard" CTA links to `/dashboard`.
- MSAI logo present.

**STEPS (500):** 3. With `NEXT_PUBLIC_E2E_AUTH_BYPASS=1` set, navigate to `/__e2e_throw`. 4. Read the page.

**VERIFY (500):**

- `error.tsx` renders with "Something went wrong" + retry CTA.
- Without the env var, `/__e2e_throw` returns 404 (R21 short-circuit).

**STEPS (inline fetch error — separate concern):** 5. Playwright route intercepts `GET /api/v1/strategies/` → returns 500. 6. Navigate to `/strategies`. 7. Read the page.

**VERIFY (inline):**

- Inline error message "Failed to load strategies (500)" renders (page-level error state, not the route boundary).

---

### Revision R24 (supersedes T24 spec file list)

**Spec files (11 trader-journey files):**

```
frontend/tests/e2e/specs/
├── morning-checklist.spec.ts        TJ-1  @smoke
├── strategy-design.spec.ts          TJ-2  @smoke
├── backtest-flow.spec.ts            TJ-3
├── paper-deploy.spec.ts             TJ-4  @smoke
├── alert-response.spec.ts           TJ-5  @smoke
├── kill-all.spec.ts                 TJ-6  @smoke
├── strategy-archive.spec.ts         TJ-7
├── operator-checkin.spec.ts         TJ-8
├── settings-honest.spec.ts          TJ-9
├── research-cancel.spec.ts          TJ-10
└── error-pages.spec.ts              TJ-11
```

5 smoke-tagged journeys run on every PR (~5 min); full suite nightly. Each spec file mirrors one trader journey from R23, written as a single `test.describe()` block with multiple steps grouped logically. Auth fixture (X-API-Key + dev-bypass) runs once per spec.

**Selector conventions** (Trust-First + Product UI):

- `getByRole('button', { name: 'Save' })` for actions
- `getByLabel('Description')` for form fields
- `getByTestId('alert-row')` for repeatable structures (alerts table rows, deployment rows)
- `getByText('Latest 50 order attempts')` for static copy
- NEVER class-name selectors (fragile under shadcn refactors)

**Persistence assertions** (TJ-2, TJ-4, TJ-5, TJ-7, TJ-9 minimum): every journey that mutates state ends with `await page.reload()` + re-assert.

**ARRANGE seed scripts** live in `frontend/tests/e2e/fixtures/seed.ts` and call public API only (no raw DB writes per `rules/critical-rules.md` NO BUGS LEFT BEHIND).

---

## Plan-Review Loop Status — FINAL (re-stamped)

| Iter              | P0  | P1  | P2  | P3  | Verdict                                                    |
| ----------------- | --- | --- | --- | --- | ---------------------------------------------------------- |
| 1 merged          | 0   | 4   | 7   | 1   | NOT ready (R1-R12 applied)                                 |
| 2 merged          | 0   | 1   | 4   | 1   | NOT ready (R13-R19 applied)                                |
| 3 merged          | 0   | 0   | 0   | 3   | READY (R20-R22 polish applied)                             |
| 3.5 (E2E framing) | n/a | n/a | n/a | n/a | R23-R24: trader-journey UCs + spec files (Pablo directive) |

**Plan-review loop CLOSED.** Proceeding to Phase 4 (subagent-driven execution).
