# PRD: Market Data v1 (universe-page)

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-01
**Last Updated:** 2026-05-01

---

## 1. Overview

A symbol-centric inventory page at `/market-data` for managing the historical-data corpus that powers backtests and live deployments. The page replaces the current `/data-management` flat table and the current single-purpose `/market-data` chart page with one consolidated surface where Pablo can see what symbols he has, whether their data is fresh, what coverage exists, and trigger refreshes or onboard new symbols. The page targets equities, futures, and FX-futures via the existing Databento provider integration; broader asset classes and Databento catalog discovery are explicit v1.1 scope.

This work expands on the symbol-onboarding API + CLI surface shipped in PR #45 (2026-04-25). Internal naming retains `universe-page` (matches branch + worktree + discussion log); the user-facing nav label is "Market Data".

## 2. Goals & Success Metrics

### Goals

- Give Pablo a single place to answer "what data do I have, is it fresh, where are the gaps" for every symbol he tracks.
- Let Pablo onboard a new symbol via UI (not just CLI) with cost visibility before commit.
- Surface the 3-state readiness matrix (registered / backtest_data_available / live_qualified) per symbol.
- Eliminate `/data-management` (inferior flat table) and the inert "Trigger Download" control.
- Keep async onboard/refresh jobs observable without blocking the page.

### Success Metrics

| Metric                                              | Target                                                                                         | How Measured                                                                            |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Pablo can identify every stale row in the inventory | 100% of stale rows visible without leaving the page                                            | UAT — Pablo opens page, every row whose data is stale shows the stale indicator visibly |
| Add-symbol flow completes without CLI fallback      | All Q2 2026 onboards Pablo runs go through the UI, none via CLI                                | Operator survey — Pablo reports zero CLI onboards after ship                            |
| Cost-cap protection works                           | Zero unintended onboard runs that exceed the cap                                               | Manual verification: submit an over-cap request, confirm server rejects                 |
| `/data-management` retired                          | Route returns 404 or redirects; nav slot reused                                                | Code-review check + UI inspection                                                       |
| Page remains usable when 80 symbols are listed      | Initial render under 1s, scroll smooth at 60fps, polling does not exceed 1 req/2s while hidden | Devtools profile + network panel                                                        |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Databento catalog discovery / free-text symbol search** — deferred to v1.1; Codex flagged Databento ≠ Polygon-style search and warrants its own design pass.
- ❌ **Inline charts on the inventory page** — chart functionality stays on a deep-link target (`/market-data/chart?symbol=X`); no inline rendering on the inventory rows.
- ❌ **Options coverage** — neither chain-level nor contract-level. Defers until "what does Add options mean" is scoped.
- ❌ **Spot FX, crypto, or any non-Databento asset class** — out of v1; would require new provider integration.
- ❌ **Strategy-to-universe binding UI** — symbol-to-strategy mapping is a `/strategies` page concern, not this PRD.
- ❌ **Watchlist management UI** — backend `watchlists/*.yaml` files remain implementation detail; no manifest editor, no group-naming UI, no watchlist-as-addressable-unit surface.
- ❌ **Multi-user permissions / read-only consumer mode** — single-user product.
- ❌ **Mobile / tablet support** — desktop-only at 1024px+; smaller widths show "best on desktop" message.
- ❌ **Per-provider attribution column** — single provider in v1; revisit when Polygon enters for options.
- ❌ **Row-level coverage timeline visual** — deferred unless later usability pass shows gap visibility is insufficient.
- ❌ **Real session-aware "stale" semantic** — v1 uses month-grain coverage with 7-day trailing-edge tolerance; exchange-calendar logic defers to v1.1 if needed.

## 3. User Personas

### Pablo (sole user)

- **Role:** Owner / sole developer of MSAI v2; defines, backtests, and deploys his own strategies.
- **Permissions:** Full access to every surface (Azure Entra ID JWT-authenticated; no RBAC tiers in v1).
- **Goals:** Know the current state of the historical-data corpus at a glance, add new symbols quickly, refresh stale data without ambiguity, and never accidentally trigger an expensive Databento pull.
- **Form factor:** Desktop primary; macOS Safari / Chrome / Firefox at 1440px+. Will not use this page on mobile.

## 4. User Stories

### US-001: Browse the market-data inventory

**As** Pablo
**I want** to see every symbol I track, with its asset class, data-freshness state, and coverage range
**So that** I know at a glance whether my historical corpus is healthy

**Scenario:**

```gherkin
Given I have 40 symbols across equities and futures registered in the inventory
When I navigate to /market-data
Then I see a flat table with one row per symbol
And each row shows: symbol, asset class, registered status, backtest_data_available, live_qualified, coverage from-to, stale flag
And rows whose latest data is older than (last completed expected month + 7 days) are visibly marked stale
And the page-level coverage window picker defaults to "trailing 5 years"
And the table renders within 1 second on initial load
```

**Acceptance Criteria:**

- [ ] Inventory rendered as a flat table (no per-watchlist grouping, no tabs).
- [ ] Filter control allows narrowing by asset class (equities / futures / FX-futures).
- [ ] Coverage window picker is page-level, defaults to trailing 5 years; choices include common windows (1y, 2y, 5y, 10y, custom).
- [ ] Window picker debounces user input by 300ms and cancels in-flight requests when the window changes.
- [ ] 3-state readiness columns reflect the current `/api/v1/symbols/inventory` response window-scoped to the active picker.
- [ ] "Stale" indicator follows the existing month-level + 7-day-trailing-edge tolerance from `services/symbol_onboarding/coverage.py`.
- [ ] Empty state (no symbols registered yet) shows the same table shell + an in-table empty CTA wired to the same Add-symbol flow as the persistent header button.

**Edge Cases:**

| Condition                                      | Expected Behavior                                                                                        |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Inventory is empty                             | Show table shell + in-table empty CTA + persistent header "Add symbol" button; no wizard, no suggestions |
| 80+ symbols registered                         | Table virtualizes if needed; scroll stays at 60fps                                                       |
| Network failure on inventory fetch             | Show actionable error banner + retry button; do not blank the table on transient errors                  |
| User changes window picker rapidly             | Only the most recent request's response renders; in-flight responses are cancelled                       |
| Symbol exists in inventory but no Parquet data | Row shows registered=true, backtest_data_available=false, coverage_status="none"                         |

**Priority:** Must Have

---

### US-002: Add a new symbol to the inventory

**As** Pablo
**I want** to onboard a symbol I don't currently have, with a cost preview before I commit
**So that** I never accidentally trigger an expensive Databento pull

**Scenario:**

```gherkin
Given I am on /market-data
When I click "Add symbol" in the header
And I enter "AAPL" with asset class "equity" and date range last-5-years
And the estimated cost from /api/v1/symbols/onboard/dry-run is $4.20 (under the cap)
And I click "Confirm"
Then the onboard job is submitted via POST /api/v1/symbols/onboard
And I see a status pill on the new row indicating "in_progress"
And I see the same job in the Jobs drawer
And when the job completes, the row updates to backtest_data_available=true and an in-page toast confirms success
```

**Acceptance Criteria:**

- [ ] "Add symbol" button is visible in the page header at all times (not gated on emptiness).
- [ ] Add modal accepts: symbol (required), asset_class (required, dropdown of equity/futures/fx), start date, end date.
- [ ] Submit calls `POST /api/v1/symbols/onboard/dry-run` first; cost estimate + estimate_basis + confidence are shown to the user.
- [ ] If estimated_cost = $0.00 (in-plan happy path for v1 schemas) → modal shows `"$0.00 — included in your Databento plan"`; "Confirm" enabled by default, no cap-exceeded UI fires. **(This is the expected v1 flow — see §10.bis.)**
- [ ] If estimated_cost > $0 AND ≤ effective cost cap → "Confirm" button enabled; click submits to `POST /api/v1/symbols/onboard`.
- [ ] If estimated_cost > effective cost cap → "Confirm" button disabled, banner shows "$X exceeds your cap of $Y" with a "raise cap" link to settings.
- [ ] Effective cap = `cost_ceiling_usd` from request (UI default = `settings.symbol_onboarding_default_cost_ceiling_usd`, $50 USD).
- [ ] On successful submit, the new row appears in the inventory with status pill = "in_progress" and the job appears in the Jobs drawer.
- [ ] Validation errors from the backend (422 INVALID_DATE_RANGE, etc.) are surfaced inline in the modal, not as toasts.

**Edge Cases:**

| Condition                                                          | Expected Behavior                                                                                                                 |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Symbol already exists in inventory                                 | Modal shows "already registered — refresh instead?" with a one-click pivot to refresh-flow                                        |
| Dry-run returns "unpriceable" (UnpriceableAssetClassError)         | Banner shows the asset class is unpriceable; submit still allowed if user explicitly opts in (out-of-scope for v1 — block submit) |
| Network failure during dry-run                                     | Inline error in modal; submit disabled until retry succeeds                                                                       |
| User submits without window picker matching the add-modal's window | Onboard uses the dates from the modal, not the page-level picker                                                                  |

**Priority:** Must Have

---

### US-003: Refresh stale data for a symbol

**As** Pablo
**I want** to refresh a symbol whose data is stale
**So that** I can backtest against current data without manual CLI work

**Scenario:**

```gherkin
Given a row's stale indicator is showing
When I open the row's kebab menu and click "Refresh"
Then a refresh job submits via the existing onboard endpoint with the row's existing window
And a status pill on the row updates to "in_progress"
And on completion the stale indicator clears and the toast confirms success
```

**Acceptance Criteria:**

- [ ] "Refresh" available in per-row kebab menu.
- [ ] Refresh reuses the symbol's existing window (no date prompt).
- [ ] Refresh applies the same effective cost cap as new-onboard (server-side enforced).
- [ ] Status pill updates live during the job; row data refreshes when complete.

**Priority:** Must Have

---

### US-004: Repair a coverage gap

**As** Pablo
**I want** to repair a known gap in a symbol's coverage
**So that** I can backtest historical periods that previously failed to ingest

**Scenario:**

```gherkin
Given a row's coverage_status is "gapped" with a missing range "2024-03-01 to 2024-03-31"
When I open the row's drawer
Then I see the missing range listed
And I click the per-range "Repair this gap" button
Then a repair job submits scoped to that range
And the row updates when the gap closes
```

**Acceptance Criteria:**

- [ ] Row drawer shows missing-ranges from `/api/v1/symbols/inventory` response (`missing_ranges` field).
- [ ] Each missing range has a "Repair this gap" button.
- [ ] Repair submits an onboard request scoped to that range only (not the full window).
- [ ] Repair applies the cost cap.

**Priority:** Must Have

---

### US-005: Remove a symbol from the inventory

**As** Pablo
**I want** to remove a symbol I no longer track
**So that** my inventory stays focused

**Scenario:**

```gherkin
Given a row exists for symbol "OBSOLETE"
When I open the row's kebab menu and click "Remove"
And I confirm the destructive action in the dialog
Then the symbol is removed from the inventory display
And on reload, it does not reappear
```

**Acceptance Criteria:**

- [ ] "Remove" available in per-row kebab menu.
- [ ] Click triggers a confirm dialog (destructive action; cannot be a one-click).
- [ ] Removal does NOT delete the underlying Parquet data — only de-lists the symbol from the user-visible inventory (Pablo can re-onboard it later without re-paying).
- [ ] Soft-removal persists across reloads.

**Edge Cases:**

| Condition                                                    | Expected Behavior                                                                               |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| Symbol is currently used by an active backtest or deployment | Backend rejects with explanatory error; UI shows which strategies depend on it                  |
| Removed symbol is re-added later                             | New onboard reuses existing Parquet if window matches; cost estimate reflects only any new gaps |

**Priority:** Should Have (can ship v1 without if remove-soft-delete proves complex; fall back to "no remove, only refresh")

---

### US-006: Open chart for a symbol

**As** Pablo
**I want** to view a candlestick chart for any symbol in my inventory
**So that** I can visually inspect the data I have

**Scenario:**

```gherkin
Given a row for "AAPL" with backtest_data_available=true
When I open the row's kebab and click "View chart"
Then I navigate to /market-data/chart?symbol=AAPL
And the existing chart page renders the OHLCV chart for AAPL
```

**Acceptance Criteria:**

- [ ] "View chart" available in per-row kebab.
- [ ] Click navigates to `/market-data/chart?symbol=X` (preserves the existing chart-page UX).
- [ ] Existing single-symbol chart page is refactored from `/market-data` (its current location) to `/market-data/chart` to make room for the inventory.

**Priority:** Must Have

---

### US-007: Bulk-refresh all stale rows

**As** Pablo
**I want** to refresh every stale row in one operation
**So that** I don't have to click each row's kebab when a batch refresh is needed

**Scenario:**

```gherkin
Given 12 rows are marked stale
When I click "Refresh all stale" in the top toolbar
Then a confirm dialog shows the 12 affected symbols + total estimated cost from a bulk dry-run
And on confirm, 12 refresh jobs submit (one per symbol or one batched job, whichever the backend supports)
And the Jobs drawer shows progress for the batch
```

**Acceptance Criteria:**

- [ ] Top-of-table toolbar shows a "Refresh all stale" action when at least one stale row exists.
- [ ] Action shows a confirm dialog with affected symbols + total estimated cost.
- [ ] Cost cap applies (sum of estimates vs cap).
- [ ] Jobs drawer aggregates the batch's progress.

**Priority:** Should Have

---

### US-008: Track async jobs in a dedicated drawer

**As** Pablo
**I want** to see all in-flight onboard / refresh / repair jobs in one place
**So that** I can monitor batch operations without losing the inventory view

**Scenario:**

```gherkin
Given I have submitted 3 onboard jobs
When I click the "Jobs" affordance (toolbar/header icon)
Then a drawer opens showing each job with: symbol(s), action type, current step, progress counts, started timestamp
And the drawer updates live via polling
And closing the drawer does not cancel the jobs
And on each job's terminal status (completed / failed / completed_with_failures), an in-page toast confirms
```

**Acceptance Criteria:**

- [ ] Jobs drawer accessible from a persistent affordance (toolbar icon or header button).
- [ ] Drawer lists all in-flight + recent (last 5) jobs for the current session.
- [ ] Each entry shows: action type, affected symbol(s), progress (n/N), elapsed time, current step.
- [ ] Polling: starts at 2s, exponential backoff to max 30s on no state change, pauses when `document.visibilityState === 'hidden'`, hard-stops on terminal status.
- [ ] On terminal status, in-page toast appears (no browser notifications, no sticky banners).
- [ ] Jobs drawer continues running in background when closed; reopening shows current state.

**Edge Cases:**

| Condition                                     | Expected Behavior                                                                                |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| User closes the tab while jobs are running    | Backend continues; on next page load, drawer reflects current state via /onboard/{job_id}/status |
| Polling fails transiently                     | Exponential backoff continues; UI shows "checking..." not error                                  |
| Status endpoint returns 404 (run lost / GC'd) | Drawer marks job as "unknown — check CLI"; does not crash the page                               |

**Priority:** Must Have

---

### US-009: Filter inventory by asset class

**As** Pablo
**I want** to narrow the inventory to only equities, only futures, or only FX-futures
**So that** I can scan a subset without scrolling through everything

**Acceptance Criteria:**

- [ ] Asset-class filter is a control above the table (chip group, segmented control, or dropdown — chairman did not specify).
- [ ] Selecting one asset class hides rows of other asset classes.
- [ ] Default = "All".
- [ ] Filter state survives page-level window-picker changes (filter is independent of window).

**Priority:** Must Have

---

### US-010: Cost-cap default is configurable

**As** Pablo (operator)
**I want** the system-level default cost cap to be configurable via settings
**So that** I can raise or lower the default without changing code

**Acceptance Criteria:**

- [ ] New env var: `MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD` (or equivalent under the existing `Settings` model).
- [ ] Default value: $50 USD.
- [ ] When `POST /api/v1/symbols/onboard` request omits `cost_ceiling_usd`, server uses settings default.
- [ ] When request includes `cost_ceiling_usd`, request value wins (per-request override).
- [ ] Server-side enforcement is mandatory: even an X-API-Key request from CLI bypassing the UI gets the cap applied.
- [ ] UI surfaces the effective cap in the Add-symbol modal as editable; default value is the settings default (or pulls live from a settings endpoint if one exists).

**Priority:** Must Have

---

## 5. Constraints & Policies

### Business / Compliance Constraints

- The user is the sole consumer; no multi-tenant compliance applies.
- Pablo's current Databento plan covers **all v1 schemas at $0 metered** (`XNAS.ITCH` OHLCV-1m / trades / definition; `GLBX.MDP3` OHLCV-1m). Verified empirically 2026-05-01 via `metadata.get_cost`. v1 onboards do not generate a Databento bill against the existing subscription.
- Cost-cap enforcement is **defense-in-depth**, not user-visible friction in the v1 happy path. It exists to (a) prevent accidental schema misuse (e.g., requesting `mbo` instead of `ohlcv-1m`), (b) protect future expansion to OPRA options or live-streaming where charges are real ($4.95 for one day of one OPRA underlying's chain, observed 2026-05-01), and (c) survive plan downgrades or future Databento pricing changes. Cap MUST be enforced at the API layer regardless of v1 UX simplicity.

### Platform / Operational Constraints

- Desktop-only at viewports ≥1024px wide. Smaller widths show a static "best on desktop" message; no responsive degradation.
- Browser support: latest two versions of Chrome / Firefox / Safari (Edge follows Chrome). No IE / legacy Edge / mobile Safari special cases.
- WCAG AA contrast required (per `.claude/rules/frontend-design.md`).
- `prefers-reduced-motion` respected on any animations introduced.

### Dependencies & Required Integrations

- **Requires:** Symbol Onboarding API + CLI (PR #45, shipped 2026-04-25).
- **Requires:** New backend endpoint `GET /api/v1/symbols/inventory` (delivered as part of this v1 — see Section 6 below).
- **Requires:** `services/symbol_onboarding/coverage.py:compute_coverage` (already shipped).
- **Requires:** Existing `/market-data/bars/{symbol}` endpoint (chart-page deep-link target).
- **Blocked by:** Nothing — backend foundation already shipped.
- **Named integrations:** Databento (price data for equities, futures, FX-futures), Interactive Brokers (live-qualified resolution, optional in onboarding flow). No new external systems.

## 6. Backend Deliverables (v1)

> The page UI cannot ship without these backend additions. They are part of this PRD's scope.

### 6.1 New endpoint: `GET /api/v1/symbols/inventory`

- Returns a list of `{symbol, asset_class, registered, provider, backtest_data_available, coverage_status, covered_range, missing_ranges, live_qualified, last_refresh_at}` rows.
- Query params: `start` (date), `end` (date), `asset_class` (optional filter — equity | futures | fx).
- Window scoping: when `start` + `end` provided, `backtest_data_available` and `coverage_status` are computed against that window; when omitted, they're null.
- Replaces the per-symbol `GET /api/v1/symbols/readiness` for bulk inventory rendering. The per-symbol endpoint stays for drawer detail / single-symbol queries.

### 6.2 New setting: `symbol_onboarding_default_cost_ceiling_usd`

- Config field on `Settings` (env-backed via pydantic-settings).
- Default: `Decimal("50.00")` USD.
- Env var: `MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD`.

### 6.3 Modify `POST /api/v1/symbols/onboard`

- When request omits `cost_ceiling_usd`, fall back to `settings.symbol_onboarding_default_cost_ceiling_usd`.
- Existing 422 COST_CEILING_EXCEEDED behavior unchanged.

### 6.4 Retire `/data-management`

- Delete `frontend/src/app/data-management/page.tsx`.
- Remove the sidebar nav entry.
- Decide whether to fold the existing `<StorageChart>` + `<IngestionStatus>` widgets into the new page (visual judgment during implementation; if they don't earn their place, drop them).
- The inert "Trigger Download" button is removed (dead controls > missing controls is a false trade).

### 6.5 Refactor existing `/market-data` chart page

- Move `frontend/src/app/market-data/page.tsx` to `frontend/src/app/market-data/chart/page.tsx`.
- The new `/market-data` route hosts the inventory.
- Update `frontend/src/lib/api.ts` consumers if needed (existing API endpoints are unchanged; only the route file moves).

## 7. Security Outcomes Required

- **Who can access what:** Pablo authenticates via Azure Entra ID JWT. All endpoints used by this page (existing `/api/v1/symbols/*` + new `/api/v1/symbols/inventory`) require valid JWT or `X-API-Key` header per existing project policy.
- **What must never leak:** Databento API keys never appear in browser-visible payloads or error messages.
- **What must be auditable:** Every onboard / refresh / repair / remove action is auditable via the existing `symbol_onboarding_runs` table (already provides this for onboard / refresh; remove must persist a row indicating the soft-delete actor and timestamp).
- **What legal/regulatory outcomes apply:** None beyond MSAI's general single-user product context.

## 8. Non-Functional Requirements (v1 Implementation Hard-Requirements)

These three items came out of the Engineering Council as accepted blockers from the Scalability Hawk. They are NOT polish — implementation MUST satisfy them:

1. **Polling discipline.** Jobs polling implements: exponential backoff (start 2s, cap 30s when no state change observed), pause on `document.visibilityState === 'hidden'`, hard stop on terminal job status.
2. **Window-picker discipline.** The page-level coverage window picker debounces user input by 300ms and cancels in-flight requests when the window changes (prevents concurrent full-table scans).
3. **Cost-cap server enforcement.** `POST /api/v1/symbols/onboard` enforces the cap on every request (using the new settings default when the request omits `cost_ceiling_usd`). UI is a convenience layer; CLI / API key consumers also get cap protection. Note: under Pablo's current Databento subscription the v1 happy-path cost is $0 (verified 2026-05-01) — the cap is a guardrail that fires only on out-of-plan schema usage (MBO order book, OPRA options, live-streaming) or future plan changes, not the primary v1 friction.

## 9. Open Questions

- [ ] StorageChart + IngestionStatus widget fate: fold into /market-data top band, OR drop entirely? (Decision deferred to design phase based on visual fit.)
- [ ] Asset-class filter control type (chip group / segmented / dropdown) — design choice in implementation phase.
- [ ] Add-modal date range default: page-level window? trailing-5y? user-empty? — design choice.
- [ ] US-005 (remove-from-inventory) priority: confirm Must vs Should during plan-review. If soft-delete persistence is non-trivial in the existing schema, drop to Should and ship v1 without remove (refresh-only).
- [ ] Whether to surface cost-cap settings in a UI control (e.g. `/settings`) or leave as env-var only for v1.

## 10. References

- **Discussion Log:** `docs/prds/universe-page-discussion.md`
- **Upstream PRD (deferred from):** `docs/prds/symbol-onboarding.md` (Non-Goals §, line 37 — UI deferred post-v1)
- **Backend foundation:** PR #45 (2026-04-25) — `backend/src/msai/api/symbol_onboarding.py`, `backend/src/msai/services/symbol_onboarding/`
- **Coverage logic:** `backend/src/msai/services/symbol_onboarding/coverage.py`
- **Council verdict (2026-05-01):** chairman synthesis recorded in discussion log § "Council verdict + Missing-Evidence resolutions"
- **Codex second opinion (2026-05-01):** discussion log § "v1 scope reframe (symbol-centric, confirmed)"
- **Existing pages being replaced:** `frontend/src/app/data-management/page.tsx`, `frontend/src/app/market-data/page.tsx`
- **Sidebar:** `frontend/src/components/layout/sidebar.tsx`

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                               |
| ------- | ---------- | -------------- | ------------------------------------------------------------------------------------- |
| 1.0     | 2026-05-01 | Claude + Pablo | Initial PRD; symbol-centric reframe; all Q1–Q12 + Missing-Evidence resolutions locked |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo, in dev capacity)
- [ ] Ready for technical design (`/superpowers:brainstorming`)
