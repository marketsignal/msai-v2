# PRD Discussion: Universe Page

**Status:** In Progress
**Started:** 2026-04-30
**Participants:** Pablo, Claude

## Original User Stories

This feature was **explicitly deferred** as a non-goal of the upstream `symbol-onboarding` PRD (ratified 2026-04-24, see `docs/prds/symbol-onboarding.md` §"Non-Goals" line 37):

> ❌ **UI surface** (`/universe` Next.js page). v1 is API + CLI only. UI deferred post-v1 once API shape is proven.

The symbol-onboarding API + CLI surface shipped in **PR #45 (2026-04-25)**. We are now building the UI on top of the proven API shape.

**Codex SPLIT_RELEASE_TRAIN verdict (2026-04-30)** defines v1 scope:

- 3-state readiness matrix (`registered` / `backtest_data_available` / `live_qualified`)
- Coverage-gap drawer per row
- Manifest editor (replaces hand-editing `watchlists/*.yaml`)
- Actions: onboard / refresh IB / repair gaps / remove
- **v1.1 deferred-but-committed:** IB market data subscription enumeration (live-probe via `reqMktData` per user × asset class). Codex commitment device: backlog issue + acceptance criteria created at v1 ship time.

## Backend contract (already shipped, do not redesign)

From PR #45:

- `POST /api/v1/symbols/onboard` — async, returns `202 + job_id`. Composes bootstrap + ingest + optional IB refresh.
- `GET  /api/v1/symbols/onboard/{job_id}/status` — per-symbol `step`/`error`/`next_action`; mixed batch ends `completed_with_failures`.
- `POST /api/v1/symbols/onboard/dry-run` — returns `estimated_cost_usd`, `estimate_basis`, `estimate_confidence`. Submit accepts `max_estimated_cost_usd`; fails closed if exceeded.
- `GET  /api/v1/symbols/status` — readiness matrix, **window+provider scoped** (`backtest_data_available` is never symbol-global; lists without window-in-scope return `null` or coverage summary).
- CLI: `msai symbols onboard <manifest>`, `msai symbols status`.

Existing pages closest to this surface: `/data-management`, `/market-data` — neither shows the 3-state matrix.

## Discussion Log

### 2026-04-30 — Initial questions for Pablo

I have the v1 scope from the SPLIT_RELEASE_TRAIN verdict and the backend contract from PR #45. Before writing the PRD, I need to nail down the user-visible behavior. Questions grouped by concern; answer in any order.

#### Page entry & navigation

**Q1 — Route and nav placement.**
Should the URL be `/universe`, `/symbols`, or `/watchlists`? Does it get a primary-nav slot, and if so where (near `/data-management`, between `/strategies` and `/backtests`, or somewhere else)? Does it _replace_ `/data-management` or `/market-data`, or sit alongside?

**Q2 — Cold-start landing view.**
When you visit the page on a fresh install (no `watchlists/*.yaml` files yet), what should you see? Options:

- (a) Empty state with one big "Create your first watchlist" CTA
- (b) The known symbols already in `instrument_definitions` from past one-off ingests, ungrouped
- (c) A read-only "you have no universes yet — here's the manifest format" tutorial card
- (d) Something else

#### Information architecture for the readiness matrix

**Q3 — Grouping when you have multiple watchlists.**
You have 3 watchlists with 50 total symbols. Default view:

- (a) One flat table, with a `watchlist` column you can sort/filter by
- (b) Tabs per watchlist + one "All symbols" tab
- (c) Collapsible sections, one per watchlist
- (d) Sidebar of watchlist names + the active one renders right-of-pane

**Q4 — Default-window for `backtest_data_available`.**
The backend told us this column is `null` unless a window is in scope (Contrarian's amendment). On the page, do you want:

- (a) A page-level window picker (e.g. "trailing 5y") that scopes the entire matrix; default to whatever the active watchlist declares
- (b) Per-row "based on this row's declared window" — column shows green/yellow/red for that row's intent
- (c) Both — a page-level override that supersedes per-row intent
- (d) No column on the list view; only show coverage on the drawer

#### Drawer (per-row detail)

**Q5 — What lives in the drawer when you click a row.**
Pick the must-haves; we cut anything else for v1:

- Coverage timeline (visual: 5y bar with green/red ranges; or just a list of gaps)
- Provider attribution per range (which gap is Databento vs IB)
- IB qualification details (contract id, exchange, last refresh time)
- Recent onboard/refresh job history for THIS symbol (last 5)
- Raw `GET /symbols/{symbol}/status` JSON in a collapsible
- "Repair this gap" button per missing range
- "Remove this symbol from watchlist" action
- Anything else?

#### Manifest editor

**Q6 — Editor model.**
Two ends of a spectrum:

- (a) **Form-based:** add/remove rows, pick symbol from typeahead, set start/end via date pickers — generates the YAML for you. Pablo never sees raw YAML.
- (b) **YAML editor:** Monaco or CodeMirror with schema-aware autocomplete + lint. Pablo edits the file.
- (c) **Both:** form by default with a "Switch to YAML" toggle.
  Which do you want for v1, and is it OK if the MVP ships only one mode?

**Q7 — Save flow.**
Manifests are git-tracked YAML files. When you click "Save" in the editor:

- (a) Backend writes the file directly via a server action; you commit later in a normal git workflow
- (b) Page generates a unified diff and shows a "copy to clipboard / commit hint" — you apply it manually
- (c) Backend writes + auto-commits with a generic message (`chore(watchlist): update core-equities`)
- (d) Backend writes + auto-commits + pushes (full automation)

What's the right level of automation for a single-user, git-as-audit-trail product?

#### Actions, jobs, and feedback

**Q8 — Onboard flow + cost preview.**
When you click "Onboard" on a row (or "Onboard all" on a watchlist):

- Does the UI _always_ run dry-run first and show the cost estimate before executing? Or is dry-run a separate button you can skip?
- If dry-run exceeds your `max_estimated_cost_usd`, do you want the submit button disabled with a banner, OR an explicit "I accept higher cost: $X" override?

**Q9 — Async job UX.**
`POST /symbols/onboard` returns `202 + job_id`. Backend recommends ~minutes wall-clock for a 5-equity 1y batch. While the job runs:

- (a) Toast + background polling; matrix rows show inline status pills updating live
- (b) Modal that blocks the page until done (with cancel-aware copy even though cancel doesn't exist yet)
- (c) Dedicated "Jobs" panel/drawer you can open/close while the matrix stays interactive
- On finish: notification (browser permission), in-page toast, sticky banner, or all three?

**Q10 — Per-row action surface.**
Where do `onboard` / `refresh IB` / `repair gaps` / `remove` actions live?

- (a) Three-dot menu on each row
- (b) Inline action buttons (visible on hover)
- (c) Toolbar at top of matrix for bulk actions; per-row drawer for single actions
- (d) Both per-row menu AND top-bar bulk actions

#### Persona scope

**Q11 — Read-only consumers.**
The upstream PRD lists strategies + dashboards as read-only API consumers. For v1, does the page need to render a useful read-only mode (e.g. for an embedded dashboard widget), or is universe-page strictly Pablo's edit surface and read-only consumers stay on raw API?

#### Form factor

**Q12 — Mobile / tablet target.**
Is universe-page a desktop-only page (manifest editor + 50-row matrix don't shrink well), or do you want at least the matrix readable on tablet/mobile?

---

### 2026-05-01 — Council verdict + Missing-Evidence resolutions (FINAL, LOCKED)

**Engineering Council (5 advisors + Codex chairman, gpt-5.5 xhigh) ratified the v1 design 2026-05-01.** Final answers per question, with Missing-Evidence verifications and Pablo's resolutions appended.

| Q                        | Final answer                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Q1.1 URL**             | `/market-data` repurposed; existing chart moves to `/market-data/chart?symbol=X`. Sidebar label stays "Market Data".                                                                                                                                                                                                                                                                                      |
| **Q2 Cold-start**        | Stable empty table shell with persistent header `Add symbol` button + in-table empty CTA wired to the same Add flow. No wizard, no suggested-tickers card.                                                                                                                                                                                                                                                |
| **Q4(i) Stale semantic** | **Ship existing month-level + 7-day trailing-edge tolerance** (`services/symbol_onboarding/coverage.py:_apply_trailing_edge_tolerance`). Chairman's preferred "expected market sessions" semantic deferred to v1.1 — current logic uses calendar-month granularity (`compute_coverage` in `coverage.py`). Documented v1 limitation: false-stale on month boundaries acceptable for single-user dashboard. |
| **Q4(ii) Window**        | Page-level coverage window picker, default trailing 5 years. Picker MUST debounce 300ms + cancel in-flight requests on change (Hawk's accepted blocker).                                                                                                                                                                                                                                                  |
| **Q5 Drawer**            | Keep: last-refresh timestamp / repair-gap action / remove-from-inventory action / view-chart deep link / recent job history (last 5). Cut: per-provider attribution (single provider in v1) / row-level coverage timeline visual (defer to later usability pass if table can't expose gaps clearly).                                                                                                      |
| **Q8 Cost flow**         | Straight-through onboarding guarded by `max_estimated_cost_usd`. If estimate exceeds cap → submit disabled + banner with "raise cap" link. **Server-side enforcement is mandatory** (Hawk's accepted blocker).                                                                                                                                                                                            |
| **Q9 Async UX**          | Dedicated Jobs drawer/panel + inline row status pills. Completion notification = in-page toast only. Polling MUST: exponential backoff (start 2s, cap 30s on no state change) + pause on `document.visibilityState === 'hidden'` + hard stop on terminal status (Hawk's accepted blocker).                                                                                                                |
| **Q10 Action surface**   | Per-row kebab menu + top-bar bulk actions (refresh-all-stale, repair-all-gaps, remove-multiple). No hover-only primary controls.                                                                                                                                                                                                                                                                          |
| **Q11 Read-only mode**   | Dropped. Single-user product.                                                                                                                                                                                                                                                                                                                                                                             |
| **Q12 Mobile target**    | Desktop-only (1024px+), with "best on desktop" message below that width.                                                                                                                                                                                                                                                                                                                                  |

**Hawk's three accepted blockers (now v1 implementation requirements, not polish):**

1. Polling discipline: exponential backoff (2s→30s), pause on hidden tab, stop on terminal status.
2. Window picker: 300ms debounce + cancel in-flight on change.
3. Cost-cap: server-side enforcement, not client-side only.

**Codex Missing-Evidence verifications (2026-05-01) and Pablo's resolutions:**

1. **Calendar/completeness exposure** ⚠️ → ✅ Resolved. Current `compute_coverage` works at month granularity with 7-day trailing-edge tolerance. **Pablo: ship as-is for v1; defer real session-aware logic to v1.1 if needed.**

2. **NO bulk readiness endpoint exists** (chairman missed this; main agent caught) ⚠️ → ✅ Resolved. **Pablo: add `GET /api/v1/symbols/inventory?start=...&end=...&asset_class=...` as a v1 backend addition** returning array of `{symbol, asset_class, registered, backtest_data_available, coverage_status, covered_range, missing_ranges, live_qualified, provider, last_refresh_at}` per row. This becomes a new v1 backend deliverable beyond the page itself.

3. **Status endpoint polling shape** — `StatusResponse` has no ETag/version/last_changed_at. Polling fetches full payload every time. **Acceptable for v1 IF Hawk's three rules are followed** (which they are per the verdict). Optional later: add `last_state_change_at` for cheaper polling.

4. **Existing `/market-data` migration** ✅ Trivial — 5 frontend references, zero internal route bookmarks, no redirects needed. Sidebar.tsx + page-refactor only.

5. **Cost-cap config + default** ⚠️ → ✅ Resolved. Server-side enforcement exists at `symbol_onboarding.py:343-357` but only when `cost_ceiling_usd` is provided. **Pablo: add `settings.symbol_onboarding_default_cost_ceiling_usd` env var (default `$50`); UI surfaces as editable; server enforces always (uses default if request omits).**

**Pablo on inventory size (Q3-of-evidence):** Medium — 30-80 symbols expected for v1 use. Confirms bulk actions + Jobs drawer are right-sized; bulk readiness endpoint is required (not optional) for that size.

**v1 backend deliverables (beyond page UI):**

- New: `GET /api/v1/symbols/inventory` (bulk readiness for all registered instruments, asset-class-filterable, window-scoped).
- New: `settings.symbol_onboarding_default_cost_ceiling_usd` env var ($50 default).
- Modified: `POST /api/v1/symbols/onboard` — when `cost_ceiling_usd` is omitted in request, fall back to settings default (currently passes through with no check).

**Status:** All Q1–Q12 + Missing Evidence resolved. Ready to write PRD.

---

### 2026-05-01 — Late finding: Databento billing is $0 for v1 schemas under Pablo's existing subscription

**Trigger:** Pablo asked during brainstorming whether Databento downloads cost money under his plan. We had been treating cost-cap enforcement as the primary user friction for the Add-symbol flow.

**Empirical probe** (using Pablo's live API key, 2026-05-01 via `metadata.get_cost`):

| Probe                         | Result                         |
| ----------------------------- | ------------------------------ |
| AAPL XNAS.ITCH ohlcv-1m, 1y   | $0.00 ✓ included               |
| AAPL XNAS.ITCH ohlcv-1m, 5y   | $0.00 ✓ included               |
| AAPL XNAS.ITCH definition, 1y | $0.00 ✓ included               |
| AAPL XNAS.ITCH trades, 1d     | $0.00 ✓ included               |
| ES.c.0 GLBX.MDP3 ohlcv-1m, 1y | $0.00 ✓ included               |
| AAPL XNAS.ITCH mbo, 1d        | $0.26 (out-of-plan order book) |
| SPY OPRA.PILLAR ohlcv-1m, 1d  | $4.95 (out-of-plan options)    |

Pablo's subscription covers unlimited XNAS.ITCH OHLCV-1m + trades + definitions (US equities historical) and unlimited GLBX.MDP3 OHLCV-1m (CME futures historical). That's the full v1 scope.

**Pablo's decision:** Keep cost-cap PRD section as-written (defense-in-depth); update happy-path UX to render `$0.00 — included in your Databento plan` so the v1 normal flow has zero friction. The cap fires only on out-of-plan schema usage (MBO, OPRA, live-streaming) or future plan changes — not the primary v1 UX.

**PRD updates committed (2026-05-01):**

- US-002 acceptance criteria — added in-plan happy-path criterion that shows `$0.00 — included in your Databento plan` and enables Confirm by default.
- §5 Business / Compliance Constraints — clarified v1 schemas are $0 metered; cap is defense-in-depth not primary friction.
- §8.3 NFR — clarified cap fires on out-of-plan / future-plan-change scenarios.

**No code changes required for this finding.** The cost estimator already calls Databento's `metadata.get_cost` which returns the correct value ($0 in-plan, real $ out-of-plan); the server-side cap enforcement at `api/symbol_onboarding.py:343` already does the right thing in both cases. This was a UX framing correction, not a logic correction.

---

### 2026-05-01 — v1 scope reframe (symbol-centric, confirmed)

**Trigger:** Pablo's vision (2026-05-01) reframed the page from watchlist-centric to symbol-centric. Codex second opinion (2026-05-01, gpt-5.5 xhigh) recommended split-release-train option (c): ship symbol-centric Market Data v1 using PR #45 backend; defer Databento discovery + inline charts + multi-asset providers to v1.1.

**Confirmed v1 scope:**

- **Page:** existing `/market-data` nav slot, repurposed. The current single-symbol chart functionality at `/market-data/page.tsx` moves to a deep-link route (`/market-data/chart?symbol=X` or `/charts`) — separate from the inventory.
- **Killed:** `/data-management` (134-line flat table is strictly inferior). `<StorageChart>` + `<IngestionStatus>` widgets fold into the new page IF they look right visually; otherwise dropped. Inert "Trigger Download" button is removed (dead controls > missing controls is false — Codex's call).
- **Asset coverage v1:** equities + futures + FX-futures, all via Databento (already the default in `data_ingestion.py:277`). FX-futures route through the `futures` asset class. Polygon stays for options when those land. Spot FX, crypto: explicit out-of-scope.
- **UI shape:** flat symbol table, filter by asset class. Columns: symbol / asset class / provider / 3-state readiness (registered / backtest_data_available / live_qualified) / coverage from-to / stale-flag / actions (onboard / refresh / repair / remove / view chart).
- **Add symbol flow:** "Add symbol" button → modal/form, type ticker, pick asset class + window. Submits to `POST /api/v1/symbols/onboard`. Cost preview via `/dry-run` before commit.
- **Watchlists / universes:** **invisible to the user in v1.** Backend `watchlists/*.yaml` files stay as-is; orchestrator writes them. No manifest editor.
- **Strategy↔universe binding:** **out of this PRD.** Belongs on `/strategies` page (future PRD). v1 of `/market-data` is pure data-platform concern; symbol-to-strategy mapping is a strategy-config concern. Universe-of-1 (e.g., SPY-only strategy) is just a degenerate universe-of-N — no separate UI needed.
- **Deferred to v1.1:** Databento catalog discovery (free-text symbol search — Codex flagged Databento ≠ Polygon-style search; needs its own design pass), inline charts on inventory page, options (ticker-level vs chain-level needs scoping), spot FX, crypto.

**Codex landmines noted:**

1. Databento "search" is a UX/abstraction trap — defer to v1.1 with dedicated design.
2. "Add options" is ambiguous (one OCC / underlying / chain / expiries / strikes / rolling rules). Needs scoping when options lands.
3. `/universe` URL was strategy-jargon-y; nav label "Market Data" is clearer for the data-inventory job. URL TBD.

**Status of original Q1–Q12 after reframe:**

| Q                                   | Status                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| Q1 — route + nav                    | ✅ ANSWERED: nav label "Market Data", existing slot. URL TBD (see Q1.1 below). |
| Q2 — cold-start view                | 🔄 Needs re-asking with symbol-centric framing                                 |
| Q3 — multi-watchlist grouping       | ⛔ OBSOLETE (no watchlists in UI)                                              |
| Q4 — backtest_data_available window | 🔄 Still relevant                                                              |
| Q5 — drawer contents                | 🔄 Still relevant                                                              |
| Q6 — manifest editor model          | ⛔ OBSOLETE (no manifest editor)                                               |
| Q7 — save flow                      | ⛔ OBSOLETE (no manifest editor)                                               |
| Q8 — onboard + cost preview         | 🔄 Still relevant                                                              |
| Q9 — async job UX                   | 🔄 Still relevant                                                              |
| Q10 — per-row action surface        | 🔄 Still relevant                                                              |
| Q11 — read-only consumer mode       | 🔄 Still relevant                                                              |
| Q12 — mobile/tablet target          | 🔄 Still relevant                                                              |

### 2026-05-01 — Round 2 questions (post-reframe)

**Q1.1 — URL.**
Three options: (a) `/market-data` (repurpose existing — but the existing chart page must move), (b) `/data` (shorter, broader), (c) `/universe` (keep this PRD's working name — but Codex flagged it as strategy-jargon for a data-inventory page). Nav label is "Market Data" regardless. The chart deep-link target: `/market-data/chart?symbol=X`, `/charts/X`, `/market-data?chart=X` (overlay), or other?

**Q2 — Cold-start view (no symbols yet).**
Brand-new install, no symbols ingested. What do you see?

- (a) Empty state + big "Add your first symbol" CTA
- (b) Pre-seeded suggestions ("Popular: SPY, QQQ, AAPL — click to add")
- (c) Empty table + "Add symbol" button always in the header
- (d) Onboarding wizard that walks you through adding 3-5 starter symbols

**Q4 — Coverage / "stale" semantics.**
The 3-state readiness column needs a window in scope to evaluate `backtest_data_available`. Two questions in one:

- **What's "stale"?** A symbol with valid coverage but data older than N days, or missing trailing days? Define "stale" = (last bar date < today - X days) where X = ?
- **Window for the matrix:** (a) page-level window picker that scopes the whole table (default "last 5y"), (b) per-symbol intent — symbol declares its own window when added, column reflects that, (c) two columns: "what's there" (coverage from-to, always) + "is it complete" (red/yellow/green vs. some target).

**Q5 — Row drawer / detail view.**
Clicking a row opens a drawer (or expands the row). Pick the must-haves; cut the rest:

- Coverage timeline (visual bar showing green ranges + red gaps over the symbol's history)
- Per-provider attribution (which range came from Databento vs. Polygon)
- Last refresh timestamp (when did we last hit the provider for this symbol)
- Recent job history for this symbol (last 5 onboard/refresh attempts + outcomes)
- "Repair gap" button per missing range
- "Remove from inventory" action
- "View chart" button (deep-link to chart page)
- Anything else?

**Q8 — Onboard cost preview behavior.**
"Add symbol" is the v1 entry point. When you submit:

- (a) Always run `dry-run` first; show cost estimate; you click "Confirm" to actually onboard
- (b) Dry-run is a separate "Estimate cost" button you can skip
- (c) Submit goes straight through with `max_estimated_cost_usd` cap; only blocks if exceeded
  On cap exceeded: (a) submit disabled with banner + "raise cap" link, (b) explicit "I accept higher cost: $X" override checkbox.

**Q9 — Async job UX.**
`POST /symbols/onboard` returns 202+job_id; jobs take seconds-to-minutes. While running:

- (a) Toast on submit + inline status pill on the row (live updating)
- (b) Modal that blocks the page until done
- (c) Dedicated "Jobs" drawer/panel you can open while the matrix stays interactive
  On finish: in-page toast / sticky banner / browser notification (with permission) / all three?

**Q10 — Per-row action surface.**
Where do row actions (refresh / repair / remove / view chart) live?

- (a) 3-dot menu on each row
- (b) Inline action buttons (visible always, or on hover)
- (c) Toolbar at top of matrix for bulk; per-row drawer for single
- (d) Both per-row menu + top-bar bulk

**Q11 — Read-only mode (drop?).**
Original question asked about read-only consumers. In a single-user product, this is probably out of scope. Confirm: drop Q11, no read-only mode in v1?

**Q12 — Mobile / tablet target.**
A flat symbol table works better on mobile than a watchlist matrix. Three options:

- (a) Desktop-only (1024px+); show "best on desktop" message on smaller screens
- (b) Tablet+ (768px+); table compresses but is usable
- (c) Mobile-friendly (320px+); table degrades to card view on small screens
  The "Add symbol" modal works on mobile regardless. Question is the inventory view.
