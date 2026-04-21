# Research: Backtest Auto-Ingest on Missing Data

**Date:** 2026-04-21
**Feature:** When backtest fails with `FailureCode.MISSING_DATA`, auto-heal (bounded lazy) re-runs transparently, surfacing `phase="awaiting_data"` + `progress_message` during heal.
**Researcher:** research-first agent
**PRD:** `docs/prds/backtest-auto-ingest-on-missing-data.md`

---

## Summary of Libraries Touched

| Library / API                          | Our Version                                       | Latest Stable      | Breaking Changes Since Ours | Key Source                                                                                                                                       |
| -------------------------------------- | ------------------------------------------------- | ------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `arq`                                  | `>=0.26.0` (lockfile resolves per install)        | 0.27.x             | None relevant to this PR    | [arq docs](https://arq-docs.helpmanual.io/) (2026-04-21)                                                                                         |
| `redis` (`redis.asyncio`)              | `>=5.2.0`                                         | 5.2.x              | None relevant               | Repo: `backend/src/msai/services/live/idempotency.py:460`                                                                                        |
| `pydantic`                             | `>=2.10.0`                                        | 2.10.x             | None relevant               | [Unions docs](https://docs.pydantic.dev/latest/concepts/unions/) (2026-04-21)                                                                    |
| `structlog`                            | `>=24.4.0`                                        | 24.4.x             | None                        | Repo: `backend/src/msai/core/logging.py:54`                                                                                                      |
| `nautilus_trader` (ParquetDataCatalog) | `[ib]>=1.222.0` (project runs 1.223.0 per memory) | 1.22x (API stable) | None                        | `nautilus_trader/persistence/catalog/parquet.py:2162,2177,2192,2236` (vendored in sibling worktree)                                              |
| Databento Historical API               | `databento>=0.43.0`                               | latest             | n/a (pricing policy)        | [Databento pricing](https://databento.com/pricing) + [CME plans blog](https://databento.com/blog/introducing-new-cme-pricing-plans) (2026-04-21) |
| Polygon.io REST                        | Raw httpx caller (no SDK pin)                     | n/a (service)      | n/a                         | [Polygon pricing (redirects to massive.com)](https://polygon.io/pricing) (2026-04-21)                                                            |

---

## Per-Target Analysis

### 1. Provider billing math — Databento + Polygon.io for 10yr × 1min

**Finding.**

- **Databento** bills either (a) pay-as-you-go usage-based ($/GB of _uncompressed binary-encoded_ bytes) or (b) flat-fee subscription plans. For CME futures (`GLBX.MDP3`) the new **Standard plan is $179/month** (introduced 2025 CME pricing). For US equities (`XNAS.ITCH`) the **Standard plan is $199/month** and explicitly grants "unlimited access to the entire history of OHLCV-1s/-1m, definitions, imbalances, statistics, and status data for US equities". For OPRA options the public `list_unit_prices` example shows `ohlcv-1m: $280/GB` historical — options are the expensive case, reinforcing the PRD's hard-reject on options-chain fan-out.
- Databento exposes a **`Historical.metadata.get_cost(dataset, schemas, symbols, start, end)`** API that returns the exact USD cost of a specific query _before_ download — the auto-heal pipeline can emit a best-effort `estimated_cost_usd` structured-log field without incurring the query.
- **Polygon.io** historical plans for stocks: Basic (free, limited), **Starter ~$29/mo** (5yr depth, unlimited calls on paid plans), **Developer ~$79/mo**, **Advanced ~$199–200/mo** (full history, unlimited calls, flat-file S3 access, market events, options expansion). Since 2024-03 **daily historical Flat Files are included at no additional charge on all paid plans** — i.e., once the Advanced plan is paid, OHLCV minute-aggregate pulls are _effectively free_ at the margin (the marginal cost per request is zero within rate limits).
- Options OHLCV via Databento's OPRA.PILLAR at $280/GB is the only regime where per-request cost scales aggressively — and the PRD already hard-rejects options in `AUTO_HEAL_ALLOW_OPTIONS=false`.

**Design impact.**

- **`AUTO_HEAL_MAX_SYMBOLS` default:** cost per additional symbol on the two in-scope asset classes (stocks, futures) is effectively **flat-fee-amortised**, not per-symbol marginal. The binding constraint on symbol count is therefore **wall-clock download time**, not billing dollars — see #2. PRD placeholder default of 20 is reasonable; we should justify it from the wall-clock math rather than billing math. Recommend **20** as the default (covers a diversified portfolio, still bounded enough that one auto-heal cycle stays under the 30-minute cap on a realistic network).
- **Structured log `estimated_cost_usd` field:** call `client.metadata.get_cost(...)` in the auto-heal pipeline before enqueueing the Databento path so the log event carries a best-effort number. For Polygon (flat-files after the monthly subscription) emit `estimated_cost_usd=0.0` with a `billing_mode="flat_fee"` discriminator — truthful and non-misleading.
- **Hard-reject options confirmed correct:** OPRA OHLCV-1m at $280/GB plus Nautilus gotcha #12 (options chain loading explodes) makes options-fan-out unacceptable for unattended auto-heal.

**Test implication.**

- Mock `databento.Historical.metadata.get_cost` in unit tests covering the structured-log event — the real call is network + credential dependent.
- Parametrize a guardrail test asserting that `asset_class == "options"` always returns `guardrail_rejected` regardless of `AUTO_HEAL_MAX_SYMBOLS` / `AUTO_HEAL_MAX_YEARS` values.
- Standard coverage sufficient for billing-path routing (already tested in `DataIngestionService` via `_resolve_plan`).

**Sources.**

1. [Databento pricing page](https://databento.com/pricing) — accessed 2026-04-21 (pay-as-you-go + subscription model confirmed)
2. [Databento CME Standard plan announcement (`$179/month`)](https://databento.com/blog/introducing-new-cme-pricing-plans) — accessed 2026-04-21
3. [Databento Jan 2025 US-equities Standard plan (`$199/month`, unlimited OHLCV-1s/-1m)](https://databento.com/blog/upcoming-changes-to-pricing-plans-in-january-2025) — accessed 2026-04-21
4. [Databento `list_unit_prices` method (OPRA OHLCV-1m = $280/GB example)](https://databento.com/docs/api-reference-historical/metadata/metadata-list-unit-prices) — accessed 2026-04-21
5. [Polygon.io / Massive pricing (Advanced ≈ $200/mo, flat files included since 2024-03)](https://polygon.io/pricing) — accessed 2026-04-21
6. [Polygon.io flat-files S3 KB article](https://polygon.io/knowledge-base/article/how-to-get-started-with-s3) — accessed 2026-04-21

---

### 2. Wall-clock ingest times at current API rate limits

**Finding.**

- **Databento Historical API rate limits (per IP, universal — not tier-gated):** 100 concurrent connections, **100 timeseries requests/sec**, 100 symbology req/s, 20 metadata req/s. No documented size limit per request ("no size limit for either stream or batch download" — batch recommended >5 GB). Throughput is not disclosed in MB/s but is network-bound, not request-rate-bound, for OHLCV-1m requests (one `get_range` call pulls the full window in one response stream).
- **Polygon REST aggregates endpoint** (`/v2/aggs/ticker/{t}/range/{m}/{timespan}/{start}/{end}`): our client (`backend/src/msai/services/data_sources/polygon_client.py:23,73`) uses a **0.25s throttle** (4 req/s) and a **50,000-bar page limit**. One US-equity trading year at 1-minute bars ≈ **98,280 bars** (252 trading days × 6.5 hrs × 60 min) = **2 pages = ~0.5s + network**. Empirically, a 1-year 1-symbol pull completes in a handful of seconds on a healthy network. **10 years × 1 symbol ≈ 20 pages × 0.25s ≈ 5s throttle + network ≈ 30–60s.** 20 symbols × 10 years × 1min ≈ **sequential ~10–20 minutes** at current throttle; could be parallelised to a couple of minutes if needed. Databento is even faster per-symbol (single `get_range` call, bulk-streaming).
- The **30-minute wall-clock cap** therefore has ~1.5–3x headroom over the worst realistic in-scope workload (20 symbols × 10 years × minute). Good safety margin — and the cap is the fail-closed anchor, not the expected runtime.
- Serial polling of the ingest job status (US-006's "reasonable interval, e.g. 10s") is dwarfed by the ingest itself. No concerns.

**Design impact.**

- **30-minute cap is defensible.** Don't tighten below this; a parallel multi-symbol Polygon pull across a year-boundary pagination miss, plus Parquet writes and catalog rebuild, can realistically run 10–15 minutes on the upper end of 20-symbol pulls.
- **Per-symbol concurrency decision** (sequential vs `asyncio.gather` across symbols) — **keep sequential in v1** (current `DataIngestionService.ingest_historical` loops symbols). Rationale: (a) hitting 100 concurrent TCP connections vs `asyncio.gather(20)` is not meaningfully different within the cap, (b) sequential keeps retry/backoff logic trivial and error isolation clean per symbol, (c) we are already far below the 30-min cap. Optimise only if operational data shows the cap is being hit.
- **`AUTO_HEAL_MAX_SYMBOLS=20` default** is consistent with the wall-clock math — at 20 symbols × 10 years × 1 min, we remain inside a single 30-min cap budget with margin for catalog rebuild.

**Test implication.**

- Unit test: simulate a 20-symbol × 10-year pull via a mock Polygon client that sleeps proportional to bar count; assert the auto-heal orchestrator tracks elapsed time correctly against the configurable `AUTO_HEAL_WALL_CLOCK_CAP_SECONDS`.
- Integration test (env-gated, skippable without `POLYGON_API_KEY`): pull a 1-year × 1-symbol real request and record wall-clock to validate the throttle assumption stays true.

**Sources.**

1. [Databento historical API request-limits](https://databento.com/docs/api-reference-historical/basics/request-limits) — accessed 2026-04-21 (100 req/s timeseries, no response-size cap)
2. Repo: `backend/src/msai/services/data_sources/polygon_client.py:23-105` (line 23 = 0.25s throttle constant; line 73 = 50,000 bar page limit)
3. Repo: `backend/src/msai/services/data_sources/databento_client.py:75-82` (single-call `timeseries.get_range`)

---

### 3. arq multi-queue topology — can we add an ingest queue without ops churn?

**Finding.**

- **arq supports multi-queue natively.** Per docs, `WorkerSettings.queue_name` picks the consumer-side queue, and `ArqRedis.enqueue_job(..., _queue_name=...)` routes the producer-side. Default queue is `arq:queue`. One worker → one queue is the documented pattern; an `ArqRedis` pool can enqueue to _any_ queue by passing `_queue_name`.
- **MSAI already has this scaffolding in place:** `backend/src/msai/workers/ingest_settings.py:33-42` defines `IngestWorkerSettings` with `queue_name = "msai:ingest"` and registers `run_nightly_ingest`, and `docker-compose.dev.yml:174-184` has a live `ingest-worker` service already consuming from that queue. Similarly, `research_queue_name` / `portfolio_queue_name` are wired in settings + queue helpers (`backend/src/msai/core/queue.py:111-144`).
- **Gap (critical for this PR):** `backend/src/msai/core/queue.py:147-179` defines `enqueue_ingest(pool, ...)` but does **NOT pass `_queue_name=settings.ingest_queue_name`**. Today `run_ingest` jobs land on the **default queue**, where they are picked up by `backtest-worker` (`WorkerSettings.max_jobs=2`, `max(backtest_timeout, 3600)` job timeout). A long ingest therefore **starves the backtest lane** — the exact Scalability-Hawk objection from the council.
- Second gap: `IngestWorkerSettings.functions = [run_nightly_ingest]` — it does **not** register `run_ingest` (the on-demand path). Both must be registered on the ingest worker.

**Design impact.**

- **Two minimal changes, no docker-compose edit needed:**
  1. `backend/src/msai/workers/ingest_settings.py`: add `run_ingest` to `IngestWorkerSettings.functions` (alongside the existing `run_nightly_ingest`).
  2. `backend/src/msai/core/queue.py:170`: pass `_queue_name=settings.ingest_queue_name` (add the settings field if it doesn't already exist — project already has `research_queue_name` / `portfolio_queue_name` precedents).
- **No new Docker service, no Helm chart change, no prod-topology change.** Existing `ingest-worker` container simply starts consuming the on-demand path it already has the settings for.
- **Risk:** the `WorkerSettings` (backtest worker) currently also has `run_ingest` in its `functions` list (`backend/src/msai/workers/settings.py:124`). Leave it registered for backward compat with any existing enqueued jobs already in flight at migration time, but route all _new_ enqueues to the ingest queue via the `_queue_name` kwarg. After one deploy cycle we can optionally drop `run_ingest` from `WorkerSettings.functions` as a cleanup PR.

**Test implication.**

- Unit test: patch `pool.enqueue_job` and assert `_queue_name=settings.ingest_queue_name` is passed.
- Integration test: enqueue an on-demand ingest through `enqueue_ingest`; assert the ingest-worker container picks it up (use Redis `LLEN arq:queue:msai:ingest` to observe) and the backtest worker's queue remains untouched.

**Sources.**

1. [arq docs — `queue_name` in WorkerSettings and `_queue_name` on enqueue](https://arq-docs.helpmanual.io/) — accessed 2026-04-21
2. [arq GitHub issue #186 — "Let a single worker listen to multiple queues"](https://github.com/samuelcolvin/arq/issues/186) — current model is one worker/one queue, confirmed — accessed 2026-04-21
3. Repo: `backend/src/msai/core/queue.py:147-179` (`enqueue_ingest` bug — missing `_queue_name`)
4. Repo: `backend/src/msai/workers/ingest_settings.py:33-42` (`IngestWorkerSettings` exists but registers only `run_nightly_ingest`)
5. Repo: `docker-compose.dev.yml:174-184` (`ingest-worker` service already running)

---

### 4. `ensure_catalog_data` per-symbol coverage verification

**Finding.**

- `ensure_catalog_data` (`backend/src/msai/services/nautilus/catalog_builder.py:339-396`) is a **batch wrapper** over `build_catalog_for_symbol`. It **does NOT** perform per-symbol time-range coverage checks. It only catches `FileNotFoundError` from `build_catalog_for_symbol` when **zero** raw Parquet files exist under `{raw_parquet_root}/{asset_class}/{raw_symbol}/**/*.parquet` (line 138-142). A partial ingest (e.g., 2024 only when the backtest requests 2022–2024) silently passes — the catalog would be populated with only 2024 and the backtest would run against a truncated universe.
- The builder does have a _source-hash marker_ (`_source_marker_path`) and stale-detection rebuild (line 147-205) to catch "raw tree changed since last catalog build", but that's a _catalog freshness_ check, not a _coverage against the requested date range_ check.
- **NautilusTrader's `ParquetDataCatalog` exposes exactly the primitives needed**, in sibling worktree venv at `.worktrees/strategy-config-schema-extraction/backend/.venv/lib/python3.12/site-packages/nautilus_trader/persistence/catalog/parquet.py`:
  - `query_first_timestamp(data_cls, identifier)` — `parquet.py:2162-2175`
  - `query_last_timestamp(data_cls, identifier)` — `parquet.py:2177-2190`
  - `get_intervals(data_cls, identifier) -> list[tuple[int, int]]` — `parquet.py:2236-2276` (returns (start_ns, end_ns) tuples sorted by start, parsed from filenames matching `{start}-{end}.parquet`)
  - `get_missing_intervals_for_request(start_ns, end_ns, data_cls, identifier) -> list[tuple[int, int]]` — `parquet.py:2192-2234` (returns the gap list; empty list when fully covered, `[(start, end)]` when totally missing)
- **Important caveat:** these APIs inspect **filenames** in the _Nautilus catalog_ (`{catalog_root}/data/bar/{instrument_id}-{bar_spec}/*.parquet`), **not** the _raw OHLCV Parquet tree_ (`{DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet`) that MSAI's ingestion pipeline writes. The MSAI raw-tree layout is YYYY/MM partitioned without embedded timestamps. So a Nautilus-catalog-level coverage check is the right tool _after_ `build_catalog_for_symbol` runs and converts the raw tree to Nautilus format. For the pre-heal "do we have _raw_ data for 2022–2024?" check, the MSAI-native approach is a direct YYYY/MM glob walk.

**Design impact.**

- Coverage verification is a **~30–60 line addition**, not a 200-line refactor — this is the best-case answer the PRD was hoping for. Two layers:
  1. **Raw-tree check** (before ingest): walk `{raw_parquet_root}/{asset_class}/{raw_symbol}/{YYYY}/{MM}.parquet` for every `(yr, mo)` in the requested range; any absent partition triggers "treat as missing — full re-ingest of requested range" per the PRD's deferred partial-range-backfill policy.
  2. **Nautilus-catalog check** (after ingest, before backtest re-spawn): call `catalog.get_missing_intervals_for_request(start_ns, end_ns, Bar, identifier=f"{instrument_id}-{_BAR_SPEC}")` for every instrument in the backtest. If any list is non-empty, classify as `MISSING_DATA` with a specific missing-range `ErrorEnvelope.message` (US-001 edge-case row 2: "Symbol exists at provider but for narrower range than requested").
- **Add a new helper `verify_catalog_coverage(instrument_ids, start, end) -> list[tuple[str, tuple[int,int]]]`** in `catalog_builder.py` that batch-calls `get_missing_intervals_for_request` across instruments. Keep `ensure_catalog_data`'s signature intact (only an additional optional kwarg for the requested window).
- **Nautilus gotcha awareness:** the identifier param must be the full `{instrument_id}-{bar_spec}` BarType string — not just the instrument id — because the catalog partitions bars by bar type.

**Test implication.**

- Unit test 1 (partial raw tree): write partitions for 2024 only, request a 2022–2024 backtest; assert `verify_catalog_coverage` returns the 2022–2023 gap.
- Unit test 2 (full coverage): write partitions for the full range, assert empty gap list.
- Unit test 3 (provider returned narrower range): simulate ingest completing with only 2023+2024 when asked for 2022–2024; assert post-ingest coverage re-check fails with `FailureCode.MISSING_DATA` and a specific 2022 gap in the envelope.

**Sources.**

1. Repo: `backend/src/msai/services/nautilus/catalog_builder.py:138-142` (current existence-only check)
2. Nautilus venv: `nautilus_trader/persistence/catalog/parquet.py:2192-2234` (`get_missing_intervals_for_request`) — accessed 2026-04-21 via `.worktrees/strategy-config-schema-extraction/backend/.venv/lib/python3.12/site-packages/`
3. Nautilus venv: `nautilus_trader/persistence/catalog/parquet.py:2236-2290` (`get_intervals` + `_get_directory_intervals` — filename-scan mechanism)

---

### 5. Redis `SET NX EX` semantics for `redis.asyncio.Redis`

**Finding.**

- `redis.asyncio.Redis.set(key, value, nx=True, ex=TTL)` is a **single atomic `SET key value NX EX ttl`** redis-protocol command. No TOCTOU race. Returns truthy on set-success, `None` when the key already exists.
- **MSAI already uses this pattern in production** at `backend/src/msai/services/live/idempotency.py:460`:
  ```python
  was_set = await self._redis.set(redis_key, marker, nx=True, ex=RESERVATION_TTL_S)
  if was_set:
      return Reserved(redis_key=redis_key)
  ```
  — and at `services/nautilus/disconnect_handler.py:219-220`, `api/live.py:814`. The pattern is well-established in this codebase.
- arq's `ArqRedis` _is_ a `redis.asyncio.Redis` subclass (from `arq.connections`) — same `.set(..., nx=True, ex=N)` signature applies directly when the auto-heal orchestrator reuses the arq pool or opens its own `aioredis.from_url` client.

**Design impact.**

- No new primitives needed. Acquire-lock pattern for the auto-heal dedupe is a direct copy of `IdempotencyStore.reserve`'s one-liner. The watchdog-cleanup contract (US-004 acceptance criterion "watchdog extends to clear stale ingest locks") is subtly different: `SET NX EX` already auto-expires on TTL, so the watchdog's role is only to _observe and emit metrics_ for crashed holders — the TTL handles cleanup. Recommend the watchdog log `auto_heal_stale_lock_cleared` but rely on TTL for the actual release.
- **TTL recommendation:** `AUTO_HEAL_WALL_CLOCK_CAP_SECONDS (1800) + 15min buffer ≈ 2700s`. Round to **3000s (50 min)** matches the PRD's "≥ auto-heal wall-clock cap + buffer (e.g., 45 minutes)" specification.

**Test implication.**

- Copy the lock-acquire / fallback-on-race pattern's unit-test shape from `tests/unit/services/live/test_idempotency.py` (expected to exist given the PR #34/#39 lineage) — parametrize against fakeredis. Standard coverage.

**Sources.**

1. [redis-py `asyncio` docs — `Redis.set(..., nx=True, ex=N)` signature](https://redis.readthedocs.io/en/stable/commands.html#redis.commands.core.CoreCommands.set) — accessed 2026-04-21 (standard redis protocol, single atomic command)
2. Repo: `backend/src/msai/services/live/idempotency.py:442-480` — existing proven pattern
3. Repo: `backend/src/msai/services/nautilus/disconnect_handler.py:217-220` — second proven pattern

---

### 6. `_nightly_if_due` cron reuse path for a future curated-universe seed PR

**Finding.**

- `backend/src/msai/workers/nightly_ingest.py` contains two separable layers:
  - **Scheduler layer (`run_nightly_ingest_if_due`, `_is_due`, `_load_last_enqueued_date`, `_write_last_enqueued_date`)** — tz-aware gate + atomic state-file idempotency. Clean, self-contained, testable; **reusable as-is** if a future curated-universe seed PR wants the "fire once per tz day" semantics.
  - **Workload layer (`run_nightly_ingest`)** — loads `AssetUniverseService.get_ingest_targets`, groups by asset_class, calls `DataIngestionService.ingest_daily(target_date=...)`. Ingests only the single `target_date` session (end-exclusive `[d, d+1)`). **NOT directly reusable** for a "10y × minute historical seed" workload — that PR would want `DataIngestionService.ingest_historical` (range-scoped, same service, different method) with a different target-universe selection + parallelisation strategy.
- There is **no coupling problem** — the two layers are separated by function boundary and can be independently extended.

**Design impact.**

- Out of scope for this PR (the PRD explicitly defers the curated-universe seed). Flag for the follow-up PR:
  > The scheduler layer is reusable as-is. The workload layer needs a new entrypoint (e.g., `run_historical_seed` calling `DataIngestionService.ingest_historical` over the curated universe for the requested range), wired as a separate arq task function with its own idempotency key (probably `"seed_historical:YYYY-MM-DD:{universe_hash}"` rather than the daily date key).
- No change required in _this_ PR.

**Test implication.** N/A for this feature (cross-referenced for the follow-up PR's research phase).

**Sources.**

1. Repo: `backend/src/msai/workers/nightly_ingest.py:57-191` (scheduler layer)
2. Repo: `backend/src/msai/workers/nightly_ingest.py:232-293` (workload layer — note `target_date` single-session shape)
3. Repo: `backend/src/msai/services/data_ingestion.py:70-166` (`ingest_historical` — the range-scoped companion method)

---

### 7. Pydantic v2 `Literal` type for `phase: Literal["awaiting_data"] | None`

**Finding.**

- `Literal[...] | None` is idiomatic Pydantic v2 and the project already uses this pattern:
  - `backend/src/msai/schemas/backtest.py:93` — `kind: Literal["ingest_data", "contact_support", "retry", "none"]`
- Pydantic v2 treats `None | Literal["..."]` equivalent to `Optional[Literal["..."]]` and generates a proper OpenAPI `nullable: true` + `enum: [...]` pair. The OpenAPI output is consumed by the typed-fetch client at `frontend/src/lib/api.ts` — no bespoke parsing needed.
- Single-value `Literal["awaiting_data"]` is technically a degenerate enum. Pydantic still emits it as an enum in the schema; if you want `phase` to grow in the future (`awaiting_backfill`, `awaiting_compile`, etc.), starting with `Literal["awaiting_data"]` is correct — future values are additive without breaking existing clients.

**Design impact.**

- `BacktestStatusResponse.phase: Literal["awaiting_data"] | None = None` is correct. No exotic discriminated-union needed in v1. Match the precedent from PR #39's `Remediation.kind`.
- `progress_message: str | None = None` — plain string, sanitized via the existing `sanitize_public_message` pipeline (PRD §7 Security). No further Pydantic validation needed.

**Test implication.**

- Round-trip test: `BacktestStatusResponse.model_validate({"phase": "awaiting_data", "progress_message": "..."})` succeeds; `{"phase": "bogus"}` raises ValidationError; `{"phase": None}` + `{"phase": absent}` both parse to `None`.
- OpenAPI schema snapshot test: after adding the field, assert `components.schemas.BacktestStatusResponse.properties.phase` has the expected nullable-enum shape so the frontend type-generator doesn't silently regress.

**Sources.**

1. [Pydantic v2 Unions concepts](https://docs.pydantic.dev/latest/concepts/unions/) — accessed 2026-04-21
2. Repo: `backend/src/msai/schemas/backtest.py:93` — project precedent

---

### 8. structlog event-name contract stability

**Finding.**

- **No centralized event-name registry exists in the codebase.** `structlog.BoundLogger` accepts any string as the event name; conventions are enforced only by code-review and grep. 67 `log.(info|warning|error)("…")` call sites across 31 files were surveyed. Naming convention is `snake_case_noun_verb` (`backtest_job_started`, `ingest_enqueue_failed`, `nautilus_catalog_built`, `backtest_missing_data`, `daily_ingest_firing`, etc.).
- **No collisions** for the 7 proposed names: `backtest_auto_heal_started` / `_completed` / `_ingest_enqueued` / `_ingest_completed` / `_ingest_failed` / `_guardrail_rejected` / `_timeout`. None of these strings appears anywhere in `backend/src/msai/**` (confirmed by grep — 12 matches of `backtest_auto_heal` occur only in the PRD file).
- Nearest existing events for grep-continuity: `backtest_missing_data`, `backtest_job_started`, `backtest_job_completed`, `backtest_job_failed`, `ingest_enqueue_failed`, `nautilus_catalog_built`. Keeping the `backtest_auto_heal_*` prefix is distinct enough for log filtering and consistent with the `{subsystem}_{action}` shape.
- structlog is configured in `backend/src/msai/core/logging.py:31-75`: dev → `ConsoleRenderer(colors=True)`, prod → `JSONRenderer`. `format_exc_info` processor is in the chain, so `log.exception("...")` attaches tracebacks. `merge_contextvars` is the first processor, so ambient `request_id` / `deployment_id` bound via `bind_contextvars` auto-flows into auto-heal log events. **Intended side-effect:** auto-heal events triggered inside an HTTP request will carry the `request_id` of the originating `POST /backtests/run` — excellent for post-hoc audit.

**Design impact.**

- No contract to violate. Adopt the 7 proposed event names verbatim.
- Bind `backtest_id` + `asset_class` at the auto-heal orchestrator entry with `structlog.contextvars.bind_contextvars` so every subordinate event inherits them (avoids repetition in each `log.info(...)` call).
- `log.exception(...)` (with `exc_info=True` via the `format_exc_info` processor) is already wired — use it for `backtest_auto_heal_ingest_failed` to attach the provider stack trace without manual formatting.

**Test implication.**

- Structured-log assertion tests (using `structlog.testing.capture_logs()`) should assert event-name presence + required field presence (`backtest_id`, `ingest_job_id`, `wall_clock_seconds`, etc.) for each of the 7 events. Field-value wildcard assertions for anything timestamp-like.
- No cross-file contract test needed (no registry to break).

**Sources.**

1. Repo: `backend/src/msai/core/logging.py:31-75` (structlog setup + processor chain)
2. Repo (grep evidence): no `backtest_auto_heal` matches in `backend/src/msai/**` (only PRD mentions)
3. [structlog docs — `contextvars.bind_contextvars` + `testing.capture_logs`](https://www.structlog.org/en/stable/contextvars.html) — accessed 2026-04-21

---

## Cross-Cutting Findings (discovered en route)

### Classifier-side asset_class regression risk (US-005)

`backend/src/msai/services/backtests/classifier.py:86-96` contains an explicit deferred-followup note: the UI's Run Backtest form does not currently send `config.asset_class`, worker defaults to `"stocks"`, so a futures-backtest via UI today produces `"msai ingest stocks ES.n.0 ..."` — the known buggy remediation. The PRD's US-005 (server-derived asset_class) closes this. Research confirms the fix is _additive_: call `SecurityMaster.resolve_for_backtest(symbols, start=...)` in the classifier path (`backend/src/msai/services/nautilus/security_master/service.py:343`) and map the returned canonical IDs to asset_class via `_asset_class_for_instrument` or `_spec_from_canonical` (service.py:540-559, 561-635). The registry is already the single source of truth post-PR #32/#35.

### arq job-registration drift

Registering `run_ingest` on _both_ `WorkerSettings.functions` (default queue) and `IngestWorkerSettings.functions` (ingest queue) is the safe zero-downtime migration. Document this in the plan: "register on both for one deploy cycle; drop from default-queue WorkerSettings in a follow-up cleanup PR after confirming no stale enqueued-but-unexecuted jobs remain on the default queue."

### Nautilus gotcha #8 still relevant

Per `.claude/rules/nautilus.md` gotcha #8: "Backtest `MessageBusConfig.database` will pollute production Redis." Auto-heal itself doesn't touch MessageBus directly, but the auto-heal orchestrator runs inside the backtest-worker process (before the Nautilus subprocess spawns). No new exposure — but if auto-heal ever starts a temporary Nautilus context to introspect the catalog, remember to keep `MessageBusConfig.database=None` in that path.

---

## Not Researched (with justification)

- **`quantstats`, `ib_async`, `httpx`, `sqlalchemy`** — not touched by auto-heal logic. Auto-heal operates _before_ the NautilusTrader subprocess spawns and does not modify reporting, IB connectivity, HTTP clients, or ORM models beyond the additive 4-column migration (which is a simple nullable-column add — standard SQLAlchemy 2.0 / Alembic pattern, no research gap).
- **`@azure/msal-browser` / `@azure/msal-react`** — frontend auth unchanged; the UI progress indicator (US-002) is an additive display-only change rendering `status.phase` + `status.progress_message` from the existing `/status` endpoint. No auth surface change.
- **`lightweight-charts`, `recharts`** — no chart changes. Progress indicator is a text badge + icon, no chart.
- **`typer`** — CLI surface unchanged. `msai ingest` still takes explicit `stocks|futures|options` positional; this PR only changes the auto-derivation path inside the worker/classifier.
- **`alembic`** — the 4-column-add migration pattern is identical to PR #39's (nullable, no default required, Postgres 16 fast-path). No version/API research needed.
- **`fastapi`, `starlette`** — endpoint contracts unchanged. Only `BacktestStatusResponse` Pydantic shape grows (backward-compatible additive fields).

---

## Open Risks

1. **Databento Standard plan feature-gate assumption.** We assume the operator's Databento subscription includes unlimited OHLCV-1m access for the in-scope datasets (XNAS.ITCH + GLBX.MDP3). If they are on pay-as-you-go, 10y × 1min × 20 symbols could cost real money per auto-heal — though still bounded because both datasets are flat-fee-cheap per-GB for OHLCV. **Mitigation:** emit `estimated_cost_usd` from `metadata.get_cost` in every `backtest_auto_heal_started` log event so cost drift is observable.
2. **Polygon.io / Massive.com domain redirect.** Polygon.io redirects to massive.com for pricing + flat files. This is a rebrand-in-progress, not a deprecation, but the project's `POLYGON_API_KEY` continues to work against `api.polygon.io`. Monitor for API deprecation announcements on the blog; no action needed now.
3. **Nautilus `get_missing_intervals_for_request` uses filename parsing.** If the MSAI catalog-builder ever writes files that don't match the `{start_ns}-{end_ns}.parquet` convention (e.g., a future custom partitioning scheme), `get_intervals` returns an empty list and coverage checks silently pass. **Mitigation:** a defensive test that writes a known bar window via `catalog.write_data` and asserts `query_first_timestamp` returns the expected value — catches filename-format drift.
4. **`asyncio.set_event_loop_policy(None)` in ingest_settings.py (Nautilus gotcha #1).** Already correctly placed AFTER the nautilus-transitive imports. Do not let the auto-heal patch move it. Add a module-level docstring comment if new imports are added near it.
5. **arq cron re-firing during a long ingest.** `cron_jobs = [_cron(_nightly_if_due, minute=None, second=0), _cron(_watchdog, minute=None, second=0)]` — these fire every minute. The nightly-ingest wrapper already has tz-idempotency protection, but the auto-heal watchdog hook should never get scheduled on a 1-minute cadence against in-flight locks — use the existing `job_watchdog` framework rather than adding a second cron.
6. **"Test environment relies on sibling worktree's venv."** The Nautilus library source I cited (`.worktrees/strategy-config-schema-extraction/backend/.venv/...`) is not in the current worktree's venv (this worktree may not have `uv sync`'d yet). The _API_ is stable across Nautilus 1.222–1.223, so the citation is correct; the plan should run `uv sync` in this worktree during Phase 3 setup to have a local copy.

---
