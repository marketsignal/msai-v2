# UI Surface Audit — MSAI v2

**Date:** 2026-05-16
**Branch:** `feat/ui-completeness`
**Auditors:** Claude (Pass 1, this file) + Codex (Pass 2, parallel, `docs/audits/2026-05-16-ui-surface-audit-codex.md` if persisted)
**Mode:** Trust-First (hedge fund, real money, irreversible actions) + Product UI (CRUD/workflow density)
**Scope:** Exhaustive — every backend API endpoint mapped to every UI consumer; gaps classified.

---

## 1. Method

1. Enumerated every `@router.{get,post,patch,delete,put}` decorator in `backend/src/msai/api/*.py`.
2. Enumerated every `frontend/src/app/**/page.tsx` file with line counts and current API consumption (grep for `/api/v1/` + typed-client function names from `frontend/src/lib/api.ts`).
3. Mapped route → consumer pages.
4. Classified gaps into five buckets:
   - **MISSING_UI** — API exists, no UI page or component consumes it.
   - **FAKE_UI** — UI hardcodes data that should come from API, OR calls endpoints that don't exist.
   - **BROKEN_UI** — UI references API contract that no longer matches reality.
   - **INCOMPLETE_UI** — UI reads data but offers no actions (CTAs missing).
   - **DEAD_NAV** — Buttons/links leading to 410, 404, or empty surfaces.

---

## 2. Stack Snapshot

| Layer              | Detail                                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------- |
| API base           | `/api/v1/*` declared per router in `backend/src/msai/api/*.py`                                            |
| API client         | `frontend/src/lib/api.ts` — typed fetch helpers (`apiGet`, `apiPost`, `apiFetch`) + Pydantic-mirror types |
| Router count       | **15 routers** registered in `backend/src/msai/main.py:270-284`                                           |
| Frontend framework | Next.js 15 + shadcn/ui + Tailwind                                                                         |
| Auth               | Azure Entra ID (MSAL frontend, PyJWT backend); `X-API-Key` fallback for dev                               |
| State              | TanStack Query (visible in `useInventoryQuery`, `useJobStatusQuery`); local `useState` elsewhere          |

---

## 3. Full API Inventory (Verified from `@router` Decorators)

### 3.1 Auth (`/api/v1/auth/`)

| Method | Path      | Source       | Consumer                                                                         |
| ------ | --------- | ------------ | -------------------------------------------------------------------------------- |
| GET    | `/me`     | `auth.py:34` | `useAuth` context (header), `/settings` (line 30) — but `user.role` not surfaced |
| POST   | `/logout` | `auth.py:73` | `useAuth`                                                                        |

### 3.2 Strategies (`/api/v1/strategies/`)

| Method | Path             | Source              | Consumer                                                                          |
| ------ | ---------------- | ------------------- | --------------------------------------------------------------------------------- |
| GET    | `/` (list)       | `strategies.py:39`  | dashboard, strategies, backtests, research, graduation, live-portfolio (all read) |
| GET    | `/{id}`          | `strategies.py:75`  | `/strategies/[id]/page.tsx:59`                                                    |
| PATCH  | `/{id}`          | `strategies.py:119` | **NONE** — no UI consumer                                                         |
| POST   | `/{id}/validate` | `strategies.py:158` | `/strategies/[id]/page.tsx` (validate dialog)                                     |
| DELETE | `/{id}`          | `strategies.py:197` | **NONE** — no UI consumer. **Backend TODO at line 197 says soft-delete needed.**  |

### 3.3 Strategy Templates (`/api/v1/strategy-templates/`)

| Method | Path        | Source                     | Consumer                                                                      |
| ------ | ----------- | -------------------------- | ----------------------------------------------------------------------------- |
| GET    | `/`         | `strategy_templates.py:34` | **NONE**                                                                      |
| POST   | `/scaffold` | `strategy_templates.py:43` | **NONE** — also contradicts `CLAUDE.md` "no UI uploads in Phase 1 — git-only" |

### 3.4 Backtests (`/api/v1/backtests/`)

| Method | Path                 | Source             | Consumer                                           |
| ------ | -------------------- | ------------------ | -------------------------------------------------- |
| POST   | `/` (run)            | `backtests.py:240` | `/backtests/page.tsx` run dialog                   |
| GET    | `/history`           | `backtests.py:423` | `/backtests/page.tsx`                              |
| GET    | `/{id}/status`       | `backtests.py:487` | `/backtests/[id]/page.tsx:71`                      |
| GET    | `/{id}/results`      | `backtests.py:520` | `/backtests/[id]/page.tsx:107`                     |
| GET    | `/{id}/trades`       | `backtests.py:591` | `/backtests/[id]/page.tsx` via `getBacktestTrades` |
| GET    | `/{id}/report`       | `backtests.py:717` | `/backtests/[id]/page.tsx` iframe                  |
| POST   | `/{id}/report-token` | `backtests.py:667` | `getBacktestReportToken`                           |

✅ **Coverage: 7/7 endpoints consumed.**

### 3.5 Alerts (`/api/v1/alerts/`)

| Method | Path | Source         | Consumer                         |
| ------ | ---- | -------------- | -------------------------------- |
| GET    | `/`  | `alerts.py:32` | **NONE** — **MISSING_UI gap #1** |

### 3.6 Live (`/api/v1/live/`)

| Method | Path                      | Source         | Consumer                                                         |
| ------ | ------------------------- | -------------- | ---------------------------------------------------------------- |
| POST   | `/start` (DEPRECATED 410) | `live.py:229`  | None — correctly retired                                         |
| POST   | `/start-portfolio`        | `live.py:672`  | `/live-trading/portfolio/page.tsx`                               |
| POST   | `/stop`                   | `live.py:1366` | `/live-trading/page.tsx`                                         |
| POST   | `/kill-all`               | `live.py:1572` | `/live-trading/page.tsx`                                         |
| POST   | `/resume`                 | `live.py:1804` | `/live-trading/page.tsx`                                         |
| GET    | `/status`                 | `live.py:1861` | dashboard, live-trading                                          |
| GET    | `/status/{id}`            | `live.py:1956` | `useLiveStream` reconnect hydration                              |
| GET    | `/positions`              | `live.py:2017` | live-trading                                                     |
| GET    | `/trades`                 | `live.py:2087` | live-trading                                                     |
| GET    | `/audits/{deployment_id}` | `live.py:2145` | **NONE** — **MISSING_UI: per-deployment audit log not surfaced** |
| WS     | `/stream/{id}`            | `websocket.py` | `useLiveStream`                                                  |

### 3.7 Account (`/api/v1/account/`)

| Method | Path         | Source           | Consumer                                            |
| ------ | ------------ | ---------------- | --------------------------------------------------- |
| GET    | `/summary`   | `account.py:90`  | `dashboard/page.tsx:35` (1 card; no dedicated page) |
| GET    | `/portfolio` | `account.py:98`  | **NONE** — **MISSING_UI gap #5**                    |
| GET    | `/health`    | `account.py:106` | **NONE** — **MISSING_UI gap #5**                    |

**Backend safety hazard (council finding):** `services/ib_account.py:62-89` opens a fresh `IB.connectAsync` per request with no caching. `itertools.count(start=900)` for `client_id` is unbounded. **Must fix before adding aggressive polling in UI.**

### 3.8 Market Data (`/api/v1/market-data/`)

| Method | Path             | Source               | Consumer                                                                                     |
| ------ | ---------------- | -------------------- | -------------------------------------------------------------------------------------------- |
| GET    | `/bars/{symbol}` | `market_data.py:45`  | `/market-data/chart/page.tsx:109`                                                            |
| GET    | `/symbols`       | `market_data.py:77`  | `/market-data/chart/page.tsx:63`                                                             |
| GET    | `/status`        | `market_data.py:87`  | **NONE** — **MISSING_UI: storage stats card**                                                |
| POST   | `/ingest`        | `market_data.py:100` | **NONE** — only consumed by symbol-onboarding hooks. **Direct ingest UI is gap #4 partial.** |

**Note:** `/market-data/page.tsx` (240 lines) is the **inventory** page (universe management), not the bars browser. The bars browser at `/market-data/chart` exists. The "market-data browser" gap (#4) is really about: a status panel + direct ingest UI + symbol→chart navigation.

### 3.9 Backtest Portfolios (`/api/v1/portfolios/`)

| Method | Path                    | Source             | Consumer                     |
| ------ | ----------------------- | ------------------ | ---------------------------- |
| GET    | `""` (list)             | `portfolio.py:47`  | `/portfolio/page.tsx`        |
| POST   | `""` (create)           | `portfolio.py:68`  | `/portfolio/page.tsx:190`    |
| GET    | `/runs`                 | `portfolio.py:100` | `/portfolio/page.tsx`        |
| GET    | `/runs/{run_id}`        | `portfolio.py:122` | `/portfolio/page.tsx`        |
| GET    | `/runs/{run_id}/report` | `portfolio.py:144` | `/portfolio/page.tsx` iframe |
| GET    | `/{portfolio_id}`       | `portfolio.py:192` | `/portfolio/page.tsx`        |
| POST   | `/{portfolio_id}/runs`  | `portfolio.py:214` | `/portfolio/page.tsx:445`    |

✅ **Coverage: 7/7 endpoints consumed.**

### 3.10 Live Portfolios (`/api/v1/live-portfolios/`)

Consumed by `/live-trading/portfolio/page.tsx` (375 lines). All POST/GET endpoints for create + add-member + freeze + start flow. ✅ Mature surface.

### 3.11 Research (`/api/v1/research/`)

| Method | Path                | Source            | Consumer                                    |
| ------ | ------------------- | ----------------- | ------------------------------------------- |
| POST   | `/sweeps`           | `research.py:52`  | `/research/page.tsx` (launch dialog)        |
| POST   | `/` (alt POST)      | `research.py:94`  | `/research/page.tsx`                        |
| GET    | `/jobs`             | `research.py:144` | `/research/page.tsx`                        |
| GET    | `/jobs/{id}`        | `research.py:172` | `/research/[id]/page.tsx`                   |
| POST   | `/jobs/{id}/cancel` | `research.py:206` | **NONE** — **INCOMPLETE_UI: no cancel CTA** |
| POST   | `/promotions`       | `research.py:246` | `/research/[id]/page.tsx:131`               |

### 3.12 Graduation (`/api/v1/graduation/`)

| Method | Path                           | Source              | Consumer                   |
| ------ | ------------------------------ | ------------------- | -------------------------- |
| GET    | `/candidates`                  | `graduation.py:45`  | `/graduation/page.tsx`     |
| POST   | `/candidates`                  | `graduation.py:72`  | `/graduation/page.tsx`     |
| GET    | `/candidates/{id}`             | `graduation.py:110` | (covered via list)         |
| POST   | `/candidates/{id}/...`         | `graduation.py:132` | `/graduation/page.tsx:440` |
| GET    | `/candidates/{id}/transitions` | `graduation.py:184` | `/graduation/page.tsx:419` |

✅ Coverage OK.

### 3.13 Symbol Onboarding (`/api/v1/symbols/`)

| Method | Path                       | Source                     | Consumer                                             |
| ------ | -------------------------- | -------------------------- | ---------------------------------------------------- |
| POST   | `/onboard/dry-run`         | `symbol_onboarding.py:118` | `/market-data/page.tsx` add flow                     |
| POST   | `/onboard`                 | `symbol_onboarding.py:339` | `/market-data/page.tsx` add flow                     |
| GET    | `/onboard/{run_id}/status` | `symbol_onboarding.py:453` | `useJobStatusQuery`                                  |
| POST   | `/onboard/...` (extra)     | `symbol_onboarding.py:500` | `/market-data/page.tsx` (refresh-symbol)             |
| GET    | `/readiness`               | `symbol_onboarding.py:624` | **NONE** — **MISSING_UI: pre-trade readiness check** |
| GET    | `/inventory`               | `symbol_onboarding.py:714` | `useInventoryQuery`                                  |
| DELETE | `/{symbol}`                | `symbol_onboarding.py:807` | `useRemoveSymbol`                                    |

### 3.14 Instruments (`/api/v1/instruments/`)

| Method | Path    | Source              | Consumer                                                          |
| ------ | ------- | ------------------- | ----------------------------------------------------------------- |
| POST   | refresh | `instruments.py:40` | **NONE** in UI; intentional CLI-only (`msai instruments refresh`) |

---

## 4. UI Route Inventory

| Route                     | File                                  | Lines | Status       | Notes                                             |
| ------------------------- | ------------------------------------- | ----- | ------------ | ------------------------------------------------- |
| `/`                       | `app/page.tsx`                        | 5     | shell        | Likely redirect to `/dashboard`                   |
| `/login`                  | `app/login/page.tsx`                  | 66    | mature       | MSAL flow                                         |
| `/dashboard`              | `app/dashboard/page.tsx`              | 103   | **partial**  | 3 KPI cards + deployments list. Minimal density.  |
| `/strategies`             | `app/strategies/page.tsx`             | 87    | **skeletal** | List only; **no edit/delete CTAs**, sparse design |
| `/strategies/[id]`        | `app/strategies/[id]/page.tsx`        | 291   | partial      | Read + validate; **no edit or delete actions**    |
| `/backtests`              | `app/backtests/page.tsx`              | 234   | mature       | List + run dialog                                 |
| `/backtests/[id]`         | `app/backtests/[id]/page.tsx`         | 342   | mature       | Charts, trades, report iframe                     |
| `/portfolio`              | `app/portfolio/page.tsx`              | 774   | mature       | Backtest portfolios (compose, runs)               |
| `/live-trading`           | `app/live-trading/page.tsx`           | 270   | mature       | Deployments + positions/trades + kill-all/resume  |
| `/live-trading/portfolio` | `app/live-trading/portfolio/page.tsx` | 375   | mature       | Live portfolio compose → start flow               |
| `/market-data`            | `app/market-data/page.tsx`            | 240   | mature       | Inventory + onboard + delete                      |
| `/market-data/chart`      | `app/market-data/chart/page.tsx`      | 300   | mature       | Bars browser/chart                                |
| `/research`               | `app/research/page.tsx`               | 263   | mature       | Job list (no cancel CTA)                          |
| `/research/[id]`          | `app/research/[id]/page.tsx`          | 369   | mature       | Detail + promote                                  |
| `/graduation`             | `app/graduation/page.tsx`             | 600   | mature       | Candidates + transitions                          |
| `/settings`               | `app/settings/page.tsx`               | 313   | **broken**   | Multiple fake/broken elements (see §5.2)          |

**Total page surface: 16 routes** (1 shell, 1 broken, 4 partial/skeletal, 10 mature).

---

## 5. Gap Findings

### 5.1 MISSING_UI

| #   | Description                                                                | API                                      | Severity | Build cost                                                |
| --- | -------------------------------------------------------------------------- | ---------------------------------------- | -------- | --------------------------------------------------------- |
| M-1 | Alerts list page                                                           | `GET /api/v1/alerts/`                    | P1       | S — single page, paginated table                          |
| M-2 | Account portfolio page (broker positions outside the live-deployment lens) | `GET /api/v1/account/portfolio`          | P1       | M — table + connection state, gated on backend safety fix |
| M-3 | Account health card or dedicated page                                      | `GET /api/v1/account/health`             | P1       | S — read cached probe, no new IB load                     |
| M-4 | Live audit log per deployment                                              | `GET /api/v1/live/audits/{id}`           | P2       | S — drawer on live-trading row                            |
| M-5 | Research job cancel CTA                                                    | `POST /api/v1/research/jobs/{id}/cancel` | P2       | S — button on `/research/[id]`                            |
| M-6 | Market-data storage status card                                            | `GET /api/v1/market-data/status`         | P2       | S — dashboard card or `/market-data` panel                |
| M-7 | Symbol readiness UI before live trade                                      | `GET /api/v1/symbols/readiness`          | P2       | M — pre-trade gate or inline check                        |

### 5.2 FAKE_UI / BROKEN_UI (`/settings` is the canonical case)

| #   | Description                                                                                                   | File:Line                                      | Severity                |
| --- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | ----------------------- |
| F-1 | `/settings` calls `/api/v1/admin/clear-data` — **endpoint does not exist** (not in any router)                | `settings/page.tsx:40`                         | **P0** user-visible lie |
| F-2 | `/settings` hardcoded `"Admin"` role badge regardless of actual `user.role`                                   | `settings/page.tsx:103` (per advisor evidence) | **P0** user-visible lie |
| F-3 | `/settings` "Save Preferences" button does nothing — fake save flow                                           | `settings/page.tsx:178` (per advisor evidence) | **P0** user-visible lie |
| F-4 | `/settings` does NOT consume `/api/v1/auth/me` — uses cached `user` only; doesn't surface real profile fields | `settings/page.tsx:30`                         | P1 — wiring gap         |

### 5.3 INCOMPLETE_UI

| #   | Description                                                                                                                                  | API                                                         | Severity              | Build cost                             |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | --------------------- | -------------------------------------- |
| I-1 | Strategy edit (config + description) — UI shows config text but cannot PATCH                                                                 | `PATCH /api/v1/strategies/{id}`                             | P1                    | S-M (form + validation)                |
| I-2 | Strategy delete — **backend hard-deletes per TODO**. Either ship UI with explicit "unregister" semantics OR fix backend to soft-delete first | `DELETE /api/v1/strategies/{id}` + `strategies.py:197` TODO | **P0 backend, P1 UI** | M (backend + UI together)              |
| I-3 | `/strategies` list — no "create" / "register from disk" CTA; user reads list but cannot act                                                  | none yet                                                    | P2                    | S (placeholder + scaffolder follow-up) |
| I-4 | `/research` job list — has launch dialog but no cancel-running button                                                                        | `POST /api/v1/research/jobs/{id}/cancel`                    | P2                    | S                                      |

### 5.4 DEAD_NAV / Risk surfaces

| #   | Description                                                                                               | Severity |
| --- | --------------------------------------------------------------------------------------------------------- | -------- |
| D-1 | `/settings` Danger Zone button hits 404 (clear-data endpoint missing). Same as F-1 from a UX perspective. | P0       |
| D-2 | No global 404/500/401/403 styled pages (verify in implementation phase)                                   | P2       |
| D-3 | No empty-state polish across `/strategies`, dashboards (verify per page)                                  | P2       |

### 5.5 NEWLY-DISCOVERED GAPS (beyond PR #67's known six)

These were NOT in PR #67's OUT OF SCOPE list. The audit surfaced them:

| #   | Description                                                                                                  | Severity | Notes                                                           |
| --- | ------------------------------------------------------------------------------------------------------------ | -------- | --------------------------------------------------------------- |
| N-1 | Live audit log surface (`GET /live/audits/{id}`)                                                             | P2       | Per-deployment audit trail for compliance + debugging           |
| N-2 | Research job cancel                                                                                          | P2       | Long-running sweeps can't be killed from UI                     |
| N-3 | Market-data storage status card                                                                              | P2       | Storage stats currently visible only via CLI `msai data-status` |
| N-4 | Symbol readiness check pre-trade                                                                             | P2       | `GET /symbols/readiness` exists; never surfaced in UI           |
| N-5 | `/settings` "Save Preferences" → also disabled (no preferences persist anywhere; deletion is the right move) | P0       | Subset of F-3                                                   |
| N-6 | Auth `user.role` plumbing to settings + dashboard (real role from JWT, not hardcoded Admin)                  | P0       | Tied to F-2                                                     |
| N-7 | Strategies-page "create" CTA is missing (registration story is git-only-but-zero-affordance)                 | P2       | Tied to the templates scaffolder controversy — see §6           |

---

## 6. Phase 1 Policy Contradictions

`CLAUDE.md` line 91 and 286 (per Contrarian audit) declares strategies are **git-only in Phase 1 — no UI uploads**.

But the codebase ships:

- `backend/src/msai/api/strategy_templates.py` — exposes `GET /` (list) and `POST /scaffold` (write file)
- `backend/src/msai/services/strategy_templates.py:140-169` — actually writes Python files into `STRATEGIES_ROOT`

**Audit verdict:** This is a silent architecture contradiction. The scaffolder service exists but no UI consumer surfaces it (which is why it hasn't bitten yet). Two options before any UI work touches templates:

- **Option A — Reverse architecture:** Write a decision doc amending Phase 1 ("git-only" → "UI scaffolder allowed for templates, registration still required via git commit"). Then build the UI.
- **Option B — Cut the backend feature:** Remove `strategy_templates.py` from API + service. Keep the templates concept as `/strategies` form pre-fills only, no file writes from UI.

**Council recommendation:** Block all UI work on this until policy is amended OR the backend feature is cut. Implementer MUST not silently reverse architecture by shipping UI that consumes the contradictory endpoint.

---

## 7. Backend Safety Findings (Council-Flagged + Audit-Verified)

| Finding                                                               | File:Line                                | Action                                                                                                                                                |
| --------------------------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Per-request IB reconnect on `/account/summary` + `/account/portfolio` | `services/ib_account.py:62-89`           | Add 15-30s TTL cache OR serve from `_ib_probe` cached state OR gate behind explicit Refresh button. **REQUIRED before adding aggressive UI polling.** |
| Unbounded `itertools.count(start=900)` for `client_id`                | `services/ib_account.py:29`              | Cap modulo or single shared client_id with serialized access                                                                                          |
| Strategy DELETE is hard-delete despite TODO                           | `api/strategies.py:197`                  | Decide: soft-delete (preserve backtest refs) vs explicit "unregister + cascade backtest cleanup". **Backend fix must land before UI delete CTA.**     |
| TOCTOU race on template scaffold                                      | `services/strategy_templates.py:140-169` | If template feature stays, replace `exists()` + `write_text()` with `O_EXCL` open. If feature is cut, no fix needed.                                  |

---

## 8. Playwright Framework Status

- `frontend/playwright.config.ts` ✅ exists (audited above)
- `frontend/tests/e2e/fixtures/auth.ts` ✅ exists (scaffold)
- `frontend/tests/e2e/specs/` — directory existence unconfirmed; Codex's parallel pass said it does not exist. **Treat as "must create" for this PR.**
- CI integration — no `.github/workflows/e2e.yml` referenced; template at `docs/ci-templates/e2e.yml` per CLAUDE.md (would need activation).

This PR should graduate Playwright specs for every shipped UI per the universal verification constraint from the council verdict.

---

## 9. Visual Quality Risk Map

For Trust-First + Product UI rigor (per `frontend-design.md` + `/ui-design` skill):

| Page                         | Concern                                                                                   | Action                                                                                  |
| ---------------------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `/strategies` (87 LOC)       | Looks like a wireframe. No empty state, no loading skeleton, no create CTA.               | Redesign as a real strategies index page with shadcn `Card` + skeleton loaders.         |
| `/strategies/[id]` (291 LOC) | Read-only feel; validate dialog is the only action.                                       | Add edit form + delete confirm dialog (gated on backend soft-delete).                   |
| `/dashboard` (103 LOC)       | Sparse 3-card grid. No comparison periods, no recent activity feed, no quick-action CTAs. | Bolt-on Alerts feed card, live-deployments status card, recent backtests card.          |
| `/settings` (313 LOC)        | **Multiple lies** + design predates real shadcn primitives elsewhere.                     | Strip fake content, rebuild as 2-section page (Profile + Alerts) wired to real APIs.    |
| Global 404/500/401/403       | Not styled per audit; default Next.js stubs likely.                                       | Add `not-found.tsx` + `error.tsx` at app root.                                          |
| Header / nav                 | No mention of a global notifications bell, command palette, theme switcher                | Trust-first means: avoid clutter, but Alerts surface should be reachable from anywhere. |

---

## 10. Prioritized All-in-One PR Scope

Per user directive (all gaps in one PR), this is the merged scope. The council's binding **technical** constraints now move IN-SCOPE (not deferred).

### P0 — User-visible lies / broken (MUST fix)

| ID       | Work                                                                                                                                                         |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **P0-1** | Strip `/settings` lies: remove `/api/v1/admin/clear-data` Danger Zone, remove fake notification save, wire real role from `/auth/me` (no hardcoded "Admin"). |
| **P0-2** | Add `user.role` plumbing from JWT/auth/me to UI components that need it.                                                                                     |

### P1 — Core missing functionality

| ID               | Work                                                                                                                                                 |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P1-1**         | New page `/alerts` consuming `GET /api/v1/alerts/`. Trust-first table with severity icons + timestamps + plain-language status.                      |
| **P1-2**         | New page `/account` (broker portfolio + health). Conditional on **P1-1-backend**: ib_account caching fix first.                                      |
| **P1-1-backend** | Backend fix: cache `/account/summary` + `/account/portfolio` with 15-30s TTL (serve from `_ib_probe` state where possible). Cap `client_id` counter. |
| **P1-3**         | Strategy EDIT form on `/strategies/[id]` (PATCH name/description/config, not file).                                                                  |
| **P1-4-backend** | Strategy DELETE backend fix: switch to soft-delete (set `deleted_at`), backtest history continues to resolve by `strategy_id`.                       |
| **P1-4-ui**      | Strategy DELETE confirm dialog on `/strategies/[id]` (gated on P1-4-backend).                                                                        |

### P1 — Phase 1 policy decision (NEEDED FIRST)

| ID            | Work                                                                                                                                                                          |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **P1-POLICY** | Decision doc: either amend Phase 1 to allow templates scaffolder OR cut the backend strategy_templates feature. **No UI scaffolder ships in this PR** until this is resolved. |

### P2 — Polish + observability parity

| ID       | Work                                                                                                   |
| -------- | ------------------------------------------------------------------------------------------------------ |
| **P2-1** | Live audit-log drawer on `/live-trading` row (`GET /live/audits/{id}`).                                |
| **P2-2** | Research job cancel CTA on `/research/[id]` (`POST /jobs/{id}/cancel`).                                |
| **P2-3** | Market-data storage status card on `/dashboard` or `/market-data` (`GET /market-data/status`).         |
| **P2-4** | Symbol readiness widget — pre-trade check using `GET /symbols/readiness`.                              |
| **P2-5** | `/strategies` redesign — proper Card grid, skeleton, empty state, link to "how to add a strategy" doc. |
| **P2-6** | `/dashboard` densification — Alerts feed card, recent activity, quick-action CTAs.                     |
| **P2-7** | Global error pages: `not-found.tsx`, `error.tsx`.                                                      |
| **P2-8** | Header notification badge for unread alerts (drives traffic to `/alerts`).                             |

### P2 — Verification scaffolding

| ID        | Work                                                                                                            |
| --------- | --------------------------------------------------------------------------------------------------------------- |
| **P2-V1** | Author Playwright specs in `frontend/tests/e2e/specs/` for every shipped UI surface (per universal constraint). |
| **P2-V2** | Activate `docs/ci-templates/e2e.yml` (or document why not).                                                     |

### Out of THIS PR (deferred with explicit reason)

| ID          | Work                                                         | Why deferred                                                                                                                                  |
| ----------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **DEFER-1** | Strategy templates scaffolder UI                             | Requires Phase 1 policy amendment (P1-POLICY) BEFORE building.                                                                                |
| **DEFER-2** | Market-data bars browser (extension of `/market-data/chart`) | The bars chart already exists at `/market-data/chart`; gap #4 from PR #67's list is effectively closed except for status panel polish (P2-3). |

---

## 11. Open Questions for Implementer (Surface Before Phase 3.2 Plan)

1. **Strategy DELETE semantics:** Soft-delete column on `strategies` table (`deleted_at`)? Or `is_archived` flag? Backtests query needs to still resolve archived strategies for historical results.
2. **Templates scaffolder fate:** Amend Phase 1 policy or cut backend service? (Affects `services/strategy_templates.py` + `api/strategy_templates.py`.)
3. **Account page polling cadence:** With cached IB probe, what UI refresh interval is appropriate? 30s aligns with `_PROBE_INTERVAL_S`. Manual-refresh only?
4. **Alerts dismiss/acknowledge:** Backend has only `GET /` (no PATCH/DELETE). Do we need acknowledge semantics in this PR or read-only?
5. **Header notifications badge:** Show unread count? Polling every 30s? Or rely on WebSocket existing live stream?
6. **Visual palette tokens:** Existing shadcn dark theme via OKLCH (per CLAUDE.md). Confirm no new palette needed; reuse existing.
7. **Empty-state copy authority:** Who writes the helpful empty-state copy for `/alerts` ("All quiet — no recent alerts"), `/strategies` (without scaffolder), etc.? Default = the implementer drafts; user can revise.

---

## 12. Summary

- **15 routers / ~60 endpoints** scanned. **16 UI routes** scanned.
- **PR #67's 6 known gaps** validated; **7 new gaps surfaced** (N-1 through N-7).
- **4 backend safety findings** must be fixed in this PR (ib_account caching, strategy soft-delete, optional TOCTOU on template scaffolder).
- **Phase 1 policy contradiction** on strategy templates must be resolved before any templates UI.
- **Playwright spec graduation** is mandatory per the council verdict.

**Recommendation:** Phase 1 PRD takes this audit and builds 5-6 thin per-concern PRDs (alerts, settings, strategies-edit, account+backend, observability-polish, verification-scaffolding) inside a single branch with focused commits. One PR, many commits, one decision doc per concern area in `docs/decisions/`.

---

## 13. Findings Update — Deep Read Pass (Post-Codex)

Codex's parallel pass stalled on the long audit prompt (memory rule `feedback_codex_cli_stalls_on_long_audit_prompts`). The Codex stream was mined for surfaced facts and pointers, but I did the actual file-reading. Several **additional lies + dead surfaces** surfaced from reading the page files end-to-end:

### 13.1 New FAKE_UI findings

| #    | Description                                                                                                                                                                                                                                                                                                                                      | File:Line                                                                  | Severity                  |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------- |
| F-5  | `/settings` "System Information" card: **EVERY value hardcoded** — version `"v0.1.0"`, environment `"development"`, uptime `"5d 14h 32m"`, disk `"17.75 GB / 100 GB"`, API "Healthy" (green dot), DB "Connected" (green dot). None is fetched from any API. Backend has `/account/health` (cached probe) but the settings page does not call it. | `settings/page.tsx:193-231`                                                | **P0** systemic UI lie    |
| F-6  | `/strategies/[id]` "Validate" button runs `JSON.parse(configText)` client-side ONLY (`handleValidate` at `:91-99`); never calls `POST /api/v1/strategies/{id}/validate`. The backend endpoint exists and does a real strategy-load validation — but is unreached from UI. The button does the wrong thing under the same label.                  | `strategies/[id]/page.tsx:91-99, 232-262`                                  | P1 — button is misleading |
| F-7  | `/strategies/[id]` Textarea config editor (`:225-230`) lets the user TYPE config changes, but there is **no Save button anywhere on the page**. Edits vanish on reload — silent data loss.                                                                                                                                                       | `strategies/[id]/page.tsx:225`                                             | P1 — silent data loss     |
| F-8  | `/settings` does NOT consume `/api/v1/auth/me` — uses MSAL `account` directly (name + email only); doesn't surface DB-side `role` / `display_name`. `auth.ts:7-10` `AuthUser` type lacks a `role` field, so the settings page literally cannot show the real role today.                                                                         | `settings/page.tsx:30`, `auth.ts:7-10`                                     | P1 — wiring gap           |
| F-9  | `PortfolioSummary` "Total Return" StatCard is permanently `"--"` regardless of data state. `value={hasAccount ? "--" : "--"}` — both branches return the same string. Dead card.                                                                                                                                                                 | `components/dashboard/portfolio-summary.tsx:95-100`                        | P1 — dead UI              |
| F-10 | `PortfolioSummary` "Total Value" and "Total Return" StatCards have `trend="up"` hardcoded — green up-arrow regardless of actual direction. Visual lie when portfolio is down.                                                                                                                                                                    | `components/dashboard/portfolio-summary.tsx:84,98`                         | P1 — visual lie           |
| F-11 | Dashboard `<EquityChart data={[]} />` — **the biggest visual element on the dashboard is permanently empty**. Component renders "No equity data available yet." forever. No API endpoint exists to populate it (no live equity timeseries; backtest has one). Either build the endpoint OR remove the card.                                      | `app/dashboard/page.tsx:95`, `components/dashboard/equity-chart.tsx`       | **P0** dead headline UI   |
| F-12 | `/strategies` index — every `StrategyCard` shows `--` for Sharpe / Return / Win Rate and "stopped" status because parent `strategies/page.tsx:76-80` only passes `{id, name, description}` — never wires status from `/live/status` or metrics from anywhere. Component supports real values; UI just doesn't pass them.                         | `app/strategies/page.tsx:73-83`, `components/strategies/strategy-card.tsx` | P1 — dead metrics         |

### 13.2 Settings page rewrite checklist (P0 surface)

The current `settings/page.tsx` ships **8 distinct lies** — making it the highest-priority UI fix:

- Hardcoded "Admin" badge (F-2)
- Three fake notification preference toggles (F-4)
- Fake "Save Preferences" button (F-3)
- Hardcoded version, environment, uptime, disk, API status, DB status — all six System Info rows (F-5)
- Nonexistent `/api/v1/admin/clear-data` button (F-1)

**The Trust-First answer is to strip the lies, keep only what we can defend with real data:**

- Profile card: `/auth/me` real `name`, `email`, `role`, `display_name`. No "Demo User" fallback.
- System status card: real `/account/health` if relevant; otherwise drop entirely (don't fake uptime).
- Drop notification preferences entirely until a backend persists preferences. Don't show toggles that don't save.
- Drop "Clear All Data" entirely. There is no admin endpoint; trying to ship one before PostgreSQL + Parquet wipe semantics are designed is a hostile feature anyway.

After deletions, settings becomes a **small honest page**: profile + maybe an "Alerts email" input that POSTs to a new backend preference endpoint IF we ship one. Or just profile.

### 13.3 Dashboard rewrite checklist

The dashboard is the front door of the app. With F-9 / F-10 / F-11 / F-12 + the missing Alerts feed + the empty Total Return card, the entire surface needs honest revision:

- **EquityChart (F-11):** Either ship a backend `/api/v1/live/equity-curve` endpoint that aggregates filled-trade PnL into a daily timeseries, OR remove the card and use the space for something honest (recent alerts feed, live deployments table).
- **Total Return (F-9):** Either drop the card OR wire to backtest-portfolio `total_return` field if a "primary portfolio" concept makes sense.
- **Trend arrows (F-10):** Drive `trend` from `value` sign on each card.
- **Active Strategies (existing ActiveStrategies component):** Good as-is (props-driven from `/live/status`).
- **Recent Trades:** Good as-is.
- **Strategies list F-12 metrics:** Either fetch per-strategy best-backtest metrics on the index (new endpoint or N+1 fetches) OR remove the metric columns and lean on the detail page.

### 13.4 Updated prioritization

Re-rank with the new findings folded in:

**P0 — must fix in this PR:**

1. Strip `/settings` to honest profile-only (F-1, F-2, F-3, F-4, F-5)
2. Replace `EquityChart data={[]}` with either a real backend curve endpoint OR a different honest dashboard card (F-11)
3. Wire `user.role` via `/auth/me` into `useAuth()` context (F-8 — required by P0-#1)

**P1 — strong candidates for this PR:** 4. Alerts list page (M-1) 5. Strategy edit form with PATCH (I-1) 6. Strategy delete (backend soft-delete + UI confirm) (I-2 + backend fix) 7. Account portfolio page (M-2) — gated on ib_account caching fix 8. Account health card (M-3) — same gate 9. Strategy "Validate" button fix — call real endpoint (F-6) 10. Strategy config "Save" button — PATCH config + name + description (F-7, completes I-1) 11. PortfolioSummary trend + Total Return fix (F-9, F-10) 12. Phase 1 templates policy decision-doc (cut OR amend — required before any templates UI in or out of this PR)

**P2 — polish if budget allows in this PR:** 13. Live audit-log drawer (M-4) 14. Research job cancel CTA (M-5) 15. Market-data storage status card (M-6) 16. Symbol readiness UI (M-7) 17. /strategies list — wire status + metrics per card (F-12) 18. Dashboard densification — add Alerts feed card linking to /alerts 19. Header notification badge for unread alerts 20. Global 404/500/401/403 styled pages 21. Playwright spec graduation for every shipped UI (P2-V1) 22. CI workflow activation (P2-V2)

### 13.5 Counted findings

- **MISSING_UI:** 7 (M-1 through M-7)
- **FAKE_UI / BROKEN_UI:** **12** (F-1 through F-12) — was 4 in the council's initial view, **tripled after deep read**
- **INCOMPLETE_UI:** 4 (I-1 through I-4)
- **DEAD_NAV:** 3 (D-1 through D-3)
- **Backend safety:** 4 (ib_account caching, client_id cap, strategy soft-delete, TOCTOU)
- **Phase 1 policy decisions:** 1 (templates scaffolder)
- **Total in-scope items for this PR:** ~25-30 distinct deliverables across UI + backend + decision docs + Playwright specs

**This is the real shape of "UI completeness."** PR #67's six-item list was the tip of the iceberg. The deep read tripled the FAKE_UI count alone. Most fakes cluster in two pages (`/settings` and `/dashboard`); fixing those two pages addresses the bulk of the visible "lying UI" problem.
