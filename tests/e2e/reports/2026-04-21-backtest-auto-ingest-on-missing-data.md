
---

## UC-BAI-005 — Re-verified after initial SKIP — PASS

**Why re-run:** Playwright MCP was available in the session; the original SKIP was due to agent toolbox, not product limitation. Re-executed with Playwright MCP driving the browser directly.

**Method:** Paused the `ingest-worker` container (`docker compose pause ingest-worker`) to hold a submitted backtest in `phase=awaiting_data` long enough for browser observation. Submitted backtest `2d61831d-7ed0-43c8-bdac-28ba386b72fa` (ES.n.0 / 2024-11-01→2024-11-30).

**Verify — all 6 assertions GREEN:**

| Assertion | Result |
|---|---|
| `data-testid="backtest-phase-indicator"` visible on detail page during awaiting_data | ✅ |
| `data-testid="backtest-phase-message"` text = "Downloading futures data for ES.n.0.XCME" | ✅ |
| List page `data-testid="backtest-list-fetching-badge"` visible on the running row | ✅ with text "Fetching data…" |
| Running-row "View details" link is clickable (iter-1 P1-e fix) | ✅ `<a href="/backtests/2d61831d-...">` present |
| Reload preserves indicator state | ✅ indicator + message still visible after `page.goto` |
| Terminal transition clears indicator | ✅ after unpausing ingest-worker, indicator disappeared; row transitioned to "failed" (ENGINE_CRASH per design — Databento data unavailable for ES.n.0.XCME, which is an env limitation not a product defect) |

**Evidence (captured via `browser_evaluate`):**

```js
{
  indicatorVisible: true,
  indicatorHTML: '<div data-testid="backtest-phase-indicator" class="mt-1 flex items-center gap-2 text-sm text-muted-foreground">
    <svg class="lucide lucide-loader-circle h-3 w-3 animate-spin" ...></svg>
    <span data-testid="backtest-phase-message">Downloading futures data for ES.n.0.XCME</span>
  </div>',
  listBadgeText: "Fetching data…",
  listBadgeCount: 1,
  runningRowHasDetailLink: true
}
```

After reload:
```js
{ afterReloadIndicatorVisible: true, afterReloadMessageText: "Downloading futures data for ES.n.0.XCME" }
```

After unpausing ingest-worker (12 × 1.5s polls):
```js
indicatorVisible: false (all 12 ticks), mainHead: "Backtest 2d61831d | failed | ..."
```

---

## Updated verdict (supersedes initial PARTIAL)

**Verdict: PASS 3/5 + PARTIAL 1 + SKIPPED 1**

- UC-BAI-002 (guardrail): PASS
- UC-BAI-003 (asset_class routing): PARTIAL — core routing verified; full envelope path blocked by Databento entitlement (env, not product)
- UC-BAI-004 (dedupe): PASS
- UC-BAI-005 (UI): PASS (re-run with Playwright MCP)
- UC-BAI-001 (happy-path cold-stock): SKIPPED_FAIL_INFRA — registry + Polygon API key unavailable in dev env

**No FAIL_BUG.** All product-behaviors that could be exercised (given env constraints) are GREEN. The two non-PASS outcomes are environmental (Databento entitlement + missing Polygon key + sparse registry) — same infra limitations that would affect the live-trading drill.
