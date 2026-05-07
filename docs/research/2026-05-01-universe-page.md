# Research: Market Data v1 (universe-page)

**Date:** 2026-05-01
**Feature:** Symbol-centric inventory page at `/market-data` — replaces `/data-management` and the existing chart-only `/market-data` route. Surfaces 3-state readiness (registered / backtest_data_available / live_qualified), gates onboard/refresh via cost-cap, exposes Jobs drawer with disciplined polling.
**Researcher:** research-first agent
**PRD:** `docs/prds/universe-page.md` (v1.0, council-ratified 2026-05-01)

---

## Libraries Touched

| Library                      | Our Version       | Latest Stable        | Breaking Changes Since Ours                                                                                                                                                                                                                                       | Source                                                                                                              |
| ---------------------------- | ----------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **shadcn/ui (CLI)**          | `^3.8.5` (devDep) | `4.x` (Mar 2026)     | `shadcn-ui` package renamed to `shadcn` (we're already on the new name); CLI 3.0 (Aug 2025) added MCP server + presets; CLI 4.0 (Mar 2026) added preset commands. No component renames affecting Table/Drawer/DropdownMenu/Dialog/Select/ToggleGroup/AlertDialog. | [shadcn changelog](https://ui.shadcn.com/docs/changelog) (2026-05-01)                                               |
| **Next.js**                  | `15.5.12`         | `15.x` line          | None for this feature — App Router stable since 14.x.                                                                                                                                                                                                             | [TanStack Query Next.js example](https://tanstack.com/query/v5/docs/framework/react/examples/nextjs) (2026-05-01)   |
| **React**                    | `19.1.0`          | `19.x`               | None — TanStack Query v5 supports React 19 since v5.40+.                                                                                                                                                                                                          | [shadcn changelog](https://ui.shadcn.com/docs/changelog) (2026-05-01)                                               |
| **lightweight-charts**       | `^5.1.0`          | `5.x`                | v5.0 (Jan 2025) was a major rewrite (series API, watermark plugin extraction, ESM-only). **We are already on v5**, so no migration needed for the chart-page route move.                                                                                          | [Lightweight Charts v5 release notes](https://github.com/tradingview/lightweight-charts/issues/1791) (2026-05-01)   |
| **TanStack Query**           | NOT INSTALLED     | `5.x` (latest)       | N/A — net-new dep.                                                                                                                                                                                                                                                | [TanStack Query v5 docs](https://tanstack.com/query/v5/docs/framework/react/guides/important-defaults) (2026-05-01) |
| **TanStack Table + Virtual** | NOT INSTALLED     | Table v8, Virtual v3 | N/A — net-new dep (only if 80+ rows force virtualization).                                                                                                                                                                                                        | [TanStack Virtual](https://tanstack.com/virtual/latest) (2026-05-01)                                                |
| **usehooks-ts**              | NOT INSTALLED     | latest               | N/A — net-new dep candidate for `useDebounceCallback`.                                                                                                                                                                                                            | [usehooks-ts useDebounceCallback](https://usehooks-ts.com/react-hook/use-debounce-callback) (2026-05-01)            |
| **FastAPI**                  | `>=0.133.0`       | 0.13x                | None for this feature.                                                                                                                                                                                                                                            | [FastAPI Query Parameter Models](https://fastapi.tiangolo.com/tutorial/query-param-models/) (2026-05-01)            |
| **pydantic-settings**        | `>=2.7.0`         | 2.x                  | None — Decimal env-var coercion stable in v2.                                                                                                                                                                                                                     | [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) (2026-05-01)                 |
| **arq**                      | `>=0.26.0`        | 0.28.x               | None — `_job_id` dedup model unchanged; pessimistic execution since v0.16.                                                                                                                                                                                        | [arq docs](https://arq-docs.helpmanual.io/) (2026-05-01)                                                            |
| **databento (SDK)**          | `>=0.43.0`        | latest               | Not researched (out of scope — `last_refresh_at` derives from local DB columns, not the SDK).                                                                                                                                                                     | N/A                                                                                                                 |

---

## Per-Library Analysis

### 1. shadcn/ui primitives — Table, Drawer, DropdownMenu, Dialog, Select, ToggleGroup, AlertDialog

**Status of project install (project uses CLI ^3.8.5, new-york style, neutral baseColor, RSC enabled, lucide icons):**

I could not list installed primitives directly (the `frontend/src/components` directory is unindexed in this scan). The `components.json` shows `aliases.ui = @/components/ui`, so installed primitives live at `frontend/src/components/ui/*.tsx`. **Implementation must verify before adding** — `ls frontend/src/components/ui/` and only `pnpm dlx shadcn@latest add <name>` for missing ones.

**Install commands (all verified current, new-york style auto-detected from components.json):**

```bash
cd frontend
pnpm dlx shadcn@latest add table          # — flat data table primitive (HTML <table> wrappers)
pnpm dlx shadcn@latest add drawer         # — Vaul-backed bottom/side drawer (Jobs drawer + row drawer)
pnpm dlx shadcn@latest add dropdown-menu  # — kebab menu (Refresh / Remove / View chart)
pnpm dlx shadcn@latest add dialog         # — Add-symbol modal
pnpm dlx shadcn@latest add alert-dialog   # — destructive confirm-remove + Refresh-all-stale confirm
pnpm dlx shadcn@latest add select         # — asset-class filter / window picker
pnpm dlx shadcn@latest add toggle-group   # — alt control for asset-class filter (PRD Open Q)
```

**Critical naming gotcha:** the npm package was renamed from `shadcn-ui` to `shadcn` — we already use the new name (see `frontend/package.json:34`). Older blog posts saying `pnpm dlx shadcn-ui@latest add ...` are stale and will print a deprecation warning. Do not regress.

**Sources:**

1. [shadcn/ui Components index](https://ui.shadcn.com/docs/components) — accessed 2026-05-01
2. [shadcn-ui package deprecation issue](https://github.com/unocss-community/unocss-preset-shadcn/issues/40) — accessed 2026-05-01
3. [shadcn changelog (CLI 3.0 Aug 2025, CLI 4.0 Mar 2026)](https://ui.shadcn.com/docs/changelog) — accessed 2026-05-01

**Design impact:** No design surprises. Drawer is the right primitive for both Jobs drawer (right-side, persistent) and row-detail drawer (right-side, modal-feel). AlertDialog (not Dialog) for destructive actions per shadcn convention. Implementation step 1 should be a single `pnpm dlx shadcn@latest add table drawer dropdown-menu dialog alert-dialog select toggle-group` call after listing what's already in `components/ui/`.

**Test implication:** Spec authors must use `data-testid` and role-based selectors (per `.claude/rules/testing.md`) — not class names from primitive sources, which are stable but still subject to shadcn template churn.

---

### 2. Data-fetching pattern — TanStack Query v5 vs SWR vs native fetch

**Decision recommendation: TanStack Query v5.**

The page has **four data-fetching surfaces with different lifecycles**:

| Surface                                | Pattern needed                                                                                  |
| -------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Inventory table (window-picker driven) | Cancel-in-flight on window change, dedup, manual refetch on mutation success                    |
| Jobs drawer                            | Polling with exponential backoff, hard stop on terminal status, pause on visibilityState=hidden |
| Add-modal dry-run                      | One-shot fetch with debounce + cancel on input change                                           |
| Per-row chart-page hop                 | Static fetch (existing pattern)                                                                 |

This is exactly the workload TanStack Query is built for. SWR is technically capable but lacks first-class support for: (a) per-query mutable polling intervals (needed for backoff), (b) `enabled` flag toggling (needed for hard-stop on terminal status), (c) query-cancellation on key change (needed for window-picker debounce).

**Comparison summary:**

| Axis                           | TanStack Query v5                                            | SWR                            | Native fetch + useState |
| ------------------------------ | ------------------------------------------------------------ | ------------------------------ | ----------------------- |
| Bundle size                    | ~13 kB                                                       | ~4 kB                          | 0                       |
| Polling with mutable interval  | ✅ `refetchInterval` accepts function returning number/false | Limited                        | Hand-rolled             |
| Cancel-in-flight on key change | ✅ via `signal` in queryFn                                   | Partial (SWR cache key change) | Hand-rolled             |
| `enabled` toggle               | ✅ first-class                                               | ✅ via `null` key              | Hand-rolled             |
| visibilityState pause          | ✅ `refetchIntervalInBackground: false` (default)            | ✅ default                     | Hand-rolled             |
| DevTools                       | ✅                                                           | ❌                             | ❌                      |

**Sources:**

1. [SWR vs TanStack Query 2026 (DEV)](https://dev.to/jake_kim_bd3065a6816799db/swr-vs-tanstack-query-2026-which-react-data-fetching-library-should-you-choose-342c) — accessed 2026-05-01
2. [TanStack Query Next.js 15 example (App Router)](https://tanstack.com/query/v5/docs/framework/react/examples/nextjs) — accessed 2026-05-01
3. [TanStack Query Important Defaults](https://tanstack.com/query/v5/docs/framework/react/guides/important-defaults) — accessed 2026-05-01

**Design impact:** Add `@tanstack/react-query` as a frontend dep. Wrap `frontend/src/app/layout.tsx` (or a client `Providers` component, since QueryClientProvider needs a client component) with `<QueryClientProvider>`. Use `useQuery` for inventory + jobs drawer; use `useMutation` for onboard/refresh/remove with `onSuccess: () => queryClient.invalidateQueries(['inventory'])` for optimistic post-mutation refresh. Cancel-on-key-change is automatic when the queryKey includes the window (`['inventory', start, end, asset_class]`).

**Test implication:** E2E tests must wait for TanStack Query's loading→idle transition before asserting (use `waitFor` not arbitrary timeouts). DevTools should be disabled in production builds (Pablo's solo-user workflow may want them in dev — opt in via env flag).

---

### 3. Polling discipline — exponential backoff, visibility pause, terminal-status stop

**TanStack Query v5 covers all three NFRs natively.** The PRD §8.1 requirements map cleanly:

```typescript
const lastSeenStatusRef = useRef<string>("");
const intervalRef = useRef<number>(2000); // start at 2s

useQuery({
  queryKey: ["onboard-status", runId],
  queryFn: ({ signal }) => fetchStatus(runId, { signal }),
  enabled: !!runId,
  refetchInterval: (query) => {
    const data = query.state.data;
    if (!data) return 2000; // initial poll cadence
    // Hard stop on terminal status (PRD requirement #1)
    if (
      ["completed", "completed_with_failures", "failed"].includes(data.status)
    ) {
      return false;
    }
    // Exponential backoff on no state change (PRD requirement #1)
    const stateChanged = JSON.stringify(data) !== lastSeenStatusRef.current;
    lastSeenStatusRef.current = JSON.stringify(data);
    if (stateChanged) {
      intervalRef.current = 2000; // reset on change
    } else {
      intervalRef.current = Math.min(intervalRef.current * 1.5, 30_000); // cap at 30s
    }
    return intervalRef.current;
  },
  refetchIntervalInBackground: false, // pause on visibilityState=hidden (PRD requirement #1)
});
```

**Known gotcha (verified, accept the risk):** [TanStack issue #8353](https://github.com/TanStack/query/issues/8353) reports that **retry** logic on failed background fetches pauses regardless of `refetchIntervalInBackground`. Our polling case isn't a retry-on-failure case (we want to silently skip ticks while hidden), so the bug doesn't bite — but our error UI should tolerate "checking..." gracefully (PRD US-008 edge case "Polling fails transiently").

**Sources:**

1. [TanStack Query useQuery API ref (refetchInterval signature)](https://tanstack.com/query/v5/docs/framework/react/reference/useQuery) — accessed 2026-05-01
2. [TanStack issue #8353 — refetchIntervalInBackground retry quirk](https://github.com/TanStack/query/issues/8353) — accessed 2026-05-01
3. [TanStack Auto-Refetching example](https://tanstack.com/query/v5/docs/framework/react/examples/auto-refetching) — accessed 2026-05-01

**Design impact:** No custom polling hook needed. Implementation lives in a single `useOnboardStatus(runId)` hook that wraps `useQuery` with the backoff logic above. PRD §8.1 is satisfied without a custom hook tree.

**Test implication:** Polling discipline is testable in two layers — (a) unit-test the backoff/terminal-stop logic by mocking `fetchStatus` and asserting `refetchInterval` callback returns the right values; (b) E2E-verify by submitting a slow job, switching tabs, and confirming the network panel shows zero requests during hidden state. Don't rely on visual-only verification — instrument via `getQueriesData` in dev.

---

### 4. Window-picker debounce + cancel-in-flight

**Decision recommendation: TanStack Query's queryKey-based cancellation, NOT a separate AbortController layer.**

When the user changes the window picker, the queryKey `['inventory', start, end, asset_class]` changes. TanStack Query automatically:

- Cancels the in-flight `queryFn` (it forwards an `AbortSignal` if `queryFn` accepts `{ signal }`)
- Discards the in-flight response
- Starts a new fetch under the new key

The 300ms **debounce** is separate — it should debounce the _queryKey input_, not the fetch itself. Pattern:

```typescript
import { useDebounceValue } from 'usehooks-ts'

const [pendingStart, setPendingStart] = useState<Date>(...)
const [debouncedStart] = useDebounceValue(pendingStart, 300)

useQuery({
  queryKey: ['inventory', debouncedStart, debouncedEnd, assetClass],
  queryFn: ({ signal }) => fetchInventory({ start, end, asset_class }, { signal }),
})
```

**Add `usehooks-ts` as a dep** (~1.5 kB, MIT-licensed, actively maintained, used by the shadcn ecosystem). Alternative: `use-debounce` (similar size, equally well-maintained). Hand-rolling is a maintenance tax for ~15 lines of code — not worth it.

**Sources:**

1. [usehooks-ts useDebounceValue](https://usehooks-ts.com/react-hook/use-debounce-value) — accessed 2026-05-01
2. [Debouncing API Calls with useEffect + AbortController](https://medium.com/@vinaykumarbr07/debouncing-api-calls-in-react-with-useeffect-and-abortcontroller-d60a16716c7f) — accessed 2026-05-01

**Design impact:** Implementation pattern locked: debounce the queryKey input, let TanStack Query handle cancellation via signal. The fetcher in `frontend/src/lib/api.ts` (if not already) must accept `{ signal }: { signal?: AbortSignal }` and pass it through to `fetch`.

**Test implication:** E2E should rapidly change the window picker (3+ changes in <300ms) and assert (a) only one network request fires, (b) it carries the final window's params. Visual flicker check: table should not flash empty between key changes (TanStack's `placeholderData: keepPreviousData` is the right knob).

---

### 5. Table virtualization for 80+ rows

**Decision recommendation: Skip virtualization in v1. Re-evaluate at >150 rows.**

Pablo's expected inventory is 30–80 symbols per the PRD §2 success metric ("Page remains usable when 80 symbols are listed"). Modern browsers render 80–150 DOM rows of static-content table cells well under 16ms — the 60fps target is met without virtualization. TanStack Virtual is the right tool but the headless integration with shadcn's Table primitive is non-trivial (custom row-rendering, manual scroll container, sticky-header coordination). Adding it now is YAGNI; defer until row counts exceed ~150.

**Trigger to revisit:** if the row count exceeds 150 OR if a row gains heavy children (sparkline charts, multi-line cells), implement TanStack Table + TanStack Virtual per the canonical pattern: row-virtualizer driven by `useVirtualizer({ count, estimateSize, getScrollElement })`, fixed-height rows, sticky `<thead>`.

**Sources:**

1. [TanStack Virtual (intro)](https://tanstack.com/virtual/latest) — accessed 2026-05-01
2. [TanStack Table virtualization guide](https://tanstack.com/table/v8/docs/guide/virtualization) — accessed 2026-05-01
3. [Building a Performant Virtualized Table with TanStack Table + Virtual (Mar 2026)](https://medium.com/@ashwinrishipj/building-a-high-performance-virtualized-table-with-tanstack-react-table-ced0bffb79b5) — accessed 2026-05-01

**Design impact:** Plan should explicitly state "no virtualization in v1" and link to this brief. Avoid adopting TanStack Table prematurely — shadcn's plain `<Table>` primitive plus a `useMemo`-d filtered/sorted array is sufficient at the row count the PRD targets.

**Test implication:** PRD §2 metric "scroll smooth at 60fps" should be verified by Devtools Performance profile during E2E (manual or CI-Lighthouse audit). If profile flags long tasks, escalate to virtualization in a follow-up — not v1.

---

### 6. TradingView Lightweight Charts (route move only)

**Status:** Already on `^5.1.0` — major v5 migration done. The PRD §6.5 only moves `frontend/src/app/market-data/page.tsx` → `frontend/src/app/market-data/chart/page.tsx`; no API changes needed. Verify by `grep -r "createChart\|addSeries\|addCandlestickSeries" frontend/src/app/market-data/` to confirm v5 idioms in place.

**Sources:**

1. [Lightweight Charts v5 migration guide (Issue #1791)](https://github.com/tradingview/lightweight-charts/issues/1791) — accessed 2026-05-01
2. [Lightweight Charts release notes](https://tradingview.github.io/lightweight-charts/docs/release-notes) — accessed 2026-05-01

**Design impact:** None — pure route move. Update any internal links from `/market-data?symbol=X` to `/market-data/chart?symbol=X`. Sidebar link in `frontend/src/components/layout/sidebar.tsx` per PRD §6.4–6.5 takes the new `/market-data` slot.

**Test implication:** Standard route-move smoke (chart page renders at new path; old `/market-data` now serves the inventory). No deeper chart testing needed.

---

### 7. Backend `GET /api/v1/symbols/inventory` endpoint shape

**Project-local conventions take precedence over external best practice.** The closest siblings are:

- `GET /api/v1/symbols/readiness` (single-instrument, file: `backend/src/msai/api/symbol_onboarding.py:557`) — returns `ReadinessResponse` (one record).
- `/api/v1/strategies/`, `/api/v1/backtests/history`, `/api/v1/live/status` — list endpoints in this project return bare `list[T]` JSON, NOT a `{items, total, page, page_size}` envelope. (Confirmed by inspecting the API map in `CLAUDE.md`.)

**Recommendation:** match the project pattern — return `list[InventoryRow]` (bare JSON array), no pagination envelope. At Pablo's scale (≤80 symbols) pagination is YAGNI; if it becomes needed, add a response-envelope variant later behind a query param. Inline filter via existing `asset_class` query param (not a Pydantic query-model wrapper — single param doesn't justify the wrapper).

**Schema sketch:**

```python
class InventoryRow(BaseModel):
    symbol: str
    asset_class: AssetClass               # equity | futures | fx | option
    registered: bool
    provider: str
    backtest_data_available: bool | None  # null when start/end omitted
    coverage_status: Literal["full", "gapped", "none"] | None
    covered_range: str | None             # "2020-01-01..2025-04-30"
    missing_ranges: list[dict[str, Any]] = []
    live_qualified: bool
    last_refresh_at: datetime | None      # see §10 below for derivation
```

Reuse the `ReadinessResponse` field set verbatim where possible — divergence between single-instrument and bulk shape is a UX bug waiting to happen.

**Sources:**

1. [FastAPI Query Parameter Models](https://fastapi.tiangolo.com/tutorial/query-param-models/) — accessed 2026-05-01
2. [FastAPI pagination patterns (2026)](https://oneuptime.com/blog/post/2026-02-02-fastapi-pagination/view) — accessed 2026-05-01
3. Project-local: `backend/src/msai/api/symbol_onboarding.py:557` (`/readiness` shape)

**Design impact:** No envelope. Endpoint signature: `GET /api/v1/symbols/inventory?start=YYYY-MM-DD&end=YYYY-MM-DD&asset_class=equity|futures|fx`. Reuse `compute_coverage` (already shipped) per row in a `gather()` over rows from `instrument_definitions ⨝ instrument_aliases`.

**Test implication:** Coverage tests must include (a) start/end omitted → `backtest_data_available = null` for every row (matches single-readiness contract), (b) filter by asset_class returns proper subset, (c) empty inventory → empty array (not 404).

**Open risk:** N+1 query risk if every row triggers a separate Parquet scan via `compute_coverage`. Plan must include either `asyncio.gather` over the per-row coverage calls, or batch the Parquet reads into a single DuckDB scan. **Test for sub-1s render at 80 rows** (PRD §2 success metric).

---

### 8. pydantic-settings — Decimal env-var coercion + AliasChoices pattern

**Project-local pattern:** `backend/src/msai/core/config.py` already uses `Field(default=..., validation_alias=AliasChoices("PRIMARY", "LEGACY"))` extensively (lines 86–128). New setting MUST follow this convention.

**Pydantic-settings v2 Decimal coercion is automatic** — strings from env vars are passed to Decimal's constructor: `Decimal("50.00")` works because `Decimal.__init__` accepts strings. No custom validator required. Strict mode would block this; we are NOT in strict mode for settings (verified — no `model_config = SettingsConfigDict(strict=True)` in current `Settings`).

**Recommended addition:**

```python
from decimal import Decimal
from pydantic import AliasChoices, Field

class Settings(BaseSettings):
    # ... existing fields ...

    # Cost-cap default for symbol onboarding (PRD US-010).
    # Per-request `cost_ceiling_usd` overrides this; server-side
    # enforcement is mandatory (see PRD §8.3).
    symbol_onboarding_default_cost_ceiling_usd: Decimal = Field(
        default=Decimal("50.00"),
        ge=Decimal("0"),
        validation_alias=AliasChoices(
            "MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD",
            "SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD",
        ),
        description="Default per-onboard cost cap (USD). Per-request value wins.",
    )
```

The `MSAI_*` prefix follows the existing `MSAI_API_KEY` precedent. Both names accepted via `AliasChoices` — same pattern as `IB_HOST`/`IB_GATEWAY_HOST`.

**Sources:**

1. [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — accessed 2026-05-01
2. [Pydantic standard library types (Decimal)](https://docs.pydantic.dev/latest/api/standard_library_types/) — accessed 2026-05-01
3. Project-local: `backend/src/msai/core/config.py:86-128` (existing AliasChoices pattern)

**Design impact:** No design surprises. The plan should add the field to `Settings` and update `POST /api/v1/symbols/onboard` (in `backend/src/msai/api/symbol_onboarding.py:325-383`) to fall back to `settings.symbol_onboarding_default_cost_ceiling_usd` when `request.cost_ceiling_usd is None`. **Important second-order change:** the existing handler at line 343 `if request.cost_ceiling_usd is not None:` only computes/enforces the cap when the request explicitly carries one. After this change it must compute the cap on **every** request so the default-fallback bites the CLI/X-API-Key path too (PRD §8.3 + US-010 server-side enforcement requirement).

**Test implication:** New unit tests: (a) request omits ceiling → settings default applied, (b) request carries ceiling → request value wins, (c) settings default ≥ estimated cost → 202, (d) settings default < estimated cost → 422 COST_CEILING_EXCEEDED, (e) env var `MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD=100.00` overrides the $50 default.

---

### 9. arq job dedup + replay safety

**Project-local pattern:** `backend/src/msai/api/symbol_onboarding.py:178-200` (`_dedup_job_id`) builds a deterministic blake2b digest of `(watchlist_name, request_live_qualification, cost_ceiling_usd, sorted symbol|asset_class|start|end)` and uses it as the arq `_job_id`. The `_enqueue_and_persist_run` helper (lines 203-317) is the proven flow: SELECT-FOR-UPDATE on digest → enqueue → commit row → 200 if duplicate / 202 if new / 503 if queue dead / 409 on race.

**arq guarantees:** [arq docs](https://arq-docs.helpmanual.io/) confirm that passing the same `_job_id` to `enqueue_job` returns `None` if a job with that id is already queued/running. The existing handler treats this as the duplicate case and re-SELECTs after a 100ms sleep.

**Rapid-click scenario from inventory page (US-003 refresh, US-007 bulk-refresh):** because the digest hashes the symbol list + window, two clicks within the same arq retention window (~24h default) produce the **same** digest → second click returns 200 with the existing `run_id`. The frontend should display "already in progress" inline rather than a confusing toast. The existing 200 path is the right return code for idempotent duplicates per `.claude/rules/api-design.md`.

**Sources:**

1. [arq docs (pessimistic execution + dedup)](https://arq-docs.helpmanual.io/) — accessed 2026-05-01
2. Project-local: `backend/src/msai/api/symbol_onboarding.py:178-317` (existing dedup flow)
3. [arq GitHub](https://github.com/python-arq/arq) — accessed 2026-05-01

**Design impact:** None new — reuse the existing `_enqueue_and_persist_run` helper for refresh/repair from the inventory page. **Minor:** US-007 bulk-refresh-all-stale should NOT batch all stale symbols into one arq job (each symbol's window may differ); instead enqueue N independent onboard runs, each idempotently keyed. The Jobs drawer aggregates the N progress streams.

**Test implication:** E2E: rapid double-click on Refresh — assert (a) only one arq job runs, (b) UI shows the same run_id both times, (c) no race condition surfaces (the existing 100ms-sleep + re-SELECT path covers this). This is already exercised by `tests/integration/api/test_symbol_onboarding_dedup.py` (verify it exists and extend if not).

---

### 10. `last_refresh_at` derivation (no new Databento integration)

**Source columns to choose from:**

- `symbol_onboarding_runs.completed_at` — populated when an onboard/refresh job finishes. Most accurate for "when did Pablo last pull data."
- `instrument_definitions.updated_at` — refreshed on registry mutations (alias windowing changes, IB qualification refresh). NOT data-pull semantics; do not use.
- Parquet file mtime via `compute_coverage` — could be derived but `last_refresh_at` is meant to be a JSON-serializable scalar, not a per-file scan result.

**Recommendation:** join `instrument_definitions` to the most recent successful `symbol_onboarding_runs` row whose `symbol_states[<symbol>].status == 'succeeded'`. SQL-side this is a lateral join or a window function. Fallback to `null` when no successful run has touched this symbol (e.g., row hand-registered via CLI before onboarding service existed).

**Sources:** Project-local — `backend/src/msai/models/symbol_onboarding_run.py` and `backend/src/msai/services/symbol_onboarding/coverage.py`. No external research applicable.

**Design impact:** Plan must specify the join. Pre-computation (denormalize `last_refresh_at` onto `instrument_definitions` via a trigger/event) is YAGNI at <80 rows; query-time derivation is fine.

**Test implication:** Inventory test fixtures must seed both an instrument **with** a completed onboard (asserts non-null `last_refresh_at`) and **without** one (asserts null) to exercise the join's left-outer-join branch.

---

## Not Researched (with justification)

- **Databento search/symbology APIs** — explicitly deferred to v1.1 per PRD §2 Non-Goals (Codex flagged as separate design pass).
- **Spot FX, crypto, options chain APIs** — explicitly out of scope per PRD §2.
- **Polygon SDK** — Polygon is the equities provider but the inventory page shows existing readiness; no new Polygon calls.
- **MSAL / Azure Entra ID** — already wired; this feature inherits the existing `get_current_user` dependency.
- **PyJWT / cryptography** — same as MSAL; auth is already wired.
- **NautilusTrader** — feature does not touch backtest/live engines.
- **DuckDB / pyarrow** — used by `compute_coverage` (already shipped); no new patterns introduced.
- **Recharts** — not used on this page; chart rendering uses lightweight-charts on the deep-link target only.
- **TradingView Lightweight Charts deep usage** — only a route move; no API touchpoints.
- **ib_async / nautilus_trader[ib]** — no IB calls from this page; live_qualified column is already populated by `instruments refresh` CLI which runs separately.
- **Tailwind v4 / tw-animate-css** — already configured at the project level; no new directives.
- **Playwright** — already scaffolded; this feature adds use cases under `tests/e2e/use-cases/data/` rather than new framework decisions.

---

## Open Risks

1. **N+1 query risk on inventory endpoint.** `compute_coverage` per row (Parquet scan via DuckDB) at 80 rows could blow PRD §2's 1s render budget. Mitigation: design phase must specify either (a) `asyncio.gather` parallelism, (b) batched DuckDB scan in one query, or (c) cached `(symbol, window)` coverage results with sub-second TTL. Spike a 50-symbol benchmark before the design freezes.
2. **TanStack Query first-time integration in this project.** The frontend is currently fetch-based (per `frontend/src/lib/api.ts` typed client style). Adding TanStack Query requires a `Providers` client component wrapping `app/layout.tsx` — minor change, but verify React 19's `<QueryClientProvider>` SSR behavior in App Router (TanStack docs cover this; `dehydrate`/`HydrationBoundary` is the canonical pattern). Don't ship without verifying SSR doesn't hydrate-mismatch on initial page render.
3. **Cost-cap server-side default fallback.** PR #45's onboard handler only validates the cap when the request carries one (line 343 condition). The PRD §8.3 hard-requirement says CLI / X-API-Key requests must also receive cap protection. The plan must drop the `if request.cost_ceiling_usd is not None:` guard — every request computes/enforces against the effective cap (request value OR settings default). This is a behavior change to PR #45's public contract; document it in the changelog entry.
4. **TanStack Query polling retry quirk on hidden tabs** (issue #8353). Not blocking — our use case isn't retry-driven — but plan should explicitly call out "don't rely on retry-on-failure semantics during background polling; surface failures only when the tab is foregrounded again." Adds a small UX nuance: the "checking..." indicator should remain (not turn into "error") during hidden state.
5. **shadcn `components/ui/` audit not performed.** Could not list directory contents during research scan. Implementation Step 1 must run `ls frontend/src/components/ui/` and only invoke `shadcn add` for missing primitives (avoid clobbering customized files).
6. **`last_refresh_at` semantics ambiguity.** If a single row's most-recent successful onboard covered only a sub-range of the displayed window, `last_refresh_at` could read "fresh" while `coverage_status` reads "gapped" — confusing. Plan must define the precise semantic: is `last_refresh_at` "wall-clock of last successful pull touching this symbol" (recommended, simpler) or "wall-clock of last pull whose window subsumed the active picker's window" (more accurate but requires knowing the active picker)? Default to the simpler semantic; the Stale flag already encodes coverage health.
7. **Soft-delete semantics for US-005** (Should-have). Plan-review must confirm whether `instrument_aliases` already has a soft-delete column (`is_active` or `effective_until`); if not, this becomes a schema migration with non-trivial scope and US-005 should drop to v1.1 per PRD Open Question 4.

---

## Summary

- **Libraries researched:** 9 (shadcn/ui, Next.js, lightweight-charts, TanStack Query, TanStack Table+Virtual, usehooks-ts, FastAPI, pydantic-settings, arq).
- **Net-new dependencies recommended:** `@tanstack/react-query` (~13 kB), `usehooks-ts` (~1.5 kB). No backend additions.
- **Design-changing findings:** 6 — TanStack Query covers all polling NFRs natively (no custom hook); skip table virtualization in v1 (revisit at >150 rows); reuse project's bare-array list response (no pagination envelope); add `validation_alias=AliasChoices(...)` per existing config pattern; drop the `if cost_ceiling is not None` guard so server-side default protects CLI/API-key callers; debounce queryKey input + let TanStack Query handle cancellation (no custom AbortController).
- **Open risks:** 7 — most important is the N+1 coverage-scan risk on the inventory endpoint (must be benchmarked before design freezes).
