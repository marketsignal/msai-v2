# Research: UI Completeness — single-PR, all gaps

**Date:** 2026-05-16
**Feature:** Single-PR closure of every gap surfaced in `docs/audits/2026-05-16-ui-surface-audit.md` (12 FAKE_UI, 7 MISSING_UI, 4 INCOMPLETE_UI, 3 DEAD_NAV, 4 backend safety findings, 1 Phase-1 policy decision, Playwright spec graduation across every shipped surface).
**Researcher:** research-first agent

## Scope filter

Targets below are the **external** libraries/patterns the design phase must commit to. MSAI-specific code (`api.ts` shape, `useAuth` extension, `providers.tsx` config) was answered by reading the repo, not by external research — see §"Internal findings (read from code)" near the end.

The branch already pins:

| Surface            | Pin                 |
| ------------------ | ------------------- |
| Next.js            | 15.5.12 + Turbopack |
| React              | 19.1.0              |
| TanStack Query     | 5.100.7             |
| shadcn registry    | shadcn@3.8.5        |
| Tailwind           | 4 (PostCSS)         |
| sonner             | 1.7.4               |
| Recharts           | 3.7.0               |
| lightweight-charts | 5.1.0               |
| @playwright/test   | 1.59.1              |
| SQLAlchemy         | 2.0.36              |
| FastAPI            | 0.133.0             |
| ib_async           | 1.0.0               |
| Pydantic           | 2.10.0              |

All "latest stable" deltas below are checked against these pins.

---

## Libraries Touched (summary)

| Library               | Our Version     | Latest Stable                     | Breaking deltas                                                                                          | Design impact in this PR                                                                                                                                                                                                               |
| --------------------- | --------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Next.js               | 15.5.12         | 16.x (early 2026)                 | Stay on 15; do not upgrade in this PR. App-router conventions stable.                                    | Use `error.tsx` / `not-found.tsx` / `loading.tsx` per route segment; force `"use client"` only where state/effects/charts need it.                                                                                                     |
| React                 | 19.1.0          | 19.x                              | strict-mode + Suspense interactions; refs are stable.                                                    | Keep `useRef` mutation outside render (already done in `use-job-status-query.ts`).                                                                                                                                                     |
| TanStack Query        | 5.100.7         | 5.x                               | v5 `onMutate`/`onSettled` rollback pattern is THE pattern; legacy `onSuccess`-only style is discouraged. | Use the documented `onMutate → cancelQueries → snapshot → setQueryData → onError rollback → onSettled invalidate` shape for PATCH/DELETE mutations. Polling: `refetchInterval` matching server cadence (30 s for `_PROBE_INTERVAL_S`). |
| shadcn/ui             | registry @3.8.5 | —                                 | `toast` is deprecated in favor of `sonner` (already adopted).                                            | Install `form`, `skeleton`, `pagination` registry components. Keep `sonner` for status feedback.                                                                                                                                       |
| react-hook-form + zod | not installed   | 7.x / 4.x                         | —                                                                                                        | New dependency: install `react-hook-form`, `@hookform/resolvers`, `zod`. Required by Strategy edit form + Settings profile form.                                                                                                       |
| SQLAlchemy            | 2.0.36          | 2.0.x                             | —                                                                                                        | Use `do_orm_execute` + `with_loader_criteria` for global `deleted_at IS NULL` filter; bypass via `execution_options(include_deleted=True)` for backtest-history reads of archived strategies.                                          |
| fastapi-cache2        | not installed   | 0.2.x                             | —                                                                                                        | **Do not introduce.** Extend the existing `IBProbe` background-task pattern instead — it already exists and matches the council's "serve from cached probe state" preference.                                                          |
| ib_async              | 1.0.0           | 2.x line (`ib_api_reloaded` fork) | We are pinned to 1.0.0; do NOT bump.                                                                     | Add a singleton-with-mutex IB connection, not per-request `connectAsync`. Cap `client_id` allocation.                                                                                                                                  |
| Recharts              | 3.7.0           | 3.x                               | Still client-only; `"use client"` directive required.                                                    | Wrap every chart host in `"use client"`. No SSR path for charts.                                                                                                                                                                       |
| Playwright            | 1.59.1          | 1.x                               | MSAL + storageState has known issues.                                                                    | Default to `X-API-Key` (NEXT_PUBLIC_MSAI_API_KEY) for E2E; persist nothing MSAL-flavored to `.auth/`.                                                                                                                                  |

---

## Per-target detail

### Target 1: Next.js 15 app-router data fetching, polling, segment files

- **Library / version:** Next.js 15.5.12 + Turbopack, React 19.1.0.
- **Sources (≥2):**
  - [TanStack Query — Advanced Server Rendering](https://tanstack.com/query/latest/docs/framework/react/guides/advanced-ssr) — accessed 2026-05-16
  - [Next.js docs — File conventions: not-found, error, loading](https://nextjs.org/docs/app/api-reference/file-conventions/not-found) — accessed 2026-05-16
  - [DevAndDeliver — Next.js 15 Error Handling best practices](https://devanddeliver.com/blog/frontend/next-js-15-error-handling-best-practices-for-code-and-routes) — accessed 2026-05-16
- **Current best practice:**
  - Server Components prefetch; Client Components own polling / mutation / charts / forms. Hybrid: server-prefetch a `queryKey`, hand off to a client component that calls `useQuery` (or `useSuspenseQuery`) — initial render skips the loading state.
  - For pages with frequent polling (account summary, alerts, live status), do NOT use a Server Component as the polling owner. Pure client component is the cheapest path.
  - `revalidate` and `cache: "no-store"` are about RSC fetch, not TanStack Query. Real-money pages should not use Server Component `fetch()` for live values; the auth surface is JWT-from-MSAL which is client-resident anyway.
  - `error.tsx` MUST be a client component (`"use client"`) and accepts `{ error, reset }`. Place a global `app/global-error.tsx` for last-resort handling and a `app/not-found.tsx` for 404s.
  - `loading.tsx` is a Suspense fallback — fine for route transitions, but per-component skeletons (TanStack Query `isPending`) are still required for in-page state.
- **Design impact (this PR):**
  - All new pages (`/alerts`, `/account`) stay client-only — `"use client"` at the top — because they poll, hold local form state, and render charts.
  - Add `app/not-found.tsx`, `app/error.tsx`, and `app/global-error.tsx` at the app root for the global error/404 surface (D-2).
  - Per-segment `loading.tsx` for slow surfaces (`/backtests/[id]`, `/account`) is a polish add — fine to skip if a TanStack `isPending` skeleton already exists.
  - Do NOT migrate existing client-only pages to Server Components. Out of scope.
- **Test implication:**
  - Add a Playwright spec asserting `app/not-found.tsx` renders on an unknown route and `app/error.tsx` renders on a thrown error.
  - For polling pages, add a Playwright assertion that the surface updates within `refetchInterval + 1 s` after server state changes (or skip if the test is unstable; mock-data API state change is unavailable for IB-backed surfaces).
- **Open risks:**
  - Next 16 is approaching. If the Codex loop nudges toward upgrade, defer — `error.tsx` API changes are minor but force a re-test sweep.

### Target 2: shadcn/ui primitives for new surfaces

- **Library / version:** shadcn registry @3.8.5 against Tailwind 4. Installed primitives: `alert-dialog`, `avatar`, `badge`, `button`, `card`, `dialog`, `dropdown-menu`, `input`, `label`, `popover`, `select`, `separator`, `sheet`, `sonner`, `table`, `tabs`, `textarea`, `toggle`, `toggle-group`, `tooltip`. NOT installed: `form`, `skeleton`, `pagination`, `checkbox`, `data-table` shell.
- **Sources (≥2):**
  - [shadcn/ui — React Hook Form](https://ui.shadcn.com/docs/forms/react-hook-form) — accessed 2026-05-16
  - [shadcn/ui — Skeleton](https://ui.shadcn.com/docs/components/radix/skeleton) — accessed 2026-05-16
  - [shadcn/ui — Sonner](https://ui.shadcn.com/docs/components/radix/sonner) — accessed 2026-05-16
  - [shadcn/ui — Data Table](https://ui.shadcn.com/docs/components/radix/data-table) — accessed 2026-05-16
  - [shadcn/ui — Pagination](https://ui.shadcn.com/docs/components/radix/pagination) — accessed 2026-05-16
  - [Frontkit — shadcn/ui 48-component WCAG 2.2 AA audit](https://thefrontkit.com/blogs/shadcn-ui-accessibility-audit-2026) — accessed 2026-05-16
  - [shadcn-ui/ui issue #8088 — muted contrast fails AA](https://github.com/shadcn-ui/ui/issues/8088) — accessed 2026-05-16
- **Current best practice:**
  - `Form` primitive ships as the canonical wrapper around `react-hook-form` + `@hookform/resolvers/zod`. The discussion to "skip rhf and roll-your-own" is open but the lead maintainer's recommendation remains rhf+zod. For this PR (Strategy edit form, Settings profile, possibly readiness widget) install `form` + rhf + zod.
  - `Skeleton` is a one-liner `<div className="animate-pulse bg-muted rounded">` — install the primitive for consistency; rolling your own is fine, but a shared component anchors a single visual rhythm.
  - `Sonner` (already wired in `providers.tsx`) is the canonical toast — call `toast.success` / `toast.error` from mutation `onSuccess` / `onError`. The deprecated `toast` primitive should not be added.
  - Data Table: shadcn's docs put `@tanstack/react-table` on top of the `Table` primitive for sort + filter + pagination. For `/alerts` (likely <500 rows), client-side pagination with `react-table` is enough. For `/backtests/history` and `/live/audits/{id}`, also client-side.
  - `AlertDialog` is Radix-backed and ships with focus-trap, ESC, and aria roles. Audit-flagged contrast issue: default `focus-visible:ring-ring/50` is below 3:1 in light themes — set `ring-1 ring-ring` (full opacity) or rely on the dark-only design preference in `CLAUDE.md`.
- **Design impact (this PR):**
  - Install: `form`, `skeleton`, `pagination`, `checkbox` (Settings preferences if any survive), via `pnpm dlx shadcn@latest add form skeleton pagination checkbox`. Add deps: `react-hook-form`, `@hookform/resolvers`, `zod`.
  - For Strategy DELETE confirm: use the installed `alert-dialog`. The button must be `variant="destructive"`, copy must be plain-language ("Archive strategy?"), and the action label MUST say what it does ("Archive", not "OK").
  - Skip `data-table`/`@tanstack/react-table` install — `/alerts` should ship with the existing `Table` + manual sort/pagination since we currently use that everywhere else. Avoid the new dep unless the audit-log row count justifies it.
  - For PATCH (`Strategy edit`, `Settings preferences`) wire `react-hook-form` + zod resolver. `zod` schema mirrors backend Pydantic `StrategyUpdate` / `UserPreferencesUpdate`.
  - Tailwind 4: theme tokens live in `app/globals.css` via `@theme inline { ... }` (OKLCH). Do not introduce a new palette — reuse existing.
- **Test implication:**
  - For every confirm-dialog, Playwright test should assert focus lands on the Cancel button (Radix default; F-1/D-1 regressions).
  - Toast assertions use `getByRole('status')` (Sonner renders `role="status"` for success and `role="alert"` for error).
- **Open risks:**
  - `react-hook-form` 8 is in alpha; pin `^7.54.0`. Mixing `^8` would force a Form primitive refresh.
  - Tailwind 4 + shadcn registry installs require Tailwind plugin compatibility — last-mile risk if `pnpm dlx shadcn add form` fails. Test the install before committing to the form-based design.

### Target 3: TanStack Query 5 mutations + polling

- **Library / version:** `@tanstack/react-query` 5.100.7. Existing pattern in `frontend/src/lib/hooks/use-symbol-mutations.ts` (mutation → `qc.invalidateQueries`) and `use-job-status-query.ts` (status-aware `refetchInterval`).
- **Sources (≥2):**
  - [TanStack Query v5 — Optimistic Updates](https://tanstack.com/query/v5/docs/framework/react/guides/optimistic-updates) — accessed 2026-05-16
  - [TanStack Query v5 — Mutations](https://tanstack.com/query/v5/docs/framework/react/guides/mutations) — accessed 2026-05-16
  - [TkDodo — Mastering Mutations](https://tkdodo.eu/blog/mastering-mutations-in-react-query) — accessed 2026-05-16
- **Current best practice:**
  - PATCH-style mutations have two patterns in v5:
    1. **Cache-update optimistic** — `onMutate → cancelQueries → snapshot → setQueryData → return context; onError → setQueryData(snapshot); onSettled → invalidateQueries`. Used when UI must reflect the change before server roundtrip.
    2. **`pending`-variables UI** — show the `useMutation` `variables` directly in the rendered row while `isPending`, no cache update, no rollback. Cleaner for one-row edits.
  - Real-money UI (Strategy archive, account view) should NOT do optimistic deletion. PATCH config edits can show optimistic, but archive should wait for server 200 to swap the row out.
  - Polling cadence:
    - `/live/status`: 10–15 s when there are active deployments, 60 s when none. `refetchIntervalInBackground: false`.
    - `/account/summary`, `/account/portfolio`, `/account/health`: align to `_PROBE_INTERVAL_S=30` on the backend. Polling faster than the probe wastes IB Gateway sockets and gives no fresher data.
    - `/alerts/`: 60 s polling sufficient; or `staleTime: 30_000` and rely on user navigation to refetch.
  - Query keys: include every variable that scopes the query (`["alerts", { limit, severity }]`). Invalidate by partial key for cross-mutation cache busts (`qc.invalidateQueries({ queryKey: ["alerts"] })`).
- **Design impact (this PR):**
  - All new hooks (`useStrategiesQuery`, `useStrategyMutation`, `useAccountSummary`, `useAccountPortfolio`, `useAccountHealth`, `useAlertsQuery`, `useResearchCancelMutation`, `useLiveAuditQuery`) follow the existing `use-symbol-mutations.ts` shape.
  - For Strategy DELETE → soft-delete: use **non-optimistic** mutation with `onSuccess → invalidate "strategies" + redirect`. Optimistic removal risks showing an empty page if backend 422s on FK violations.
  - For Strategy PATCH (config + description): non-optimistic; reload `["strategies", id]` after success.
  - For Settings profile / preferences PATCH: pending-variables UI is fine — small form, single field.
  - Add a `useAccountSummary` with `refetchInterval: 30_000` and `staleTime: 25_000`. Display "as of HH:MM:SS" in the card to be honest about staleness.
- **Test implication:**
  - Playwright: after archiving a strategy, list should refresh (test asserts the row is gone after `await page.waitForResponse(/\/strategies/)`).
  - Unit (vitest) is overkill for one-off helpers (memory rule `feedback_drop_vitest_for_one_off_pure_helpers`); rely on Playwright assertion.
- **Open risks:** None — pattern is in-codebase and current.

### Target 4: FastAPI cached-probe / TTL pattern for IB endpoints

- **Library / version:** FastAPI 0.133.0; no `fastapi-cache2` installed. Existing pattern: module-singleton `IBProbe` started via lifespan in `api/account.py:46-61`, polled every 30 s.
- **Sources (≥2):**
  - [fastapi-cache2 (long2ice/fastapi-cache)](https://github.com/long2ice/fastapi-cache) — accessed 2026-05-16
  - [DEV — Caching in FastAPI](https://dev.to/sivakumarmanoharan/caching-in-fastapi-unlocking-high-performance-development-20ej) — accessed 2026-05-16
  - [Greeden blog — FastAPI Performance Tuning & Caching Strategy 101 (2026)](https://blog.greeden.me/en/2026/02/03/fastapi-performance-tuning-caching-strategy-101-a-practical-recipe-for-growing-a-slow-api-into-a-lightweight-fast-api/) — accessed 2026-05-16
- **Current best practice (matched to our shape):**
  - For periodically-refreshed values shared across requests, the **background-task-fills-singleton** pattern (already used by `IBProbe`) is the canonical "no new dep" answer. `fastapi-cache2` is overkill for two endpoints.
  - `cachetools.TTLCache` wrapped in a `Depends()` is the second-cheapest path — useful when refreshes must be lazy (no background task).
  - Council's binding constraint specifies: 15–30 s TTL OR serve from `_ib_probe` OR Refresh-button gating. The cleanest path that satisfies all three constraints simultaneously is: **extend `IBProbe` (or add `IBAccountProbe`) to also cache `accountSummary` / `portfolio` snapshots; `/account/summary` and `/account/portfolio` read from the cached snapshot; a `?refresh=1` query forces a fresh pull (rate-limited to once per 5 s).** This reuses the existing lifespan + cancel pattern in `account.py:46-89`.
- **Design impact (this PR):**
  - **Backend P1-1-backend:** Create `IBAccountSnapshot` that owns one `ib_async.IB` instance, a single allocated `client_id` (capped at 999), and a `last_refreshed_at` timestamp. Periodic task refreshes every 30 s. `/summary` and `/portfolio` read from `_snapshot`. Remove `_ACCOUNT_CLIENT_COUNTER` (the unbounded `itertools.count`).
  - Do NOT introduce `fastapi-cache2` or `cachetools` as a dep — extending an existing pattern is cheaper than a new one.
  - Connection: open ONCE at startup, retain across refreshes; reconnect-with-backoff on disconnect.
- **Test implication:**
  - Integration test: hit `/account/summary` 100 times in 1 s; assert no new IB connections were opened (mock `ib_async.IB` with a connection-count spy).
  - Playwright: navigate to `/account`, observe "As of HH:MM:SS" badge updates every ~30 s without page reload.
- **Open risks:**
  - If the singleton dies (gateway restart), the probe must detect via `IB.isConnected()` and reconnect. Without this, stale cache plus dead connection silently lies.

### Target 5: SQLAlchemy 2.0 soft-delete pattern for `strategies`

- **Library / version:** SQLAlchemy 2.0.36, alembic 1.14.
- **Sources (≥2):**
  - [SQLAlchemy discussion #11468 — Built-in Soft Delete](https://github.com/sqlalchemy/sqlalchemy/discussions/11468) — accessed 2026-05-16
  - [Medium — Mastering Soft Delete: Advanced SQLAlchemy Techniques](https://theshubhendra.medium.com/mastering-soft-delete-advanced-sqlalchemy-techniques-4678f4738947) — accessed 2026-05-16
  - [sqlalchemy-easy-softdelete on PyPI](https://pypi.org/project/sqlalchemy-easy-softdelete/) — accessed 2026-05-16
- **Current best practice:**
  - `deleted_at: Mapped[datetime | None]` column on `strategies`. NULL = active.
  - Filter via `event.listens_for(Session, "do_orm_execute")` + `with_loader_criteria(Strategy, Strategy.deleted_at.is_(None), include_aliases=True)`.
  - Add an opt-out: `session.execute(stmt, execution_options={"include_deleted": True})` so backtest history can resolve archived strategies by `strategy_id`. Inside the listener, skip applying the criteria when `orm_execute_state.execution_options.get("include_deleted")` is truthy.
  - DO NOT use `is_archived: bool`. `deleted_at` is timestamped and ordered — superior for "soft-deleted 7 days ago" follow-ups.
  - Migration must be additive-only per `.claude/rules/database.md`: `ADD COLUMN deleted_at TIMESTAMP NULL`. No backfill needed (no soft-deleted rows yet).
  - Foreign keys: backtests reference `strategy_id`. ON DELETE behavior doesn't matter (we're not deleting). The backtest query path needs to USE `include_deleted=True` when fetching the strategy row for a historical backtest — otherwise old backtests can't render.
- **Design impact (this PR):**
  - Migration `add_deleted_at_to_strategies.py`: `ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE NULL` on `strategies`. Add a partial index `CREATE INDEX strategies_active_idx ON strategies (id) WHERE deleted_at IS NULL` for the active list path.
  - `models/strategy.py`: add `deleted_at: Mapped[datetime | None]`.
  - `api/strategies.py:197`: DELETE handler → `strategy.deleted_at = func.now()` instead of `session.delete()`. Return 204.
  - `core/database.py` (or new `core/soft_delete.py`): register the `do_orm_execute` listener globally so list/get queries auto-filter.
  - `api/backtests.py` strategy-resolve helper: pass `execution_options(include_deleted=True)`.
- **Test implication:**
  - Integration: create strategy → run backtest → archive strategy → list strategies (assert hidden) → fetch backtest by id (assert strategy still resolves).
  - Integration: assert direct query `select(Strategy).where(Strategy.id == archived_id)` returns None unless `include_deleted=True` is set.
- **Open risks:**
  - `with_loader_criteria` does not auto-apply to RAW SQL or `session.get()` — verify the existing code paths use ORM queries (they do, per the strategies router).

### Target 6: ib_async connection management

- **Library / version:** ib_async 1.0.0 (pinned). Existing code (`services/ib_account.py:62-89`) opens a fresh connection per request.
- **Sources (≥2):**
  - [ib_async on PyPI](https://pypi.org/project/ib_async/) — accessed 2026-05-16
  - [ib_async docs (ib-api-reloaded.github.io)](https://ib-api-reloaded.github.io/ib_async/) — accessed 2026-05-16
  - [.claude/rules/nautilus.md gotcha #3](./../../.claude/rules/nautilus.md) — accessed 2026-05-16 (project-internal source)
- **Current best practice:**
  - One IB connection per process. Pool only if multiple concurrent requests need parallel IB calls (we don't — account read is sequential).
  - `client_id` must be unique across every concurrent IB connection on the same gateway. Allocation:
    - Reserve `0` for IB master.
    - Live `TradingNode`(s) use `client_id` derived from `hash(deployment_slug)` (already in `live_node_config`).
    - Reads (account, instruments-refresh CLI) need a STATIC, well-known `client_id` per process. Recommendation: `client_id = 900` (account snapshot), `client_id = 901` (instruments refresh CLI), `client_id = 902` (probe ping). No rotation. Document in `nautilus.md`.
  - Detect stale connection: `IB.isConnected()` returns False after gateway restart. Wrap snapshot refresh in `if not ib.isConnected(): await ib.connectAsync(...)`.
- **Design impact (this PR):**
  - `IBAccountSnapshot` opens ONE `ib_async.IB()` with `client_id=900` at app startup, holds it for the process lifetime, refreshes via `ib.reqAccountSummaryAsync()` every 30 s.
  - Remove `itertools.count(start=900)`. Use the bare constant `_ACCOUNT_CLIENT_ID = 900`.
  - On gateway 502/disconnect: log + attempt one reconnect, then back off; do not loop reconnects (the `IBProbe` background task already handles "is gateway alive" signal).
- **Test implication:**
  - Integration: kill IB Gateway mid-test; assert snapshot returns last-known values + `stale: true` flag; assert no `client_id` collision logs after gateway restart.
- **Open risks:**
  - If a CLI invocation collides on `client_id=900` while the backend is up, IB silently disconnects the older session. Mitigation: CLI uses 901+ as noted in nautilus.md.

### Target 7: Playwright auth + selectors for Next.js + MSAL

- **Library / version:** @playwright/test 1.59.1; auth via Azure Entra (MSAL frontend, PyJWT backend); backend accepts `X-API-Key` header.
- **Sources (≥2):**
  - [Playwright — Authentication](https://playwright.dev/docs/auth) — accessed 2026-05-16
  - [microsoft/playwright issue #17328 — Reusing auth with MSAL + Azure B2C does not work reliably](https://github.com/microsoft/playwright/issues/17328) — accessed 2026-05-16
  - [Checkly — Playwright Authentication](https://www.checklyhq.com/docs/learn/playwright/authentication/) — accessed 2026-05-16
- **Current best practice:**
  - `storageState` works for cookie/sessionStorage auth, but **MSAL + Azure B2C/Entra has a documented failure mode** (issue #17328) where reusing storageState shows the user as unauthenticated. MSAL stores tokens with timestamps in localStorage; expiry triggers re-redirect.
  - For server-tested apps that accept a static API key, prefer `extraHTTPHeaders: { "X-API-Key": process.env.TEST_API_KEY }` on the Playwright `use` block — this avoids the MSAL flow entirely.
  - Selector preference: `getByRole` first, `getByTestId` fallback when role isn't unique. `getByRole('button', { name: 'Archive strategy' })` is more durable than `getByTestId('archive-btn')` — `name` matches accessible name, which is also what screen readers see.
- **Design impact (this PR):**
  - Use `extraHTTPHeaders: { "X-API-Key": process.env.NEXT_PUBLIC_MSAI_API_KEY }` in `playwright.config.ts` for all specs. Do NOT attempt MSAL storageState reuse — the project precedent (only an `.auth/` placeholder exists) confirms this hasn't been wired and the MSAL issue makes the cost high.
  - For UI assertions, prefer `getByRole`. Add `data-testid` ONLY where role + name is ambiguous (e.g., two "Cancel" buttons on the same page).
  - Per-route spec organization: `frontend/tests/e2e/specs/{auth,strategies,backtests,live,alerts,account,settings,market-data}.spec.ts`. Tag smoke-critical specs with `@smoke` for PR-time CI.
  - Activate `docs/ci-templates/e2e.yml` → `.github/workflows/e2e.yml` per audit P2-V2.
- **Test implication:**
  - Each spec must call `await page.context().clearCookies()` if storageState ever drifts; with X-API-Key on every request, this is moot.
  - `forbidOnly` is already on for CI — no change needed.
- **Open risks:**
  - X-API-Key is dev-mode auth; CI must populate `NEXT_PUBLIC_MSAI_API_KEY` from a secret. The current pipeline doesn't have a CI E2E job yet, so this is a fresh secret + workflow.

### Target 8: Recharts under React 19

- **Library / version:** Recharts 3.7.0. Existing usage: `EquityChart` (empty array problem, F-11), backtest detail charts.
- **Sources (≥2):**
  - [Recharts npm](https://www.npmjs.com/package/recharts) — accessed 2026-05-16
  - [recharts/recharts discussion #6390 — SSR in recharts](https://github.com/recharts/recharts/discussions/6390) — accessed 2026-05-16
- **Current best practice:**
  - Recharts components require `"use client"`. They use `useEffect` + `useContext` internally; SSR renders an empty container.
  - For live-updating charts (live equity curve): keep the data array small (windowed last-N), use `isAnimationActive={false}` when updating frequently — animation re-runs on every data change otherwise. Drop new points in via `setData(prev => [...prev.slice(-N), point])`.
  - Y-axis currency formatting: `tickFormatter={(v) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(v)}`. Define the formatter once outside the component to avoid re-render churn.
- **Design impact (this PR):**
  - F-11 fix path: **drop EquityChart from the dashboard for now**. We do not have a backend `/api/v1/live/equity-curve` endpoint and shipping a fake one is forbidden (lies). Replace the slot with an honest card — recent alerts feed (driven by `/api/v1/alerts/`) — or the existing `/portfolio/runs` summary if scope permits.
  - For `/backtests/[id]` keep current Recharts setup; honest historical data.
- **Test implication:**
  - Playwright: assert that the dashboard does NOT contain "No equity data available yet." — replaced by an honest card.
- **Open risks:**
  - If Pablo insists on an equity curve, scope creep into a backend daily-pnl aggregation endpoint. Surface as an open question in plan review.

### Target 9: Accessibility tokens + WCAG AA on dark theme

- **Library / version:** shadcn/ui dark theme via OKLCH tokens in `app/globals.css`; Geist font.
- **Sources (≥2):**
  - [Frontkit — shadcn/ui WCAG 2.2 AA audit (48 components)](https://thefrontkit.com/blogs/shadcn-ui-accessibility-audit-2026) — accessed 2026-05-16
  - [shadcn-ui/ui issue #8088](https://github.com/shadcn-ui/ui/issues/8088) — accessed 2026-05-16
- **Current best practice:**
  - shadcn AlertDialog/Dialog focus and aria are correct out of the box (Radix). Verify focus-visible ring contrast — default `ring-ring/50` fails AA in some themes.
  - Skeleton should add `aria-busy="true"` on the parent container so screen readers announce loading; remove on data arrival.
  - Trust-First text contrast: 7:1 (AAA) preferred for real-money displays. Reuse `--foreground` (existing OKLCH) which already meets AAA on the shipped dark palette; only `muted-foreground` borderline AA — avoid for primary financial values.
- **Design impact (this PR):**
  - For destructive-action surfaces (Strategy archive, kill-all already exists), use `--destructive` (button) and full-opacity `ring-1 ring-ring` on focus-visible.
  - Skeleton loaders for `/alerts`, `/account`, `/strategies` redesign wrap content in `<div aria-busy="true">` while `isPending`.
  - All financial values use `--foreground` not `--muted-foreground`.
- **Test implication:**
  - Add a single `@a11y` Playwright spec that runs axe-core against the dashboard, `/alerts`, `/account`, `/strategies`, `/settings`. Fail on serious + critical violations only.
- **Open risks:** None — color tokens already shipped.

---

## Internal findings (read from code, not external research)

These were answered by reading the repo at 2026-05-16; no library research needed. Recording them for the design phase.

- **`frontend/src/lib/auth.ts` `AuthUser` type:** currently `{ name, email }` only. Extend to `{ name, email, role, displayName }` by reading from `/api/v1/auth/me` (claims projection). Implementation: `useAuth` already returns `account`; add a `useUserProfile` hook that wraps `useQuery({ queryKey: ['auth', 'me'], queryFn: () => apiGet('/api/v1/auth/me', token) })`. Settings page consumes that hook directly. The MSAL `account.idTokenClaims` is NOT the source of truth for `role` — backend `/auth/me` is.
- **`frontend/src/lib/api.ts` typing pattern:** every new endpoint follows the existing pattern — declare a response interface, declare a typed helper that calls `apiGet`/`apiPost`. Add new helpers for `getAlerts`, `getAccountSummary`, `getAccountPortfolio`, `getAccountHealth`, `patchStrategy`, `archiveStrategy`, `cancelResearchJob`, `getLiveAudits`, `getMarketDataStatus`, `getReadiness`, `getAuthMe`.
- **`frontend/src/components/providers.tsx` QueryClient defaults:** `staleTime: 30_000`, `gcTime: 5*60_000`, `refetchOnWindowFocus: false`, `retry: 1`. These are sane for our polling cadence — do not change globally; per-query overrides handle the few outliers (e.g., job-status query has its own `refetchInterval` policy).
- **Existing TanStack patterns:** `useInventoryQuery`, `useJobStatusQuery`, `useRemoveSymbol` — the templates for everything in this PR. No new state-management pattern needed.

---

## Not Researched (with justification)

- **TradingView Lightweight Charts (5.1.0):** present in the bundle for `/market-data/chart`. No new UI in this PR uses it (the dashboard equity chart is being removed, not migrated to lightweight-charts). N/A.
- **NautilusTrader:** no Strategy / live-supervisor changes in this PR. Audit findings are UI + safety only.
- **arq:** no worker changes.
- **DuckDB / Parquet:** no data-layer changes.
- **Azure MSAL details:** existing `useAuth` is stable; we extend with `useUserProfile` query, not touch MSAL config.

---

## Open Risks

1. **MSAL + Playwright storageState** is a known broken combo (issue #17328). The X-API-Key bypass works for E2E but means CI never exercises MSAL — accept this gap, document in the Playwright README.
2. **ib_async 1.0.0 is pinned; upstream is on 2.x** (`ib_api_reloaded`). Do NOT bump in this PR — out of scope. File a follow-up note in CHANGELOG.
3. **`react-hook-form` install risk** — Tailwind 4 + shadcn registry `form` install is the last-mile risk. Test the install in the first commit before designing every form against it.
4. **Equity-curve removal vs backend endpoint** — Pablo may reject "drop the card" as the F-11 fix and ask for a real `/live/equity-curve` endpoint. Surface this in plan review BEFORE committing to a layout.
5. **Phase-1 strategy templates policy decision** is a Phase-3 deliverable, not Phase-2 research. Flagged here only because it gates Gap-3 UI. Two options: (a) cut `api/strategy_templates.py` + service, (b) amend CLAUDE.md to allow scaffolder. Council Maintainer + Contrarian + Simplifier + Pragmatist consensus = blocker.
6. **No CI E2E job today** — activating `docs/ci-templates/e2e.yml` requires `NEXT_PUBLIC_MSAI_API_KEY` as a GitHub secret AND the workflow file. Slice these as two commits at end of PR.
7. **AlertDialog focus-visible contrast** — apply full-opacity ring in our shadcn theme override or accept the known low contrast on focus.
