# Bulk ingest: requested window is complete through its final (inclusive) day

> Graduated 2026-06-06 from `docs/plans/2026-06-06-bulk-ingest-end-exclusive.md` after verify-e2e PASS
> (reports: `tests/e2e/reports/2026-06-06-17-30-bulk-ingest-end-exclusive.md` — UC-IEX-001 + SPY heal;
> `tests/e2e/reports/2026-06-06-22-07-bulk-ingest-end-exclusive-uc-iex-002.md` — UC-IEX-002).
> Bug pinned: every bulk-ingest entrypoint forwarded the operator's inclusive `end` verbatim to
> Databento's exclusive `get_range`, so every onboarded window permanently missed its final day and
> reported `gapped`/`backtest_data_available=false` (coverage judges the closed `[start, end]`).
> Fixed by translating at `DataIngestionService._fetch_bars` (+1d, Databento branch only).

**Surface coverage decision:** API — Covered (UC-IEX-001). CLI — Covered (UC-IEX-002). UI — N/A: the
fix changes no UI behavior; the inventory/coverage UI renders the same coverage data the API serves
(same internal data path — duplicating the assertion through a second interface is not E2E per
testing.md). UI rendering of coverage stays covered by uc-cdp-ui-001/002.

## UC-IEX-001 — Onboarded window is complete through its final day (API) `@smoke`

```
Actor:         Operator onboarding a new symbol via the HTTP API for backtesting
Scenario:      They need a specific historical window (ending on a known trading day)
               available for a backtest. Pre-fix, the readiness report permanently
               showed the final day missing, blocking the backtest gate.
Interface:     API
Intent:        The operator onboards a symbol for an exact date window and sees the
               whole window — including the final day — reported as available.
Setup:         Stack up; auth via X-API-Key. Pick a symbol NOT in inventory
               (GET /api/v1/symbols/inventory first) and a FRESH watchlist name
               (the idempotency digest is per watchlist+window — a reused name
               replays a historical run, which may carry pre-fix failure state).
               Window must END on a trading day. Do NOT pre-ingest the symbol.
Steps:         1) POST /api/v1/symbols/onboard {watchlist, [{symbol, equity, start, end}]}
               2) Poll GET /api/v1/symbols/onboard/{run_id}/status to terminal `completed`
               3) GET /api/v1/symbols/readiness?symbol=X&asset_class=equity&start=<start>&end=<end>
Verification:  Readiness response includes coverage_status="full",
               backtest_data_available=true, missing_ranges=[], and covered_range
               whose right edge is the FINAL TRADING DAY of the requested window
               (pre-fix: gapped with missing_ranges=[{end-day, end-day}]). The
               operator can proceed to run a backtest over the full window.
Persistence:   Re-request the same readiness call after a delay — still full; the
               inventory endpoint lists the symbol for the window.
```

2026-06-06 PASS evidence: IWM, watchlist `iex001-iwm-jun2024`, window 2024-06-03 → 2024-06-28 →
`full`, `covered_range "2024-06-03 → 2024-06-28"`, `missing_ranges=[]`.

## UC-IEX-002 — Shell ingest covers the requested range through the end date (CLI)

```
Actor:         Operator running the msai CLI on the host to pull data for research
Scenario:      They ingest a specific symbol/date range from the shell before a
               research session. Pre-fix the final day silently never arrived.
Interface:     CLI
Intent:        The operator ingests a date range from the shell and confirms, from
               the shell, that data through the end date landed.
Setup:         Stack up; CLI via the documented container pattern
               (`docker compose ... exec -T backend /app/.venv/bin/python -m msai.cli ...`
               — NOT bare `python`, which is the system interpreter) or host
               `cd backend && uv run msai ...` with the stack env. Pick a
               symbol+window slice not yet on disk. End on a trading day.
Steps:         1) Run `msai ingest stocks <SYM> <start> <end>`
               2) Verify from the shell: readiness for the same window (curl)
Verification:  Ingest stdout reports bars written with exit 0 and a last_timestamp
               ON the end date; the status payload echoes the OPERATOR window
               (start/end as requested — never the provider-translated end); the
               follow-up readiness invocation returns coverage_status="full" with
               missing_ranges=[] for the requested window INCLUDING the end date.
Persistence:   A fresh shell invocation of the readiness check returns the same
               "full" answer (data on disk, not session state).
```

2026-06-06 PASS evidence: IWM 2024-07-01 → 2024-07-31 (pre-state `none`), 10,668 bars,
`last_timestamp 2024-07-31T23:54Z`, readiness `full`, `covered_range "2024-07-01 → 2024-07-31"`.
Bonus: `msai ingest --help` shows `End date YYYY-MM-DD (inclusive)`.
