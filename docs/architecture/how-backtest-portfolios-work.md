<!-- forge:doc how-backtest-portfolios-work -->

> ### Naming alert — read this before anything else
>
> **There are two unrelated "portfolio" domains in MSAI v2.** They share the word and the candidate input — and otherwise nothing.
>
> | Domain                 | This doc?      | API URL                   | API file (singular vs plural)        | UI route                |
> | ---------------------- | -------------- | ------------------------- | ------------------------------------ | ----------------------- |
> | **Backtest portfolio** | **Yes — here** | `/api/v1/portfolios`      | `backend/src/msai/api/portfolio.py`  | `/portfolio` (frontend) |
> | **Live portfolio**     | No — see doc 7 | `/api/v1/live-portfolios` | `backend/src/msai/api/portfolios.py` | `/live-trading`         |
>
> The split is not a typo. The **file name is singular** (`portfolio.py`); the **URL is plural** (`/api/v1/portfolios`) for RESTful consistency. The live-trading domain is the opposite: file `portfolios.py` (plural), URL `/api/v1/live-portfolios`. Codex caught this collision during planning and we kept them strictly separate to keep the data model honest. A backtest portfolio is **read-only analysis** — an allocation across `GraduationCandidate` rows that you backtest as a basket. It does NOT submit orders, does NOT touch IB Gateway, does NOT spawn a TradingNode. Going to live trading happens in [doc 7 — How Live Portfolios and IB Accounts Work](how-live-portfolios-and-ib-accounts.md), which is a separate `LivePortfolio → LivePortfolioRevision → LiveDeployment` chain.

---

# How Backtest Portfolios Work

A backtest portfolio is the next step after [graduation](how-graduation-works.md). You take a set of `GraduationCandidate` rows — typically ones that survived research, walk-forward, and your editorial gate (graduation has stages `discovery → validation → paper_candidate → paper_running → paper_review → live_candidate → live_running`, plus `paused` / `archived`; there is no literal `approved` stage). Now you decide: _which candidates run together, how is capital split between them, and how do they perform as a basket?_ That's a backtest portfolio.

> **Note on stage enforcement.** The portfolio service does NOT check `GraduationCandidate.stage` at portfolio composition time. `PortfolioService.create` (`services/portfolio_service.py:115-119`) only validates that the referenced candidate rows exist. Filtering "what's eligible to allocate" is an editorial / UI responsibility today, not a service-side constraint — see "Constraint: candidates must exist (NOT must-be-approved)" below.

## The Component Diagram

```
                   ┌─ GraduationCandidate ROWS (any stage) ──────┐
                   │                                             │
                   │   [Cand A: EMA-Cross AAPL]   weight=0.4     │
                   │   [Cand B: RSI-Mean SPY ]    weight=None    │
                   │   [Cand C: MA-Diff QQQ ]    weight=None    │
                   │                                             │
                   └────────────────────┬────────────────────────┘
                                        │
                                        ▼
                   ┌─ PortfolioCreate (validate + persist) ──────┐
                   │                                             │
                   │   POST /api/v1/portfolios                   │
                   │   PortfolioService.create()                 │
                   │     • dedupe candidate_ids                  │
                   │     • assert each candidate exists          │
                   │     • objective ∈ {equal_weight, manual,    │
                   │         maximize_profit, _sharpe, _sortino} │
                   │   →  Portfolio (UUID)                       │
                   │   →  PortfolioAllocation rows               │
                   │                                             │
                   └────────────────────┬────────────────────────┘
                                        │
                                        ▼
                   ┌─ PortfolioRun (allocation engine) ──────────┐
                   │                                             │
                   │   POST /api/v1/portfolios/{id}/runs         │
                   │   PortfolioService.create_run()             │
                   │     status=pending → enqueue arq job        │
                   │                                             │
                   └────────────────────┬────────────────────────┘
                                        │
                                        ▼
                   ┌─ Portfolio Worker (run_portfolio_job) ──────┐
                   │                                             │
                   │   1. mark_run_running                       │
                   │   2. acquire compute slots (Redis lease)    │
                   │   3. resolve weights:                       │
                   │        • explicit (manual objective) OR     │
                   │        • heuristic from candidate metrics   │
                   │      → normalize to sum 1.0                 │
                   │   4. fan-out per-candidate backtests        │
                   │      via BacktestRunner subprocess          │
                   │      (max_workers ≤ compute_slot_lease)     │
                   │   5. combine weighted returns               │
                   │   6. apply leverage + downside-target scale │
                   │   7. compute portfolio metrics (vs benchmark)│
                   │   8. QuantStats tearsheet → report.html     │
                   │                                             │
                   └────────────────────┬────────────────────────┘
                                        │
                                        ▼
                   ┌─ Persisted PortfolioRun (status=completed) ─┐
                   │                                             │
                   │   metrics      JSONB  (sharpe, return, dd…) │
                   │   series       JSONB  (equity / drawdown)   │
                   │   allocations  JSONB  (per-candidate result)│
                   │   report_path  →  reports/portfolio/...html │
                   │                                             │
                   └─────────────────────────────────────────────┘
                                        │
                                        ▼
                          ┌─ Read-only audit ─┐
                          │  /portfolio       │
                          │  Recent Runs table│
                          │  → report iframe  │
                          └───────────────────┘
```

The flow is a fan-out / fan-in: one portfolio run dispatches N candidate backtests, waits for all of them, and folds the per-candidate equity curves into a single weighted basket. Nothing in this flow touches IB Gateway, NautilusTrader's `TradingNode`, or any live order surface — the only Nautilus invocation is the same `BacktestRunner` subprocess used by `/api/v1/backtests/run` (see [doc 3](how-backtesting-works.md)), called once per allocated candidate.

---

## TL;DR

A backtest portfolio is **allocation + portfolio-level backtest**. It takes a set of `GraduationCandidate` rows (which carry their own `(strategy, config, instruments)` triple frozen from research promotion), assigns weights, and runs every candidate's backtest in parallel against the same `(start_date, end_date)` window. The worker normalizes weights, combines per-candidate returns into a single equity curve, applies portfolio-level leverage and downside-target scaling, and produces metrics + a QuantStats tearsheet. **This is purely analysis** — it never submits an order, never connects to IB, never wraps anything in `LivePortfolioRevisionStrategy`. Promoting a vetted backtest portfolio into a live trading deployment is a separate, manual step covered in [doc 7](how-live-portfolios-and-ib-accounts.md).

---

## Table of Contents

1. [Concepts and data model](#1-concepts-and-data-model)
2. [The three surfaces (parity table)](#2-the-three-surfaces-parity-table)
3. [Internal sequence diagram](#3-internal-sequence-diagram)
4. [See / Verify / Troubleshoot](#4-see--verify--troubleshoot)
5. [Common Failures](#5-common-failures)
6. [Idempotency / Retry Behavior](#6-idempotency--retry-behavior)
7. [Rollback / Repair](#7-rollback--repair)
8. [Key Files](#8-key-files)

---

## 1. Concepts and data model

Three tables hold the entire backtest-portfolio domain. They live alongside the strategy / backtest / graduation tables but never reference each other beyond the foreign keys called out below.

### `Portfolio` — the basket definition

The `Portfolio` row (`backend/src/msai/models/portfolio.py:17-45`) is the durable definition: a name, an objective (how weights are derived when an allocation omits its own), capital sizing, and an optional benchmark. It has no schedule, no run history, no execution state. Once created, it's an inert configuration — until you point a `PortfolioRun` at it.

| Column                    | Type                    | Notes                                                                                                    |
| ------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------- |
| `id`                      | `UUID`                  | PK                                                                                                       |
| `name`                    | `String(128)`           | Operator-facing label                                                                                    |
| `description`             | `Text`                  | Free-form notes                                                                                          |
| `objective`               | `String(64)`            | One of `equal_weight`, `manual`, `maximize_profit`, `maximize_sharpe`, `maximize_sortino`                |
| `base_capital`            | `Numeric(18,2)`         | Starting capital in dollars                                                                              |
| `requested_leverage`      | `Numeric(8,4)`          | Multiplier applied during return aggregation; default 1.0                                                |
| `downside_target`         | `Numeric` (nullable)    | Optional drawdown ceiling — drives leverage scaling at run time                                          |
| `benchmark_symbol`        | `String(32)` (nullable) | E.g. `"SPY"`; loaded by `_load_benchmark_returns()`                                                      |
| `account_id`              | `String(64)` (nullable) | Currently unused; reserved on the schema (no readers in `services/`, `workers/`, or `api/portfolio.py`). |
| `created_by`              | `UUID` (FK)             | User who created it                                                                                      |
| `created_at`/`updated_at` | timestamps              | Standard `TimestampMixin`                                                                                |

The `objective` field is stored as a `String(64)` rather than a Postgres enum — `PortfolioService.create` writes `data.objective.value` into the column (`portfolio_service.py:98`), and `_coerce_objective` (`portfolio_service.py:843-861`) accepts the enum or its string value on the way back out, with a legacy alias for `max_sharpe`. This is mostly a "StrEnum + SQLAlchemy is simpler than a DB enum" choice, with the side benefit that adding a new objective doesn't require an `ALTER TYPE`. The Pydantic boundary still rejects unknown values via the `PortfolioObjective` enum (see schema validation in §5).

### `PortfolioAllocation` — one weight per candidate

`PortfolioAllocation` (`backend/src/msai/models/portfolio_allocation.py:19-48`) is the join row between a `Portfolio` and a `GraduationCandidate`. The unique constraint `(portfolio_id, candidate_id)` makes it impossible to allocate the same candidate twice in the same portfolio.

| Column         | Type                      | Notes                                                          |
| -------------- | ------------------------- | -------------------------------------------------------------- |
| `id`           | `UUID`                    | PK                                                             |
| `portfolio_id` | `UUID` (FK, CASCADE)      | Deleting the portfolio removes all its allocations             |
| `candidate_id` | `UUID` (FK)               | Targets `graduation_candidates.id`                             |
| `weight`       | `Numeric(8,6)` (nullable) | Explicit weight (>0, ≤1) — `None` means "derive heuristically" |
| `created_at`   | timestamp                 |                                                                |

Note that `PortfolioAllocation` has only `created_at` — no `updated_at`. Allocations are immutable; to change a weight or candidate set, recreate the portfolio (the `Portfolio` table itself does carry `created_at`/`updated_at` via `TimestampMixin`).

A `null` weight is meaningful: it says _"defer to the portfolio's objective for this slot."_ At run time, the service resolves nulls via `_heuristic_weight()` (using the candidate's own metrics), then normalizes the full vector to sum to 1.0. This is why no `weights-must-sum-to-1` request validator exists at the API boundary — the normalization layer cleans up whatever you submit.

### `PortfolioRun` — the executed backtest

A `PortfolioRun` (`backend/src/msai/models/portfolio_run.py:20-61`) is the artifact of running the basket against a date range. Multiple runs can target the same portfolio with different windows.

| Column                                       | Type                 | Notes                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                                         | `UUID`               | PK                                                                                                                                                                                                                                                                                                                                                       |
| `portfolio_id`                               | `UUID` (FK)          | **Not** CASCADE — see §7 for the rollback consequence                                                                                                                                                                                                                                                                                                    |
| `created_by`                                 | `UUID` (FK)          | User who triggered the run                                                                                                                                                                                                                                                                                                                               |
| `status`                                     | `String(32)`         | `pending` → `running` → `completed` \| `failed`                                                                                                                                                                                                                                                                                                          |
| `metrics`                                    | `JSONB`              | Keys: `total_return`, `sharpe`, `sortino`, `max_drawdown`, `win_rate`, `annualized_volatility`, `downside_risk`, `alpha`, `beta`, plus orchestrator-added `num_strategies`, `effective_leverage`. (No `profit_factor`; no `sharpe_ratio`/`sortino_ratio` — see `services/analytics_math.py:71-94, 119-161` and `services/portfolio_service.py:497-507`.) |
| `series`                                     | `JSONB`              | Time-series arrays (equity, drawdown, returns) — multi-MB on completed runs                                                                                                                                                                                                                                                                              |
| `allocations`                                | `JSONB`              | Per-candidate contribution: weight used, individual metrics, equity curve                                                                                                                                                                                                                                                                                |
| `report_path`                                | `String(512)`        | Path to QuantStats HTML under `{DATA_ROOT}/reports/portfolio/…`                                                                                                                                                                                                                                                                                          |
| `start_date`                                 | `Date`               | Backtest window start                                                                                                                                                                                                                                                                                                                                    |
| `end_date`                                   | `Date`               | Backtest window end                                                                                                                                                                                                                                                                                                                                      |
| `max_parallelism`                            | `Integer` (nullable) | Caller-requested fan-out cap; clamped to `compute_slot_limit`                                                                                                                                                                                                                                                                                            |
| `error_message`                              | `Text` (nullable)    | Set on `failed` status                                                                                                                                                                                                                                                                                                                                   |
| `heartbeat_at`                               | timestamp            | Worker's lease-renewal pulse (used by future stale-job scanner)                                                                                                                                                                                                                                                                                          |
| `created_at` / `updated_at` / `completed_at` | timestamps           |                                                                                                                                                                                                                                                                                                                                                          |

Because `series` and `allocations` can grow into the megabytes for completed runs, `PortfolioService.list_runs()` (`backend/src/msai/services/portfolio_service.py:247-286`) explicitly defers those columns and expunges rows from the session — paginating the runs list never lazy-loads multi-MB JSONB blobs. `get_run()` (`backend/src/msai/services/portfolio_service.py:288`) returns the full payload for the detail surface.

### Allocation objectives — what `objective` actually controls

The objective only matters when at least one allocation has `weight=None`. If every allocation comes in with an explicit weight, objectives `equal_weight` / `maximize_*` and `manual` produce identical results.

The exact JSONB keys `_heuristic_weight` reads are listed below — match these spellings when writing graduation-candidate `metrics` payloads, otherwise the heuristic silently falls through to `1.0` (equal pre-normalization weight) and you get equal-weighting under what looks like an objective-driven portfolio. Source: `services/portfolio_service.py:864-879`.

| Objective          | Behavior on `weight=None`                                              |
| ------------------ | ---------------------------------------------------------------------- |
| `equal_weight`     | Each null-weight slot gets `1.0`; final normalization divides by count |
| `manual`           | **Forbidden** — Pydantic rejects with 422 (see schema validator in §5) |
| `maximize_profit`  | Reads `metrics["total_return"]` from the candidate                     |
| `maximize_sharpe`  | Reads `metrics["sharpe"]` (NOT `sharpe_ratio`) from the candidate      |
| `maximize_sortino` | Reads `metrics["sortino"]` (NOT `sortino_ratio`) from the candidate    |

For all three `maximize_*` paths the helper additionally clamps with `max(value, 0.0) or 1.0` — a zero, negative, or missing metric falls back to `1.0` so the candidate still participates in the basket; normalization downstream rescales the weight proportionally.

The legacy alias `max_sharpe` is translated to `maximize_sharpe` by a `field_validator(mode="before")` on `PortfolioCreate.objective` (`backend/src/msai/schemas/portfolio.py:58-62`).

### Constraint: candidates must exist (NOT stage-gated)

The plan for this doc anticipated that allocation would be gated by `GraduationCandidate.stage`. **The current implementation only checks existence, not stage.** There is no `approved` stage in the graduation state machine — see `services/graduation.py` for the canonical 9-state set (`discovery / validation / paper_candidate / paper_running / paper_review / live_candidate / live_running / paused / archived`) — so even if the service did stage-gate, "approved-only" wouldn't be the right gate. Reading `PortfolioService.create()` (`backend/src/msai/services/portfolio_service.py:115-119`):

```python
for alloc in data.allocations:
    candidate = await session.get(GraduationCandidate, alloc.candidate_id)
    if candidate is None:
        raise ValueError(f"Graduation candidate {alloc.candidate_id} not found")
```

This is a known gap. Operators are expected to filter by stage in the UI / before composing the request. The graduation lifecycle in [doc 5](how-graduation-works.md) is the editorial gate.

### Weight resolution — a worked example

Suppose you create a portfolio with `objective: "maximize_sharpe"` and three allocations:

```jsonc
{
  "objective": "maximize_sharpe",
  "allocations": [
    { "candidate_id": "A", "weight": 0.5 }, // explicit
    { "candidate_id": "B" }, // null → heuristic
    { "candidate_id": "C" }, // null → heuristic
  ],
}
```

At run time, `_resolve_allocations()` (`portfolio_service.py:663-727`) walks the rows:

1. **Candidate A** has `weight=0.5` — passed through verbatim.
2. **Candidate B** has `weight=None` — `_heuristic_weight(B.metrics, MAXIMIZE_SHARPE)` reads `B.metrics["sharpe"]` and returns it as the raw weight. Suppose it returns `1.8`.
3. **Candidate C** likewise: returns `1.2`.

The raw vector is `[0.5, 1.8, 1.2]`. `normalize_weights()` divides by the sum (3.5) to get `[0.143, 0.514, 0.343]`. **The explicit `0.5` you submitted for A becomes ~0.143 after normalization.** This is intentional — the heuristic-derived candidates dominate when their objective scores are large. If you wanted to pin A at 0.5 exactly, use `objective: "manual"` and provide explicit weights for B and C too.

The allocations JSONB on the completed run echoes the **post-normalization** weight in each entry, so the UI report always shows the actual capital fraction, not the request payload. This is the source of truth for "how much did each candidate contribute."

---

## 2. The three surfaces (parity table)

Every backtest-portfolio operation has API + CLI + UI parity, with one exception called out in the table: **portfolio _creation_ has no CLI command yet.** Use the API or the UI for that step. Everything else has all three surfaces.

| Intent                      | API                                             | CLI                                                           | UI                                                                       | Observe / Verify                                                               |
| --------------------------- | ----------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| Create portfolio            | `POST /api/v1/portfolios`                       | _(no CLI; use API or UI)_                                     | `/portfolio` → "Create Portfolio" button (top-right) opens dialog        | 201 Created with `PortfolioResponse` body; row visible in list                 |
| List portfolios             | `GET /api/v1/portfolios?limit=N`                | `msai portfolio list --limit N`                               | `/portfolio` "Portfolios" card (top)                                     | `items[]`, `total` count                                                       |
| Show portfolio detail       | `GET /api/v1/portfolios/{id}`                   | `msai portfolio show {id}`                                    | _(no UI surface for full detail)_ — use API or CLI                       | Allocations + per-allocation weights are NOT rendered in the UI today (see §4) |
| Run portfolio backtest      | `POST /api/v1/portfolios/{id}/runs`             | `msai portfolio run {id} {start} {end} [--max-parallelism N]` | `/portfolio` → "Run Backtest" button per portfolio row opens dialog      | 201 Created with `PortfolioRunResponse` (status=`pending`)                     |
| List runs (all)             | `GET /api/v1/portfolios/runs?limit=N`           | `msai portfolio runs --limit N`                               | `/portfolio` → "Recent Runs" card (bottom)                               | `items[]` (series/allocations deferred — small payload)                        |
| List runs for one portfolio | `GET /api/v1/portfolios/runs?portfolio_id={id}` | `msai portfolio runs --portfolio-id {id}`                     | _(no per-portfolio filter in UI)_ — Recent Runs shows all                | Filtered by FK at API/CLI                                                      |
| Show run detail             | `GET /api/v1/portfolios/runs/{run_id}`          | _(no dedicated CLI; use `runs --limit 1` or curl)_            | _(no detail page or row click-through)_                                  | Full payload incl. metrics + series + allocations JSONB available via API      |
| Download report             | `GET /api/v1/portfolios/runs/{run_id}/report`   | _(use curl or browser)_                                       | _(no in-UI download link)_ — open the URL directly or fetch via API/curl | Returns `text/html` from `{DATA_ROOT}/reports/...`                             |

All URLs are versioned (`/api/v1/...`) per the [API design rule](../../.claude/rules/api-design.md). All API calls except `/health` and `/ready` require an Azure Entra ID JWT or the `X-API-Key` header (dev/CLI path).

### Route ordering note (FastAPI)

`/api/v1/portfolios/runs` is registered **before** `/api/v1/portfolios/{portfolio_id}` (`backend/src/msai/api/portfolio.py:100` vs `:192`). FastAPI matches routes in declaration order; if `/{portfolio_id}` came first, a request to `/runs` would try to parse `runs` as a UUID and fail with 422. This is a deliberate ordering — when adding new collection routes (e.g. a future `/api/v1/portfolios/templates`), declare them before the path-parameter route too.

### Schema notes per surface

- **API request body** (`PortfolioCreate` at `backend/src/msai/schemas/portfolio.py:40-79`): `name`, `description?`, `objective`, `base_capital`, `requested_leverage` (default 1.0), `downside_target?`, `benchmark_symbol?`, `allocations[]` (min 1, each `{candidate_id, weight?}` with `weight ∈ (0, 1]` if provided).
- **CLI args** (`backend/src/msai/cli.py:548-597`): `portfolio_app` is a Typer sub-app of the top-level `msai` CLI. The `run` subcommand takes positional `start` / `end` (YYYY-MM-DD) and an optional `--max-parallelism`.
- **UI form** (`frontend/src/app/portfolio/page.tsx`): the create-portfolio dialog is `CreatePortfolioDialog` (`page.tsx:99`) and the run-backtest dialog is `RunBacktestDialog` (`page.tsx:421`). Allocations are added one row at a time via an "Add Allocation" button (`page.tsx:325-334`) inside the create dialog. The `objective` select is **hand-rolled, not enum-driven**: only three of the five `PortfolioObjective` values are exposed in the UI (`page.tsx:277-281`) — `maximize_sharpe`, `equal_weight`, `manual`. `maximize_profit` and `maximize_sortino` are valid in the API + service but unreachable from the form, and `objectiveLabel()` / `objectiveColor()` (`page.tsx:54-78`) special-case only those same three. To use the missing objectives, post directly to the API.

---

## 3. Internal sequence diagram

What happens after `POST /api/v1/portfolios/{id}/runs` lands.

```
Client            FastAPI (api/portfolio.py)        PortfolioService           arq Redis        Portfolio Worker         BacktestRunner subprocesses
  │                       │                              │                        │                   │                          │
  │ POST /portfolios/     │                              │                        │                   │                          │
  │   {id}/runs           │                              │                        │                   │                          │
  ├──────────────────────►│                              │                        │                   │                          │
  │                       │  resolve user_id             │                        │                   │                          │
  │                       │  create_run(portfolio_id,    │                        │                   │                          │
  │                       │             start, end,      │                        │                   │                          │
  │                       │             max_parallelism) │                        │                   │                          │
  │                       ├─────────────────────────────►│                        │                   │                          │
  │                       │                              │  validate portfolio    │                   │                          │
  │                       │                              │     exists             │                   │                          │
  │                       │                              │  insert PortfolioRun   │                   │                          │
  │                       │                              │     status=pending     │                   │                          │
  │                       │                              │  flush (NOT commit)    │                   │                          │
  │                       │◄─────────────────────────────┤                        │                   │                          │
  │                       │  enqueue_portfolio_run(      │                        │                   │                          │
  │                       │    pool, run_id, port_id)    │                        │                   │                          │
  │                       ├─────────────────────────────────────────────────────►│                   │                          │
  │                       │  commit + refresh            │                        │                   │                          │
  │ 201 Created           │                              │                        │                   │                          │
  │ {status: "pending"}   │                              │                        │                   │                          │
  │◄──────────────────────┤                              │                        │                   │                          │
  │                       │                              │                        │  job pulled       │                          │
  │                       │                              │                        ├──────────────────►│                          │
  │                       │                              │                        │                   │  read-after-write retry  │
  │                       │                              │  mark_run_running      │                   │  (commit may lag enqueue)│
  │                       │                              │◄──────────────────────────────────────────┤                          │
  │                       │                              │  status=running        │                   │                          │
  │                       │                              ├──────────────────────────────────────────►│                          │
  │                       │                              │                        │                   │  acquire compute slots   │
  │                       │                              │                        │                   │  (Redis lease)           │
  │                       │                              │                        │  lease_id         │                          │
  │                       │                              │                        │◄──────────────────┤                          │
  │                       │                              │                        │                   │  start lease-renewal     │
  │                       │                              │                        │                   │  + heartbeat task        │
  │                       │                              │                        │                   │                          │
  │                       │                              │  run_portfolio_backtest│                   │                          │
  │                       │                              │◄──────────────────────────────────────────┤                          │
  │                       │                              │                        │                   │                          │
  │                       │                              │  Phase 1: load portfolio + allocations     │                          │
  │                       │                              │  selectinload(candidate.strategy)          │                          │
  │                       │                              │  resolve weights:                          │                          │
  │                       │                              │    - explicit OR _heuristic_weight()       │                          │
  │                       │                              │    - normalize to sum 1.0                  │                          │
  │                       │                              │  release session ◄── (no DB held)          │                          │
  │                       │                              │                        │                   │                          │
  │                       │                              │  Phase 2: pre-warm catalogs serially       │                          │
  │                       │                              │  ensure_catalog_data() per asset class     │                          │
  │                       │                              │  (avoids races on cold SPY/AAPL data)      │                          │
  │                       │                              │                        │                   │                          │
  │                       │                              │  Phase 3: fan out N candidate backtests    │                          │
  │                       │                              │  worker_count = min(N, max_workers,        │                          │
  │                       │                              │                     compute_slot_limit)    │                          │
  │                       │                              │                        │                   │  spawn subprocess per    │
  │                       │                              │                        │                   │  allocation              │
  │                       │                              │                        │                   ├─────────────────────────►│
  │                       │                              │                        │                   │                          │  BacktestRunner.run()
  │                       │                              │                        │                   │                          │  Nautilus SIM venue
  │                       │                              │                        │                   │                          │  (per-candidate metrics
  │                       │                              │                        │                   │                          │   + equity curve)
  │                       │                              │                        │                   │◄─────────────────────────┤
  │                       │                              │                        │                   │  collect strategy_results│
  │                       │                              │                        │                   │                          │
  │                       │                              │  Phase 4: combine                          │                          │
  │                       │                              │   - weighted equity sum                    │                          │
  │                       │                              │   - apply requested_leverage               │                          │
  │                       │                              │   - apply downside-target scaling          │                          │
  │                       │                              │   - portfolio metrics vs benchmark         │                          │
  │                       │                              │     (if benchmark_symbol set)              │                          │
  │                       │                              │                        │                   │                          │
  │                       │                              │  Phase 5: ReportGenerator                  │                          │
  │                       │                              │   - QuantStats tearsheet HTML              │                          │
  │                       │                              │   - write to reports/portfolio/...html     │                          │
  │                       │                              │                        │                   │                          │
  │                       │                              │  persist PortfolioRun:                     │                          │
  │                       │                              │    metrics, series,    │                   │                          │
  │                       │                              │    allocations, report_path,               │                          │
  │                       │                              │    status=completed,   │                   │                          │
  │                       │                              │    completed_at=now    │                   │                          │
  │                       │                              ├──────────────────────────────────────────►│                          │
  │                       │                              │                        │                   │  cancel renewal task     │
  │                       │                              │                        │                   │  release_compute_slots() │
  │                       │                              │                        │                   │                          │
  │ (operator polls or                                   │                        │                   │                          │
  │  WS-bound dashboard                                  │                        │                   │                          │
  │  picks up status)                                    │                        │                   │                          │
```

A few things to call out in this flow:

1. **The DB session is released between Phase 1 and Phase 3** (`backend/src/msai/services/portfolio_service.py:386-388`). A 10-minute backtest must not hold a Postgres connection. This is the same pattern the single-strategy backtest worker uses.
2. **`max_workers` is a hard cap**, not a hint. The portfolio worker passes the compute-slot lease size directly into `run_portfolio_backtest()`, and `_execute_candidate_backtests()` clamps `worker_count = min(len(allocations), requested, compute_slot_limit)`. Even if the run row says `max_parallelism=8`, you cannot oversubscribe the cluster semaphore.
3. **Per-candidate failures abort the whole run.** `_execute_candidate_backtests` uses `gather` semantics: any candidate raising propagates and the entire portfolio run fails (`backend/src/msai/services/portfolio_service.py:752-756`). The reasoning is documented inline: a partial backtest would silently dilute the portfolio with a zero-return stream and lie about `num_strategies` in the metrics. **Don't expect partial results.**
4. **arq retries vs data errors.** The worker (`backend/src/msai/workers/portfolio_job.py:230-260`) distinguishes `PortfolioOrchestrationError` / `FileNotFoundError` / `TimeoutError` (deterministic — mark failed, don't re-raise) from generic exceptions (infra — re-raise so arq retries). On the final attempt, even infra errors are marked failed so the operator UI surfaces them rather than the row sitting `running` forever.
5. **Read-after-write guard.** The API enqueues to arq _before_ committing the `PortfolioRun` row. If the worker pulls the job before Postgres has the row visible, `mark_run_running` retries lookups with backoff (`workers/portfolio_job.py:62-63, 108-124` — `_START_LOOKUP_ATTEMPTS = 5`, `_START_LOOKUP_BACKOFF_SECONDS = 0.5`, so the lookup window is ~2.5 s before giving up). This is why you may see a brief delay before status flips to `running`.
6. **Walk-forward at portfolio level is NOT implemented.** Portfolio backtest is per-component fan-out + aggregation only. There is no per-fold weight rebalance, no walk-forward stitching, and no fold-aware aggregation in `run_portfolio_backtest`. `grep -n 'walk\|fold\|rebalance' backend/src/msai/services/portfolio_service.py backend/src/msai/workers/portfolio_job.py backend/src/msai/services/analytics_math.py` returns zero matches. Weights are resolved once before the fan-out (`_resolve_allocations` + `normalize_weights` at `services/portfolio_service.py:663-727`); the combined return series is a single weighted sum produced by `combine_weighted_returns(...)` at `services/portfolio_service.py:482`. Walk-forward analysis lives **inside graduation** ([doc 5](how-graduation-works.md)), not here.
7. **`max_workers` is double-clamped.** The worker computes `slot_count = max(1, min(requested, len(allocations), compute_slot_limit))` before passing it as `max_workers` (`workers/portfolio_job.py:185-188`); the service then re-clamps via `min(len(allocations), requested, compute_slot_limit)` (`portfolio_service.py:758-761`). The double-clamp is intentional defense-in-depth — a high `max_parallelism` from the schema is **not** honored verbatim past the smaller of the cluster budget and the allocation count.

### A worked end-to-end run

A concrete walkthrough, useful as a smoke-test recipe. Assume you already have three `GraduationCandidate` rows you intend to allocate, whose IDs are `A`, `B`, `C` (the service does not stage-gate — it's on you to pick candidates that have meaningful `metrics`).

```bash
# 1. Create the portfolio (no CLI for this step today — use curl or the UI).
curl -sf -H "X-API-Key: $MSAI_API_KEY" -H "Content-Type: application/json" \
  -X POST http://localhost:8800/api/v1/portfolios \
  -d '{
    "name": "Smoke A+B+C",
    "objective": "equal_weight",
    "base_capital": 100000,
    "allocations": [
      {"candidate_id": "A"},
      {"candidate_id": "B"},
      {"candidate_id": "C"}
    ]
  }' | jq -r .id
# → 11111111-2222-3333-4444-555555555555

# 2. Trigger a run.
uv run msai portfolio run 11111111-2222-3333-4444-555555555555 2024-01-01 2025-01-01 \
  --max-parallelism 3
# → {"id": "...", "status": "pending", ...}

# 3. Wait for completion (poll once a second).
RUN_ID="<from step 2>"
until [ "$(curl -sf -H "X-API-Key: $MSAI_API_KEY" \
  http://localhost:8800/api/v1/portfolios/runs/$RUN_ID | jq -r .status)" = "completed" ]; do
  sleep 1
done

# 4. Read the metrics + open the report.
curl -sf -H "X-API-Key: $MSAI_API_KEY" \
  http://localhost:8800/api/v1/portfolios/runs/$RUN_ID | jq '.metrics'
open http://localhost:8800/api/v1/portfolios/runs/$RUN_ID/report
```

If step 3 takes longer than `len(allocations) × backtest_timeout_seconds`, something is wrong — start at the troubleshooting list below.

---

## 4. See / Verify / Troubleshoot

### What you see in the UI

The single page `/portfolio` (`frontend/src/app/portfolio/page.tsx`) is the operator surface — there's no nested `/portfolio/[id]` page in the current build. Sections of the page:

- **Page header "Create Portfolio" button** (top-right, `page.tsx:618-622`): opens `CreatePortfolioDialog`. The trigger is labeled "Create Portfolio" — there is no "+ New Portfolio" button anywhere in the codebase. The empty-state CTA inside the Portfolios card (`page.tsx:646-651`) reads `"No portfolios yet. Click \"Create Portfolio\" to start."`.
- **Portfolios card** (top, `page.tsx:633-710`): a flat `<Table>` with columns `Name | Objective | Capital | Leverage | Benchmark | Created | Run-Backtest button`. Objective is rendered as a colored Tailwind badge from `objectiveColor()` (`page.tsx:67-78`). **There is no inline expander, no per-row reveal, and no UI listing of allocations / candidate IDs / weights.** Inspecting allocations requires `GET /api/v1/portfolios/{id}` (API/CLI).
- **Run Backtest button** per portfolio row (`page.tsx:699-702`): opens `RunBacktestDialog` (start/end date pickers only — there is no max-parallelism input on the run dialog; the API field exists but the form omits it). Submitting POSTs to `/runs` and reloads the table.
- **Recent Runs card** (bottom, `page.tsx:712-771`): newest-first, sorted by `created_at` (`page.tsx:599-602`). Actual columns: `Portfolio | Status | Date Range | Metrics | Created` (`page.tsx:732-739`). The `Metrics` column renders a `metricsSnippet(run.metrics)` (`page.tsx:528-538`) showing `R: <total_return%>` and `DD: <max_drawdown%>` when those fields are present. _Caveat:_ the snippet's Sharpe branch reads `metrics.sharpe_ratio`, but the orchestrator persists `metrics.sharpe` (see §1) — so the `S: …` segment never lights up today. The other two render correctly. **There is no `started_at` column, no `completed_at` column, and no "Report" link column.** Status is a colored badge. To download the report, hit `GET /api/v1/portfolios/runs/{run_id}/report` directly.

The page polls — every load calls `Promise.all([apiGet("/api/v1/portfolios"), apiGet("/api/v1/portfolios/runs")])`. There's no WebSocket for portfolio runs (that's [doc 8](how-real-time-monitoring-works.md), live-deployment territory). To watch a run progress, refresh the page.

### What to verify after triggering a run

| What                              | How                                                                                           |
| --------------------------------- | --------------------------------------------------------------------------------------------- |
| Run was accepted                  | API returned 201 with `status: "pending"`                                                     |
| Worker picked it up               | `GET /api/v1/portfolios/runs/{run_id}` shows `status: "running"`, `heartbeat_at` updating     |
| Per-candidate backtests in flight | Logs: `portfolio_job_started`, then per-candidate logs from `BacktestRunner` subprocesses     |
| Run completed                     | `status: "completed"`, `completed_at` set, `metrics` populated, `report_path` set             |
| Report renders                    | `GET /api/v1/portfolios/runs/{run_id}/report` returns `text/html`                             |
| Per-candidate contribution        | `allocations` JSONB on the run row — one entry per candidate with weight + individual metrics |

### Troubleshooting

- **Run stuck in `pending` for more than a minute.** Worker isn't pulling. Check arq with `docker compose -f docker-compose.dev.yml logs portfolio-worker`. Common cause: workers cached an old import — `./scripts/restart-workers.sh` (see CLAUDE.md "Worker stale-import refresh").
- **Run flips to `running`, then stuck for 30+ minutes.** A single candidate backtest is hung. Check `heartbeat_at` — if it's stale, the worker died; arq will retry on schedule (or hit max_tries and mark failed). If it's fresh, the BacktestRunner subprocess is still working (large date range, many bars).
- **`failed` status with `error_message: "Compute slots unavailable"`.** Cluster semaphore is exhausted. Either another large backtest is running, or a previous run leaked its lease (look for `portfolio_slots_release_failed` in logs). Wait, or restart the worker to drop the orphan lease.
- **`failed` with `FileNotFoundError`.** A candidate's instruments lack Parquet coverage for the requested window. Run the symbol coverage check (see [doc 1](how-symbols-work.md)) before retrying — same window won't suddenly grow data.
- **Report 404.** `report_path` is set but the file is missing on disk. The path-traversal guard (`api/portfolio.py:144-184`) confirms the resolved path is under `{DATA_ROOT}/reports`; if the file is genuinely missing, the run completed but the disk write failed silently. Look for `report_generation_failed` in the structured logs.

---

## 5. Common Failures

These are the failure modes that show up at the API boundary or during run execution. Each one cites the actual enforcement point.

### 5.1 Allocation references a candidate that doesn't exist

```http
POST /api/v1/portfolios
{
  "name": "Test",
  "objective": "equal_weight",
  "base_capital": 100000,
  "allocations": [{"candidate_id": "00000000-0000-0000-0000-000000000000"}]
}
```

→ `422 Unprocessable Entity`. `PortfolioService.create()` raises `ValueError` at `services/portfolio_service.py:117-119`; the router catches it and explicitly maps to 422 via `try/except ValueError` at `api/portfolio.py:80-86` (FastAPI does **not** auto-translate `ValueError` — the mapping is wired in the handler). This is the only existence check today; the candidate's `stage` is **not** validated.

### 5.2 Duplicate candidate in allocations

→ `422 Unprocessable Entity` from the dedupe loop at `portfolio_service.py:108-113`, mapped through the same `try/except ValueError` handler at `api/portfolio.py:80-86`. The same candidate twice in one portfolio is also blocked at the DB level by the unique constraint on `(portfolio_id, candidate_id)`.

### 5.3 Manual objective without explicit weights

```json
{
  "objective": "manual",
  "allocations": [{ "candidate_id": "..." }] // weight omitted!
}
```

→ `422 Unprocessable Entity` from the model validator `_manual_objective_requires_explicit_weights` at `schemas/portfolio.py:63-79`. The error message names the offending candidates so the UI form can highlight them.

### 5.4 Zero or negative weight

`{"weight": 0}` or `{"weight": -0.1}` → `422` from the `Field(default=None, gt=0.0, le=1.0)` constraint at `schemas/portfolio.py:37`. **Use `null` (omit the field entirely) to opt into heuristic derivation; `0` is rejected.**

### 5.5 Weights don't sum to 1.0

This is _not_ rejected at the API. The schema accepts any combination of weights `∈ (0, 1]`; the service `normalize_weights()` divides by the sum at run time. If you submit `[0.5, 0.5, 0.5]`, you get effective weights `[1/3, 1/3, 1/3]` after normalization. If your submission semantics require strict-sum-to-one, enforce that at the UI layer.

### 5.6 Empty allocations

`"allocations": []` → `422` from the `Field(min_length=1)` constraint on `PortfolioCreate.allocations` at `schemas/portfolio.py:56`.

### 5.7 Candidate has no instruments configured

Discovered at run time, not creation time. `_resolve_allocations` checks `candidate.config["instruments"]` (or the strategy's default) and raises `PortfolioOrchestrationError` if the list is empty (`portfolio_service.py:698-701`). The worker classifies `PortfolioOrchestrationError` as deterministic (`workers/portfolio_job.py:230-248`), marks the run `failed`, and does not re-raise — so arq does not retry.

### 5.8 Per-candidate backtest fails

If any single candidate backtest raises during `_execute_candidate_backtests`, the gather aborts and the entire portfolio run is marked `failed`. There is intentionally no partial-completion path. `error_message` will contain the type and message of the first candidate exception. Fix the candidate's data / config and re-run the portfolio (a new `PortfolioRun` row is fine; the failed one stays as audit).

### 5.9 Compute slots unavailable

`ComputeSlotUnavailableError` is treated as a deterministic failure (the lease budget is exhausted; retrying immediately won't help). Run is marked `failed` and arq does **not** retry (`workers/portfolio_job.py:197-205`).

### 5.10 Redis pool unavailable at enqueue

`POST /api/v1/portfolios/{id}/runs` — if `enqueue_portfolio_run()` raises, the API rolls back the `PortfolioRun` row and returns `503 Service Unavailable` (`api/portfolio.py:240-246`). No orphan `pending` row is left behind.

### 5.11 Conflicting symbols across candidates

If two candidates allocate the same instrument (e.g. one trades AAPL, another also trades AAPL), the service does **not** detect this — each runs its own backtest in its own subprocess and the contributions are weighted-summed. This isn't a bug; it's a deliberate property of the basket model (you can deliberately overweight a symbol via two correlated candidates). Verify the resulting `allocations` JSONB if you suspect double exposure.

---

## 6. Idempotency / Retry Behavior

### Determinism of a single run

Given the same `(portfolio_id, start_date, end_date)` AND the same set of allocations AND the same underlying Parquet data AND the same candidate configs (which are frozen on the `GraduationCandidate` row at promotion time), a portfolio run is **deterministic** end-to-end:

- Each candidate backtest is deterministic (NautilusTrader SIM venue, same bars, same code hash).
- Weight resolution is deterministic (heuristics read frozen `candidate.metrics`; explicit weights are explicit).
- Normalization is deterministic.
- Aggregation (`weighted return + leverage + downside scaling`) is deterministic.

You can safely re-run the same `(portfolio_id, start, end)` and expect bit-identical metrics. The intended use is a "what if I re-ran today" sanity check before promoting to live.

### Two runs of the same portfolio with the same dates

Both runs succeed. They produce two separate `PortfolioRun` rows with identical metrics (modulo non-determinism in QuantStats's HTML rendering, which has no semantic effect). The system does not deduplicate — that's a feature: you might genuinely want a re-run to confirm catalog updates haven't drifted the result.

### arq retry policy

- `PortfolioOrchestrationError` / `FileNotFoundError` / `TimeoutError`: mark `failed`, do **not** re-raise. arq does not retry.
- Any other exception: re-raise on attempts < `max_tries` (default 2). On the final attempt, the worker also marks the run `failed` so it's not orphaned.
- `mark_run_running` is guarded by a terminal-state check — once a run is `completed` or `failed`, attempts to flip it back to `running` raise `PortfolioRunTerminalStateError` and the worker bails. The exception class is defined at `services/portfolio_service.py:65-71`; the actual guard fires inside `mark_run_running` at `services/portfolio_service.py:573-580` (`if current.is_terminal: raise PortfolioRunTerminalStateError(...)`); the worker-side consumer is at `workers/portfolio_job.py:114-120`. This is what makes the read-after-write retry safe: even if attempt 2 starts after attempt 1 marked the row `completed`, attempt 2 short-circuits immediately.

### Heartbeat semantics

The `heartbeat_at` column is updated every `_RENEWAL_INTERVAL_SECONDS` by `_renew_lease_forever` (`workers/portfolio_job.py:278-315`). If the worker dies mid-run, the heartbeat goes stale; a future stale-job scanner (a `job_watchdog` extension — referenced in `heartbeat_run`'s docstring at `services/portfolio_service.py:594-601` and in `workers/portfolio_job.py:285-295` / `:328`) will reap rows that have been `running` with a stale heartbeat past a threshold. Today, a crashed worker leaves a `running` row that operators must manually transition to `failed` via direct DB update if needed. This is a known gap.

### Independence of definition and execution

The `Portfolio` row is independent of any `PortfolioRun`. You can:

- Create a portfolio, never run it, leave it indefinitely — no penalty.
- Run the same portfolio with different date windows in parallel — separate runs, separate compute leases, separate report files.
- Modify `requested_leverage` on the portfolio and re-run — but note: there is no PATCH endpoint today. The portfolio is effectively immutable from the API side after creation. To change it, create a new portfolio.

---

## 7. Rollback / Repair

### Deleting a single run

There is **no `DELETE /api/v1/portfolios/runs/{run_id}` endpoint** today. Cleaning up a `failed` or test run requires direct DB intervention, and even that is awkward because:

- The run holds a row in `portfolio_runs`.
- The row references `portfolio_id` via FK without `ondelete` configured on the model side (`models/portfolio_run.py:34-35`).
- The QuantStats HTML on disk under `{DATA_ROOT}/reports/portfolio/...` is not removed by row deletion.

In practice, runs are kept as audit trail. If you must purge a run, delete the row in Postgres manually and (separately) delete the report file. Do not script this — it's rare enough that operator review is the right policy.

### Deleting a portfolio

There is **no `DELETE /api/v1/portfolios/{id}` endpoint either.** If you delete a `portfolios` row directly in the database:

- `PortfolioAllocation` rows cascade-delete (FK has `ondelete="CASCADE"` at `models/portfolio_allocation.py:34`).
- `PortfolioRun` rows do **not** cascade — the FK has no `ondelete` clause (`models/portfolio_run.py:34-35`). If runs exist for that portfolio, the DELETE will fail with a foreign-key violation. Delete the runs first, then the portfolio.
- `GraduationCandidate` rows are not affected — they live in their own table and only the join (`PortfolioAllocation`) goes away.

This is intentionally cumbersome. The graduation domain is the source of truth for "which candidates exist"; the portfolio domain is a basket-level test layered on top. Nothing in MSAI's day-to-day flow requires deleting a portfolio.

### What about IB / live trading?

**Nothing here touches live.** Repeat: a backtest portfolio has zero live-trading footprint. There is no IB connection to drop, no order to cancel, no `TradingNode` subprocess to terminate. Rollback is pure DB cleanup.

The seam to live: a `GraduationCandidate` (from [doc 5](how-graduation-works.md)) is consumed here for backtest-portfolio analysis, AND it is also the same candidate that gets consumed by the **live portfolio** chain in [doc 7](how-live-portfolios-and-ib-accounts.md). But the live path wraps it in a `LivePortfolioRevisionStrategy` row and ignores the backtest-portfolio chain entirely. Promoting a vetted basket from backtest to live is a manual step: the operator reads the backtest portfolio's run report, decides to ship it, and constructs a new live portfolio at `/api/v1/live-portfolios` with the same candidates. There is no automated `POST /portfolios/{id}/promote-to-live` endpoint, by design — the council decided the cognitive break between "tested" and "deployed" is operator-load-bearing.

### Repair playbook for a stuck `running` row

1. Confirm the worker is alive: `docker compose -f docker-compose.dev.yml ps`.
2. Check `heartbeat_at` on the run — if it's < `_RENEWAL_INTERVAL_SECONDS * 3` old, the worker is genuinely working.
3. If stale, restart the portfolio worker: `./scripts/restart-workers.sh`. The lease will time out in Redis and a future stale-job scan (when implemented) will mark the row failed; until then, manually update Postgres: `UPDATE portfolio_runs SET status='failed', error_message='manually marked - stale heartbeat' WHERE id = '...';`.
4. Verify the compute-slot lease was released. `redis-cli -p 6380 KEYS 'compute_slot:portfolio:*'` — orphaned keys can be deleted directly.

---

## 8. Key Files

| Concern                         | Path                                                     | Highlights                                                                                              |
| ------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | --------------- |
| API router                      | `backend/src/msai/api/portfolio.py:36`                   | Router prefix `/api/v1/portfolios`                                                                      |
| List portfolios                 | `backend/src/msai/api/portfolio.py:48`                   | `GET /` — paginated                                                                                     |
| Create portfolio                | `backend/src/msai/api/portfolio.py:73`                   | `POST /` — 201 Created (no `Location` header set); maps `ValueError` → 422 via `try/except` at `:80-86` |
| List runs (collection)          | `backend/src/msai/api/portfolio.py:101`                  | `GET /runs` — declared **before** `/{portfolio_id}` for FastAPI ordering                                |
| Show run                        | `backend/src/msai/api/portfolio.py:123`                  | `GET /runs/{run_id}`                                                                                    |
| Download report                 | `backend/src/msai/api/portfolio.py:145`                  | `GET /runs/{run_id}/report` — path-traversal guarded                                                    |
| Show portfolio                  | `backend/src/msai/api/portfolio.py:193`                  | `GET /{portfolio_id}`                                                                                   |
| Create run                      | `backend/src/msai/api/portfolio.py:219`                  | `POST /{portfolio_id}/runs` — enqueues to arq, rolls back on failure                                    |
| Service entrypoint              | `backend/src/msai/services/portfolio_service.py:74`      | `class PortfolioService`                                                                                |
| Create + validate allocations   | `backend/src/msai/services/portfolio_service.py:77-137`  | Existence check (NOT stage check), dedupe                                                               |
| Create run row                  | `backend/src/msai/services/portfolio_service.py:201`     | Sets status `pending`, returns flushed (not committed)                                                  |
| Run portfolio backtest          | `backend/src/msai/services/portfolio_service.py:331`     | Phase 1 load → Phase 2 fan-out → aggregation → report                                                   |
| Resolve weights + normalize     | `backend/src/msai/services/portfolio_service.py:663-727` | Heuristic vs explicit, normalize to sum 1.0                                                             |
| Per-candidate fan-out           | `backend/src/msai/services/portfolio_service.py:729`     | `worker_count = min(N, max_workers, compute_slot_limit)`                                                |
| Heuristic weight                | `backend/src/msai/services/portfolio_service.py:864`     | Reads candidate metrics by objective                                                                    |
| Service errors                  | `backend/src/msai/services/portfolio_service.py:54-71`   | `PortfolioOrchestrationError`, `PortfolioRunTerminalStateError`                                         |
| Worker entrypoint               | `backend/src/msai/workers/portfolio_job.py:66`           | `run_portfolio_job(ctx, run_id, portfolio_id)`                                                          |
| Worker error classification     | `backend/src/msai/workers/portfolio_job.py:230-260`      | Data error → mark failed; infra → re-raise (final attempt marks)                                        |
| Lease + heartbeat renewal       | `backend/src/msai/workers/portfolio_job.py:278-315`      | `_renew_lease_forever`                                                                                  |
| `Portfolio` model               | `backend/src/msai/models/portfolio.py:17-45`             | `objective`, `base_capital`, `requested_leverage`, …                                                    |
| `PortfolioAllocation` model     | `backend/src/msai/models/portfolio_allocation.py:19-48`  | CASCADE on portfolio_id, unique on (portfolio_id, candidate_id)                                         |
| `PortfolioRun` model            | `backend/src/msai/models/portfolio_run.py:20-61`         | `metrics`, `series`, `allocations` JSONB; `heartbeat_at`                                                |
| `PortfolioCreate` schema        | `backend/src/msai/schemas/portfolio.py:40-79`            | Manual-objective validator; legacy `max_sharpe` translation                                             |
| `PortfolioAllocationInput`      | `backend/src/msai/schemas/portfolio.py:24-37`            | `weight` is `None                                                                                       | float ∈ (0, 1]` |
| Response schemas                | `backend/src/msai/schemas/portfolio.py:82-150`           | `PortfolioResponse`, `PortfolioRunResponse`, list responses                                             |
| CLI sub-app                     | `backend/src/msai/cli.py:548-597`                        | `msai portfolio list / show / runs / run` (no `create`)                                                 |
| Frontend page                   | `frontend/src/app/portfolio/page.tsx`                    | Single-page UI; `CreatePortfolioDialog`, `RunBacktestDialog`                                            |
| Objective label / color helpers | `frontend/src/app/portfolio/page.tsx:54-78`              | `objectiveLabel()`, `objectiveColor()`                                                                  |

---

## See also

- **Previous:** [How Graduation Works →](how-graduation-works.md) — where `GraduationCandidate` rows come from
- **Next:** [How Live Portfolios and IB Accounts Work →](how-live-portfolios-and-ib-accounts.md) — the live-trading chain that consumes the same candidates via a separate path
- [Developer Journey overview](00-developer-journey.md) — full step list
- [How Backtesting Works](how-backtesting-works.md) — the per-candidate backtest mechanism this doc fans out over
- [API design rule](../../.claude/rules/api-design.md) — versioning, error format, status codes
- [NautilusTrader gotchas](../../.claude/rules/nautilus.md) — applies inside the per-candidate backtest subprocesses

---

**Date verified against codebase:** 2026-04-28
