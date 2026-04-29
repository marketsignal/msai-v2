<!-- forge:doc how-backtesting-works -->

# How Backtesting Works

This is the third document in the [Developer Journey](00-developer-journey.md) — the one that turns a strategy file in `strategies/` into a measured, reproducible answer to the question "would this have made money?" It covers a **single-strategy, single-symbol** backtest end-to-end: enqueue, execute, persist, render. Multi-strategy basket backtests are doc 06; parameter sweeps and walk-forward are doc 04. This doc is the foundation both of those build on.

If you've already read [How Strategies Work](how-strategies-work.md), you know the registry stamps each strategy with a `code_hash` and a `config_schema`. Backtesting is what consumes that — it's the first place those columns earn their keep.

---

## The Component Diagram

Read top-to-bottom. The flow runs from API/CLI/UI through arq to a worker, which spawns a **separate** subprocess to host the Nautilus engine, then materializes results back into Postgres and writes a QuantStats HTML to disk.

```
┌─ ENTRY SURFACES ─────────────────────────────────────────────────────────┐
│                                                                          │
│   API                          CLI                          UI           │
│   POST /api/v1/backtests/run   msai backtest run …          /backtests   │
│   :8800                        (Typer)                      :3300        │
│                                                                          │
└──────────────────────────────────────┬───────────────────────────────────┘
                                       │
                                       ▼
                       ┌─ FASTAPI ROUTER ────────────────────┐
                       │  api/backtests.py                   │
                       │   • _prepare_and_validate_…()       │
                       │   • SecurityMaster.resolve_for_     │
                       │       backtest(start=…)             │
                       │   • StrategyConfig.parse() (422)    │
                       │   • Backtest row INSERT (pending)   │
                       │   • enqueue_backtest() → arq        │
                       └──────────────────┬──────────────────┘
                                          │
                                          ▼
                       ┌─ ARQ QUEUE (Redis) ─────────────────┐
                       │  queue: arq:queue                   │
                       │  job_id ↔ Backtest.queue_job_id     │
                       └──────────────────┬──────────────────┘
                                          │
                                          ▼
                       ┌─ BACKTEST WORKER (process) ─────────┐
                       │  workers/backtest_job.py            │
                       │   • _start_backtest:                │
                       │       status pending → running      │
                       │   • heartbeat task (15s)            │
                       │   • retry-once auto-heal loop       │
                       │       on FileNotFoundError          │
                       │   • _execute_backtest: capture      │
                       │       nautilus_version,             │
                       │       python_version, data_snapshot │
                       └──────────────────┬──────────────────┘
                                          │
                                          ▼
                       ┌─ BACKTEST RUNNER ───────────────────┐
                       │  services/nautilus/backtest_runner  │
                       │   • mp.get_context("spawn")         │
                       │   • _RunPayload (pickle)            │
                       │   • subprocess.join(timeout=30 min) │
                       └──────────────────┬──────────────────┘
                                          │     spawn()
                                          ▼
            ┌─ NAUTILUS BACKTEST SUBPROCESS ────────────────────────┐
            │                                                       │
            │  resolve_importable_strategy_paths(strategy_file)     │
            │    → ImportableStrategyConfig(strategy_path,          │
            │         config_path, config)                          │
            │  (code_hash was computed once, at enqueue;            │
            │   not recomputed here. git_sha is reserved on the     │
            │   model but unwritten — see §1.)                      │
            │                                                       │
            │  ┌─ PARQUET CATALOG (read-only) ─────────────┐        │
            │  │  {DATA_ROOT}/parquet/{asset}/{symbol}/    │        │
            │  │    {YYYY}/{MM}.parquet                    │        │
            │  │  ParquetDataCatalog (Nautilus native)     │        │
            │  └────────────────┬──────────────────────────┘        │
            │                   │                                   │
            │                   ▼                                   │
            │  ┌─ NAUTILUS ENGINE (per-venue config) ──────┐        │
            │  │  BacktestNode + one BacktestVenueConfig   │        │
            │  │    per unique venue suffix in the         │        │
            │  │    instrument-id list (NASDAQ, CME, …)    │        │
            │  │  Strategy class instance                  │        │
            │  │  Bar → on_bar() → orders → fills          │        │
            │  └────────────────┬──────────────────────────┘        │
            │                   │                                   │
            │                   ▼                                   │
            │  BacktestResult: orders_df, positions_df,             │
            │                  account_df, metrics                  │
            │  → pickled to result_path tempfile                    │
            └────────────────┬──────────────────────────────────────┘
                             │
                             ▼ (worker reads pickle)
            ┌─ MATERIALIZATION (worker process) ──────────────────┐
            │   • _materialize_series_payload() →                 │
            │       SeriesPayload + series_status                 │
            │   • Trade rows INSERT (paginated read later)        │
            │   • Backtest UPDATE: metrics, series, status=       │
            │       completed, completed_at                       │
            │   • QuantStats HTML written to                      │
            │       {DATA_ROOT}/reports/{backtest_id}.html        │
            │   • report_path stamped on Backtest row             │
            └────────────────┬────────────────────────────────────┘
                             │
                             ▼
                ┌─ POSTGRES (single source of truth) ─┐
                │  backtests row                      │
                │   + trades rows (paginated)         │
                │   + series JSONB (canonical)        │
                │   + report_path → tearsheet         │
                └─────────────────────────────────────┘
```

Key shape to notice: **the engine runs in its own process**, not in the worker. That's not paranoia — it's a Nautilus rule. Importing `nautilus_trader` rewires `asyncio`'s event-loop policy at module-import time, so once any import happens in the worker, that interpreter is contaminated for arq's purposes (see `.claude/rules/nautilus.md` gotcha #1). The worker stays clean by isolating Nautilus inside a `mp.get_context("spawn")` child. Same engine, fresh interpreter per run.

---

## TL;DR

A backtest is an arq job that spawns a Nautilus subprocess, runs your strategy against historical Parquet bars under one `BacktestVenueConfig` per unique venue suffix in the canonical instrument-id list (e.g. `AAPL.NASDAQ` → NASDAQ; `["AAPL.NASDAQ", "ESM5.CME"]` → two venue configs), persists every metric and trade to Postgres, and writes a QuantStats HTML report to disk. Every result row stamps `strategy_code_hash`, `nautilus_version`, `python_version`, and a `data_snapshot` of the catalog so the run is fully reproducible (`strategy_git_sha` exists on the model but is not written today — see §1). The trade log is paginated and the report is iframed on `/backtests/[id]`.

**Three surfaces:**

- **API** — `POST /api/v1/backtests/run` enqueues; `GET /api/v1/backtests/{id}/{status,results,trades,report}` reads back.
- **CLI** — `msai backtest run`, `msai backtest history`, `msai backtest show`.
- **UI** — `/backtests` (list + form) and `/backtests/[id]` (chart, paginated trade log, QuantStats iframe).

---

## Table of Contents

1. [Concepts and data model](#1-concepts-and-data-model)
2. [The three surfaces (parity table)](#2-the-three-surfaces)
3. [Internal sequence](#3-internal-sequence)
4. [See it / verify it / troubleshoot it](#4-see-it--verify-it--troubleshoot-it)
5. [Common failures](#5-common-failures)
6. [Idempotency and retry behavior](#6-idempotency-and-retry-behavior)
7. [Rollback and repair](#7-rollback-and-repair)
8. [Key files](#8-key-files)

---

## 1. Concepts and data model

### The `Backtest` row is the audit record

A backtest is fundamentally a row in the `backtests` table — created the moment the request lands, mutated through `pending → running → completed | failed`, and never deleted as part of normal operation. Every observable about the run lives on that row. There is **no** `updated_at`: the row is append-only in spirit, even though SQL technically lets us mutate it.

`backend/src/msai/models/backtest.py` defines 35 columns (`mapped_column` entries on `Backtest`, lines 46-115). They cluster into nine concerns:

| Concern              | Columns                                                                                              | What they answer                                             |
| -------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| **Identity**         | `id`, `created_by`, `created_at`                                                                     | Which backtest is this, and who initiated it?                |
| **Strategy lineage** | `strategy_id`, `strategy_code_hash`, `strategy_git_sha`, `config`                                    | Which strategy version + config did we run?                  |
| **Run window**       | `instruments`, `start_date`, `end_date`                                                              | Which symbols, which date range?                             |
| **Lifecycle**        | `status`, `progress`, `started_at`, `completed_at`                                                   | Where is it now?                                             |
| **Results**          | `metrics`, `series`, `series_status`, `report_path`                                                  | What did it return?                                          |
| **Failure**          | `error_message`, `error_code`, `error_public_message`, `error_suggested_action`, `error_remediation` | If it failed, why — and what can the operator do?            |
| **Operator**         | `phase`, `progress_message`, `heal_started_at`, `heal_job_id`                                        | Is it auto-healing right now?                                |
| **Worker debug**     | `queue_name`, `queue_job_id`, `worker_id`, `attempt`, `heartbeat_at`                                 | Which arq job, which worker, how many attempts, still alive? |
| **Reproducibility**  | `nautilus_version`, `python_version`, `data_snapshot`                                                | Can I reconstruct this exactly?                              |

The reproducibility cluster is the load-bearing one. Without it you have a number, not a measurement.

#### The four reproducibility stamps (and one gap)

Every completed backtest carries four stamps that, together, let a future operator (or post-mortem investigator, or auditor) reconstruct what ran:

1. **`strategy_code_hash` — `String(64)`, line 50.** SHA256 of the strategy file's bytes at enqueue time, computed once in `api/backtests.py:248` via `hashlib.sha256(strategy_file.read_bytes()).hexdigest()`. If you edit the file later, this row still points at the version you ran. The `Backtest` does not have a foreign key to a "strategy version" row — there isn't one. The hash _is_ the version.
2. **`nautilus_version` — `String(32)`, line 106.** Captured from `nautilus_trader.__version__` inside the worker at the start of `_execute_backtest()`. Different Nautilus versions produce different fills — this stamp is what tells you "this number is from 1.223.0, not 1.224.0."
3. **`python_version` — `String(16)`, line 107.** From `sys.version_info`. The matrix of Python × Nautilus × strategy is the actual unit of reproducibility.
4. **`data_snapshot` — `JSONB`, line 108.** Catalog metadata — which Parquet files were on disk, their sizes, mtimes, plus a SHA256 `catalog_hash` (`services/nautilus/catalog_builder.py:403-470`) that lets two backtests assert "we ran against the exact same files." `files` is capped at 50 entries per snapshot. If you re-ingest data later (add gap fills, correct splits, reclassify futures rolls), the snapshot is your record of "what bars did the run actually see?"

**Known gap — `strategy_git_sha`.** The column exists on the model (`String(40)`, line 51, nullable) but **no code path on the backtest side writes to it.** Neither `api/backtests.py` (the enqueue) nor `workers/backtest_job.py:_persist_lineage` populates it; `grep -rn "strategy_git_sha" backend/src/msai/api/backtests.py backend/src/msai/workers/backtest_job.py backend/src/msai/services/nautilus/backtest_runner.py` returns zero hits. The live-trading path (`live_deployment.py`, `order_attempt_audit.py`) does set it, but for backtests this column is reserved-but-empty. Sections 6 and 7 below describe `git checkout <strategy_git_sha>` workflows that work in principle once this gap is closed; today they require recovering the SHA from `created_at` plus the surrounding git history.

Together the four written stamps answer the only honest question about a backtest: _if I run this again from scratch on a clean machine, do I get the same number?_ The answer should always be yes.

### The auto-heal lifecycle

The worker is allowed to take the run off the happy path **once**. If `_execute_backtest()` raises `FileNotFoundError` — the canonical signal that Parquet bars are missing for the requested window — the worker triggers one auto-heal cycle (`workers/backtest_job.py:235-294`).

During auto-heal, four extra columns light up:

| Column             | Type         | Meaning                                                  |
| ------------------ | ------------ | -------------------------------------------------------- |
| `phase`            | `String(32)` | Set to `"awaiting_data"` while the heal is in flight     |
| `progress_message` | `Text`       | Human-readable: "Downloading AAPL 2024-06 from Polygon…" |
| `heal_started_at`  | `DateTime`   | When the heal job was triggered                          |
| `heal_job_id`      | `String(64)` | The arq job ID of the ingest job we kicked off           |

The heal calls `run_auto_heal()`, which classifies its own outcome — `SUCCESS`, `GUARDRAIL_REJECTED`, `COVERAGE_STILL_MISSING`, `TIMEOUT`, or `INGEST_FAILED`. The `_OUTCOME_TO_EXC` dict (`backtest_job.py:79-84`) maps those non-success outcomes to exception types so the failure classifier downstream picks the right `FailureCode`:

```python
{
    AutoHealOutcome.GUARDRAIL_REJECTED:     FileNotFoundError,
    AutoHealOutcome.COVERAGE_STILL_MISSING: FileNotFoundError,
    AutoHealOutcome.TIMEOUT:                TimeoutError,
    AutoHealOutcome.INGEST_FAILED:          RuntimeError,
}
```

On `SUCCESS` the worker re-enters `_execute_backtest()` once with the same snapshot. On any other outcome it goes to `_handle_terminal_failure()` and the four heal columns are cleared together when the row reaches its terminal state.

The auto-heal is **retry-once**. There is no retry-many loop. If the second attempt also raises `FileNotFoundError`, the run fails — terminally — with a `MISSING_DATA` `FailureCode`. This bound is deliberate: an unbounded heal loop hides systemic data problems, and a hand-rolled retry is the wrong tool for what is really an ingestion bug.

### The two-tier `series_status` payload

The `series` column holds a daily-normalized equity curve as JSONB — but the question "is the curve renderable?" is not the same as "did the backtest succeed?" A backtest can complete with valid metrics yet fail to materialize a presentable curve (corrupt timestamps, NaN runs, encoding edge cases). To keep the frontend honest, we split the signal in two:

- `series` — the JSONB payload itself, validated through `SeriesPayload.model_validate()` round-trip.
- `series_status` — `String(32)` with a CHECK constraint pinning it to one of `ready`, `not_materialized`, `failed` (`models/backtest.py:72-76`, CHECK at lines 122-125).

The materialization path is fail-soft (`workers/backtest_job.py:87-159`): if `build_series_payload()` blows up, the worker writes `(None, "failed")` and the backtest still completes with metrics intact. The frontend's results page reads `series_status` first; if it's not `ready`, the chart panel shows a "series unavailable" placeholder while the metrics table and the QuantStats iframe still render.

This is a small thing but worth its own field. Conflating "we have a chart" with "the run worked" cost us a debugging afternoon early on; the two-tier split is the fix.

### The trade log dedup contract

The `trades` rows associated with a backtest are sorted by `(executed_at, id) ASC` (`api/backtests.py:502-577`). That second key matters: bar-aligned fills can land at the exact same `executed_at` timestamp, so we need a tiebreaker, and `id` is the only column guaranteed to be deterministic and unique. Pagination uses `(page, page_size)` query params with a soft cap of 500 (clamped, not 422'd). The frontend's "Load more" UI sends sequential pages; the soft clamp is what makes a hand-typed `?page_size=10000` not break the world.

There is no explicit dedup operation needed on backtest trades — they're inserted once by the worker process at completion. (Live trades are different; they have a partial unique index on `(deployment_id, broker_trade_id)` for reconciliation replay. That's doc 07's problem, not ours.)

---

## 2. The three surfaces

The same five operations exist across all three surfaces. The table is the contract.

| Intent               | API                                                    | CLI                                           | UI                                             | Observe / Verify                                                                          |
| -------------------- | ------------------------------------------------------ | --------------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Run a backtest       | `POST /api/v1/backtests/run`                           | `msai backtest run <id> <syms> <start> <end>` | `/backtests` → "New Backtest" form             | Response is `BacktestStatusResponse` with `status="pending"`. Worker logs show enqueue.   |
| Poll status          | `GET /api/v1/backtests/{id}/status`                    | (implicit in `msai backtest show`)            | `/backtests/[id]` polls every 3s while running | `status`, `progress` (0–100), `phase` (`awaiting_data` during heal), `progress_message`.  |
| Get results          | `GET /api/v1/backtests/{id}/results`                   | `msai backtest show <id>`                     | `/backtests/[id]` chart panel                  | `metrics` dict, `series` payload (if `series_status=ready`), `trade_count`, `has_report`. |
| Browse the trade log | `GET /api/v1/backtests/{id}/trades?page=N&page_size=M` | (n/a — UI-driven)                             | `/backtests/[id]` trade log table (paginated)  | Sorted `(executed_at, id) ASC`, page_size soft cap 500.                                   |
| Download QuantStats  | `GET /api/v1/backtests/{id}/report` (signed token)     | `msai backtest show <id>` (link)              | `/backtests/[id]` iframe                       | HTML tearsheet generated by QuantStats; stamped against `report_path`.                    |
| List historical runs | `GET /api/v1/backtests/history`                        | `msai backtest history`                       | `/backtests` table                             | Paginated, ordered by `created_at DESC`.                                                  |

A few notes on the parity:

- **The CLI is a thin wrapper around the API.** `msai backtest run` calls `POST /api/v1/backtests/run`; `msai backtest history` calls `GET /api/v1/backtests/history`; `msai backtest show` does both `/status` and `/results` and stitches them. There is no separate CLI execution path — the CLI exists for ops and scripting, not for bypassing the API.
- **No "create draft" verb.** A backtest exists only after `POST /run`. There's no draft, no two-stage commit, no stage-then-execute. You either submit it or you don't.
- **The report is a separately-fetched artifact.** `GET /report` takes a signed token (`POST /report-token` mints one). This is to keep the report iframable from a different origin than the API while preserving JWT auth — see `api/backtests.py:578`.

---

## 3. Internal sequence

This is the post-entry walk for `POST /api/v1/backtests/run`. CLI and UI both funnel here; the CLI is `httpx.post(...)` and the UI is `apiPost(...)`. From the API down it's identical.

```
Caller                FastAPI                arq               Worker             Subprocess          Postgres        Filesystem
  │                      │                    │                    │                    │                    │                    │
  │  POST /backtests/run │                    │                    │                    │                    │                    │
  │  (strategy_id,       │                    │                    │                    │                    │                    │
  │   config, syms,      │                    │                    │                    │                    │                    │
  │   start, end)        │                    │                    │                    │                    │                    │
  ├─────────────────────►│                    │                    │                    │                    │                    │
  │                      │                                                                                                         │
  │                      │  SecurityMaster.resolve_for_backtest(start=start_date)                                                   │
  │                      │  → canonical instrument IDs (alias-window respected)                                                    │
  │                      │                                                                                                         │
  │                      │  _prepare_and_validate_backtest_config()                                                                 │
  │                      │   • inject canonical instrument_id / bar_type if missing                                                │
  │                      │   • load StrategyConfig class by name                                                                    │
  │                      │   • StrategyConfig.parse(config) → 422 on ValidationError                                                │
  │                      │                                                                                                         │
  │                      │  INSERT backtest (status=pending,                                                                        │
  │                      │                    strategy_code_hash, instruments, ...) ─────────────────────────►│                    │
  │                      │                                                                                                         │
  │                      │  enqueue_backtest(job)  ─────────────►│                                                                  │
  │                      │                                       │  (job sits in arq:queue, Redis ZADD)                              │
  │                      │                                                                                                         │
  │  201 Created         │                                                                                                         │
  │  + Location          │                                                                                                         │
  │  + BacktestStatus    │                                                                                                         │
  │  (status=pending)    │                                                                                                         │
  │◄─────────────────────│                                                                                                         │
  │                      │                                                                                                         │
  │                      │                                       │                    │                                            │
  │                      │                                       │  arq picks job ───►│                                            │
  │                      │                                       │                    │                                            │
  │                      │                                                            │  _start_backtest()                          │
  │                      │                                                            │   • status: pending → running ─────────────►│
  │                      │                                                            │  spawn heartbeat task (15s)                 │
  │                      │                                                            │                                             │
  │                      │                                                            │  _execute_backtest()                        │
  │                      │                                                            │   • ensure_catalog_data(...)                │
  │                      │                                                            │   • capture nautilus_version,               │
  │                      │                                                            │       python_version, data_snapshot ───────►│
  │                      │                                                            │                                             │
  │                      │                                                            │  BacktestRunner.run(...)                    │
  │                      │                                                            │   • mp.get_context("spawn")                 │
  │                      │                                                            │   • _RunPayload pickled                     │
  │                      │                                                            │   • spawn fresh interpreter ───►│           │
  │                      │                                                            │                                  │           │
  │                      │                                                            │                                  │ resolve_importable_strategy_paths(file)
  │                      │                                                            │                                  │ ImportableStrategyConfig(strategy_path,
  │                      │                                                            │                                  │   config_path, config)
  │                      │                                                            │                                  │
  │                      │                                                            │                                  │ ParquetDataCatalog.load(catalog_path)
  │                      │                                                            │                                  │   ◄── reads {DATA_ROOT}/parquet/...
  │                      │                                                            │                                  │
  │                      │                                                            │                                  │ _extract_venues_from_instrument_ids(...)
  │                      │                                                            │                                  │   AAPL.NASDAQ → "NASDAQ"
  │                      │                                                            │                                  │   ESM5.CME    → "CME"
  │                      │                                                            │                                  │ BacktestVenueConfig(name=v) per venue
  │                      │                                                            │                                  │ BacktestNode.run()
  │                      │                                                            │                                  │   bar → on_bar() → orders → fills
  │                      │                                                            │                                  │
  │                      │                                                            │                                  │ generate_account_report(venue=Venue(v))
  │                      │                                                            │                                  │   per venue, then pd.concat()
  │                      │                                                            │                                  │ generate_orders_report()
  │                      │                                                            │                                  │ generate_positions_report()
  │                      │                                                            │                                  │
  │                      │                                                            │                                  │ pickle BacktestResult → result_path
  │                      │                                                            │                                  │ exit 0
  │                      │                                                            │                                  ▼
  │                      │                                                            │  process.join(timeout=30 min)               │
  │                      │                                                            │  read result_path pickle                    │
  │                      │                                                            │                                             │
  │                      │                                                            │  _materialize_series_payload()              │
  │                      │                                                            │   • build_series_payload(returns)           │
  │                      │                                                            │   • SeriesPayload.model_validate()          │
  │                      │                                                            │   → (payload_dict, "ready") | (None,"failed")│
  │                      │                                                            │                                             │
  │                      │                                                            │  generate_quantstats_report() ───────────────────────────────►│
  │                      │                                                            │   • {DATA_ROOT}/reports/{id}.html                              │
  │                      │                                                            │                                             │
  │                      │                                                            │  INSERT trades (paginated) ────────────────►│
  │                      │                                                            │  UPDATE backtest                            │
  │                      │                                                            │   • metrics, series, series_status,         │
  │                      │                                                            │     report_path                             │
  │                      │                                                            │   • status: running → completed             │
  │                      │                                                            │   • completed_at = now() ──────────────────►│
  │                      │                                                            │                                             │
  │                      │                                                            │  finally: stop heartbeat                    │
  │                      │                                                            │                                             │
  │                                                                                                                                  │
  │  GET /backtests/{id}/status (poll)                                                                                              │
  ├─────────────────────►│                                                                                                          │
  │                      │  SELECT … WHERE id = … ◄──────────────────────────────────────────────────────────────────────────────  │
  │  status=completed,   │                                                                                                          │
  │  progress=100        │                                                                                                          │
  │◄─────────────────────│                                                                                                          │
  │                      │                                                                                                          │
  │  GET /backtests/{id}/results                                                                                                    │
  ├─────────────────────►│                                                                                                          │
  │  metrics, series,    │                                                                                                          │
  │  trade_count,        │                                                                                                          │
  │  has_report=true     │                                                                                                          │
  │◄─────────────────────│                                                                                                          │
  │                      │                                                                                                          │
  │  GET /backtests/{id}/trades?page=1&page_size=50                                                                                  │
  ├─────────────────────►│                                                                                                          │
  │  paginated trades    │                                                                                                          │
  │◄─────────────────────│                                                                                                          │
  │                      │                                                                                                          │
  │  POST /backtests/{id}/report-token  (UI)                                                                                        │
  ├─────────────────────►│                                                                                                          │
  │  signed_url          │                                                                                                          │
  │◄─────────────────────│                                                                                                          │
  │                      │                                                                                                          │
  │  GET /backtests/{id}/report?token=…                                                                                              │
  ├─────────────────────►│                                                                                                          │
  │                      │  Send file from report_path  ◄────────────────────────────────────────────────────────────────────────  │
  │  HTML iframe         │                                                                                                          │
  │◄─────────────────────│                                                                                                          │
```

A few points worth dwelling on:

**The API does the validation, not the worker.** `_prepare_and_validate_backtest_config()` (`api/backtests.py:123-205`) injects canonical instrument IDs into `instrument_id` / `bar_type` if the caller omitted them, loads the strategy's `*Config` class by name, and runs `StrategyConfig.parse(config)`. A `ValidationError` becomes a structured `StrategyConfigValidationError` envelope with HTTP 422. This is deliberate — failing fast with a useful error at the API edge is much friendlier than failing inside an arq worker where the failure mode is "your job vanished into the queue."

**The instrument resolution is dated.** `SecurityMaster.resolve_for_backtest(start=start_date, ...)` (`api/backtests.py:272-276`) honors the `start_date` kwarg specifically so historical alias windows are respected. If you backtest 2023's `ESM5` you get the December 2023 `ES` contract, not whatever's currently active. The instrument registry's `instrument_aliases` table is what makes this work — see doc 01.

**The `code_hash` is computed once, at enqueue.** The API hashes the strategy file's bytes inline at `api/backtests.py:240-248` (`hashlib.sha256(strategy_file.read_bytes()).hexdigest()`) and persists the result on the `Backtest` row before the job is enqueued. The subprocess does **not** recompute it — `grep -n "hashlib\\|sha256\\|code_hash" services/nautilus/backtest_runner.py` returns zero hits. The runner imports the strategy via `resolve_importable_strategy_paths(...)` and constructs an `ImportableStrategyConfig`, but it never re-reads the file's bytes or compares hashes. If a developer hot-edits the file between enqueue and execution, the row records the pre-edit hash and the run executes the post-edit code; nothing in the worker flags the divergence. If you need that guard, it's a future hardening item — not a property of the system today.

**The subprocess is `spawn`, not `fork`.** `mp.get_context("spawn")` (`backtest_runner.py:222`) is non-negotiable. `fork()` would inherit the parent's memory, including any Nautilus state from a prior run — and Nautilus carries Rust + Cython state that does not survive `fork()` cleanly. Spawn gives us a clean `python -c "<bootstrap>"` interpreter every time, at the cost of an extra second of startup.

**The 30-minute timeout is a wall, not a target.** The runner's default is `timeout_seconds: int = 30 * 60` (`backtest_runner.py:174`); the worker passes `settings.backtest_timeout_seconds` (`backtest_job.py:377`) so an operator can change it via Pydantic settings without touching the runner. The parent calls `process.join(timeout_seconds)` and `process.terminate()`s anything still alive, then raises `TimeoutError` (`backtest_runner.py:226-231`). A 5-year minute-bar single-symbol backtest finishes in ~2 minutes; a 10-year multi-symbol options sweep can push 15. If you're hitting the wall, the answer is "split the run," not "raise the timeout."

---

## 4. See it / verify it / troubleshoot it

### `/backtests` (the list page)

`frontend/src/app/backtests/page.tsx` is a table joined against the strategies registry. Each row shows `name` (resolved via `strategy_id` lookup), `status` badge, `start_date → end_date`, and `created_at`. The badge color comes from `statusColor()` (lines 40-53) — `pending`/`running` are yellow, `completed` green, `failed` red. There's a `RunBacktestForm` component at the top wired to `POST /api/v1/backtests/run`.

The form has three tabs:

- **Strategy** — dropdown populated from `GET /api/v1/strategies/`.
- **Symbols** — comma-separated, validated client-side against the strategy's expected instruments.
- **Window** — date pickers, defaulting to last 12 months.

Submitting redirects you to `/backtests/[id]` immediately (status `pending`).

### `/backtests/[id]` (the detail page)

This is where most of the operator time gets spent. The page (`frontend/src/app/backtests/[id]/page.tsx`) is a **header + tabs** layout:

1. **Header** — back button, the truncated backtest id, a `status` badge, and the started/completed timestamps. When `phase === "awaiting_data"` an inline auto-heal indicator appears under the timestamps with `progress_message` displayed verbatim ("Downloading AAPL 2024-06 from Polygon…"). When the backtest is `completed`, a "Download Report" button is rendered on the right side of the header. The page polls `GET /status` every **3 seconds** while `pending`/`running` (`MAX_RESULTS_RETRIES = 10` × 3s = 30s wall-clock retry window for `/results` if it 404s transiently after `status=completed`).
2. **Native view tab** (default) — renders `<ResultsCharts>` (metrics table + TradingView Lightweight Chart of the equity curve from the `series` JSONB, gated on `series_status === "ready"`; the metrics table renders unconditionally because it reads the `metrics` dict directly) followed by `<TradeLog>` (paginated table of `(executed_at, instrument, side, quantity, price, pnl, commission)` rows). The trade log fixes `pageSize` at 100 — there is **no page-size dropdown**; navigation is just Previous / Next chevron buttons. Sort is fixed to `(executed_at, id) ASC`; the soft cap of 500 is enforced server-side.
3. **Full report tab** — renders `<ReportIframe>` for the QuantStats tearsheet (drawdown chart, monthly returns heatmap, rolling Sharpe, etc). The tab trigger is `disabled` when `results.has_report === false` (e.g., `series_status="failed"` and report-gen was skipped).

The two tabs sit inside a single shadcn `<Tabs defaultValue="native">` block; the user toggles between them rather than scrolling through stacked panels. When the backtest is `failed`, the tabs are replaced by a `<FailureCard>` rendering of the `error` envelope from `/status`; when it's still `pending`/`running`, a placeholder ("Backtest in progress (NN%)") sits where the tabs would go.

### Verifying via the API directly

The simplest end-to-end smoke test is a curl chain:

```bash
# 1. Enqueue
RUN=$(curl -s -X POST http://localhost:8800/api/v1/backtests/run \
  -H "X-API-Key: $MSAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id":"<strategy-uuid>",
    "config":{"fast_ema":10,"slow_ema":30},
    "instruments":["AAPL"],
    "start_date":"2024-01-01",
    "end_date":"2024-12-31"
  }')
ID=$(echo "$RUN" | jq -r .id)

# 2. Poll
while true; do
  STATUS=$(curl -s http://localhost:8800/api/v1/backtests/$ID/status \
    -H "X-API-Key: $MSAI_API_KEY")
  echo "$STATUS" | jq -c '{status, progress, phase}'
  [[ "$(echo "$STATUS" | jq -r .status)" == "completed" ]] && break
  [[ "$(echo "$STATUS" | jq -r .status)" == "failed" ]] && { echo "$STATUS" | jq .error; exit 1; }
  sleep 3
done

# 3. Read results
curl -s http://localhost:8800/api/v1/backtests/$ID/results \
  -H "X-API-Key: $MSAI_API_KEY" | jq '{metrics, trade_count, series_status}'
```

This is exactly what the verify-e2e agent runs as a regression. If the API path passes and the UI doesn't render, the bug is in the frontend; if the API path fails, you have a worker or engine problem and should not look at the UI.

### History page

`GET /api/v1/backtests/history` (`api/backtests.py:354-395`) supports only two query params: `page` (default 1) and `page_size` (default 20, max 100). There are no `?strategy_id=...` or `?status=...` filter params today — the endpoint always returns every backtest in the system, paged and ordered by `created_at DESC`. The frontend list table (`frontend/src/app/backtests/page.tsx:135-141`) renders four columns — `Strategy`, `Date Range`, `Status`, `Created` — plus a chevron link to the detail page. There is no `code_hash` column in the UI; if you need to scope results to one strategy or filter by status, you read the JSON response and filter client-side. Adding query-param filters to the endpoint is an open follow-up.

---

## 5. Common failures

Backtest failures classify into a fixed set of `FailureCode` enum values (`services/backtests/failure_code.py` defines five: `MISSING_DATA`, `STRATEGY_IMPORT_ERROR`, `ENGINE_CRASH`, `TIMEOUT`, `UNKNOWN`). The classification logic itself lives next door at `services/backtests/classifier.py:classify_worker_failure`, which inspects the exception type + message and picks a code. Each terminal write populates the four error columns atomically: `error_code` (the enum string), `error_message` (full traceback for debugging), `error_public_message` (sanitized via `services/backtests/sanitize.py:sanitize_public_message()`), and `error_suggested_action` (a one-liner for the operator). Below are the ones you'll see in the wild.

### Instrument not pre-loaded — `ENGINE_CRASH`

NautilusTrader's `InstrumentProvider` raises at the **first bar event** if a strategy subscribes to an instrument that wasn't loaded at engine startup. Not at `BacktestNode.run()` start — at `on_bar()` time, which means your backtest looks healthy for the first 30 seconds and then explodes. This is `.claude/rules/nautilus.md` gotcha #9.

In MSAI, this manifests as a `RuntimeError` propagated from the subprocess pickle. The classifier maps it to `ENGINE_CRASH`. The public message is "Instrument required by strategy was not loaded into the catalog." The remediation is to verify the symbol is in the registry (see doc 01) and that its Parquet files exist for the requested window.

### Venue mismatch — `ENGINE_CRASH`

NautilusTrader's `BacktestNode` will refuse to run if any instrument id has a venue suffix that the run config didn't declare a `BacktestVenueConfig` for: `Venue 'NASDAQ' for AAPL.NASDAQ does not have a BacktestVenueConfig`. This is `.claude/rules/nautilus.md` gotcha #4.

MSAI's runner avoids this by deriving the venue config list from the instrument ids themselves. `_extract_venues_from_instrument_ids()` (`backtest_runner.py:67-98`) walks every canonical id (`"AAPL.NASDAQ" → "NASDAQ"`, `"ESM5.CME" → "CME"`), deduplicates, and `_build_backtest_run_config` (`backtest_runner.py:362-413`) emits one `BacktestVenueConfig` per unique venue. So a single-venue equity backtest gets one venue config; a multi-venue (equities + futures) backtest gets one per venue. There is no SIM-pinning anywhere in the production path — that name only survives in test fixtures and a `catalog_builder.py` docstring example.

The remaining failure shapes are:

1. **Empty / non-canonical instrument id.** `_extract_venues_from_instrument_ids` raises `ValueError` if any id is missing the `.<VENUE>` suffix or the list is empty, surfaced as a `RuntimeError` from the subprocess. This is the "you bypassed `SecurityMaster.resolve_for_backtest` and shipped a bare ticker" failure.
2. **Venue suffix doesn't match the catalog.** The runner builds a venue config for whatever the id says, so this only fails downstream if the Parquet catalog has no bars filed under that venue — which manifests as the gotcha-#9 instrument-not-pre-loaded crash, not as a venue config error.

### Strategy class load error — `STRATEGY_IMPORT_ERROR`

Three sub-flavors:

1. **The strategy file fails to import.** Maybe it has a syntax error, or it imports something that's not in the worker's environment. The subprocess catches the `ImportError`, writes an error pickle, and exits nonzero. The classifier maps to `STRATEGY_IMPORT_ERROR`.
2. **The strategy file imports, but no class subclasses `Strategy`.** The registry catches this at `validate_strategy_file()` time, but if a file was registered before its class was renamed/removed, the discovery cycle is what notices.
3. **The strategy's `*Config` class fails `parse()` on the supplied config dict.** This one is caught at the API edge (`_prepare_and_validate_backtest_config()`) and returned as an HTTP 422 — the worker never sees it. If you see `STRATEGY_IMPORT_ERROR` from a _running_ backtest, it's class load (1 or 2), not parse failure.

### Auto-heal cycle — `awaiting_data` → recover or fail

The auto-heal loop is the only place a backtest's status field stays still while something is happening — it'll be `running` with `phase="awaiting_data"` for as long as the ingest job takes. You can watch this through `GET /status`; the `progress_message` field is surfaced verbatim to the UI banner.

When does it fire? Specifically when `_execute_backtest()` raises `FileNotFoundError` (`workers/backtest_job.py:235-294`). That exception comes from the Parquet catalog when a requested date range has no files on disk. The worker calls `run_auto_heal()` once, which dispatches an ingest job to fill the gap, waits for it to complete, then re-enters `_execute_backtest()`.

When does it _not_ fire? On any other exception — `TimeoutError`, `RuntimeError`, `ImportError`, `KeyError`, you name it. Those are terminal on the first attempt. The auto-heal is purely a missing-data recovery path; it doesn't try to fix logic bugs.

The four heal columns (`phase`, `progress_message`, `heal_started_at`, `heal_job_id`) are cleared together when the row reaches a terminal status — so a successful heal-then-complete row looks the same as a complete-on-first-attempt row, except for the `attempt` counter.

### Parquet missing for date range — `MISSING_DATA`

The terminal version of the auto-heal failure. After one heal cycle, if `_execute_backtest()` _still_ raises `FileNotFoundError`, the failure is classified `MISSING_DATA` with public message "Required market data is not available for the backtest window." The remediation links to `msai data-status` so the operator can see exactly which symbol/window is missing.

This is also the failure mode when the symbols were never onboarded in the first place — `COVERAGE_INCOMPLETE` from the symbols pipeline (see doc 01) bubbles up as `COVERAGE_STILL_MISSING` from the heal, which `_OUTCOME_TO_EXC` maps to `FileNotFoundError`, which classifies as `MISSING_DATA`. The chain looks long because it is — but the operator sees one message: "data not available, here's the symbol/window."

### Subprocess timeout — `TIMEOUT`

If `process.join(timeout=30*60)` times out, the runner calls `process.terminate()` — which sends **SIGTERM**, not SIGKILL (`backtest_runner.py:228-231`) — gives the child five seconds to wind down, and raises `TimeoutError`. The classifier maps it to `TIMEOUT` and any partial state is discarded; you won't get a half-completed result. Caveat: a strategy that installs a `signal.signal(SIGTERM, ...)` handler can in principle catch the terminate and keep running past the parent's `join(timeout=5)`, leaving an orphan subprocess. We don't call `process.kill()` (the SIGKILL escalation), so the kernel-guarantee assumption that "nothing the child does can prevent termination" doesn't hold — orphan-after-timeout is rare but possible.

If you hit this regularly, the run is too big for one process. Options:

1. Narrow the date range and run sequentially (cheapest).
2. Reduce instrument count (but for single-symbol/single-strategy backtests this is one).
3. Profile the strategy — most strategies that timeout are doing too much in `on_bar()`.

There's a second timeout to be aware of: the **arq job** has its own timeout (`workers/settings.py:131`: `job_timeout: int = max(settings.backtest_timeout_seconds, 60 * 60)`), set to the larger of the subprocess timeout and one hour, so it always sits above the subprocess wall. If the arq timeout fires, the whole worker is recycled and the row is left in `running` with stale `heartbeat_at`. The watchdog (`services/job_watchdog.py:_scan_backtests`, lines 46-87) will eventually move it to `status="failed"` with a stale-heartbeat reason like `"Watchdog: no heartbeat for NN seconds"` written to `error_message`. Note: the watchdog does **not** set `error_code` — the column keeps its `"unknown"` server-default, so when triaging a watchdog-cleaned row read `error_message`, not `error_code`. This path is rare in practice; the subprocess timeout fires first.

---

## 6. Idempotency and retry behavior

### What's deterministic

Given identical:

- `strategy_id` (and identical `strategy_code_hash` — i.e., the file hasn't changed)
- `config` dict
- `instruments` list
- `start_date`, `end_date`
- `nautilus_version`, `python_version`
- `data_snapshot` (i.e., the catalog hasn't been re-ingested)

…the same backtest produces the same `metrics`, the same `trades` rows in the same order, and the same `series` payload. Every time. The Nautilus engine is deterministic by design — fills and order events are functions of the bar stream and the strategy's reactions, with no clock-driven randomness in the per-venue `BacktestVenueConfig` simulation.

The reproducibility stamps on the row are what you use to verify this. To confirm a 6-month-old backtest still reproduces, you check out the strategy file at the version whose bytes hash to the row's `strategy_code_hash`, install the recorded `python_version` + `nautilus_version`, point the catalog at the recorded `data_snapshot` (cross-checking via `data_snapshot.catalog_hash`), and re-run. You should land on identical metrics. If you don't, one of those four inputs has drifted — and the audit trail tells you which. Recovering the strategy file from `strategy_code_hash` alone is awkward today (search history for a file whose SHA256 matches); once `strategy_git_sha` is wired up on the backtest enqueue path the recipe shortens to `git checkout <strategy_git_sha>`.

### What's not deterministic

- **Wall-clock fields.** `started_at`, `completed_at`, `heartbeat_at`, the `id` UUID — none of these survive across runs. They're observability, not measurement.
- **The `data_snapshot` JSONB itself.** On re-ingest (gap fill, split correction, futures roll fix), the snapshot fingerprint changes. The fact that the `metrics` change after a re-ingest is the system _correctly_ registering that the inputs changed. This is a feature; if metrics didn't move when data moved, that would be the bug.
- **Auto-heal-triggered runs.** If a backtest auto-heals on attempt 1, the row's `data_snapshot` reflects the _post-heal_ catalog state. Re-running the same request on a fresh DB now skips the heal (the data is already there) and produces the same result. So idempotency is preserved across re-runs _of the same request_ — but the first one's lifecycle (with `phase="awaiting_data"`) is not reproduced.

### The retry-once auto-heal loop

Documented above in §1 and §5 but worth repeating in the retry-behavior context: the worker retries **once**, only on `FileNotFoundError`, only after a successful auto-heal. Anything else is terminal on attempt 1.

### Transient vs terminal classification

The classifier (`services/backtests/classifier.py:classify_worker_failure`, called via `_mark_backtest_failed` from `_handle_terminal_failure()` in `backtest_job.py`) treats every non-success outcome as terminal once it's recorded. There is no "retry the whole job" semantics at the arq level — arq's retry mechanism is disabled for backtest jobs because a re-run of an already-failed backtest would clobber the audit trail. If you want to "retry," you submit a fresh `POST /run` request, which produces a new `Backtest` row.

The implicit transient/terminal split:

| Initial failure                                | Treated as     | Why                                                                                              |
| ---------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------ |
| `FileNotFoundError` (Parquet missing)          | Transient      | Auto-heal can fix this — one chance to fill data.                                                |
| `TimeoutError`                                 | Terminal       | Re-running is expensive and unlikely to help.                                                    |
| `RuntimeError` (subprocess crash, non-Parquet) | Terminal       | Probably a code bug; needs investigation.                                                        |
| `ValidationError` (caught at API)              | Never enqueued | Returned as 422 to caller before the row is created.                                             |
| Stale heartbeat (watchdog-cleaned)             | Terminal       | `error_code` stays `"unknown"`; reason in `error_message`. Operator decides whether to resubmit. |

The principle: auto-heal handles the one well-understood transient mode. Everything else surfaces as a terminal failure with a clear `FailureCode`, and the operator decides what to do next. We don't paper over crashes with retry loops.

---

## 7. Rollback and repair

There is no "undo a backtest" verb. Backtests are immutable runs; the row stays in the database for the audit trail. What you actually do depends on what went wrong.

### Bad result, want to delete the row

`Backtest` rows can be hard-deleted via direct SQL — there is no `DELETE /api/v1/backtests/{id}` endpoint. This is intentional: deletion of audit records is an administrative operation, not a user operation. The operator runs:

```sql
DELETE FROM trades WHERE backtest_id = '<uuid>';
DELETE FROM backtests WHERE id = '<uuid>';
```

…and separately removes the QuantStats HTML at `{DATA_ROOT}/reports/{id}.html`. We retain the report file by default even after row deletion — auditors sometimes want the tearsheet without the row, or the row without the tearsheet, and the two are loosely coupled by `report_path`. Operationally, "clean delete" is two commands.

### Want to re-run with the same inputs

Submit a fresh `POST /api/v1/backtests/run` with the same payload. You get a new `id`, a new audit row, a fresh QuantStats HTML. The original row stays. This is the right move 95% of the time — the audit trail is what you're paying for.

If the strategy file has changed since the original run, the new run will have a different `strategy_code_hash`. That's the intended behavior — we want the registry to track strategy versions implicitly through the hash.

### Strategy file regressed; want to confirm the old run was legitimate

Recover the strategy file at the version whose bytes hash to the original row's `strategy_code_hash` (search the strategies repo's history for a commit where `sha256(strategy_file)` matches), check that revision out, then re-run with the same `config`/`instruments`/`start`/`end`. The new row will have the same `strategy_code_hash` as the original (because the file content is the same), and assuming `nautilus_version` + catalog haven't drifted, the metrics should match. If they don't, you've found a non-determinism bug — file an issue. (Once `strategy_git_sha` is wired up at enqueue, this becomes a one-liner: `git checkout <strategy_git_sha>`.)

### Catalog was re-ingested; old backtests look different now

The old rows still hold the old `data_snapshot` JSONB — that's your record of what they ran against. The catalog on disk is now the new snapshot. Re-running the same `POST /run` against the new catalog produces a new row with a new snapshot fingerprint. Both rows are valid records of what they say they are; they're not contradicting each other, they're describing different inputs.

If you need to certify "this strategy on this version of the data," the right artifact to ship is a row id — not "the latest backtest of strategy X on AAPL."

---

## 8. Key files

| Path                                                            | Role                                                                       |
| --------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `backend/src/msai/api/backtests.py:208-351`                     | `POST /run` endpoint — validation, instrument resolve, enqueue.            |
| `backend/src/msai/api/backtests.py:354-395`                     | `GET /history` — paginated list.                                           |
| `backend/src/msai/api/backtests.py:398-428`                     | `GET /{id}/status` — status + progress + heal phase.                       |
| `backend/src/msai/api/backtests.py:431-484`                     | `GET /{id}/results` — metrics, series, trade_count, has_report.            |
| `backend/src/msai/api/backtests.py:502-577`                     | `GET /{id}/trades` — paginated, sorted `(executed_at, id) ASC`.            |
| `backend/src/msai/api/backtests.py:578-625`                     | `POST /{id}/report-token` — signed token for cross-domain iframe.          |
| `backend/src/msai/api/backtests.py:628-737`                     | `GET /{id}/report` — QuantStats HTML download.                             |
| `backend/src/msai/api/backtests.py:123-205`                     | `_prepare_and_validate_backtest_config()` — 422 on parse failure.          |
| `backend/src/msai/api/backtests.py:104-120`                     | `_report_is_deliverable()` — gates `has_report`.                           |
| `backend/src/msai/models/backtest.py:46-115`                    | `Backtest` model — 35 columns, lifecycle + lineage + heal.                 |
| `backend/src/msai/models/backtest.py:122-125`                   | `series_status` CHECK constraint.                                          |
| `backend/src/msai/workers/backtest_job.py:162-306`              | `run_backtest_job` — entrypoint, retry-once loop.                          |
| `backend/src/msai/workers/backtest_job.py:79-84`                | `_OUTCOME_TO_EXC` — maps heal outcomes to exception types.                 |
| `backend/src/msai/workers/backtest_job.py:87-159`               | `_materialize_series_payload` — fail-soft series build.                    |
| `backend/src/msai/workers/backtest_job.py:215-233`              | Heartbeat task spawn (15s).                                                |
| `backend/src/msai/services/nautilus/backtest_runner.py:106-118` | `BacktestResult` dataclass.                                                |
| `backend/src/msai/services/nautilus/backtest_runner.py:126-149` | `_RunPayload` — pickle-safe parent→child bundle.                           |
| `backend/src/msai/services/nautilus/backtest_runner.py:165-252` | `BacktestRunner.run()` — spawn subprocess, join, read pickle.              |
| `backend/src/msai/services/nautilus/backtest_runner.py:362-413` | `_build_backtest_run_config` — one `BacktestVenueConfig` per unique venue. |
| `backend/src/msai/services/nautilus/backtest_runner.py:67-98`   | `_extract_venues_from_instrument_ids` — venue dedup + validation.          |
| `backend/src/msai/services/nautilus/backtest_runner.py:64`      | `_DEFAULT_STARTING_BALANCE = "1000000 USD"`.                               |
| `backend/src/msai/services/security_master/`                    | `resolve_for_backtest(start=…)` — alias-window honored.                    |
| `backend/src/msai/services/backtests/failure_code.py`           | `FailureCode` enum (5 values).                                             |
| `backend/src/msai/services/backtests/classifier.py`             | `classify_worker_failure` — maps exceptions to `FailureCode`.              |
| `backend/src/msai/services/job_watchdog.py:46-87`               | `_scan_backtests` — stale-heartbeat cleanup; sets `error_message` only.    |
| `backend/src/msai/services/backtests/sanitize.py`               | `sanitize_public_message` — strip internal details.                        |
| `backend/src/msai/cli.py:320-348`                               | `msai backtest run` — Typer command.                                       |
| `backend/src/msai/cli.py:351-367`                               | `msai backtest history` — paginated listing.                               |
| `backend/src/msai/cli.py:370-392`                               | `msai backtest show` — status + results stitched.                          |
| `backend/src/msai/workers/settings.py`                          | `asyncio.set_event_loop_policy(None)` after Nautilus imports — gotcha #1.  |
| `frontend/src/app/backtests/page.tsx`                           | List page + `RunBacktestForm`.                                             |
| `frontend/src/app/backtests/[id]/page.tsx`                      | Detail page — header + Tabs (Native view / Full report); 3s poll.          |
| `frontend/src/components/backtests/trade-log.tsx`               | `<TradeLog>` — `pageSize` prop (default 100), Previous/Next chevrons only. |
| `docs/nautilus-reference.md`                                    | Full Nautilus reference for engine internals.                              |
| `.claude/rules/nautilus.md`                                     | Top-20 gotchas — read before any Nautilus code work.                       |

---

**Date verified against codebase:** 2026-04-28
**Previous doc:** [How Strategies Work →](how-strategies-work.md)
**Next doc:** [How Research and Selection Work →](how-research-and-selection-works.md)
