# Research, data, backtest, and strategy audit

Audit date: 2026-10-03. Source revision: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. Initial working tree was clean. This is an assessment, not an implementation or trading-readiness certification.

The intended product is Python-authored strategies, roughly 100 symbols, minute/daily historical data across stocks/indexes/futures/options/some crypto, 5–10-minute or daily decisions, Nautilus backtest/live consistency, and strong protection against overfitting.

**Assessment:** keep the Nautilus/Parquet/DuckDB foundation and the asynchronous execution boundary. The current source implements a substantial control plane and real research execution, but it does not yet provide a trustworthy general multi-asset, multi-frequency research pipeline. The most important repair work is instrument/data identity, statistical selection boundaries, and return accounting. Adding optimization methods or operational abstractions before those repairs would amplify incorrect answers.

## Scope and method

Read `docs/agent-context.md` and `.forge/instructions.md` completely, inspected workflow state, then followed source and unit tests through the owned paths. Read-only application audit; no vendor, broker, external database, migrations, running services, or secrets were accessed. Python checks used the existing `backend/.venv`, a fresh temporary working directory, temporary `DATA_ROOT`, and a socket-connect denial guard. No dependencies were installed. Only this audit document was intentionally added to the repository.

The memory search found only prior Forge migration records, which were not used for any substantive assessment. All capability and defect conclusions below come from the current checkout and explicitly described offline probes.

## Actual module map

| Area | Actual implementation and flow | Assessment against the goal |
| --- | --- | --- |
| Historical ingestion | `services/data_ingestion.py:73` resolves a provider plan, downloads each symbol serially, normalizes OHLCV, writes raw Parquet, then calls `ensure_catalog_data` at line 175. Databento bars are fetched at `services/data_sources/databento_client.py:89`; Polygon at `data_sources/polygon_client.py:38`. | Real ingestion exists. Databento is selected automatically only for equities/futures (`data_ingestion.py:285`); other assets route to Polygon. No default Databento options dataset exists here. |
| Raw storage | `services/parquet_store.py:69` merges monthly files, deduplicates by timestamp, and uses atomic rename through `core/data_integrity.py:20`. Layout is `{asset_class}/{symbol}/{year}/{month}.parquet` (`parquet_store.py:264`). | Sensible local storage choice; missing feed, interval, adjustment, and instrument identity dimensions are material. Atomic replacement does not serialize concurrent read/merge/write operations. |
| Query | `services/market_data_query.py:29` uses a bound-parameter DuckDB query over Parquet. `get_symbols`/`get_storage_stats` scan the filesystem. | Good fit for local analytical access. `interval` is ignored and same-symbol asset-class selection takes the first directory found (`:142`). |
| Instrument registry | `services/nautilus/security_master/service.py:428` resolves historical aliases, with date-windowed non-continuous aliases and a Databento continuous path at `:673`. `specs.py:45` models equity/future/option/forex/index; `continuous_futures.py:76` synthesizes continuous metadata. | Useful control-plane work. Actual catalog conversion does not consume these resolved instrument definitions. Crypto is absent from the logical `InstrumentSpec` union even though other taxonomies admit it. |
| Catalog | `services/nautilus/catalog_builder.py:64` reads raw files in 100,000-row batches, creates a Nautilus instrument, writes bars, and maintains a source-change marker. `ensure_catalog_data` at `:353` is shared by ingest, single backtests, research, and portfolio backtests. | Native catalog reuse and streaming are valuable. It is currently an equity/one-minute conversion path regardless of input asset or schema. |
| Single backtest | `api/backtests.py:351` resolves registry IDs, stores them at `:392`, and enqueues a job. `workers/backtest_job.py:201` reads those strings, builds the catalog at `:331`, captures lineage, and calls `BacktestRunner` at `:369`. | Actual Nautilus execution, not a hand-coded price loop. Canonical string resolution alone does not preserve contract metadata or the original raw symbol. |
| Nautilus execution | `services/nautilus/backtest_runner.py:165` spawns a fresh process, bounds runtime, receives reports through a temporary result file; `:382` constructs `BacktestNode` config. | Process isolation and bounded jobs are justified. One million USD is hardcoded per venue (`:64`, `:405`); the builder exposes no commission/slippage/latency parameters. Fidelity to the user's actual account/cost model is unverified. |
| Research | `api/research.py:54` / `:94` submit sweeps/walk-forward jobs. `workers/research_job.py:154` builds the same catalog; `services/research_engine.py:534` performs grid/halving/Optuna search and `:764` performs rolling/expanding walk-forward evaluation. | Real machinery exists. The statistical meaning of “holdout” and “OOS” is weaker than the names imply; see findings. |
| Graduation | `services/graduation.py:79` creates a candidate and validates strategy existence/halt-API lint; `:157` enforces legal stage transitions and records audit rows. `api/research.py:251` promotes completed-job results. | It is an audited state machine, not a quantitative acceptance gate. Live safety checks elsewhere must not be confused with statistical validation. |
| Portfolio research | `services/portfolio/orchestration.py:246` runs each member separately, combines return series, and reports metrics. Full mode at `:673` caches each member's full-range return stream, then invokes `portfolio_backtest/optimizer.py:93`. | Useful analytical composition, but not a shared-account multi-strategy Nautilus simulation. It does not prove shared buying power, order interaction, cross-strategy netting, or live sizing parity. |
| Analytics | `services/analytics_math.py:28` normalizes daily returns, `:119` computes metrics, `:191` combines weighted series. `backtest_runner.py:441` compacts account reports; `portfolio_backtest/allocators.py` provides equal/fixed/inverse-vol/vol-targeted allocation. | Several deterministic accounting defects undermine return/drawdown evidence; these should be repaired before using optimized metrics for selection. |
| Strategies | Eight tracked Python files in `strategies/`, including two package initializers, EMA config, EMA cross, smoke order, two cycling test strategies, and an intentionally failing strategy. EMA subscribes to one instrument/bar type (`strategies/example/ema_cross.py:74`, `:104`) and uses fixed quantity (`:188`). | Python strategy authoring is real. The shipped examples are largely operational smoke fixtures. No tracked slope/linear-regression strategy implementation was found in `strategies/` or `backend/src/msai/`. No 100-symbol strategy or throughput demonstration was performed. |

Paths in this table are relative to `backend/src/msai/` unless they begin with `strategies/` or another root directory.

## Findings

Severity is assessed against the requested research/trading goal, not a claim that a production loss has occurred. P1 means results or supported behavior should not be relied on until repaired; P2 means a concrete correctness/efficiency gap with narrower reach. “Reproduced” means a tiny offline helper/flow probe, not a running API-to-broker test.

### RD-01 — P1, high confidence: registry-backed requests still become synthetic equities

The reachable path is `api/backtests.py:351–355` → persisted canonical ID strings at `:392` → `workers/backtest_job.py:201` → `_execute_backtest` at `:331–336` → `catalog_builder.py:124` → `instruments.py:23–41`. The final helper always calls `TestInstrumentProvider.equity`. Registry multiplier, tick size, expiry, option right, and actual instrument class are not passed through.

Offline outputs:

```text
ESM6.CME       -> Equity, raw_symbol ESM6, multiplier 1
ES.c.0.GLBX    -> Equity, raw_symbol ES,   multiplier 1
BTCUSD.COINBASE -> Equity, raw_symbol BTCUSD, multiplier 1
```

The continuous-symbol case has an additional reachable path failure: writing raw bars under `futures/ES.c.0`, then calling the actual catalog builder with `ES.c.0.GLBX`, raises `FileNotFoundError` for `futures/ES`. The helper splits at the first dot (`instruments.py:35–37`); the builder derives its raw storage lookup from that equity (`catalog_builder.py:133–141`). The worker does not provide `raw_symbol_override`.

**Repair:** transport and write the exact resolved Nautilus instrument definition, preserve the requested/raw storage identifier separately, and add real futures/options/crypto catalog fixtures. Keep registry normalization; remove this production use of test instruments. For continuous futures, separately define roll/adjustment/execution-contract policy: alias metadata is not a roll simulation.

### RD-02 — P1, high confidence: daily and minute data can collide, and daily data is mislabeled as one minute

Ingest accepts a schema override (`data_ingestion.py:80–82`, `:289`), but raw storage has no schema/dataset/provider/adjustment component (`parquet_store.py:264–266`); it deduplicates only timestamps (`:102–117`). Normalization drops every column other than OHLCV/timestamp (`data_ingestion.py:569`). The catalog hardcodes `1-MINUTE-LAST-EXTERNAL` (`catalog_builder.py:48–50`, `:230–255`). Query's `interval` parameter is never used (`market_data_query.py:29–71`).

Offline probes reproduced a second same-symbol bar replacing close 100 with 999 at the same timestamp while a neighboring minute bar remained, and identical results for `interval='1m'` versus `'1d'`. Two daily input rows were emitted as `DAILY.NASDAQ-1-MINUTE-LAST-EXTERNAL` bars.

This also conflates provider/dataset changes. Polygon explicitly requests adjusted bars (`polygon_client.py:68–73`), yet raw storage keeps no adjustment/feed provenance to distinguish them from a different feed.

**Repair:** give raw series an explicit identity including instrument, provider/dataset, interval and adjustment policy; reject incompatible writes. Use one deliberate aggregation path for 5/10-minute and daily bars, with matching historical/live timestamps and sessions. Preserve the correct bar type through catalog and strategy configuration. Do not claim the current interval argument implements aggregation.

### RD-03 — P1, high confidence: “holdout” is optimized and failed validation can still produce a winner

For grid/halving, every survivor is evaluated on the holdout and its `metrics` replaced with holdout metrics before ranking (`research_engine.py:661–708`). Optuna is stronger contamination: holdout metrics become the objective supplied to `study.tell` (`:1279–1317`), so the sampler adapts directly to this purported holdout. This can be a validation set, but not an untouched final test.

The fallback in `_select_best_result` omits the `holdout_error` check (`:1372–1396`). A tiny stub run in which all holdouts raise `RuntimeError('holdout unavailable')` still returned a non-null `best_result` carrying that error.

The same probe requested `min_trades=100` and `require_positive_return=True`; grid selected a candidate with one trade and -10% total return. Those acceptance knobs are applied in successive-halving eligibility (`:312`) but are only passed/reported in the other branches, not enforced at final selection. Optuna similarly receives them without enforcing them in its trial loop.

**Repair:** use train/validation for selection and an untouched outer test for evidence; score only the train-selected winner on each outer test. Apply final eligibility across every search method and fail closed when required validation fails. Record selection count and tested variants; a time split alone does not correct repeated search or analyst reuse of the same test.

### RD-04 — P1, high confidence: portfolio OOS allocation uses future returns and selects on OOS

`portfolio_backtest/optimizer.py:239–248` invokes the same trial function on the test window. `_aggregate_returns_trial` slices test-window returns (`portfolio/orchestration.py:1808–1821`) and calls the allocator on those returns (`:1832–1838`). Inverse-vol and vol-targeted therefore estimate their weights using the very test returns they are supposed to evaluate.

The offline counterexample used two series whose volatility ranking reverses between train and test. The actual trial helper produced:

```text
train weights: A=0.909091 B=0.090909
test weights:  A=0.090909 B=0.909091
```

Thus test-period volatility was known before its weights were applied to that same period. Additionally, the selected configuration maximizes `min(is_score, oos_score)` across trials/windows (`optimizer.py:275–283`). The displayed IS/OOS scores are averages over all successful trial evaluations (`:259–260`, `:323–330`), not a stitched return stream of train-selected winners. These are selection diagnostics, not evidence of one implementable walk-forward policy.

**Repair:** freeze allocation weights from training information and carry them into the next test segment (or use an explicitly causal rolling allocator). Select parameters only inside training; concatenate actual outer-test returns and report their metrics. Preserve the cheap cached-return optimization only where it faithfully represents the intended policy.

### RD-05 — P1, high confidence: return and drawdown calculations have deterministic accounting errors

`backtest_runner.py:490–495` keeps each day's last equity, then computes percent change between daily endpoints and fills the first return with zero. A one-day equity path 100 → 110 produces equity 110 and return 0, dropping that day's gain from downstream reports/portfolio optimization.

For multiple accounts, `:480–485` sums only the accounts with an event at each exact timestamp; it does not carry other account balances forward. The offline example starts A=B=100, then next day A=101 at noon and B=102 at 13:00. True portfolio total is 200 → 203; compaction produced 200 → 102 and -49%. This is a proven helper defect; no multi-venue Nautilus execution was run to quantify its occurrence in a real report.

`analytics_math.py:106–107` and `:143–144` omit starting capital from the running high-water mark. Returns [-10%, 0%] produce `max_drawdown=0.0`, rather than -10%. Downside limits/objectives that consume this metric inherit the understatement.

**Repair:** include the starting-capital baseline, align/forward-fill accounts before summing their equity, and compute drawdown relative to the initial high-water mark. Prove full account NAV/mark-to-market semantics separately: the current compactor accepts fields such as `balance_total`/`total` as equity (`backtest_runner.py:467–469`); this audit did not establish that those fields include unrealized P&L for each supported instrument/account model.

### RD-06 — P1, high confidence: declared inclusive backtest end date becomes midnight

The payload documents the backtest end as inclusive (`backtest_runner.py:137–139`). Workers pass `end_date.isoformat()` unchanged (`workers/backtest_job.py:202–203`); run and data configs both receive it (`backtest_runner.py:417–430`). The installed Nautilus `BacktestDataConfig.end_time_nanos` decoded the sample `2024-01-02` as `2024-01-02 00:00:00+00:00`, not the end of that day.

Consequently ordinary intraday bars on the requested final date lie beyond the configured bound. This affects research train/test windows too, because they pass date-only strings through the same runner. Ingestion already adds a provider-specific end-day translation (`data_ingestion.py:334–346`), which does not fix the runner bound.

**Repair:** establish one explicit inclusive-date-to-timestamp translation at the execution boundary, and verify a last-session bar is consumed. This audit proved the config timestamp, not a complete Nautilus trade-path reproduction.

### RD-07 — P2, high confidence: graduation records stage changes without quantitative evidence

`GraduationService.update_stage` checks only existence and allowed transition membership (`graduation.py:167–191`). A typed offline `AsyncSession` mock with `metrics={}`, no research job and no deployment advanced through all five legal transitions from discovery to live_candidate, producing five audit rows. `create_candidate` accepts caller metrics and requires only that a referenced research job exists (`:118–131`).

Research promotion checks completed status and presence of a best result (`api/research.py:264–274`); selecting a specific trial substitutes its config/metrics without requiring successful trial status (`:279–293`).

This does **not** prove that such a candidate can execute live: snapshot/risk/account checks exist in other owners' scope. It does prove the graduation stage itself is not evidence of overfitting protection, minimum trades, minimum observation duration, untouched-test success, or economic viability after costs.

**Repair:** bind graduation to an immutable evaluated code/config/data identity and explicit evidence requirements. Keep the stage audit trail; distinguish human sign-off from automated quantitative checks.

### RD-08 — P2, high confidence: atomic Parquet replacement can still lose concurrent ingestion

`parquet_store.py:110–117` reads the current file, merges, then replaces it without a per-partition lock or compare-and-swap. Two concurrent writers can each read the same old file and overwrite the other's new rows. A barrier-controlled two-thread offline probe started with day 1, concurrently appended days 2 and 3, and ended with two rows rather than three; both writes returned successfully.

The dedicated ingest worker is single-job (`workers/ingest_settings.py:49–53`), which reduces this risk, but the generic worker also registers `run_ingest` with two jobs (`workers/settings.py:128–131`) and CLI/service entry points exist. The serial prewarm within a single portfolio run (`portfolio/orchestration.py:397–427`) likewise does not serialize different jobs against the shared catalog. Shared catalog purge/rebuild in `catalog_builder.py:190–197` lacks a reader snapshot boundary.

**Repair:** serialize writes per data-series/partition across all ingestion entry points, and publish catalog generations atomically for readers. Keep the atomic file primitive; it solves partial-file visibility, a different problem.

### RD-09 — P2, high confidence: coverage and lineage are useful diagnostics, not exact research evidence

Coverage infers every trading day between a partition's min/max timestamps (`symbol_onboarding/coverage.py:238–275`), explicitly missing internal gaps. It grants seven recent trading days of tolerance (`:50`, `:128–140`). Crypto's calendar falls back to weekdays (`trading_calendar.py:96–100`), excluding Saturday/Sunday. These cannot establish a complete minute/daily research sample for the requested asset mix.

Lineage `describe_catalog` uses name/size/mtime, a 16-character hash, and ignores the requested date range (`catalog_builder.py:475–522`). The actual cache invalidation marker also samples file edges instead of hashing all content (`:275–313`). Backtest strategy hash is computed when enqueuing (`api/backtests.py:319–327`) but the worker imports a mutable file path later (`backtest_runner.py:389–395`), without validating that the bytes still match. The hash covers one file only (`nautilus/strategy_hash.py:46–73`), not imported helpers.

Optuna journals also reuse insufficient identities: strategy study names exclude code/config/data/search-space fingerprints (`research_engine.py:403–425`); portfolio names include only portfolio ID/objective (`portfolio_backtest/optimizer.py:157–166`), reusing history across windows and changed runs.

**Repair:** separate inventory health from research sample validation; record exact series/version/content identity and immutable strategy bundle, validate required bars/sessions, and scope studies to immutable experiments. Keep cheap cache fingerprints for cache use, but do not advertise them as reproducible snapshots.

### RD-10 — P2, high confidence: research reserves parallel capacity but executes serially; cancellation waits for the whole experiment

Research jobs reserve `requested_parallelism` slots (`workers/research_job.py:138–152`), but `_run_candidates` is a serial list comprehension (`research_engine.py:979–1004`) and Optuna's loop is serial too. `_resolved_parallelism` (`:499`) is not used. The worker sets a cancellation event on heartbeat (`workers/research_job.py:118–131`) and checks it only after the engine returns (`:259–269`); the engine receives no cancellation callback. A canceled long sweep can continue using resources until every remaining trial finishes.

Databento bar ingestion is also an `async` function calling the synchronous SDK directly (`databento_client.py:127–141`), unlike the definition fetch's `asyncio.to_thread` + retry (`:216–225`). Single backtest catalog construction is synchronous in the async worker (`backtest_job.py:331`), potentially stalling heartbeat renewal while rebuilding. Catalog cache-hit checks materialize all existing bars (`catalog_builder.py:173`, `:208`), despite having a lighter interval approach elsewhere (`:420`). Raw `ParquetStore.read_bars` loads every historical partition before filtering (`parquet_store.py:185–197`).

**Simplify/repair:** make declared resource limits match execution; reserve one slot while serial or implement bounded parallelism, and propagate cancellation between trials. Move blocking work off the event loop. Use metadata/projection/date filtering for cache checks and raw reads. Benchmark the corrected 100-symbol workload before introducing distributed compute.

## Complexity judgment and priorities

**Keep:** Python-native strategies; Nautilus for event/order/position semantics; Parquet as local historical storage; DuckDB for analytical reads; PostgreSQL for durable job/config/audit metadata; asynchronous background jobs; bounded child-process execution; instrument registry; explicit failure reporting and stage audit history. Each has a concrete role for this product.

**Simplify:** choose one default research recipe, initially a small grid with train-selected outer walk-forward winners. Treat Optuna/halving as optional accelerators after statistical validation. Consolidate repeated config/canonicalization/returns adapters; the source contains multiple layers that preserve similar data differently. Keep a single authoritative bar/instrument identity rather than adding more alias repair paths around the equity helper. Separate analytical portfolio composition from execution-accurate portfolio backtesting in names and acceptance criteria.

**Repair before relying on research:** RD-01 through RD-06, then graduation/evidence requirements and dataset completeness. These change the meaning of results, not cosmetic maintainability. For intraday economic viability, add explicit commission/slippage/spread/latency assumptions and validate against observed fills; this audit did not establish existing costs are zero, only that the production config builder does not expose or pin a realistic cost model.

**Defer until evidence supports it:** more search algorithms, distributed workers, large-universe UI, additional asset/provider abstraction, and claims of full multi-asset live parity. Defer the capability claim, not a known defect in a supported path. A bounded end-to-end vertical slice should first prove real instrument metadata, exact input bars, 5/10-minute aggregation, costs, untouched evaluation, and a paper/live comparison on identical strategy/config semantics.

Portfolio Full mode currently optimizes aggregate leverage/concentration over cached independent member returns, not the strategy parameters, and it does not rerun a shared-account Nautilus portfolio for each selected policy (`optimizer.py:64–80`; `orchestration.py:770–864`). That cheap calculation is justified for exploratory allocation analysis. It is overclaimed if treated as a complete deployable portfolio backtest. Statistical simplification would currently add more value than another layer of optimizer orchestration.

## Executed validation and exact limits

Source inspections used `rg`, `nl`, `sed`, `wc`, `git ls-files`, `git status --short`, and `git rev-parse HEAD`. No exhaustive suite, lint sweep, services, migrations, live endpoints, or external research was run by this audit owner. Parent owns external-library/alternative-platform research.

Focused pytest call, from a temporary directory with `PYTHONPATH` pointing at this checkout:

```text
backend/.venv/bin/python -> pytest.main([
  '-q', '-p', 'no:cacheprovider',
  '<repo>/backend/tests/unit/test_research_engine.py',
  '<repo>/backend/tests/unit/test_parquet_store.py',
  '<repo>/backend/tests/unit/test_market_data_query.py',
  '<repo>/backend/tests/unit/test_graduation.py',
  '<repo>/backend/tests/unit/services/portfolio_backtest/test_optimizer.py'
])
89 passed, 7 warnings in 0.84s
```

Warnings: one FastAPI 422 constant deprecation and six Optuna `constant_liar` experimental warnings. Import initialization took longer than the test timer. The fixtures use temporary files, mock sessions, stub runners and real local Optuna journals; they do not prove production statistical validity.

Three one-shot offline probe programs additionally exercised: instrument resolution (three symbols); same-symbol overwrite; ignored query interval; daily catalog labeling; holdout selection and ignored eligibility; failed-holdout fallback; train/test allocator weights; graduation without evidence; first-day return compaction; asynchronous-account compaction; initial drawdown; concurrent writer lost update; installed Nautilus end-date decoding; and the continuous raw-symbol catalog failure. All output is recorded in the relevant findings above. The graduation probe was repeated using `AsyncMock(spec=AsyncSession)` and a synchronous `add` mock after an initial untyped mock emitted a coroutine warning; the corrected probe produced the same five transitions without that warning.

Representative counterexamples are intentionally tiny:

```python
# Actual production helpers, synthetic inputs, no vendor/broker/DB calls.
compute_series_metrics(pd.Series([-0.1, 0.0], index=pd.date_range('2024-01-01', periods=2))).max_drawdown
# observed 0.0; initial capital baseline requires -0.1

_compact_account_report(pd.DataFrame({
    'timestamp': pd.to_datetime(['2024-01-02T09:00Z', '2024-01-02T16:00Z']),
    'equity': [100.0, 110.0],
}))
# observed one daily row with equity=110, returns=0.0
```

Unverified: live broker/vendor entitlements; available real data completeness; production registry content; true futures/options execution economics; actual fill/mark-to-market behavior; API/CLI/UI end-to-end state; 100-symbol throughput/memory envelope; strategy-specific economic edge; detailed dollar impact of any finding. The current runtime was not started. No production-readiness or profitability conclusion is supported by the passing unit tests.
