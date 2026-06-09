# Research: Dashboard Account Selector (PR4)

**Date:** 2026-06-08
**Feature:** Global dashboard account selector + explicit confirmed deploy-target + server-side real-money confirmation gate + explicit `is_real_money` account attribute.
**Researcher:** research-first agent
**Worktree:** `.worktrees/dashboard-account-selector` (branch `feat/dashboard-account-selector`)

---

## Libraries Touched

| Library                 | Our Version                                             | Latest Stable | Breaking Changes vs ours                                                                                                            | Source                                                                                                                                                                        |
| ----------------------- | ------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Next.js                 | `15.5.12` (`next` + `eslint-config-next`)               | 16.x (16.2.x) | We are on 15; not upgrading in this PR. No breaking change consumed.                                                                | [package.json](frontend/package.json) (2026-06-08)                                                                                                                            |
| React                   | `19.1.0`                                                | 19.x          | None                                                                                                                                | [package.json](frontend/package.json) (2026-06-08)                                                                                                                            |
| shadcn/ui (CLI)         | `shadcn@3.8.5`; primitives via unified `radix-ui@1.4.3` | shadcn 3.x    | shadcn now ships **Radix and Base UI** registries; we use the Radix registry. `Select` exists locally; `Command`/`Combobox` do NOT. | [package.json](frontend/package.json) (2026-06-08), [ui.shadcn.com select](https://ui.shadcn.com/docs/components/radix/select)                                                |
| `usehooks-ts`           | `3.1.1`                                                 | 3.x           | None — provides `useLocalStorage` (SSR-safe) and `useReadLocalStorage`. Already a dep.                                              | [package.json](frontend/package.json) (2026-06-08)                                                                                                                            |
| `@tanstack/react-query` | `5.100.7`                                               | 5.x           | None — `QueryClientProvider` already mounted in `providers.tsx`.                                                                    | [providers.tsx:49](frontend/src/components/providers.tsx) (2026-06-08)                                                                                                        |
| FastAPI                 | `>=0.133.0`                                             | 0.13x         | None relevant                                                                                                                       | [pyproject.toml](backend/pyproject.toml) (2026-06-08)                                                                                                                         |
| Pydantic                | `>=2.10.0` (V2)                                         | 2.x           | V1 `@root_validator`/`@validator` gone — use `@model_validator`/`@field_validator` (we already do).                                 | [pyproject.toml](backend/pyproject.toml), [FastAPI 422 guide](https://www.getorchestra.io/guides/pydantic-http-exceptions-handling-validation-errors-in-fastapi) (2026-06-08) |
| SQLAlchemy              | `>=2.0.36`                                              | 2.0.x         | None                                                                                                                                | [pyproject.toml](backend/pyproject.toml) (2026-06-08)                                                                                                                         |
| Alembic                 | `>=1.14.0`                                              | 1.18.x        | None relevant                                                                                                                       | [Alembic autogenerate docs](https://alembic.sqlalchemy.org/en/latest/autogenerate.html) (2026-06-08)                                                                          |
| Typer                   | `>=0.15.0`                                              | 0.15.x        | None                                                                                                                                | [pyproject.toml](backend/pyproject.toml) (2026-06-08)                                                                                                                         |

---

## Per-Library / Per-Area Analysis

### 1. Next.js 15 App Router — global selector state (persist across navigation AND full reload)

**Versions:** ours=`next 15.5.12` / `react 19.1.0`.

**What exists today (read, not assumed):**

- The app shell is a single client tree: `RootLayout` (`app/layout.tsx:33-39`) → `AuthProvider` → `QueryProviders` → `TooltipProvider` → `AppShell`. `AppShell` (`components/layout/app-shell.tsx:13`) is `"use client"` and renders `<Sidebar/>` + `<Header/>` + `<main>`. The header (`components/layout/header.tsx:20`) is the natural home for a global selector — it is rendered once and wraps every protected route.
- **There is no existing global client-state pattern.** No `createContext` app store, no Zustand, no `nuqs`. Grep found only `usehooks-ts` (`use-inventory-query.ts:5`, `useDebounceValue`), `useSearchParams` used **per-page** on `market-data/page.tsx:51` and `market-data/chart/page.tsx:47`, and two local `createContext` usages inside `ui/form.tsx` / `ui/toggle-group.tsx` (component-internal only).
- `@tanstack/react-query` `QueryClientProvider` is already mounted (`providers.tsx:49-67`) but pages mostly use manual `useState`/`useEffect` fetches (e.g. `live-trading/page.tsx:30-90`).
- The shell already renders entirely client-side with an MSAL-init spinner gate (`app-shell.tsx:54-63`); the protected content does **not** stream meaningful SSR HTML for authenticated users.

**Recommended pattern (lowest complexity that meets "persists across navigation AND reload"):**
React **Context provider mounted in the client app shell + `localStorage` via the already-installed `usehooks-ts` `useLocalStorage`**, read behind a hydration-safe mount flag. Rationale:

- **Navigation persistence** is free — the provider lives above the `<main>` route outlet in `AppShell`, so App Router client navigations never unmount it.
- **Reload persistence** comes from `localStorage`. `usehooks-ts.useLocalStorage` already implements the SSR-safe init (returns the default on first render, syncs after mount) so we avoid hand-rolling it.
- A lightweight store (Zustand) is also valid but adds a dependency for a single string + helpers; Context is sufficient for one selected-account value consumed by a few components. KISS (rules/principles.md) favors Context here.

**Why NOT a URL search param (`useSearchParams`/`nuqs`) as the source of truth:** the PRD wants the selection to persist across _all_ navigation including to pages that are not account-scoped (backtests, strategies) and across a cold reload of any deep link; a URL param would either have to be threaded onto every link or be lost when navigating to a non-scoped route. `localStorage` is the durable store; a URL param is an optional shareable override at most. Default = "All".

**Why NOT cookies + `next/headers` server read** (the generic 2026 "avoid theme flash" advice): that pattern's payoff is server-rendering the correct HTML on first paint. This app gates all protected content behind a client MSAL spinner (`app-shell.tsx:54-63`), so there is no meaningful pre-hydration HTML to mismatch for the selector. Cookies add server-read plumbing for a benefit this shell doesn't realize. localStorage + mount-flag is simpler here.

**Gotcha (will bite the design):** **Hydration mismatch.** Do NOT read `localStorage` directly during render. The server (and the client's first render) must emit the _default_ ("All"); only after `useEffect`/mount may the persisted value appear. Concretely: initialise state to the default, and either (a) use `usehooks-ts.useLocalStorage` (handles this) or (b) gate the "real" value behind an `isHydrated` boolean. A naive `useState(() => localStorage.getItem(...))` throws on the server and/or produces a hydration error. (Sources below.)

**Sources:**

1. [How to Fix Next.js localStorage and Hydration Errors Cleanly — FluentReact](https://www.fluentreact.com/blog/nextjs-localstorage-hydration-errors-fix) — accessed 2026-06-08
2. [How to Fix Hydration Mismatch in Next.js 15 — DailyDevPost](https://dailydevpost.com/blog/how-to-fix-hydration-mismatch-in-next-js-15) — accessed 2026-06-08
3. [Fix Next.js hydration error with Zustand (localStorage) — Medium](https://medium.com/@koalamango/fix-next-js-hydration-error-with-zustand-state-management-0ce51a0176ad) — accessed 2026-06-08

**Design impact:** Put the selector in `Header` (or a thin wrapper around it), back it with a Context provider added inside `AppShell` (so it sits above the route outlet but below auth), persist with `usehooks-ts.useLocalStorage`, default "All". Account-scoped pages (`live-trading/page.tsx`, `dashboard/page.tsx`) read the context and client-filter their already-fetched `/live/status` deployments by `account_id`. Backtests/strategies pages simply do not consume the context (PRD non-goal: backtesting unaffected).
**Test implication:** E2E must verify the selection survives (a) client navigation to another page and back, and (b) a full `page.reload()` — the `usehooks-ts` write path is what makes reload pass. Add a hydration-mismatch guard check (no console hydration error on first load with a non-default persisted value).

---

### 2. shadcn/ui — searchable account picker (`Select` vs `Combobox`/`Command`)

**Versions:** shadcn CLI `3.8.5`; primitives are vendored locally and import from the **unified `radix-ui@1.4.3`** package (e.g. `ui/select.tsx:5` → `import { Select as SelectPrimitive } from "radix-ui"`). This is the current single-package convention (not per-component `@radix-ui/react-*`).

**What exists today:** `ui/select.tsx` and `ui/popover.tsx` are present. **`ui/command.tsx` and a `Combobox` are NOT present** (confirmed by glob of `components/ui/`). shadcn's `Combobox` is not a single primitive — it is a _recipe_ composed of `Popover` + `Command` (the latter wraps `cmdk`). Adding it means running `npx shadcn@latest add command` (and `popover`, already present), which pulls the `cmdk` dependency.

**Recommendation:** With LVP/HVP/FUND the registry is tiny (≤ ~5 accounts). A plain `Select` (already vendored, zero new deps) is sufficient and is the KISS choice. Reach for `Combobox` (`Command`/`cmdk`) only if the design explicitly wants type-to-filter; that adds a new component + the `cmdk` dependency and a fresh audit (rules/skill-audit not required for first-party shadcn, but `package.json` review applies). Default recommendation: **`Select`**, with `Combobox` as an explicit upgrade if product wants search.

**Gotcha:** shadcn now publishes both a Radix UI and a Base UI registry; pull the **Radix** variant to match the existing `radix-ui`-based primitives (`ui/select.tsx`). Mixing a Base-UI `Combobox` with Radix `Select` would fork the primitive layer.

**Sources:**

1. [shadcn/ui Select (Radix registry)](https://ui.shadcn.com/docs/components/radix/select) — accessed 2026-06-08
2. [shadcn/ui Combobox (Radix registry)](https://ui.shadcn.com/docs/components/radix/combobox) — accessed 2026-06-08

**Design impact:** Build the selector on the existing `Select` primitive. No new dependency unless search is mandated. The selector options come from the **union** (US-004): `listBrokerAccounts()` + accounts seen in `/live/status` deployments + "All" + "Unassigned" + "Unknown/retired account <id>" for deployment-only ids.
**Test implication:** Use `data-testid` / role selectors (existing convention — e.g. `portfolio-start-account-id`). The selector and each option need stable testids for the verify-e2e UI use case.

---

### 3. Pydantic V2 + FastAPI — the real-money confirmation gate → 422

**Versions:** Pydantic `>=2.10.0` (V2), FastAPI `>=0.133.0`.

**Established project pattern (read, not assumed):** `@model_validator(mode="after")` is already used for cross-field request validation — `LiveStartRequest`/`PortfolioStartRequest._require_account_selector` (`schemas/live.py:66-80`) and `BrokerAccountCreateRequest._account_prefix_matches_trading_mode` (`schemas/broker_account.py:57-71`). Raising `ValueError` inside these surfaces as a **422** (FastAPI wraps body-validation `ValueError` into a `RequestValidationError` → 422). Confirmed by docs + existing code.

**CRITICAL design subtlety:** The PRD's gate is `confirm_account_id == resolved ib_account_id` _when the target is real-money_. The **resolved `ib_account_id` is NOT in the request body** — it comes from resolving `broker_account_id`/`account_id` against the `broker_accounts` table (DB I/O, async). A Pydantic `@model_validator` runs at body-parse time with **no DB session**, so it **cannot** perform the identity match. Therefore:

- A `@model_validator` CAN enforce the _syntactic_ preconditions on the new `confirm_account_id` field: e.g. "if real-money intent is asserted, `confirm_account_id` must be present and non-blank; it must not be 'all'/'unassigned'." Those raise `ValueError` → 422 for free.
- The _semantic_ identity match (`confirm_account_id == account.ib_account_id`, and `account.is_real_money is True`) MUST run in the handler `live_start_portfolio` (`api/live.py:1149`) AFTER `_resolve_effective_account` (`api/live.py:1197`) returns the resolved `BrokerAccount`. There it must raise an explicit `HTTPException(status_code=422, detail={"error": {...}})` — matching the existing deploy-error envelope builder `_deploy_error` (`api/live.py:476-486`).
- **Place the check INSIDE `_resolve_effective_account` or immediately after it, BEFORE the idempotency reservation and BEFORE any publish** — mirroring how `validate_account_row_state` already fails closed at `api/live.py:676-680`. This keeps the gate on the effective (resolved) account, never the raw request string (the same halt-bypass concern documented at `api/live.py:454-469` / `1276-1279`).

**FastAPI status semantics confirmed:** body `ValueError` in a validator → **422 automatically**; a hand-raised `HTTPException(422,...)` → **422** with your custom body. Both satisfy the PRD's "→ 422". Use `HTTPException(422)` for the DB-dependent identity mismatch (consistent with `_deploy_error`); use a `ValueError` model_validator only for the body-only preconditions.

**Gotcha:** Do NOT try to force the identity match into the schema with a hidden dependency — it has no DB. And do NOT return 400; the project's `rules/api-design.md` reserves 400 for malformed _syntax_ and 422 for semantic validation. The PRD explicitly wants 422.

**Sources:**

1. [Pydantic HTTP Exceptions / 422 in FastAPI — Orchestra](https://www.getorchestra.io/guides/pydantic-http-exceptions-handling-validation-errors-in-fastapi) — accessed 2026-06-08
2. [FastAPI 422 Validation Error — Markaicode](https://markaicode.com/errors/fastapi-422-validation-error-fix/) — accessed 2026-06-08
3. Existing code: `backend/src/msai/schemas/live.py:66-80`, `backend/src/msai/api/live.py:476-520, 676-686` — read 2026-06-08

**Design impact:** Add `confirm_account_id: str | None` to `PortfolioStartRequest` (`schemas/live.py:21`). Add a body-only `@model_validator` for the "absent / 'all' / 'unassigned' / blank" rejections (422 via ValueError). Add the _identity match_ as an `HTTPException(422)` in the handler right after `_resolve_effective_account`, gated on `account.is_real_money`. The same handler is reused by UI/API/CLI, so the gate is enforced server-side for all three surfaces in one place (PRD US-003).
**Test implication:** API use case must cover all four reject branches (mismatch, absent, "all", "unassigned") → 422 with actionable body, AND the happy path (`confirm_account_id == ib_account_id`) → deploy proceeds. A test-account (non-real-money) deploy must NOT require the identity echo but still require an explicit single target.

---

### 4. Alembic + SQLAlchemy 2.0 — additive `is_real_money` / `account_class` column

**Versions:** Alembic `>=1.14.0`, SQLAlchemy `>=2.0.36`, Postgres 16.

**Established project pattern (decisive — read, not assumed):** `BrokerAccount` maps `StrEnum`s (`BrokerAccountStatus`, `CredentialsBackend`) onto **`String(N)` columns**, NOT native Postgres `ENUM` types (`models/broker_account.py:24-52`). The create migration uses **`sa.String(...)` with a string `server_default`** — e.g. `sa.Column("status", sa.String(32), nullable=False, server_default="active")` and `sa.Column("trading_mode", sa.String(16), nullable=False, server_default="paper")` (`alembic/versions/d87c2aa5f751_create_broker_accounts.py:34,36`). **No migration in `alembic/versions/` creates a native PG enum type.** The broker-account FK was added additively via `op.add_column` (`81e7efe6d772_...py:46-54`).

**Recommendation:** Follow the existing pattern exactly. Add the new attribute as a **`String`-backed `StrEnum`** (e.g. `AccountClass` = `test` / `real` / `paper`) OR a **`Boolean is_real_money`** — and write the column as **`nullable=False` + `server_default`** so existing rows backfill automatically and old (rolled-back) code ignores it. This is additive-only per `rules/database.md` (deploy rolls back image SHAs but not schema).

- If **boolean**: `sa.Column("is_real_money", sa.Boolean(), nullable=False, server_default=sa.false())` (or `sa.text("false")`).
- If **enum-as-string** (PRD Open Question 7.2 leans future-proof): `sa.Column("account_class", sa.String(16), nullable=False, server_default="test")`, mapped as `Mapped[AccountClass]` over `String(16)` exactly like `status`. **Do NOT introduce a native PG `ENUM`** — it breaks the project's uniform String-enum convention and triggers the autogenerate `CREATE TYPE` gotcha below.

**Backfill correctness:** a `server_default` alone marks every _existing_ row as the default (`test`/`false`). The real FUND/HVP/LVP rows must get their true class. Since the migration is additive and existing rows pre-date the column, add an explicit data backfill in the migration (or a follow-up step) — e.g. set `is_real_money=true`/`account_class='real'` where the account is the fund, derive from existing `trading_mode='live'` + non-DU/DF prefix as a _one-time migration heuristic only_ (NOT runtime inference — runtime must read the explicit column, per PRD §6 "never inferred unsafely"). The migration heuristic is acceptable because it runs once under operator control; the frontend/string-prefix inference is what the PRD forbids.

**Gotcha (why we avoid native enum):** Alembic autogenerate is **unreliable at emitting `CREATE TYPE` for new native Postgres enums** — it can reference the type in the column before creating it, yielding `type "..." does not exist` at upgrade. The fix requires manual `postgresql.ENUM(...).create()` or the `alembic-postgresql-enum` plugin. The project sidesteps this entirely by storing enums as `String`. Keep doing that. Also: `--autogenerate` will not invent the data backfill — write it by hand.

**Sources:**

1. [Alembic Auto Generating Migrations](https://alembic.sqlalchemy.org/en/latest/autogenerate.html) — accessed 2026-06-08
2. [Postgres enum not created by autogenerate — alembic#488](https://github.com/sqlalchemy/alembic/issues/488) — accessed 2026-06-08
3. Existing code: `backend/alembic/versions/d87c2aa5f751_create_broker_accounts.py:34-36`, `backend/src/msai/models/broker_account.py:24-52` — read 2026-06-08

**Design impact:** Add `is_real_money` (or `account_class`) as a `String`/`Boolean` `server_default` column on `broker_accounts`, surface it in `BrokerAccountResponse` (`schemas/broker_account.py:90`) and in the frontend `BrokerAccount` type (`frontend/src/lib/api/broker-accounts.ts:24`). Write a hand-authored data backfill in the migration for the known real-money account(s). No native PG enum.
**Test implication:** Migration test must assert existing rows get the default and the targeted backfill sets the fund to real-money. A unit test must assert the runtime real-money decision reads the column, never the `ib_account_id` prefix.

---

### 5. Existing code inventory (so design does not reinvent)

**Backend — what already exists:**

- **No `is_real_money` / `account_class` / `confirm_account_id` anywhere** (grep over `backend/src/msai` found zero matches). All net-new.
- Real-money is currently expressed only as `trading_mode == "live"` + the **string-prefix guard** `assert_account_mode_consistent` (`services/nautilus/ib_port_validator.py:38-82`; `IB_PAPER_PREFIXES = ("DU","DF")`, live = starts with `U`). PR4 must add an _explicit_ attribute and stop relying on prefix parsing for the real-money decision (PRD §6).
- `PortfolioStartRequest` (`schemas/live.py:21-97`) already supports **either** `broker_account_id` (registry selector) **or** `account_id`+`ib_login_key` (legacy pair), enforced by `_require_account_selector` (`schemas/live.py:66-80`). PR4 adds `confirm_account_id` here.
- `live_start_portfolio` handler (`api/live.py:1149`) already resolves the **effective account** via `_resolve_effective_account` (`api/live.py:523-686`) → returns `_EffectiveAccount(account_id, ib_login_key, broker_account)`. This is the exact seam to insert the real-money identity gate (after resolution, before idempotency reserve). The deploy-error envelope builder `_deploy_error`/`_deploy_validation_fail` (`api/live.py:476-520`) is the canonical 422 raiser to reuse.
- `/live/status` returns `LiveDeploymentInfo` with `account_id`, `ib_login_key`, `broker_account_id` (`schemas/live.py:106-161`) — the data the UI client-filter needs is already present.
- Broker-account CRUD router `api/broker_accounts.py` + `BrokerAccountResponse` (`schemas/broker_account.py:90-115`) — add the new field to the response (additive).

**CLI — what already exists:**

- `msai live start-portfolio` (`cli.py:932-1015`) already prompts a typed real-money confirm via the shared `_resolve_cli_account_payload` (`cli.py:159-230`): on `--no-paper` it runs `typer.confirm("This will start REAL-MONEY trading on {confirm_target}. Continue?", abort=True)` (`cli.py:211-227`). PR4 must add a `--confirm-account-id` option that is sent in the payload so the **server-side** gate is satisfied identically to UI/API (PRD US-003: "CLI surfaces the same confirmation requirement"). The existing client-side prefix guard at `cli.py:200-210` stays but is NOT the authority.

**Frontend — what already exists:**

- App shell + header (`components/layout/app-shell.tsx`, `components/layout/header.tsx`) — selector home.
- `PortfolioStartDialog` (`components/live/portfolio-start-dialog.tsx`) is a 4-stage deploy flow that **today takes a free-text `account_id` Input and parses `startsWith("DU"/"DF"/"U")` in the browser** (`portfolio-start-dialog.tsx:248-262`) and does a **purely client-side** typed-confirm (`confirmMatches`, `portfolio-start-dialog.tsx:369-374`). This is exactly the unsafe inference + client-only confirm the PRD replaces. The dialog must gain an **explicit target-account picker** (not free text), pre-filled from the global selector only when it is a single concrete account, with "All"/"Unassigned" disabling Deploy (US-002), and must send `confirm_account_id` so the server enforces the gate.
- `startPortfolio()` client (`frontend/src/lib/api.ts:967-989`) currently only accepts `{portfolio_revision_id, account_id, paper_trading, ib_login_key}` — **no `broker_account_id`, no `confirm_account_id`**. Must be extended additively.
- Broker-account client + type already exist at `frontend/src/lib/api/broker-accounts.ts` (`listBrokerAccounts`, `BrokerAccount` type) — add the new `is_real_money`/`account_class` field there.
- shadcn `Select` + `Popover` present; **`Command`/`Combobox` absent**.

---

## Not Researched (with justification)

- **MSAL / Azure Entra (`@azure/msal-*`)** — auth is unchanged by PR4; the selector lives behind the existing auth guard. The PRD explicitly does not touch auth/RBAC (§6). Not researched.
- **NautilusTrader / IB Gateway / live supervisor** — PR4 is a dashboard + deploy-contract feature; it does not change the trading node, reconciliation, or gateway wiring. Balances stay gateway-bound (US-005, a non-goal to change). Not researched.
- **TradingView Lightweight Charts / Recharts / Nivo** — charting libs untouched by an account selector. Not researched.
- **Databento / DuckDB / Parquet** — market-data path; backtesting is explicitly out of scope (PRD non-goal). Not researched.

---

## Open Risks

1. **`account_class` enum vs `is_real_money` boolean is still an open product question** (PRD §7 Q2). Research recommends **either** as a `String`/`Boolean` `server_default` column (NOT native PG enum). A boolean is the minimum that satisfies the gate; an enum (`test`/`real`/`paper`) is more future-proof for PR5. Design must pick one; both are additive-safe.
2. **Backfill of the real-money flag for existing rows.** `server_default` marks all existing rows as the default (non-real). The fund/HVP/LVP rows need an explicit one-time data backfill in the migration. Using `trading_mode='live'` + non-DU/DF prefix as a _migration-time_ heuristic is acceptable (one-time, operator-run); runtime must read the column, never the prefix.
3. **Do existing `account_id = null` legacy deployment rows actually exist on dev/prod?** PRD §7 Q3 + council "missing evidence". The "Unassigned" bucket only matters if such rows exist — verify at implementation by inspecting `/live/status` (sanctioned read), not by assuming. If none exist, "Unassigned" is a defensive empty group.
4. **Hydration-mismatch regression risk** is the single most likely frontend bug. The persisted-value-on-first-render trap (Area 1) must be handled via `usehooks-ts.useLocalStorage` or an `isHydrated` flag, and verified in E2E (reload with a non-default persisted account → no hydration console error, correct value shown).
5. **`startPortfolio` client + `PortfolioStartRequest` are widening their contract.** Adding `confirm_account_id` (and exposing `broker_account_id` to the UI) must stay backward-compatible: the field is optional and only _required_ on the real-money branch, so existing paper/test callers and warm-restart paths (`api/live.py:596-686`) are unaffected. Confirm no existing test asserts the absence of the field.

---

## Design implications for PR4 (concrete recommendations)

1. **Global state:** React Context provider mounted inside `AppShell` (above the route outlet), value persisted with the already-installed **`usehooks-ts.useLocalStorage`**, default `"all"`. Selector UI lives in `Header`. Account-scoped pages (`live-trading`, `dashboard`) client-filter their existing `/live/status` deployments by `account_id`; non-scoped pages ignore the context. Reason: meets navigation + reload persistence at the lowest complexity, no new dependency, no SSR-mismatch (shell is already client-gated).
2. **Selector widget:** existing shadcn **`Select`** (no new dep). Options = union of `listBrokerAccounts()` + `/live/status` account ids + "All" + "Unassigned" + "Unknown/retired account <id>". Add `Combobox`/`Command` only if product mandates search.
3. **Real-money attribute:** add an additive `String`/`Boolean` `server_default` column to `broker_accounts` (NOT native PG enum — matches `status`/`trading_mode` convention), surface in `BrokerAccountResponse` + the frontend `BrokerAccount` type, hand-write the fund backfill in the migration.
4. **Server-side confirm gate:** add `confirm_account_id` to `PortfolioStartRequest`; body-only preconditions (absent/"all"/"unassigned"/blank → 422) via a `@model_validator` raising `ValueError`; the DB-dependent identity match (`confirm_account_id == account.ib_account_id` when `account.is_real_money`) as an `HTTPException(422)` in `live_start_portfolio` right after `_resolve_effective_account`, reusing the `_deploy_error` envelope. One handler ⇒ enforced identically for UI/API/CLI.
5. **Deploy dialog:** replace the free-text `account_id` Input + browser prefix-parse in `PortfolioStartDialog` with an explicit target-account picker (pre-filled from the global selector only when it is a single concrete account; "All"/"Unassigned" disable Deploy), send `confirm_account_id` (and `broker_account_id`) so the server is the authority; keep the existing UI confirm as UX only.
6. **CLI:** add `--confirm-account-id` to `msai live start-portfolio`, include it in the payload, keep the existing `typer.confirm` as UX. Surface coverage: UI + API + CLI all required (PRD US-003; `surfaces: [API, CLI, UI]`).
7. **Balances:** leave `/account/summary` + `/account/portfolio` gateway-bound; the selector must NOT fabricate per-account balances. Label cards "connected account only — <id>" (US-005).
