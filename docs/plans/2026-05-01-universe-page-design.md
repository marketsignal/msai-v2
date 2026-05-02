# Design: Market Data v1 (universe-page)

**Status:** Design complete · ready for implementation plan
**Created:** 2026-05-01
**Author:** Claude + Pablo
**PRD:** [`docs/prds/universe-page.md`](../prds/universe-page.md)
**Discussion log:** [`docs/prds/universe-page-discussion.md`](../prds/universe-page-discussion.md)
**Research brief:** [`docs/research/2026-05-01-universe-page.md`](../research/2026-05-01-universe-page.md)
**Council verdict:** see discussion log § "Council verdict + Missing-Evidence resolutions"
**Branch / worktree:** `feat/universe-page` at `.worktrees/universe-page/`

---

## 1. Overview

A symbol-centric inventory page at `/market-data` for managing the historical-data corpus. Replaces the current `/data-management` flat table; the existing `/market-data` chart page moves to `/market-data/chart`. Backend already shipped (PR #45) plus a new bulk inventory endpoint and a settings-default cost cap.

**Mode:** Product UI with Trust-First confirmation patterns on cost/destructive flows. Sole user is Pablo; desktop-only at 1024px+.

**Cost framing:** Pablo's Databento subscription covers v1 schemas at $0 metered (`XNAS.ITCH` OHLCV-1m / trades / definition; `GLBX.MDP3` OHLCV-1m), verified empirically 2026-05-01. Cost-cap UI is defense-in-depth — fires only on out-of-plan schema usage (MBO, OPRA, live) or future plan changes.

---

## 2. Locked Design Decisions (post-brainstorming)

| #   | Decision                               | Choice                                                                                                                                            |
| --- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Mode                                   | Product UI + Trust-First on cost/destructive                                                                                                      |
| 2   | Layout direction                       | A · Control Center (single dense table; header has Add + Jobs trigger; sub-toolbar has filter + window + passive stale nudge; click row → drawer) |
| 3   | Status pill style                      | Single named badge (drawer reveals breakdown)                                                                                                     |
| 4   | Status taxonomy                        | `Ready` / `Stale` / `Gapped` / `Backtest only` / `Live only` / `Not registered` (priority: worst-actionable wins)                                 |
| 5   | Stale row treatment                    | Subtle row tint (rgba yellow ~6% alpha) + yellow timestamp in Last refresh column when > 7 days                                                   |
| 6   | Column set                             | `Symbol` · `Class` · `Status` · `Coverage` · `Last refresh` · `⋯ kebab`                                                                           |
| 7   | Row drawer layout                      | A · Sectioned panel (single scroll; header → Status → Actions → Coverage → Recent jobs → Metadata)                                                |
| 8   | StorageChart + IngestionStatus widgets | Drop. Small footer link `Storage: 12.4 GB · last ingest 3h ago` or page-header tooltip                                                            |
| 9   | Asset-class filter control             | shadcn `ToggleGroup` (`All` / `Equity` / `Futures` / `FX`)                                                                                        |
| 10  | Window picker control                  | shadcn `Select` (`1y` / `2y` / **`5y`** / `10y` / `Custom`); `Custom` opens date-range popover                                                    |
| 11  | Jobs trigger                           | Header button with count: `⏵ Jobs (3)`; opens same-width drawer; only one drawer open at a time                                                   |
| 12  | Add-symbol modal                       | Single-step shadcn `Dialog` (not Sheet, not multi-step)                                                                                           |
| 13  | Empty-state CTA                        | "No symbols in your inventory yet." + `[+ Add your first symbol]` button wired to same Add modal                                                  |
| 14  | Coverage cell                          | `2019-01 → 2026-04` (compact); `· 2 gaps` suffix when gapped; `none` when empty                                                                   |
| 15  | Last refresh cell                      | Relative (`2h ago` / `3d ago` / `2w ago`); yellow when > 7 days; absolute on hover tooltip                                                        |

---

## 3. Architecture

### 3.1 Page structure

```
┌─ Sidebar (existing) ─┬─ Page (1024px+) ────────────────────────────────────┐
│                      │                                                       │
│  ...                 │  ┌─ Header ─────────────────────────────────────┐    │
│  Live Trading        │  │ Market Data            [+ Add symbol] [⏵ Jobs(0)]│    │
│ ▶ Market Data        │  └──────────────────────────────────────────────┘    │
│  Settings            │  ┌─ Sub-toolbar ────────────────────────────────┐    │
│                      │  │ [All|Equity|Futures|FX]  [5y ▾]   3 stale ↻ │    │
│                      │  └──────────────────────────────────────────────┘    │
│                      │  ┌─ Inventory Table ────────────────────────────┐    │
│                      │  │ Sym  Class    Status         Coverage   Refresh⋮│    │
│                      │  │ SPY  equity   ● Ready        2019→2026  2h  ⋮│    │
│                      │  │ ES   futures  ⚠ Gapped       2021→2026  8d  ⋮│ ←row click opens
│                      │  │ ...                                          │    │
│                      │  └──────────────────────────────────────────────┘    │
│                      │  Storage: 12.4 GB · last ingest 3h ago               │
└──────────────────────┴───────────────────────────────────────────────────────┘
                                                      ┌─ Row Drawer (slide-in) ─┐
                                                      │ ES                    ✕ │
                                                      │ futures · GLBX.MDP3     │
                                                      │ ⚠ Gapped                │
                                                      │ ─ Actions ─             │
                                                      │ [↻ Refresh] [📈 Chart]  │
                                                      │ [🗑 Remove]             │
                                                      │ ─ Coverage ─            │
                                                      │ 2021-01 → 2026-04       │
                                                      │ Missing 2024-03 [Repair]│
                                                      │ ─ Recent jobs ─         │
                                                      │ ↻ 2026-04-23  ✓         │
                                                      │ + 2026-04-20  ✓         │
                                                      │ ─ Metadata ─            │
                                                      │ Provider: Databento     │
                                                      │ IB: ✓ ESH6 · CME        │
                                                      │ Live qualified: yes     │
                                                      └─────────────────────────┘
```

Below 1024px viewport: render `<MobileBlocker>` placeholder ("Best on desktop"); no responsive degradation.

### 3.2 Routing changes

| Path                          | Today                                               | After                                         |
| ----------------------------- | --------------------------------------------------- | --------------------------------------------- |
| `/market-data`                | single-symbol candlestick chart                     | inventory table (this page)                   |
| `/market-data/chart?symbol=X` | does not exist                                      | the existing chart page, moved here unchanged |
| `/data-management`            | flat symbols table + storage widgets + inert button | **deleted** (route returns 404)               |

Sidebar: same slot, same label "Market Data". `/data-management` entry removed.

Frontend file moves:

- `frontend/src/app/market-data/page.tsx` → `frontend/src/app/market-data/chart/page.tsx` (verbatim move; existing chart logic preserved)
- New `frontend/src/app/market-data/page.tsx` for the inventory
- Delete `frontend/src/app/data-management/page.tsx`
- Update `frontend/src/components/layout/sidebar.tsx` to remove the data-management entry (label/href stays for Market Data)

### 3.3 Data-fetching strategy

Per research brief: **TanStack Query v5** for all async surfaces. Native handling of refetch interval, visibility-pause, AbortSignal cancellation, mutation lifecycle.

Four query/mutation surfaces:

| Surface                             | Hook                                                                   | Backend                                                                                                                 | Polling                                                                                                                                                                 | Notes                                                                                                         |
| ----------------------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Inventory list                      | `useQuery(['inventory', windowStart, windowEnd, assetClass])`          | `GET /api/v1/symbols/inventory?start=&end=&asset_class=`                                                                | none (manual refetch on action completion)                                                                                                                              | Debounced via `useDebounceValue` on `windowStart/End` (300ms) — TanStack auto-cancels in-flight on key change |
| Single symbol detail (drawer)       | `useQuery(['readiness', symbol, asset_class, windowStart, windowEnd])` | `GET /api/v1/symbols/readiness?symbol=&asset_class=&start=&end=`                                                        | none                                                                                                                                                                    | Drawer-open trigger; refetched on action completion                                                           |
| Active jobs                         | `useQuery(['jobs'])`                                                   | `GET /api/v1/symbols/onboard/{run_id}/status` per active run; aggregated client-side from a list of in-flight `run_id`s | `refetchInterval: callback` returning 2000→30000ms based on state-change observation; `refetchIntervalInBackground: false` (visibility-pause); stops on terminal status | Jobs drawer maintains the active `run_id` list in client state                                                |
| Onboard / refresh / repair / remove | `useMutation` × 4                                                      | `POST /api/v1/symbols/onboard` (with manifest variants)                                                                 | n/a                                                                                                                                                                     | On success: optimistic row update + invalidate inventory query + add `run_id` to active jobs list             |

**Polling discipline (Hawk's accepted blocker):**

```ts
useQuery({
  queryKey: ["jobs", runId],
  queryFn: ({ signal }) => fetchJobStatus(runId, { signal }),
  refetchInterval: (query) => {
    const data = query.state.data;
    if (!data) return 2000;
    if (
      data.status === "completed" ||
      data.status === "failed" ||
      data.status === "completed_with_failures"
    )
      return false; // hard stop
    if (lastPolledIdenticalTo(data, query.state.dataUpdatedAt))
      return Math.min((query.state.fetchFailureCount + 1) * 2000, 30000); // exp backoff
    return 2000;
  },
  refetchIntervalInBackground: false, // visibility-pause
});
```

**Window picker debounce (Hawk's accepted blocker):**

```ts
const [windowChoice, setWindowChoice] = useState<WindowChoice>('5y');
const debouncedChoice = useDebounceValue(windowChoice, 300);
const { start, end } = windowToDateRange(debouncedChoice);
const { data } = useQuery({ queryKey: ['inventory', start, end, assetClass], ... });
// TanStack handles cancellation via AbortSignal automatically when queryKey changes
```

### 3.4 State machine

Two mutually-exclusive drawers (only one can be open at a time):

```
            ┌─────────────────────────────────────┐
            │            no drawer open           │
            └────┬─────────┬───────────────┬──────┘
                 │         │               │
        click row│         │click "Jobs"   │click background / Esc
                 ▼         ▼               │
        ┌──────────────┐ ┌──────────────┐  │
        │ row drawer   │ │ jobs drawer  │──┘ (close)
        │ (symbol = X) │ │              │
        └──────┬───────┘ └──────┬───────┘
               │                │
    click another row OR        │
    click "Jobs"                │
               │                │
               ▼                │
        ┌──────────────┐        │
        │ row drawer   │ ←──────┘ (open jobs from row drawer closes row drawer)
        │ (symbol = Y) │
        └──────────────┘
```

Add modal is a Dialog (modal overlay) — orthogonal to drawer state. Esc closes whichever is on top.

---

## 4. Components

### 4.1 `InventoryTable`

shadcn `Table` (no virtualization needed at 30–80 rows; revisit if > 150).

Sticky header. Row click → opens row drawer. Per-row kebab menu (shadcn `DropdownMenu`) for: Refresh / Repair gaps / View chart / Remove. Top-bar bulk actions: "Refresh all stale" (visible when ≥ 1 stale row), "Repair all gaps" (visible when ≥ 1 gapped row).

Cells:

- **Symbol** — monospace, plain text. Click row anywhere → opens drawer.
- **Class** — `equity` / `futures` / `fx` lowercase, muted color.
- **Status** — `<StatusBadge value={...} />` (see 4.2).
- **Coverage** — text format from §15 in locked decisions.
- **Last refresh** — relative time; yellow when > 7 days.
- **⋯** — kebab.

Row tint when status ∈ {`Stale`, `Gapped`}: `bg-yellow-500/[0.06]` Tailwind class.

### 4.2 `StatusBadge`

Single component, six variants:

```tsx
type StatusValue =
  | "ready"
  | "stale"
  | "gapped"
  | "backtest_only"
  | "live_only"
  | "not_registered";
```

Color tokens (Tailwind, dark-mode-first):

- `ready` — `bg-emerald-500/15 text-emerald-400`
- `stale` — `bg-yellow-500/15 text-yellow-400`
- `gapped` — `bg-orange-500/15 text-orange-400`
- `backtest_only` — `bg-sky-500/15 text-sky-400`
- `live_only` — `bg-violet-500/15 text-violet-400`
- `not_registered` — `bg-zinc-500/15 text-zinc-400`

All include an icon prefix per Trust-First sub-pattern (color + icon + text, never color alone): ● / ⚠ / ⚠ / 📊 / 📡 / ○.

Priority resolution (server-side computed by `/symbols/inventory`, NOT client):

```python
def derive_status(reg, bt_avail, live, coverage_status) -> str:
    if not reg: return "not_registered"
    if reg and bt_avail and live and coverage_status == "full":
        return "ready"
    if coverage_status == "gapped" and is_trailing_only(...):
        return "stale"
    if coverage_status == "gapped":
        return "gapped"
    if bt_avail and not live: return "backtest_only"
    if live and not bt_avail: return "live_only"
    return "not_registered"  # fallback
```

`is_trailing_only` distinguishes stale (only the trailing month missing) from gapped (any mid-window gap) — cheap to compute from the `missing_ranges` list.

### 4.3 `RowDrawer`

shadcn `Sheet` from `@/components/ui/sheet`. Right side, ~420px wide. Sections in order:

1. **Header** — symbol (large, bold) + asset class + dataset (muted) + close `✕`.
2. **Status** — `<StatusBadge>` repeated, larger size.
3. **Actions** — three buttons inline: Refresh / View chart / Remove (Remove styled destructive — red on dark background).
4. **Coverage** — text "2021-01 → 2026-04 · trailing 5y window". Below: per-missing-range row, each with a `Repair` inline button. Empty when status = ready.
5. **Recent jobs** — last 5 onboard/refresh/repair runs with date + outcome icon. "Show all" link if more.
6. **Metadata** — Provider, IB qualification details, last refresh timestamp, live qualified flag.

Each section has a thin top border (`border-t border-border/50`) and a small uppercase label.

Empty drawer states:

- Loading → skeleton
- Error → inline error message + retry
- Symbol not registered → reduced drawer with onboard CTA

### 4.4 `AddSymbolDialog`

shadcn `Dialog`. Single step. Form:

- **Symbol** — text input with autocomplete from `/api/v1/symbols/inventory` (deduplicated against already-registered symbols — show "already registered" hint with one-click pivot to refresh).
- **Asset class** — `Select` (`equity` / `futures` / `fx`).
- **Start date** — date picker; default = trailing-5y (`endDate - 5y`).
- **End date** — date picker; default = today.

On Submit: dry-run runs first; cost estimate displays inline:

- `$0.00 — included in your Databento plan` (the v1 happy path) → Confirm enabled
- `$X.XX (above $50 cap)` → Confirm disabled, banner with `[Raise cap]` link to settings
- Error → inline (422 INVALID_DATE_RANGE, 422 UNPRICEABLE_ASSET_CLASS, etc.)

Confirm → `POST /api/v1/symbols/onboard`. On 202: close dialog + add `run_id` to active jobs. Error → keep dialog open + show error inline.

### 4.5 `JobsDrawer`

shadcn `Sheet`. Right side, same width as RowDrawer. Mutually exclusive with RowDrawer (opening one closes the other).

Sections:

1. **Header** — "Jobs" title + close.
2. **Active jobs** (if any) — each job: action type + affected symbols + progress (n/N) + elapsed + spinner.
3. **Recent (last 5)** — completed jobs with status icon.
4. **Empty state** — "No active jobs."

Polling lifecycle: see §3.3.

### 4.6 `Header` (page-level)

Two rows:

**Row 1 (header):**

- Title: "Market Data"
- Right side: `[+ Add symbol]` (primary button) + `[⏵ Jobs (N)]` (button with count badge)

**Row 2 (sub-toolbar):**

- Left: `<ToggleGroup>` for asset class filter
- Center-right: `<Select>` for window picker
- Right: passive stale-count nudge — `"3 stale ·"` + inline `Refresh all stale` button (visible only when ≥ 1 stale row), `"Repair all gaps"` button (visible only when ≥ 1 gapped row)

### 4.7 Empty state

Only visible when `inventory.length === 0`. Renders inside the table region:

```
┌────────────────────────────────────────────┐
│                                            │
│    No symbols in your inventory yet.       │
│                                            │
│       [+ Add your first symbol]            │
│                                            │
└────────────────────────────────────────────┘
```

Click → opens same `AddSymbolDialog` as the header button.

### 4.8 Toast surface

shadcn `sonner` (or `Toaster`). In-page toasts only. No browser notifications, no sticky banners. Toast triggers:

- Onboard / refresh / repair completion (success or failure)
- Network error during a mutation
- Cap-exceeded error (also has banner in modal — toast is reinforcement)

---

## 5. Backend additions (recap)

Per PRD §6:

### 5.1 New endpoint: `GET /api/v1/symbols/inventory`

Bulk readiness for all registered instruments.

Query params:

- `start: date` (optional) — window scope start
- `end: date` (optional) — window scope end
- `asset_class: Literal['equity', 'futures', 'fx', 'option']` (optional)

Response: bare array of `InventoryRow`:

```python
class InventoryRow(BaseModel):
    instrument_uid: UUID
    symbol: str
    asset_class: str
    provider: str
    registered: bool
    backtest_data_available: bool | None  # null when no window
    coverage_status: Literal['full', 'gapped', 'none'] | None
    covered_range: str | None  # e.g. "2019-01 → 2026-04"
    missing_ranges: list[dict[str, date]]  # [{start, end}, ...]
    is_stale: bool  # derived: trailing-edge only missing
    live_qualified: bool
    last_refresh_at: datetime | None
    status: Literal['ready', 'stale', 'gapped', 'backtest_only', 'live_only', 'not_registered']  # server-side derived per §4.2
```

**Performance risk** (research brief top open risk): per-row `compute_coverage` filesystem scan on 80 rows could blow the 1s render budget. Mitigation candidates — design phase picks one; default = (a):

- **(a)** `asyncio.gather` parallel scans, capped at concurrency-of-10 — simplest, leverages existing function unchanged.
- **(b)** Batched DuckDB scan over the full Parquet tree per asset class — single query for all rows.
- **(c)** Short-TTL Redis cache (60s) on the inventory response — refresh on action completion.

Plan-writing phase will benchmark (a) first; if it exceeds 800ms at 80 rows, fall back to (b) or (c).

### 5.2 New setting: `symbol_onboarding_default_cost_ceiling_usd`

Pydantic-settings field on `Settings`:

```python
class Settings(BaseSettings):
    ...
    symbol_onboarding_default_cost_ceiling_usd: Decimal = Decimal("50.00")
    model_config = SettingsConfigDict(
        env_prefix="MSAI_",
        ...
    )
```

Env var: `MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD`.

### 5.3 Modify `POST /api/v1/symbols/onboard`

At `backend/src/msai/api/symbol_onboarding.py:343`, change:

```python
# BEFORE:
estimated_cost: Decimal | None = None
if request.cost_ceiling_usd is not None:
    estimate = await _compute_cost_estimate(request)
    estimated_cost = Decimal(str(estimate.total_usd))
    if estimated_cost > request.cost_ceiling_usd:
        return error_response(...)

# AFTER:
effective_cap = request.cost_ceiling_usd or settings.symbol_onboarding_default_cost_ceiling_usd
estimate = await _compute_cost_estimate(request)
estimated_cost = Decimal(str(estimate.total_usd))
if estimated_cost > effective_cap:
    return error_response(
        status_code=422,
        code="COST_CEILING_EXCEEDED",
        message=f"Estimated cost ${estimated_cost:.2f} exceeds cap ${effective_cap:.2f}.",
    )
```

This change protects CLI/API key callers who omit the cap, plus the v1 UI.

### 5.4 Retire `/data-management`

Frontend deletion only. No backend route to remove (the existing `/api/v1/market-data/*` endpoints stay — the chart page at `/market-data/chart` continues to call them).

---

## 6. Error handling

| Surface                             | Failure mode                      | Handling                                                                                                                                                             |
| ----------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Inventory query                     | network / 500                     | Inline error banner above table + Retry button. Empty table renders during load.                                                                                     |
| Inventory query                     | 401                               | Redirect to /login (existing app-shell behavior)                                                                                                                     |
| Drawer query (readiness per symbol) | network / 500                     | Inline error in drawer + Retry button                                                                                                                                |
| Drawer query                        | 404 (symbol no longer registered) | Drawer renders "Symbol no longer registered" + Close button                                                                                                          |
| Add modal dry-run                   | network                           | Inline error in modal + Retry; submit disabled until dry-run succeeds                                                                                                |
| Add modal dry-run                   | 422 INVALID_DATE_RANGE            | Inline error on the offending field                                                                                                                                  |
| Add modal dry-run                   | 422 UNPRICEABLE_ASSET_CLASS       | Banner — "This asset class isn't supported for cost estimation. Submitting may incur unbounded cost. v1: Confirm disabled." (Future v1.1 may add explicit override.) |
| Onboard mutation                    | 422 COST_CEILING_EXCEEDED         | Banner in modal + raise-cap link; toast reinforcement                                                                                                                |
| Onboard mutation                    | 503 (queue full)                  | Toast — "Queue temporarily full. Retry in a moment."                                                                                                                 |
| Job status polling                  | network blip                      | TanStack handles transient retry. UI shows "checking…" not error.                                                                                                    |
| Job status polling                  | 404 (run lost / GC'd)             | Mark job as "unknown — check CLI" in jobs drawer; do not crash page                                                                                                  |
| Job mutation success                | n/a                               | Optimistic row update + invalidate inventory query + toast                                                                                                           |

---

## 7. State coverage

Every component must handle:

| State                         | InventoryTable                 | RowDrawer            | AddDialog                | JobsDrawer           |
| ----------------------------- | ------------------------------ | -------------------- | ------------------------ | -------------------- |
| Loading                       | skeleton rows (3)              | skeleton sections    | submit-disabled, spinner | skeleton list (1)    |
| Error                         | inline banner + retry          | inline error         | inline error             | inline error + retry |
| Empty                         | empty-state CTA                | n/a                  | n/a                      | "No active jobs."    |
| Default                       | rendered rows                  | rendered sections    | empty form               | rendered jobs        |
| In-progress                   | n/a (per-row pill on table)    | n/a                  | submit-pending           | active polling       |
| Stale (specific to inventory) | row tint + yellow refresh time | drawer notes "stale" | n/a                      | n/a                  |

---

## 8. Testing strategy (high level)

Detailed test plan in the writing-plans phase. Coverage targets:

- **Unit (backend, pytest)** — `derive_status` priority resolution; cost-cap fallback to settings default; new `/symbols/inventory` endpoint contract; `is_trailing_only` boundary at month grain.
- **Integration (backend, pytest + real DB)** — `/symbols/inventory` response shape with mixed-readiness symbols; cost-cap server-side enforcement on omitted vs provided `cost_ceiling_usd`.
- **Component (frontend, vitest)** — `<StatusBadge>` variants render correct color+icon+text; `<InventoryTable>` row click opens drawer; `<AddSymbolDialog>` cost-preview branches.
- **E2E (verify-e2e agent)** — UC1 browse inventory; UC2 add symbol with $0 happy path; UC3 refresh stale row; UC4 repair gap; UC5 remove from inventory; UC6 jobs drawer polling discipline (verify backoff + visibility-pause via DevTools network panel during a real run).

E2E uses are graduated to `tests/e2e/use-cases/` post-ship.

---

## 9. Performance + NFRs

Per PRD §8 (Hawk's accepted blockers — implementation hard-requirements):

1. **Polling discipline:** exp backoff 2s→30s, visibility-pause, terminal-stop. **Implementation:** TanStack Query `refetchInterval` callback per §3.3.
2. **Window picker discipline:** 300ms debounce + cancel-in-flight. **Implementation:** `useDebounceValue` (usehooks-ts) + TanStack queryKey-change auto-cancel.
3. **Cost-cap server enforcement:** every `POST /onboard` call gets capped. **Implementation:** §5.3 above.

Render budget: 1s initial paint at 80 rows. Bulk inventory endpoint is the load-bearing constraint — see §5.1 mitigations.

---

## 10. Out of scope (per PRD §2 Non-Goals)

These will be referred-to as deferred in the implementation plan but no code lands in this PR:

- Databento catalog discovery / free-text search → v1.1
- Inline charts on inventory rows → v1.1
- Options coverage (any flavor) → deferred until "what does Add options mean" is scoped
- Spot FX, crypto, non-Databento providers → out of v1
- Strategy↔universe binding UI → `/strategies` page, separate PRD
- Watchlist management UI → `watchlists/*.yaml` stays as backend implementation detail
- Multi-user permissions / read-only consumer mode → single-user product
- Mobile / tablet → desktop-only
- Per-provider attribution column → revisit when Polygon enters for options
- Row-level coverage timeline visual → revisit if the table can't expose gaps clearly
- Real session-aware "stale" semantic → month-grain + 7-day grace ships v1; exchange-calendar logic v1.1 if needed

---

## 11. Open questions for plan phase

These do not block design lock; they are design-phase deferrals that the implementation plan must resolve:

- [ ] Inventory endpoint performance mitigation pick (§5.1 a / b / c)
- [ ] `is_trailing_only` semantics — exact rule for what counts as "trailing only" given the existing 7-day-grace coverage code
- [ ] Cost-cap settings UI surface (env var only for v1, or simple `/settings` field?)
- [ ] Storage stats footer — pull from existing `/api/v1/market-data/status` (already shipped); confirm response shape fits inline display
- [ ] Symbol autocomplete in Add dialog — drives off `instrument_definitions`; needs a tiny `/symbols/autocomplete?q=...` endpoint OR client-side filter on the inventory result (cheaper)
- [ ] Toast library — confirm shadcn `sonner` is already installed vs needs `npx shadcn add sonner`

---

## 12. References

- **PRD:** `docs/prds/universe-page.md` — 10 user stories, council-ratified scope
- **Discussion log:** `docs/prds/universe-page-discussion.md` — full Q1–Q12 + Missing-Evidence trail + Databento billing finding
- **Research brief:** `docs/research/2026-05-01-universe-page.md` — 9 libraries, 6 design-changing findings, 7 open risks
- **Backend foundation (PR #45):** `backend/src/msai/api/symbol_onboarding.py`, `backend/src/msai/services/symbol_onboarding/`
- **Coverage logic:** `backend/src/msai/services/symbol_onboarding/coverage.py` (compute_coverage, \_apply_trailing_edge_tolerance)
- **Existing pages being replaced:** `frontend/src/app/data-management/page.tsx`, `frontend/src/app/market-data/page.tsx`
- **Sidebar:** `frontend/src/components/layout/sidebar.tsx`
- **Council verdict (chairman synthesis):** discussion log § "Council verdict + Missing-Evidence resolutions"
- **Codex second opinion (2026-05-01):** discussion log § "v1 scope reframe"

---

## 13. Next: implementation plan

Invoke `/superpowers:writing-plans` to produce the implementation plan with:

- Dispatch plan (parallel-eligible tasks per `worktree-policy.md`)
- Concrete file paths per task (frontend + backend)
- TDD ordering per task
- Plan-review loop checkpoints

Plan file lands at `docs/plans/2026-05-01-universe-page.md` (separate from this design doc).
