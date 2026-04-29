<!-- forge:doc how-research-and-selection-works -->

# How Research and Selection Work

This is doc 04 of 08 in the [Developer Journey](00-developer-journey.md). One backtest tells you how a strategy did with one config; **research** runs the same strategy across a parameter grid and a walk-forward schedule, scores every combination, and lets you promote the winner to a `GraduationCandidate`. The seam to the next stage is one HTTP call: `POST /api/v1/research/promotions`. The graduation gate takes over from there.

---

## Component Diagram

```
                      ┌─ LAUNCH ────────────────────────────────────────────────┐
                      │                                                         │
   API ────►  POST /api/v1/research/sweeps                                      │
              POST /api/v1/research/walk-forward                                │
                                                                                │
   CLI ────►  msai research list / show / cancel                                │
              (launch is API-/UI-only — POST is constructed by the form)        │
                                                                                │
   UI  ────►  /research                  → "Launch Research" dialog             │
              /research/[id]             → progress · trial table · Promote     │
                      │                                                         │
                      └────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                      ┌─ FastAPI router (api/research.py) ──────────────────────┐
                      │  • _resolve_strategy()      DB lookup, 404 if missing   │
                      │  • _resolve_strategy_path() path-traversal guard        │
                      │  • _build_sweep_payload()   freeze instruments, grid    │
                      │  • _enqueue_job()           push to arq, rollback on    │
                      │                             Redis failure (503)         │
                      │  → INSERT ResearchJob (status="pending")                │
                      │  → arq enqueue → returns queue_job_id                   │
                      └────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                      ┌─ Redis (arq queue) ─────────────────────────────────────┐
                      │  msai:research                                          │
                      │   ├── job:<queue_job_id>  payload + retry budget        │
                      │   └── compute slot semaphore (Redis key)                │
                      └────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                      ┌─ research worker (workers/research_job.py) ─────────────┐
                      │  run_research_job(ctx, job_id, job_type, payload)       │
                      │   1. _mark_running()        status → running            │
                      │   2. heartbeat task         renew lease, poll cancel    │
                      │   3. acquire_compute_slots  block if at capacity        │
                      │   4. ensure_catalog_data()  ingest gaps if needed       │
                      │   5. ResearchEngine.run_*  via asyncio.to_thread()      │
                      │   6. _finalize_job()        upsert best_*, write trials │
                      │   7. release_compute_slots  always run                  │
                      └────────────────────┬────────────────────────────────────┘
                                           │
                                           ▼
                      ┌─ ResearchEngine (services/research_engine.py) ──────────┐
                      │                                                         │
                      │  parameter_sweep:           walk_forward:               │
                      │  ┌───────────────────┐     ┌──────────────────────┐     │
                      │  │ expand_grid()     │     │ build_walk_forward_  │     │
                      │  │  N configs        │     │   windows()          │     │
                      │  ├───────────────────┤     │  (rolling | exp.)    │     │
                      │  │ resolve_search_   │     ├──────────────────────┤     │
                      │  │  strategy()       │     │ for each window:     │     │
                      │  │  grid | succ.     │     │   run_parameter_     │     │
                      │  │  halving | optuna │     │     sweep(train)     │     │
                      │  ├───────────────────┤     │   evaluate(test)     │     │
                      │  │ fan-out           │     │   record OOS metric  │     │
                      │  │  per-config       │     │     for that fold    │     │
                      │  │  BacktestRunner   │     ├──────────────────────┤     │
                      │  │  on SIM venue     │     │ pick best window     │     │
                      │  └────────┬──────────┘     │  + carry config      │     │
                      │           │                 └─────────┬────────────┘    │
                      │           │                           │                 │
                      └───────────┼───────────────────────────┼─────────────────┘
                                  ▼                           ▼
                          ┌─ Postgres ──────────────────────────────────────────┐
                          │  research_jobs       1 row per launch               │
                          │   ├ best_config       JSONB winning params          │
                          │   ├ best_metrics      JSONB winning metrics         │
                          │   ├ results           JSONB full report             │
                          │   └ status            completed | failed | …        │
                          │                                                     │
                          │  research_trials     N rows per job                 │
                          │   ├ (research_job_id, trial_number)  unique         │
                          │   ├ config            JSONB candidate params        │
                          │   ├ metrics           JSONB per-trial metrics       │
                          │   ├ objective_value   Numeric(18,8) for ranking     │
                          │   └ backtest_id       FK → backtests (nullable)     │
                          └────────────────────┬────────────────────────────────┘
                                               │
                                               │  POST /api/v1/research/promotions
                                               ▼
                          ┌─ GraduationCandidate ───────────────────────────────┐
                          │  stage = "discovery"                                │
                          │  config  = best_config (or selected trial)          │
                          │  metrics = best_metrics (or trial metrics)          │
                          │  research_job_id  = ResearchJob.id   (audit link)   │
                          │                                                     │
                          │  → handed off to how-graduation-works.md            │
                          └─────────────────────────────────────────────────────┘
```

The seam at the bottom is the single line of code in `api/research.py:300` that calls `GraduationService.create_candidate()`. After that, this document is done; [How Graduation Works](how-graduation-works.md) takes over.

---

## TL;DR

Research is **where walk-forward lives** — not in the portfolio. Codex flagged this during the planning review for the documentation set: there is exactly one walk-forward implementation, and it sits inside `ResearchEngine.run_walk_forward` as a wrapper around `run_parameter_sweep` (the train phase) plus an OOS evaluation on the test window. The backtest portfolio (doc 06) does its own portfolio-level fan-out, but it does not run walk-forward — it consumes already-graduated candidates whose walk-forward fingerprint was already validated here. If you find yourself wanting "walk-forward at the portfolio level," you are probably looking for "research over a multi-strategy basket," and that is still a research job.

---

## Table of Contents

1. [Concepts and data model](#1-concepts-and-data-model)
2. [The three surfaces](#2-the-three-surfaces)
3. [Internal sequence — POST /research/walk-forward](#3-internal-sequence--post-researchwalk-forward)
4. [See, verify, troubleshoot](#4-see-verify-troubleshoot)
5. [Common failures](#5-common-failures)
6. [Idempotency and retry behavior](#6-idempotency-and-retry-behavior)
7. [Rollback and repair](#7-rollback-and-repair)
8. [Key files](#8-key-files)

---

## 1. Concepts and data model

### `ResearchJob` — one row per launch

`backend/src/msai/models/research_job.py` defines a single table row that represents the entire run. The columns split into three groups:

**Identity and configuration** (set at launch, never mutated):

- `id` — UUID primary key.
- `strategy_id` — FK to `strategies.id` (the file in `strategies/` that this run is sweeping).
- `job_type` — one of `"parameter_sweep"` or `"walk_forward"`. The string is also the dispatch key inside the worker (`workers/research_job.py:186` and `:218`).
- `config` — JSONB blob holding the full payload. Includes `instruments`, `start_date`, `end_date`, `parameter_grid` (after list normalization), `objective`, `max_parallelism`, `search_strategy`, holdout settings, and (for walk-forward) `train_days`, `test_days`, `step_days`, `mode`. Frozen at launch — the worker reads this, never the original request.
- `created_by` — FK to `users.id`, captured from the JWT claims.

**Status state machine** (mutated by the worker):

- `status` — `"pending"` → `"running"` → `"completed"` | `"failed"` | `"cancelled"`. Terminal states do not unwind. There is no separate `"queued"` state — pending is the queued state.
- `progress` — `SmallInteger` 0–100. Updated by the engine's progress callback through `_update_progress()` (`workers/research_job.py:337`).
- `progress_message` — short human-readable string. The cancellation poll inspects this for backwards compatibility; if it starts with `"Cancel"`, the worker treats the job as cancelled.
- `started_at`, `completed_at` — UTC timestamps stamped by `_mark_running()` and `_finalize_job()` / `_mark_cancelled()` / `_mark_failed()`.

**Lifecycle / watchdog fields** (used by the stale-job detector):

- `queue_name` — `"msai:research"` from `settings.research_queue_name` (`backend/src/msai/core/config.py:172`).
- `queue_job_id` — opaque arq identifier. Set after `enqueue_research()` returns; null if Redis was unavailable (which raises 503 to the API caller).
- `worker_id` — `"<hostname>:<pid>"` of the worker that owns the run. Recomputed every heartbeat so a hand-off between workers is visible.
- `attempt` — incremented by `_mark_running()` each time the job moves into `running`. arq's retry budget reuses the same job id, so this catches re-attempts.
- `heartbeat_at` — touched on every heartbeat tick (`compute_slot_lease_seconds / 3` cadence — 40s by default given the 120s lease).

**Result fields** (set once at completion):

- `results` — full JSONB report from the engine. For parameter sweeps, includes `summary.best_result`, the per-config result list, and (if configured) the holdout evaluation. For walk-forward, includes `windows[]` with `best_train_result` and `test_result` per fold.
- `best_config`, `best_metrics` — the headline result. Parameter sweeps copy `summary.best_result.config` and `.metrics`. Walk-forward derives the best fold by `objective_metric_key` on the test window and uses its train config + test metrics (`workers/research_job.py:368-393`).
- `error_message` — populated only on the failed path.

### `ResearchTrial` — one row per config evaluation

`backend/src/msai/models/research_trial.py` is the leaderboard table. Every config the engine evaluated lands here exactly once. The table key is `UniqueConstraint("research_job_id", "trial_number")`, which means trial numbers are dense within a job (0..N-1) — re-runs of the same job (which do not happen in practice; see § 6) would conflict on this constraint and fail loudly.

Per-row columns:

- `trial_number` — engine-assigned ordinal, dense `0..N-1`. For parameter sweeps it is the **rank index** into the engine's already-sorted result list (`report["results"]` is `ranked_results` per `research_engine.py:761`; `_finalize_job` enumerates that list at `workers/research_job.py:413`) — so `trial_number=0` is the leaderboard winner, **not** the first config in the original expanded grid. For walk-forward it is the window index (`workers/research_job.py:399`).
- `config` — the JSONB params that were evaluated. For walk-forward this is the train-phase config (the one chosen as best on the train window).
- `metrics` — JSONB metrics from the underlying backtest. For walk-forward these are **out-of-sample metrics** from the test window; the train metrics are not stored on the trial row (they live inside `ResearchJob.results.windows[i].best_train_result`).
- `objective_value` — `Numeric(18,8)` for indexable, deterministic ordering. The engine derives this from `metrics` via `extract_objective_value()` (`services/research_engine.py:166`), which negates `max_drawdown` so that "less negative ranks higher."
- `backtest_id` — nullable FK to `backtests.id`. The engine may or may not persist a full `Backtest` row per trial (parameter sweeps run via the engine's own `_run_one()`; the FK exists so a future change can attach trials to standalone Backtest rows for drilldown).

The cascade is `ON DELETE CASCADE`: deleting a `ResearchJob` row deletes all its trials. There is no soft-delete.

### Walk-forward, in one paragraph

`build_walk_forward_windows()` (`services/research_engine.py:119`) generates a list of `(train_start, train_end, test_start, test_end)` dicts. `mode="rolling"` advances both ends by `step_days` (default = `test_days` for non-overlapping OOS). `mode="expanding"` pins `train_start` to `start_date` and only advances `train_end` and the test window — useful when you want all available history feeding the train phase. For each window, `run_walk_forward()` (`services/research_engine.py:764`) calls `run_parameter_sweep()` over the train range to pick a best config, then evaluates that single config on the test range, and records the test metrics as the OOS result for that fold. The "selection" step at the end picks the fold with the best test-side objective value — this is the result that promotes.

### Objective metric mapping

Users pick a short name (`"sharpe"`, `"sortino"`, `"total_return"`, `"max_drawdown"`); the engine stores the canonical metric key (`sharpe_ratio`, `sortino_ratio`, etc.). The map lives at `workers/research_job.py:48`:

```python
_OBJECTIVE_METRIC_MAP: dict[str, str] = {
    "sharpe": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "total_return": "total_return",
    "max_drawdown": "max_drawdown",
}
```

The `extract_objective_value()` helper handles both forms — pass it `"sharpe"` or `"sharpe_ratio"` and it returns the same float. Maximization is unambiguous because the helper negates drawdown for you.

### Search strategy auto-selection

`resolve_search_strategy()` (`services/research_engine.py:189`) auto-resolves between exactly two strategies when the user asked for `"auto"`:

- `"grid"` — full Cartesian product. Default for small grids or short ranges.
- `"successive_halving"` — multi-stage screening that runs all candidates on a short prefix of the date range, drops the worst by `reduction_factor`, then re-runs the survivors on a longer prefix. Triggered when `candidate_count >= 8 and total_days >= 60` (`research_engine.py:207`).

A third strategy, `"optuna"` (Bayesian optimization), is **available but never auto-selected** — the user has to pass `search_strategy="optuna"` explicitly in the request.

Each strategy stores its own per-trial result rows; from the consumer's point of view the leaderboard table looks identical.

---

## 2. The three surfaces

This is where the parity rule applies: every operation that exists on one surface exists on all three (or has an explicit reason it does not). The launch operations are notable for being API-/UI-only — the CLI does not currently have a `research launch` command because the parameter grid and base config are nested JSON, which is awkward to type at a shell prompt. Use the UI's "Launch Research" dialog or a curl call.

| Intent                     | API                                             | CLI                                          | UI                                                             | Observe / Verify                                                                       |
| -------------------------- | ----------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Launch a parameter sweep   | `POST /api/v1/research/sweeps` (201)            | _(none — use API or UI)_                     | `/research` → "Launch Research" → mode = "Parameter Sweep"     | `GET /api/v1/research/jobs` shows new row with `status="pending"`                      |
| Launch a walk-forward      | `POST /api/v1/research/walk-forward` (201)      | _(none — use API or UI)_                     | `/research` → "Launch Research" → mode = "Walk-forward"        | Same as above; `job_type="walk_forward"`                                               |
| List research jobs         | `GET /api/v1/research/jobs?page=1&page_size=20` | `msai research list --page 1 --page-size 20` | `/research` (auto-polls every 5s while jobs are active)        | UI KPI cards count `pending` / `running` / `completed` / `failed`                      |
| Show one job (with trials) | `GET /api/v1/research/jobs/{id}`                | `msai research show <id>`                    | `/research/[id]`                                               | UI shows progress bar, trial leaderboard, OOS plots (for walk-forward), Promote button |
| Cancel a running job       | `POST /api/v1/research/jobs/{id}/cancel`        | `msai research cancel <id>`                  | (cancel button on detail page — to be added; for now, CLI/API) | Status flips to `cancelled`; `progress_message="Cancelled"` after engine winds down    |
| Promote best result        | `POST /api/v1/research/promotions` (201)        | _(none — use API or UI)_                     | `/research/[id]` → "Promote" button                            | New `GraduationCandidate` with `stage="discovery"`, `research_job_id` back-link        |

A few specifics worth pinning down:

**Launch returns the job row immediately.** The 201 response is `ResearchJobResponse` (`backend/src/msai/schemas/research.py:41`) with `status="pending"` and `progress=0`. The arq enqueue happens synchronously before the response — if Redis is down, the API returns 503 (`api/research.py:414`) and rolls back the DB row. There is no orphan-row case where the DB has the job but the queue doesn't.

**List is plain pagination.** `page` is 1-indexed; `page_size` is bounded 1–100. Sort order is `created_at DESC` (`api/research.py:158`). There are no filter parameters — clients filter client-side.

**Show eagerly loads trials.** `get_research_job` selects `ResearchTrial` ordered by `trial_number` and embeds them in `ResearchJobDetailResponse.trials` (`api/research.py:200`). For a 200-trial sweep this is fine; if jobs grow much larger, this endpoint will need pagination on the trials list.

**Cancel is best-effort, and not actually an early stop.** `api/research.py:209` flips the status to `"cancelled"` immediately for pending jobs. For running jobs it sets the same status, and the worker's heartbeat task notices the change on its next tick and sets the in-process `cancel_requested` event. The engine has no callback that interrupts it — it runs the **entire** sweep or walk-forward to completion before the worker even sees the flag. Once the engine returns, the worker checks `cancel_requested` and, if set, skips `_finalize_job()` and calls `_mark_cancelled()` instead — so cancelled runs end with **zero** trial rows persisted (see § 3 for the exact code path).

**Promote requires `status="completed"`.** `api/research.py:267` returns 409 if the job is not completed, and 409 again (`api/research.py:273`) if **either** `best_config` **or** `best_metrics` is null — both must be populated. By default it promotes the headline (`best_config` + `best_metrics`); pass `trial_index` in the request body to promote a specific trial instead — useful when the headline is not the one you want (e.g., it has higher Sharpe but fewer trades).

---

## 3. Internal sequence — `POST /research/walk-forward`

This is the sequence for the walk-forward path because it is the more interesting of the two; the parameter-sweep path is the same minus the per-window train/test fan-out.

```
Client                     FastAPI                     Postgres                Redis              Worker                  Engine
  │                            │                          │                       │                  │                       │
  │ POST /research/walk-       │                          │                       │                  │                       │
  │  forward {…}               │                          │                       │                  │                       │
  │ ──────────────────────────►│                          │                       │                  │                       │
  │                            │ get_current_user (JWT)   │                       │                  │                       │
  │                            │ _resolve_strategy(id) ──►│ SELECT strategies     │                  │                       │
  │                            │                          ├──── row ─────────────►│                  │                       │
  │                            │ _resolve_strategy_path() │ (path-traversal       │                  │                       │
  │                            │   under strategies_root  │  guard)               │                  │                       │
  │                            │ _build_sweep_payload()   │                       │                  │                       │
  │                            │   stamp train/test/      │                       │                  │                       │
  │                            │   step_days, mode        │                       │                  │                       │
  │                            │ INSERT research_jobs ───►│                       │                  │                       │
  │                            │  (status=pending,        │                       │                  │                       │
  │                            │   job_type=walk_forward) │                       │                  │                       │
  │                            │ enqueue_research() ──────────────────────────────►│                  │                       │
  │                            │                          │                       │ XADD msai:       │                       │
  │                            │                          │                       │  research        │                       │
  │                            │ UPDATE queue_job_id, ───►│                       │                  │                       │
  │                            │  queue_name + COMMIT     │                       │                  │                       │
  │ ◄──── 201 ResearchJobResponse                         │                       │                  │                       │
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │ ◄── arq pick up ──┤                       │
  │                            │                          │                       │                  │ run_research_job(     │
  │                            │                          │                       │                  │  job_id, "walk_       │
  │                            │                          │                       │                  │  forward", payload)   │
  │                            │                          │                       │                  │ _mark_running() ─────►│ status=running
  │                            │                          │                       │ acquire_compute_ │  attempt += 1         │
  │                            │                          │                       │  slots(N) ◄──────┤  worker_id stamped    │
  │                            │                          │                       │ ──── lease_id ──►│                       │
  │                            │                          │                       │                  │ heartbeat task fires  │
  │                            │                          │                       │ ◄─── renew_ ─────┤  (every 40s)          │
  │                            │                          │                       │       compute_   │                       │
  │                            │                          │                       │       slots      │                       │
  │                            │                          │                       │                  │ ensure_catalog_data() │
  │                            │                          │                       │                  │  (ingest gaps if      │
  │                            │                          │                       │                  │   needed)             │
  │                            │                          │                       │                  │ asyncio.to_thread(    │
  │                            │                          │                       │                  │  ResearchEngine       │
  │                            │                          │                       │                  │   .run_walk_forward) ►│ build_walk_forward_
  │                            │                          │                       │                  │                       │  windows()
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │                  │                       │ for each window i:
  │                            │                          │                       │                  │                       │   run_parameter_sweep
  │                            │                          │                       │                  │                       │     on train range
  │                            │                          │                       │                  │                       │   pick best train
  │                            │                          │                       │                  │                       │     config
  │                            │                          │                       │                  │                       │   evaluate(test)
  │                            │                          │                       │                  │                       │   record OOS metrics
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │                  │ progress_callback ───►│ (every config)
  │                            │                          │ UPDATE research_jobs ◄┤                  │   _update_progress()  │
  │                            │                          │  SET progress=…       │                  │                       │
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │                  │ ◄──── report ─────────┤ {windows: [...]}
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │                  │ _finalize_job():      │
  │                            │                          │ UPDATE research_jobs ◄┤                  │   results = report    │
  │                            │                          │  status=completed     │                  │   pick best window    │
  │                            │                          │  best_config = train  │                  │     by test-side      │
  │                            │                          │  best_metrics = test  │                  │     objective         │
  │                            │                          │ INSERT research_      │                  │   write trial rows    │
  │                            │                          │  trials (N rows)      │                  │     (one per window)  │
  │                            │                          │                       │ release_compute_ │                       │
  │                            │                          │                       │  slots(lease) ◄──┤                       │
  │                            │                          │                       │                  │ stop_heartbeat.set()  │
  │                            │                          │                       │                  │                       │
  │                            │                          │                       │                  │ ─── return ───►       │
```

> **Cancellation branch (not drawn above):** if `cancel_requested.is_set()` after the engine returns, the worker takes a different fork at `workers/research_job.py:265-269`: it calls `_mark_cancelled()` and **skips** `_finalize_job()`. No `INSERT research_trials` rows are written; `best_config` / `best_metrics` stay null; the only DB write is `UPDATE research_jobs SET status='cancelled', completed_at=NOW(), progress_message='Cancelled'`. Compute slots are still released in the `finally` block.

A few details that matter when you are debugging:

**The heartbeat task does double duty.** It renews the compute-slot lease every 40s _and_ polls Postgres for a status change. If the API marks the job `cancelled` while the engine is mid-fold, the heartbeat sets `cancel_requested.set()`; the worker only checks the flag _after_ the engine returns (`workers/research_job.py:265`). The engine itself has no early-stop hook, so it runs the full sweep or walk-forward to completion regardless. Once the engine returns, `_finalize_job()` is **bypassed** (`workers/research_job.py:265-269`) and `_mark_cancelled()` is called instead — meaning a cancelled walk-forward writes **zero** trial rows. The leaderboard is empty even if the engine completed every fold. If you need partial results from a cancelled run, recover them from the engine's in-memory `report` via worker logs; the DB has nothing.

**`asyncio.to_thread()` is load-bearing.** `ResearchEngine.run_walk_forward` is synchronous because the underlying `BacktestRunner` is synchronous (Nautilus' `nautilus_trader.backtest.engine.BacktestEngine` is sync; see `docs/nautilus-reference.md`). Wrapping the whole call in `to_thread()` means the worker's event loop stays free to handle the heartbeat task, the cancel poll, and the progress callbacks. The progress callback uses `loop.call_soon_threadsafe()` to schedule async DB writes from inside the sync thread (`workers/research_job.py:174`).

**Compute slots are the rate-limiter, not the queue.** arq's queue is FIFO; the slot semaphore is what enforces concurrency. `acquire_compute_slots()` blocks (with timeout) until N slots are free. If you launch six 4-slot sweeps on a 16-slot cluster, two are picked up immediately and four wait. `ComputeSlotUnavailableError` (`services/compute_slots.py:45`) is the user-visible failure if the wait times out — the worker catches it and writes a friendly message to `error_message`.

**Catalog prep can take minutes.** `ensure_catalog_data()` (`services/nautilus/catalog_builder.py`) is called on every research job. If the requested instruments have gaps in `{DATA_ROOT}/parquet/`, this triggers ingestion before the engine can start. The progress field reads `"Preparing market data catalog"` during this phase.

---

## 4. See, verify, troubleshoot

### `/research/[id]` — what you see

The detail page (`frontend/src/app/research/[id]/page.tsx`) polls `/jobs/{id}` every 3s while status is `pending` or `running`. The layout is:

- **Header card** — strategy name, job type pill, status badge, `started_at` / `completed_at`.
- **Progress card** (visible while running) — `progress` bar with `progress_message` underneath. The message is the engine's view of "what am I doing right now" — `"Running window 3 of 12"`, `"Evaluating candidate 17/24"`, etc.
- **Best result card** (visible when completed) — `best_config` JSON snippet, `best_metrics` summary (Sharpe, total return, max drawdown, trade count). "Promote to Graduation" button posts to `/promotions`.
- **Trials table** — sorted by `objective_value DESC`. Columns: trial number, config snippet (truncated to 80 chars), metrics snippet (top 3 keys), status, objective. Clicking a row opens a drilldown modal with the full config and metrics JSON.
- **OOS plot** (walk-forward only) — train-window vs test-window objective for each fold. A "stable" run has correlated train/test bars; a "look-ahead-leaking" run has dramatic train-test divergence and is the canonical signal to throw it away.

### Verification through the API

If the UI is unavailable, the same things are visible through the API:

```bash
# Did the job complete?
curl -s http://localhost:8800/api/v1/research/jobs/<id> | jq '.status, .progress'

# What was the headline result?
curl -s http://localhost:8800/api/v1/research/jobs/<id> | jq '{best_config, best_metrics}'

# How did the trial leaderboard come out?
curl -s http://localhost:8800/api/v1/research/jobs/<id> \
  | jq '.trials | sort_by(-.objective_value) | .[0:5]'

# Did the per-window OOS plot make sense?
curl -s http://localhost:8800/api/v1/research/jobs/<id> \
  | jq '.results.windows | map({i: .index, train: .best_train_result.metrics.sharpe_ratio, test: .test_result.metrics.sharpe_ratio})'
```

The `results` JSONB blob is the canonical source for plot data — the trial table is the leaderboard, not the chart.

### Heartbeat watchdog behavior

A research worker that dies mid-run leaves its `ResearchJob` row in `status="running"` with a stale `heartbeat_at`. The watchdog (currently a manual operator query, not a daemon) is the SQL:

```sql
SELECT id, worker_id, heartbeat_at, NOW() - heartbeat_at AS stale_for
FROM research_jobs
WHERE status = 'running'
  AND heartbeat_at < NOW() - INTERVAL '5 minutes'
ORDER BY heartbeat_at;
```

A row that is "stale for 5 minutes" with no progress is a dead worker. The recovery path is to flip the row to `failed` manually and re-launch — see § 7. This is the one place in the research pipeline that we have not yet automated; the equivalent supervisor exists for the live path but not for the research path.

### Cancellation polling cadence

The heartbeat task wakes every `compute_slot_lease_seconds / 3` (default 40s) — that is the latency between `POST /jobs/{id}/cancel` and the worker setting the in-process `cancel_requested` flag. But the engine never inspects this flag, so a cancelled job keeps running until the **entire** sweep or walk-forward finishes. The cancellation only takes effect at the worker boundary, where `_finalize_job()` is skipped and `_mark_cancelled()` runs instead. Practical implication: re-pressing cancel does nothing useful, and a "cancelled" job can sit in `status="cancelled"` while the engine is still computing trials in the background.

---

## 5. Common failures

The five failures below cover ~95% of what we have seen during operator runs.

### Bad sweep config — empty grid or wrong shape

The Pydantic schema `ResearchSweepRequest.parameter_grid: dict[str, list[Any]]` (`schemas/research.py:21`) accepts shapes that the engine cannot consume:

```json
{ "parameter_grid": { "fast_period": 10 } }     // ← scalar, not list
{ "parameter_grid": { "fast_period": [] } }     // ← empty list
{ "parameter_grid": {} }                        // ← empty dict
```

The first form fails at Pydantic-validation time (422). The second and third pass validation and reach the engine, where `expand_parameter_grid()` returns an empty list and the run completes immediately with zero trials and `best_config=null`. The job lands in `completed` state with no results, which is technically not a failure but is operationally one. We have a follow-up TODO to upgrade the schema to enforce `min_length=1` on each grid entry; until then, watch for `progress=100` with `len(trials)=0` and re-launch with a valid grid.

### Walk-forward window too tight

`build_walk_forward_windows()` raises `ValueError: No walk-forward windows fit inside the requested date range` when `train_days + test_days > total_days`, or when the step pushes the test window past `end_date` on the first iteration. The worker catches this in the generic `except Exception` block at `workers/research_job.py:281` and writes the message to `error_message`. The fix is on the operator: shorten `train_days` or extend the date range. A common mistake is launching a 252-day train window over a 200-day backtest — the math doesn't work.

### No positive folds (selection criteria fail)

`require_positive_return=True` plus `min_trades=N` in the request body causes `is_stage_eligible()` (`services/research_engine.py:312`) to filter out any candidate that did not produce at least N trades and a positive return. If every candidate fails the filter — which happens with overly tight constraints on a strategy that genuinely is bad on the data — the run completes with `best_config=null` and `best_metrics=null`. Promotion will then 409 with `"Research job has no best result to promote"`. The fix is to relax the constraints or trust the data.

### Compute slot starvation

If the cluster is at slot capacity and your job has been waiting longer than the configured slot-acquire timeout (`compute_slot_wait_seconds`, default **900s** — `core/config.py:227`), the worker raises `ComputeSlotUnavailableError` with a message of the form `"Timed out waiting for {slot_count} compute slot(s) for {job_kind}:{job_id} (limit={limit}, used={used})"` (`services/compute_slots.py:101-105`). The worker catches it, marks the job `failed`, and exits. arq retries the job up to its retry budget; if all retries hit the same starvation, the job stays failed. The recovery is to wait for the cluster to free up and re-launch — re-attempts of the same job have no special handling (see § 6).

### Worker timeout / heartbeat-stale

If the worker process dies (OOM kill, OS-level timeout, kernel panic), the `finally` block in `run_research_job` does not execute. The compute-slot lease expires after `compute_slot_lease_seconds` (120s default) and the slot is reclaimed by the next consumer. The `ResearchJob` row, however, is still `running`. The watchdog query in § 4 is how you find these; the manual recovery is `UPDATE research_jobs SET status='failed', error_message='Worker died — restart job manually' WHERE id = '...'`.

A subtler variant: if the worker is alive but Postgres has been disconnected when the heartbeat runs, the heartbeat exception is swallowed (`workers/research_job.py:333`) and the job continues running blind. The next heartbeat will likely succeed, but if Postgres stays down the engine will eventually fail to write trial rows in `_finalize_job()` — the run will look successful in logs but the leaderboard will be empty.

---

## 6. Idempotency and retry behavior

**The same `(strategy_id, sweep_config, window_config)` produces a deterministic trial set.** The engine seeds `BacktestRunner` from the strategy's `code_hash` plus the canonical config, so re-running the same job (different `ResearchJob.id`) over the same Parquet snapshot gives the same trials and the same headline. This is the property that lets you re-launch a failed run with confidence.

**Cancel + re-launch creates a new job.** There is no "resume" semantic. Cancelling a job sets `status="cancelled"`; re-launching means a new `POST /research/sweeps`, which creates a new `ResearchJob` row with a new id, fresh trial numbering, and a fresh queue job id. The audit trail keeps both rows — you can see `[cancelled, completed]` for the same `(strategy, params)` pair. This is by design; "resume" is too coupled to internal engine state to expose safely.

**arq retries the worker function on transient errors.** If the worker raises a non-domain exception (network blip, transient DB error during state transitions), arq's retry budget kicks in. The `attempt` column on `ResearchJob` increments on each `_mark_running()`. By the time `attempt > 1`, you should treat the job with suspicion — something flapped.

**The cancel poll is monotonic.** Once `cancel_requested` is set, the worker does not un-set it. Even if the API later un-cancels (which it cannot in the current code path), the worker would still mark the job cancelled at the end. This is intentional: an operator cancellation should never be silently revoked.

**There is no row-level dedup.** Two simultaneous `POST /research/sweeps` calls with identical bodies create two separate `ResearchJob` rows. The system does not look for "the same sweep is already running." If the operator double-clicks the launch button, two jobs go out. The cluster slot semaphore will prevent them from running in parallel beyond capacity, but it will not deduplicate them.

---

## 7. Rollback and repair

### Cancel a running job

```bash
# API
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8800/api/v1/research/jobs/<id>/cancel

# CLI
msai research cancel <id>
```

Effect: status flips to `cancelled` immediately for `pending`; for `running`, the heartbeat task picks it up within ~40s and sets the in-process `cancel_requested` flag, but the engine has no early-stop hook — it runs the full sweep/walk-forward to completion. Once the engine returns, the worker sees the flag, skips `_finalize_job()`, and calls `_mark_cancelled()` to write the terminal state. **No trial rows are written for a cancelled run.** Compute slots are released in the `finally` block regardless.

### Delete a research job (cascade)

`research_trials.research_job_id` has `ON DELETE CASCADE` (`models/research_trial.py:35`). Deleting a `ResearchJob` row deletes its trials in one statement:

```sql
DELETE FROM research_jobs WHERE id = '<job-id>';
```

This is the right move when a run is permanently broken (e.g., the strategy has been rewritten, or the data window was wrong) and you do not want it cluttering `/research`. **Watch out:** the FK from `graduation_candidates.research_job_id → research_jobs.id` (`models/graduation_candidate.py:36-38`) has **no `ON DELETE` clause** — it defaults to NO ACTION. If any `GraduationCandidate` row still points at this job, the `DELETE` will **fail with a Postgres FK violation**, not silently leave a dangling pointer. Always check first:

```sql
SELECT id, stage FROM graduation_candidates WHERE research_job_id = '<job-id>';
```

If rows come back, you have two choices before retrying the delete:

```sql
-- Option A: detach the candidates from this job (the column is nullable)
UPDATE graduation_candidates SET research_job_id = NULL WHERE research_job_id = '<job-id>';

-- Option B: archive the candidate(s) graduation-side first — see how-graduation-works.md
```

Then rerun the `DELETE FROM research_jobs ...`. The trial rows cascade automatically (`research_trials.research_job_id` has `ON DELETE CASCADE` — `models/research_trial.py:35`).

### Recovery from a stale running job

Use the watchdog query in § 4 to find dead workers, then:

```sql
UPDATE research_jobs
   SET status = 'failed',
       error_message = 'Worker died — manually marked failed',
       completed_at = NOW()
 WHERE id = '<id>';
```

Then re-launch via the API. The original `ResearchJob` row stays in the DB as a record of the dead run.

### Promotion is one-way

`POST /api/v1/research/promotions` creates a `GraduationCandidate` row at `stage="discovery"`. There is no "un-promote" endpoint — to revert a promotion, you go to the graduation pipeline and either move the candidate to `archived` (the terminal stage) or delete the candidate row directly. This is intentional: graduation owns the lifecycle of candidates, and research's only role is to seed them. If you discover a bad result was promoted, that is a graduation-side problem, and the audit trail (`graduation_stage_transitions`) keeps a record.

The promotion handler does check that `ResearchJob.status == "completed"` (`api/research.py:267`) — you cannot promote a cancelled or failed run. This is the only sanity gate; it does not check that the metrics are good.

---

## 8. Key files

### Backend

| Concern                  | Path                                                    | Anchor                                                                                                           |
| ------------------------ | ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| API router               | `backend/src/msai/api/research.py`                      | `router = APIRouter(prefix="/api/v1/research")` (line 44)                                                        |
| Sweep endpoint           | `backend/src/msai/api/research.py`                      | `submit_parameter_sweep()` (line 52)                                                                             |
| Walk-forward endpoint    | `backend/src/msai/api/research.py`                      | `submit_walk_forward()` (line 99)                                                                                |
| List / show / cancel     | `backend/src/msai/api/research.py`                      | lines 144 / 175 / 209                                                                                            |
| Promotion endpoint       | `backend/src/msai/api/research.py`                      | `promote_research_result()` (line 254)                                                                           |
| Strategy path guard      | `backend/src/msai/api/research.py`                      | `_resolve_strategy_path()` (line 342)                                                                            |
| Pydantic schemas         | `backend/src/msai/schemas/research.py`                  | `ResearchSweepRequest`, `ResearchWalkForwardRequest` (lines 12, 32)                                              |
| Job model                | `backend/src/msai/models/research_job.py`               | `class ResearchJob` (line 20)                                                                                    |
| Trial model              | `backend/src/msai/models/research_trial.py`             | `class ResearchTrial` (line 20)                                                                                  |
| Worker entry             | `backend/src/msai/workers/research_job.py`              | `run_research_job()` (line 56)                                                                                   |
| Heartbeat / cancel poll  | `backend/src/msai/workers/research_job.py`              | `_heartbeat_loop()` (line 100)                                                                                   |
| Finalization             | `backend/src/msai/workers/research_job.py`              | `_finalize_job()` (line 355)                                                                                     |
| Engine — sweep           | `backend/src/msai/services/research_engine.py`          | `ResearchEngine.run_parameter_sweep()` (line 534)                                                                |
| Engine — walk-forward    | `backend/src/msai/services/research_engine.py`          | `ResearchEngine.run_walk_forward()` (line 764)                                                                   |
| Window builder           | `backend/src/msai/services/research_engine.py`          | `build_walk_forward_windows()` (line 119)                                                                        |
| Objective extraction     | `backend/src/msai/services/research_engine.py`          | `extract_objective_value()` (line 166)                                                                           |
| Search-strategy resolver | `backend/src/msai/services/research_engine.py`          | `resolve_search_strategy()` (line 189)                                                                           |
| Compute slots            | `backend/src/msai/services/compute_slots.py`            | `acquire_compute_slots()` (line 54)                                                                              |
| Catalog prep             | `backend/src/msai/services/nautilus/catalog_builder.py` | `ensure_catalog_data()`                                                                                          |
| Settings                 | `backend/src/msai/core/config.py`                       | `research_queue_name` (line 172), `research_max_parallelism` (line 221), `compute_slot_lease_seconds` (line 228) |
| CLI sub-app              | `backend/src/msai/cli.py`                               | `research_app` (line 90), commands at lines 400 / 412 / 421                                                      |

### Frontend

| Concern        | Path                                               | Notes                                                                          |
| -------------- | -------------------------------------------------- | ------------------------------------------------------------------------------ |
| List page      | `frontend/src/app/research/page.tsx`               | Polls `/jobs` every 5s while jobs are active                                   |
| Detail page    | `frontend/src/app/research/[id]/page.tsx`          | Polls `/jobs/{id}` every 3s; renders trials, OOS plot, Promote button          |
| Launch dialog  | `frontend/src/components/research/launch-form.tsx` | Toggles between sweep and walk-forward; serializes JSON for grid + base config |
| Status helpers | `frontend/src/lib/status.ts`                       | `statusColor()`, `jobTypeLabel()`                                              |

### Migrations

| Concern                                      | Path                                                                     |
| -------------------------------------------- | ------------------------------------------------------------------------ |
| `research_jobs` table create                 | `backend/alembic/versions/*` (find by `op.create_table("research_jobs"`) |
| `research_trials` table create               | same                                                                     |
| Lifecycle columns (queue, worker, heartbeat) | added in a follow-up migration after the initial table                   |

---

## Cross-references

- **Previous:** [How Backtesting Works](how-backtesting-works.md) — research is N backtests fanned out; if you do not have one passing backtest, do not start a sweep.
- **Next:** [How Graduation Works](how-graduation-works.md) — once you have a `GraduationCandidate` at `stage="discovery"`, the promotion lifecycle is graduation's job.
- **Sibling:** [How Backtest Portfolios Work](how-backtest-portfolios-work.md) — the doc that does **not** own walk-forward. Codex's planning-time observation: walk-forward at the portfolio level would mean "research over a multi-strategy basket," which is a research job, not a portfolio operation. The split is binding.

---

**Date verified against codebase:** 2026-04-28
