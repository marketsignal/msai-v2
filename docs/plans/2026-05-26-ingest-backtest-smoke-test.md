# ingest-backtest-smoke-test Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Plan revision history:**
>
> | Rev | Date       | Trigger                                                                                                                                                             |
> | --- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
> | v1  | 2026-05-26 | First-pass plan from writing-plans                                                                                                                                  |
> | v2  | 2026-05-26 | Phase 3.3 plan-review iter-1: Codex returned 11 findings (2 P0 + 6 P1 + 2 P2 + 1 P3); Claude review confirmed several; council 2026-05-26 dropped ES from v1 scope. |

**Goal:** Ship an operational workflow smoke that runs the existing ingest → multi-strategy portfolio backtest → metrics-block → report pipeline end-to-end on real Databento data across two equity symbols (AAPL+SPY), via CLI + UI button + nightly schedule + opt-in pre-deploy preflight. ES is deferred to a near-term follow-up per council verdict 2026-05-26.

**Architecture:** Thin additive reuse of the existing `PortfolioRun` pipeline.

1. Alembic migration adds `PortfolioRun.smoke: Boolean` column AND seeds four canonical Strategy rows (`__smoke__/smoke_market_order/AAPL`, `__smoke__/smoke_market_order/SPY`, `__smoke__/ema_cross/AAPL`, `__smoke__/ema_cross/SPY`) via `op.bulk_insert`.
2. A new `services/smoke/` module hosts the hardcoded canonical configs, idempotent Portfolio bootstrap, Redis-based ingest mutex, and the smoke-run orchestrator that pre-ingests via the existing `ingest_symbols(...)` helper then fires the existing `POST /api/v1/portfolios/{id}/runs` with `smoke=True`.
3. The G5 risk-metrics block is wired into `services/portfolio/orchestration.py` where `core = compute_series_metrics(combined_returns, benchmark_returns=...).as_dict()` already runs (~line 482). The smoke path enriches that dict with `pnl`, `trade_count_by_strategy`, `trade_count_total`, `benchmark_symbol`, and `smoke_config`. No new QuantStats wrapper — reuse `compute_series_metrics` + `compute_alpha_beta` from `analytics_math.py`.
4. CLI: `msai backtest smoke [--config nightly] [--json]` under existing `backtest_app`, using the project's `_api_call(...)` helper (cli.py:147). Polls `GET /api/v1/portfolios/runs/{run_id}` until terminal.
5. UI: button on `/backtests` page POSTs to a thin `/api/v1/portfolios/smoke/runs` endpoint (registered BEFORE `/{portfolio_id}/runs` to avoid route shadowing). The endpoint calls `services/smoke/runner.py` directly — no HTTP-to-self.
6. CI: `.github/workflows/smoke.yml` (cron `0 5 * * *` literal + workflow_dispatch). On-VM smoke runs via SSH, invokes `msai backtest smoke --config nightly --json`. Alert dispatch via a new CLI sub-command `msai system smoke-alert` that calls `AlertingService.send_alert(...)` on the VM directly (NOT the read-only `/api/v1/alerts` endpoint). `deploy.yml` gains a `run_smoke` boolean input that adds a preflight job.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy 2.0 + Alembic + arq + NautilusTrader 1.223 + Databento 0.71.0 + QuantStats 0.0.81 (already used via `analytics_math.py`/`report_generator.py`) + redis-py + Typer + Next.js 15 + React + shadcn/ui + Playwright + GitHub Actions.

---

## Approach Comparison

### Chosen Default

Thin additive reuse of the existing PortfolioRun pipeline. One new boolean column on `PortfolioRun`, four pre-seeded Strategy rows (one per `(kind, symbol)` pair across the two equities), idempotent bootstrap by sentinel name, Redis-based ingest mutex, G5 metrics block wired into the existing `compute_series_metrics` call path. CLI + UI + nightly + opt-in preflight all funnel through one `services/smoke/runner.py` orchestrator.

### Best Credible Alternative

Dedicated `/api/v1/smoke-tests` resource + new domain model. Rejected by Codex Contrarian gate.

### Scoring

| Axis                  | Default | Alternative |
| --------------------- | ------- | ----------- |
| Complexity            | L       | H           |
| Blast Radius          | L       | M-H         |
| Reversibility         | H       | L           |
| Time to Validate      | L       | H           |
| User/Correctness Risk | L       | M           |

### Cheapest Falsifying Test

< 30 min spike: POST a 2-strategy payload (`strategy_ids=[smoke_market_order_aapl.id, smoke_market_order_spy.id]`, `objective=mean_return`, `base_capital=100000`) to `POST /api/v1/portfolios` and `POST /api/v1/portfolios/{id}/runs` directly. Confirm a PortfolioRun row is created and the existing worker produces metrics. Risk low — PR #73 merged the portfolio path.

## Contrarian Verdict

**VALIDATE** (Phase 3.1c, 2026-05-26 Codex): "Default exercises the real portfolio pipeline with small additive changes; alternative adds schema/endpoints/UI that do not reduce the main risk; the proposed spike covers the only material uncertainty."

## Council Verdict (Phase 3.3 mid-loop on ES scope fork)

**APPROVE_A** (Phase 3.3 iter-1 escalation, 2026-05-26, 4 advisors APPROVE_A + 1 dissent): Drop ES from v1; track as near-term follow-up. Minority report: Maintainer (Codex) dissented APPROVE_B — concern was PRD/code-scope divergence. Overruled because absorbing futures routing would expand a smoke-test PR into shared instrument-resolution design work.

---

## File Structure

### Files to Create

| Path                                                                             | Responsibility                                                                                                                |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `backend/alembic/versions/<auto>_add_portfolio_run_smoke_and_seed_strategies.py` | Migration: add `smoke: BOOLEAN NOT NULL DEFAULT false` column + index; bulk_insert 4 canonical Strategy rows (sentinel names) |
| `backend/src/msai/services/smoke/__init__.py`                                    | Package marker                                                                                                                |
| `backend/src/msai/services/smoke/config.py`                                      | Hardcoded `SmokeConfig` dataclass + `SMOKE_FAST` + `SMOKE_NIGHTLY` constants (AAPL+SPY only)                                  |
| `backend/src/msai/services/smoke/ingest_lock.py`                                 | Async Redis-based ingest mutex per `(symbol, year-month)` key                                                                 |
| `backend/src/msai/services/smoke/runner.py`                                      | Idempotent canonical Portfolio bootstrap + pre-ingest + fire portfolio run + return run row                                   |
| `backend/src/msai/services/smoke/alert_dispatch.py`                              | Helpers to dispatch smoke result via existing `AlertingService.send_alert(...)`                                               |
| `backend/tests/unit/services/smoke/__init__.py`                                  | Test package marker                                                                                                           |
| `backend/tests/unit/services/smoke/test_config.py`                               | Unit: SmokeConfig fields, fast/nightly window resolution                                                                      |
| `backend/tests/unit/services/smoke/test_ingest_lock.py`                          | Unit: mutex acquire/release/timeout against fakeredis (already in dev extras — no pyproject change)                           |
| `backend/tests/integration/test_smoke_runner.py`                                 | Integration: end-to-end smoke run against test catalog producing PortfolioRun with G5 metrics                                 |
| `frontend/src/components/backtests/run-smoke-button.tsx`                         | "Run smoke" button on `/backtests` page                                                                                       |
| `frontend/src/components/portfolio/metrics-block.tsx`                            | G5 metrics block card                                                                                                         |
| `frontend/tests/e2e/specs/run-smoke-button.spec.ts`                              | Playwright spec for US-002 button                                                                                             |
| `frontend/tests/e2e/specs/metrics-block.spec.ts`                                 | Playwright spec for metrics-block visibility                                                                                  |
| `.github/workflows/smoke.yml`                                                    | Nightly schedule + workflow_dispatch (literal cron, SSH-to-VM)                                                                |
| `tests/e2e/use-cases/backtests/smoke-cli-fast.md`                                | E2E use case — CLI + metrics-from-stdout                                                                                      |
| `tests/e2e/use-cases/backtests/smoke-ui.md`                                      | E2E use case — UI button + metrics-block render                                                                               |
| `tests/e2e/use-cases/backtests/smoke-nightly-schedule.md`                        | E2E use case — nightly schedule SSH path                                                                                      |
| `tests/e2e/use-cases/backtests/smoke-preflight-gate.md`                          | E2E use case — opt-in `run_smoke=true` preflight on deploy.yml                                                                |

### Files to Modify

| Path                                                   | Change                                                                                                                                                                                                                                              |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `backend/src/msai/models/portfolio_run.py`             | Add `smoke: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", index=True)`                                                                                                                                              |
| `backend/src/msai/schemas/portfolio.py`                | Add `smoke: bool = False` to `PortfolioRunCreate` and to `PortfolioRunResponse`                                                                                                                                                                     |
| `backend/src/msai/api/portfolio.py`                    | (1) Register `start_smoke_run` route at `/smoke/runs` BEFORE `/{portfolio_id}/runs` (route ordering). (2) Pass `body.smoke` into `_service.create_run(...)`.                                                                                        |
| `backend/src/msai/services/portfolio/lifecycle.py`     | Pass `body.smoke` to `PortfolioRun(...)` construction                                                                                                                                                                                               |
| `backend/src/msai/services/portfolio/orchestration.py` | At ~line 482 where `core = compute_series_metrics(...).as_dict()` runs, branch on `run.smoke` and add `pnl`, `trade_count_by_strategy`, `trade_count_total`, `benchmark_symbol`, `smoke_config` keys to `core` before persisting into `run.metrics` |
| `backend/src/msai/api/backtests.py`                    | Add `smoke_only: bool = Query(False)` and `include_smoke: bool = Query(False)` filtering on the portfolio branch of `/history`; include `smoke` field on the `BacktestHistoryRow` response                                                          |
| `backend/src/msai/schemas/backtest.py`                 | Add `smoke: bool` to `BacktestHistoryRow`                                                                                                                                                                                                           |
| `backend/src/msai/cli.py`                              | Register `msai backtest smoke [--config fast                                                                                                                                                                                                        | nightly] [--json]`using the project's`\_api_call(...)`helper. Add`msai system smoke-alert <result-file>` for the workflow. |
| `frontend/src/app/backtests/page.tsx`                  | Mount `<RunSmokeButton />` in the header                                                                                                                                                                                                            |
| `frontend/src/app/portfolio/runs/[runId]/page.tsx`     | Render `<MetricsBlock />` ABOVE the existing report-iframe area when `metrics` contains the G5 keys                                                                                                                                                 |
| `.github/workflows/deploy.yml`                         | Add `run_smoke: type=boolean default=false` input + preflight job gated by `if: github.event.inputs.run_smoke == 'true'`                                                                                                                            |
| `docs/how_to_deploy.md`                                | Document the new `-f run_smoke=true` opt-in with the "environment + data preflight, NOT candidate-code validation" framing                                                                                                                          |

### Files NOT Modified

- `backend/src/msai/services/data_sources/databento_client.py` (existing; smoke pre-ingest uses higher-level `ingest_symbols`)
- `backend/src/msai/services/report_generator.py` (existing; smoke metrics use existing `compute_series_metrics`, NOT the QuantStats HTML path)
- `strategies/example/smoke_market_order.py` + `strategies/example/ema_cross.py` (existing strategy classes — referenced from seeded Strategy rows)
- `.github/workflows/build-and-push.yml` (unchanged)

---

## Task Sequence

> Each step is one action, 2-5 minutes. Steps follow Red/Green/Refactor TDD discipline where applicable.

---

### Task 1: Alembic migration — add smoke column + seed 4 Strategy rows

**Files:**

- Create: `backend/alembic/versions/<auto>_add_portfolio_run_smoke_and_seed_strategies.py`
- Modify: `backend/src/msai/models/portfolio_run.py`

- [ ] **Step 1: Add the model column**

In `backend/src/msai/models/portfolio_run.py`, after `status`:

```python
# Smoke-test marker. Distinguishes operator-driven canonical smoke runs
# (per /api/v1/backtests/history?smoke_only=true) from ordinary portfolio
# backtests. PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
smoke: Mapped[bool] = mapped_column(
    Boolean, nullable=False, server_default="false", index=True
)
```

Import `Boolean` from sqlalchemy at the top if missing.

- [ ] **Step 2: Generate the autogenerated migration**

Run: `cd backend && uv run alembic revision --autogenerate -m "add_portfolio_run_smoke_and_seed_smoke_strategies"`

- [ ] **Step 3: Replace upgrade()/downgrade() bodies with the explicit ops**

Edit the autogenerated file to look exactly like:

```python
"""add portfolio_run smoke + seed smoke strategies

Revision ID: <auto>
Revises: <prev>
Create Date: 2026-05-26

PRD: docs/prds/ingest-backtest-smoke-test.md v1.3
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "<auto>"
down_revision = "<prev>"
branch_labels = None
depends_on = None


# Iter-3 fix (Codex iter-2 P0): the Strategy table has no `kind` column.
# Actual NOT NULL columns are `name`, `file_path`, `strategy_class`.
# Optional: `config_class`, `config_schema`, `default_config`.
SMOKE_STRATEGIES: list[dict] = [
    {
        "name": "__smoke__/smoke_market_order/AAPL",
        "file_path": "strategies/example/smoke_market_order.py",
        "strategy_class": "SmokeMarketOrderStrategy",
        "config_class": "SmokeMarketOrderConfig",
        "default_config": {
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
    },
    {
        "name": "__smoke__/smoke_market_order/SPY",
        "file_path": "strategies/example/smoke_market_order.py",
        "strategy_class": "SmokeMarketOrderStrategy",
        "config_class": "SmokeMarketOrderConfig",
        "default_config": {
            "instrument_id": "SPY.NASDAQ",
            "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
    },
    {
        "name": "__smoke__/ema_cross/AAPL",
        "file_path": "strategies/example/ema_cross.py",
        "strategy_class": "EMACrossStrategy",
        "config_class": "EMACrossConfig",
        "default_config": {
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 20,
            "trade_size": 1,
        },
    },
    {
        "name": "__smoke__/ema_cross/SPY",
        "file_path": "strategies/example/ema_cross.py",
        "strategy_class": "EMACrossStrategy",
        "config_class": "EMACrossConfig",
        "default_config": {
            "instrument_id": "SPY.NASDAQ",
            "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 20,
            "trade_size": 1,
        },
    },
]


def upgrade() -> None:
    op.add_column(
        "portfolio_runs",
        sa.Column(
            "smoke",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_portfolio_runs_smoke", "portfolio_runs", ["smoke"], unique=False
    )

    # Seed canonical smoke Strategy rows. Names use the SMOKE_STRATEGY_SENTINEL_PREFIX
    # so the runner can look them up deterministically and so they don't conflict
    # with operator-created strategies. `created_by` was made nullable by a
    # subsequent migration (a1b2c3d4e5f6), so we leave it NULL — these rows
    # are system-seeded, not operator-created.
    conn = op.get_bind()
    for entry in SMOKE_STRATEGIES:
        # Skip if a previous deploy already seeded it (re-runnable in dev/test).
        existing = conn.execute(
            sa.text("SELECT id FROM strategies WHERE name = :name"),
            {"name": entry["name"]},
        ).first()
        if existing is not None:
            continue
        conn.execute(
            sa.text(
                """
                INSERT INTO strategies (
                    id, name, file_path, strategy_class, config_class,
                    default_config, created_at, updated_at
                )
                VALUES (
                    :id, :name, :file_path, :strategy_class, :config_class,
                    CAST(:cfg AS JSONB), now(), now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "name": entry["name"],
                "file_path": entry["file_path"],
                "strategy_class": entry["strategy_class"],
                "config_class": entry["config_class"],
                "cfg": json.dumps(entry["default_config"]),
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM strategies WHERE name LIKE '__smoke__/%'")
    )
    op.drop_index("ix_portfolio_runs_smoke", table_name="portfolio_runs")
    op.drop_column("portfolio_runs", "smoke")
```

If the autogenerate produced spurious ops on other columns, strip them.

- [ ] **Step 4: Apply the migration**

Run: `cd backend && uv run alembic upgrade head`
Expected: clean apply, no errors.

- [ ] **Step 5: Verify the seeded rows**

```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U msai -d msai -c "SELECT name, file_path, strategy_class, config_class FROM strategies WHERE name LIKE '__smoke__/%' ORDER BY name;"
```

Expected: 4 rows.

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/*_add_portfolio_run_smoke_and_seed_strategies.py backend/src/msai/models/portfolio_run.py
git commit -m "feat(smoke): add PortfolioRun.smoke column + seed 4 canonical smoke Strategy rows (AAPL+SPY)"
```

---

### Task 2: SmokeConfig Python module

**Files:**

- Create: `backend/src/msai/services/smoke/__init__.py`
- Create: `backend/src/msai/services/smoke/config.py`
- Create: `backend/tests/unit/services/smoke/__init__.py`
- Create: `backend/tests/unit/services/smoke/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/services/smoke/test_config.py`:

```python
"""Unit tests for the canonical smoke configurations."""

from __future__ import annotations

from datetime import date

import pytest

from msai.services.smoke.config import SMOKE_FAST, SMOKE_NIGHTLY, SmokeConfig


def test_smoke_fast_two_equity_symbols_and_one_month_window() -> None:
    # Act / Assert
    assert isinstance(SMOKE_FAST, SmokeConfig)
    assert SMOKE_FAST.name == "fast"
    assert tuple(SMOKE_FAST.symbols) == ("AAPL", "SPY")
    assert SMOKE_FAST.start_date == date(2024, 12, 1)
    assert SMOKE_FAST.end_date == date(2024, 12, 31)
    assert SMOKE_FAST.benchmark_symbol == "SPY"
    assert SMOKE_FAST.strategy_names == (
        "__smoke__/smoke_market_order/AAPL",
        "__smoke__/smoke_market_order/SPY",
        "__smoke__/ema_cross/AAPL",
        "__smoke__/ema_cross/SPY",
    )


def test_smoke_nightly_two_equity_symbols_and_full_year_window() -> None:
    assert SMOKE_NIGHTLY.name == "nightly"
    assert tuple(SMOKE_NIGHTLY.symbols) == ("AAPL", "SPY")
    assert SMOKE_NIGHTLY.start_date == date(2024, 1, 1)
    assert SMOKE_NIGHTLY.end_date == date(2024, 12, 31)


def test_smoke_config_is_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        SMOKE_FAST.symbols = ()  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/services/smoke/test_config.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the config module**

Create `backend/src/msai/services/smoke/__init__.py`:

```python
"""Operational workflow smoke — canonical end-to-end smoke for the existing
ingest → portfolio-backtest → metrics → report pipeline on AAPL + SPY.

See docs/prds/ingest-backtest-smoke-test.md v1.3.
"""
```

Create `backend/src/msai/services/smoke/config.py`:

```python
"""Canonical smoke configurations — pinned in code for reproducibility.

PRD: docs/prds/ingest-backtest-smoke-test.md v1.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


SmokeConfigName = Literal["fast", "nightly"]


@dataclass(frozen=True)
class SmokeConfig:
    """Immutable canonical smoke configuration."""

    name: SmokeConfigName
    symbols: tuple[str, ...]
    strategy_names: tuple[str, ...]
    start_date: date
    end_date: date
    benchmark_symbol: str
    runtime_budget_warm_sec: int
    runtime_budget_cold_sec: int


_SMOKE_STRATEGY_NAMES = (
    "__smoke__/smoke_market_order/AAPL",
    "__smoke__/smoke_market_order/SPY",
    "__smoke__/ema_cross/AAPL",
    "__smoke__/ema_cross/SPY",
)


SMOKE_FAST = SmokeConfig(
    name="fast",
    symbols=("AAPL", "SPY"),
    strategy_names=_SMOKE_STRATEGY_NAMES,
    start_date=date(2024, 12, 1),
    end_date=date(2024, 12, 31),
    benchmark_symbol="SPY",
    runtime_budget_warm_sec=180,
    runtime_budget_cold_sec=600,
)


SMOKE_NIGHTLY = SmokeConfig(
    name="nightly",
    symbols=("AAPL", "SPY"),
    strategy_names=_SMOKE_STRATEGY_NAMES,
    start_date=date(2024, 1, 1),
    end_date=date(2024, 12, 31),
    benchmark_symbol="SPY",
    runtime_budget_warm_sec=600,
    runtime_budget_cold_sec=3600,
)


SMOKE_CONFIGS: dict[SmokeConfigName, SmokeConfig] = {
    "fast": SMOKE_FAST,
    "nightly": SMOKE_NIGHTLY,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/unit/services/smoke/test_config.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/smoke/ backend/tests/unit/services/smoke/
git commit -m "feat(smoke): hardcoded canonical SmokeConfig (fast / nightly, AAPL+SPY)"
```

---

### Task 3: Redis ingest-mutex helper

> NOTE: `fakeredis[lua]>=2.20` is already in `backend/pyproject.toml` dev extras (verified). Do NOT re-add or churn `uv.lock`.

**Files:**

- Create: `backend/src/msai/services/smoke/ingest_lock.py`
- Create: `backend/tests/unit/services/smoke/test_ingest_lock.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/services/smoke/test_ingest_lock.py` (identical to v1 plan's Task 3 step 1 — paste verbatim).

- [ ] **Step 2: Run test — FAIL on ImportError.**

- [ ] **Step 3: Implement `ingest_lock.py`** (identical to v1 plan's Task 3 step 3 — paste verbatim).

- [ ] **Step 4: Run test — 5 PASS.**

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/smoke/ingest_lock.py backend/tests/unit/services/smoke/test_ingest_lock.py
git commit -m "feat(smoke): Redis-backed ingest mutex per (symbol, year-month)"
```

---

### Task 4: Plumb `smoke` through PortfolioRunCreate + lifecycle

**Files:**

- Modify: `backend/src/msai/schemas/portfolio.py`
- Modify: `backend/src/msai/api/portfolio.py`
- Modify: `backend/src/msai/services/portfolio/lifecycle.py`
- Create: `backend/tests/integration/api/test_portfolio_runs_smoke.py`

- [ ] **Step 1: Failing test**

Create `backend/tests/integration/api/test_portfolio_runs_smoke.py`:

```python
"""smoke=True flag plumbing through portfolio-run API."""

import pytest


@pytest.mark.asyncio
async def test_create_portfolio_run_with_smoke_true_persists_smoke_column(
    api_client_authed, sample_portfolio_id
) -> None:
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{sample_portfolio_id}/runs",
        json={"start_date": "2024-12-01", "end_date": "2024-12-31", "smoke": True},
    )
    assert response.status_code == 201
    assert response.json()["smoke"] is True


@pytest.mark.asyncio
async def test_create_portfolio_run_smoke_defaults_false(
    api_client_authed, sample_portfolio_id
) -> None:
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{sample_portfolio_id}/runs",
        json={"start_date": "2024-12-01", "end_date": "2024-12-31"},
    )
    assert response.status_code == 201
    assert response.json()["smoke"] is False
```

> Test fixtures: `api_client_authed` and `sample_portfolio_id` are existing fixtures per the codebase conventions (per Codex P2-2). If a `sample_portfolio_id` fixture doesn't exist, add it to `backend/tests/integration/api/conftest.py` by reusing the existing portfolio factory pattern from `test_portfolios.py`.

- [ ] **Step 2: Run test — FAIL (KeyError 'smoke' in response).**

- [ ] **Step 3: Add `smoke: bool = False` to `PortfolioRunCreate` and `PortfolioRunResponse`** in `backend/src/msai/schemas/portfolio.py` (after `mode` field on Create; alongside other read fields on Response).

- [ ] **Step 4: Plumb `body.smoke` to the model**

In `backend/src/msai/services/portfolio/lifecycle.py`, find `PortfolioLifecycle.create_run`. **Note (iter-3 fix):** the existing parameter name is `data`, NOT `body` (`lifecycle.py:283`). Add `smoke=data.smoke` (the schema's `smoke` field, default False) to the `PortfolioRun(...)` instantiation. If `create_run` doesn't construct `PortfolioRun` directly (e.g. delegates to `PortfolioService.create_run`), thread the flag through the same call chain — the `PortfolioService.create_run` signature in `services/portfolio/orchestration.py` (and/or `services/portfolio/__init__.py`) must also be updated to accept and propagate `smoke`.

- [ ] **Step 5: Run test — 2 PASS.**

- [ ] **Step 6: Commit**

```bash
git add backend/src/msai/schemas/portfolio.py backend/src/msai/services/portfolio/lifecycle.py backend/tests/integration/api/test_portfolio_runs_smoke.py
git commit -m "feat(smoke): plumb smoke flag through PortfolioRunCreate + lifecycle"
```

---

### Task 5: Wire G5 metrics block into orchestration (reuse compute_series_metrics)

**Files:**

- Modify: `backend/src/msai/services/portfolio/orchestration.py` (around line 482 where `core = compute_series_metrics(...).as_dict()` already runs)
- Create: `backend/tests/unit/services/portfolio/test_orchestration_smoke_metrics.py`

> Background: `analytics_math.SeriesMetrics` already exposes total_return / sharpe / sortino / max_drawdown / alpha / beta / annualized_volatility / win_rate. The existing orchestration calls `compute_series_metrics(combined_returns, benchmark_returns=...)` and persists the dict into `run.metrics`. For smoke runs, we ADD `pnl`, `trade_count_by_strategy`, `trade_count_total`, `benchmark_symbol`, `smoke_config` to that same dict.
>
> **Iter-3 (Codex iter-2 P1) fix:** the existing `_run_candidate_backtest` discards `result.orders_df`. To get per-strategy trade counts WITHOUT changing the orchestration return-shape contract, this task ALSO extends `_run_candidate_backtest` to return `trade_count: int` (the row count of `result.orders_df`) alongside the existing `metrics` / `returns` / `timestamps` fields. The metrics enrichment then aggregates by `strategy_name` from the per-allocation successes list — no DataFrame plumbing.

- [ ] **Step 1: Failing test**

Create `backend/tests/unit/services/portfolio/test_orchestration_smoke_metrics.py`:

```python
"""Unit tests for the smoke-only G5 metrics enrichment in orchestration."""

from __future__ import annotations

import pytest

from msai.services.portfolio.orchestration import enrich_smoke_metrics


def test_enrich_smoke_metrics_adds_g5_keys() -> None:
    base = {
        "total_return": 0.05,
        "sharpe": 1.3,
        "sortino": 1.6,
        "max_drawdown": -0.08,
        "alpha": 0.02,
        "beta": 0.95,
        "win_rate": 0.55,
        "annualized_volatility": 0.18,
        "downside_risk": 0.12,
    }
    enriched = enrich_smoke_metrics(
        core_metrics=base,
        base_capital=100_000.0,
        trade_counts_by_strategy={
            "__smoke__/smoke_market_order/AAPL": 1,
            "__smoke__/smoke_market_order/SPY": 1,
            "__smoke__/ema_cross/AAPL": 2,
        },
        benchmark_symbol="SPY",
        smoke_config="fast",
    )
    assert enriched["pnl"] == pytest.approx(5_000.0, rel=1e-9)
    assert enriched["trade_count_by_strategy"] == {
        "__smoke__/smoke_market_order/AAPL": 1,
        "__smoke__/smoke_market_order/SPY": 1,
        "__smoke__/ema_cross/AAPL": 2,
    }
    assert enriched["trade_count_total"] == 4
    assert enriched["benchmark_symbol"] == "SPY"
    assert enriched["smoke_config"] == "fast"
    # Existing keys preserved
    assert enriched["sharpe"] == 1.3


def test_enrich_smoke_metrics_handles_empty_counts() -> None:
    base = {"total_return": 0.0}
    enriched = enrich_smoke_metrics(
        core_metrics=base,
        base_capital=100_000.0,
        trade_counts_by_strategy={},
        benchmark_symbol="SPY",
        smoke_config="fast",
    )
    assert enriched["trade_count_by_strategy"] == {}
    assert enriched["trade_count_total"] == 0
    assert enriched["pnl"] == pytest.approx(0.0, abs=1e-9)
```

- [ ] **Step 2: Run test — FAIL (function not exported).**

- [ ] **Step 3: Add the helper to `orchestration.py`**

In `backend/src/msai/services/portfolio/orchestration.py`, near the top-level helper functions (above `class PortfolioService`):

```python
def enrich_smoke_metrics(
    *,
    core_metrics: dict[str, float | None],
    base_capital: float,
    trade_counts_by_strategy: dict[str, int],
    benchmark_symbol: str,
    smoke_config: str,
) -> dict[str, object]:
    """Enrich the existing SeriesMetrics.as_dict() output with G5 smoke fields.

    Adds:
      * pnl (dollar value, derived from total_return and base_capital)
      * trade_count_by_strategy (caller pre-aggregates by strategy_name)
      * trade_count_total
      * benchmark_symbol
      * smoke_config ('fast' | 'nightly')

    PRD docs/prds/ingest-backtest-smoke-test.md v1.3 G5.

    Iter-3 fix: takes pre-aggregated counts dict (NOT orders_df) because
    orders_df is not in scope at the orchestration metrics-emit site
    (see `_run_candidate_backtest` — discards `result.orders_df`). The
    caller aggregates from `successes` list using each result's new
    `trade_count` + `strategy_name` fields.
    """
    enriched = dict(core_metrics)
    total_return = float(core_metrics.get("total_return") or 0.0)
    enriched["pnl"] = float(base_capital) * total_return
    by_strategy = {str(k): int(v) for k, v in (trade_counts_by_strategy or {}).items()}
    enriched["trade_count_by_strategy"] = by_strategy
    enriched["trade_count_total"] = int(sum(by_strategy.values()))
    enriched["benchmark_symbol"] = benchmark_symbol
    enriched["smoke_config"] = smoke_config
    return enriched
```

- [ ] **Step 4a: Extend `_run_candidate_backtest` to surface trade count**

In `backend/src/msai/services/portfolio/orchestration.py`, find `_run_candidate_backtest` (around line 1204). Where it currently returns the dict with `metrics` / `returns` / `timestamps`, add one more field:

```python
return {
    "candidate_id": str(allocation["candidate_id"]),
    "strategy_id": str(allocation["strategy_id"]),
    "strategy_name": str(allocation["strategy_name"]),
    "instruments": list(instrument_ids),
    "weight": float(allocation["weight"]),
    "metrics": dict(result.metrics),
    "returns": returns,
    "timestamps": timestamps,
    # Iter-3 addition: per-allocation order count for smoke G5 metrics.
    # ``result.orders_df`` is the Nautilus orders report — its row count is
    # the number of orders this strategy submitted in the backtest. For
    # ``smoke_market_order`` this is exactly 1 per instrument; for
    # ``ema_cross`` it varies. Non-smoke runs ignore this field.
    "trade_count": int(len(result.orders_df)) if result.orders_df is not None else 0,
}
```

- [ ] **Step 4b: Wire the helper into the smoke branch of `run_portfolio_backtest`**

In the same file, the actual persistence pattern (orchestration.py:482-491) is:

```python
core = compute_series_metrics(combined_returns).as_dict()
# ...alpha/beta computation...
metrics = {**core, "alpha": alpha, "beta": beta}
metrics["num_strategies"] = len(strategy_results)
# ...effective_leverage computed...
# ...later: run.metrics = metrics
```

**Iter-4 fix:** the enrichment must apply to `metrics` (the persisted dict), NOT `core` (which gets superseded). Add the smoke-only block IMMEDIATELY BEFORE `run.metrics = metrics` is set (search for the assignment; it's the next mutation of `metrics` after the alpha/beta merge). Add:

```python
if run.smoke:
    # G5 metrics block — additive to the existing metrics dict. smoke_config
    # carries the named window ('fast' vs 'nightly') so the UI metrics-block
    # and the alerts payload can render the right label without a separate
    # DB column.
    smoke_config_name = "fast" if (run.end_date - run.start_date).days <= 35 else "nightly"
    trade_counts: dict[str, int] = {}
    # `strategy_results` is the list of per-allocation result dicts
    # (orchestration.py:392). Each entry now carries `trade_count` per Step 4a.
    for strat_result in strategy_results:
        name = strat_result.get("strategy_name", "unknown")
        trade_counts[name] = trade_counts.get(name, 0) + int(strat_result.get("trade_count", 0))
    metrics = enrich_smoke_metrics(
        core_metrics=metrics,           # NB: enrich the persisted dict, not `core`
        base_capital=float(portfolio.base_capital),
        trade_counts_by_strategy=trade_counts,
        benchmark_symbol="SPY",
        smoke_config=smoke_config_name,
    )
```

Verify the wiring by reading orchestration.py:482-510 + the later `run.metrics = metrics` line. Place this block AFTER all existing `metrics[...] = ...` mutations (`num_strategies`, `effective_leverage`) and IMMEDIATELY BEFORE the `run.metrics = metrics` assignment so the enriched dict is what lands in the DB.

- [ ] **Step 5: Run test — 2 PASS.**

- [ ] **Step 6: Commit**

```bash
git add backend/src/msai/services/portfolio/orchestration.py backend/tests/unit/services/portfolio/test_orchestration_smoke_metrics.py
git commit -m "feat(smoke): enrich PortfolioRun.metrics with G5 block on smoke=True (reuse compute_series_metrics)"
```

---

### Task 6: Smoke runner — bootstrap canonical Portfolio + cold-ingest + fire run

**Files:**

- Create: `backend/src/msai/services/smoke/runner.py`
- Modify: `backend/tests/integration/test_smoke_runner.py` (extend)

> Cold-ingest is the missing piece Codex flagged (P1). The runner calls `data_ingestion.ingest_symbols("stocks", ["AAPL", "SPY"], start, end)` BEFORE creating the PortfolioRun. This uses the existing in-process helper (no arq round-trip; see `data_ingestion.py:381`). The ingest mutex from Task 3 wraps the call to prevent concurrent Databento fetches for the same `(symbol, year-month)`.

- [ ] **Step 1: Failing integration test**

Add to `backend/tests/integration/test_smoke_runner.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_bootstraps_portfolio_idempotently_and_returns_run_row(
    portfolio_db_session, api_client_authed
) -> None:
    from msai.services.smoke.runner import run_smoke

    run_1 = await run_smoke(db=portfolio_db_session, config_name="fast")
    run_2 = await run_smoke(db=portfolio_db_session, config_name="fast")
    # Same canonical Portfolio is reused
    assert run_1.portfolio_id == run_2.portfolio_id
    # Distinct PortfolioRun rows
    assert run_1.id != run_2.id
    assert run_1.smoke is True
    assert run_2.smoke is True
```

> Fixtures: `portfolio_db_session` and `api_client_authed` are the existing fixtures per Codex P2-2.

- [ ] **Step 2: Run test — FAIL (ImportError).**

- [ ] **Step 3: Implement the runner**

Create `backend/src/msai/services/smoke/runner.py`:

```python
"""Smoke runner — pre-ingests, bootstraps the canonical Portfolio idempotently,
then fires the existing portfolio_run pipeline.

Calls services directly (no HTTP-to-self). Used by both the CLI command
(``msai backtest smoke``) and the API endpoint (``POST /api/v1/portfolios/smoke/runs``).

Iter-3 fix (Codex iter-2 P0): ``PortfolioLifecycle`` is a static namespace
(``lifecycle.py:50`` — class docstring says "do not instantiate"). Use the
``@staticmethod`` API (``PortfolioLifecycle.create(...)``) for Portfolio
creation, and use module-level ``PortfolioService()`` (the orchestration one
from ``services.portfolio``, NOT the live one) for run creation, mirroring
the route at ``api/portfolio.py:306-326``. Also mimic the route's
rollback-on-enqueue-failure behavior.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.models.portfolio import Portfolio
from msai.models.portfolio_enums import (
    BacktestMode,
    PortfolioObjective,
)
# Iter-4 fix: AllocatorName is a Literal in schemas/portfolio.py:44, NOT an
# enum in models/portfolio_enums. Import from the schemas module.
from msai.schemas.portfolio import AllocatorName
from msai.models.portfolio_run import PortfolioRun
from msai.models.strategy import Strategy
from msai.schemas.portfolio import PortfolioCreate, PortfolioRunCreate
from msai.services.data_ingestion import ingest_symbols
from msai.services.portfolio import PortfolioService
from msai.services.portfolio.lifecycle import PortfolioLifecycle
from msai.services.smoke.config import SMOKE_CONFIGS, SmokeConfigName
from msai.services.smoke.ingest_lock import (
    acquire_ingest_lock,
    release_ingest_lock,
)

logger = logging.getLogger(__name__)

SMOKE_PORTFOLIO_NAME = "__msai_smoke__"

# Module-level service singleton — mirrors api/portfolio.py:42 (_service = PortfolioService()).
_service = PortfolioService()


async def _get_or_create_canonical_portfolio(
    db: AsyncSession, *, user_id: UUID | None
) -> Portfolio:
    """Idempotent bootstrap. Looks up by sentinel name; creates if missing.

    Race note: there is no DB-level uniqueness on Portfolio.name. Two parallel
    smoke invocations that both miss could both attempt to INSERT. The
    Postgres transaction isolation will let one through; the other's commit
    will succeed too (no constraint). The downstream runner code is the
    second-line defense — even if two canonical portfolios exist briefly,
    each run still goes through the existing create_run code path. Document
    as a known low-priority gap; revisit if smoke-volume increases.
    """
    existing = (
        await db.execute(select(Portfolio).where(Portfolio.name == SMOKE_PORTFOLIO_NAME))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Look up the four pre-seeded Strategy rows (seeded by the smoke migration).
    smoke_strategy_names = [
        "__smoke__/smoke_market_order/AAPL",
        "__smoke__/smoke_market_order/SPY",
        "__smoke__/ema_cross/AAPL",
        "__smoke__/ema_cross/SPY",
    ]
    rows = (
        await db.execute(
            select(Strategy).where(Strategy.name.in_(smoke_strategy_names))
        )
    ).scalars().all()
    if len(rows) != 4:
        raise RuntimeError(
            f"Expected 4 canonical smoke Strategy rows; found {len(rows)}. "
            "Did the smoke Alembic migration run?"
        )
    strategy_ids = [r.id for r in rows]

    create = PortfolioCreate(
        name=SMOKE_PORTFOLIO_NAME,
        # Iter-4 fix: actual enum is MAXIMIZE_SHARPE (RISK_ADJUSTED_RETURN doesn't exist).
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        base_capital=100_000.0,
        requested_leverage=1.0,
        default_mode=BacktestMode.QUICK,
        allocator_name=cast(AllocatorName, "equal_weight"),
        strategy_ids=strategy_ids,
    )
    # Iter-3 fix: PortfolioLifecycle.create is a @staticmethod (lifecycle.py:64).
    portfolio = await PortfolioLifecycle.create(db, create, user_id=user_id)
    await db.commit()
    return portfolio


async def _ensure_ingested(symbols: tuple[str, ...], start: str, end: str) -> None:
    """Cold-ingest pre-flight via the existing in-process ``ingest_symbols`` helper.

    Wrapped per-symbol in the Redis mutex to prevent concurrent Databento fetches
    when CLI + UI + scheduler fire near each other.
    """
    from datetime import datetime
    redis = await get_redis_pool()
    window_start = datetime.fromisoformat(start).date()
    tokens: list[tuple[str, str]] = []
    try:
        for symbol in symbols:
            token = await acquire_ingest_lock(
                redis,
                symbol=symbol,
                window_start=window_start,
                ttl_seconds=900,
                wait_timeout_seconds=600,
            )
            assert token is not None  # acquire raises if it cannot get the lock
            tokens.append((symbol, token))
        # Single in-process call; Databento ingest only fetches what's missing.
        await ingest_symbols(
            "stocks",
            list(symbols),
            start,
            end,
            provider="databento",
            dataset="EQUS.MINI",
            schema="ohlcv-1m",
        )
    finally:
        for symbol, token in tokens:
            try:
                await release_ingest_lock(
                    redis, symbol=symbol, window_start=window_start, token=token
                )
            except Exception:  # noqa: BLE001
                logger.warning("smoke_ingest_lock_release_failed", extra={"symbol": symbol})


async def run_smoke(
    *,
    db: AsyncSession,
    config_name: SmokeConfigName = "fast",
    user_id: UUID | None = None,
) -> PortfolioRun:
    """Fire the canonical smoke run. Returns the persisted PortfolioRun row.

    Steps:
      1. Pre-ingest AAPL+SPY for the configured window (idempotent — Parquet
         already on disk → no-op).
      2. Bootstrap (or look up) the canonical __msai_smoke__ Portfolio.
      3. Submit a PortfolioRun with smoke=True via the existing lifecycle.
      4. Enqueue the existing arq job that runs the backtest + persists metrics.
    """
    config = SMOKE_CONFIGS[config_name]
    effective_user_id = user_id

    # 1. Cold-ingest pre-flight (mutex-guarded). No-op if Parquet is already present.
    await _ensure_ingested(
        config.symbols,
        config.start_date.isoformat(),
        config.end_date.isoformat(),
    )

    # 2. Idempotent canonical Portfolio bootstrap.
    portfolio = await _get_or_create_canonical_portfolio(db, user_id=effective_user_id)

    # 3. Create the PortfolioRun and enqueue the existing arq job, mirroring
    # api/portfolio.py:306-326 (enqueue BEFORE commit; rollback row on enqueue failure).
    body = PortfolioRunCreate(
        start_date=config.start_date,
        end_date=config.end_date,
        smoke=True,
    )
    run = await _service.create_run(db, portfolio.id, body, user_id=effective_user_id)
    try:
        pool = await get_redis_pool()
        await enqueue_portfolio_run(pool, str(run.id), str(portfolio.id))
    except Exception as exc:
        await db.rollback()
        logger.error("smoke_portfolio_run_enqueue_failed", extra={"error": str(exc)})
        raise
    await db.commit()
    await db.refresh(run)
    return run
```

The exact private-API call (`lifecycle._service.create_run`) is the same path the existing `create_portfolio_run` route uses (see `api/portfolio.py:266-345`). If the lifecycle re-exposes `create_run` as a public method, prefer that. The point is to call services in-process, not HTTP-to-self.

- [ ] **Step 4: Run test — 1 PASS.**

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/services/smoke/runner.py backend/tests/integration/test_smoke_runner.py
git commit -m "feat(smoke): runner — idempotent bootstrap + cold-ingest mutex + portfolio run dispatch"
```

---

### Task 7: API endpoint `POST /api/v1/portfolios/smoke/runs` (route-ordered)

**Files:**

- Modify: `backend/src/msai/api/portfolio.py` (add NEW route at file's static-route region, BEFORE `/{portfolio_id}/runs`)

> Codex P1 finding: registering `/smoke/runs` AFTER `/{portfolio_id}/runs` causes FastAPI to match `smoke` as the UUID path-parameter and return 422. The new route MUST be declared before the dynamic one.

- [ ] **Step 1: Failing test**

Add to `backend/tests/integration/api/test_portfolio_runs_smoke.py`:

```python
@pytest.mark.asyncio
async def test_smoke_endpoint_creates_run_and_returns_201(api_client_authed) -> None:
    response = await api_client_authed.post(
        "/api/v1/portfolios/smoke/runs?config=fast",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["smoke"] is True
    assert body["status"] in {"pending", "running"}
```

- [ ] **Step 2: Run test — FAIL.**

- [ ] **Step 3: Register the route BEFORE `/{portfolio_id}/runs`**

In `backend/src/msai/api/portfolio.py`, find the existing `@router.post("/{portfolio_id}/runs", ...)` block. Insert IMMEDIATELY ABOVE it:

```python
# ---------------------------------------------------------------------------
# POST /api/v1/portfolios/smoke/runs -- canonical smoke run (no portfolio_id)
#
# Registered BEFORE /{portfolio_id}/runs so FastAPI doesn't try to bind
# 'smoke' as a UUID — Codex plan-review P1 finding.
# ---------------------------------------------------------------------------


@router.post(
    "/smoke/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioRunResponse,
)
async def start_smoke_run(
    config: Literal["fast", "nightly"] = "fast",
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Authenticated shortcut for the smoke. Bootstraps the canonical Portfolio
    if absent, pre-ingests AAPL+SPY for the configured window, then fires a
    PortfolioRun via the existing lifecycle. Returns the run row.

    PRD docs/prds/ingest-backtest-smoke-test.md v1.3 US-002 / US-001.
    """
    from msai.services.smoke.runner import run_smoke

    user_id = await resolve_user_id(db, claims)
    run = await run_smoke(db=db, config_name=config, user_id=user_id)
    return PortfolioRunResponse.model_validate(run)
```

Make sure `Literal` is imported from `typing` at the top of the file.

- [ ] **Step 4: Run test — PASS.**

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/api/portfolio.py backend/tests/integration/api/test_portfolio_runs_smoke.py
git commit -m "feat(smoke): POST /api/v1/portfolios/smoke/runs shortcut (route-ordered before /{id}/runs)"
```

---

### Task 8: CLI `msai backtest smoke` command (using `_api_call`)

**Files:**

- Modify: `backend/src/msai/cli.py`
- Modify: `backend/tests/unit/test_cli.py`

> Codex P0 finding: the v1 plan invented `authenticated_async_client` (doesn't exist). The CLI uses `_api_call(method, path, ...)` (cli.py:147). The CLI polls the run via `GET /api/v1/portfolios/runs/{run_id}` (portfolio.py:135 — `get_portfolio_run`).

- [ ] **Step 1: Failing test**

In `backend/tests/unit/test_cli.py`, add (using existing CLI test conventions — runner / mocker):

```python
def test_cli_backtest_smoke_fast_default(cli_runner, mocker):
    fake_post = mocker.patch("msai.cli._api_call")
    # First call: POST /api/v1/portfolios/smoke/runs?config=fast
    # Then polling GETs of /api/v1/portfolios/runs/{run_id} until status=completed
    smoke_run_id = "00000000-0000-0000-0000-000000000111"
    completed_body = {
        "id": smoke_run_id,
        "status": "completed",
        "smoke": True,
        "metrics": {
            "total_return": 0.05, "pnl": 5000.0, "sharpe": 1.3, "sortino": 1.6,
            "alpha": 0.02, "beta": 0.95, "max_drawdown": -0.08,
            "trade_count_by_strategy": {
                "__smoke__/smoke_market_order/AAPL": 1,
                "__smoke__/smoke_market_order/SPY": 1,
            },
            "trade_count_total": 2,
            "benchmark_symbol": "SPY",
            "smoke_config": "fast",
        },
        "report_path": "/app/data/reports/x.html",
        "error_message": None,
    }
    create_resp = mocker.Mock(status_code=201, json=lambda: completed_body)
    get_resp = mocker.Mock(status_code=200, json=lambda: completed_body)
    fake_post.side_effect = [create_resp, get_resp]

    result = cli_runner.invoke(app, ["backtest", "smoke"])
    assert result.exit_code == 0
    assert "PASS" in result.stdout
    assert "Sharpe" in result.stdout
    assert "AAPL" in result.stdout
```

(Add 3 more tests for `--config nightly`, `--json`, and structural FAIL exit code, modeled on this.)

- [ ] **Step 2: Run tests — FAIL.**

- [ ] **Step 3: Implement the CLI**

In `backend/src/msai/cli.py`, after the existing `@backtest_app.command("trades")` definition, add:

```python
@backtest_app.command("smoke")
def smoke_cmd(
    config: str = typer.Option(
        "fast", "--config",
        help="Smoke config: 'fast' (1 month) or 'nightly' (2024 full year).",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the metrics block as a single JSON document on stdout.",
    ),
) -> None:
    """Run the canonical multi-strategy portfolio smoke against AAPL+SPY.

    PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
    """
    import json as _json
    import time

    if config not in {"fast", "nightly"}:
        typer.echo(f"unknown --config {config}; expected 'fast' or 'nightly'", err=True)
        raise typer.Exit(code=2)

    # Iter-3 fix: the smoke endpoint synchronously runs cold ingest before
    # returning, which can exceed the _api_call default 30s timeout for
    # cold-fast (≤10 min budget) and cold-nightly (≤60 min budget). Override
    # the timeout to the smoke-nightly cold budget + slack.
    create_resp = _api_call(
        "POST", f"/api/v1/portfolios/smoke/runs?config={config}",
        timeout=3700.0,
    )
    run = create_resp.json()
    run_id = run["id"]

    # Poll until terminal — same pattern as msai backtest run.
    # Iter-3 fix: PortfolioRunStatus.CANCELED = "canceled" (single L), not "cancelled".
    terminal = {"completed", "failed", "canceled"}
    timeout_at = time.monotonic() + 3700  # cold-nightly budget + slack
    while True:
        if time.monotonic() > timeout_at:
            typer.echo(f"FAIL @ poll: timed out waiting for run {run_id}", err=True)
            raise typer.Exit(code=1)
        status_resp = _api_call("GET", f"/api/v1/portfolios/runs/{run_id}")
        run = status_resp.json()
        if run["status"] in terminal:
            break
        time.sleep(2)

    # Iter-4 fix: status="completed" alone is NOT sufficient for PASS. The
    # PRD's structural assertions must also hold:
    #   - metrics block exists and contains the required G5 keys
    #   - trade_count_total >= deterministic floor of 2 (one smoke_market_order per equity)
    #   - report_path is non-empty
    # Any of these failing is a STRUCTURAL FAIL even when the run's lifecycle
    # status reads "completed".
    G5_REQUIRED = {
        "total_return", "pnl", "sharpe", "sortino", "alpha", "beta",
        "max_drawdown", "trade_count_by_strategy", "trade_count_total",
        "benchmark_symbol", "smoke_config",
    }
    DETERMINISTIC_TRADE_FLOOR = 2

    metrics = run.get("metrics") or {}
    structural_problems: list[str] = []
    if run.get("status") != "completed":
        structural_problems.append(
            f"status={run.get('status')!r}: {run.get('error_message') or 'unknown'}"
        )
    if run.get("status") == "completed":
        missing_keys = G5_REQUIRED - set(metrics.keys())
        if missing_keys:
            structural_problems.append(f"missing G5 keys: {sorted(missing_keys)}")
        if int(metrics.get("trade_count_total") or 0) < DETERMINISTIC_TRADE_FLOOR:
            structural_problems.append(
                f"trade_count_total={metrics.get('trade_count_total')} < floor {DETERMINISTIC_TRADE_FLOOR} — order/fill plumbing broken"
            )
        if not run.get("report_path"):
            structural_problems.append("report_path empty — report generation failed")

    if json_output:
        # Add a structural-problems list to the JSON so downstream pipes can grep.
        out = {**run, "structural_problems": structural_problems}
        typer.echo(_json.dumps(out))
        if structural_problems:
            raise typer.Exit(code=1)
        return

    if not structural_problems:
        _print_smoke_metrics_table(metrics, run.get("report_path") or "")
        typer.echo(f"\nPASS — Portfolio run {run['id']}")
    else:
        for problem in structural_problems:
            typer.echo(f"FAIL: {problem}", err=True)
        raise typer.Exit(code=1)


def _print_smoke_metrics_table(metrics: dict, report_path: str) -> None:
    """Render the G5 metrics block as a compact human-readable table."""
    def row(label: str, value: str) -> None:
        typer.echo(f"  {label:<28} {value:>14}")

    typer.echo("Smoke metrics:")
    row("Total return", f"{(metrics.get('total_return') or 0):.2%}")
    row("P&L (USD)", f"${(metrics.get('pnl') or 0):,.0f}")
    row("Sharpe", f"{(metrics.get('sharpe') or 0):.2f}")
    row("Sortino", f"{(metrics.get('sortino') or 0):.2f}")
    row("Alpha vs SPY", f"{(metrics.get('alpha') or 0):.2%}")
    row("Beta vs SPY", f"{(metrics.get('beta') or 0):.2f}")
    row("Max drawdown", f"{(metrics.get('max_drawdown') or 0):.2%}")
    row("Trades total", str(metrics.get("trade_count_total") or 0))
    for strat, n in (metrics.get("trade_count_by_strategy") or {}).items():
        row(f"  · {strat.removeprefix('__smoke__/')}", str(n))
    row("Benchmark", str(metrics.get("benchmark_symbol") or "—"))
    row("Smoke config", str(metrics.get("smoke_config") or "—"))
    if report_path:
        typer.echo(f"\nReport: {report_path}")
```

Also add `msai system smoke-alert <result-file>` under the existing `system_app`:

```python
@system_app.command("smoke-alert")
def smoke_alert_cmd(
    result_file: str = typer.Argument(..., help="Path to JSON file with smoke run result."),
) -> None:
    """Dispatch a smoke result as an alert via AlertingService.

    Used by .github/workflows/smoke.yml — reads the JSON the nightly run wrote,
    constructs a single alert entry, and persists via the existing alert log.

    Iter-5 fix: `status="completed"` is NOT sufficient to call it a PASS — the
    structural assertions (G5 key presence, trade-count floor >=2, non-empty
    report_path) must also hold. The CLI's smoke command writes a
    `structural_problems: [...]` list to the JSON when those assertions fail
    even on a 'completed' run; this command checks that list.
    """
    import json as _json
    from pathlib import Path
    from msai.services.alerting import AlertingService

    payload = _json.loads(Path(result_file).read_text())
    structural_problems = payload.get("structural_problems") or []
    is_pass = (
        payload.get("status") == "completed"
        and not structural_problems
    )
    level = "info" if is_pass else "error"
    if is_pass:
        title = f"Smoke PASS — {payload.get('metrics', {}).get('smoke_config', 'fast')}"
    elif structural_problems:
        title = f"Smoke FAIL (structural) — {len(structural_problems)} problem(s)"
    else:
        title = "Smoke FAIL"
    body: dict = {"metrics": payload.get("metrics") or {}, "status": payload.get("status")}
    if structural_problems:
        body["structural_problems"] = structural_problems
    if payload.get("error_message"):
        body["error_message"] = payload["error_message"]
    message = _json.dumps(body, sort_keys=True)
    AlertingService().send_alert(level=level, title=title, message=message)
    typer.echo(f"alert dispatched: {title}")
```

- [ ] **Step 4: Run tests — PASS.**

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/cli.py backend/tests/unit/test_cli.py
git commit -m "feat(smoke): CLI commands — backtest smoke + system smoke-alert (using _api_call + AlertingService)"
```

---

### Task 9: History endpoint smoke filters

> Iter-3 fix (Codex iter-2 P2): the actual response schema is `BacktestListItem` (`backend/src/msai/schemas/backtest.py:91`), NOT `BacktestHistoryRow`. Also the existing `include_smoke` query param ONLY filters single-strategy `Backtest.smoke`; the PortfolioRun side needs new filter logic.

**Files:**

- Modify: `backend/src/msai/api/backtests.py` — extend the portfolio branch of `/history` (around line 569-628) with `smoke_only: bool = Query(False)` and `include_smoke: bool = Query(False)` filters against `PortfolioRun.smoke`. The existing `include_smoke` on the Backtest branch stays unchanged.
- Modify: `backend/src/msai/schemas/backtest.py:91` — add `smoke: bool = False` field to `BacktestListItem`; populate from `pr.smoke` on the portfolio branch and from `bt.smoke` on the single branch.

- [ ] **Step 1: Failing test** — write the same test text as v1 plan Task 9 step 1 but reference `BacktestListItem` not `BacktestHistoryRow`.
- [ ] **Step 2: Run — FAIL** (smoke field missing on the portfolio rows).
- [ ] **Step 3: Implement** — within the existing `if type in ("portfolio", "all"):` branch in `backtests.py`:

```python
# Apply the same filter to BOTH the row-fetch and count queries — otherwise
# pagination totals lie for type=portfolio and type=all. Codex iter-3 P2.
if smoke_only:
    portfolio_query = portfolio_query.where(PortfolioRun.smoke.is_(True))
    portfolio_count_query = portfolio_count_query.where(PortfolioRun.smoke.is_(True))
elif not include_smoke:
    portfolio_query = portfolio_query.where(PortfolioRun.smoke.is_(False))
    portfolio_count_query = portfolio_count_query.where(PortfolioRun.smoke.is_(False))
```

ALSO apply the same `where(...)` clause to the separate `type="all"` `portfolio_total` query (around `api/backtests.py:644`). All three queries — row fetch, count, all-total — must honor the same smoke filter for pagination correctness.

**Iter-4 fix:** when `smoke_only=true` AND `type in ("single", "all")`, the existing single-strategy `Backtest.smoke` filter ALSO needs to gate to smoke-only. The existing `include_smoke` query parameter already does this in the inverse direction (`include_smoke=false` defaults exclude Backtest.smoke=True rows); add the smoke_only path so:

```python
if smoke_only:
    single_query = single_query.where(Backtest.smoke.is_(True))
    single_count_query = single_count_query.where(Backtest.smoke.is_(True))
```

Apply identically to the `single_total` query if separate from `single_count_query`. Without this, `smoke_only=true&type=all` leaks ordinary single-strategy rows.

Populate the `smoke` field when building the per-row `BacktestListItem(type="portfolio", smoke=pr.smoke, ...)` AND on the single-strategy branch (`smoke=bt.smoke`).

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(smoke): /backtests/history smoke filters on PortfolioRun branch`.

---

### Task 10: UI — `<RunSmokeButton />` on `/backtests`

**Files:**

- Create: `frontend/src/components/backtests/run-smoke-button.tsx`
- Create: `frontend/tests/e2e/specs/run-smoke-button.spec.ts`
- Modify: `frontend/src/app/backtests/page.tsx`

> Codex P1 finding: use `apiPost` from `frontend/src/lib/api.ts`, not the fictional `@/lib/api-client`. The endpoint is `/api/v1/portfolios/smoke/runs?config=fast`.

- [ ] **Step 1: Playwright spec (failing)**

Create `frontend/tests/e2e/specs/run-smoke-button.spec.ts`:

```typescript
import { test, expect } from "../fixtures/auth";

test("operator clicks Run smoke and sees the run appear @smoke", async ({
  authedPage,
}) => {
  await authedPage.goto("/backtests");
  await authedPage.getByTestId("run-smoke-button").click();
  await expect(
    authedPage.getByRole("status").filter({ hasText: /smoke run started/i }),
  ).toBeVisible({ timeout: 3000 });
});
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement the component**

Create `frontend/src/components/backtests/run-smoke-button.tsx`:

```tsx
"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { apiPost } from "@/lib/api";
// Iter-5 fix: the endpoint is authenticated via get_current_user. Browser
// SSO requests without the bearer token 401 in prod (NEXT_PUBLIC_MSAI_API_KEY
// is dev-only). Use the project's existing auth hook to obtain the token and
// pass it on the call — matches the existing mutation pattern.
import { useAuth } from "@/lib/auth-context";

export function RunSmokeButton() {
  const [pending, setPending] = React.useState(false);
  const { getToken } = useAuth();

  async function handleClick() {
    setPending(true);
    try {
      const token = await getToken();
      const data = await apiPost<{ id: string }>(
        "/api/v1/portfolios/smoke/runs?config=fast",
        null,
        { token },
      );
      toast.success("Smoke run started", { description: `Run id: ${data.id}` });
    } catch (err) {
      toast.error("Failed to start smoke", {
        description: err instanceof Error ? err.message : "Unknown error",
      });
    } finally {
      setPending(false);
    }
  }

  return (
    <Button
      data-testid="run-smoke-button"
      onClick={handleClick}
      disabled={pending}
      variant="secondary"
    >
      {pending ? (
        <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:hidden" />
      ) : null}
      Run smoke
    </Button>
  );
}
```

The exact import path for `useAuth` (`@/lib/auth-context` vs `@/hooks/use-auth` vs other) AND the exact `apiPost` third-argument options shape (`{ token }` vs `{ headers: { Authorization } }` vs other) MUST match the project's existing pattern. The implementer reads `frontend/src/lib/api.ts` + an existing authenticated mutation component (e.g., a `/backtests/new`-form submit) and adapts both imports + the `apiPost` call to mirror that pattern verbatim.

- [ ] **Step 4: Mount on `/backtests`**

In `frontend/src/app/backtests/page.tsx`, render `<RunSmokeButton />` in the page header alongside any existing actions.

- [ ] **Step 5: Run — PASS.**

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/backtests/run-smoke-button.tsx frontend/src/app/backtests/page.tsx frontend/tests/e2e/specs/run-smoke-button.spec.ts
git commit -m "feat(smoke): UI Run smoke button (apiPost to /smoke/runs)"
```

---

### Task 11: UI — `<MetricsBlock />` on portfolio-run details

(Same as v1 plan Task 11, but pull `metrics.smoke_config` and render it as a sub-label. Drop ES references.)

---

### Task 12: `.github/workflows/smoke.yml` (nightly + dispatch via SSH)

**Files:**

- Create: `.github/workflows/smoke.yml`

> Codex P1 finding: alerts API is read-only GET. Dispatch via VM-side CLI (`msai system smoke-alert`) directly against the AlertingService log, not via HTTP POST to `/api/v1/alerts`.
>
> The previous v1 design also had a SSH-output-to-local-file bug (output redirected on runner, then asked VM to read `/tmp/smoke-result.json` which was never written there). The corrected pattern: write the result file ON the VM via `tee`, then invoke `msai system smoke-alert` on the VM in the same SSH session.

```yaml
name: Smoke

on:
  schedule:
    - cron: "0 5 * * *" # literal — vars.* cannot interpolate inside schedule:
  workflow_dispatch:
    inputs:
      config:
        required: false
        type: choice
        default: "fast"
        options: ["fast", "nightly"]

permissions:
  id-token: write
  contents: read

jobs:
  smoke:
    runs-on: ubuntu-latest
    timeout-minutes: 75
    steps:
      - uses: actions/checkout@v4

      - uses: azure/login@v2
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}

      - name: Open transient NSG SSH rule
        run: |
          RUNNER_IP=$(curl -fsSL https://api.ipify.org)
          az network nsg rule create \
            --resource-group ${{ vars.RESOURCE_GROUP }} \
            --nsg-name ${{ vars.NSG_NAME }} \
            --name smoke-ssh-${{ github.run_id }} \
            --priority 200 \
            --source-address-prefixes "$RUNNER_IP/32" \
            --destination-port-ranges 22 \
            --access Allow --protocol Tcp

      - uses: webfactory/ssh-agent@v0.9.0
        with:
          ssh-private-key: ${{ secrets.VM_SSH_PRIVATE_KEY }}

      - name: Trust VM host key
        run: |
          mkdir -p ~/.ssh
          echo "${{ vars.VM_SSH_KNOWN_HOSTS }}" >> ~/.ssh/known_hosts

      - name: Run smoke on VM and dispatch alert
        # Iter-5 fix #2 + #3:
        #   - `set -e` short-circuits when the smoke CLI exits non-zero, which
        #     skipped the alert dispatch. Capture exit, ALWAYS dispatch alert,
        #     then exit with captured code.
        #   - The JSON must live INSIDE the backend container so smoke-alert
        #     can read it. tee on the VM host wrote it outside any container.
        env:
          CONFIG: ${{ github.event_name == 'schedule' && 'nightly' || (github.event.inputs.config || 'fast') }}
        run: |
          # Iter-6 fix #1: pass CONFIG explicitly across the SSH boundary.
          # The runner-side heredoc-`'EOF'` prevents premature expansion, and
          # SSH doesn't forward env by default, so we set CONFIG on the
          # remote shell invocation. The remote bash then reads it via env.
          ssh ${{ vars.VM_USER }}@${{ vars.VM_PUBLIC_IP }} "CONFIG='${CONFIG}' bash -s" <<'EOF'
            set -uo pipefail  # NOTE: no -e — we want to capture nonzero exits
            cd /opt/msai
            CID=$(docker compose ps -q backend)
            test -n "$CID"

            RESULT_PATH=/tmp/smoke-result.json

            # Iter-6 fix #2: pre-seed the result file with a failure payload.
            # If the smoke CLI exits before emitting JSON (auth fail, backend
            # down, config typo), smoke-alert still has a valid file to read
            # and can dispatch a structured failure alert.
            docker exec -i "$CID" bash -c "cat > $RESULT_PATH" <<JSON
            {"status":"failed","structural_problems":["pre-CLI-launch failure (smoke CLI did not emit JSON)"],"metrics":null}
            JSON

            # Run smoke inside the backend container; the CLI overwrites the
            # pre-seeded file with its real --json payload on success or on
            # any failure that reaches the JSON-emit stage.
            docker exec -i "$CID" bash -c "msai backtest smoke --config '$CONFIG' --json > $RESULT_PATH 2>&1" || true
            # Capture exit status of the smoke CLI via the file's structural_problems list.
            # The CLI exits non-zero when structural_problems is non-empty (Task 8 iter-4).

            # ALWAYS dispatch the alert from inside the same container, where
            # the JSON file is reachable.
            docker exec -i "$CID" msai system smoke-alert "$RESULT_PATH" \
              || echo "::warning::smoke-alert dispatch failed; check VM logs"

            # Workflow exit = smoke result. Re-derive from the JSON the CLI
            # wrote — this avoids relying on docker-exec's RC propagation
            # (which Docker swallows for non-TTY exec sometimes per #4717).
            STATUS=$(docker exec -i "$CID" python -c "import json;print(json.load(open('$RESULT_PATH')).get('status'))")
            PROBLEMS=$(docker exec -i "$CID" python -c "import json;d=json.load(open('$RESULT_PATH'));print(len(d.get('structural_problems') or []))")
            test "$STATUS" = "completed" && test "$PROBLEMS" = "0"
          EOF

      - name: Close NSG rule
        if: always()
        run: |
          az network nsg rule delete \
            --resource-group ${{ vars.RESOURCE_GROUP }} \
            --nsg-name ${{ vars.NSG_NAME }} \
            --name smoke-ssh-${{ github.run_id }} \
            || true
```

Run `actionlint .github/workflows/smoke.yml` and fix any errors.

- [ ] **Commit:** `feat(smoke): nightly + workflow_dispatch via SSH-to-VM (alert via msai system smoke-alert)`

---

### Task 13: `deploy.yml` opt-in `run_smoke` preflight

(Same as v1 plan Task 13 — add `run_smoke: type=boolean default=false` input + a preflight job. Update `docs/how_to_deploy.md` with the honest "env + data preflight, NOT candidate-code validation" framing.)

---

### Task 14: E2E use cases

(Same as v1 plan Task 14, but symbols are AAPL+SPY everywhere; deterministic-trades floor is ≥ 2.)

---

## Dispatch Plan (Phase 4.0)

Same shape as v1's dispatch plan, with these adjustments:

- T5 now WRITES `backend/src/msai/services/portfolio/orchestration.py` (was `workers/portfolio_job.py`); same caller set.
- T6 (runner) WRITES `backend/src/msai/services/smoke/runner.py` — depends on T2, T4 (NOT T5; runner doesn't need orchestration changes to land first).
- T7 (smoke endpoint) depends on T6; serializes against T4 because both write `api/portfolio.py`.
- T8 depends on T7 (CLI needs the endpoint live).
- Concurrency: T2, T3 in parallel (disjoint writes); T9 + T10 + T11 + T12 + T13 can run in parallel after their deps clear (disjoint files except for the `api/portfolio.py` chain).

| Task ID | Depends on | Writes (concrete file paths)                                                                                                                              |
| ------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1      | —          | `backend/alembic/versions/*_add_portfolio_run_smoke_and_seed_strategies.py`, `backend/src/msai/models/portfolio_run.py`                                   |
| T2      | —          | `backend/src/msai/services/smoke/__init__.py`, `backend/src/msai/services/smoke/config.py`, `backend/tests/unit/services/smoke/{__init__,test_config}.py` |
| T3      | —          | `backend/src/msai/services/smoke/ingest_lock.py`, `backend/tests/unit/services/smoke/test_ingest_lock.py`                                                 |
| T4      | T1         | `backend/src/msai/schemas/portfolio.py`, `backend/src/msai/api/portfolio.py`, `backend/src/msai/services/portfolio/lifecycle.py`, integration test        |
| T5      | T1         | `backend/src/msai/services/portfolio/orchestration.py`, `backend/tests/unit/services/portfolio/test_orchestration_smoke_metrics.py`                       |
| T6      | T2, T3, T4 | `backend/src/msai/services/smoke/runner.py`, `backend/tests/integration/test_smoke_runner.py`                                                             |
| T7      | T4, T6     | `backend/src/msai/api/portfolio.py`, integration test                                                                                                     |
| T8      | T7         | `backend/src/msai/cli.py`, `backend/tests/unit/test_cli.py`                                                                                               |
| T9      | T1         | `backend/src/msai/api/backtests.py`, `backend/src/msai/schemas/backtest.py`, integration test                                                             |
| T10     | T7         | `frontend/src/components/backtests/run-smoke-button.tsx`, `frontend/src/app/backtests/page.tsx`, Playwright spec                                          |
| T11     | T5         | `frontend/src/components/portfolio/metrics-block.tsx`, `frontend/src/app/portfolio/runs/[runId]/page.tsx`, Playwright spec                                |
| T12     | T8         | `.github/workflows/smoke.yml`                                                                                                                             |
| T13     | T8         | `.github/workflows/deploy.yml`, `docs/how_to_deploy.md`                                                                                                   |
| T14     | —          | `tests/e2e/use-cases/backtests/{smoke-cli-fast,smoke-ui,smoke-nightly-schedule,smoke-preflight-gate}.md`                                                  |

---

## Self-Review Pass (v2)

Spec coverage:

- ✓ PRD US-001 (CLI): Tasks 2, 6, 7, 8.
- ✓ PRD US-002 (UI button + metrics block): Tasks 10, 11.
- ✓ PRD US-003 (nightly schedule + alert): Task 12.
- ✓ PRD US-004 (opt-in preflight): Task 13.
- ✓ G5 metrics block: Task 5 (enrich in orchestration) + Task 11 (UI render) + Task 8 (CLI render).
- ✓ Deterministic-trades floor of 2: 2 pre-seeded smoke_market_order Strategy rows (Task 1) → orders_df.groupby('strategy_id') in Task 5 → assertion in Task 6 + Task 8 (FAIL exit on `trade_count_total < 2`).
- ✓ Smoke flag persistence: Tasks 1, 4, 9.
- ✓ Ingest mutex: Task 3, used in Task 6's runner.
- ✓ Cold ingest: Task 6's runner pre-ingests via existing `ingest_symbols`.
- ✓ Route ordering: Task 7 places `/smoke/runs` before `/{portfolio_id}/runs`.
- ✓ AlertingService write path: Task 8's `msai system smoke-alert` + Task 12's VM-side invocation.
- ✓ E2E use cases: Task 14.

Codex finding closure check:

- ✓ P0-1 (PortfolioCreate shape): Task 6's runner uses `strategy_ids` + `objective` + `base_capital`.
- ✓ P0-2 (fictional helpers): Task 8 uses `_api_call`; Task 7's endpoint calls services directly.
- ✓ P1-1 (worker var assumptions): Task 5 wires at the `compute_series_metrics` call site, not in worker.
- ✓ P1-2 (ES routing): GONE by council verdict.
- ✓ P1-3 (cold-ingest missing): Task 6's runner pre-ingests.
- ✓ P1-4 (route shadowing + frontend import): Task 7 ordering; Task 10 uses `apiPost`.
- ✓ P1-5 (alerts API read-only): Task 8 + Task 12 use `AlertingService.send_alert` via CLI.
- ✓ P2-1 (Boolean can't distinguish fast/nightly): `smoke_config` key on `PortfolioRun.metrics` JSONB (Task 5).
- ✓ P2-2 (test fixture names): Task 4 / Task 6 reference `api_client_authed` + `portfolio_db_session`.
- ✓ P3-1 (fakeredis already present): Task 3 doesn't re-add.

No placeholders found. All file paths concrete.
