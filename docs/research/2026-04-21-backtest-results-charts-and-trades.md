# Research: Backtest Results — Charts & Trade Log

**Date:** 2026-04-21
**Feature:** Persist canonical daily-normalized `Backtest.series` JSONB + paginated `/trades` + authenticated Next.js iframe proxy + wire 4 scaffolded React components to real data
**Researcher:** research-first agent (worktree: `.worktrees/backtest-results-charts-and-trades`)

**Brief file:** this document at `docs/research/2026-04-21-backtest-results-charts-and-trades.md`.

---

## Libraries Touched

| Library                      | Our Version   | Latest Stable   | Breaking Changes since ours                                | Primary Source                                                                        |
| ---------------------------- | ------------- | --------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| **QuantStats**               | 0.0.81        | 0.0.81          | None (we're on latest)                                     | https://github.com/ranaroussi/quantstats/releases (2026-04-21)                        |
| **Next.js**                  | 15.5.12       | 16.2.4          | GET default went from static→dynamic in 15 RC; `context.params` is a Promise in 15+ — already on 15, so N/A | https://nextjs.org/docs/app/api-reference/file-conventions/route (2026-04-21)         |
| **React**                    | 19.1.0        | 19.1.x          | None pertinent                                             | Via Next.js 15                                                                        |
| **Recharts**                 | 3.7.0         | 3.8.1           | v3.0 removed `recharts-scale`, removed `activeIndex` prop; v3.7 deprecated `Cell` (still usable); v3.8 adds TS generics + throttling + `niceTicks` | https://github.com/recharts/recharts/releases (2026-04-21)                            |
| **lightweight-charts**       | 5.1.0         | 5.1.x           | None relevant to heatmap scope (library doesn't do heatmaps anyway) | Already used in `candlestick-chart.tsx`                                                |
| **PostgreSQL**               | 16            | 16              | N/A                                                        | https://www.postgresql.org/docs/16/storage-toast.html (2026-04-21)                    |
| **FastAPI**                  | 0.133.1       | 0.133.x         | None                                                       | `backend/uv.lock`                                                                     |
| **Pydantic**                 | 2.12.5        | 2.12.x          | None (V2 idioms stable)                                    | `backend/uv.lock`                                                                     |
| **Alembic**                  | 1.18.4        | 1.18.x          | None                                                       | `backend/uv.lock`                                                                     |
| **SQLAlchemy**               | 2.0.47        | 2.0.x           | None                                                       | `backend/uv.lock`                                                                     |
| **NautilusTrader**           | 1.223.0       | 1.223.x         | None since installation                                    | `backend/uv.lock`                                                                     |
| **pandas**                   | 2.3.3         | 2.3.x           | None (pandas 2.x compat already resolved in QS 0.0.78)     | `backend/uv.lock`                                                                     |

Note on `fastapi-pagination`: not installed; not used. The project uses hand-rolled `{items, total, ...}` shape consistently (`StrategyListResponse`, `BacktestHistoryResponse`, `PortfolioListResponse`, etc.). See target #5.

---

## Per-Library Analysis

### 1. QuantStats (0.0.81, pypi: `quantstats`)

**Versions:** ours=0.0.81, latest=0.0.81 (January 13, 2026 — the 0.0.78/0.0.81 combined modernization push).

**Breaking changes since ours:** None — we're on the head.

**Key findings:**

- `qs.reports.html(returns, benchmark=..., title=..., output=path, download_filename=...)` is the canonical public API. Signature unchanged in 0.0.78/0.0.81. `output=path` writes HTML to disk and returns `None`; we already work around this in `report_generator.generate_tearsheet` by writing to a temp file and reading back.
- The `benchmark=` kwarg accepts a pandas Series of returns (same normalization needed as strategy returns). This means **future-PR benchmark overlay** (PRD non-goal #2) is natively supported — the persisted `series` payload just needs a parallel `benchmark_returns` field later.
- 0.0.78 release notes explicitly call out pandas 2.x compatibility (removed deprecated `fill_method` kwarg). We're on pandas 2.3.3 — safe.
- 0.0.78 added a Monte Carlo simulation module, type hints throughout, Chrome dark-mode plot fixes. None of these alter the HTML tear sheet output format we embed.
- **No breaking changes reported** to `qs.reports.html()` signature, `benchmark=` kwarg, or HTML structure since 0.0.77. The 0.0.81 patch specifically "restored `reports.html()` functionality without requiring output file specification" — a bugfix, not a format change.
- A parallel fork `quantstats-lumi` (1.1.3, maintained by Lumiwealth) exists but is out of scope — we're pinned to canonical `ranaroussi/quantstats`.
- PRD open question "HTML structure stability across minor versions" is **low risk** — the only cross-version drift between 0.0.77 (where many MSAI tests were likely written) and 0.0.81 is bugfixes + Chrome dark-mode plot colors, not the embedded HTML structure. `generate_tearsheet` wraps in a try/except fallback, which already handles format drift gracefully.

**Sources:**

1. https://github.com/ranaroussi/quantstats/releases — accessed 2026-04-21
2. https://pypi.org/project/quantstats/ — accessed 2026-04-21
3. `backend/src/msai/services/report_generator.py` (lines 113–125) — our current invocation

**Design impact:** **No impact on the canonical `qs.reports.html()` invocation.** The decision doc's `parity contract` table is already correct: QuantStats HTML is a snapshot, the DB is ground truth. **But one concrete design nudge**: since QS 0.0.78 accepts `benchmark=` cleanly and is stable, shape the new `Backtest.series` payload so `benchmark_returns` can be added as a sibling key in a future PR without schema migration (pre-bake the structure `{daily: [...], monthly_returns: [...]}` with room for `{..., benchmark_daily: [...]}`).

**Test implication:** Add one regression test that generates a QS HTML report against a 250-day synthetic returns series, asserts the generated HTML contains **specific structural anchors** (e.g., the string `"Strategy Visualization"` or a known `<div class="title">` marker) — cheaply catches future QS format drift. Do NOT pin content exactness; pin structural shape only.

---

### 2. Next.js 15 App Router Route Handlers (15.5.12; latest 16.2.4)

**Versions:** ours=15.5.12, latest=16.2.4.

**Breaking changes since ours:** None that touch the iframe-proxy pattern. Next.js 16 was released during/after our 15.5.12 pin and mostly refines caching ("Cache Components"), Suspense streaming, and the `use cache` directive — not Route Handler auth or streaming primitives. `context.params` promise + GET-default-dynamic landed in 15.0 (pre-our-pin).

**Key findings (combining the Next docs and Streaming guide):**

**(a) Reading the incoming request's cookie/session server-side.** Two supported paths in Next 15:

```ts
// Path 1: next/headers
import { cookies } from 'next/headers';
const cookieStore = await cookies();
const token = cookieStore.get('entra_session_token');

// Path 2: NextRequest directly
export async function GET(request: NextRequest) {
  const token = request.cookies.get('entra_session_token');
  const authHeader = request.headers.get('authorization');
}
```

Because the project currently mints auth via MSAL in the browser and sends `Authorization: Bearer <token>` on every `apiFetch` (see `frontend/src/lib/api.ts:35–40`), the iframe can't attach that header — it loads a URL. The Next.js Route Handler must therefore source the auth credential from somewhere accessible server-side. **Three realistic options**:

1. **Session cookie** (httpOnly, set during MSAL login → Next.js) — requires a login-completion callback that writes a cookie; doesn't exist today.
2. **`X-API-Key` env var** (server-side only, via `process.env.MSAI_API_KEY` — **not** `NEXT_PUBLIC_*`) — works for Pablo-only single-tenant dev/demo, but bypasses Entra ID JWT. Simplest to ship.
3. **Forward incoming request's `Authorization` header**. If the iframe is embedded in a page that has a valid Bearer, the header arrives on the iframe's same-origin GET. **Except** iframes loaded via `src=` never carry Authorization headers from the parent — browsers make an independent fetch. This option doesn't work for a plain `<iframe src>`.

→ This research finds **Option 2 is the only zero-infrastructure option** that fits today's auth model. A proper session cookie (Option 1) is the "right" long-term answer but requires new login-callback plumbing, outside this PR's scope.

**(b) Forwarding auth as `Authorization: Bearer <token>` or `X-API-Key: <key>` header on an upstream `fetch`.** Standard — `fetch(upstreamUrl, { headers: { Authorization: \`Bearer ${token}\` } })`. Route Handlers run in Node.js by default.

**(c) Streaming the upstream response body (3–5 MB HTML) to the iframe without buffering.** The Next.js docs show two canonical patterns:

```ts
// Pattern A: Pass fetch's ReadableStream body directly (simplest proxy):
export async function GET(request: NextRequest, ctx: RouteContext<'/api/backtests/[id]/report'>) {
  const { id } = await ctx.params;
  const upstream = await fetch(`${BACKEND}/api/v1/backtests/${id}/report`, {
    headers: { 'X-API-Key': process.env.MSAI_API_KEY! },
  });
  if (!upstream.ok) {
    return new Response(null, { status: upstream.status });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'X-Content-Type-Options': 'nosniff',
      // Critical when a reverse proxy is in front (nginx buffers by default):
      'X-Accel-Buffering': 'no',
    },
  });
}

// Pattern B: File-handle streaming (not applicable — our backend serves FileResponse over HTTP, not local fs).
```

Pattern A is the correct choice: `upstream.body` is already a `ReadableStream` (Web Streams API). Returning it as the body of a new `Response` passes chunks through without buffering. This is the pattern for a BFF/proxy — Next.js docs explicitly call it out: "Route Handlers are useful for proxy/BFF patterns."

**(d) Does `NextResponse` support `ReadableStream` body?** Yes — `NextResponse extends Response`, and the Fetch `Response` constructor accepts a `ReadableStream` body. The docs example shows raw `new Response(stream, { headers: ... })` — use that over `NextResponse.json()`-style helpers for raw HTML.

**(e) `runtime = "nodejs"` vs `"edge"`.** For our iframe proxy:

- `"nodejs"` (the default, and what we need): supports full `fetch`, Node fs APIs, longer timeouts, and the Docker container's server-side networking to the FastAPI backend (via Compose service name). **Correct choice.**
- `"edge"`: would run in a V8 isolate (Vercel Edge runtime). Cannot reach our Docker-internal backend service `http://backend:8000` because edge runtimes execute outside the compose network. Also: edge has stricter response-size + execution-time limits.

Explicit config: `export const runtime = 'nodejs';` at the top of the Route Handler file.

**(f) `dynamic = "force-dynamic"`.** In Next.js 15, GET handlers default to dynamic (opposite of 14). So **`force-dynamic` is NOT strictly required** — the handler will be dynamic by default because it calls `cookies()`/reads `request.headers` and fetches external data. But setting `export const dynamic = 'force-dynamic'` is defensive and eliminates any chance Next.js tries to cache the response (critical when the upstream HTML may change per-id).

**(g) Token-leakage surfaces.**

- **URL query strings** are logged by default in every HTTP access log (nginx, caddy, browser history, referer headers). The PRD's "no token-in-query" rule is correct. Route Handler + header-only auth is the only safe path.
- **Next.js server logs** may print `request.url` at WARN/INFO — if a developer ever appends a query token, it ends up in Vercel/Node logs. Enforce at code-review time.
- **`console.log(request.headers)`** during debugging prints Authorization values. Don't; our code-review loop should flag any `console.log` near auth values.
- **Upstream logs**: FastAPI uvicorn logs `request.url` but NOT headers by default. Fine.

**Sources:**

1. https://nextjs.org/docs/app/api-reference/file-conventions/route — accessed 2026-04-21
2. https://nextjs.org/docs/app/guides/streaming — accessed 2026-04-21
3. https://github.com/vercel/next.js/discussions/50614 (ReadableStream in API route) — accessed 2026-04-21
4. https://dev.to/bsorrentino/how-to-stream-data-over-http-using-nextjs-1kmb — accessed 2026-04-21

**Design impact:**

1. **Use Pattern A** (return `upstream.body` directly as the Response body). Do NOT buffer (`await upstream.text()` → `return new Response(html)`) — that loads 3–5 MB into Node memory on every view. Streaming is free with Pattern A and preserves the Phase 4 spike's "TBD iframe UX".
2. **Auth credential must be server-side only** — use `process.env.MSAI_API_KEY` inside the Route Handler (NOT `NEXT_PUBLIC_MSAI_API_KEY` which leaks to the browser bundle). This is a new env-var surface for the frontend container; needs `docker-compose.dev.yml` update to pass `MSAI_API_KEY` to the `frontend` service. Call out in the plan.
3. **Explicit `export const runtime = 'nodejs';` + `export const dynamic = 'force-dynamic';`** at top of `frontend/src/app/api/backtests/[id]/report/route.ts`. Belt-and-suspenders.
4. **`X-Accel-Buffering: 'no'` header on the Response** in case a future nginx/proxy is introduced in front of Next.js.
5. **Content-Type must be explicit** `text/html; charset=utf-8` — do not rely on upstream passthrough of headers.
6. **Error propagation**: on `upstream.ok === false`, return `new Response(null, { status: upstream.status })`. The iframe will render its own error UI (browser default) — the React detail page should render a wrapper that checks the upstream via HEAD and shows "Report not available" if 404.

**Test implication:**

- E2E use case: sign in → open detail page → click "Open Full Report" → assert iframe `src` points at `/api/backtests/<uuid>/report` (same-origin) → assert iframe content loaded (document has `<title>` from QS) via `page.frameLocator(...)`.
- Unit test on the Route Handler: mock `fetch` to return a `Response(new ReadableStream([...]))`; assert `Response.body` was passed through (pointer equality or chunk-level compare).
- Negative test: upstream returns 404 → handler returns 404; upstream returns 500 → handler returns 500 (don't leak error bodies to client).
- Negative test: ensure no `Authorization` header appears in the response body OR response headers (regex-check headers).

---

### 3. (renumbered from PRD list's #3; matches PRD's #4) PostgreSQL 16 JSONB TOAST behavior

**Versions:** PG 16. Server default `default_toast_compression = pglz` (unless explicitly set to `lz4`).

**Key findings:**

- **TOAST trigger:** `TOAST_TUPLE_THRESHOLD = TOAST_TUPLE_TARGET = 2 KB` by default on 8 KB pages. A JSONB value that pushes the tuple over 2 KB gets compressed; if still > target, it gets pushed out-of-line into the TOAST table with an extra index traversal on read.
- **At 100 KB:** compressed (probably to ~20–40 KB after LZ4 or pglz, depending on JSON text repetition), stored out-of-line, one extra TOAST index lookup per read. Negligible perf hit for a single-row GET.
- **At 1 MB:** compressed, sliced into ~2 KB chunks, stored across many TOAST rows. Sequential read needs ~500 TOAST chunks + decompression. Still fast (<10 ms read) for a single row, but memory pressure grows: the Python driver allocates a 1 MB bytes buffer + ~1 MB parsed Python dict. API p99 latency holds if the route returns the payload as-is; watch out for `jsonb_path_ops` or GIN indexes (not needed here).
- **At 5 MB:** same mechanics but now you're eating 5 MB/request in Python memory for the API serializer, plus ~5 MB over the wire. The JSON decode in the asyncpg driver is C-implemented and fast (~50 ms for 5 MB), but uvicorn's default pipe buffer is 64 KB, so response streaming becomes important. **Over 10 MB** PostgreSQL JSONB starts hitting real read-path slowness and the 1 GB column-value hard limit is within one order of magnitude.
- **Council's ≤ 200 KB target is validated.** At 200 KB, even uncompressed, the payload is comfortably in the "single TOAST lookup" regime. Decompression overhead is negligible (< 1 ms). The 1 MB PRD soft cap is a reasonable WARN-log threshold that catches minute-bar leaks; 5 MB should be treated as an emergency (log.ERROR + refuse to embed in `/results` response?).
- **pglz vs lz4**: LZ4 is faster decompression (~5x pglz) but 20–25% bigger on disk for JSONB. Postgres 16 default is pglz. If the ALTER TABLE statement doesn't explicitly set `COMPRESSION lz4`, the column inherits pglz. Not a correctness concern; performance is still excellent either way. Probably leave default.
- **Adding `series JSONB NULL` on an existing table with `status` + `metrics` JSONB columns**: no conflict. JSONB columns are independent per-column; there's no "table-level" JSONB limit in Postgres 16. Each column has its own TOAST-triggering threshold on its own storage path.
- **Index/TOAST pitfalls on ALTER TABLE ADD COLUMN**: **None.** Adding a nullable JSONB column (no default, or `DEFAULT NULL`) is a **metadata-only catalog update** — no table rewrite, instant even on a 50-row OR a 50M-row table. Confirmed in Postgres ≥ 11. The `series_status String(32) NOT NULL DEFAULT 'not_materialized'` is also metadata-only because the default is non-volatile (it's a string literal), stored in `pg_attribute.attmissingval` and materialized lazily on row read. Same no-rewrite guarantee.

**Sources:**

1. https://www.postgresql.org/docs/16/storage-toast.html — accessed 2026-04-21
2. https://dev.to/franckpachot/postgresql-jsonb-size-limits-to-prevent-toast-slicing-9e8 — accessed 2026-04-21
3. https://www.credativ.de/en/blog/postgresql-en/toasted-jsonb-data-in-postgresql-performance-tests-of-different-compression-algorithms/ — accessed 2026-04-21
4. https://www.postgresql.fastware.com/blog/what-is-the-new-lz4-toast-compression-in-postgresql-14 — accessed 2026-04-21

**Design impact:**

- **≤200 KB target in decision doc is well-inside the safe regime** — no architectural changes needed. The council's cap is the correct guard rail.
- **1 MB WARN-log threshold** (PRD US-001 edge case): add a check in `_finalize_backtest()` — if `len(json.dumps(series)) > 1_048_576`, emit `msai_backtest_series_payload_oversized` WARN log with `backtest_id` + `payload_bytes`. Still write; don't fail.
- **Consider a 5 MB hard ceiling** that downgrades `series_status = "failed"` with reason `payload_too_large` — prevents any single backtest from dumping 100k minute bars into JSONB and catastrophically slowing `/results` GET.
- **Explicit TOAST strategy** (optional): set `ALTER TABLE backtests ALTER COLUMN series SET STORAGE EXTENDED;` — default for JSONB, but explicit is defensive if any future ORM migration tooling changes the default.
- **No index on `series`**: the PRD never queries *inside* `series` — we only fetch the whole column per backtest. Skip `GIN` / expression indexes; they'd cost ingest time for zero value.

**Test implication:**

- Unit test: write a synthetic 1.2 MB `series` payload → assert WARN log fires, payload still persists, `/results` still returns 200.
- Unit test: write a 10 MB synthetic payload → assert `series_status = "failed"` path fires with `payload_too_large` reason (if 5 MB hard ceiling adopted).
- Integration test: `/results` p99 latency < 200 ms with a real 200 KB series payload (council's target metric from PRD success metrics).
- Migration round-trip test (Alembic upgrade + downgrade + re-upgrade) with the existing `test_alembic_migrations.py` harness — MSAI has a pattern established via PR#32's `v0q1r2s3t4u5_instrument_registry.py` round-trip.

---

### 4. (PRD #5) FastAPI pagination conventions (0.133.1 + Pydantic 2.12.5)

**Versions:** FastAPI 0.133.1, Pydantic 2.12.5.

**Key findings:**

- The project's `.claude/rules/api-design.md` specifies `{items, total, page, page_size}` — this matches the mainstream FastAPI convention in 2026 and the existing codebase-wide idiom. All list endpoints (`StrategyListResponse`, `BacktestHistoryResponse`, `ResearchJobListResponse`, `GraduationCandidateListResponse`, `PortfolioListResponse`, `PortfolioRunListResponse`) follow the same 4-field shape. Consistency = no new type, no new client-side wrapper.
- The third-party library `fastapi-pagination` (uriyyo) **is** popular (2026) and offers: drop-in `Page[Schema]` generic, cursor + page-based, 10+ ORM integrations. But:
  - We'd need a new dep.
  - Swagger/OpenAPI with generic classes has known quirks — Pydantic V2 + FastAPI generics occasionally fumble JSON-string vs object encoding at the OpenAPI boundary.
  - Hand-rolled is `if page < 1 or page_size < 1: raise 422` + `SELECT count()` + `SELECT … LIMIT page_size OFFSET (page-1) * page_size` — ~15 LOC. One endpoint. Zero benefit to the library.
- **Recommend: hand-rolled**, matching the existing idiom. Project has ~7 examples of the same 4-field shape; a new endpoint using `fastapi-pagination` would stand out (and violate the project's "reuse over new deps" principle).
- Pydantic V2 idiom: define three classes (`TradesQueryParams` as a `Depends()` param, `BacktestTradeItem`, `BacktestTradesResponse`). `BacktestTradesResponse` reuses the shape:
  ```python
  class BacktestTradesResponse(BaseModel):
      items: list[BacktestTradeItem]
      total: int
      page: int
      page_size: int
  ```
- **Server-side page_size clamping**: the PRD open question asks "clamp to 500 or reject with 422?" — rules/api-design.md §5 and §7 say 422 for semantic errors. `page_size > 500` is not semantically invalid (client could legitimately want 1000); it's a rate-limit. Pydantic `Field(le=500)` rejects with 422 — but rejecting is hostile for a client who "asked too much and got nothing." **Recommend: clamp via custom validator**, not reject. Return the clamped value in response and add `X-Actual-Page-Size` header. But this is a plan-review question, not a research question — both options are idiomatic.
- Use `SELECT func.count()` for total — not `len(result.scalars().all())`. Project's `.claude/rules/database.md` explicitly calls this out.

**Sources:**

1. https://github.com/uriyyo/fastapi-pagination — accessed 2026-04-21
2. https://fastapi-pagination.netlify.app/ — accessed 2026-04-21
3. `backend/src/msai/schemas/*.py` — existing pagination shape in the codebase
4. `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/.claude/rules/api-design.md` — project rules

**Design impact:**

- Hand-roll. Reuse the exact `{items, total, page, page_size}` shape. No new dep.
- Sort order: PRD says `executed_at ASC`. Add explicit `ORDER BY executed_at ASC, id ASC` for deterministic pagination when two trades share the same timestamp (minute-bar strategies can fire multiple orders at one bar — ties break by UUID).
- Use `func.count()` for total (never `len(scalars().all())`).
- Add a `WHERE Trade.backtest_id = :id` filter and index hint if needed (`trades.backtest_id` — verify the existing FK index exists; `.claude/rules/database.md` mandates FK indexes. If it doesn't, add in the same migration).

**Test implication:**

- Test: `page=1&page_size=100` returns first 100 + correct total.
- Test: `page=5&page_size=100` on a 420-row backtest returns the final 20 + `total=420`.
- Test: `page=99` past-end returns empty `items: []` + correct `total`, NOT 404 (PRD edge case).
- Test: `page_size=0` or `page=0` → 422.
- Test: `page_size=10000` → clamps to 500 (or 422 — pick and test).
- Test: ordering stable across calls (insert 5 trades with identical `executed_at`, verify same order on two GETs).

---

### 5. (PRD #6) NautilusTrader `BacktestResult.account_df` structure (1.223.0)

**Versions:** ours=1.223.0. Matches the installed version in `backend/uv.lock` line 1411.

**Key findings:**

- `engine.trader.generate_account_report(venue=Venue("SIM"))` returns a `pandas.DataFrame`. Columns depend on the run — not every run emits a `returns` column. Commonly present: `account_id`, `balances_total`, `balances_free`, `equity`, `unrealized_pnl`, `realized_pnl`, `currency`, `ts_event`, `ts_init`. The `returns` column appears when Nautilus has computed period-over-period equity-to-equity returns internally; this is not guaranteed.
- The index is usually `DatetimeIndex` (the timestamp of each balance/equity snapshot), but can be `RangeIndex` in some code paths. The existing `_extract_returns_series` (`backtest_job.py:586–630`) already handles this gracefully: it looks for `"returns"` column → errors empty if absent; then tries to build a timestamp index from `ts_last`, `ts_init`, or `timestamp` columns, falling back to the frame's index if already `DatetimeIndex`.
- Running a backtest where the account never materially updates (zero trades, pure buy-and-hold of cash) can produce an `account_df` with only one row and no `returns` column — **edge case that's already in the PRD** as US-001 "Backtest produces zero trades" path.
- `_compact_account_report` (`backtest_runner.py:421–469`) already daily-compounds intraday account snapshots into a flat `timestamp | equity | returns` frame before passing to the worker — this is the ideal source for the new `series` payload. The existing function just needs to expose the pre-compact returns series to `_finalize_backtest()` via the subprocess IPC surface (`BacktestResult.account_df`).
- `_extract_returns_series` reads `account_df["returns"]` — which in the current code is the **already-daily-compacted** returns column (because `_compact_account_report` runs inside the subprocess). So the returns series feeding QuantStats is already daily. The council's "single normalization path" mandate is close — both `analytics_math.build_series_from_returns` and `report_generator._normalize_report_returns` operate on this same daily returns series, but they compute equity/drawdown independently. Consolidation is straightforward.
- One subtle point: `account_df["returns"]` assumes Nautilus's `returns` column represents **simple period returns** (`equity_t/equity_{t-1} - 1`). If Nautilus ever switched to log returns internally, the formula `(1 + r).cumprod() - 1` would break silently. Worth a one-line assertion in `_finalize_backtest()` that sums a synthetic check: reconcile `equity_t` against `initial_equity * prod(1 + r)` within tolerance.

**Sources:**

1. https://nautilustrader.io/docs/latest/concepts/backtesting/ — accessed 2026-04-21
2. https://docs.nautilustrader.io/api_reference/backtest.html — accessed 2026-04-21
3. `backend/src/msai/workers/backtest_job.py:586–630` — our extraction logic
4. `backend/src/msai/services/nautilus/backtest_runner.py:421–469` — our daily-compaction logic
5. `.claude/rules/nautilus.md` gotcha #2 — `generate_account_report()` requires `venue=`

**Design impact:**

- **`_extract_returns_series` is reading the right column** — no change needed to its contract.
- **The daily-compaction logic is already correct** and living in `_compact_account_report` in the subprocess. The council's "one atomic write in `_finalize_backtest()`" mandate means the new `series` payload should be **derived from the compacted frame**, not re-derived from raw ticks. Concretely: the subprocess's IPC result should include a `daily_returns_series` alongside `account_df` and `metrics`, so the worker doesn't re-compact.
- **Empty-`returns`-column edge case**: code today returns an empty `pd.Series(dtype=float)`. In the new path, this should flow to `series_status = "failed"` with reason `empty_returns_column` — NOT silently produce an empty `series.daily = []` that the UI would render as a flat line.
- **Add a reconciliation assertion** (WARN-level, not ERROR): verify `equity_t / equity_{t-1} - 1 ≈ returns_t` within 1e-6 tolerance on first + last row. Catches any future Nautilus switch to log returns.

**Test implication:**

- Integration test with a real synthetic account_df (existing fixtures in `test_parity_determinism.py` produce these) — assert `series.daily[-1].equity == initial_capital * prod(1 + daily_returns)`.
- Edge-case test: `account_df` with only `balances_total` (no `returns` column) → `series_status = "failed"`, reason `empty_returns_column`.
- Version-compat test: run the ingest end-to-end against a real NautilusTrader BacktestNode, assert `series.daily` is non-empty and has `date/equity/drawdown/daily_return` keys per row. Catches any future Nautilus version bump that changes `generate_account_report()`'s columns.

---

### 6. (PRD #7) Pandas daily compounding / returns normalization

**Versions:** pandas 2.3.3.

**Key findings:**

- **Canonical equity formula** (matches `analytics_math.build_series_from_returns`):
  ```python
  equity = (1.0 + returns).cumprod() * base_value   # starts above base_value
  # OR the form the decision doc calls out:
  equity_normalized = (1.0 + returns).cumprod() - 1.0   # starts at 0 (percentage)
  ```
  Both are standard. Current code uses the first form (dollar equity); decision doc uses the second form conceptually. These are `equity / base_value - 1` equivalents.
- **Canonical drawdown formula** (matches `analytics_math.build_series_from_returns` and `compute_series_metrics`):
  ```python
  running_max = equity.cummax()
  drawdown = equity / running_max - 1.0   # ≤ 0, 0 when at new high
  ```
  The PRD phrasing `"min(running_max, equity) - 1"` is slightly off — should be `equity / running_max - 1` (division, not subtraction). The existing `analytics_math.py` line 46–51 has the correct math.
- **Decimal precision**: pandas does float64 throughout. Over a 10-year daily series (~2520 points) with `cumprod()`, error accumulates but stays well within 1e-10 relative. Irrelevant for chart rendering but worth knowing for the reconciliation assertion above.
- **Timezone-aware DatetimeIndex edge cases**:
  - `_clean_returns_series` in `analytics_math.py` forces UTC. Good.
  - `_normalize_report_returns` uses `index.normalize()` to strip time-of-day for daily grouping. This is the canonical path.
  - Beware: if the DatetimeIndex is tz-naive (e.g. raw Parquet load), `pd.to_datetime(..., utc=True)` assumes UTC. If the source data is actually exchange-local (e.g. NYSE close times), every bar gets nudged 4–5 hours. Existing code appears to assume UTC throughout; this matches NautilusTrader's convention (it stores `ts_event` as UTC nanoseconds). Safe.
  - Daily grouping via `.groupby(index.normalize())` on a tz-aware index stays tz-aware. Good.
- **Perf on large series**: `(1 + r).cumprod()` is pandas-vectorized (NumPy C-backed) — a 2520-element series completes in < 1 ms. A 500k-element minute-bar series completes in ~20 ms. Perf is a non-issue for the daily-compounded payload (target < 2000 rows); only matters if we ever feed raw minute bars through.
- **The drift issue the Contrarian/Maintainer flagged**: currently `analytics_math.build_series_from_returns` (for general use) and `report_generator._normalize_report_returns` (for QuantStats) are separate functions with overlapping logic. `_normalize_report_returns` does `groupby(index.normalize()).prod() - 1` — compounds intraday to daily. `build_series_from_returns` does NOT daily-compound; it just computes `cumprod()`. **These are intentionally different and do different jobs**:
  - `_normalize_report_returns` = "convert minute-bar-returns into daily-returns for QuantStats."
  - `build_series_from_returns` = "given a returns series (presumed already daily), compute cumulative equity + drawdown for the chart."
  - The council's "single normalization path" mandate asks us to chain them: `daily_returns = _normalize_report_returns(raw_returns)` → `series_frame = build_series_from_returns(daily_returns)`. Both callers (worker → JSONB persistence; worker → QuantStats HTML) start from the same `daily_returns` intermediate. This **does not** require rewriting either function — just explicitly composing them and giving the composite a name like `build_canonical_series(raw_returns)`.

**Sources:**

1. `backend/src/msai/services/analytics_math.py:36–55` — equity + drawdown math
2. `backend/src/msai/services/report_generator.py:20–59` — daily-compounding for QS
3. https://pandas.pydata.org/docs/reference/api/pandas.Series.cumprod.html — accessed 2026-04-21

**Design impact:**

- **Drawdown formula in the PRD is wrong** (`min(running_max, equity) - 1`). Fix during plan writing: it's `equity / running_max - 1`. The decision doc doesn't use this phrasing — PRD did. Flag to plan author.
- **Define `build_canonical_series(raw_returns)`** as the single entry point the worker calls. Internally it chains `_normalize_report_returns` + `build_series_from_returns`. Both existing functions stay (QuantStats and other callers still use them individually). This satisfies the Maintainer's "single normalization path" constraint **without** a risky refactor.
- **Preserve tz-awareness** in the JSONB payload by emitting `"date": "YYYY-MM-DD"` (already planned in PRD) — no time-of-day, no tz offset in the string. This matches monthly aggregation (no ambiguity about which day a bar belongs to).
- **Monthly returns sub-payload derivation**: `daily_returns.groupby(lambda d: d.strftime("%Y-%m")).apply(lambda r: (1 + r).prod() - 1)`. Same compound-then-subtract-1 pattern. Add to `build_canonical_series` as the `monthly_returns` section.

**Test implication:**

- Synthetic test: 252-day returns of 0.001 each → `equity.iloc[-1] == (1.001**252) * base_value` within 1e-10.
- Synthetic test: returns with 3 consecutive down days after a new high → `drawdown.iloc[idx_of_low] == expected_drawdown` within 1e-10.
- tz-awareness test: tz-naive DatetimeIndex input → `build_canonical_series` doesn't silently shift dates.
- Monthly aggregation test: 2-year daily returns with known monthly sums → assert `series.monthly_returns` matches.
- Reconciliation test between new `series` payload and QuantStats output on the same backtest — pick one stat (e.g., total return) and assert both paths produce the same number within 1e-6.

---

### 7. (PRD #8) Recharts 3.7.0 + TradingView Lightweight Charts 5.1.0 for the 3 native charts

**Versions:** recharts 3.7.0, lightweight-charts 5.1.0, React 19.1.0, Next.js 15.5.12.

**Key findings:**

**(a) Recharts `<AreaChart>` + `<Area>` for equity curve** — current best practice in 3.7.0:

```tsx
<ResponsiveContainer width="100%" height={400}>
  <AreaChart data={equityData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
    <CartesianGrid strokeDasharray="3 3" />
    <XAxis dataKey="date" />
    <YAxis />
    <Tooltip />
    <Area type="monotone" dataKey="equity" stroke="#8884d8" fill="#8884d8" fillOpacity={0.3}
          isAnimationActive={false /* <- REQUIRED for 1000+ points perf */} />
  </AreaChart>
</ResponsiveContainer>
```

- `isAnimationActive={false}` is essential for 1260-point charts. Without it, Recharts animates every point on mount, which triggers 1260 SVG re-renders over ~1.5 s. With it, the chart paints in one frame.
- Memoize `data` with `useMemo` — Recharts treats prop identity as "data changed" and reruns scale calculation on every render otherwise.
- `<ResponsiveContainer>` is the correct wrapper. Do NOT use `%` width on `<AreaChart>` directly — it requires a pixel width.
- Recharts 3.8.0 added TS generics (`<AreaChart<EquityPoint> data={...}>`) — we're on 3.7.0 so no-op; can bump later.

**(b) Drawdown overlay on the same chart**: two acceptable patterns:
1. `<AreaChart>` with two `<Area>` components on different `yAxisId`. Clean visually but the scales are very different (equity 100k-level, drawdown -0.1..0). Needs dual-axis tuning.
2. Two separate charts stacked — simpler, which is what the PRD already proposes (Equity + Drawdown as distinct components).

Go with #2 — matches PRD, matches the existing `ResultsCharts` scaffold.

**(c) Monthly returns heatmap** — Recharts does NOT have a built-in heatmap. Options:
1. **Custom SVG grid** (what the existing scaffold does, based on `results-charts.tsx` — have to verify). Rows = years, columns = months, cell color = tailwind class mapped to return magnitude. ~100 LOC. Same-origin, no new deps.
2. **visx `<HeatmapRect>`** (airbnb's visx is what most teams adopt for custom D3-in-React). New dep. Overkill for 12×N grid.
3. **nivo `<HeatMap>`** — new dep. Overkill.
4. **React + CSS Grid + conditional `bg-*` classes** — simplest, no SVG even needed. 60 LOC. Accessible via `<table>` semantics.

→ Recommend #4 (React + CSS Grid + Tailwind oklch colors). Matches project's `frontend-design.md` premium-dark-mode aesthetic (CSS variables + oklch). No new deps. TradingView Lightweight Charts has no heatmap support and is not appropriate for this component.

**(d) Rendering performance:**
- 1260-point daily equity (~5 years daily): with `isAnimationActive={false}` + `useMemo`, paints in ~50 ms. Fine.
- 60-month heatmap (5 years × 12 months): trivial. Table with 60 cells.
- 252-point yearly breakdown: trivial.

**(e) React 19 / Next.js 15 hydration gotchas**:
- Recharts 3.x has had React 19 issues historically (issue #4558 in recharts repo). The current 3.7.0 should be fine — the fix landed around 3.5.0 — but there are intermittent reports of charts failing to render when dynamically imported in Next.js 15 App Router without `"use client"`. **Mandate `"use client"` directive** at the top of any chart component. The existing scaffold already does this — `results-charts.tsx:1` has `"use client";`.
- Inline data: 1260 points × 4 float keys × JSON bytes ≈ 80 KB of React props. Passed as a prop, it doesn't re-serialize on rerender (JS reference identity). Fine. Hydration mismatch risk: only if the server-rendered chart produces different SVG paths than the client-hydrated one, which shouldn't happen with stable data.
- If you see "hydration error" in the browser console, wrap the chart in `<Suspense>` + `dynamic(() => import('./chart'), { ssr: false })`. Fallback plan only; not needed by default.

**(f) Recharts v3 deprecations** to watch:
- `Cell` component deprecated in 3.7.0 (removed in next major) — we don't use `<Cell>` in any chart, so N/A.
- `Pie.activeShape` / `Pie.inactiveShape` deprecated — we don't use Pie, N/A.
- `CartesianAxis` deprecated — we use `XAxis` / `YAxis` (not `CartesianAxis`), N/A.

**Sources:**

1. https://recharts.github.io/en-US/guide/performance/ — accessed 2026-04-21
2. https://github.com/recharts/recharts/releases — accessed 2026-04-21
3. https://github.com/recharts/recharts/issues/4558 (React 19 support) — accessed 2026-04-21
4. https://github.com/recharts/recharts/wiki/3.0-migration-guide — accessed 2026-04-21
5. `frontend/src/components/backtests/results-charts.tsx:1–20` — existing imports (AreaChart/Area/LineChart/Line from recharts)

**Design impact:**

- **Equity + Drawdown components**: `<AreaChart>` + `<Area isAnimationActive={false} />` + memoized data. Two separate chart components, stacked vertically. Matches PRD.
- **Monthly heatmap**: custom CSS Grid of `<div>` cells colored via `bg-[oklch(...)]` with opacity proportional to return magnitude. Accessible via `<table><thead><tbody>` semantics. No new deps.
- **Do NOT import Recharts in the SSR shell** — chart components must be client-only. `"use client"` directive at file top. The existing scaffold has this; preserve it.
- **Do not use TradingView Lightweight Charts** for any of these three charts. It's the right tool for OHLC + indicators (the existing `candlestick-chart.tsx` uses it), but not for equity / drawdown / heatmap.
- **No need to bump Recharts** from 3.7 to 3.8.1 in this PR. Optional follow-up; nothing here blocks on it.

**Test implication:**

- Playwright smoke test: open detail page → `getByTestId("equity-chart")` has a `<svg>` child with >100 `<path>` elements (indicates actual data rendered, not empty).
- Visual regression: Playwright snapshot of the chart at a fixed viewport size on a known-good backtest.
- Accessibility: heatmap must have `<table>` semantics + `<th scope="col">` + `<caption>` — verify with `page.getByRole("table")`.
- React 19 hydration test: navigate from `/backtests/history` (server render) → click row → `/backtests/[id]` (detail) — assert no hydration errors in console.

---

### 8. (PRD #9) iframe render of 3–5 MB HTML

**Versions:** Chromium 120+, Firefox latest, Safari 17+ (all current browsers).

**Key findings:**

- **Browser behavior on a 5 MB same-origin iframe `src`**:
  - Chromium: same-site iframes run in the same renderer process as the parent by default (Site Isolation only kicks in for cross-site). A 5 MB HTML parse in the same process means the parent tab's main thread stalls briefly during iframe parse/layout — probably 100–500 ms on a modern machine.
  - Firefox: similar architecture.
  - Safari/WebKit: same.
- **Memory**: each iframe adds a full document/layout/paint tree. 5 MB HTML → ~50–100 MB of live DOM (roughly 10–20× blow-up from source HTML size due to layout/style objects). **Non-trivial but not catastrophic.** On a 16 GB laptop it's invisible; on a 4 GB Chromebook it would hurt.
- **Streaming vs full-load**: browsers render HTML progressively as they parse. Our Next.js Route Handler streaming via `upstream.body` means the iframe starts painting before the full 5 MB arrives. **This is the core UX win** for the iframe approach and is preserved by Pattern A (research target #2).
- **QuantStats HTML structure**: QuantStats emits a single HTML file with embedded base64 PNG images (the charts are matplotlib PNG, not SVG). At 5 MB, roughly 80% is PNG base64. There are usually ~20 images at 150-200 KB each after base64. Parsing + decoding all of them takes ~200–500 ms wall-clock on first paint. Subsequent paints are cached in memory.
- **Known iframe memory leaks**: if JavaScript holds a reference to `iframe.contentWindow`, the iframe document can't be GC'd even after `iframe.remove()` (web.dev: "Detached window memory leaks"). The MSAI detail page should NOT hold such references; the iframe is a dumb `<iframe src="…" />`. Plan-review item: code review should flag any `ref.current.contentWindow` usage.
- **`sandbox` attribute**: QS HTML is a self-contained document with no cross-origin calls. Can safely use `sandbox="allow-same-origin"` to block scripts (QS embeds a few interactive JS bits — `Plotly`-like hover tooltips on some charts, depending on QS version). Test with the actual output HTML before deciding — probably `sandbox="allow-same-origin allow-scripts"` is needed for full functionality, and is still safer than no sandbox.
- **Lazy-loading**: `<iframe loading="lazy">` delays load until near-viewport. Useful if the iframe is below the fold in a tab. But if it's the active tab's default view, lazy-loading does nothing. OK either way.
- **Phase 4 spike still warranted**: browser behavior at 5 MB HTML with 20 embedded PNGs is **not documented to the "this works fine" level** in any authoritative source I could find. The 30-minute spike the PRD flags is the right call. But **research finds no fundamental blocker** — this is a UX characterization, not a "does it work at all?" question.

**Sources:**

1. https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe — accessed 2026-04-21
2. https://www.chromium.org/developers/design-documents/oop-iframes/ — accessed 2026-04-21
3. https://web.dev/articles/detached-window-memory-leaks — accessed 2026-04-21
4. https://web.dev/articles/iframe-lazy-loading — accessed 2026-04-21
5. https://webperf.tips/tip/iframe-multi-process/ — accessed 2026-04-21

**Design impact:**

- **Keep the Phase 4 spike** (30 min) — but know in advance: there's no authoritative "iframe-of-5MB-HTML is broken" signal. Expect it to work.
- **Add `sandbox="allow-same-origin allow-scripts"`** to the iframe to reduce attack surface (future-proofing — doesn't hurt today's single-tenant use).
- **No lazy-loading** — the iframe is the detail page's full-report tab; user clicks through to view it, so loading should begin immediately.
- **Do NOT read `iframe.contentWindow`** from React — flag at code-review time.
- **Streaming MUST work end-to-end**: the Route Handler passes `upstream.body` (Pattern A from target #2), upstream FastAPI `FileResponse` streams the file in chunks. Sanity-check that our reverse proxy (if any) doesn't buffer — set `X-Accel-Buffering: no`.

**Test implication:**

- Manual: the Phase 4 spike — spin up docker-compose + a real backtest, open the detail page, measure time-to-first-iframe-paint and memory overhead in Chrome DevTools Performance tab.
- Automated: E2E with Playwright — open detail page → click "Open Full Report" → `page.frameLocator('iframe').locator('body').waitFor({ state: 'visible', timeout: 10_000 })` → no timeouts/errors.
- Memory profile (optional, nice-to-have): run the spike in Chrome with `performance.measureUserAgentSpecificMemory()` before/after iframe load.

---

### 9. (PRD #10) Alembic NULL→DEFAULT migration on existing table

**Versions:** Alembic 1.18.4, Postgres 16.

**Key findings:**

- `ALTER TABLE backtests ADD COLUMN series JSONB NULL` on Postgres 16 is a **metadata-only catalog update** — no table rewrite, no AccessExclusiveLock beyond microseconds, instant on 50 rows or 50M rows.
- `ALTER TABLE backtests ADD COLUMN series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'` is ALSO metadata-only on Postgres ≥ 11 because:
  - The default is non-volatile (a string literal).
  - Postgres stores the default in `pg_attribute.attmissingval`.
  - Existing rows appear to have the default on read (lazy materialization); new rows get the physical default on insert.
  - Confirmed by postgres.org docs and multiple sources.
- Alembic revision template: standard `op.add_column('backtests', sa.Column('series', JSONB, nullable=True))`. No `postgresql_using` needed (no type conversion).
- **Downgrade path**: `op.drop_column('backtests', 'series')` + `op.drop_column('backtests', 'series_status')`. Drops are metadata-only too.
- **Alembic `autogenerate` + JSONB `server_default` known issue** (referenced in alembic issue #272 / #411): autogenerate can emit invalid SQL if the JSONB server_default isn't a SQL-quoted JSON string. Since the PRD uses `NULL` for `series` and a plain string `'not_materialized'` for `series_status`, **this doesn't affect us** — we're not autogenerating against a Python-side default, we're hand-writing the migration with `sa.text("'not_materialized'")` for the `server_default`.
- **MSAI's migration chain** currently ends at `y3s4t5u6v7w8_add_backtest_auto_heal_columns` (per `backend/alembic/versions/` listing). New revision IDs in MSAI follow the pattern `<one-letter><digit><five-char-random>` — next would be something like `z4t5u6v7w8x9_add_backtest_series_column`.
- **Migration round-trip test** pattern established in `test_alembic_migrations.py` (upgrade → downgrade → upgrade). Apply same pattern.

**Sources:**

1. https://www.postgresql.org/docs/16/sql-altertable.html — accessed 2026-04-21
2. https://www.bytebase.com/reference/postgres/how-to/how-to-alter-large-table-postgres/ — accessed 2026-04-21
3. https://www.depesz.com/2018/04/04/waiting-for-postgresql-11-fast-alter-table-add-column-with-a-non-null-default/ — accessed 2026-04-21
4. https://github.com/sqlalchemy/alembic/issues/272 — accessed 2026-04-21 (known autogen issue — we're not affected because we hand-write)
5. `backend/alembic/versions/` — existing MSAI migration chain

**Design impact:**

- Hand-write the migration, don't rely on autogenerate (MSAI pattern anyway).
- Use `sa.text("'not_materialized'")` for the `server_default` — NOT a Python string, NOT a Python callable.
- Nullable JSONB + `server_default` string on VARCHAR both qualify for metadata-only ALTER.
- Test pattern: follow `test_alembic_migrations.py` upgrade/downgrade round-trip. Assert pre-migration rows have `series_status = 'not_materialized'` after upgrade (lazy materialization).
- **Do NOT add a CHECK constraint** on `series_status` in the initial migration — the decision doc says "CHECK constraint optional." Enforce the enum in Pydantic `Literal` only for v1; promote to CHECK constraint in a follow-up if ops discipline fails. Reason: adding the CHECK later is one more metadata-only ALTER, so no cost to deferring.

**Test implication:**

- Alembic round-trip: upgrade → downgrade → upgrade; assert pre-migration rows survive and have `series_status = 'not_materialized'` post-upgrade.
- Integration: insert a legacy-shape backtest row, upgrade the schema, assert `/results` endpoint returns `series_status: "not_materialized"` (US-005 coverage).
- Unit: Pydantic `Literal["ready", "not_materialized", "failed"]` validator rejects any other string — catch one-character typos at API boundary.

---

## Not Researched (with justification)

- **asyncpg**: the PG driver is version-pinned indirectly through SQLAlchemy. No new usage patterns here — we're adding one JSONB column on an existing table that already has JSONB columns. Current usage is fine.
- **arq** (job queue): not touched by this feature. `_finalize_backtest()` is inside an existing arq worker; the new logic happens synchronously within that function.
- **Azure Entra ID / PyJWT / MSAL**: auth wiring is unchanged. All new endpoints inherit `Depends(get_current_user)`. MSAL on the frontend is out of scope.
- **TradingView Lightweight Charts**: not appropriate for any of this PR's 3 native charts. Already used for `candlestick-chart.tsx` elsewhere. No usage change.
- **Tailwind 4 / shadcn/ui**: purely presentational, no research needed for this feature's data-flow decisions.
- **QuantStats 0.0.78 Monte Carlo module**: out of scope (future PR); does not affect today's HTML output.
- **fastapi-pagination**: explicitly considered and rejected in target #5. Using hand-rolled pattern.

---

## Cross-Cutting Observations

1. **PRD has one math typo**: drawdown formula `min(running_max, equity) - 1` should be `equity / running_max - 1`. The existing `analytics_math.py` code is correct; PRD phrasing is off. Should be fixed during plan-writing (Phase 3.2).
2. **PRD decision on session-cookie vs API-key auth for the iframe proxy is underspecified.** Today's `apiFetch` sends Bearer on every fetch, but the iframe's GET has no way to attach a Bearer. Three options exist (session cookie / server-side env API key / forwarded Authorization). **Only server-side env API key works today without new login-callback plumbing.** Flag to plan-writing: either commit to the env-API-key path (simple, single-tenant OK) or schedule a prerequisite PR to add a session-cookie login callback.
3. **Server-side env var for Next.js container**: `MSAI_API_KEY` must be passed to the `frontend` service in `docker-compose.dev.yml` (and its prod equivalent) as a non-public env var. Today `NEXT_PUBLIC_MSAI_API_KEY` is shipped to the browser (see `frontend/src/lib/api.ts:13`); the **server-side-only** key is a different env var and a different surface. This is a small but real new infrastructure change.
4. **Existing `BacktestTradeItem` TS type is wrong** (shows `entryPrice/exitPrice/holdingPeriod` — lines 324–334 of `frontend/src/lib/api.ts`). Confirmed by reading the file. PRD US-004 already calls this out; no research surprise.
5. **No existing Next.js Route Handler proxy pattern** in the codebase (no `frontend/src/app/api/` directory). This PR creates the first such pattern. Worth documenting as a reusable template for future features.
6. **QuantStats invocation in `report_generator.py` uses `qs.reports.html(..., download_filename=tmp_path)`** — a QS kwarg that names the download-button filename. Unchanged. Not relevant to this PR but worth noting we understand it.
7. **The `account_df` IPC boundary** (file-based pickle in `backtest_runner.py`) is already solid. No changes needed to the IPC layer; `_finalize_backtest` just starts using the daily-returns series from the same pickle.
8. **Existing `/results` endpoint returns `trades` inline** (`backtests.py:449–454`) — this is the behavior US-004 explicitly removes. Deleting inline trades from the `BacktestResultsResponse` schema is a **minor breaking change** to the API contract for any CLI/external consumer; but the only known consumer is the frontend, which we're changing in the same PR. Worth one CHANGELOG line.
9. **Docker-compose volumes are pinned** (see CONTINUITY "Done cont'd 10") — so adding new env vars to the frontend service won't disturb data.
10. **CI workflow is broken** per CONTINUITY (item "High-priority #1"). Plan should not assume CI passes — this PR's tests will need local + verify-e2e validation.

---

## Open Risks

1. **Iframe auth path is undecided** (see cross-cutting #2). If the plan chooses session-cookie auth, the scope expands to include a login-callback Route Handler. If it chooses server-side env-API-key, the plan must add the env var to the frontend container and document that the iframe bypasses Entra JWT for Pablo-only context.
2. **QuantStats HTML may include base64 PNGs up to 5 MB total** — browser behavior at this size is probably fine but unmeasured. Phase 4 spike (30 min) validates. Risk: if paint time on detail-page-first-load is > 1 s, UX suffers. Mitigation: keep the iframe in a tab/collapse section (PRD already has this).
3. **`series` JSONB payload size for long minute-bar backtests.** Council capped the ceiling via "daily-compound at worker-write time." Validate during Phase 4 TDD on a 5-year minute-bar backtest: ensure `len(json.dumps(series)) < 200 KB` for ~1260 daily points. Risk: if we accidentally leak minute-bar data into `series.daily`, the payload explodes to 50+ MB and crashes both the API response and the browser.
4. **Reconciliation of `series` aggregate stats with QuantStats HTML stats.** The decision doc's parity contract says `Backtest.series` wins if they diverge. But divergence means UX confusion. Risk: a future QS version bump changes compounding semantics — our persisted `series` stays correct, HTML goes stale, and the UI needs to flag this. Mitigation: already in the decision doc ("HTML is flagged stale-but-viewable"). Plan must include this flag in the iframe tab.
5. **Legacy backtests (pre-PR) and the `report_path` / `series` mismatch.** Pre-PR rows have `report_path` (QS HTML is old) but `series = NULL`. Legacy iframes still work (HTML still on disk); native charts show empty state. PRD US-005 handles this. Risk: a legacy backtest whose report file got deleted (disk cleanup) has no iframe and no native charts — user sees a dead detail page. Mitigation: detail page should check both `report_path` file existence (via HEAD) and `series_status` before picking the empty-state copy.
6. **`page_size` clamp vs 422 decision is deferred to plan review**. Risk: the plan picks a convention that doesn't match the rest of the API. Mitigation: codebase convention is clamp + warn (not reject) — follow the precedent set by `ResearchJobListResponse`, `GraduationCandidateListResponse`, etc.
7. **React 19 + Next.js 15 App Router hydration edge cases with large inline chart data**. Not a bug today but a class of issues that may surface. Mitigation: `"use client"` directive on all chart components (already in scaffold); defensively `dynamic({ ssr: false })` wrap if hydration errors appear.
8. **Recharts v3 activeIndex prop removal + Cell deprecation**. Not affecting this PR's code paths (we don't use activeIndex or Cell), but future PRs adding interactive charts should plan for these.
9. **Next.js 16 upgrade pending.** We're on 15.5.12; Next 16 is out. Not blocking this PR, but any new Route Handler patterns we write should be forward-compatible with Next 16 (they are — Route Handler API didn't change).
10. **Dev container env var propagation**. Changing `docker-compose.dev.yml` to pass `MSAI_API_KEY` to the frontend service requires operator action — follow-up: restart compose stack. Flag in runbook.

---

## Design-Changing Findings (TL;DR)

1. **Use Next.js Route Handler Pattern A** (`new Response(upstream.body, {headers})`) for the iframe proxy — NOT buffer-then-return. Streams 5 MB HTML without memory pressure.
2. **Iframe auth must use server-side `MSAI_API_KEY` env var (not `NEXT_PUBLIC_*`)** — requires docker-compose env change; this is the only path that works without new login-callback plumbing.
3. **`ALTER TABLE … ADD COLUMN series JSONB NULL`** + **`series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'`** are both metadata-only on Postgres 16 — no table rewrite, no lock escalation. Safe for production, fast even if the `backtests` table grows large.
4. **PRD drawdown formula typo** — should be `equity / running_max - 1`, not `min(running_max, equity) - 1`. Fix in the plan (existing `analytics_math.py` code is already correct).
5. **Monthly heatmap: use CSS Grid + Tailwind oklch, not Recharts/lightweight-charts** — Recharts has no heatmap, lightweight-charts is wrong tool. ~60 LOC of React table. Matches project's premium-dark-mode aesthetic.

---

## Summary

```
Research complete.
Libraries researched: 10 (deep) + 5 (noted)
Design-changing findings: 5
Open risks: 10

Key finding: Iframe auth requires a server-side `MSAI_API_KEY` env var on
the frontend container — NEXT_PUBLIC_ vars leak to the browser bundle, and
today's Bearer-token auth model has no way for an iframe `src=` URL to
carry an Authorization header. Streaming 5 MB HTML works cleanly via
Response(upstream.body) Pattern A with explicit runtime='nodejs' +
dynamic='force-dynamic' route segment config.
```

---

## Relevant absolute file paths

- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/pyproject.toml`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/uv.lock`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/frontend/package.json`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/docs/prds/backtest-results-charts-and-trades.md`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/docs/decisions/backtest-results-charts-and-trades.md`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/src/msai/workers/backtest_job.py` (lines 312, 586–630 for `_extract_returns_series`)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/src/msai/services/nautilus/backtest_runner.py` (lines 421–469 for `_compact_account_report`)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/src/msai/services/analytics_math.py` (lines 36–55 for canonical equity/drawdown math)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/src/msai/services/report_generator.py` (lines 20–59 for `_normalize_report_returns`)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/src/msai/api/backtests.py` (lines 410–498 for current `/results` + `/report` handlers)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/frontend/src/lib/api.ts` (lines 12–41 for `apiFetch`, lines 316–340 for wrong `BacktestTradeItem` type)
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/frontend/src/components/backtests/results-charts.tsx`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/frontend/src/components/backtests/trade-log.tsx`
- `/Users/pablomarin/Code/msai-v2/.worktrees/backtest-results-charts-and-trades/backend/alembic/versions/y3s4t5u6v7w8_add_backtest_auto_heal_columns.py` (latest revision, for chaining)

Sources:
- [QuantStats Releases](https://github.com/ranaroussi/quantstats/releases)
- [QuantStats PyPI](https://pypi.org/project/quantstats/)
- [Next.js Route Handlers reference](https://nextjs.org/docs/app/api-reference/file-conventions/route)
- [Next.js Streaming Guide](https://nextjs.org/docs/app/guides/streaming)
- [Next.js ReadableStream Discussion #50614](https://github.com/vercel/next.js/discussions/50614)
- [PostgreSQL 16 Storage TOAST](https://www.postgresql.org/docs/16/storage-toast.html)
- [PostgreSQL 16 ALTER TABLE](https://www.postgresql.org/docs/16/sql-altertable.html)
- [Fast ALTER TABLE ADD COLUMN (depesz)](https://www.depesz.com/2018/04/04/waiting-for-postgresql-11-fast-alter-table-add-column-with-a-non-null-default/)
- [JSONB Size Limits (franckpachot)](https://dev.to/franckpachot/postgresql-jsonb-size-limits-to-prevent-toast-slicing-9e8)
- [TOASTed JSONB compression (credativ)](https://www.credativ.de/en/blog/postgresql-en/toasted-jsonb-data-in-postgresql-performance-tests-of-different-compression-algorithms/)
- [LZ4 TOAST compression (Fastware)](https://www.postgresql.fastware.com/blog/what-is-the-new-lz4-toast-compression-in-postgresql-14)
- [fastapi-pagination GitHub](https://github.com/uriyyo/fastapi-pagination)
- [FastAPI Pagination docs](https://fastapi-pagination.netlify.app/)
- [NautilusTrader Backtesting Concepts](https://nautilustrader.io/docs/latest/concepts/backtesting/)
- [NautilusTrader Backtest API ref](https://docs.nautilustrader.io/api_reference/backtest.html)
- [pandas cumprod reference](https://pandas.pydata.org/docs/reference/api/pandas.Series.cumprod.html)
- [Recharts Performance Guide](https://recharts.github.io/en-US/guide/performance/)
- [Recharts Releases (GitHub)](https://github.com/recharts/recharts/releases)
- [Recharts React 19 issue #4558](https://github.com/recharts/recharts/issues/4558)
- [Recharts 3.0 migration guide](https://github.com/recharts/recharts/wiki/3.0-migration-guide)
- [MDN iframe element](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)
- [Chromium OOPIFs design](https://www.chromium.org/developers/design-documents/oop-iframes/)
- [web.dev Detached window memory leaks](https://web.dev/articles/detached-window-memory-leaks)
- [web.dev iframe lazy-loading](https://web.dev/articles/iframe-lazy-loading)
- [webperf.tips iframe multi-process](https://webperf.tips/tip/iframe-multi-process/)
- [Bytebase ALTER large table in Postgres](https://www.bytebase.com/reference/postgres/how-to/how-to-alter-large-table-postgres/)
- [Alembic issue #272 (JSONB server_default autogen)](https://github.com/sqlalchemy/alembic/issues/272)
