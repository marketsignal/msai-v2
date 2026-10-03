# Bounded local research journey — execution plan

Prepared 2026-10-03 from current source and existing local market-data metadata. This document is a plan, not evidence that the workflow has run. The research/data audit agent performed no API requests, service startup, queue submission, database changes, ingestion, vendor calls, broker calls, or source changes while preparing it. Root owns execution and should record observed responses separately.

## Intended proof and cost

Use one existing Python EMA strategy and existing AAPL minute bars. Discover/register the strategy through the public API, validate it, run one real Nautilus backtest, inspect trades and report, then execute a two-combination parameter sweep. Budget: three engine runs, one symbol, three market sessions, one research compute slot. A successful journey proves wiring and persistence; three sessions do not establish investment merit, statistical validity, robust overfitting protection, or live/backtest parity.

Use the real EMA strategy, not an intentional-failure or special smoke fixture. `GET /api/v1/strategies/` performs discovery and database upsert; there is no separate strategy-creation POST in this journey. Select the response item whose `strategy_class` is `EMACrossStrategy` and whose file is the tracked example EMA implementation. Capture its returned UUID and `code_hash`; do not assume an existing database ID. Sources: [strategy discovery API](/Users/pablomarin/Code/msai-v2/backend/src/msai/api/strategies.py:41), [EMA config](/Users/pablomarin/Code/msai-v2/strategies/example/config.py:10).

## Startup conditions and side effects

Root should use explicit Compose service names: `postgres`, `redis`, `backend`, `backtest-worker`, `research-worker`, and optionally `frontend`. Existing persistent PostgreSQL state is required unless root separately resolves schema initialization; the backend image command does not automatically apply migrations. Do not recreate database volumes to make an audit pass.

| Surface | Source-confirmed behavior | Execution control |
| --- | --- | --- |
| Backend startup | Ensures an API-key user in the local DB; starts state projections for existing active deployments; initializes credentials store; starts IB probe and account-snapshot tasks unconditionally. | Development mode; no broker services; empty `GATEWAY_CONFIG`; explicitly isolate broker host/port to unreachable container-local loopback. Inspect local active deployment state before treating startup as idle. |
| `backtest-worker` | Generic worker can execute `run_backtest` **and** `run_ingest`; cron also runs nightly ingestion, PnL aggregation, and watchdog cleanup. | Set `DAILY_INGEST_ENABLED=false` in the actual container environment. Observe queued work before starting; old queued ingestion is not disabled by the nightly flag. |
| `job-watchdog` | Runs exactly the same `WorkerSettings` as `backtest-worker`, including job consumption and cron. | Do not start this extra container for the minimal journey. The backtest worker already has watchdog cron. |
| `research-worker` | Consumes the separate research queue; no cron in its settings. | Start only after local queue state is understood; submit `max_parallelism: 1`. |
| Auto-heal | Missing data can enqueue ingestion. There is no `AUTO_HEAL_ENABLED` setting. Guardrails reject nonempty symbol lists when `AUTO_HEAL_MAX_SYMBOLS=0`. | Set `AUTO_HEAL_MAX_SYMBOLS=0`; stop if existing data is missing instead of enabling download. |
| Broker defaults | `IB_HOST` has precedence over `IB_GATEWAY_HOST`; probes use configured host/port. Simply omitting the broker Compose profile does not disable backend probes. | In the actual backend environment set both host names to `127.0.0.1`, and `IB_PORT` to an unused container-local port (for example `65534`). Failed local probes are expected. |
| Vendor configuration | Compose interpolates the Databento key from the environment. Instrument bootstrap and onboarding can contact the vendor; even onboarding cost/dry-run paths may make metadata calls. | Explicitly blank vendor keys in the runtime environment; do not invoke bootstrap, onboarding, ingest, or cost-estimation endpoints. |
| Health checks | Worker health checks only ping Redis. Backend `/health` only establishes a limited service check. | Require the API workflow evidence below; a green container health badge does not prove job execution. |

Compose's shared worker environment does **not** currently forward `DAILY_INGEST_ENABLED` or `AUTO_HEAL_MAX_SYMBOLS`. Merely exporting these on the host is insufficient: root needs a temporary Compose override or equivalent explicit container environment. Do not print the resolved full Compose environment or credential-bearing request headers into evidence.

Sources: [Compose worker environment](/Users/pablomarin/Code/msai-v2/docker-compose.dev.yml:24), [worker containers](/Users/pablomarin/Code/msai-v2/docker-compose.dev.yml:148), [backend lifespan](/Users/pablomarin/Code/msai-v2/backend/src/msai/main.py:259), [generic worker cron](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/settings.py:125), [nightly disabled guard](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/nightly_ingest.py:139), [auto-heal guard](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/backtests/auto_heal_guardrails.py:92), [IB settings precedence](/Users/pablomarin/Code/msai-v2/backend/src/msai/core/config.py:91).

## Existing local data and preflight

Data is mounted from host `data/` to container `/app/data`. Raw partitions live in `data/parquet`; the Nautilus catalog is `data/nautilus`, not `data/nautilus_catalog`. Source: [Compose mounts](/Users/pablomarin/Code/msai-v2/docker-compose.dev.yml:18), [catalog preparation](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/research_job.py:154).

Read-only inspection found:

| Raw partition | Rows | Timestamp bounds observed |
| --- | ---: | --- |
| `data/parquet/stocks/AAPL/2025/01.parquet` | 1,728 | 2025-01-22 12:00 UTC through 2025-01-24 23:57 UTC |
| `data/parquet/stocks/AAPL/2024/12.parquet` | 10,288 | 2024-12-02 12:00 UTC through 2024-12-31 23:34 UTC |
| `data/parquet/stocks/SPY/2024/12.parquet` | 10,959 | 2024-12-02 12:00 UTC through 2024-12-31 23:47 UTC |

An AAPL catalog directory exists under `data/nautilus/data/bar/AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL`. Existence and row counts do not establish coverage completeness or valid registry mappings. Several other equities have only isolated May/June 2026 sessions, so a broad recent-date default is inappropriate.

Use authenticated requests against `http://localhost:8800`, supplying the authorized development API key without storing it in the report. First require `/health` and `/ready` to succeed. Then:

1. `GET /api/v1/market-data/symbols` — expect `symbols.stocks` contains `AAPL`.
2. `GET /api/v1/market-data/bars/AAPL?start=2025-01-22&end=2025-01-24&interval=1m` — expect nonempty bars, `count == len(bars)`, chronological timestamps within the requested days, valid OHLC and nonnegative volume. Record actual count and first/last timestamps; 1,728 is the current partition-level expectation, not a substitute for the API response.
3. `GET /api/v1/strategies/` — expect one selected real EMA item, valid UUID, nonempty code hash/config schema. This GET writes the registry.
4. `POST /api/v1/strategies/{strategy_id}/validate` with no request body — expect HTTP 200 and a successful-validation message.

If the backtest API rejects the historical equity because its instrument registry is cold or missing an effective-dated alias, record the 422 as a real workflow blocker. Do not conceal it by directly inserting database rows or calling vendor bootstrap. The raw files alone do not satisfy the instrument resolver. Source: [backtest resolution](/Users/pablomarin/Code/msai-v2/backend/src/msai/api/backtests.py:351).

## Single real backtest

`POST /api/v1/backtests/run`, replacing the placeholder with the discovered UUID:

```json
{
  "strategy_id": "<discovered-EMA-UUID>",
  "instruments": ["AAPL.NASDAQ"],
  "start_date": "2025-01-22",
  "end_date": "2025-01-25",
  "smoke": false,
  "config": {
    "fast_ema_period": 5,
    "slow_ema_period": 20,
    "trade_size": "1"
  }
}
```

The API injects/canonicalizes `instrument_id` and the default minute `bar_type`; passing them in this single-backtest request is unnecessary. The end date is deliberately the following midnight because the prior audit confirmed that the runner forwards a date string interpreted as midnight. This is an explicit workaround for the existing end-date defect, not evidence that date semantics are correct. Bars are still sourced from January 22–24. Sources: [request schema](/Users/pablomarin/Code/msai-v2/backend/src/msai/schemas/backtest.py:16), [config preparation](/Users/pablomarin/Code/msai-v2/backend/src/msai/api/backtests.py:155), [date defect and offline evidence](/Users/pablomarin/Code/msai-v2/docs/audits/2026-10-03/research-data.md).

Require HTTP 201 and preserve the job UUID. Poll `GET /api/v1/backtests/{job_id}/status` at a modest interval, with a bounded wall-time budget. Record pending/running/terminal transitions when observed; a short run can legitimately finish before the first poll. Stop on `failed`, structured errors, or `phase: awaiting_data`. Do not retry repeatedly or allow ingestion to turn a missing-data test into a paid download.

After completion:

| Request | Required evidence |
| --- | --- |
| `GET /api/v1/backtests/{job_id}/results` | HTTP 200; nonempty metrics; positive `trade_count` for the intended end-to-end order/fill proof; `series_status: ready` with nonempty series; `has_report: true`. If no trades occurred, report that limitation rather than claiming order/fill proof. |
| `GET /api/v1/backtests/{job_id}/trades?page=1&page_size=500` | `total == results.trade_count`; actual fill rows, correct AAPL identity, finite positive prices/quantities, nonnegative commission, and execution timestamps within the run window. Retrieve further pages only if needed to reconcile totals. |
| `GET /api/v1/backtests/{job_id}/report` | HTTP 200, HTML content type, substantive report content. Record whether it is full QuantStats, basic fallback, or empty fallback. |
| `GET /api/v1/backtests/history?type=single` | Returned history contains this run, subject to pagination. |

Report generation can succeed with fallback HTML. The empty report contains `No returns data available`; the basic fallback contains `Install quantstats for full tearsheet reports.` even when QuantStats is installed but failed. Three sessions can be insufficient for the full report's analytics. Preserve this distinction in observed evidence. Source: [report generator](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/report_generator.py:48).

## Two-trial research sweep

Only proceed after the single backtest has proved the data path. Use the canonical AAPL ID established by the preceding run. Unlike the single-backtest path, the research worker forwards `base_config` directly: `instrument_id` and `bar_type` must be supplied here. Source: [research dispatch](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/research_job.py:184).

`POST /api/v1/research/sweeps`:

```json
{
  "strategy_id": "<discovered-EMA-UUID>",
  "instruments": ["AAPL.NASDAQ"],
  "start_date": "2025-01-22",
  "end_date": "2025-01-25",
  "asset_class": "stocks",
  "base_config": {
    "instrument_id": "AAPL.NASDAQ",
    "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
    "fast_ema_period": 5,
    "slow_ema_period": 20,
    "trade_size": "1"
  },
  "parameter_grid": {
    "fast_ema_period": [5, 10],
    "slow_ema_period": [20]
  },
  "objective": "sharpe",
  "max_parallelism": 1,
  "search_strategy": "grid",
  "require_positive_return": false,
  "holdout_fraction": null,
  "holdout_days": null,
  "purge_days": 5
}
```

Require HTTP 201; poll `GET /api/v1/research/jobs/{research_job_id}`. A successful operational result requires status `completed`, progress 100, no job error, exactly two persisted trials, both trials completed with populated metrics, the expected two distinct fast-EMA configurations, and `results.summary.successful_runs == 2`. Capture `best_config`, `best_metrics`, trial objectives and report summary. Check `GET /api/v1/research/jobs?page=1&page_size=20` for persistence, accounting for pagination.

Do not accept the overall completed status alone: `_finalize_job` sets it before considering whether trial results contain errors. No profit threshold should be asserted; poor performance is a legitimate result. The sweep deliberately has no holdout and makes no out-of-sample claim. Prior audit findings about holdout reuse and eligibility filters remain unresolved; do not use this run to promote a strategy or graduate it to live trading. Sources: [research schema](/Users/pablomarin/Code/msai-v2/backend/src/msai/schemas/research.py:12), [research job finalization](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/research_job.py:355).

## Evidence to retain and stop conditions

Capture timestamps, selected strategy UUID/hash, exact credential-free request JSON, returned backtest/research UUIDs, terminal status/error envelopes, bar count/window, trade count and representative actual fills, metrics and series status, report classification, trial count/configurations/statuses, and which services/environment controls were actually applied. Preserve failed responses as evidence; do not substitute synthetic data, manual SQL, or smoke fixtures to obtain green status.

Stop dependent steps for missing registry aliases, absent local bars, schema/authentication failures, vendor-ingestion requests, unexpected active deployments/queued jobs, or engine/report errors. Parent may separately authorize and document a narrow repair; this plan does not authorize source fixes or migrations. Research cancellation is cooperative after the engine returns, so keep the trial count bounded at submission rather than assuming cancel immediately kills work. Source: [research cancellation behavior](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/research_job.py:103).

Preparation consisted of bounded `rg`/`sed`/`nl` source reads and a read-only PyArrow examination of existing Parquet metadata/timestamp bounds, using the existing environment. No additional test suite or real workflow was executed by this planning agent. The broader audit's focused offline validation and counterexamples are recorded in [research-data.md](/Users/pablomarin/Code/msai-v2/docs/audits/2026-10-03/research-data.md).

## Observed local result and accounting reconciliation

Root subsequently executed the real local journey; this subsection independently reconciles the saved [API evidence](/Users/pablomarin/Code/msai-v2/docs/audits/2026-10-03/local-runtime-evidence.json), without executing more jobs. Strategy `example.ema_cross` validated, all 1,728 expected bars were returned, backtest `7e10e3c7-74ec-4e99-96df-817ecda44653` completed, and the report request returned HTTP 200 with 327,766 bytes of QuantStats HTML, with neither recorded fallback marker. Research job `4f39f4ff-3c54-44e1-a190-b0123feba503` completed with two successful persisted trials and no holdout. Its fast=5 trial exactly matches the single run's metrics; the fast=10 trial is also completed. Root correctly used the resolver's actual `AAPL.XNAS` identity in research rather than assuming the illustrative `AAPL.NASDAQ` payload above remained canonical.

All 100 returned trade rows are present (`total=100`, page size 500). They alternate 50 one-share BUYs and 50 one-share SELLs, end flat, and form exactly 50 complete round trips. Decimal arithmetic directly on their prices gives:

| Quantity | Observed or independently calculated value |
| --- | ---: |
| Winning / losing completed round trips | 18 / 32 |
| Win rate | 18 / 50 = 0.36, matching the API |
| January 22 gross PnL | -$0.04 |
| January 23 gross PnL | +$0.38 |
| January 24 gross PnL | -$0.87 |
| Total gross PnL from fill prices | **-$0.53** |
| Persisted fill `pnl` sum / `commission` sum | $0 / $0 |
| API `metrics.total_return` | -0.00005299999999115244 |
| Correct gross return ratio on the runner's $1,000,000 | **-0.00000053** |
| Chart first / last equity | 100,000 / 99,999.95099999805 |
| Chart endpoint return ratio | -0.0000004900000194485443 |
| January `monthly_returns[0].pct` | -0.0000004900000195595666 |

**Confirmed P1: headline total return is in the wrong units.** The runner initializes each venue with `$1,000,000` ([balance](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:64), [venue](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:407)). Installed Nautilus `total_pnl_percentage` computes `(difference / starting) * 100` ([source](/Users/pablomarin/Code/msai-v2/backend/.venv/lib/python3.12/site-packages/nautilus_trader/analysis/analyzer.py:339)). MSAI copies `PnL% (total)` unchanged into `total_return` ([extraction](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:584), [return](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:622)), while its frontend expects a ratio and multiplies again by 100 ([consumer](/Users/pablomarin/Code/msai-v2/frontend/src/app/backtests/[id]/page.tsx:215)). The API's -0.000053 is therefore percentage points masquerading as a ratio: a 100-fold magnitude error. Dividing this field by 100 reproduces the fill-derived -$0.53 / $1,000,000 within floating-point precision. The account/position fallbacks return ratios, so the current field's units can also change according to the successful extraction tier.

**Confirmed P1: the return series drops first-day PnL.** Daily account compaction takes each day's last balance and computes `pct_change().fillna(0.0)`, forcing the first day to zero ([source](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:490)). The observed chart explicitly reports January 22 return zero although fills lost $0.04 that day. Its cumulative return instead equals `(-0.53 - (-0.04)) / (1000000 - 0.04) = -0.0000004900000196000008`, matching the monthly and endpoint returns. Thus merely dividing the headline metric by 100 would still leave this independent disagreement. The series renderer then rebuilds equity from an unrelated default base of $100,000 ([builder](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/analytics_math.py:248), [caller omitting starting balance](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/backtest_job.py:130)); 99,999.951 is a rebased curve; the runner capital and fill-derived gross PnL instead imply approximately 999,999.47 ending balance before any costs. The display needs an explicit normalized-equity interpretation or the real account basis. A full QuantStats HTML report does not establish that its input returns retained the first day.

**Confirmed P1 accounting limitation: persisted fill economics cannot reconcile PnL or execution costs.** All 100 API rows have `pnl=0`, even though closed pairs range between gains and losses and sum to -$0.53. `_order_row_to_trade` reads PnL from an order report row with a default zero and hardcodes every commission to zero ([source](/Users/pablomarin/Code/msai-v2/backend/src/msai/workers/backtest_job.py:809)). Order rows are not position round-trip PnL records. The observed zeros do not establish zero economic PnL, nor do they prove fees were simulated and were actually free. This run's headline metric reconciles to **gross** fill-price PnL; net performance under realistic broker costs remains unverified. The source-level hardcoded commission also guarantees that any nonzero engine commission would be lost in persisted trade rows.

**Confirmed count distinction: 100 represents order rows, not 100 completed trades.** `num_trades` is `len(orders_df)` ([source](/Users/pablomarin/Code/msai-v2/backend/src/msai/services/nautilus/backtest_runner.py:624)); persisted trade count is also 100 here. The 50 complete round trips and 18 winners correctly explain `win_rate=0.36`. There is no missing-fill discrepancy in this observed run. The problem is mixed denominators and labeling: a consumer interpreting `num_trades=100` as 100 independent closed trade outcomes, or using it as such for minimum-sample gates, would be wrong. General partial-fill/unfilled-order handling was not exercised by this simple one-share market-order run.

The reconciliation used one bounded standard-library Python read of the saved JSON, asserting full pagination coverage, one-share quantities, and alternating BUY/SELL order before pairing; Decimal arithmetic produced the PnL totals above. Source reads confirmed the unit conversion, first-day omission, rebasing, and hardcoded commission. No broker, vendor, production deployment, additional market window, live parity, fee model, or overfitting protection was validated by this observed local journey.
