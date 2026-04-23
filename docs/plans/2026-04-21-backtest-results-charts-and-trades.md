# Backtest Results — Charts & Trade Log Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Every completed backtest renders populated Equity Curve, Drawdown, Monthly Returns Heatmap, and paginated Trade Log in the React detail page, with an in-app "Full Report" iframe of the QuantStats HTML and the existing download preserved.

**Architecture:** Worker materializes a canonical daily-normalized `Backtest.series` JSONB payload in the same `_finalize_backtest()` transaction as `metrics`, `report_path`, and Trade rows. `/results` endpoint returns `metrics + series + series_status + has_report` (no inline trades); new paginated `GET /backtests/{id}/trades?page=N&page_size=100` sibling endpoint serves fills. Iframe auth (iter-9 rework — no Next.js proxy): frontend calls `POST /api/v1/backtests/{id}/report-token` (authenticated) and receives a short-lived HMAC-signed URL (60s TTL, bound to `backtest_id + user_sub + exp`); `<ReportIframe>` origin-qualifies the path with `NEXT_PUBLIC_API_URL` and uses that as `src`. Backend `GET /report` accepts either normal Bearer/X-API-Key auth OR `?token=<hmac>`.

**Tech Stack:** Python 3.12 + FastAPI 0.133 + SQLAlchemy 2.0 + Pydantic 2.12 + Alembic 1.18 + NautilusTrader 1.223 + QuantStats 0.0.81 + pandas 2.3 + Postgres 16 · Next.js 15.5 + React 19.1 + Recharts 3.7 + shadcn/ui + Tailwind 4 + TypeScript 5.

**References:**

- PRD: `docs/prds/backtest-results-charts-and-trades.md`
- Decision: `docs/decisions/backtest-results-charts-and-trades.md`
- Research: `docs/research/2026-04-21-backtest-results-charts-and-trades.md`

---

## Plan Review History

### Iter 1 (2026-04-21) — NEEDS_REVISION

Claude + Codex in parallel. 4 P0 + 5 P1 + 3 P2 + 0 P3. All fixes applied in iter-1 revision (this file's current state). Summary:

- **P0 #1** `_finalize_backtest` signature wrong — real: `*, backtest_id, metrics, report_path, orders_df, strategy_id, strategy_code_hash` (no `account_df`). Revised B5 to materialize series in CALLER (at `workers/backtest_job.py:312-325` where `account_df` + `returns_series` are in scope); pass `series_payload + series_status` as two NEW params to `_finalize_backtest`.
- **P0 #2** Histogram API doesn't exist in `observability/metrics.py` (only Counter + Gauge). Added NEW Task B0b to extend the metrics primitive first; B9 now uses the extension.
- **P0 #3** Frontend auth import wrong: `useAuth()` from `@/lib/auth` exposes `getToken()` async, not `token`. F6 revised to `const { getToken } = useAuth(); const token = await getToken();` inside effect.
- **P0 #4** `report_path` not in any frontend state. Revised B6 to add `report_path: str | None` to `BacktestResultsResponse`; F3 uses `results?.report_path`.
- **P1 #1** Missed reuse of `build_series_from_returns` — B4 now delegates.
- **P1 #2** Migration test path wrong — `backend/tests/integration/test_alembic_migrations.py` with subprocess harness.
- **P1 #3** Line anchors stale — `_finalize_backtest` write block is 479-495 (not 325-332 which is the caller); `equityCurve: []` hardcode is 205-207; render at 303-310.
- **P1 #4** Test fixtures mostly missing — NEW Task B0 creates all persistence fixtures (`seeded_backtest`, `account_df_factory`, `seeded_completed_backtest_with_series`, `seeded_legacy_backtest`, `seeded_backtest_with_failed_series`, `seeded_backtest_with_n_trades`).
- **P1 #5** B3 `normalize_daily_returns` regressed existing `_normalize_report_returns` behavior — revised to preserve None handling, non-datetime indexes, string-parsed dates, and legitimate zero-return days.
- **P1 #6** UC-BRC-004 endpoint path — fixed to `/api/v1/backtests/{id}/trades`.
- **P2 #1** Observability metric name contract drift — single canonical name `msai_backtest_results_payload_bytes` per PRD + decision doc; dropped invented `_series_` + `_response_` variants.
- **P2 #2** Trade pagination non-deterministic on equal `executed_at` — added secondary sort `Trade.id.asc()`.
- **P2 #3** UC-BRC-002 ARRANGE wording — rewritten to use the migration's default `series_status='not_materialized'` as the legitimate pre-state (no manual DB mutation).

---

## Table of contents

- Design notes
- Task B0 — Test fixtures (NEW, prerequisite)
- Task B0b — Add `Histogram` primitive to observability layer (NEW, prerequisite for B9)
- Task B1 — Alembic migration: add `series` + `series_status` columns
- Task B2 — `Backtest` model + Pydantic types for series
- Task B3 — Dedupe returns-normalization into `normalize_daily_returns`
- Task B4 — `build_series_payload()` in analytics_math
- Task B5 — Worker integration in `_finalize_backtest`
- Task B6 — Extend `BacktestResultsResponse` + drop inline trades
- Task B7 — Update `/results` handler
- Task B8 — NEW paginated `/trades` endpoint
- Task B9 — Payload-size observability
- Task B10 — Signed-URL machinery for `/report` (iter-9 rework; replaces former `MSAI_API_KEY` iframe proxy)
- Task F1 — TypeScript types: `BacktestTradeItem`, `SeriesPayload`, `SeriesStatus`
- Task F2 — Signed-URL iframe client (iter-9 rework; was Next.js Route Handler proxy)
- Task F3 — Detail page: Tabs wrapper (Native view / Full report)
- Task F4 — Wire `<EquityCurveChart>` + `<DrawdownChart>` to real data
- Task F5 — Build `<MonthlyReturnsHeatmap>` native component
- Task F6 — Paginated `<TradeLog>` via `/trades` endpoint
- Task F7 — `series_status` empty-state handling across components
- E2E Use Cases (Phase 3.2b)

---

## Design notes (from `/ui-design` Phase 3.0)

**Mode: Product UI.** Dense dashboard, solo power user, no marketing/trust-first overrides.

- Dark-mode-first Geist/oklch theme (existing). No decorative motion. Skeleton loaders during `/results` poll.
- Layout: extend existing `/backtests/[id]` page. Add a `<Tabs>` container above the chart grid: **Native view** (default) + **Full report** (iframe). Existing "Download Report" button stays in the page header, visible from both tabs.
- `series_status` visual treatment:
  - `"ready"` → no indicator (expected state)
  - `"not_materialized"` → gray info icon + text "Analytics not available for backtests run before 2026-04-21"
  - `"failed"` → amber warning icon + text "Analytics computation failed — metrics below still valid"
- Functional motion only: 150–200ms tab transitions, skeleton rows while loading, optimistic pagination (disable Next while fetching).
- Color + icon + text for every status communication (never color alone).

**Component inventory:**

| Component                         | Source                                                          | Change                                                    |
| --------------------------------- | --------------------------------------------------------------- | --------------------------------------------------------- |
| `<ResultsCharts>`                 | `frontend/src/components/backtests/results-charts.tsx`          | Accept real `series` prop; remove hardcoded `[]` fallback |
| `<EquityCurveChart>` (internal)   | same file                                                       | Wire to `series.daily[]`                                  |
| `<DrawdownChart>` (internal)      | same file                                                       | Wire to `series.daily[].drawdown`                         |
| `<MonthlyReturnsHeatmap>`         | same file, currently placeholder                                | **Rewrite** as CSS Grid + Tailwind oklch (~60 LOC)        |
| `<TradeLog>`                      | `frontend/src/components/backtests/trade-log.tsx`               | Rewrite columns for individual-fill shape + pagination    |
| **NEW** `<ReportIframe>`          | `frontend/src/components/backtests/report-iframe.tsx`           | Wraps iframe w/ loading + error states                    |
| **NEW** `<SeriesStatusIndicator>` | `frontend/src/components/backtests/series-status-indicator.tsx` | Shared empty-state strip for 4 chart components           |

---

## Task B0 — Test helpers (prerequisite)

**Why:** Plan review iter-1 (P1 #4) / iter-2 (P1 #1): tests across B1–B9 reference `seeded_backtest`, `account_df_factory`, `seeded_completed_backtest_with_series`, `seeded_legacy_backtest`, `seeded_backtest_with_failed_series`, `seeded_backtest_with_n_trades`. The repo's existing pattern is NOT a shared `async_session` fixture — **there is NO `backend/tests/integration/conftest.py`**. Shared fixtures live at `backend/tests/conftest.py:21-37`. Real-DB integration tests use **per-module `session_factory`** fixtures (see `backend/tests/integration/test_backtest_live_parity.py:42-60`, `test_alembic_migrations.py`'s `isolated_postgres_url`). API-level tests mock the session via `_mock_session_returning(row)` + `get_db` override (see `backend/tests/integration/test_backtests_api.py:62-99`).

**Dual-pattern strategy:**

- **(a) Pure factories in `backend/tests/unit/conftest.py`** — in-memory Python objects. Consumed by unit tests + the API-level integration tests that mock sessions. No new DB infrastructure needed.
- **(b) Per-module `session_factory` fixture in `backend/tests/integration/test_backtest_job_finalize.py`** — real Postgres via testcontainers. Consumed ONLY by B5's atomic-write test. Follows the pattern at `test_backtest_live_parity.py:42-60`.

**Files:**

- Modify: `backend/tests/unit/conftest.py` — add pure-factory helpers (strategy (a))
- Test: helpers validated implicitly by B1–B9 tests that consume them.
- Pattern (b) lives INSIDE the specific integration test that needs it (B5), not in a shared conftest.

### Step 1: Add pure-factory helpers to `backend/tests/unit/conftest.py`

Append alongside existing `_make_backtest`. These build Python objects; no DB:

```python
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pandas as pd
import pytest

from msai.models.trade import Trade


def _make_backtest_completed_with_series(**overrides: object) -> Backtest:
    default_series = {
        "daily": [
            {"date": "2024-01-02", "equity": 100_500.0, "drawdown": 0.0, "daily_return": 0.005},
            {"date": "2024-01-03", "equity": 101_000.0, "drawdown": 0.0, "daily_return": 0.005},
        ],
        "monthly_returns": [{"month": "2024-01", "pct": 0.01}],
    }
    series = overrides.pop("series", default_series)
    series_status = overrides.pop("series_status", "ready")
    return _make_backtest(
        status="completed",
        metrics={"sharpe_ratio": 2.1, "total_return": 0.01, "num_trades": 4},
        report_path="/tmp/ready-report.html",
        series=series,
        series_status=series_status,
        **overrides,
    )


def _make_backtest_legacy(**overrides: object) -> Backtest:
    """Pre-PR Backtest: series=None, series_status='not_materialized'.

    NOTE (iter-3 P1 #1 fix): SQLAlchemy `server_default` only applies at
    DB INSERT time. Pure-factory helpers don't round-trip the DB, so we
    must set `series_status` explicitly — otherwise the attribute would
    be `None` on the returned instance and `_mock_session_returning()`
    would serve a row with `series_status=None` to handlers that expect
    `"not_materialized"`.
    """
    return _make_backtest(
        status="completed",
        metrics={"sharpe_ratio": 1.2, "total_return": 0.05, "num_trades": 10},
        report_path="/tmp/legacy-report.html",
        series=None,
        series_status="not_materialized",
        **overrides,
    )


def _make_backtest_failed_series(**overrides: object) -> Backtest:
    """Completed backtest with series_status='failed' (metrics present, series NULL)."""
    return _make_backtest(
        status="completed",
        metrics={"sharpe_ratio": 0.8, "total_return": 0.02, "num_trades": 6},
        report_path="/tmp/fail-report.html",
        series=None,
        series_status="failed",
        **overrides,
    )


def _make_backtest_with_trades(n: int) -> tuple[Backtest, list[Trade]]:
    """SYNC factory: in-memory backtest + N individual Trade fills."""
    bt = _make_backtest(status="completed", metrics={"num_trades": n})
    base_ts = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)
    trades: list[Trade] = []
    for i in range(n):
        t = Trade(
            id=uuid4(),
            backtest_id=bt.id,
            strategy_id=bt.strategy_id,
            strategy_code_hash=bt.strategy_code_hash,
            instrument="SPY.XNAS",
            side="BUY" if i % 2 == 0 else "SELL",
            quantity=Decimal("10"),
            price=Decimal("450.00"),
            pnl=Decimal("5.00") if i % 3 != 0 else None,  # mix None to exercise coalesce path
            commission=Decimal("0.50"),
            executed_at=base_ts + timedelta(seconds=i),
        )
        trades.append(t)
    return bt, trades


@pytest.fixture
def account_df_factory() -> Callable[..., pd.DataFrame]:
    """Factory: Nautilus-shaped account_df with tz-aware `returns` column."""

    def _factory(periods: int = 21, seed: float = 0.001) -> pd.DataFrame:
        idx = pd.date_range("2024-01-02", periods=periods, freq="B", tz="UTC")
        returns = pd.Series(
            [seed * (1 + i * 0.1) for i in range(periods)],
            index=idx,
            name="returns",
        )
        frame = pd.DataFrame({"returns": returns})
        frame.index = idx
        return frame

    return _factory
```

### Step 2: Pattern (b) — real-DB test harness (only for B5's atomic-write test)

This pattern stays inside the new `backend/tests/integration/test_backtest_job_finalize.py` file; NO shared integration conftest. Model it on `test_backtest_live_parity.py:42-60`:

```python
# Inside backend/tests/integration/test_backtest_job_finalize.py

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import msai.models  # noqa: F401 — triggers full Base.metadata registration for create_all
from msai.models.backtest import Backtest
from msai.models.base import Base
from msai.models.strategy import Strategy
from tests.unit.conftest import _make_backtest


# isolated_postgres_url is a PER-MODULE fixture — every test file that needs
# real DB persistence defines its own (the repo doesn't share it across modules).
# Pattern copied from backend/tests/integration/test_alembic_migrations.py:48-62.
@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()
        # Convert psycopg driver prefix → asyncpg (testcontainers returns the
        # sync form; our stack uses asyncpg). Match test_alembic_migrations.py.
        yield url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
            "postgresql://", "postgresql+asyncpg://"
        )


@pytest_asyncio.fixture
async def isolated_session_maker(isolated_postgres_url: str):
    """Per-module async session factory bound to an ISOLATED testcontainer DB.

    Uses this module's own `isolated_postgres_url` (NOT the shared `postgres_url`)
    — matches `test_backtest_live_parity.py:42-60` + `test_alembic_migrations.py`.
    """
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        # drop_all before create_all to prevent cross-test row leakage
        # (module-scoped container is reused by every B5 test). Matches
        # test_backtest_live_parity.py:52-56.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed_backtest_with_strategy_parent(maker) -> Backtest:
    """Seed a Strategy FK parent + Backtest row.

    `_make_backtest()` generates a random `strategy_id`, but `Backtest` has
    a NOT NULL FK to `strategies.id`. Without the parent seed, commit() hits
    a ForeignKeyViolation.
    """
    async with maker() as sess:
        bt = _make_backtest(status="pending")
        # Minimal valid Strategy row. NOT NULL fields per
        # backend/src/msai/models/strategy.py:28-34: name, file_path, strategy_class.
        # config_schema_status has a server_default so no explicit value needed.
        strategy = Strategy(
            id=bt.strategy_id,
            name=f"test-strategy-{bt.strategy_id.hex[:8]}",
            file_path=f"strategies/test_{bt.strategy_id.hex[:8]}.py",
            strategy_class="TestStrategy",
        )
        sess.add(strategy)
        await sess.flush()  # parent row visible to the child FK
        sess.add(bt)
        await sess.commit()
        await sess.refresh(bt)
        return bt


@pytest.mark.asyncio
async def test_finalize_persists_series(
    isolated_session_maker,
    account_df_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B5 atomic-write test — materialize series in caller, persist via _finalize_backtest.

    CRITICAL: `_finalize_backtest` uses the module-global
    `async_session_factory` from `msai.workers.backtest_job`. The fixture-
    yielded session maker won't be picked up unless we monkeypatch the
    global. See `backend/src/msai/workers/backtest_job.py:51` for the
    actual import site.
    """
    from msai.services.analytics_math import build_series_payload
    from msai.workers import backtest_job
    from msai.workers.backtest_job import _extract_returns_series, _finalize_backtest

    # Point the worker's global session factory at the testcontainer DB
    monkeypatch.setattr(backtest_job, "async_session_factory", isolated_session_maker)

    # Seed Strategy parent + Backtest row (FK constraint)
    bt = await _seed_backtest_with_strategy_parent(isolated_session_maker)

    account_df = account_df_factory(periods=21)
    payload = build_series_payload(_extract_returns_series(account_df))

    await _finalize_backtest(
        backtest_id=str(bt.id),
        metrics={"sharpe_ratio": 1.2, "total_return": 0.05, "num_trades": 10},
        report_path="/tmp/report.html",
        orders_df=pd.DataFrame(),
        strategy_id=bt.strategy_id,
        strategy_code_hash=bt.strategy_code_hash,
        series_payload=payload,
        series_status="ready",
    )

    # Read back from a fresh session (same isolated DB)
    async with isolated_session_maker() as verify_sess:
        from sqlalchemy import select

        row = (await verify_sess.execute(select(Backtest).where(Backtest.id == bt.id))).scalar_one()
        assert row.series_status == "ready"
        assert row.series is not None
        assert len(row.series["daily"]) == 21
```

For API-level tests in B6/B7/B8, use the existing `_mock_session_returning(row)` pattern at `test_backtests_api.py:62-99` — mock the session that the handler receives via `get_db` override. No new DB infrastructure.

### Step 3: Prerequisite dependency

The `Backtest` model change from Task B2 must be MERGED FIRST for the `series`/`series_status` attributes to exist on the ORM class. So: **B0 lands AFTER B1+B2 but BEFORE B3**. Update dependency graph (see Execution summary at end).

### Step 4: Commit

```bash
git add backend/tests/unit/conftest.py
git commit -m "test(backtest): pure-factory helpers for series + trade fixtures"
```

---

## Task B0b — Add `Histogram` primitive to observability layer (prerequisite for B9)

**Why:** Plan review iter-1 (P0 #2): `backend/src/msai/services/observability/metrics.py` implements only `Counter` + `Gauge`. Task B9 depends on `Histogram.observe(bytes)`. Add the primitive first.

**Files:**

- Modify: `backend/src/msai/services/observability/metrics.py` — add `Histogram` class mirroring `Counter` pattern
- Test: `backend/tests/unit/test_metrics.py`

### Step 1: Write failing test

```python
def test_histogram_records_observations_and_buckets() -> None:
    from msai.services.observability.metrics import MetricsRegistry
    registry = MetricsRegistry()
    hist = registry.histogram(
        "msai_test_hist",
        "Test histogram.",
        buckets=(100, 1_000, 10_000),
    )
    hist.observe(50)
    hist.observe(500)
    hist.observe(5_000)
    hist.observe(50_000)

    # Rendered prometheus text format includes bucket counts + sum + count
    text = registry.render()
    assert "msai_test_hist_bucket" in text
    assert "msai_test_hist_sum" in text
    assert "msai_test_hist_count" in text
    # 50 falls into le=100; 500 into le=1000; 5000 into le=10000; 50000 only into le=+Inf
    assert 'msai_test_hist_bucket{le="100"} 1' in text
    assert 'msai_test_hist_bucket{le="1000"} 2' in text
    assert 'msai_test_hist_bucket{le="10000"} 3' in text
    assert 'msai_test_hist_bucket{le="+Inf"} 4' in text
    assert "msai_test_hist_count 4" in text


def test_histogram_idempotent_registration() -> None:
    from msai.services.observability.metrics import MetricsRegistry
    registry = MetricsRegistry()
    h1 = registry.histogram("msai_dup", "dup", buckets=(1, 10))
    h2 = registry.histogram("msai_dup", "dup", buckets=(1, 10))
    assert h1 is h2
```

### Step 2: Run test to verify it fails

```bash
cd backend && uv run pytest tests/unit/test_metrics.py::test_histogram_records_observations_and_buckets -v
```

Expected: FAIL with `AttributeError: 'MetricsRegistry' object has no attribute 'histogram'`.

### Step 3: Implement `Histogram`

In `backend/src/msai/services/observability/metrics.py`, add alongside `Counter` + `Gauge`:

```python
class Histogram(_LabeledMetric):
    """Histogram with cumulative buckets (Prometheus-style).

    Follows the existing `_LabeledMetric` contract: `self.name`,
    `self.help_text`, `render()` returns `list[str]`. Stored under
    `MetricsRegistry._metrics` alongside Counter + Gauge.
    """

    metric_type = "histogram"

    def __init__(self, name: str, help_text: str, buckets: tuple[int, ...]) -> None:
        super().__init__(name, help_text)
        # Always include +Inf bucket; keep as ints for integer bucket labels.
        self._bucket_upper_bounds: tuple[int | float, ...] = tuple(sorted(buckets)) + (float("inf"),)
        self._bucket_counts: list[int] = [0] * len(self._bucket_upper_bounds)
        self._sum: float = 0.0
        self._count: int = 0

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += float(value)
            self._count += 1
            for i, upper in enumerate(self._bucket_upper_bounds):
                if value <= upper:
                    self._bucket_counts[i] += 1  # cumulative at observe time

    def render(self) -> list[str]:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            for upper, count in zip(self._bucket_upper_bounds, self._bucket_counts, strict=True):
                label = "+Inf" if upper == float("inf") else str(upper)
                lines.append(f'{self.name}_bucket{{le="{label}"}} {count}')
            lines.append(f"{self.name}_sum {self._sum}")
            lines.append(f"{self.name}_count {self._count}")
        return lines
```

Add `histogram()` method on `MetricsRegistry` mirroring the existing `counter()` / `gauge()` methods (see `metrics.py:186-213`):

```python
def histogram(self, name: str, help_text: str, buckets: tuple[int, ...]) -> Histogram:
    with self._lock:
        existing = self._metrics.get(name)
        if existing is not None:
            if not isinstance(existing, Histogram):
                raise TypeError(f"Metric '{name}' already registered as non-Histogram")
            return existing
        h = Histogram(name, help_text, buckets)
        self._metrics[name] = h
        return h
```

The existing `MetricsRegistry.render()` at `metrics.py:216-225` collates each metric's `list[str]` — adding Histogram requires no change to `render()`.

### Step 4: Run test

```bash
cd backend && uv run pytest tests/unit/test_metrics.py -v
cd backend && uv run mypy src/msai/services/observability/metrics.py --strict
```

Expected: PASS.

### Step 5: Commit

```bash
git add backend/src/msai/services/observability/metrics.py backend/tests/unit/test_metrics.py
git commit -m "feat(observability): Histogram primitive for byte/latency observations"
```

---

## Task B1 — Alembic migration: add `series` + `series_status` columns

**Why:** `Backtest.series` JSONB stores the canonical daily-normalized payload; `series_status` disambiguates "ready" from "not_materialized" (legacy rows) and "failed" (compute errors). Both nullable-safe via ADD COLUMN metadata-only on Postgres 16 (research finding #3).

**Files:**

- Create: `backend/alembic/versions/z4x5y6z7a8b9_add_backtest_series_columns.py`
- Modify: `backend/src/msai/models/backtest.py` (in later task — B2 depends on this migration existing)
- Test: `backend/tests/integration/test_alembic_migrations.py` (add round-trip test for new revision)

### Step 1: Write the failing test

In `backend/tests/integration/test_alembic_migrations.py`, add a test that runs AFTER the existing `test_alembic_upgrade_head_on_fresh_db` pattern (subprocess-based, uses the `isolated_postgres_url` fixture). Follow the existing harness convention — do NOT introduce `async_engine`/`async_session`:

```python
def test_migration_z4x5y6z7a8b9_adds_series_columns(
    isolated_postgres_url: str,
) -> None:
    """Migration z4x5y6z7a8b9 adds series JSONB NULL + series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'."""
    env = os.environ.copy()
    env["DATABASE_URL"] = isolated_postgres_url
    # Migrate to HEAD (includes our new revision)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],  # backend/
        env=env,
        check=True,
    )
    engine = create_async_engine(isolated_postgres_url)
    # Synchronous inspection through a sync connection (test uses asyncio.run internally
    # in the existing harness; follow that convention if present, or use engine.begin in an asyncio.run)
    import asyncio

    async def _inspect() -> None:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'backtests' AND column_name IN ('series', 'series_status') "
                    "ORDER BY column_name"
                )
            )
            rows = list(result)
            assert len(rows) == 2
            series = next(r for r in rows if r[0] == "series")
            assert series[1] == "jsonb"
            assert series[2] == "YES"
            assert series[3] is None
            status_col = next(r for r in rows if r[0] == "series_status")
            assert status_col[1] == "character varying"
            assert status_col[2] == "NO"
            assert "'not_materialized'" in (status_col[3] or "")

    asyncio.run(_inspect())
    # Engine cleanup handled by the existing isolated_postgres_url teardown
```

Adapt to the existing harness's conventions — if the existing test uses `asyncio.get_event_loop().run_until_complete(...)` or a different inspection pattern, match it exactly.

### Step 2: Run test to verify it fails

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_migration_z4x5y6z7a8b9_adds_series_columns -v
```

Expected: FAIL with "relation backtests has no column series" or similar.

### Step 3: Write the migration

Create `backend/alembic/versions/z4x5y6z7a8b9_add_backtest_series_columns.py`. Chain after the latest revision (`y3s4t5u6v7w8_add_backtest_auto_heal_columns`):

```python
"""add backtest series + series_status columns

Revision ID: z4x5y6z7a8b9
Revises: y3s4t5u6v7w8
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "z4x5y6z7a8b9"
down_revision = "y3s4t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add series JSONB NULL + series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'."""
    op.add_column(
        "backtests",
        sa.Column("series", JSONB, nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column(
            "series_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'not_materialized'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("backtests", "series_status")
    op.drop_column("backtests", "series")
```

### Step 4: Apply the migration

```bash
cd backend && uv run alembic upgrade head
```

Expected: Migration applied cleanly. Pre-existing `backtests` rows now have `series = NULL` and `series_status = 'not_materialized'`.

### Step 5: Run test to verify it passes

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_migration_z4x5y6z7a8b9_adds_series_columns -v
```

Expected: PASS.

### Step 6: Commit

```bash
git add backend/alembic/versions/z4x5y6z7a8b9_add_backtest_series_columns.py backend/tests/integration/test_alembic_migrations.py
git commit -m "feat(backtest): alembic migration for series + series_status columns"
```

---

## Task B2 — `Backtest` model + Pydantic schema types

**Why:** SQLAlchemy model exposes the new columns as attributes; new Pydantic types `SeriesStatus` enum + `SeriesPayload` / `SeriesDailyPoint` / `SeriesMonthlyReturn` define the JSONB shape contract that both worker (writer) and API (reader) must honor.

**Files:**

- Modify: `backend/src/msai/models/backtest.py`
- Modify: `backend/src/msai/schemas/backtest.py`
- Test: `backend/tests/unit/test_backtest_model.py`, `backend/tests/unit/test_backtest_schemas.py`

### Step 1: Write failing tests

In `backend/tests/unit/test_backtest_model.py`:

```python
def test_backtest_model_has_series_attribute() -> None:
    from msai.models.backtest import Backtest
    assert hasattr(Backtest, "series")
    assert hasattr(Backtest, "series_status")


def test_backtest_model_defaults(make_backtest: Callable[..., Backtest]) -> None:
    """Unset series_status defaults to 'not_materialized' at DB level."""
    bt = make_backtest()
    # Value only populated after flush/insert; the DB DEFAULT handles new rows.
    assert bt.series is None
    # No Python-side default; DB DEFAULT handles it.
```

In `backend/tests/unit/test_backtest_schemas.py`:

```python
from typing import get_args

import pytest
from pydantic import ValidationError

from msai.schemas.backtest import (
    SeriesDailyPoint,
    SeriesMonthlyReturn,
    SeriesPayload,
    SeriesStatus,
)


def test_series_status_enum_values() -> None:
    assert set(get_args(SeriesStatus)) == {"ready", "not_materialized", "failed"}


def test_series_daily_point_validation() -> None:
    p = SeriesDailyPoint(date="2024-01-02", equity=100250.50, drawdown=-0.05, daily_return=0.0025)
    assert p.date == "2024-01-02"
    assert p.drawdown <= 0  # invariant: drawdown is non-positive

    with pytest.raises(ValidationError):
        SeriesDailyPoint(date="not-a-date", equity=100_000, drawdown=0, daily_return=0)


def test_series_monthly_return_format() -> None:
    m = SeriesMonthlyReturn(month="2024-01", pct=0.0512)
    assert m.month == "2024-01"

    with pytest.raises(ValidationError):
        SeriesMonthlyReturn(month="2024-1", pct=0.05)  # must be zero-padded


def test_series_payload_round_trip() -> None:
    payload = SeriesPayload(
        daily=[SeriesDailyPoint(date="2024-01-02", equity=100_000.0, drawdown=0.0, daily_return=0.0)],
        monthly_returns=[SeriesMonthlyReturn(month="2024-01", pct=0.05)],
    )
    dumped = payload.model_dump(mode="json")
    restored = SeriesPayload.model_validate(dumped)
    assert restored == payload
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/unit/test_backtest_model.py::test_backtest_model_has_series_attribute tests/unit/test_backtest_schemas.py -v
```

Expected: FAIL with `AttributeError` or `ImportError`.

### Step 3: Implement the model change

In `backend/src/msai/models/backtest.py`, add to the `Backtest` class (preserve all existing columns/relationships):

```python
    # ... existing columns ...
    series: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    series_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="not_materialized",
    )
```

### Step 4: Implement the Pydantic schemas

In `backend/src/msai/schemas/backtest.py`, add:

```python
from __future__ import annotations

from datetime import date as _date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SeriesStatus = Literal["ready", "not_materialized", "failed"]


class SeriesDailyPoint(BaseModel):
    """One day of the canonical normalized returns series."""

    date: str  # ISO YYYY-MM-DD
    equity: float = Field(..., gt=0.0)
    drawdown: float = Field(..., le=0.0)  # non-positive by construction
    daily_return: float

    @field_validator("date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        _date.fromisoformat(v)  # raises ValueError on bad format
        return v


class SeriesMonthlyReturn(BaseModel):
    """Month-end return aggregate."""

    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    pct: float


class SeriesPayload(BaseModel):
    """Canonical analytics payload written by worker, consumed by API + UI."""

    daily: list[SeriesDailyPoint]
    monthly_returns: list[SeriesMonthlyReturn]
```

### Step 5: Run tests to verify they pass

```bash
cd backend && uv run pytest tests/unit/test_backtest_model.py tests/unit/test_backtest_schemas.py -v
cd backend && uv run mypy src/msai/models/backtest.py src/msai/schemas/backtest.py --strict
cd backend && uv run ruff check src/msai/models/backtest.py src/msai/schemas/backtest.py
```

Expected: all PASS; mypy clean; ruff clean.

### Step 6: Commit

```bash
git add backend/src/msai/models/backtest.py backend/src/msai/schemas/backtest.py backend/tests/unit/test_backtest_model.py backend/tests/unit/test_backtest_schemas.py
git commit -m "feat(backtest): Backtest.series + SeriesPayload/SeriesStatus schemas"
```

---

## Task B3 — Dedupe returns-normalization into `normalize_daily_returns`

**Why:** Research finding #4 + Maintainer blocking objection: `_normalize_report_returns` (in `report_generator.py`) and `build_series_from_returns` (in `analytics_math.py`) are two separate compute paths that can drift. Extract one canonical function both paths call.

**Files:**

- Modify: `backend/src/msai/services/analytics_math.py` — add `normalize_daily_returns(returns) -> pd.Series`
- Modify: `backend/src/msai/services/report_generator.py` — use `normalize_daily_returns` in `_normalize_report_returns`
- Test: `backend/tests/unit/test_analytics_math.py`, `backend/tests/unit/test_report_generator.py`

### Step 1: Write the failing test

In `backend/tests/unit/test_analytics_math.py`:

```python
import pandas as pd

from msai.services.analytics_math import normalize_daily_returns


def test_normalize_daily_returns_compounds_intraday_to_daily() -> None:
    """Minute-bar returns compound to daily via (1+r).resample('1D').prod()-1."""
    # 3 intraday bars on 2024-01-02 (Tue), 2 bars on 2024-01-03
    idx = pd.DatetimeIndex(
        [
            "2024-01-02 09:30", "2024-01-02 12:00", "2024-01-02 15:59",
            "2024-01-03 09:30", "2024-01-03 15:59",
        ],
        tz="UTC",
    )
    returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx, name="returns")

    daily = normalize_daily_returns(returns)

    assert len(daily) == 2  # 2 trading days
    assert daily.index[0].strftime("%Y-%m-%d") == "2024-01-02"
    # (1.01 * 0.995 * 1.003) - 1
    assert daily.iloc[0] == pytest.approx((1.01 * 0.995 * 1.003) - 1, rel=1e-6)


def test_normalize_daily_returns_handles_tz_naive() -> None:
    """Accepts tz-naive index (legacy data), localizes to UTC."""
    idx = pd.date_range("2024-01-02", periods=3, freq="D")
    returns = pd.Series([0.01, 0.02, -0.01], index=idx, name="returns")
    result = normalize_daily_returns(returns)
    assert result.index.tz is not None
    assert len(result) == 3


def test_normalize_daily_returns_empty_input() -> None:
    """Empty Series → empty Series, no exception."""
    empty = pd.Series(dtype=float, name="returns")
    result = normalize_daily_returns(empty)
    assert len(result) == 0


def test_normalize_daily_returns_drops_nan() -> None:
    """NaN rows are dropped before compounding."""
    idx = pd.date_range("2024-01-02", periods=3, freq="D")
    returns = pd.Series([0.01, float("nan"), 0.02], index=idx)
    result = normalize_daily_returns(returns)
    assert len(result) == 2


def test_normalize_daily_returns_accepts_none() -> None:
    """None input returns an empty Series named 'returns' (preserves legacy contract)."""
    result = normalize_daily_returns(None)
    assert result.empty
    assert result.name == "returns"


def test_normalize_daily_returns_preserves_zero_return_days() -> None:
    """A legitimate zero-return day must be retained (unlike a no-data day)."""
    idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
    returns = pd.Series([0.0, 0.01, 0.0], index=idx)
    result = normalize_daily_returns(returns)
    assert len(result) == 3
    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[2] == pytest.approx(0.0)


def test_normalize_daily_returns_coerces_string_dates() -> None:
    """String-parsed date index is coerced to UTC datetime (legacy data path)."""
    returns = pd.Series(
        [0.01, 0.02],
        index=pd.Index(["2024-01-02", "2024-01-03"], dtype=object),
    )
    result = normalize_daily_returns(returns)
    assert result.index.tz is not None


def test_report_generator_backwards_compatibility() -> None:
    """_normalize_report_returns delegates to normalize_daily_returns and passes existing test coverage."""
    # Run the existing test_report_generator.py::194-287 suite after the refactor — it must still be GREEN.
    # (This is a meta-assertion; validated in Step 5 by running the existing test file.)
    pass
```

### Step 2: Run test to verify it fails

```bash
cd backend && uv run pytest tests/unit/test_analytics_math.py::test_normalize_daily_returns_compounds_intraday_to_daily -v
```

Expected: FAIL with `ImportError`.

### Step 3: Implement

In `backend/src/msai/services/analytics_math.py`, add near the top:

```python
def normalize_daily_returns(series: pd.Series | None) -> pd.Series:
    """Canonical returns normalization used by BOTH the QuantStats report
    generator and the persisted `Backtest.series` payload. Preserves the
    existing `_normalize_report_returns()` behavior exactly — do NOT
    regress:
    - Accepts `None` → empty Series named `"returns"`
    - Accepts tz-naive → localizes to UTC
    - Accepts non-datetime indexes or string-parsed date indexes → coerce to UTC datetime
    - Accepts numeric dtype → unchanged
    - Non-numeric dtype → coerce to float (errors → NaN → dropped)
    - **PRESERVES legitimate zero-return days** (only drops NaN)
    - Compounds intraday → daily via `(1 + r).resample("1D").prod() - 1`

    This function is a literal move of the existing
    `backend/src/msai/services/report_generator.py::_normalize_report_returns`
    body into analytics_math. Do not restructure the logic; only move +
    rename.
    """
    # [Move the exact body of _normalize_report_returns from
    #  backend/src/msai/services/report_generator.py:20-59 to here.
    #  Do not alter the semantics; the coverage at
    #  backend/tests/unit/test_report_generator.py::194-287 must still pass.]
    raise NotImplementedError("Move the body of _normalize_report_returns() here verbatim.")
```

**IMPORTANT:** Open `backend/src/msai/services/report_generator.py:20-59` and copy the full body of `_normalize_report_returns` into this new function. The existing test `backend/tests/unit/test_report_generator.py::194-287` is the behavior contract — do not change any branch of the original logic.

Then in `backend/src/msai/services/report_generator.py`, update `_normalize_report_returns` to delegate:

```python
from msai.services.analytics_math import normalize_daily_returns

def _normalize_report_returns(series: pd.Series | None) -> pd.Series:
    """Legacy wrapper — delegates to analytics_math.normalize_daily_returns.

    Kept for backwards-compatibility with existing callers; remove when
    all sites migrate to the canonical helper.
    """
    if series is None:
        return pd.Series(dtype=float, name="returns")
    return normalize_daily_returns(series)
```

### Step 4: Update existing test expectations

Open `backend/tests/unit/test_report_generator.py`; confirm existing tests still pass with the delegation (they test behavior, not internals). If any test directly introspected the old `_normalize_report_returns` implementation, update to assert against the canonical function's output.

### Step 5: Run all affected tests

```bash
cd backend && uv run pytest tests/unit/test_analytics_math.py tests/unit/test_report_generator.py -v
cd backend && uv run mypy src/msai/services/analytics_math.py src/msai/services/report_generator.py --strict
```

Expected: all PASS.

### Step 6: Commit

```bash
git add backend/src/msai/services/analytics_math.py backend/src/msai/services/report_generator.py backend/tests/unit/test_analytics_math.py backend/tests/unit/test_report_generator.py
git commit -m "refactor(analytics): single canonical normalize_daily_returns"
```

---

## Task B4 — `build_series_payload()` — canonical series + monthly aggregation

**Why:** Produce the `SeriesPayload` the worker will persist. Reuses existing `build_series_from_returns` for `daily[]` (equity/drawdown/daily_return per row) and adds month-end aggregation.

**Files:**

- Modify: `backend/src/msai/services/analytics_math.py` — add `build_series_payload`
- Test: `backend/tests/unit/test_analytics_math.py` — new test class `TestBuildSeriesPayload`

### Step 1: Write failing tests

```python
class TestBuildSeriesPayload:
    def test_builds_daily_and_monthly_from_returns(self) -> None:
        from msai.services.analytics_math import build_series_payload

        # Jan 2024 — 5 trading days, simple returns
        idx = pd.date_range("2024-01-02", periods=5, freq="B", tz="UTC")
        returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx)

        payload = build_series_payload(returns)

        assert len(payload["daily"]) == 5
        assert payload["daily"][0]["date"] == "2024-01-02"
        assert payload["daily"][0]["equity"] == pytest.approx(101_000.0, rel=1e-6)
        assert payload["daily"][0]["drawdown"] == 0.0  # new high, no drawdown
        assert len(payload["monthly_returns"]) == 1
        assert payload["monthly_returns"][0]["month"] == "2024-01"

    def test_drawdown_is_non_positive(self) -> None:
        from msai.services.analytics_math import build_series_payload
        idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
        returns = pd.Series([0.02, -0.03, 0.01], index=idx)
        payload = build_series_payload(returns)
        drawdowns = [p["drawdown"] for p in payload["daily"]]
        assert all(d <= 0.0 for d in drawdowns)

    def test_multi_month_produces_multi_monthly(self) -> None:
        from msai.services.analytics_math import build_series_payload
        idx = pd.date_range("2024-01-02", periods=45, freq="B", tz="UTC")
        returns = pd.Series([0.001] * 45, index=idx)
        payload = build_series_payload(returns)
        assert len(payload["monthly_returns"]) == 2  # Jan + Feb
        assert payload["monthly_returns"][0]["month"] == "2024-01"
        assert payload["monthly_returns"][1]["month"] == "2024-02"

    def test_empty_returns_yields_empty_payload(self) -> None:
        from msai.services.analytics_math import build_series_payload
        payload = build_series_payload(pd.Series(dtype=float))
        assert payload == {"daily": [], "monthly_returns": []}

    def test_payload_validates_against_pydantic(self) -> None:
        """Output must round-trip through SeriesPayload validation."""
        from msai.schemas.backtest import SeriesPayload
        from msai.services.analytics_math import build_series_payload
        idx = pd.date_range("2024-01-02", periods=5, freq="B", tz="UTC")
        returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx)
        payload_dict = build_series_payload(returns)
        SeriesPayload.model_validate(payload_dict)  # raises if invalid
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/unit/test_analytics_math.py::TestBuildSeriesPayload -v
```

Expected: FAIL with `ImportError`.

### Step 3: Implement

In `backend/src/msai/services/analytics_math.py`:

```python
from typing import TypedDict


class _DailyPointDict(TypedDict):
    date: str
    equity: float
    drawdown: float
    daily_return: float


class _MonthlyReturnDict(TypedDict):
    month: str
    pct: float


class _PayloadDict(TypedDict):
    daily: list[_DailyPointDict]
    monthly_returns: list[_MonthlyReturnDict]


def build_series_payload(
    returns: pd.Series | None, starting_equity: float = 100_000.0
) -> _PayloadDict:
    """Build the canonical `SeriesPayload` dict from a returns Series.

    Delegates equity + drawdown math to the existing `build_series_from_returns`
    helper (single compute path per council Maintainer requirement), then
    adds monthly aggregation and formats output as `SeriesPayload`.

    Returns an empty payload if input is empty or None.
    """
    daily_returns = normalize_daily_returns(returns)
    if daily_returns.empty:
        return {"daily": [], "monthly_returns": []}

    # Delegate to existing helper for equity/drawdown math — same formula,
    # same DataFrame shape [timestamp, returns, equity, drawdown].
    frame = build_series_from_returns(daily_returns, base_value=starting_equity)
    if frame.empty:
        return {"daily": [], "monthly_returns": []}

    daily: list[_DailyPointDict] = [
        {
            "date": pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%d"),
            "equity": float(row["equity"]),
            "drawdown": float(row["drawdown"]),
            "daily_return": float(row["returns"]),
        }
        for _, row in frame.iterrows()
    ]

    # Monthly returns: compound daily into month-end aggregates.
    monthly_series = (1.0 + daily_returns).resample("ME").prod() - 1.0
    monthly_returns: list[_MonthlyReturnDict] = [
        {"month": pd.Timestamp(ts).strftime("%Y-%m"), "pct": float(pct)}
        for ts, pct in zip(monthly_series.index, monthly_series, strict=True)
    ]

    return {"daily": daily, "monthly_returns": monthly_returns}
```

### Step 4: Run tests

```bash
cd backend && uv run pytest tests/unit/test_analytics_math.py::TestBuildSeriesPayload -v
cd backend && uv run mypy src/msai/services/analytics_math.py --strict
```

Expected: PASS.

### Step 5: Commit

```bash
git add backend/src/msai/services/analytics_math.py backend/tests/unit/test_analytics_math.py
git commit -m "feat(analytics): build_series_payload with daily equity/drawdown + monthly aggregation"
```

---

## Task B5 — Worker integration: series materialization in caller + persistence in `_finalize_backtest`

**Why:** Materialize `series` atomically with `metrics`, `report_path`, and terminal status. Catch exceptions → set `series_status="failed"` + structured log, but DO NOT fail the backtest itself (series failure doesn't cascade — per PRD US-006).

**Architecture (revised iter-1 P0 #1):** `_finalize_backtest()` has an existing keyword-only signature `*, backtest_id, metrics, report_path, orders_df, strategy_id, strategy_code_hash` — it does NOT take `account_df`. Revised split:

1. **In the CALLER** (`_execute_backtest` around `backtest_job.py:312-325`, where `result.account_df` + `returns_series` are already in scope), build the series payload via `build_series_payload(returns_series)` BEFORE calling `_finalize_backtest`. Wrap in try/except → on failure, pass `series_payload=None, series_status="failed"`.
2. **Add two NEW keyword-only parameters** to `_finalize_backtest`: `series_payload: dict[str, Any] | None` and `series_status: str`. Both persisted in the existing write block (actual location: lines 479-495 — NOT 325-332 which is the caller).

**Files:**

- Modify: `backend/src/msai/workers/backtest_job.py`:
  - `_execute_backtest` caller block around lines 312-325 (build series payload before `_finalize_backtest` call)
  - `_finalize_backtest` signature + write block at lines 479-495 (accept + persist `series_payload` + `series_status`)
- Test: `backend/tests/integration/test_backtest_job_finalize.py` (create if absent) + add cases to existing `backend/tests/unit/test_backtest_schemas.py tests/integration/test_backtest_job_finalize.py`

### Step 1: Write the failing test

In `backend/tests/integration/test_backtest_job_finalize.py`:

```python
"""Integration tests for _finalize_backtest series materialization."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models.backtest import Backtest


@pytest.mark.asyncio
async def test_finalize_accepts_failed_series_status(
    isolated_session_maker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller passes series_payload=None + series_status='failed' → row reflects that; status stays 'completed'."""
    from msai.workers import backtest_job
    from msai.workers.backtest_job import _finalize_backtest

    monkeypatch.setattr(backtest_job, "async_session_factory", isolated_session_maker)

    bt = await _seed_backtest_with_strategy_parent(isolated_session_maker)

    await _finalize_backtest(
        backtest_id=str(bt.id),
        metrics={"sharpe_ratio": 0.5},
        report_path="/tmp/report.html",
        orders_df=pd.DataFrame(),
        strategy_id=bt.strategy_id,
        strategy_code_hash=bt.strategy_code_hash,
        series_payload=None,
        series_status="failed",
    )

    from sqlalchemy import select
    async with isolated_session_maker() as verify_sess:
        row = (await verify_sess.execute(select(Backtest).where(Backtest.id == bt.id))).scalar_one()
        assert row.status == "completed"
        assert row.series_status == "failed"
        assert row.series is None
        assert row.metrics is not None


@pytest.mark.asyncio
async def test_finalize_atomic_with_metrics_and_report_path(
    isolated_session_maker,
    account_df_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """series, series_status, metrics, report_path all committed in one transaction."""
    from msai.services.analytics_math import build_series_payload
    from msai.workers import backtest_job
    from msai.workers.backtest_job import _extract_returns_series, _finalize_backtest

    monkeypatch.setattr(backtest_job, "async_session_factory", isolated_session_maker)

    bt = await _seed_backtest_with_strategy_parent(isolated_session_maker)

    account_df = account_df_factory(periods=10)
    payload = build_series_payload(_extract_returns_series(account_df))

    await _finalize_backtest(
        backtest_id=str(bt.id),
        metrics={"sharpe_ratio": 1.0},
        report_path="/tmp/r.html",
        orders_df=pd.DataFrame(),
        strategy_id=bt.strategy_id,
        strategy_code_hash=bt.strategy_code_hash,
        series_payload=payload,
        series_status="ready",
    )

    from sqlalchemy import select
    async with isolated_session_maker() as verify_sess:
        row = (await verify_sess.execute(select(Backtest).where(Backtest.id == bt.id))).scalar_one()
        assert row.series_status == "ready"
        assert row.metrics is not None
        assert row.report_path == "/tmp/r.html"
        assert row.completed_at is not None


def test_materialize_series_payload_success_returns_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: helper returns (payload_dict, "ready") + emits structured INFO log."""
    import pandas as pd
    import structlog.testing

    from msai.workers import backtest_job

    idx = pd.date_range("2024-01-02", periods=3, freq="B", tz="UTC")
    returns = pd.Series([0.01, -0.005, 0.003], index=idx, name="returns")

    with structlog.testing.capture_logs() as captured:
        payload, status = backtest_job._materialize_series_payload(
            returns_series=returns,
            backtest_id="bt-happy",
        )

    assert payload is not None
    assert status == "ready"
    assert "daily" in payload
    assert "monthly_returns" in payload
    assert any(
        entry.get("event") == "backtest_series_materialized"
        and entry.get("log_level") == "info"
        for entry in captured
    )


def test_materialize_series_payload_failure_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When build_series_payload raises, helper returns (None, "failed") + WARNING structured log.

    Uses `structlog.testing.capture_logs()` — this project's structlog config
    bypasses stdlib logging so `caplog` would miss events. See precedent:
    `backend/tests/unit/services/backtests/test_auto_heal.py:251`.
    """
    import pandas as pd
    import structlog.testing

    from msai.workers import backtest_job

    def _boom(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("simulated series-build failure")

    monkeypatch.setattr(backtest_job, "build_series_payload", _boom)

    with structlog.testing.capture_logs() as captured:
        payload, status = backtest_job._materialize_series_payload(
            returns_series=pd.Series([0.01], name="returns"),
            backtest_id="bt-fail",
        )

    assert payload is None
    assert status == "failed"
    assert any(
        entry.get("event") == "backtest_series_materialization_failed"
        and entry.get("log_level") in ("warning", "error")
        for entry in captured
    )
```

These two tests are concrete, executable, and FAIL until the helper is implemented (red). Then turn green once B5's `_materialize_series_payload` lands.

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/integration/test_backtest_job_finalize.py -v
```

Expected: FAIL (series column written as NULL; series_status stays 'not_materialized').

### Step 3: Implement (three changes)

**Change 3a — Extract `_materialize_series_payload` helper** in `backend/src/msai/workers/backtest_job.py` (module-level, alongside existing helpers). This is the function the B5 failure-path test targets:

```python
import json
from typing import Any

from msai.schemas.backtest import SeriesPayload
from msai.services.analytics_math import build_series_payload


def _materialize_series_payload(
    returns_series: pd.Series,
    backtest_id: str,
    nautilus_version: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Build the canonical SeriesPayload. Fail-soft: returns (None, "failed") on exception.

    Returns:
        (payload_dict, "ready") on success — payload validated against Pydantic SeriesPayload.
        (None, "failed") on any exception — WARNING log with exc_info + nautilus_version.

    This helper is the unit the caller-failure test exercises directly (see
    `test_materialize_series_payload_failure_returns_failed`). Extracting it
    keeps the test scope narrow and the caller-side block small.

    PRD §7 audit contract:
    - Success: INFO log `backtest_series_materialized` with backtest_id + daily_rows + monthly_rows + payload_bytes.
    - Failure: WARNING log `backtest_series_materialization_failed` with backtest_id + nautilus_version + exc_info.
    """
    try:
        payload = build_series_payload(returns_series)
        SeriesPayload.model_validate(payload)
        payload_bytes = len(json.dumps(payload).encode("utf-8"))
        # msai_backtest_results_payload_bytes.observe(payload_bytes)  — wired in Task B9
        if payload_bytes > 1_048_576:
            log.warning(
                "backtest_series_payload_oversized",
                backtest_id=backtest_id,
                payload_bytes=payload_bytes,
            )
        log.info(
            "backtest_series_materialized",
            backtest_id=backtest_id,
            daily_rows=len(payload["daily"]),
            monthly_rows=len(payload["monthly_returns"]),
            payload_bytes=payload_bytes,  # PRD §7 field
        )
        return payload, "ready"
    except Exception:  # noqa: BLE001 — fail-soft per PRD US-006
        log.warning(
            "backtest_series_materialization_failed",
            backtest_id=backtest_id,
            nautilus_version=nautilus_version,  # PRD §7 field
            exc_info=True,
        )
        return None, "failed"
```

**Change 3b — Caller-side call** in `_execute_backtest` around lines 312-325 (right after QuantStats runs, before `_finalize_backtest` is called):

```python
# returns_series already in scope from _extract_returns_series(result.account_df) at line 312
# nautilus_ver captured earlier around line 284 for _persist_lineage() — reuse it here.
series_payload, series_status = _materialize_series_payload(
    returns_series=returns_series,
    backtest_id=backtest_id,
    nautilus_version=nautilus_ver,
)

await _finalize_backtest(
    backtest_id=backtest_id,
    metrics=result.metrics,
    report_path=report_path,
    orders_df=result.orders_df,
    strategy_id=strategy_id,
    strategy_code_hash=strategy_code_hash,
    series_payload=series_payload,
    series_status=series_status,
)
```

**Change 3c — Extend `_finalize_backtest` signature + write block** at lines 449-495. Current signature keeps its existing keyword-only params; add two more:

```python
async def _finalize_backtest(
    *,
    backtest_id: str,
    metrics: dict[str, float | int],
    report_path: str,
    orders_df: pd.DataFrame,
    strategy_id: Any,
    strategy_code_hash: str,
    series_payload: dict[str, Any] | None,   # NEW
    series_status: str,                       # NEW — one of "ready" | "failed"
) -> None:
    """Persist metrics, report path, trade rows, AND canonical series payload atomically."""
    async with async_session_factory() as session:
        row = await session.get(Backtest, backtest_id)
        if row is None:
            return

        row.status = "completed"
        row.progress = 100
        row.metrics = dict(metrics)
        row.report_path = report_path
        row.completed_at = datetime.now(UTC)
        row.series = series_payload           # NEW
        row.series_status = series_status     # NEW

        for order in orders_df.to_dict(orient="records"):
            trade = _order_row_to_trade(
                order=order,
                backtest_id=row.id,
                strategy_id=strategy_id,
                strategy_code_hash=strategy_code_hash,
            )
            if trade is not None:
                session.add(trade)

        await session.commit()
```

This follows the ORM attribute-assignment pattern that's already in place at lines 479-495 (matches the existing style, avoids introducing `update()` statement drift).

### Step 4: Run tests

```bash
cd backend && uv run pytest tests/integration/test_backtest_job_finalize.py -v
cd backend && uv run pytest tests/unit/test_backtest_schemas.py tests/integration/test_backtest_job_finalize.py -v
cd backend && uv run mypy src/msai/workers/backtest_job.py --strict
```

Expected: all PASS.

### Step 5: Commit

```bash
git add backend/src/msai/workers/backtest_job.py backend/tests/integration/test_backtest_job_finalize.py
git commit -m "feat(worker): atomic series materialization in _finalize_backtest"
```

---

> **Test-pattern note (iter-2 P1 #1 fix) applies to all B6/B7/B8 API tests below.**
>
> The existing integration tests at `backend/tests/integration/test_backtests_api.py:62-99` mock the session via `_mock_session_returning(row)` + an `app.dependency_overrides[get_db]` override + the shared `client` fixture from `backend/tests/conftest.py`. The seeded-fixture names below (`seeded_completed_backtest_with_series`, `seeded_legacy_backtest`, etc.) are **conceptual shorthand**. During implementation, construct the row via the pure factories from B0 (e.g., `_make_backtest_completed_with_series()`), hand it to `_mock_session_returning(row)`, install the `get_db` override, then make the HTTP request via the shared `client`.
>
> Example pattern to follow:
>
> ```python
> from tests.integration.test_backtests_api import _mock_session_returning
> from tests.unit.conftest import _make_backtest_completed_with_series
> from msai.core.database import get_db
> from msai.main import app
>
> @pytest.mark.asyncio
> async def test_results_returns_series_when_ready(client: httpx.AsyncClient) -> None:
>     bt = _make_backtest_completed_with_series()
>     session = _mock_session_returning(bt)
>     app.dependency_overrides[get_db] = lambda: _yield(session)
>     try:
>         response = await client.get(f"/api/v1/backtests/{bt.id}/results")
>         body = response.json()
>         assert body["series_status"] == "ready"
>         assert body["series"] is not None
>     finally:
>         app.dependency_overrides.pop(get_db, None)
> ```
>
> B8 tests that need N trades use `_make_backtest_with_trades(150)` (sync, returns `(bt, trades)`) and then mock the session's trade-query execution accordingly.

## Task B6 — Extend `BacktestResultsResponse`, drop inline trades

**Why:** API contract must include `series` + `series_status`; simultaneously remove `trades: list[dict]` from `/results` response (moves to new paginated `/trades` endpoint per PRD US-004). This is a breaking change for any external API client — but per PRD §Non-Goals, only the frontend consumes this endpoint, and the frontend update is in F1/F6.

**Files:**

- Modify: `backend/src/msai/schemas/backtest.py`
- Test: `backend/tests/unit/test_backtest_schemas.py`

### Step 1: Write the failing test

```python
def test_backtest_results_response_has_series_and_status() -> None:
    from msai.schemas.backtest import BacktestResultsResponse, SeriesPayload

    fields = BacktestResultsResponse.model_fields
    assert "series" in fields
    assert "series_status" in fields
    # trades removed
    assert "trades" not in fields


def test_backtest_results_response_round_trip_with_series() -> None:
    from msai.schemas.backtest import BacktestResultsResponse
    response = BacktestResultsResponse(
        id=uuid4(),
        metrics={"sharpe_ratio": 1.2, ...},
        trade_count=10,
        series={"daily": [...], "monthly_returns": [...]},
        series_status="ready",
    )
    dumped = response.model_dump(mode="json")
    restored = BacktestResultsResponse.model_validate(dumped)
    assert restored == response


def test_backtest_results_response_accepts_not_materialized() -> None:
    from msai.schemas.backtest import BacktestResultsResponse
    resp = BacktestResultsResponse(
        id=uuid4(),
        metrics=None,
        trade_count=0,
        series=None,
        series_status="not_materialized",
    )
    assert resp.series is None
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/unit/test_backtest_schemas.py::test_backtest_results_response_has_series_and_status -v
```

Expected: FAIL (`trades` still present; `series` missing).

### Step 3: Implement

In `backend/src/msai/schemas/backtest.py`, update `BacktestResultsResponse`:

```python
class BacktestResultsResponse(BaseModel):
    """Response for GET /api/v1/backtests/{id}/results."""

    id: UUID
    metrics: dict[str, Any] | None = None
    trade_count: int
    series: SeriesPayload | None = None
    series_status: SeriesStatus = "not_materialized"
    has_report: bool = False  # true iff Backtest.report_path is populated — gate for iframe tab
```

`has_report: bool` (iter-7 P3 fix) — derived server-side from `Backtest.report_path is not None`. Don't expose the raw internal filesystem path.

**Deferred to v2 (accepted as P3 polish):** the decision doc ("Parity contract" section line 64) calls for a "stale-but-viewable" flag when `series` and the QS HTML diverge (e.g., future QS upgrade changes compounding). v1 ships with `has_report: bool` only — once divergence is observed in practice, add a follow-up `report_stale: bool` field and render a banner in the iframe tab. Not blocking for this PR; logged in the Known-Issues section of CONTINUITY after merge.

### Step 4: Run tests

```bash
cd backend && uv run pytest tests/unit/test_backtest_schemas.py -v
```

### Step 5: Commit

```bash
git add backend/src/msai/schemas/backtest.py backend/tests/unit/test_backtest_schemas.py
git commit -m "feat(api): BacktestResultsResponse adds series + series_status, drops inline trades"
```

---

## Task B7 — Update `/results` handler

**Why:** Wire the new schema to the DB read; populate `series` when `series_status="ready"`; drop the trades DB query + serialization (moves to B8's new endpoint).

**Files:**

- Modify: `backend/src/msai/api/backtests.py::get_backtest_results()` (lines 410–454)
- Test: `backend/tests/unit/test_backtests_api.py` + integration test

### Step 1: Write the failing test

In `backend/tests/integration/test_backtests_api.py` (extend existing):

```python
@pytest.mark.asyncio
async def test_results_returns_series_when_ready(client, seeded_completed_backtest_with_series):
    response = await client.get(f"/api/v1/backtests/{seeded_completed_backtest_with_series.id}/results")
    assert response.status_code == 200
    body = response.json()
    assert body["series_status"] == "ready"
    assert body["series"] is not None
    assert "daily" in body["series"]
    assert "monthly_returns" in body["series"]
    assert "trades" not in body  # removed from /results


@pytest.mark.asyncio
async def test_results_returns_not_materialized_for_legacy(client, seeded_legacy_backtest):
    response = await client.get(f"/api/v1/backtests/{seeded_legacy_backtest.id}/results")
    assert response.status_code == 200
    body = response.json()
    assert body["series_status"] == "not_materialized"
    assert body["series"] is None


@pytest.mark.asyncio
async def test_results_returns_failed_status(client, seeded_backtest_with_failed_series):
    response = await client.get(f"/api/v1/backtests/{seeded_backtest_with_failed_series.id}/results")
    assert response.status_code == 200
    body = response.json()
    assert body["series_status"] == "failed"
    assert body["series"] is None
    assert body["metrics"] is not None  # aggregate metrics still present
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/integration/test_backtests_api.py::test_results_returns_series_when_ready -v
```

Expected: FAIL (handler still returns old shape).

### Step 3: Implement

In `backend/src/msai/api/backtests.py`, replace `get_backtest_results`:

```python
@router.get("/{job_id}/results", response_model=BacktestResultsResponse)
async def get_backtest_results(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestResultsResponse:
    """Return aggregate metrics + canonical series payload + trade count.

    Trades are no longer inline — see GET /api/v1/backtests/{id}/trades.
    """
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()
    if backtest is None:
        # iter-8 P2 fix: use JSONResponse to avoid FastAPI's `{"detail": ...}` wrapping.
        # api-design.md requires top-level `{"error": {...}}`. HTTPException(detail={...})
        # would ship `{"detail":{"error":...}}` — non-compliant. Precedent at backtests.py:55.
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND", "message": f"Backtest {job_id} not found"}},
        )

    trade_count_result = await db.execute(
        select(func.count()).select_from(Trade).where(Trade.backtest_id == job_id)
    )
    trade_count = trade_count_result.scalar_one()

    return BacktestResultsResponse(
        id=backtest.id,
        metrics=backtest.metrics,
        trade_count=trade_count,
        series=backtest.series,
        series_status=backtest.series_status,  # type: ignore[arg-type]  # DB String -> SeriesStatus Literal
        has_report=backtest.report_path is not None,
    )
```

Remove the old trade-serialization block (actual location: `backend/src/msai/api/backtests.py:426-447`).

### Step 4: Run tests

```bash
cd backend && uv run pytest tests/integration/test_backtests_api.py -v -k "results"
```

### Step 5: Commit

```bash
git add backend/src/msai/api/backtests.py backend/tests/integration/test_backtests_api.py
git commit -m "feat(api): /results returns series + series_status; trades moved to /trades"
```

---

## Task B8 — NEW paginated `/trades` endpoint

**Why:** PRD US-004. Individual fills can reach 100k+ per backtest; inlining on `/results` is a browser-lockup risk (Scalability Hawk blocking objection). Sibling endpoint follows existing `.claude/rules/api-design.md` pagination shape `{items, total, page, page_size}`.

**Files:**

- Modify: `backend/src/msai/api/backtests.py` (add new route)
- Modify: `backend/src/msai/schemas/backtest.py` (add `BacktestTradeItem`, `BacktestTradesResponse`)
- Test: `backend/tests/integration/test_backtests_api.py`

### Step 1: Write the failing test

**Multi-query mock pattern (iter-3 P1 fix):** The `/trades` handler issues THREE sequential queries:

1. `select(Backtest.id).where(Backtest.id == job_id)` — existence check (scalar)
2. `select(func.count()).select_from(Trade).where(Trade.backtest_id == job_id)` — total count (scalar)
3. `select(Trade).where(...).order_by(...).offset(...).limit(...)` — paginated rows (scalars)

`_mock_session_returning()` from `test_backtests_api.py:62-99` only supplies one shape. For multi-query tests, build a small helper that wires `session.execute.side_effect` to a list of prepared `MagicMock`s:

```python
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock
import pytest

from msai.core.database import get_db
from msai.main import app
from tests.unit.conftest import _make_backtest_with_trades


def _mock_trades_session(
    backtest_exists: bool,
    total: int,
    rows: list[Trade],
) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    exists_result = MagicMock()
    # Handler checks `scalar_one_or_none() is None` — return the backtest UUID when
    # exists=True and None when exists=False (NOT False — False is not None).
    exists_result.scalar_one_or_none.return_value = (uuid4() if backtest_exists else None)
    count_result = MagicMock()
    count_result.scalar_one.return_value = total
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    session.execute.side_effect = [exists_result, count_result, rows_result]
    return session


def _override_get_db(session: AsyncMock) -> None:
    def _gen() -> Iterator[AsyncMock]:
        yield session
    app.dependency_overrides[get_db] = _gen


@pytest.mark.asyncio
async def test_trades_endpoint_paginates(client) -> None:
    bt, trades = _make_backtest_with_trades(n=150)
    # First page: 100 rows sliced from trades[0:100]
    _override_get_db(_mock_trades_session(backtest_exists=True, total=150, rows=trades[:100]))
    try:
        response = await client.get(f"/api/v1/backtests/{bt.id}/trades?page=1&page_size=100")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 150
        assert body["page"] == 1
        assert body["page_size"] == 100
        assert len(body["items"]) == 100
        assert body["items"][0]["id"] == str(trades[0].id)  # sorted by (executed_at, id) ASC
        item = body["items"][0]
        assert set(item.keys()) >= {"id", "instrument", "side", "quantity", "price", "pnl", "commission", "executed_at"}
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_trades_endpoint_second_page(client) -> None:
    bt, trades = _make_backtest_with_trades(n=150)
    _override_get_db(_mock_trades_session(backtest_exists=True, total=150, rows=trades[100:150]))
    try:
        response = await client.get(f"/api/v1/backtests/{bt.id}/trades?page=2&page_size=100")
        body = response.json()
        assert len(body["items"]) == 50
        assert body["page"] == 2
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_trades_endpoint_empty_beyond_range(client) -> None:
    bt, _ = _make_backtest_with_trades(n=50)
    _override_get_db(_mock_trades_session(backtest_exists=True, total=50, rows=[]))
    try:
        response = await client.get(f"/api/v1/backtests/{bt.id}/trades?page=99&page_size=100")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 50
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_trades_endpoint_clamps_page_size(client) -> None:
    bt, trades = _make_backtest_with_trades(n=10)
    _override_get_db(_mock_trades_session(backtest_exists=True, total=10, rows=trades))
    try:
        response = await client.get(f"/api/v1/backtests/{bt.id}/trades?page=1&page_size=9999")
        body = response.json()
        assert body["page_size"] == 500  # clamped to max
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_trades_endpoint_rejects_zero_page(client) -> None:
    bt, _ = _make_backtest_with_trades(n=1)
    # page=0 fails validation before any session call — no side_effect needed
    _override_get_db(_mock_trades_session(backtest_exists=True, total=1, rows=[]))
    try:
        response = await client.get(f"/api/v1/backtests/{bt.id}/trades?page=0&page_size=100")
        assert response.status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_trades_endpoint_backtest_not_found(client) -> None:
    """404 when backtest doesn't exist — existence query returns None."""
    from uuid import uuid4
    _override_get_db(_mock_trades_session(backtest_exists=False, total=0, rows=[]))
    try:
        response = await client.get(f"/api/v1/backtests/{uuid4()}/trades?page=1&page_size=10")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/integration/test_backtests_api.py -v -k "trades_endpoint"
```

Expected: 404 (endpoint doesn't exist).

### Step 3: Implement the schemas

In `backend/src/msai/schemas/backtest.py`:

```python
from decimal import Decimal


class BacktestTradeItem(BaseModel):
    """One individual Nautilus fill from a backtest."""

    id: UUID
    instrument: str
    side: str  # "BUY" or "SELL"
    quantity: float
    price: float
    pnl: float
    commission: float
    executed_at: datetime


class BacktestTradesResponse(BaseModel):
    """Paginated response for GET /api/v1/backtests/{id}/trades."""

    items: list[BacktestTradeItem]
    total: int
    page: int
    page_size: int
```

### Step 4: Implement the endpoint

In `backend/src/msai/api/backtests.py`:

```python
MAX_TRADE_PAGE_SIZE = 500
DEFAULT_TRADE_PAGE_SIZE = 100


@router.get("/{job_id}/trades", response_model=BacktestTradesResponse)
async def get_backtest_trades(
    job_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_TRADE_PAGE_SIZE, ge=1),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestTradesResponse:
    """Return paginated individual fills for a backtest, sorted by executed_at ASC."""
    # Clamp page_size (don't 422 — follows project convention)
    effective_page_size = min(page_size, MAX_TRADE_PAGE_SIZE)

    # Verify backtest exists (auth-gated via Depends; 404 if not found)
    exists = await db.execute(select(Backtest.id).where(Backtest.id == job_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": f"Backtest {job_id} not found"}},
        )

    total_result = await db.execute(
        select(func.count()).select_from(Trade).where(Trade.backtest_id == job_id)
    )
    total = total_result.scalar_one()

    offset = (page - 1) * effective_page_size
    rows_result = await db.execute(
        select(Trade)
        .where(Trade.backtest_id == job_id)
        .order_by(Trade.executed_at.asc(), Trade.id.asc())  # secondary sort: ties broken by id for deterministic pagination
        .offset(offset)
        .limit(effective_page_size)
    )
    rows = list(rows_result.scalars().all())

    items = [
        BacktestTradeItem(
            id=r.id,
            instrument=r.instrument,
            side=r.side,
            quantity=float(r.quantity),
            price=float(r.price),
            pnl=float(r.pnl) if r.pnl is not None else 0.0,
            commission=float(r.commission) if r.commission is not None else 0.0,
            executed_at=r.executed_at,
        )
        for r in rows
    ]

    return BacktestTradesResponse(
        items=items,
        total=total,
        page=page,
        page_size=effective_page_size,
    )
```

### Step 5: Run tests

```bash
cd backend && uv run pytest tests/integration/test_backtests_api.py -v -k "trades_endpoint"
cd backend && uv run mypy src/msai/api/backtests.py --strict
cd backend && uv run ruff check src/msai/api/backtests.py
```

### Step 6: Commit

```bash
git add backend/src/msai/api/backtests.py backend/src/msai/schemas/backtest.py backend/tests/integration/test_backtests_api.py
git commit -m "feat(api): paginated GET /backtests/{id}/trades endpoint"
```

---

## Task B9 — Payload-size observability

**Why:** Scalability Hawk blocking objection. Catch accidental minute-bar leaks (50 MB JSONB explosion) early via a histogram. Single canonical name per PRD + decision doc: **`msai_backtest_results_payload_bytes`**. Observed at BOTH observation sites (worker-write AND /results response) — they measure the same payload since `series` dominates the response body.

**Files:**

- Modify: `backend/src/msai/services/observability/trading_metrics.py` — register the canonical histogram via `_r.histogram(...)`
- Modify: `backend/src/msai/workers/backtest_job.py` — observe in the caller's series-materialization block (from B5)
- Modify: `backend/src/msai/api/backtests.py` — observe in `/results` handler before return
- Test: `backend/tests/unit/test_metrics.py`

### Step 1: Write failing tests

Test through the public render contract (matches the style at `backend/tests/unit/test_metrics.py:123-171`) — do NOT couple to internal `_count` / `_buckets` attrs.

```python
def test_backtest_results_payload_bytes_histogram_defined() -> None:
    from msai.services.observability.trading_metrics import msai_backtest_results_payload_bytes
    assert msai_backtest_results_payload_bytes is not None


def test_histogram_observe_shows_in_render() -> None:
    from msai.services.observability.trading_metrics import msai_backtest_results_payload_bytes
    msai_backtest_results_payload_bytes.observe(50_000)
    lines = msai_backtest_results_payload_bytes.render()
    text = "\n".join(lines)
    assert "msai_backtest_results_payload_bytes_bucket" in text
    assert 'le="102400"' in text  # 100 KB bucket should receive the 50_000 observation
    assert "msai_backtest_results_payload_bytes_count" in text
    assert "msai_backtest_results_payload_bytes_sum" in text


def test_histogram_registered_via_global_registry() -> None:
    from msai.services.observability import get_registry
    registry = get_registry()
    render_text = registry.render()
    # Exposition contract: HELP line must exist in the global registry render
    assert "# HELP msai_backtest_results_payload_bytes" in render_text
    assert "# TYPE msai_backtest_results_payload_bytes histogram" in render_text
```

### Step 2: Run tests

Expected: FAIL (histogram not defined yet in this module).

### Step 3: Implement

In `backend/src/msai/services/observability/trading_metrics.py`, alongside existing counter/gauge registrations:

```python
msai_backtest_results_payload_bytes = _r.histogram(
    "msai_backtest_results_payload_bytes",
    "Size in bytes of the Backtest.series JSONB payload (observed at worker-write + /results response).",
    buckets=(1_024, 10_240, 102_400, 1_048_576, 10_485_760),  # 1 KB, 10 KB, 100 KB, 1 MB, 10 MB
)

# Request profiling for the paginated /trades endpoint — labeled by page_size
# so SRE can spot abuse or clients accidentally requesting huge pages.
msai_backtest_trades_page_count = _r.counter(
    "msai_backtest_trades_page_count",
    "Count of GET /api/v1/backtests/{id}/trades requests, labeled by page_size.",
)
```

In `backend/src/msai/api/backtests.py::get_backtest_trades` (inside the handler, after `effective_page_size` is computed, before the DB queries):

```python
from msai.services.observability.trading_metrics import msai_backtest_trades_page_count

msai_backtest_trades_page_count.labels(page_size=str(effective_page_size)).inc()
```

In `backend/src/msai/workers/backtest_job.py` (inside the B5 caller block, replace the TODO-histogram comment):

```python
from msai.services.observability.trading_metrics import msai_backtest_results_payload_bytes

# inside the try block in _execute_backtest, right after payload_bytes computed:
msai_backtest_results_payload_bytes.observe(payload_bytes)
```

In `backend/src/msai/api/backtests.py::get_backtest_results` (before `return`):

```python
import json as _json
from msai.services.observability.trading_metrics import msai_backtest_results_payload_bytes

response = BacktestResultsResponse(...)
msai_backtest_results_payload_bytes.observe(
    len(_json.dumps(response.model_dump(mode="json")).encode("utf-8"))
)
return response
```

### Step 4: Run tests + existing regression

```bash
cd backend && uv run pytest tests/unit/test_metrics.py -v
```

### Step 5: Commit

```bash
git add backend/src/msai/services/observability/trading_metrics.py backend/src/msai/workers/backtest_job.py backend/src/msai/api/backtests.py backend/tests/unit/test_metrics.py
git commit -m "feat(observability): series + /results payload-size histograms"
```

---

## Task B10 — Signed-URL machinery for `/report` (iframe auth without proxy)

**Why (iter-9 rework):** The previous Next.js-proxy-with-server-side-`MSAI_API_KEY` approach was an auth bypass — anyone reaching the frontend origin + guessing a UUID got the report. Since msai-v2's roadmap is "each VM becomes a service," we adopt the stateless signed-URL pattern used by AWS S3 pre-signed URLs / Azure SAS / Cloudflare Access: backend mints a short-lived HMAC-signed URL for a specific `(backtest_id, user_sub)`; iframe `src` uses that signed URL; backend validates signature + expiry.

**This task replaces the previous B10 (docker-compose `MSAI_API_KEY` env) and restructures Task F2 into a signed-URL client-side fetch** — no Next.js Route Handler needed.

**Files:**

- Create: `backend/src/msai/services/report_signer.py` — HMAC signer + verifier
- Modify: `backend/src/msai/core/config.py` — add `report_signing_secret: str`
- Modify: `backend/src/msai/api/backtests.py` — add `POST /{id}/report-token` route + extend `GET /{id}/report` to accept `?token=`
- Modify: `backend/src/msai/schemas/backtest.py` — add `BacktestReportTokenResponse`
- Modify: `.env.example` — document `REPORT_SIGNING_SECRET`
- Test: `backend/tests/unit/test_report_signer.py` (new), `backend/tests/integration/test_backtests_api.py` (extend)

### Step 1: Write failing tests

In `backend/tests/unit/test_report_signer.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from msai.services.report_signer import (
    InvalidReportTokenError,
    sign_report_token,
    verify_report_token,
)


def test_sign_then_verify_roundtrip() -> None:
    backtest_id = uuid4()
    user_sub = "test-user"
    expires_at = datetime.now(UTC) + timedelta(seconds=60)
    token = sign_report_token(
        backtest_id=backtest_id,
        user_sub=user_sub,
        expires_at=expires_at,
        secret="test-secret",
    )
    # Must roundtrip cleanly
    claims = verify_report_token(token, backtest_id=backtest_id, secret="test-secret")
    assert claims.backtest_id == backtest_id
    assert claims.user_sub == user_sub


def test_verify_rejects_expired_token() -> None:
    backtest_id = uuid4()
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    token = sign_report_token(
        backtest_id=backtest_id,
        user_sub="u",
        expires_at=expired_at,
        secret="test-secret",
    )
    with pytest.raises(InvalidReportTokenError, match="expired"):
        verify_report_token(token, backtest_id=backtest_id, secret="test-secret")


def test_verify_rejects_tampered_payload() -> None:
    """A token minted for backtest A must not unlock backtest B."""
    bt_a, bt_b = uuid4(), uuid4()
    token = sign_report_token(
        backtest_id=bt_a,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="s",
    )
    with pytest.raises(InvalidReportTokenError):
        verify_report_token(token, backtest_id=bt_b, secret="s")


def test_verify_rejects_wrong_secret() -> None:
    """A token signed with secret A must not validate under secret B."""
    bt = uuid4()
    token = sign_report_token(
        backtest_id=bt,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="secret-a",
    )
    with pytest.raises(InvalidReportTokenError, match="signature"):
        verify_report_token(token, backtest_id=bt, secret="secret-b")


def test_verify_rejects_garbage_token() -> None:
    with pytest.raises(InvalidReportTokenError):
        verify_report_token("not.a.token", backtest_id=uuid4(), secret="s")
```

### Step 2: Run tests to verify they fail

```bash
cd backend && uv run pytest tests/unit/test_report_signer.py -v
```

Expected: FAIL (module doesn't exist).

### Step 3: Implement the signer

Create `backend/src/msai/services/report_signer.py`:

```python
"""HMAC signer + verifier for short-lived report URLs.

Pattern adopted from S3 pre-signed URLs / Azure SAS / Cloudflare signed tokens.
Stateless: the backend mints a token that carries its own scope (backtest_id,
user_sub) and expiry; verification is a pure-function HMAC check. No session
store, no cookies, no cross-service SSO.

Token format: ``<base64url(payload_json)>.<base64url(hmac_sha256_hex)>``

Intentionally NOT using JWT because we don't need the full JWS/JWT claim-set
machinery and JWT libraries add a large dependency surface. 40 lines of HMAC
is sufficient.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


class InvalidReportTokenError(ValueError):
    """Raised when a token is expired, tampered, malformed, or signed with a different secret."""


@dataclass(frozen=True, slots=True)
class ReportTokenClaims:
    backtest_id: UUID
    user_sub: str
    expires_at: datetime


def sign_report_token(
    *,
    backtest_id: UUID,
    user_sub: str,
    expires_at: datetime,
    secret: str,
) -> str:
    payload = {
        "backtest_id": str(backtest_id),
        "user_sub": user_sub,
        "exp": int(expires_at.timestamp()),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_report_token(
    token: str,
    *,
    backtest_id: UUID,
    secret: str,
) -> ReportTokenClaims:
    """Validate signature, expiry, and backtest_id match. Returns claims or raises."""
    try:
        payload_b64, sig_hex = token.split(".", 1)
    except ValueError as e:
        raise InvalidReportTokenError("malformed token") from e

    expected_sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_hex):
        raise InvalidReportTokenError("invalid signature")

    try:
        # Re-pad for base64 decode
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload: dict[str, Any] = json.loads(payload_bytes)
    except Exception as e:  # noqa: BLE001 — catch-all on malformed base64 or JSON
        raise InvalidReportTokenError("malformed payload") from e

    now = datetime.now(UTC)
    try:
        exp = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        token_backtest_id = UUID(payload["backtest_id"])
        user_sub = str(payload["user_sub"])
    except (KeyError, ValueError, TypeError) as e:
        raise InvalidReportTokenError("missing or invalid claim") from e

    if now >= exp:
        raise InvalidReportTokenError("token expired")
    if token_backtest_id != backtest_id:
        raise InvalidReportTokenError("backtest_id mismatch")

    return ReportTokenClaims(
        backtest_id=token_backtest_id,
        user_sub=user_sub,
        expires_at=exp,
    )
```

### Step 4: Wire config

In `backend/src/msai/core/config.py`, add to `Settings`:

```python
report_signing_secret: str = Field(
    default="dev-report-signing-secret-change-in-prod",
    description="HMAC secret for /backtests/{id}/report signed URLs. Must be rotated per deploy.",
)
report_token_ttl_seconds: int = Field(
    default=60,
    description="TTL for signed report URLs. Short by design — browser-history leakage harmless after expiry.",
)
```

### Step 5: Add `POST /{id}/report-token` + extend `GET /report`

In `backend/src/msai/schemas/backtest.py`:

```python
class BacktestReportTokenResponse(BaseModel):
    """Response for POST /api/v1/backtests/{id}/report-token."""

    signed_url: str  # absolute path starting with /api/v1/...
    expires_at: datetime
```

In `backend/src/msai/api/backtests.py`:

```python
from datetime import UTC, datetime, timedelta
from msai.services.report_signer import (
    InvalidReportTokenError,
    sign_report_token,
    verify_report_token,
)


@router.post(
    "/{job_id}/report-token",
    response_model=BacktestReportTokenResponse,
)
async def mint_backtest_report_token(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestReportTokenResponse:
    """Mint a short-lived signed URL for the report iframe.

    The returned URL contains an HMAC-signed token bound to (backtest_id, user_sub, expires_at).
    The iframe uses it as its `src`. Expires in settings.report_token_ttl_seconds (default 60s).
    """
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()
    if backtest is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NOT_FOUND", "message": f"Backtest {job_id} not found"}},
        )
    if backtest.report_path is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "NO_REPORT", "message": "Report not yet generated"}},
        )

    user_sub = claims.get("sub", "unknown")
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.report_token_ttl_seconds)
    token = sign_report_token(
        backtest_id=job_id,
        user_sub=user_sub,
        expires_at=expires_at,
        secret=settings.report_signing_secret,
    )
    return BacktestReportTokenResponse(
        signed_url=f"/api/v1/backtests/{job_id}/report?token={token}",
        expires_at=expires_at,
    )
```

Extend the existing `GET /{job_id}/report` handler to accept `?token=` as an alternative to Bearer/X-API-Key auth:

```python
@router.get("/{job_id}/report")
async def get_backtest_report(
    job_id: UUID,
    token: str | None = None,  # NEW query param — short-lived signed URL
    claims: dict[str, Any] | None = Depends(get_current_user_or_none),  # allow unauth if token present
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Stream the QuantStats HTML report. Auth via Bearer/X-API-Key OR a valid ?token=<signed>."""
    if token is not None:
        try:
            verify_report_token(
                token,
                backtest_id=job_id,
                secret=settings.report_signing_secret,
            )
        except InvalidReportTokenError as e:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "INVALID_TOKEN", "message": str(e)}},
            )
    elif claims is None:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "UNAUTHENTICATED", "message": "Missing auth"}},
        )

    # ... existing FileResponse logic returning backtest.report_path ...
```

The `get_current_user_or_none` helper is a small variant of `get_current_user` that returns `None` instead of raising 401 when no auth header is present — so the handler can decide whether token-auth is sufficient. Add it to `backend/src/msai/core/auth.py`:

```python
async def get_current_user_or_none(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, Any] | None:
    """Same as get_current_user but returns None instead of raising 401 on missing auth.

    Use ONLY when the caller has a fallback auth path (e.g., a signed token in query string).
    """
    api_key = request.headers.get("X-API-Key")
    if api_key is None and credentials is None:
        return None
    try:
        return await get_current_user(request=request, credentials=credentials)
    except HTTPException:
        return None
```

### Step 6: Integration test

In `backend/tests/integration/test_backtests_api.py`:

```python
@pytest.mark.asyncio
async def test_report_token_endpoint_returns_signed_url(client) -> None:
    bt = _make_backtest_completed_with_series()
    bt.report_path = "/tmp/r.html"
    session = _mock_session_returning(bt)
    _override_get_db(session)
    try:
        response = await client.post(f"/api/v1/backtests/{bt.id}/report-token")
        assert response.status_code == 200
        body = response.json()
        assert body["signed_url"].startswith(f"/api/v1/backtests/{bt.id}/report?token=")
        assert "expires_at" in body
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_report_endpoint_accepts_valid_token(client, tmp_path) -> None:
    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token
    from datetime import UTC, datetime, timedelta

    bt = _make_backtest_completed_with_series()
    report_file = tmp_path / "r.html"
    report_file.write_text("<html>tearsheet</html>")
    bt.report_path = str(report_file)

    token = sign_report_token(
        backtest_id=bt.id,
        user_sub="test-user",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret=settings.report_signing_secret,
    )

    session = _mock_session_returning(bt)
    _override_get_db(session)
    try:
        # Disable the autouse auth override so we exercise the unauth+token path
        app.dependency_overrides.pop(get_current_user, None)
        response = await client.get(
            f"/api/v1/backtests/{bt.id}/report?token={token}"
        )
        assert response.status_code == 200
        assert "<html>" in response.text
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_report_endpoint_rejects_expired_token(client) -> None:
    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token
    from datetime import UTC, datetime, timedelta

    bt = _make_backtest_completed_with_series()
    token = sign_report_token(
        backtest_id=bt.id,
        user_sub="u",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),  # expired
        secret=settings.report_signing_secret,
    )
    session = _mock_session_returning(bt)
    _override_get_db(session)
    try:
        app.dependency_overrides.pop(get_current_user, None)
        response = await client.get(f"/api/v1/backtests/{bt.id}/report?token={token}")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_TOKEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_report_endpoint_rejects_cross_backtest_token(client) -> None:
    """A token minted for A must not unlock B."""
    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    bt_a = uuid4()
    bt_b = _make_backtest_completed_with_series()  # serves for B
    token_for_a = sign_report_token(
        backtest_id=bt_a,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret=settings.report_signing_secret,
    )
    session = _mock_session_returning(bt_b)
    _override_get_db(session)
    try:
        app.dependency_overrides.pop(get_current_user, None)
        response = await client.get(f"/api/v1/backtests/{bt_b.id}/report?token={token_for_a}")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)
```

### Step 7: Document env var

Append to `.env.example`:

```bash
# HMAC secret for signed /report URLs. Rotate on every production deploy.
# Must be a cryptographically random string (openssl rand -base64 48 is fine).
REPORT_SIGNING_SECRET=dev-report-signing-secret-change-in-prod
```

### Step 8: Run tests + commit

```bash
cd backend && uv run pytest tests/unit/test_report_signer.py tests/integration/test_backtests_api.py -v -k "report"
cd backend && uv run mypy src/msai/services/report_signer.py src/msai/core/config.py src/msai/api/backtests.py --strict
cd backend && uv run ruff check src/msai/services/report_signer.py src/msai/core/auth.py
```

```bash
git add backend/src/msai/services/report_signer.py backend/src/msai/core/config.py backend/src/msai/core/auth.py backend/src/msai/api/backtests.py backend/src/msai/schemas/backtest.py backend/tests/unit/test_report_signer.py backend/tests/integration/test_backtests_api.py .env.example
git commit -m "feat(api): signed-URL auth for /report (replaces unsafe server-side-key iframe proxy)"
```

---

## Task F1 — TypeScript types update

**Why:** Frontend API client and UI components need types matching backend B6/B8 contracts. Also fix the pre-existing wrong `BacktestTradeItem` shape (currently expects entry/exit pairs).

**Files:**

- Modify: `frontend/src/lib/api.ts`
- Test: frontend types compile; add unit tests if project has them (check `frontend/src/**/__tests__/`)

### Step 1: Write failing compile/type check

Run `cd frontend && pnpm exec tsc --noEmit` — should compile clean now; after updating types, `<ResultsCharts>` and `<TradeLog>` will error until F4/F6 land (that's OK — we land them back-to-back).

### Step 2: Update types

In `frontend/src/lib/api.ts`, add:

```typescript
export type SeriesStatus = "ready" | "not_materialized" | "failed";

export interface SeriesDailyPoint {
  date: string; // YYYY-MM-DD
  equity: number;
  drawdown: number; // ≤ 0
  daily_return: number;
}

export interface SeriesMonthlyReturn {
  month: string; // YYYY-MM
  pct: number;
}

export interface SeriesPayload {
  daily: SeriesDailyPoint[];
  monthly_returns: SeriesMonthlyReturn[];
}
```

Update `BacktestResultsResponse`:

```typescript
export interface BacktestResultsResponse {
  id: string;
  metrics: BacktestMetrics | null;
  trade_count: number;
  series: SeriesPayload | null;
  series_status: SeriesStatus;
  has_report: boolean; // true when the "Full report" iframe tab should be enabled
  // trades removed — use getBacktestTrades()
}
```

Replace the old `BacktestTradeItem`:

```typescript
export interface BacktestTradeItem {
  id: string;
  instrument: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  pnl: number;
  commission: number;
  executed_at: string; // ISO datetime
}

export interface BacktestTradesResponse {
  items: BacktestTradeItem[];
  total: number;
  page: number;
  page_size: number;
}

export async function getBacktestTrades(
  id: string,
  params: { page: number; page_size?: number },
  token?: string | null,
): Promise<BacktestTradesResponse> {
  const q = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size ?? 100),
  });
  return apiGet<BacktestTradesResponse>(
    `/api/v1/backtests/${encodeURIComponent(id)}/trades?${q}`,
    token,
  );
}
```

### Step 3: Verify types compile in isolation

```bash
cd frontend && pnpm exec tsc --noEmit --project tsconfig.json 2>&1 | head -30
```

Will show errors in `results-charts.tsx` / `trade-log.tsx` until F4/F6 — expected.

### Step 4: Commit

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(types): SeriesPayload, SeriesStatus, BacktestTradeItem match backend"
```

---

## Task F2 — Signed-URL iframe (iter-9 rework)

**Why (iter-9 revision):** The original plan proxied through a Next.js Route Handler using a new SERVER-SIDE `MSAI_API_KEY` env var on the frontend container. Codex flagged that as an auth bypass — anyone who could reach the frontend origin and guess a backtest UUID got the report. Replaced by the signed-URL flow from Task B10: backend mints a per-user-per-backtest HMAC URL that expires in 60 seconds; iframe uses it directly as `src`. No Next.js proxy. The SERVER-SIDE `MSAI_API_KEY` on the frontend container is NOT added.

> **Terminology note (addresses iter-10 P1 confusion):** `NEXT_PUBLIC_MSAI_API_KEY` is a PRE-EXISTING client-visible dev convenience used by `lib/api.ts` for REST auth fallback and `lib/use-live-stream.ts` for WebSocket auth. That remains as-is — it is NOT the key this iter-9 rework removed. The removed key was the hypothetical SERVER-ONLY `MSAI_API_KEY` that the Next.js Route Handler would have added. Two different keys, two different surfaces.

**Files:**

- Modify: `frontend/src/lib/api.ts` — add `getBacktestReportToken(id, token)` client function
- Modify: `frontend/src/components/backtests/report-iframe.tsx` — fetch signed URL, use as iframe `src`
- **Delete (if exists from earlier iteration):** `frontend/src/app/api/backtests/[id]/report/route.ts`
- Test: verified through UC-BRC-005 E2E use case (rewritten below)

### Step 1: Add the client function

In `frontend/src/lib/api.ts`:

```typescript
export interface BacktestReportTokenResponse {
  signed_url: string;
  expires_at: string; // ISO datetime
}

export async function getBacktestReportToken(
  id: string,
  token?: string | null,
): Promise<BacktestReportTokenResponse> {
  return apiPost<BacktestReportTokenResponse>(
    `/api/v1/backtests/${encodeURIComponent(id)}/report-token`,
    undefined,
    token,
  );
}
```

(`apiPost` is the existing POST helper in `frontend/src/lib/api.ts`; use whatever the project calls it. The call is a plain authenticated POST with an empty body.)

### Step 2: Rewrite `<ReportIframe>` to use the signed URL

`frontend/src/components/backtests/report-iframe.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { getBacktestReportToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface ReportIframeProps {
  backtestId: string;
  hasReport: boolean;
}

export function ReportIframe({
  backtestId,
  hasReport,
}: ReportIframeProps): JSX.Element {
  const { getToken } = useAuth();
  const [signedUrl, setSignedUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!hasReport) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const token = await getToken();
        const res = await getBacktestReportToken(backtestId, token);
        if (cancelled) return;
        setSignedUrl(res.signed_url);
      } catch (e: unknown) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "Failed to load report");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Re-fetch when tab is re-opened (component mounts) so expired tokens auto-refresh.
  }, [backtestId, hasReport, getToken]);

  if (!hasReport) {
    return (
      <Card className="border-border/50">
        <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-muted-foreground">
          <AlertCircle className="h-8 w-8" />
          <p className="text-sm">
            Full report not available for this backtest.
          </p>
          <p className="text-xs">
            Switch to Native view to see populated charts.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return (
      <div className="flex h-[900px] items-center justify-center rounded-lg border border-border/50">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error || !signedUrl) {
    return (
      <Card className="border-destructive/50">
        <CardContent className="flex flex-col items-center justify-center gap-3 py-12 text-destructive">
          <AlertCircle className="h-8 w-8" />
          <p className="text-sm">
            Unable to load report: {error ?? "unknown error"}
          </p>
          <p className="text-xs text-muted-foreground">
            Try refreshing the page.
          </p>
        </CardContent>
      </Card>
    );
  }

  // Origin-qualify the signed URL: backend returns a path
  // (`/api/v1/backtests/{id}/report?token=...`). In split-origin setups the
  // iframe would otherwise resolve against the frontend origin and miss the
  // backend entirely. Prepend NEXT_PUBLIC_API_URL (same base `apiFetch` uses).
  const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  const absoluteSignedUrl = signedUrl.startsWith("http")
    ? signedUrl
    : `${API_BASE}${signedUrl}`;

  return (
    <div className="relative h-[900px] w-full overflow-hidden rounded-lg border border-border/50">
      <iframe
        src={absoluteSignedUrl}
        className="h-full w-full"
        title="QuantStats tear sheet"
        // Sandbox: allow scripts (Plotly inline); same-origin needed for Plotly internals.
        // The combination effectively neutralizes sandbox but is required for QS output.
        sandbox="allow-scripts allow-same-origin"
      />
    </div>
  );
}
```

### Step 3: Delete the obsolete Next.js Route Handler

If an earlier iteration of this plan created `frontend/src/app/api/backtests/[id]/report/route.ts`, delete it now:

```bash
rm -f frontend/src/app/api/backtests/\[id\]/report/route.ts
# Also drop the empty directory if applicable:
rmdir -p frontend/src/app/api/backtests/\[id\]/report 2>/dev/null || true
```

### Step 4: Type check + dev smoke

```bash
cd frontend && pnpm exec tsc --noEmit
# Browser: /backtests/{id} → Full report tab → iframe loads with ?token=... URL
# Inspect Network tab: iframe src is /api/v1/backtests/{id}/report?token=<hmac>
# Verify no SERVER-SIDE `MSAI_API_KEY` (non-prefixed) leaks to browser bundle.
# NOTE: `NEXT_PUBLIC_MSAI_API_KEY` is a PRE-EXISTING client-visible dev convenience
# used by `lib/api.ts` for REST auth + `lib/use-live-stream.ts` for WS auth — that
# value IS intended to appear in the browser bundle (dev only; prod uses Entra SSO).
# The iter-9 rework specifically removed the SERVER-ONLY `MSAI_API_KEY` that would
# have been added for the now-dropped Next.js iframe proxy.
grep -r "[^_]MSAI_API_KEY" frontend/.next/static/ 2>/dev/null | grep -v NEXT_PUBLIC_ | head  # should be empty
```

### Step 5: Commit

```bash
git add frontend/src/lib/api.ts frontend/src/components/backtests/report-iframe.tsx
git rm -f frontend/src/app/api/backtests/\[id\]/report/route.ts 2>/dev/null || true
git commit -m "feat(frontend): signed-URL iframe (drops unsafe server-side-key proxy)"
```

### Step 6: Implementation (OBSOLETE — skip this section)

<details>
<summary>Historical content (dropped iter-9) — kept for forensic reference</summary>

Create `frontend/src/app/api/backtests/[id]/report/route.ts`:

```typescript
// Server-side proxy for the QuantStats HTML report iframe.
// Authenticates upstream with MSAI_API_KEY (server-only env, never NEXT_PUBLIC_*).
// Streams the response body so 5 MB HTML files don't buffer in memory.

import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.MSAI_BACKEND_URL ?? "http://backend:8000";
const API_KEY = process.env.MSAI_API_KEY;

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  if (!API_KEY) {
    return new Response(
      JSON.stringify({
        error: {
          code: "MISCONFIGURED",
          message: "MSAI_API_KEY not set on frontend",
        },
      }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    );
  }

  const { id } = await params;
  const upstream = await fetch(
    `${BACKEND_URL}/api/v1/backtests/${encodeURIComponent(id)}/report`,
    {
      headers: { "X-API-Key": API_KEY },
      cache: "no-store",
    },
  );

  if (!upstream.ok) {
    return new Response(
      JSON.stringify({
        error: {
          code: upstream.status === 404 ? "NOT_FOUND" : "UPSTREAM_ERROR",
          message: `Upstream /report returned ${upstream.status}`,
        },
      }),
      {
        status: upstream.status,
        headers: { "Content-Type": "application/json" },
      },
    );
  }

  // Stream the HTML body straight through. No buffering. Pattern A per research.
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "text/html",
      // Content-Length preserved if upstream set it; otherwise chunked transfer.
      ...(upstream.headers.get("Content-Length")
        ? { "Content-Length": upstream.headers.get("Content-Length")! }
        : {}),
      // Cache-Control: HTML is stable per backtest-id; aggressive cache fine for authenticated users.
      "Cache-Control": "private, max-age=300",
    },
  });
}
```

### Step 2: Verify with a completed backtest

```bash
# In the dev stack, pick a backtest with a report file:
BT_ID=$(docker compose -f docker-compose.dev.yml exec -T postgres \
  psql -U msai -d msai -tAc "SELECT id FROM backtests WHERE report_path IS NOT NULL LIMIT 1")
# Hit the Next.js proxy route:
curl -sI "http://localhost:3300/api/backtests/${BT_ID}/report" | head -5
```

Expected: `HTTP/1.1 200 OK`, `Content-Type: text/html`, no auth error.

### Step 3: Commit

```bash
git add frontend/src/app/api/backtests/[id]/report/route.ts
git commit -m "feat(frontend): Next.js route handler proxies /report to iframe with server-side auth"
```

</details>

---

## Task F3 — Detail page: Tabs wrapper (Native view / Full report)

**Why:** PRD US-001 + US-002. Add shadcn `<Tabs>` container above the chart grid so the iframe gets its own discoverable section without cluttering the default view.

**Files:**

- Modify: `frontend/src/app/backtests/[id]/page.tsx`
- Test: Playwright spec (Phase 6.2c, if framework)

### Step 1: Identify the insertion point

In `frontend/src/app/backtests/[id]/page.tsx` around line 290–310, find where `<ResultsCharts>` and `<TradeLog>` are rendered. Wrap them in:

```tsx
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";

// ... inside the component body, at the chart grid section:
<Tabs defaultValue="native" className="mt-6">
  <TabsList>
    <TabsTrigger value="native">Native view</TabsTrigger>
    <TabsTrigger value="full_report" disabled={!results?.has_report}>
      Full report
    </TabsTrigger>
  </TabsList>
  <TabsContent value="native" className="space-y-6">
    <ResultsCharts
      backtest={backtestForCharts}
      series={results?.series ?? null}
      seriesStatus={results?.series_status ?? "not_materialized"}
    />
    <TradeLog backtestId={id} />
  </TabsContent>
  <TabsContent value="full_report">
    <ReportIframe backtestId={id} hasReport={Boolean(results?.has_report)} />
  </TabsContent>
</Tabs>;
```

Remove the old `equityCurve: []` hardcode (line ~203) and the `<TradeLog trades={[]} />` hardcode (line ~309).

### Step 2: `<ReportIframe>` — see Task F2

The `<ReportIframe>` component is implemented in **Task F2** (signed-URL rewrite). Do not re-implement it here. This step exists only to document that the detail page imports `ReportIframe` from `@/components/backtests/report-iframe` and passes `backtestId` + `hasReport` props as shown in Step 1 above.

<details>
<summary>Historical content (pre-iter-9 — dropped; had unsafe proxy URL)</summary>

The original F3 Step 2 created `<ReportIframe>` here with `src={`/api/backtests/...`}` pointing at the Next.js Route Handler. Both the component implementation AND the iframe src construction moved to F2 after the iter-9 auth-bypass fix. See Task F2 for the canonical implementation.

</details>

### Step 3: Type check + dev smoke

```bash
cd frontend && pnpm exec tsc --noEmit
cd frontend && pnpm build
# Open http://localhost:3300/backtests/<id> — click "Full report" tab → iframe renders
```

### Step 4: Commit

```bash
git add frontend/src/app/backtests/[id]/page.tsx frontend/src/components/backtests/report-iframe.tsx
git commit -m "feat(frontend): Tabs container with Native view + Full report iframe"
```

---

## Task F4 — Wire `<EquityCurveChart>` + `<DrawdownChart>` to real data

**Why:** PRD US-001. `series.daily[]` contains `{date, equity, drawdown, daily_return}`; Recharts maps them directly.

**Files:**

- Modify: `frontend/src/components/backtests/results-charts.tsx`

### Step 1: Update the component signature

```tsx
import type { SeriesPayload, SeriesStatus } from "@/lib/api";

interface ResultsChartsProps {
  backtest: ResultsChartsBacktest;
  series: SeriesPayload | null;
  seriesStatus: SeriesStatus;
}

export function ResultsCharts({
  backtest,
  series,
  seriesStatus,
}: ResultsChartsProps): JSX.Element {
  // metrics cards render as before (from backtest.metrics)
  // chart data:
  const daily = series?.daily ?? [];

  return (
    <>
      {/* Metric cards (unchanged) */}
      {/* ... */}

      {/* Equity Curve */}
      <Card>
        <CardHeader>
          <CardTitle>Equity Curve</CardTitle>
          <CardDescription>
            Cumulative strategy equity over the backtest window
          </CardDescription>
        </CardHeader>
        <CardContent>
          {seriesStatus === "ready" && daily.length > 0 ? (
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={daily}>
                <defs>
                  <linearGradient
                    id="equityGradient"
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop
                      offset="5%"
                      stopColor="oklch(0.6 0.18 250)"
                      stopOpacity={0.6}
                    />
                    <stop
                      offset="95%"
                      stopColor="oklch(0.6 0.18 250)"
                      stopOpacity={0.0}
                    />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" stroke="oklch(0.55 0 0)" />
                <YAxis stroke="oklch(0.55 0 0)" />
                <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.25 0 0)" />
                <Tooltip
                  contentStyle={{
                    background: "oklch(0.15 0 0)",
                    border: "1px solid oklch(0.25 0 0)",
                  }}
                  formatter={(val: number) => [
                    `$${val.toLocaleString()}`,
                    "Equity",
                  ]}
                />
                <Area
                  type="monotone"
                  dataKey="equity"
                  stroke="oklch(0.6 0.18 250)"
                  fill="url(#equityGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <SeriesStatusIndicator status={seriesStatus} />
          )}
        </CardContent>
      </Card>

      {/* Drawdown — mirror structure */}
      <Card>
        <CardHeader>
          <CardTitle>Drawdown</CardTitle>
          <CardDescription>
            Peak-to-trough decline over the backtest window
          </CardDescription>
        </CardHeader>
        <CardContent>
          {seriesStatus === "ready" && daily.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <AreaChart data={daily}>
                <defs>
                  <linearGradient
                    id="drawdownGradient"
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop
                      offset="5%"
                      stopColor="oklch(0.6 0.2 25)"
                      stopOpacity={0.6}
                    />
                    <stop
                      offset="95%"
                      stopColor="oklch(0.6 0.2 25)"
                      stopOpacity={0.0}
                    />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" stroke="oklch(0.55 0 0)" />
                <YAxis
                  stroke="oklch(0.55 0 0)"
                  tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`}
                />
                <CartesianGrid strokeDasharray="3 3" stroke="oklch(0.25 0 0)" />
                <Tooltip
                  formatter={(val: number) => [
                    `${(val * 100).toFixed(2)}%`,
                    "Drawdown",
                  ]}
                  contentStyle={{
                    background: "oklch(0.15 0 0)",
                    border: "1px solid oklch(0.25 0 0)",
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="drawdown"
                  stroke="oklch(0.6 0.2 25)"
                  fill="url(#drawdownGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <SeriesStatusIndicator status={seriesStatus} />
          )}
        </CardContent>
      </Card>

      {/* Monthly heatmap — built in F5 */}
    </>
  );
}
```

### Step 2: Update the detail page call site

`frontend/src/app/backtests/[id]/page.tsx`:

```tsx
<ResultsCharts
  backtest={backtestForCharts}
  series={results?.series ?? null}
  seriesStatus={results?.series_status ?? "not_materialized"}
/>
```

### Step 3: Type check + dev smoke

```bash
cd frontend && pnpm exec tsc --noEmit
# Browser: open /backtests/<completed_id> — equity + drawdown populated.
```

### Step 4: Commit

```bash
git add frontend/src/components/backtests/results-charts.tsx frontend/src/app/backtests/[id]/page.tsx
git commit -m "feat(frontend): wire equity + drawdown charts to series.daily"
```

---

## Task F5 — Build `<MonthlyReturnsHeatmap>` native component

**Why:** PRD US-001. Research finding #5: Recharts has no heatmap primitive; build with CSS Grid + Tailwind oklch, ~60 LOC.

**Files:**

- Modify: `frontend/src/components/backtests/results-charts.tsx` (or extract to `monthly-returns-heatmap.tsx` if >80 LOC)

### Step 1: Implement

```tsx
interface MonthlyReturnsHeatmapProps {
  monthly: SeriesMonthlyReturn[];
}

function MonthlyReturnsHeatmap({
  monthly,
}: MonthlyReturnsHeatmapProps): JSX.Element {
  if (monthly.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No monthly data available.
      </p>
    );
  }

  // Pivot to years × months grid
  const byYear = new Map<string, Map<string, number>>();
  for (const { month, pct } of monthly) {
    const [yr, mo] = month.split("-");
    if (!byYear.has(yr)) byYear.set(yr, new Map());
    byYear.get(yr)!.set(mo, pct);
  }
  const years = Array.from(byYear.keys()).sort();
  const monthLabels = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];

  // Color mapping: negative = red, positive = green, intensity ∝ |pct|
  const cellColor = (pct: number | undefined): string => {
    if (pct === undefined) return "oklch(0.18 0 0)"; // empty month
    const hue = pct >= 0 ? 145 : 25; // green vs red
    const chroma = Math.min(0.25, Math.abs(pct) * 2); // scale intensity
    const lightness = 0.45 + Math.min(0.15, Math.abs(pct) * 1.5);
    return `oklch(${lightness} ${chroma} ${hue})`;
  };

  return (
    <div className="overflow-x-auto">
      <div
        className="grid gap-1 text-xs"
        style={{ gridTemplateColumns: `auto repeat(12, minmax(2.5rem, 1fr))` }}
      >
        {/* Header row */}
        <div />
        {monthLabels.map((m) => (
          <div key={m} className="text-center text-muted-foreground">
            {m}
          </div>
        ))}
        {/* Data rows */}
        {years.map((yr) => (
          <FragmentKey key={yr}>
            <div className="flex items-center pr-2 text-muted-foreground">
              {yr}
            </div>
            {monthLabels.map((_, idx) => {
              const moKey = String(idx + 1).padStart(2, "0");
              const pct = byYear.get(yr)?.get(moKey);
              return (
                <div
                  key={moKey}
                  className="flex h-8 items-center justify-center rounded text-[10px] font-medium"
                  style={{ backgroundColor: cellColor(pct) }}
                  title={
                    pct !== undefined
                      ? `${yr}-${moKey}: ${(pct * 100).toFixed(2)}%`
                      : "No data"
                  }
                >
                  {pct !== undefined ? `${(pct * 100).toFixed(1)}` : ""}
                </div>
              );
            })}
          </FragmentKey>
        ))}
      </div>
    </div>
  );
}

// Tiny helper for React fragments with key
function FragmentKey({ children }: { children: React.ReactNode }): JSX.Element {
  return <>{children}</>;
}
```

Wire into `<ResultsCharts>`:

```tsx
<Card>
  <CardHeader>
    <CardTitle>Monthly Returns</CardTitle>
  </CardHeader>
  <CardContent>
    {seriesStatus === "ready" ? (
      <MonthlyReturnsHeatmap monthly={series?.monthly_returns ?? []} />
    ) : (
      <SeriesStatusIndicator status={seriesStatus} />
    )}
  </CardContent>
</Card>
```

### Step 2: Verify visually

```bash
cd frontend && pnpm build
# Open detail page — heatmap renders w/ month labels + year rows + color cells
```

### Step 3: Commit

```bash
git add frontend/src/components/backtests/results-charts.tsx
git commit -m "feat(frontend): native MonthlyReturnsHeatmap component (CSS Grid + oklch)"
```

---

## Task F6 — Paginated `<TradeLog>` via `/trades` endpoint

**Why:** PRD US-004. Replace the old round-trip-shape table with individual-fill columns + Prev/Next pagination + page counter.

**Files:**

- Modify: `frontend/src/components/backtests/trade-log.tsx`

### Step 1: Rewrite component

```tsx
"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { getBacktestTrades, type BacktestTradeItem } from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface TradeLogProps {
  backtestId: string;
  pageSize?: number;
}

export function TradeLog({
  backtestId,
  pageSize = 100,
}: TradeLogProps): JSX.Element {
  const { getToken } = useAuth();
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<BacktestTradeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      const token = await getToken();
      const res = await getBacktestTrades(
        backtestId,
        { page, page_size: pageSize },
        token,
      );
      if (cancelled) return;
      setItems(res.items);
      setTotal(res.total);
    })().finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [backtestId, page, pageSize, getToken]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const canPrev = page > 1 && !loading;
  const canNext = page < totalPages && !loading;

  return (
    <Card className="border-border/50">
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Trade Log</CardTitle>
          <CardDescription>
            {total > 0 ? `${total} fills` : "No trades executed"} · Page {page}{" "}
            of {totalPages}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((p) => p - 1)}
            disabled={!canPrev}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((p) => p + 1)}
            disabled={!canNext}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : items.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No trades executed in this backtest.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Quantity</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">P&L</TableHead>
                <TableHead className="text-right">Commission</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((t) => (
                <TableRow key={t.id} data-testid={`trade-row-${t.id}`}>
                  <TableCell className="font-mono text-xs">
                    {new Date(t.executed_at).toLocaleString()}
                  </TableCell>
                  <TableCell>{t.instrument}</TableCell>
                  <TableCell>
                    <Badge variant={t.side === "BUY" ? "default" : "secondary"}>
                      {t.side}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">{t.quantity}</TableCell>
                  <TableCell className="text-right">
                    ${t.price.toFixed(2)}
                  </TableCell>
                  <TableCell
                    className={`text-right ${t.pnl >= 0 ? "text-green-500" : "text-red-500"}`}
                  >
                    ${t.pnl.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-right">
                    ${t.commission.toFixed(2)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
```

### Step 2: Update the detail page call site

`frontend/src/app/backtests/[id]/page.tsx`:

```tsx
<TradeLog backtestId={id} />
```

Remove the old `<TradeLog trades={[]} />` call.

### Step 3: Verify visually

```bash
cd frontend && pnpm build
# Open detail page — TradeLog paginated + sortable by timestamp
```

### Step 4: Commit

```bash
git add frontend/src/components/backtests/trade-log.tsx frontend/src/app/backtests/[id]/page.tsx
git commit -m "feat(frontend): paginated TradeLog consumes /trades endpoint"
```

---

## Task F7 — `<SeriesStatusIndicator>` shared empty-state component

**Why:** PRD US-005 + US-006. `<EquityCurveChart>`, `<DrawdownChart>`, `<MonthlyReturnsHeatmap>` all need the same 3-state empty fallback (ready/not_materialized/failed). DRY helper.

**Files:**

- Create: `frontend/src/components/backtests/series-status-indicator.tsx`

### Step 1: Implement

```tsx
import { AlertTriangle, Info } from "lucide-react";
import type { SeriesStatus } from "@/lib/api";

interface SeriesStatusIndicatorProps {
  status: SeriesStatus;
}

export function SeriesStatusIndicator({
  status,
}: SeriesStatusIndicatorProps): JSX.Element {
  if (status === "not_materialized") {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 py-10 text-muted-foreground"
        data-testid="series-status-not-materialized"
      >
        <Info className="h-6 w-6" />
        <p className="text-sm">
          Analytics not available for backtests run before 2026-04-21.
        </p>
        <p className="text-xs">Re-run this backtest to populate charts.</p>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 py-10 text-amber-500"
        data-testid="series-status-failed"
      >
        <AlertTriangle className="h-6 w-6" />
        <p className="text-sm">Analytics computation failed.</p>
        <p className="text-xs text-muted-foreground">
          Aggregate metrics above still valid. Try re-running the backtest.
        </p>
      </div>
    );
  }

  // "ready" → never rendered (parent gates)
  return <></>;
}
```

Import + use in `<ResultsCharts>` (already wired in F4/F5 step code).

### Step 2: Commit

```bash
git add frontend/src/components/backtests/series-status-indicator.tsx
git commit -m "feat(frontend): SeriesStatusIndicator shared empty-state component"
```

---

## E2E Use Cases (Phase 3.2b)

Project type: `fullstack`. All cases run API-first, then UI-second per CLAUDE.md rule. ARRANGE via sanctioned paths only (no raw DB writes).

### UC-BRC-001 — Native charts populated after a fresh backtest (happy path, API-first + UI)

**Interface:** API + UI
**Setup (ARRANGE):**

1. `POST /api/v1/backtests/run` with `strategy_id` of the committed EMA Cross strategy and `instruments=["SPY.XNAS"]`, `start="2024-01-02"`, `end="2024-01-31"`, `config={"fast_ema_period": 10, "slow_ema_period": 30}`.
2. Poll `GET /api/v1/backtests/{id}/status` until `status=completed` (existing behavior; handles auto-ingest from PR #40).

**Steps (API):**

1. `GET /api/v1/backtests/{id}/results` → assert `series_status="ready"`, `series.daily` is non-empty, `series.monthly_returns` has exactly 1 entry `{month: "2024-01", pct: <float>}`.
2. Spot-check that `series.daily[0].drawdown <= 0` and `series.daily[-1].equity > 0`.
3. `GET /api/v1/backtests/{id}/trades?page=1&page_size=100` → assert `total > 0`, items sorted by `executed_at ASC`, each item has the individual-fill shape.

**Steps (UI, via Playwright MCP):** 4. Navigate to `http://localhost:3300/backtests/{id}`. 5. Wait for the Equity Curve card to show a non-empty SVG (`getByTestId("equity-curve-chart") > svg`). 6. Click the "Full report" tab; wait for iframe to load; assert iframe `<body>` contains "Performance" (QuantStats header).

**Verification:** all assertions above pass. Reload page → all still visible.

**Classification on failure:** `FAIL_BUG` if charts empty post-completion.

### UC-BRC-002 — Legacy backtest renders gracefully (US-005)

**Interface:** API + UI
**Setup (ARRANGE):** This UC validates the behavior produced by the Alembic migration's `DEFAULT 'not_materialized'` clause. Any row completed before the migration ran will automatically be in this state. Legitimate sanctioned-path setup:

1. From `main` (before the feature branch's migration), submit + complete a backtest via `POST /api/v1/backtests/run`.
2. Apply the migration (`alembic upgrade head` — which is idempotent per B1).
3. Per the migration's `DEFAULT 'not_materialized'`, the row now has `series=NULL`, `series_status='not_materialized'`.

If this PR is run against a fresh/clean environment where no pre-PR backtest exists, the UC is vacuously satisfied — the test-harness equivalent is the `seeded_legacy_backtest` fixture from Task B0, used in the backend integration tests. Mark this UC as `N/A — covered by backend integration test` if the E2E harness doesn't have a pre-migration backtest available.

**Steps (API):**

1. `GET /api/v1/backtests/{legacy_id}/results` → assert `series_status="not_materialized"`, `series is null`, `metrics` still present.

**Steps (UI):** 2. Navigate to detail page for `{legacy_id}`. 3. Assert the "Analytics not available for backtests run before 2026-04-21" message is visible in 3 chart cards. 4. Click "Full report" tab → iframe still loads (pre-PR backtests still have `report_path`).

**Classification:** `FAIL_BUG` if the page 500s.

### UC-BRC-003 — Compute-failed backtest disambiguates (US-006)

**Interface:** API
**Setup:** ARRANGE — submit a backtest that triggers a pathological `account_df` (e.g., a strategy that intentionally returns no bars). If we can't produce this state through legitimate inputs, **skip this UC as `SKIPPED_INFRA`** and note we'll validate via the unit test path (task B5 already tests it).

**Steps:**

1. `GET /api/v1/backtests/{id}/results` → assert `series_status="failed"`, `metrics` present, `series is null`.
2. UI shows amber warning in chart cards.

**Classification:** `FAIL_BUG` if `series_status` ends up `"ready"` when it should be `"failed"`.

### UC-BRC-004 — Trade log pagination (US-004)

**Interface:** API + UI
**Setup:** use a backtest with ≥ 150 trades (PR #40 demo SPY 2024-01 produced 418; run a similar one).

**Steps (API):**

1. `GET /api/v1/backtests/{id}/trades?page=1&page_size=100` → 100 items, `total=418`.
2. `GET /api/v1/backtests/{id}/trades?page=5&page_size=100` → 18 items (remainder).
3. `GET /api/v1/backtests/{id}/trades?page=99&page_size=100` → 0 items, no 404.
4. `GET /api/v1/backtests/{id}/trades?page_size=9999` → `page_size=500` in response (clamped).

**Steps (UI):** 5. Navigate to detail page. 6. Assert first 100 rows visible in TradeLog. 7. Click Next → assert rows change + "Page 2 of 5" text updates. 8. Click Next 3 more times → "Page 5 of 5", Next disabled.

**Classification:** `FAIL_BUG` if pagination returns wrong totals or rows don't change.

### UC-BRC-005 — Signed-URL iframe auth (US-002, iter-9 rework)

**Interface:** API + UI
**Setup:** backtest with `report_path` exists.

**Steps (API — verify the auth boundary):**

1. `curl -sI http://localhost:8800/api/v1/backtests/{id}/report` (no auth header, no `?token=`). Expected: **401 UNAUTHENTICATED**. Without a token or valid Bearer/X-API-Key, the endpoint rejects.
2. `curl -sI "http://localhost:8800/api/v1/backtests/{id}/report?token=not-a-real-token"`. Expected: **401 INVALID_TOKEN**.
3. Authenticated POST: `curl -X POST -H "X-API-Key: $MSAI_API_KEY" http://localhost:8800/api/v1/backtests/{id}/report-token` → returns `{"signed_url": "/api/v1/backtests/{id}/report?token=...", "expires_at": "..."}`.
4. GET that signed URL with NO auth header: `curl -sI "http://localhost:8800$signed_url"`. Expected: **200 OK**, `Content-Type: text/html`.
5. Wait 65 seconds (past TTL), retry the same signed URL. Expected: **401 INVALID_TOKEN** (expired).
6. Try signed URL minted for backtest A against backtest B: `curl -sI "http://localhost:8800/api/v1/backtests/{B}/report?token=$token_for_A"`. Expected: **401** (backtest_id mismatch).

**Steps (UI — verify iframe works end-to-end):**

7. Navigate to `/backtests/{id}`. Click "Full report" tab. Observe network tab:
   - A POST to `/api/v1/backtests/{id}/report-token` returns the signed URL.
   - The iframe's `src` is the signed URL (includes `?token=`).
   - The iframe renders the QuantStats tear sheet.
8. Close and re-open the "Full report" tab after > 60 seconds. New POST fires; iframe reloads with a fresh token.

**Classification:** `FAIL_BUG` if unauthenticated curl without a token returns 200 (this is the iter-9 auth-bypass regression we must prevent); `FAIL_BUG` if expired tokens are accepted; `FAIL_BUG` if cross-backtest tokens are accepted; `FAIL_BUG` if the iframe fails to render with a valid token.

### UC-BRC-006 — Download Report regression guard (US-003)

**Interface:** UI
**Setup:** completed backtest with `report_path`.

**Steps:**

1. Navigate to detail page.
2. Click "Download Report" → browser receives a `.html` file.
3. Open downloaded file standalone → renders QuantStats tear sheet.

**Classification:** `FAIL_BUG` if download flow is broken.

---

## Execution summary

**19 tasks total** (12 backend B0, B0b, B1–B10 + 7 frontend F1–F7). Estimated wall-clock: ~2–3 days of focused work. Commit cadence: one commit per task (19 commits before PR, batch-squashable at merge).

**Task dependency graph (iter-1 revised):**

```
B1 (migration) → B2 (model + Pydantic types) → B0 (fixtures depend on model) → B3 → B4 → B5 → B9
B0b (Histogram primitive, independent of B0 — prereq for B9)
B6 (BacktestResultsResponse w/ has_report) → B7 (/results handler)
B8 (/trades endpoint, independent, parallel to B3–B7)
B10 (signed-URL signer + /report-token endpoint + /report ?token= extension — iter-9 rework)

F1 (needs B6 + B10) → F2 (needs F1, B10) → F3 (needs F2)
                                              ↓
F4 (needs F1 + F3 + F7) ← F7 (SeriesStatusIndicator) consumed by F4/F5
F5 (needs F1 + F3 + F7)
F6 (needs F1 + F3 + B8)
```

Key revisions baked into the graph:

- **iter-1:** B0 fixtures depend on B2 (model needs `series` attribute); B0b Histogram is new prereq for B9; B5 signature revised (caller builds payload; `_finalize_backtest` +2 params).
- **iter-9:** Task F2 is no longer a Next.js Route Handler — it's a signed-URL client fetch. B10 is no longer a docker-compose env change — it's the signed-URL backend machinery (signer service + `POST /report-token` + `GET /report?token=` extension). The `MSAI_API_KEY` on the frontend container is NOT added; that was the unsafe design dropped after iter-9 Codex review.

**6 E2E use cases** designed (UC-BRC-001..006). All will graduate to `tests/e2e/use-cases/backtests/charts-and-trades.md` after Phase 5.4 passes.

---

**Execution handoff:**

Plan complete and saved to `docs/plans/2026-04-21-backtest-results-charts-and-trades.md`. Two execution options:

1. **Subagent-Driven (this session)** — dispatch fresh subagent per task, review between tasks, fast iteration.
2. **Parallel Session (separate)** — open new session with executing-plans, batch execution with checkpoints.

Which approach?
