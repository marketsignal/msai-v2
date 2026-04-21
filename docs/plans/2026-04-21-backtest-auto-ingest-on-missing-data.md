# Backtest Auto-Ingest on Missing Data — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development` if staying in-session) to implement this plan task-by-task.

**Goal:** When a backtest fails with `FailureCode.MISSING_DATA` (FileNotFoundError from `ensure_catalog_data`), auto-download the missing data (bounded lazy: ≤10y, ≤20 symbols, no options-chain fan-out) and transparently re-run the backtest. While healing is in flight, surface `status="running"` + `phase="awaiting_data"` + `progress_message` on `GET /backtests/{id}/status`. Close the PR #39 scope-defer by deriving `asset_class` server-side. Keep the FailureCard only for auto-heal failures (guardrail rejections + 30-min cap timeouts + coverage verification failures).

**Architecture:** A single `run_auto_heal(backtest_id, exc, instruments, start, end)` orchestrator is called from `run_backtest_job`'s outer `except FileNotFoundError` branch BEFORE `_mark_backtest_failed`. The orchestrator: (1) derives `asset_class` via `SecurityMaster.resolve_for_backtest`, (2) evaluates guardrails, (3) acquires a Redis `SET NX EX` dedupe lock, (4) enqueues `run_ingest` on the dedicated `msai:ingest` arq queue (now actually routed, fixing a 2-line bug) and stores `heal_job_id`, (5) marks backtest `phase="awaiting_data"` + `progress_message`, (6) polls ingest job status + wall-clock elapsed (cap 30 min), (7) on success runs Nautilus-native `catalog.get_missing_intervals_for_request` coverage re-check, (8) if fully covered, re-enters the backtest execution body one time; on any failure, falls through to the existing `_mark_backtest_failed` envelope. No new top-level status value; phase/progress_message are additive nullable columns.

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy 2.0 + Alembic + arq + Redis + NautilusTrader 1.223.0 + structlog + pydantic 2.10 + Next.js 15 + React + shadcn/ui + TradingView Charts. Providers: Databento (futures/equities) + Polygon.io (equities fallback). Deployment: Docker Compose (`ingest-worker` container already exists, consumes `msai:ingest` queue).

**Scope control:**

- **In:** backend auto-heal pipeline, guardrails, dedupe, coverage verification, server-side asset_class derivation, queue routing fix, 4 additive backtest columns, structured logs, UI phase indicator (subtle, additive).
- **Out (deferred to future PRs):** eager 10y pre-seed for curated universe, SSE/WS progress streaming, telemetry dashboard, rich cost visibility UI, "Retry Backtest" button, partial-range backfill optimization, auto-heal for non-MISSING_DATA codes, auto-expanding options chains.

**Key deferrals and knobs settled by council (2026-04-21):**

- `AUTO_HEAL_MAX_YEARS = 10` (hard cap, inclusive).
- `AUTO_HEAL_MAX_SYMBOLS = 20` (derived from wall-clock math — 20 sym × 10y × 1min on Polygon throttle ≈ 10-20min, leaves 10-20min margin under the 30-min cap).
- `AUTO_HEAL_ALLOW_OPTIONS = False` (Databento OPRA OHLCV-1m = $280/GB + Nautilus gotcha #12).
- `AUTO_HEAL_WALL_CLOCK_CAP_SECONDS = 1800` (30 min).
- `AUTO_HEAL_POLL_INTERVAL_SECONDS = 10`.
- `AUTO_HEAL_LOCK_TTL_SECONDS = 3000` (50 min = cap + 20min buffer).
- `ingest_queue_name = "msai:ingest"` (matches existing `IngestWorkerSettings.queue_name`).

---

## Task Graph

Tasks are ordered by dependency. Each task ends with a commit. Do NOT run `git commit` on a full phase-5 workflow gate — CONTINUITY's PreToolUse hook will block mid-phase commits; accumulate changes across tasks in the worktree and commit them all in Phase 6 Ship. (Per feedback memory `workflow_gate_blocks_preflight_commits`.)

The `git commit` blocks below are **staging markers** — run `git add <files>` after each task and `git status` to verify. The actual atomic commit happens in Phase 6.

- **B0** Migration + model: 4 additive columns on `backtests` (phase, progress_message, heal_started_at, heal_job_id)
- **B1** Settings + config: auto-heal env knobs + `ingest_queue_name`
- **B2** Fix queue routing: `enqueue_ingest` passes `_queue_name`; `IngestWorkerSettings.functions` registers `run_ingest`
- **B3** Server-side asset_class derivation (closes PR #39 scope-defer) — new `derive_asset_class` helper + wire into classifier
- **B4** Redis dedupe lock (`auto_heal_lock.py`)
- **B5** Coverage verification helper (`catalog_builder.verify_catalog_coverage`)
- **B6** Guardrail evaluator (`auto_heal_guardrails.py`)
- **B7** Auto-heal orchestrator (`auto_heal.py`) — integrates B2-B6, emits 7 structured log events
- **B8** Wire auto-heal into `backtest_job.py` (retry-once pattern in outer except branch)
- **B9** Extend `BacktestStatusResponse` with `phase` + `progress_message` + populate in API handler
- **F1** Typed API client: add `phase` + `progress_message` to TS types
- **F2** UI subtle indicator on `/backtests/{id}` detail + list-page badge

---

### Task B0: Alembic migration + SQLAlchemy columns for `phase`, `progress_message`, `heal_started_at`, `heal_job_id`

**Files:**

- Create: `backend/alembic/versions/y3s4t5u6v7w8_add_backtest_auto_heal_columns.py`
- Modify: `backend/src/msai/models/backtest.py:57-59` (insert 4 new `Mapped[]` columns after the PR #39 error envelope block, before `started_at`)
- Test: `backend/tests/integration/test_migrations.py` (append a new round-trip test OR add a new standalone file — check existing file's convention)

**Step 1: Read current migration head to pin `down_revision`**

Run: `cd backend && uv run alembic heads`
Expected output: `x2r3s4t5u6v7 (head)` (the PR #39 migration — confirm this is the tip).

**Step 2: Write the failing round-trip test**

Append to `backend/tests/integration/test_migrations.py`:

```python
def test_y3_backtest_auto_heal_columns_roundtrip(alembic_subprocess: Callable[[str], tuple[str, str]]) -> None:
    """y3s4t5u6v7 should add phase/progress_message/heal_started_at/heal_job_id, all nullable."""
    # Upgrade to head
    stdout, stderr = alembic_subprocess("upgrade head")
    assert "y3s4t5u6v7" in (stdout + stderr) or "already" in (stdout + stderr).lower()

    # Inspect columns
    with _engine().begin() as conn:  # reuse existing test helper
        result = conn.execute(
            text("SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                 "WHERE table_name = 'backtests' AND column_name IN "
                 "('phase', 'progress_message', 'heal_started_at', 'heal_job_id')")
        ).fetchall()
    cols = {row[0]: (row[1], row[2]) for row in result}
    assert cols == {
        "phase": ("YES", "character varying"),
        "progress_message": ("YES", "text"),
        "heal_started_at": ("YES", "timestamp with time zone"),
        "heal_job_id": ("YES", "character varying"),
    }

    # Downgrade and re-upgrade
    alembic_subprocess("downgrade -1")
    alembic_subprocess("upgrade head")
```

**Step 3: Run to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_migrations.py::test_y3_backtest_auto_heal_columns_roundtrip -v`
Expected: FAIL — migration file does not exist yet.

**Step 4: Create the migration file**

Create `backend/alembic/versions/y3s4t5u6v7w8_add_backtest_auto_heal_columns.py`:

```python
"""Add auto-heal phase/progress/heal metadata columns to backtests.

Revision ID: y3s4t5u6v7w8
Revises: x2r3s4t5u6v7
Create Date: 2026-04-21 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "y3s4t5u6v7w8"
down_revision = "x2r3s4t5u6v7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 4 additive nullable columns for auto-heal lifecycle tracking."""
    op.add_column("backtests", sa.Column("phase", sa.String(length=32), nullable=True))
    op.add_column("backtests", sa.Column("progress_message", sa.Text(), nullable=True))
    op.add_column(
        "backtests",
        sa.Column("heal_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("backtests", sa.Column("heal_job_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("backtests", "heal_job_id")
    op.drop_column("backtests", "heal_started_at")
    op.drop_column("backtests", "progress_message")
    op.drop_column("backtests", "phase")
```

**Step 5: Update SQLAlchemy model**

Edit `backend/src/msai/models/backtest.py` — insert AFTER line 57 (`error_remediation` column) and BEFORE line 58 (`started_at`):

```python
    # --- Auto-heal lifecycle (added by PR #<this>) -----------------------
    # Populated by ``services/backtests/auto_heal.py`` while the worker
    # waits for a triggered ingest job to complete. All four are cleared
    # together when the heal reaches a terminal state.
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    heal_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heal_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Note: `datetime` is already imported at the top with the PR #39 `noqa: TC003` comment — no new imports needed.

**Step 6: Apply migration + run test**

Run:

```bash
docker compose -f docker-compose.dev.yml exec backend uv run alembic upgrade head
cd backend && uv run pytest tests/integration/test_migrations.py::test_y3_backtest_auto_heal_columns_roundtrip -v
```

Expected: test passes. Verify new columns exist with `docker compose -f docker-compose.dev.yml exec postgres psql -U msai -d msai -c "\d backtests"`.

**Step 7: Stage**

```bash
git add backend/alembic/versions/y3s4t5u6v7w8_add_backtest_auto_heal_columns.py backend/src/msai/models/backtest.py backend/tests/integration/test_migrations.py
```

---

### Task B1: Settings — auto-heal knobs + `ingest_queue_name`

**Files:**

- Modify: `backend/src/msai/core/config.py:172-173` (append auto-heal + ingest_queue_name settings after existing queue_name settings)
- Test: `backend/tests/unit/core/test_config.py` (may not exist; if not, create with minimal setting-load assertions)

**Step 1: Write failing test**

Append to or create `backend/tests/unit/core/test_config.py`:

```python
from msai.core.config import Settings


def test_auto_heal_settings_have_council_defaults() -> None:
    """Council-locked defaults (2026-04-21)."""
    s = Settings()
    assert s.auto_heal_max_years == 10
    assert s.auto_heal_max_symbols == 20
    assert s.auto_heal_allow_options is False
    assert s.auto_heal_wall_clock_cap_seconds == 1800
    assert s.auto_heal_poll_interval_seconds == 10
    assert s.auto_heal_lock_ttl_seconds == 3000
    assert s.ingest_queue_name == "msai:ingest"


def test_auto_heal_settings_env_override() -> None:
    import os
    os.environ["AUTO_HEAL_MAX_YEARS"] = "5"
    try:
        s = Settings()
        assert s.auto_heal_max_years == 5
    finally:
        del os.environ["AUTO_HEAL_MAX_YEARS"]
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/core/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'auto_heal_max_years'`.

**Step 3: Add settings fields**

Edit `backend/src/msai/core/config.py` — insert AFTER the existing `portfolio_queue_name: str = "msai:portfolio"` line (around line 173):

```python
    # --- Ingest queue (existing topology — see docker-compose.dev.yml `ingest-worker` service) ---
    ingest_queue_name: str = "msai:ingest"

    # --- Auto-heal knobs (council-locked defaults, 2026-04-21) ---
    # See docs/prds/backtest-auto-ingest-on-missing-data.md §5 (Technical Constraints)
    # and the research brief §1-2 for the math behind these values.
    auto_heal_max_years: int = 10
    auto_heal_max_symbols: int = 20
    auto_heal_allow_options: bool = False
    auto_heal_wall_clock_cap_seconds: int = 1800
    auto_heal_poll_interval_seconds: int = 10
    auto_heal_lock_ttl_seconds: int = 3000
```

**Step 4: Run test again**

Run: `cd backend && uv run pytest tests/unit/core/test_config.py -v`
Expected: PASS.

**Step 5: Stage**

```bash
git add backend/src/msai/core/config.py backend/tests/unit/core/test_config.py
```

---

### Task B2: Fix queue routing for on-demand ingest jobs

**Files:**

- Modify: `backend/src/msai/core/queue.py:170` (one-line fix: add `_queue_name=`)
- Modify: `backend/src/msai/workers/ingest_settings.py:33-42` (register `run_ingest` in `IngestWorkerSettings.functions`)
- Test: `backend/tests/unit/core/test_queue.py` (likely exists for `enqueue_backtest`; add a parallel test for `enqueue_ingest`)

**Step 1: Write failing test**

Append to `backend/tests/unit/core/test_queue.py` (or create):

```python
from unittest.mock import AsyncMock

import pytest

from msai.core.config import settings
from msai.core.queue import enqueue_ingest


@pytest.mark.asyncio
async def test_enqueue_ingest_routes_to_ingest_queue() -> None:
    """On-demand ingest must NOT land on the default backtest queue."""
    fake_pool = AsyncMock()
    fake_pool.enqueue_job = AsyncMock()
    await enqueue_ingest(
        pool=fake_pool,
        asset_class="stocks",
        symbols=["AAPL"],
        start="2024-01-01",
        end="2024-12-31",
    )
    assert fake_pool.enqueue_job.call_count == 1
    _, kwargs = fake_pool.enqueue_job.call_args
    assert kwargs.get("_queue_name") == settings.ingest_queue_name
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/core/test_queue.py::test_enqueue_ingest_routes_to_ingest_queue -v`
Expected: FAIL — current `enqueue_ingest` omits `_queue_name`.

**Step 3: Fix `enqueue_ingest`**

Edit `backend/src/msai/core/queue.py:170-179` — replace the `enqueue_job` call body:

```python
    from msai.core.config import settings as _settings  # lazy to avoid circular deps

    await pool.enqueue_job(
        "run_ingest",
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
        provider=provider,
        dataset=dataset,
        schema=schema,
        _queue_name=_settings.ingest_queue_name,
    )
```

**Step 4: Register `run_ingest` on ingest worker**

Edit `backend/src/msai/workers/ingest_settings.py:33-42` — change `functions = [run_nightly_ingest]` to include `run_ingest`:

```python
from msai.workers.settings import run_ingest  # reuse the arq function wrapper

class IngestWorkerSettings:
    """arq worker config for the dedicated msai:ingest queue.

    This worker handles both the nightly cron ingest AND on-demand
    ingest jobs triggered by backtest auto-heal. Isolating these from
    the backtest worker (max_jobs=2) prevents ingest-vs-backtest
    starvation — see docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md §3.
    """

    functions = [run_nightly_ingest, run_ingest]
    # ...existing redis_settings / queue_name / cron_jobs lines unchanged...
```

Note: the `from msai.workers.settings import run_ingest` import may need `asyncio.set_event_loop_policy(None)` guard — check existing module header. The Nautilus uvloop gotcha #1 is relevant if `run_ingest` transitively imports nautilus_trader; it does (via `data_ingestion` → eventually nautilus catalog writers). Follow existing pattern in `workers/settings.py` for the import-order + `set_event_loop_policy(None)` placement.

Leave `run_ingest` also registered on `WorkerSettings.functions` (line 124 of `workers/settings.py`) for zero-downtime migration — any already-enqueued jobs on the default queue at deploy time still execute. A follow-up cleanup PR can drop it from the default worker once the queue drains.

**Step 5: Run test**

Run: `cd backend && uv run pytest tests/unit/core/test_queue.py::test_enqueue_ingest_routes_to_ingest_queue -v`
Expected: PASS.

Add an integration-level assertion that `IngestWorkerSettings.functions` contains `run_ingest`:

```python
def test_ingest_worker_registers_on_demand_ingest() -> None:
    from msai.workers.ingest_settings import IngestWorkerSettings
    fn_names = [fn.__name__ for fn in IngestWorkerSettings.functions]
    assert "run_ingest" in fn_names
    assert "run_nightly_ingest" in fn_names
```

**Step 6: Stage**

```bash
git add backend/src/msai/core/queue.py backend/src/msai/workers/ingest_settings.py backend/tests/unit/core/test_queue.py
```

---

### Task B3: Server-side asset_class derivation (closes PR #39 scope-defer, US-005)

**Files:**

- Create: `backend/src/msai/services/backtests/derive_asset_class.py`
- Modify: `backend/src/msai/services/backtests/classifier.py:102-137` (replace the regex-recovery + caller-kwarg fallback with the new helper)
- Test: `backend/tests/unit/services/backtests/test_derive_asset_class.py`

**Step 1: Write parametrized failing test**

Create `backend/tests/unit/services/backtests/test_derive_asset_class.py`:

```python
from datetime import date
from unittest.mock import MagicMock

import pytest

from msai.services.backtests.derive_asset_class import derive_asset_class


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("AAPL.NASDAQ", "stocks"),
        ("SPY.ARCA", "stocks"),
        ("ES.n.0", "futures"),
        ("ESM6.CME", "futures"),
        ("EUR/USD.IDEALPRO", "forex"),
        ("SPY_CALL_400_20251231.OPRA", "options"),  # heuristic branch
    ],
)
def test_derive_asset_class_from_shape(symbol: str, expected: str) -> None:
    """Shape-based fallback when registry lookup is unavailable or returns nothing."""
    assert derive_asset_class([symbol], start=date(2024, 1, 1), registry=None) == expected


def test_derive_asset_class_prefers_registry_over_shape() -> None:
    """If SecurityMaster returns a canonical asset_class, use it."""
    fake_registry = MagicMock()
    fake_registry.resolve_asset_class = MagicMock(return_value="crypto")
    assert (
        derive_asset_class(["BTC.BINANCE"], start=date(2024, 1, 1), registry=fake_registry)
        == "crypto"
    )


def test_derive_asset_class_falls_back_to_stocks_when_unknown() -> None:
    """Unknown symbol + no registry → "stocks" default + warning log (assert via caplog)."""
    result = derive_asset_class(["Ω_WEIRD_SYMBOL"], start=date(2024, 1, 1), registry=None)
    assert result == "stocks"


def test_derive_asset_class_mixed_asset_class_returns_first() -> None:
    """Mixed-asset-class requests return the asset class of the first symbol.

    The guardrail layer (B6) is responsible for rejecting mixed-asset-class
    auto-heal — this helper stays simple and deterministic.
    """
    assert (
        derive_asset_class(
            ["AAPL.NASDAQ", "ES.n.0"],
            start=date(2024, 1, 1),
            registry=None,
        )
        == "stocks"
    )
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_derive_asset_class.py -v`
Expected: FAIL — module does not exist.

**Step 3: Implement the helper**

Create `backend/src/msai/services/backtests/derive_asset_class.py`:

```python
"""Server-authoritative derivation of ``asset_class`` from instrument symbols.

Closes the scope-defer from PR #39 (backtest failure surfacing) where the
UI Run Backtest form didn't send ``config.asset_class`` and the worker
defaulted to ``"stocks"`` — producing wrong remediation commands for
futures and other asset classes.

Resolution precedence:

1. Registry (``SecurityMaster.resolve_asset_class``) — authoritative.
2. Shape-based heuristic on the first symbol (``.n.0`` suffix → futures,
   ``/`` in ticker → forex, ``.OPRA`` venue → options, ``.NASDAQ`` /
   ``.ARCA`` / ``.NYSE`` / ``.XNAS`` → stocks, ``.CME`` / ``.GLBX`` →
   futures).
3. Fallback ``"stocks"`` (matches existing worker default so behavior
   is backward-compatible on unknown instruments).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

log = get_logger(__name__)

_FUTURES_PATTERNS = (
    re.compile(r"\.n\.0$"),          # continuous futures (ES.n.0)
    re.compile(r"\.CME$"),
    re.compile(r"\.GLBX$"),
    re.compile(r"\.XCME$"),
    re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d\."),  # e.g. ESM6.CME
)
_FOREX_PATTERNS = (re.compile(r"/.+\."),)  # EUR/USD.IDEALPRO
_OPTIONS_PATTERNS = (re.compile(r"\.OPRA$"),)
_STOCKS_PATTERNS = (
    re.compile(r"\.NASDAQ$"),
    re.compile(r"\.ARCA$"),
    re.compile(r"\.NYSE$"),
    re.compile(r"\.XNAS$"),
    re.compile(r"\.BATS$"),
)


def derive_asset_class(
    symbols: list[str],
    *,
    start: "date",
    registry: Any | None,
) -> str:
    """Return the asset_class for the first symbol.

    Args:
        symbols: User-submitted instrument ID list (e.g., ``["ES.n.0"]``).
        start: Backtest start date — used for registry alias windowing.
        registry: A ``SecurityMaster``-compatible object exposing
            ``resolve_asset_class(symbol, *, start) -> str | None``, or
            ``None`` to skip registry lookup (unit-test convenience).

    Returns:
        One of ``"stocks"``, ``"futures"``, ``"options"``, ``"forex"``,
        ``"crypto"``, or the fallback ``"stocks"`` on full miss.
    """
    if not symbols:
        return "stocks"

    first = symbols[0]

    if registry is not None:
        try:
            from_registry = registry.resolve_asset_class(first, start=start)
        except Exception:  # noqa: BLE001 — registry misconfig must not bubble up mid-heal
            log.warning("asset_class_registry_lookup_failed", symbol=first, exc_info=True)
            from_registry = None
        if from_registry:
            return str(from_registry)

    for pattern in _OPTIONS_PATTERNS:
        if pattern.search(first):
            return "options"
    for pattern in _FUTURES_PATTERNS:
        if pattern.search(first):
            return "futures"
    for pattern in _FOREX_PATTERNS:
        if pattern.search(first):
            return "forex"
    for pattern in _STOCKS_PATTERNS:
        if pattern.search(first):
            return "stocks"

    log.warning(
        "asset_class_derivation_fallback",
        symbol=first,
        reason="no registry hit, no shape match",
    )
    return "stocks"
```

Note: `SecurityMaster` exposes `resolve_for_backtest` but not `resolve_asset_class` directly — a small wrapper is required. Add to `backend/src/msai/services/nautilus/security_master/service.py` near the existing `_asset_class_for_instrument` helper (line 540):

```python
def resolve_asset_class(self, symbol: str, *, start: "date") -> str | None:
    """Best-effort asset_class lookup by symbol as of ``start``.

    Returns ``None`` if the symbol isn't in the registry; callers should
    fall back to shape-based heuristics.
    """
    try:
        resolved = self.resolve_for_backtest([symbol], start=start)
    except Exception:  # noqa: BLE001
        return None
    if not resolved:
        return None
    first = resolved[0]
    return self._asset_class_for_instrument(first)
```

**Step 4: Wire into classifier**

Edit `backend/src/msai/services/backtests/classifier.py` — replace the `asset_class` resolution inside the `MISSING_DATA` branch (line 107: `resolved_asset_class = asset_class or (m.group(2) if m else None)`) with:

```python
        # Server-authoritative asset_class resolution — closes PR #39 scope-defer.
        # Caller-supplied kwarg is the LAST fallback, not the first, because
        # the worker's default is "stocks" which is wrong for futures/forex/options.
        from msai.services.backtests.derive_asset_class import derive_asset_class

        resolved_asset_class = (
            derive_asset_class(instruments, start=start_date, registry=_get_security_master())
            or asset_class
            or (m.group(2) if m else None)
        )
```

Add a module-level `_get_security_master()` helper at the top of classifier.py that returns a `SecurityMaster` instance or `None` if unavailable in the current context. The classifier runs in worker context where DB sessions are available; use a lazy factory matching the pattern in `api/backtests.py`.

Remove the `known limitation` block from the classifier docstring (lines 86-96) — the scope-defer is closed.

**Step 5: Run tests**

Run: `cd backend && uv run pytest tests/unit/services/backtests/ -v`
Expected: all pass, including existing PR #39 tests (classifier behavior unchanged for non-registry paths).

**Step 6: Stage**

```bash
git add backend/src/msai/services/backtests/derive_asset_class.py \
        backend/src/msai/services/backtests/classifier.py \
        backend/src/msai/services/nautilus/security_master/service.py \
        backend/tests/unit/services/backtests/test_derive_asset_class.py
```

---

### Task B4: Redis dedupe lock helper

**Files:**

- Create: `backend/src/msai/services/backtests/auto_heal_lock.py`
- Test: `backend/tests/unit/services/backtests/test_auto_heal_lock.py`

**Step 1: Write failing test**

Create `backend/tests/unit/services/backtests/test_auto_heal_lock.py`:

```python
import hashlib
from datetime import date

import pytest
from redis.asyncio import Redis

from msai.services.backtests.auto_heal_lock import (
    AutoHealLock,
    build_lock_key,
)


@pytest.fixture
async def redis_client() -> Redis:
    """Use fakeredis; project convention in live/idempotency tests."""
    from fakeredis import aioredis as fakeredis_asyncio  # already a dev dep per PR #34 era
    client = fakeredis_asyncio.FakeRedis()
    yield client
    await client.flushall()
    await client.aclose()


def test_build_lock_key_normalizes_symbol_order() -> None:
    """Sorting symbols makes (AAPL,MSFT) and (MSFT,AAPL) hash to the same key."""
    key_a = build_lock_key(
        asset_class="stocks",
        symbols=["AAPL", "MSFT"],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    key_b = build_lock_key(
        asset_class="stocks",
        symbols=["MSFT", "AAPL"],
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert key_a == key_b
    assert key_a.startswith("auto_heal:")


@pytest.mark.asyncio
async def test_try_acquire_first_holder_wins(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    first = await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")
    second = await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h2")
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_try_acquire_releases_allow_reacquire(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    assert await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1") is True
    await lock.release("auto_heal:test", holder_id="h1")
    assert await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h2") is True


@pytest.mark.asyncio
async def test_release_only_by_holder(redis_client: Redis) -> None:
    """A non-holder's release() is a no-op (don't steal a lock you don't own)."""
    lock = AutoHealLock(redis_client)
    assert await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1") is True
    await lock.release("auto_heal:test", holder_id="h2")  # wrong holder
    assert await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h3") is False


@pytest.mark.asyncio
async def test_get_holder_returns_value_when_locked(redis_client: Redis) -> None:
    lock = AutoHealLock(redis_client)
    await lock.try_acquire("auto_heal:test", ttl_s=60, holder_id="h1")
    assert await lock.get_holder("auto_heal:test") == "h1"
    assert await lock.get_holder("auto_heal:missing") is None
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal_lock.py -v`
Expected: FAIL — module doesn't exist.

**Step 3: Implement**

Create `backend/src/msai/services/backtests/auto_heal_lock.py`:

```python
"""Redis-backed dedupe lock for concurrent auto-heal requests.

Pattern mirrors ``services/live/idempotency.py::IdempotencyStore.reserve`` —
single atomic ``SET key value NX EX ttl`` acquire; TTL-based auto-release
on crashed holder (watchdog observes but doesn't intervene).

Key normalization: ``auto_heal:sha256(asset_class|sorted(symbols)|start|end)``.
Sorting makes ``[AAPL, MSFT]`` and ``[MSFT, AAPL]`` collide into a single
lock — correct for ingest dedupe because the backing download is
symmetric in symbol order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

    from redis.asyncio import Redis

log = get_logger(__name__)


def build_lock_key(
    *,
    asset_class: str,
    symbols: list[str],
    start: "date",
    end: "date",
) -> str:
    """Deterministic lock key for a normalized ingest scope."""
    canonical = "|".join(
        [
            asset_class,
            ",".join(sorted(symbols)),
            start.isoformat(),
            end.isoformat(),
        ]
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"auto_heal:{digest}"


@dataclass
class AutoHealLock:
    """Thin wrapper over ``redis.asyncio.Redis`` with safe release semantics."""

    redis: "Redis"

    async def try_acquire(self, key: str, *, ttl_s: int, holder_id: str) -> bool:
        """Atomic acquire; return True iff we own the lock."""
        was_set = await self.redis.set(key, holder_id, nx=True, ex=ttl_s)
        return bool(was_set)

    async def release(self, key: str, *, holder_id: str) -> None:
        """Release only if we still hold it.

        Uses a GET-before-DEL pattern — not a Lua script — because the
        race window is bounded by TTL; a spurious release after TTL
        expiry is functionally identical to TTL expiry itself.
        """
        current = await self.redis.get(key)
        if current is None:
            return
        current_str = current.decode() if isinstance(current, bytes) else str(current)
        if current_str != holder_id:
            log.warning(
                "auto_heal_lock_release_wrong_holder",
                key=key,
                current=current_str,
                requested=holder_id,
            )
            return
        await self.redis.delete(key)

    async def get_holder(self, key: str) -> str | None:
        """Inspect the current holder — used by the concurrent-wait path."""
        current = await self.redis.get(key)
        if current is None:
            return None
        return current.decode() if isinstance(current, bytes) else str(current)
```

Verify fakeredis is already a dev dependency:

```bash
cd backend && grep -q "fakeredis" pyproject.toml && echo "YES" || echo "NO - add to dev deps"
```

If missing, add `fakeredis[aio]>=2.20` under `[project.optional-dependencies].dev` in pyproject.toml and `uv sync`.

**Step 4: Run, verify all tests pass**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal_lock.py -v`
Expected: PASS.

**Step 5: Stage**

```bash
git add backend/src/msai/services/backtests/auto_heal_lock.py \
        backend/tests/unit/services/backtests/test_auto_heal_lock.py
```

---

### Task B5: Catalog coverage verification helper

**Files:**

- Modify: `backend/src/msai/services/nautilus/catalog_builder.py` (append new function near `ensure_catalog_data`)
- Test: `backend/tests/unit/services/nautilus/test_catalog_builder.py` (likely exists for existing `build_catalog_for_symbol` — append new tests)

**Step 1: Write failing test**

Append to `backend/tests/unit/services/nautilus/test_catalog_builder.py` (or create):

```python
from datetime import date
from pathlib import Path

from msai.services.nautilus.catalog_builder import verify_catalog_coverage


def test_verify_catalog_coverage_empty_catalog_returns_full_gap(tmp_path: Path) -> None:
    """An instrument with no catalog data → one gap == full requested range."""
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir(parents=True)
    gaps = verify_catalog_coverage(
        catalog_root=catalog_root,
        instrument_ids=["AAPL.NASDAQ"],
        bar_spec="1-MINUTE-LAST-EXTERNAL",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(gaps) == 1
    assert gaps[0][0] == "AAPL.NASDAQ"
    assert len(gaps[0][1]) == 1  # one gap
    # gap spans the whole requested range (ns-granular)


def test_verify_catalog_coverage_full_coverage_returns_empty(tmp_path: Path) -> None:
    """After writing a Bar catalog covering the requested range → no gaps."""
    # Use nautilus ParquetDataCatalog.write_data to seed a known window
    # (construct synthetic Bar objects; reuse test helpers from existing
    # `test_catalog_builder.py` fixtures or write minimal bar builders).
    # ...seed 2024-01-01..2024-12-31 for AAPL.NASDAQ with 1-MINUTE bars...
    ...
    gaps = verify_catalog_coverage(
        catalog_root=seeded_catalog_root,
        instrument_ids=["AAPL.NASDAQ"],
        bar_spec="1-MINUTE-LAST-EXTERNAL",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert gaps == [("AAPL.NASDAQ", [])]


def test_verify_catalog_coverage_partial_coverage_returns_specific_gap(tmp_path: Path) -> None:
    """Catalog has 2024 only; requesting 2022-2024 → returns 2022-01-01..2023-12-31 gap."""
    ...
```

Note: the "seed with real Bar objects" path may be complex — consider using `nautilus_trader.test_kit.providers.TestDataProvider` or `ParquetDataCatalog.write_data` with a minimal synthetic BarType. If that's too much for a unit test, keep just the empty-catalog test here and defer the seeded cases to an integration test that uses a real Nautilus harness.

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_catalog_builder.py::test_verify_catalog_coverage_empty_catalog_returns_full_gap -v`
Expected: FAIL.

**Step 3: Implement**

Append to `backend/src/msai/services/nautilus/catalog_builder.py`:

```python
def verify_catalog_coverage(
    *,
    catalog_root: Path,
    instrument_ids: list[str],
    bar_spec: str = _BAR_SPEC,
    start: "date",
    end: "date",
) -> list[tuple[str, list[tuple[int, int]]]]:
    """Return per-instrument gaps against the requested date range.

    Uses Nautilus-native
    ``ParquetDataCatalog.get_missing_intervals_for_request``, which scans
    the catalog's filename conventions ``{start_ns}-{end_ns}.parquet``
    under ``{catalog_root}/data/bar/{instrument_id}-{bar_spec}/``.

    Args:
        catalog_root: Nautilus catalog root directory.
        instrument_ids: Canonical Nautilus instrument ID strings (e.g.
            ``"AAPL.NASDAQ"``).
        bar_spec: Bar type suffix; defaults to the project's 1-minute
            external spec.
        start: Inclusive requested start (converted to nanoseconds).
        end: Inclusive requested end (converted to nanoseconds at
            end-of-day UTC).

    Returns:
        A list of ``(instrument_id, gaps)`` tuples. ``gaps`` is a list
        of ``(start_ns, end_ns)`` tuples; empty list means full
        coverage.
    """
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.identifiers import BarType
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(str(catalog_root))
    start_ns = int(
        datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1e9
    )
    end_ns = int(
        datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC).timestamp() * 1e9
    )

    results: list[tuple[str, list[tuple[int, int]]]] = []
    for instrument_id in instrument_ids:
        bar_type = BarType.from_str(f"{instrument_id}-{bar_spec}")
        gaps = catalog.get_missing_intervals_for_request(
            start=start_ns,
            end=end_ns,
            data_cls=Bar,
            identifier=str(bar_type),
        )
        results.append((instrument_id, list(gaps)))
    return results
```

Add required imports at the top of `catalog_builder.py`:

```python
from datetime import UTC, datetime
# (`date` only under TYPE_CHECKING — already present)
```

**Step 4: Run tests**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_catalog_builder.py -v`
Expected: PASS (at minimum the empty-catalog case).

**Step 5: Stage**

```bash
git add backend/src/msai/services/nautilus/catalog_builder.py \
        backend/tests/unit/services/nautilus/test_catalog_builder.py
```

---

### Task B6: Guardrail evaluator

**Files:**

- Create: `backend/src/msai/services/backtests/auto_heal_guardrails.py`
- Test: `backend/tests/unit/services/backtests/test_auto_heal_guardrails.py`

**Step 1: Write failing test**

Create `backend/tests/unit/services/backtests/test_auto_heal_guardrails.py`:

```python
from datetime import date

import pytest

from msai.services.backtests.auto_heal_guardrails import (
    GuardrailResult,
    evaluate_guardrails,
)


def _g(**overrides):
    base = {
        "asset_class": "stocks",
        "symbols": ["AAPL"],
        "start": date(2024, 1, 1),
        "end": date(2024, 12, 31),
        "max_years": 10,
        "max_symbols": 20,
        "allow_options": False,
    }
    base.update(overrides)
    return evaluate_guardrails(**base)


def test_happy_path_within_all_caps() -> None:
    result = _g()
    assert result.allowed is True
    assert result.reason is None


def test_rejects_options_asset_class() -> None:
    result = _g(asset_class="options")
    assert result.allowed is False
    assert result.reason == "options_disabled"
    assert "options" in result.human_message.lower()


def test_allows_options_when_explicitly_enabled() -> None:
    result = _g(asset_class="options", allow_options=True)
    assert result.allowed is True


def test_rejects_excessive_date_range() -> None:
    result = _g(start=date(2010, 1, 1), end=date(2024, 12, 31))  # ~15y
    assert result.allowed is False
    assert result.reason == "range_exceeds_max_years"
    assert "15" in result.human_message


def test_accepts_exactly_10_years() -> None:
    result = _g(start=date(2014, 1, 1), end=date(2023, 12, 31))
    assert result.allowed is True


def test_rejects_excessive_symbol_count() -> None:
    result = _g(symbols=[f"SYM{i}" for i in range(25)])
    assert result.allowed is False
    assert result.reason == "symbol_count_exceeds_max"


def test_accepts_exactly_max_symbols() -> None:
    result = _g(symbols=[f"SYM{i}" for i in range(20)])
    assert result.allowed is True


def test_empty_symbols_rejected() -> None:
    result = _g(symbols=[])
    assert result.allowed is False
    assert result.reason == "no_symbols"
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal_guardrails.py -v`
Expected: FAIL — module doesn't exist.

**Step 3: Implement**

Create `backend/src/msai/services/backtests/auto_heal_guardrails.py`:

```python
"""Auto-heal workload guardrails.

Evaluates bounded-lazy constraints BEFORE enqueueing a provider download
— prevents accidental unbounded spend on a malformed or agent-generated
backtest request.

Council-locked invariants:
- ``max_years = 10`` (cap is inclusive)
- ``max_symbols = 20``
- ``allow_options = False`` (OPRA OHLCV-1m is $280/GB on Databento)
- Mixed-asset-class requests are out of scope — caller is responsible
  for dispatching one guardrail check per asset class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date

GuardrailReason = Literal[
    "options_disabled",
    "range_exceeds_max_years",
    "symbol_count_exceeds_max",
    "no_symbols",
]


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """Outcome of a single guardrail evaluation."""

    allowed: bool
    reason: GuardrailReason | None
    human_message: str
    details: dict[str, int | str] | None = None


def evaluate_guardrails(
    *,
    asset_class: str,
    symbols: list[str],
    start: "date",
    end: "date",
    max_years: int,
    max_symbols: int,
    allow_options: bool,
) -> GuardrailResult:
    """Return whether the request passes all guardrails.

    First-match returns immediately — order is: empty, options, range, count.
    """
    if not symbols:
        return GuardrailResult(
            allowed=False,
            reason="no_symbols",
            human_message="Auto-download disabled — request has no symbols.",
        )

    if asset_class == "options" and not allow_options:
        return GuardrailResult(
            allowed=False,
            reason="options_disabled",
            human_message=(
                "Auto-download disabled for options (OPRA cost + chain-fan-out risk). "
                "Manually scope and run: msai ingest options <strike-scoped-ids> ..."
            ),
            details={"asset_class": asset_class},
        )

    range_years = (end - start).days / 365.25
    if range_years > max_years:
        return GuardrailResult(
            allowed=False,
            reason="range_exceeds_max_years",
            human_message=(
                f"Auto-download disabled — {range_years:.0f}-year range exceeds "
                f"{max_years}-year cap."
            ),
            details={"range_years": int(range_years), "max_years": max_years},
        )

    if len(symbols) > max_symbols:
        return GuardrailResult(
            allowed=False,
            reason="symbol_count_exceeds_max",
            human_message=(
                f"Auto-download disabled — {len(symbols)} symbols exceeds "
                f"{max_symbols}-symbol cap per request."
            ),
            details={"symbol_count": len(symbols), "max_symbols": max_symbols},
        )

    return GuardrailResult(
        allowed=True,
        reason=None,
        human_message="Guardrails passed.",
    )
```

**Step 4: Run tests**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal_guardrails.py -v`
Expected: PASS all 8.

**Step 5: Stage**

```bash
git add backend/src/msai/services/backtests/auto_heal_guardrails.py \
        backend/tests/unit/services/backtests/test_auto_heal_guardrails.py
```

---

### Task B7: Auto-heal orchestrator — integrates B2-B6, emits structured log events

**Files:**

- Create: `backend/src/msai/services/backtests/auto_heal.py`
- Test: `backend/tests/unit/services/backtests/test_auto_heal.py`
- Test: `backend/tests/integration/test_auto_heal_end_to_end.py`

**Step 1: Write a failing orchestration test**

Create `backend/tests/unit/services/backtests/test_auto_heal.py` with unit tests that mock:

- `AutoHealLock` (control whether lock acquires)
- `enqueue_ingest` (assert it's called with the right kwargs, or NOT called on dedupe path)
- A fake arq pool with `job_from_id(...)` → mocked job result
- `verify_catalog_coverage` (return empty list = covered, or a gap list = still missing)
- `structlog.testing.capture_logs()` for event assertions

Test cases:

1. `test_auto_heal_happy_path_enqueues_ingest_updates_phase_polls_succeeds`
2. `test_auto_heal_guardrail_rejection_does_not_enqueue_and_clears_phase`
3. `test_auto_heal_dedupe_lock_already_held_waits_for_existing_holder`
4. `test_auto_heal_wall_clock_cap_transitions_backtest_to_failed`
5. `test_auto_heal_ingest_fails_propagates_provider_error_to_envelope`
6. `test_auto_heal_coverage_still_missing_after_ingest_returns_partial_gap`
7. `test_auto_heal_options_guardrail_rejected_emits_structured_log`
8. `test_auto_heal_emits_all_seven_events_on_happy_path`

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal.py -v`
Expected: FAIL — module doesn't exist.

**Step 3: Implement orchestrator**

Create `backend/src/msai/services/backtests/auto_heal.py`:

```python
"""Auto-heal orchestrator for MISSING_DATA backtest failures.

Entry point called from ``workers/backtest_job.py``'s outer except
``FileNotFoundError`` branch. Performs the full bounded-lazy auto-heal
cycle: derive asset_class → evaluate guardrails → acquire dedupe lock →
enqueue ingest → poll with wall-clock cap → verify coverage → signal
success/failure to the caller.

Council-locked contract: never mutates anything outside the backtest
row's phase/progress_message/heal_started_at/heal_job_id fields. The
caller (``backtest_job.py``) is responsible for the retry-once
re-execution path and for propagating failure reasons into the
``ErrorEnvelope``.

Structured-log events (all include ambient ``backtest_id`` via
``structlog.contextvars``):

- ``backtest_auto_heal_started``
- ``backtest_auto_heal_guardrail_rejected``
- ``backtest_auto_heal_ingest_enqueued``
- ``backtest_auto_heal_ingest_completed``
- ``backtest_auto_heal_ingest_failed``
- ``backtest_auto_heal_timeout``
- ``backtest_auto_heal_completed``
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import enqueue_ingest, get_redis_pool
from msai.models.backtest import Backtest
from msai.services.backtests.auto_heal_guardrails import evaluate_guardrails
from msai.services.backtests.auto_heal_lock import AutoHealLock, build_lock_key
from msai.services.backtests.derive_asset_class import derive_asset_class
from msai.services.nautilus.catalog_builder import verify_catalog_coverage

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

log = get_logger(__name__)


class AutoHealOutcome(StrEnum):
    """Terminal state of a single auto-heal cycle."""

    SUCCESS = "success"
    GUARDRAIL_REJECTED = "guardrail_rejected"
    TIMEOUT = "timeout"
    INGEST_FAILED = "ingest_failed"
    COVERAGE_STILL_MISSING = "coverage_still_missing"


@dataclass(frozen=True, slots=True)
class AutoHealResult:
    """Return value to the caller in ``backtest_job.py``."""

    outcome: AutoHealOutcome
    asset_class: str
    resolved_instrument_ids: list[str] | None
    reason_human: str | None
    gaps: list[tuple[str, list[tuple[int, int]]]] | None = None


async def run_auto_heal(
    *,
    backtest_id: str,
    instruments: list[str],
    start: "date",
    end: "date",
    catalog_root: "Path",
    caller_asset_class_hint: str | None = None,
) -> AutoHealResult:
    """Run one bounded-lazy auto-heal cycle.

    On SUCCESS the caller re-enters the backtest execution body once.
    On any other outcome the caller falls through to the PR-#39
    ``_mark_backtest_failed`` path.
    """
    structlog.contextvars.bind_contextvars(backtest_id=backtest_id)

    # 1. Derive asset_class server-side (closes PR #39 scope-defer)
    registry = _get_security_master()
    asset_class = (
        derive_asset_class(instruments, start=start, registry=registry)
        or caller_asset_class_hint
        or "stocks"
    )

    log.info(
        "backtest_auto_heal_started",
        symbols=instruments,
        asset_class=asset_class,
        start=start.isoformat(),
        end=end.isoformat(),
    )

    # 2. Guardrail evaluation
    guardrails = evaluate_guardrails(
        asset_class=asset_class,
        symbols=instruments,
        start=start,
        end=end,
        max_years=settings.auto_heal_max_years,
        max_symbols=settings.auto_heal_max_symbols,
        allow_options=settings.auto_heal_allow_options,
    )
    if not guardrails.allowed:
        log.info(
            "backtest_auto_heal_guardrail_rejected",
            reason=guardrails.reason,
            details=guardrails.details,
        )
        return AutoHealResult(
            outcome=AutoHealOutcome.GUARDRAIL_REJECTED,
            asset_class=asset_class,
            resolved_instrument_ids=None,
            reason_human=guardrails.human_message,
        )

    # 3. Dedupe lock + ingest enqueue + phase transition
    pool = await get_redis_pool()
    lock = AutoHealLock(pool)
    lock_key = build_lock_key(
        asset_class=asset_class, symbols=instruments, start=start, end=end,
    )
    holder_id = f"{backtest_id}:{uuid4().hex[:8]}"

    acquired = await lock.try_acquire(
        lock_key, ttl_s=settings.auto_heal_lock_ttl_seconds, holder_id=holder_id,
    )
    ingest_job_id: str | None = None
    dedupe_result = "acquired"

    try:
        if acquired:
            job = await pool.enqueue_job(
                "run_ingest",
                asset_class=asset_class,
                symbols=instruments,
                start=start.isoformat(),
                end=end.isoformat(),
                _queue_name=settings.ingest_queue_name,
            )
            ingest_job_id = job.job_id if job else None
        else:
            existing_holder = await lock.get_holder(lock_key)
            dedupe_result = f"wait_for_existing_holder:{existing_holder}"

        log.info(
            "backtest_auto_heal_ingest_enqueued",
            ingest_job_id=ingest_job_id,
            lock_key=lock_key,
            dedupe_result=dedupe_result,
        )

        await _set_backtest_phase(
            backtest_id=backtest_id,
            phase="awaiting_data",
            progress_message=f"Downloading {asset_class} data for {', '.join(instruments[:3])}"
            + ("..." if len(instruments) > 3 else ""),
            heal_started_at=datetime.now(UTC),
            heal_job_id=ingest_job_id,
        )

        # 4. Poll wall-clock cap
        cap = settings.auto_heal_wall_clock_cap_seconds
        interval = settings.auto_heal_poll_interval_seconds
        deadline = time.monotonic() + cap
        ingest_ok = False
        ingest_exc: str | None = None

        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            holder = await lock.get_holder(lock_key)
            if holder is None:
                # Lock cleared — either dedupe holder finished or TTL expired.
                # Proceed to coverage re-check.
                ingest_ok = True
                break

        if not ingest_ok:
            log.warning(
                "backtest_auto_heal_timeout",
                wall_clock_seconds=cap,
                ingest_job_id_still_running=ingest_job_id,
            )
            return AutoHealResult(
                outcome=AutoHealOutcome.TIMEOUT,
                asset_class=asset_class,
                resolved_instrument_ids=None,
                reason_human=f"Data download exceeded {cap // 60}-minute cap.",
            )

        log.info(
            "backtest_auto_heal_ingest_completed",
            ingest_duration_seconds=int(time.monotonic() - (deadline - cap)),
        )

        # 5. Coverage re-check (Nautilus native)
        # Map user-submitted symbols to canonical instrument IDs via the
        # registry so get_missing_intervals_for_request looks up the right
        # BarType directory.
        resolved_ids = [_to_canonical(registry, s, start=start) for s in instruments]
        gaps = verify_catalog_coverage(
            catalog_root=catalog_root,
            instrument_ids=resolved_ids,
            start=start,
            end=end,
        )
        any_gap = any(len(g) > 0 for _, g in gaps)
        if any_gap:
            log.warning(
                "backtest_auto_heal_coverage_still_missing",
                gaps=[
                    {"instrument_id": iid, "gap_count": len(g)} for iid, g in gaps
                ],
            )
            return AutoHealResult(
                outcome=AutoHealOutcome.COVERAGE_STILL_MISSING,
                asset_class=asset_class,
                resolved_instrument_ids=resolved_ids,
                reason_human="Provider returned data for a narrower range than requested.",
                gaps=gaps,
            )

        log.info("backtest_auto_heal_completed", outcome="success")
        return AutoHealResult(
            outcome=AutoHealOutcome.SUCCESS,
            asset_class=asset_class,
            resolved_instrument_ids=resolved_ids,
            reason_human=None,
        )

    finally:
        if acquired:
            await lock.release(lock_key, holder_id=holder_id)
        # Always clear phase — caller transitions to terminal state next.
        await _set_backtest_phase(
            backtest_id=backtest_id,
            phase=None,
            progress_message=None,
            heal_started_at=None,
            heal_job_id=None,
        )
        structlog.contextvars.unbind_contextvars("backtest_id")


async def _set_backtest_phase(
    *,
    backtest_id: str,
    phase: str | None,
    progress_message: str | None,
    heal_started_at: datetime | None,
    heal_job_id: str | None,
) -> None:
    """Atomically update the 4 auto-heal columns on the backtest row."""
    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.phase = phase
            row.progress_message = progress_message
            if heal_started_at is not None:
                row.heal_started_at = heal_started_at
            if heal_job_id is not None:
                row.heal_job_id = heal_job_id
            if phase is None:
                row.heal_started_at = None
                row.heal_job_id = None
            await session.commit()
    except Exception:
        log.exception("backtest_auto_heal_phase_update_failed")


def _get_security_master() -> Any | None:
    """Lazy factory; returns None if unavailable (test path)."""
    try:
        from msai.services.nautilus.security_master.service import SecurityMaster
        return SecurityMaster()
    except Exception:
        log.warning("security_master_unavailable_in_auto_heal", exc_info=True)
        return None


def _to_canonical(registry: Any | None, symbol: str, *, start: "date") -> str:
    """Best-effort canonical instrument ID; fall back to the symbol as-is."""
    if registry is None:
        return symbol
    try:
        resolved = registry.resolve_for_backtest([symbol], start=start)
        return resolved[0] if resolved else symbol
    except Exception:
        return symbol
```

Note: the poll-via-lock-holder pattern above relies on the ingest worker releasing the dedupe lock AFTER the ingest finishes. The ingest worker is not currently aware of the auto-heal lock — so that assumption is wrong. **Revise:** instead of polling the lock, poll the arq job status directly. Use `arq.jobs.Job(ingest_job_id, pool).status()` with a timeout loop, and rely on the TTL for crashed-holder cleanup. See `arq` docs for `Job.info()` / `Job.result()` patterns.

Also consider: the dedupe lock's value is for _producer-side_ (don't enqueue twice) not _consumer-side_ (worker tracking). Producer-side is sufficient — two concurrent auto-heal callers each acquire the lock; the second loses, sees there's already a holder, and polls job status for the existing `holder_id` by parsing it (holder_id starts with `backtest_id:` but we need the `ingest_job_id` — revise: store `ingest_job_id` in the lock value, not `backtest_id:rand`).

**Revise the lock value contract:** use `holder_id = ingest_job_id` so the non-acquirer can read the lock value and poll the same job. First-acquirer lifecycle: acquire → enqueue → write `job.job_id` to lock → poll → release on success/timeout. The caller knows `existing_holder` is the `ingest_job_id`.

Document this in the `auto_heal.py` module docstring; update tests.

**Step 4: Iterate tests until all pass**

Run: `cd backend && uv run pytest tests/unit/services/backtests/test_auto_heal.py -v`
Expected: all 8 scenarios pass. Iterate on the orchestrator if needed — this is the largest task in the plan, budget 30-60 min.

**Step 5: Integration test (end-to-end against fake arq + fake redis)**

Create `backend/tests/integration/test_auto_heal_end_to_end.py` with one happy-path test that:

- Writes a backtest row (status=running, no phase)
- Calls `run_auto_heal` with a fake pool that returns a synthetic `run_ingest` job_id
- Simulates ingest completion (drop the lock)
- Asserts: backtest row phase goes through `awaiting_data` → cleared; `heal_job_id` set then cleared; `structlog.testing.capture_logs()` sees the 4 happy-path events in order.

**Step 6: Stage**

```bash
git add backend/src/msai/services/backtests/auto_heal.py \
        backend/tests/unit/services/backtests/test_auto_heal.py \
        backend/tests/integration/test_auto_heal_end_to_end.py
```

---

### Task B8: Wire auto-heal into `backtest_job.py`

**Files:**

- Modify: `backend/src/msai/workers/backtest_job.py:207-235` (outer `except` block — intercept FileNotFoundError BEFORE `_mark_backtest_failed`)
- Test: `backend/tests/integration/test_backtest_job_auto_heal.py`

**Step 1: Write failing test**

Create `backend/tests/integration/test_backtest_job_auto_heal.py`:

```python
async def test_backtest_job_invokes_auto_heal_on_missing_data(...):
    """When ensure_catalog_data raises FileNotFoundError, run_auto_heal runs
    once; on SUCCESS the backtest body re-executes and produces completed state.
    """
    # Arrange: seed a backtest row; mock ensure_catalog_data to raise FNF on
    # first call and succeed on second; mock run_auto_heal to return SUCCESS.
    # Act: run_backtest_job
    # Assert: backtest.status == "completed" AND ensure_catalog_data called twice.


async def test_backtest_job_guardrail_rejection_marks_failed_with_envelope(...):
    """When run_auto_heal returns GUARDRAIL_REJECTED, the backtest goes to
    failed status with the PR #39 ErrorEnvelope populated.
    """


async def test_backtest_job_non_missing_data_failure_bypasses_auto_heal(...):
    """A TimeoutError in the subprocess still goes straight to _mark_backtest_failed;
    auto-heal is MISSING_DATA-only.
    """
```

**Step 2: Run, verify fails**

Run: `cd backend && uv run pytest tests/integration/test_backtest_job_auto_heal.py -v`
Expected: FAIL — current `run_backtest_job` has no auto-heal call.

**Step 3: Wire auto-heal**

Refactor `backend/src/msai/workers/backtest_job.py` — extract the try body (lines 103-205) into a helper `_execute_backtest(...)` that takes the already-loaded `backtest_row` + derived locals and runs the catalog-build + subprocess-spawn + finalize path. Then wrap the top-level logic:

```python
# After _start_backtest:
attempt = 0
last_exc: BaseException | None = None
while attempt < 2:
    attempt += 1
    try:
        await _execute_backtest(
            backtest_row=backtest_row,
            backtest_id=backtest_id,
            strategy_path=strategy_path,
            config=config,
            symbols=symbols,
            asset_class=asset_class,
            start_iso=start_iso,
            end_iso=end_iso,
            strategy_id=strategy_id,
            strategy_code_hash=strategy_code_hash,
        )
        return  # success
    except FileNotFoundError as exc:
        if attempt == 1:
            from msai.services.backtests.auto_heal import (
                AutoHealOutcome,
                run_auto_heal,
            )

            result = await run_auto_heal(
                backtest_id=backtest_id,
                instruments=symbols,
                start=backtest_row["start_date"],
                end=backtest_row["end_date"],
                catalog_root=settings.nautilus_catalog_root,
                caller_asset_class_hint=asset_class,
            )
            if result.outcome == AutoHealOutcome.SUCCESS:
                # Re-enter the execution body with healed data.
                last_exc = None
                continue
            # Translate non-success outcome to a typed exception for the
            # _mark_backtest_failed path below. Reuse FileNotFoundError so
            # classifier continues to tag as MISSING_DATA, with the outcome's
            # human message overlaid on the envelope.
            last_exc = FileNotFoundError(result.reason_human or "Auto-heal failed")
            break
        last_exc = exc
        break
    except Exception as exc:
        last_exc = exc
        break

if last_exc is not None:
    await _handle_terminal_failure(backtest_id, symbols, asset_class, backtest_row, last_exc)
```

And `_handle_terminal_failure` is the existing structured-log + `_mark_backtest_failed` block lifted out.

**Step 4: Run tests**

Run: `cd backend && uv run pytest tests/integration/test_backtest_job_auto_heal.py -v`
Expected: all pass.

**Step 5: Stage**

```bash
git add backend/src/msai/workers/backtest_job.py \
        backend/tests/integration/test_backtest_job_auto_heal.py
```

---

### Task B9: Extend `BacktestStatusResponse` schema + API handler

**Files:**

- Modify: `backend/src/msai/schemas/backtest.py:25-35` (add phase + progress_message)
- Modify: `backend/src/msai/api/backtests.py:321-330` (populate phase + progress_message in GET /status handler)
- Test: `backend/tests/unit/schemas/test_backtest_schemas.py`
- Test: `backend/tests/integration/test_backtests_api.py` (append new assertions)

**Step 1: Write failing schema test**

Append to `backend/tests/unit/schemas/test_backtest_schemas.py`:

```python
def test_backtest_status_response_accepts_phase_and_progress_message() -> None:
    from msai.schemas.backtest import BacktestStatusResponse
    resp = BacktestStatusResponse(
        id="00000000-0000-0000-0000-000000000001",
        status="running",
        progress=50,
        started_at=None,
        completed_at=None,
        phase="awaiting_data",
        progress_message="Downloading AAPL...",
    )
    assert resp.phase == "awaiting_data"
    assert resp.progress_message == "Downloading AAPL..."


def test_backtest_status_response_rejects_unknown_phase() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BacktestStatusResponse(
            id="00000000-0000-0000-0000-000000000001",
            status="running",
            progress=50,
            started_at=None,
            completed_at=None,
            phase="bogus",
            progress_message=None,
        )
```

**Step 2: Run, verify fails**

Expected: FAIL — schema doesn't accept those fields.

**Step 3: Update schema**

Edit `backend/src/msai/schemas/backtest.py:25-35`:

```python
class BacktestStatusResponse(BaseModel):
    """Response schema for backtest status polling."""

    id: UUID
    status: str
    progress: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: ErrorEnvelope | None = None
    # --- Auto-heal lifecycle (added by PR #<this>) ---
    # When an auto-heal cycle is in flight, ``phase`` is
    # ``"awaiting_data"`` and ``progress_message`` carries the user-facing
    # "Downloading ..." text. Both are ``None`` outside heal windows.
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None

    model_config = {"from_attributes": True}
```

**Step 4: Update API handler**

Edit `backend/src/msai/api/backtests.py:321-330` (the `/status` endpoint handler): populate `phase` + `progress_message` from the row. Also update the POST `/run` endpoint (line 295) which constructs an initial `BacktestStatusResponse` — leave `phase=None` + `progress_message=None` there (initial state).

Because both endpoints already use `response_model_exclude_none=True`, absent phase/progress stay absent — preserves backward compat with PR #39 callers.

**Step 5: Run tests**

Run: `cd backend && uv run pytest tests/unit/schemas/ tests/integration/test_backtests_api.py -v`
Expected: all pass.

**Step 6: Stage**

```bash
git add backend/src/msai/schemas/backtest.py \
        backend/src/msai/api/backtests.py \
        backend/tests/unit/schemas/test_backtest_schemas.py \
        backend/tests/integration/test_backtests_api.py
```

---

### Task F1: Typed API client — add `phase` + `progress_message` to TS types

**Files:**

- Modify: `frontend/src/lib/api.ts` (extend `BacktestStatusResponse` type)

**Step 1: Extend TS types**

Find the `BacktestStatusResponse` interface/type in `frontend/src/lib/api.ts` and add:

```typescript
export interface BacktestStatusResponse {
  // existing fields...
  phase?: "awaiting_data" | null;
  progress_message?: string | null;
}
```

(Optional fields — older responses without the keys still parse.)

**Step 2: Typecheck**

Run: `cd frontend && pnpm exec tsc --noEmit`
Expected: PASS (no new errors).

**Step 3: Stage**

```bash
git add frontend/src/lib/api.ts
```

---

### Task F2: UI subtle indicator on `/backtests/{id}` detail + list-page badge

**Files:**

- Modify: `frontend/src/app/backtests/[id]/page.tsx`
- Modify: `frontend/src/app/backtests/page.tsx`

**Step 1: Detail page indicator**

Edit `frontend/src/app/backtests/[id]/page.tsx` — in the "status block" renderer (where the existing running spinner appears), add:

```tsx
{
  status.phase === "awaiting_data" && (
    <div
      data-testid="backtest-phase-indicator"
      className="mt-1 flex items-center gap-2 text-sm text-muted-foreground"
    >
      <Loader2 className="h-3 w-3 animate-spin" />
      <span data-testid="backtest-phase-message">
        {status.progress_message || "Downloading data…"}
      </span>
    </div>
  );
}
```

**Step 2: List page badge**

Edit `frontend/src/app/backtests/page.tsx` — in the Status cell of the row table, render a compact badge alongside the "Running" badge when `row.phase === "awaiting_data"`:

```tsx
{
  row.status === "running" && row.phase === "awaiting_data" && (
    <Badge
      data-testid="backtest-list-fetching-badge"
      variant="outline"
      className="ml-1 text-xs"
    >
      Fetching data…
    </Badge>
  );
}
```

Note: the list endpoint (`GET /backtests/history`) returns `BacktestListItem`, not `BacktestStatusResponse` — check the current shape and either add `phase` to `BacktestListItem` too, or have the UI call `/status` lazily on hover (too much). Simpler: extend `BacktestListItem` in the schema similarly to how `error_code` + `error_public_message` were added in PR #39. This adds a small backend change — update task B9 scope to include it.

**Step 3: Manual smoke test**

Run: `docker compose -f docker-compose.dev.yml up -d` → open `http://localhost:3300/backtests/<id>` for a backtest currently in `awaiting_data` state. Should see the indicator. Then reload — persistent.

**Step 4: Stage**

```bash
git add frontend/src/app/backtests/[id]/page.tsx \
        frontend/src/app/backtests/page.tsx
```

---

## Phase 3.2b: E2E Use Cases

Staged in this plan. Graduated to `tests/e2e/use-cases/backtests/auto-ingest.md` in Phase 6.2b.

### UC-BAI-001 — Happy path auto-heal (API)

**Intent:** Agent submits a backtest for a cold symbol; platform heals transparently; agent sees `status=completed` with metrics, no error envelope.

**Interface:** API-first.

**Setup (ARRANGE):**

- Authenticate via dev `X-API-Key`.
- Ensure `{DATA_ROOT}/parquet/stocks/SOMETHING_COLD/**` is empty (pick a symbol not previously ingested).
- Register a known-good strategy file.

**Steps:**

1. `POST /api/v1/backtests/run` with `instruments=["XYZ.NASDAQ"]`, `start=2024-01-01`, `end=2024-06-30`, valid strategy id.
2. Poll `GET /api/v1/backtests/{id}/status` every 10s.
3. Within 3 polls, expect `status=running` + `phase=awaiting_data` + non-empty `progress_message`.
4. Within 30 minutes, expect `status=completed` with `metrics.num_trades >= 0`.
5. Confirm `error` key is absent (response_model_exclude_none).

**Verification:**

- API: status transitions observed; metrics present; no error envelope.
- Persistence: final state persists after `GET /backtests/{id}/status` repeat.

**Expected failure modes:**

- Provider is up and data exists → PASS.
- Provider rate limit → FAIL_INFRA (retry once).
- Cold symbol truly doesn't exist → UC-BAI-006 applies instead.

---

### UC-BAI-002 — Guardrail rejection: 11-year range (API)

**Intent:** Request outside the 10-year cap fails immediately with the PR #39-style envelope; no auto-heal attempted.

**Setup:** authenticated; any cold symbol works.

**Steps:**

1. `POST /backtests/run` with `start=2013-01-01, end=2024-12-31` (~12 years).
2. Poll `/status`.

**Verification:**

- Expect `status=failed` within 2 minutes.
- `error.code == "missing_data"`.
- `error.message` mentions "year" and the cap "10".
- `error.remediation.auto_available == false`.
- `error.suggested_action` starts with "Run: msai ingest ...".
- No `backtest_auto_heal_ingest_enqueued` log event fired (guardrail short-circuits before enqueue).

---

### UC-BAI-003 — Server-side asset_class derivation (API)

**Intent:** Futures symbol submitted via API (no `config.asset_class`) should route auto-heal to futures provider (Databento) and produce correct `msai ingest futures ...` remediation.

**Setup:** authenticated; symbol `ES.n.0`.

**Steps:**

1. `POST /backtests/run` with `instruments=["ES.n.0"]` + `config` that does NOT set `asset_class`.
2. Wait for either success or guardrail-expected failure.
3. If `status=failed`, inspect `error.suggested_action`.

**Verification:**

- `suggested_action` contains `msai ingest futures` (not `msai ingest stocks`).
- Remediation envelope's `asset_class == "futures"`.

---

### UC-BAI-004 — Concurrent dedupe (API)

**Intent:** Two backtests for the same cold symbol/range within 5s trigger only one ingest.

**Setup:** authenticated; cold symbol `XYZ.NASDAQ`.

**Steps:**

1. Within 3 seconds, POST two backtests, same instruments+range.
2. Poll both statuses.

**Verification:**

- Both eventually `completed` (or both fail for same reason).
- Only ONE `backtest_auto_heal_ingest_enqueued` structured log event has `dedupe_result=acquired`; the second has `dedupe_result=wait_for_existing_holder:...`.
- Provider API call count = 1 (if inspectable via logs).

---

### UC-BAI-005 — UI progress indicator + persistence (UI)

**Intent:** Human opens detail page during a heal; sees indicator; reload preserves state; after completion, indicator gone and metrics visible.

**Setup:** start a UC-BAI-001-style backtest via the UI Run form.

**Steps:**

1. Navigate to `/backtests/{id}`.
2. Wait for `phase=awaiting_data` (may need to refresh once if the first poll happens too fast).
3. Verify `data-testid="backtest-phase-indicator"` is visible.
4. Verify `data-testid="backtest-phase-message"` text matches the progress_message (e.g., "Downloading stocks data for XYZ...").
5. `page.reload()`.
6. Verify indicator still visible.
7. Wait for completion.
8. Verify indicator is gone; metrics card visible.

**Verification:** Playwright MCP-driven.

---

## Validation Checklist (from `/prd:create`)

- [x] Clear overview — "When a backtest fails with MISSING_DATA, auto-heal re-runs transparently."
- [x] At least 1 user story with Gherkin scenario — 7 stories, all have Gherkin.
- [x] Acceptance criteria for every story — each has 5-10 bullets.
- [x] Edge cases documented — per-story table + cross-story.
- [x] Explicit non-goals — 9 items.
- [x] Success metrics with targets — 6 metrics with measurement method.
- [x] Technical constraints listed — `BacktestStatus` column, arq config, `ensure_catalog_data` semantics, options hard-reject.
- [x] Security considerations addressed — `sanitize_public_message`, structured log field hygiene, env-only guardrail config, TTL-bounded lock.
- [x] No TBD or placeholder text.

---

## Plan Review History

### Iter-1 (2026-04-21) — Claude + Codex in parallel

**Verdict:** PLAN NEEDS REVISION. Findings tagged `[iter-1 P0/P1/P2]` in-place in the tasks below. 2 P0 + 5 P1 + 3 P2 caught before implementation.

**P0 — all applied in-place:**

- **[iter-1 P0-a]** `SecurityMaster.__init__` requires mandatory kwarg `db: AsyncSession` (`backend/src/msai/services/nautilus/security_master/service.py:109-116`); `resolve_for_backtest` is `async` (line 343). B3 and B7 rewritten to use an `async` factory that opens a session via `async_session_factory()` and `await`s the registry calls. All mocks become `AsyncMock`.
- **[iter-1 P0-b]** The original orchestrator poll loop watched the dedupe lock but the ingest worker never releases it — caller would block until TTL. B7 rewritten: store `job.job_id` as lock value AND poll `arq.jobs.Job(ingest_job_id, pool).status()` until it reaches `JobStatus.complete` / `not_found`. Verified arq API: `Job(job_id, redis, _queue_name="msai:ingest")` + `await job.status() -> JobStatus` + `JobStatus.{deferred,queued,in_progress,complete,not_found}`.
- **[iter-1 P0-c]** Orchestrator called `pool.enqueue_job(...)` directly bypassing `enqueue_ingest` — reintroduced the queue-routing bug B2 fixes. Revised `enqueue_ingest` signature to return the arq `Job` object (matching `enqueue_backtest`/`enqueue_portfolio_run`'s pattern of returning `job.job_id`). Orchestrator now calls `enqueue_ingest` and gets the job id back.

**P1 — all applied in-place:**

- **[iter-1 P1-a]** `fakeredis[aio]` is NOT in `backend/pyproject.toml` dev deps. Added Step 0 to B4: add `fakeredis[aio]>=2.20` under `[project.optional-dependencies].dev` + `uv sync`. Alternative path documented: use `testcontainers.redis.RedisContainer` (already in dev deps) and tag tests `integration`.
- **[iter-1 P1-b]** Existing `backend/tests/unit/test_queue.py:77-99` and `:101-129` use strict `assert_awaited_once_with(...)` without `_queue_name`. Updating B2 to fix the existing tests (not just add a new one).
- **[iter-1 P1-c]** `_start_backtest` at `workers/backtest_job.py:257-273` unconditionally increments `attempt` and flips `status → running` + resets `started_at` + `heartbeat_at`. On second attempt the re-entry would double-count. B8 refactored: the retry loop calls `_execute_backtest(backtest_row, ...)` with the already-loaded snapshot from the FIRST `_start_backtest` call — no second `_start_backtest` call on success-after-heal. `attempt` increments once.
- **[iter-1 P1-d]** `frontend/src/app/backtests/[id]/page.tsx` currently loads once on mount via one `fetch` — does not poll. F2 extended: add a `useEffect` polling loop (3s interval) that re-fetches `/status` while `status ∈ {pending, running}`, stops on terminal states. Cleanup on unmount.
- **[iter-1 P1-e]** `frontend/src/app/backtests/page.tsx` row-navigation link only renders for `status ∈ {completed, failed}`. Without fix, users can't click into a `running+awaiting_data` row to see the detail phase indicator. F2 extended: make the "View details" ExternalLink render for `running` rows too.

**P2 — all applied in-place:**

- **[iter-1 P2-a]** Bare `except Exception:` in `_get_security_master` + `derive_asset_class` + `_to_canonical` violates `.claude/rules/python-style.md` rule 4. Updated to `log.warning(..., exc_info=True)` at every catch site.
- **[iter-1 P2-b]** `verify_catalog_coverage` end_ns computed as `23:59:59` risks a 1-second false-gap. Revised to `end_ns = int((datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)).timestamp() * 1e9) - 1` (end-of-day inclusive, nanosecond-precise). Added a unit test that seeds a bar at `23:59:59.999` on the end date and asserts zero gaps.
- **[iter-1 P2-c]** `BacktestListItem.phase` was buried in an F2 footnote — will silently cause the list-page badge to never render. Promoted to B9 explicit scope: add `phase: Literal["awaiting_data"] | None = None` to `BacktestListItem`, populate in `list_backtests` endpoint, add integration-test assertion.

**Open-decision resolutions (all 6 author-flagged items resolved):**

1. Poll strategy: arq job status polling via `arq.jobs.Job` — locked.
2. Heartbeat during heal: continues firing via `asyncio.create_task` at outer level (before `_execute_backtest`); `asyncio.sleep` in poll loop yields to event loop — locked.
3. `attempt` double-increment: fixed via P1-c above (single-increment guarantee via snapshot reuse) — locked.
4. Dual `run_ingest` registration: keep on both `WorkerSettings.functions` + `IngestWorkerSettings.functions` for one deploy cycle, cleanup PR drops from default queue — locked. Added comment marker in `WorkerSettings` noting removal in follow-up.
5. `BacktestListItem.phase`: option (a) — promoted to B9 (see P2-c above) — locked.
6. ns-precision end-date: fixed via P2-b above — locked.

### Iter-2 (2026-04-21) — Claude + Codex in parallel

**Verdict:** PLAN NEEDS REVISION. Findings tagged `[iter-2 P0/P1/P2]`. 1 P0 + 3 P1 + 3 P2.

**P0 — applied:**

- **[iter-2 P0-a]** REV F2 `apiGet` call passes an object `{ token: await getToken() }` where the actual signature is `apiGet<T>(path: string, token?: string | null)` (see `frontend/src/lib/api.ts:44`). A non-null object is truthy → `Authorization: Bearer [object Object]` → every poll 401s. Both reviewers flagged this. **Fix:** fetch token first, pass the string — matches the existing pattern at `frontend/src/components/data/ingestion-status.tsx:35` and the initial-load call in the same file. See REV F2-v2 below.

**P1 — applied:**

- **[iter-2 P1-a]** `asset_class_for_alias` stub would naively return `spec.asset_class` which is registry-taxonomy (`"equity"` / `"future"` / `"option"` / `"forex"`) — but ingest-taxonomy is `"stocks"` / `"futures"` / `"options"` / `"forex"`. A mismatch would route Parquet writes to `data/parquet/equity/` while the catalog reader expects `stocks/` — perpetual re-heal loop. **Fix:** add explicit taxonomy map in the method body. See REV B3-v2 below.

- **[iter-2 P1-b]** REV B7 placeholder→job_id handoff is an unconditional `pool.set(lock_key, ingest_job_id, ex=...)` — if the placeholder TTL expired between acquire and swap, a second caller could have acquired a fresh lock; the first caller would then overwrite that second holder's lock with a stale `job_id`. **Fix:** replace the unconditional `set` with a Lua script that compares-and-swaps only if the current value matches the placeholder. `fakeredis[lua]` already planned for this (REV B4). See REV B7-v2 below for the Lua snippet.

- **[iter-2 P1-c]** REV B8 wraps every `AutoHealOutcome != SUCCESS` as `FileNotFoundError(result.reason_human)`. But `_mark_backtest_failed` → `classify_worker_failure` keys on `isinstance(exc, FileNotFoundError)` (classifier.py:96), so a TIMEOUT or INGEST_FAILED outcome gets classified as MISSING_DATA — the wrong FailureCode, wrong remediation. **Fix:** map each non-SUCCESS outcome to a distinct exception type:
  - `GUARDRAIL_REJECTED` → `FileNotFoundError` (stays MISSING_DATA; envelope shows guardrail message; acceptable because `Remediation.auto_available` is False so no re-heal loop)
  - `COVERAGE_STILL_MISSING` → `FileNotFoundError` (stays MISSING_DATA with specific-gap remediation)
  - `TIMEOUT` → `TimeoutError` (classifier returns `FailureCode.TIMEOUT`)
  - `INGEST_FAILED` → `RuntimeError` (classifier returns `FailureCode.ENGINE_CRASH`)

  See REV B8-v2 below.

**P2 — applied:**

- **[iter-2 P2-a]** REV B7's `JobStatus.not_found` branch: "treat as completed (best effort)" + break out of the poll loop. But the following `await ingest_job.result(timeout=5.0)` raises `ResultNotFound` exactly when status was `not_found` — downgrading the SUCCESS path to INGEST_FAILED. **Fix:** split the two paths — on `not_found`, skip `result()` entirely and go straight to coverage re-check (the catalog is the source of truth for whether data landed). See REV B7-v2.

- **[iter-2 P2-b]** REV B9 extended backend `BacktestListItem` but the frontend TS type `BacktestHistoryItem` at `frontend/src/lib/api.ts:167` was not extended. Without it, F2 list-page badge has no typed source. **Fix:** add `phase?: "awaiting_data" | null` + `progress_message?: string | null` to `BacktestHistoryItem`. Also ensure F2 consumes the new fields. See REV B9-v2 + REV F2-v2 below.

- **[iter-2 P2-c]** REV B2 text says iter-1 fixes are "applied in-place," which could mislead a subagent into thinking `backend/tests/unit/test_queue.py` has already been updated on disk. It has not — the test updates happen during B2 implementation (TDD: update the old tests + add the new one in the same task). **Fix:** clarify the framing. See REV B2-v2 below.

### Iter-2 Definitive Revisions

These supersede iter-1's definitive code for the affected tasks.

---

#### REV B2-v2 — clarified test-update sequencing

Task B2 implementation steps (in this order):

1. Add the new test `test_enqueue_ingest_routes_to_ingest_queue` (red — fails because no `_queue_name` kwarg yet).
2. Update the two EXISTING tests at `backend/tests/unit/test_queue.py:77-99` and `:101-129` so their `assert_awaited_once_with(...)` includes `_queue_name=settings.ingest_queue_name` (they are currently green; updating now keeps them red-until-fix).
3. Modify `enqueue_ingest` to pass `_queue_name=` + return `Job | None`.
4. Register `run_ingest` on `IngestWorkerSettings.functions`.
5. All three tests pass.

These files are ON-DISK unchanged at iter-2 start; don't assume otherwise.

---

#### REV B3-v2 — `asset_class_for_alias` with explicit taxonomy mapping

```python
def asset_class_for_alias(self, alias_str: str) -> str | None:
    """Canonical alias → ingest-taxonomy asset_class.

    Translates registry taxonomy (``"equity"`` / ``"future"`` /
    ``"option"`` / ``"forex"``) to the ingest / Parquet-storage
    taxonomy (``"stocks"`` / ``"futures"`` / ``"options"`` /
    ``"forex"``). This mapping is critical — if the wrong name
    reaches ``DataIngestionService._resolve_plan`` the Parquet
    writes go to the wrong directory and the subsequent catalog
    re-check fails, producing a perpetual auto-heal loop.

    Returns ``None`` if the alias shape is not recognized — caller
    falls back to the shape heuristic in
    ``derive_asset_class_sync``.
    """
    try:
        spec = self._spec_from_canonical(alias_str)
    except Exception:  # noqa: BLE001
        return None

    _REGISTRY_TO_INGEST = {
        "equity": "stocks",
        "future": "futures",
        "option": "options",
        "forex": "forex",
        "crypto": "crypto",
    }
    registry_taxon = getattr(spec, "asset_class", None)
    if registry_taxon is None:
        return None
    # Unknown taxonomy passes through unchanged — operator can still
    # see it and decide; tests parametrize each known key.
    return _REGISTRY_TO_INGEST.get(registry_taxon, registry_taxon)
```

Add a parametrized unit test: each of `{"equity", "future", "option", "forex"}` → `{"stocks", "futures", "options", "forex"}`. Plus one test for an unknown-taxonomy pass-through behavior.

---

#### REV B7-v2 — Lua CAS on lock value + `not_found`-skips-result

```python
# Lua script: compare-and-swap the lock value.
# Returns 1 if the swap succeeded, 0 if the current value no longer
# matches the expected placeholder (i.e., lock expired + someone else
# acquired, OR was already overwritten).
_CAS_LOCK_VALUE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
else
    return 0
end
"""
```

In the acquire-then-publish block:

```python
placeholder = f"reserving:{backtest_id}:{uuid4().hex[:8]}"
acquired = await lock.try_acquire(
    lock_key, ttl_s=settings.auto_heal_lock_ttl_seconds, holder_id=placeholder,
)
try:
    if acquired:
        job = await enqueue_ingest(pool=pool, asset_class=asset_class, ...)
        if job is None:
            # Release placeholder so next caller gets a fresh slot, then fail.
            await lock.release(lock_key, holder_id=placeholder)
            return AutoHealResult(outcome=AutoHealOutcome.INGEST_FAILED, ...)
        ingest_job_id = job.job_id

        # Compare-and-swap: only overwrite if WE still hold the placeholder.
        swap_ok = await pool.eval(
            _CAS_LOCK_VALUE_LUA,
            1,
            lock_key,
            placeholder,
            ingest_job_id,
            str(settings.auto_heal_lock_ttl_seconds),
        )
        if int(swap_ok) != 1:
            # Our placeholder expired mid-enqueue; a later caller
            # now owns the lock. Don't overwrite their value.
            # The enqueued ingest still runs (arq doesn't cancel it)
            # — data lands in catalog for future backtests. We fall
            # back to polling by our own job_id.
            log.warning(
                "auto_heal_lock_cas_lost",
                lock_key=lock_key,
                ingest_job_id=ingest_job_id,
            )
        dedupe_result = "acquired"
```

For the poll-loop `not_found` branch, skip `result()` entirely:

```python
while time.monotonic() < deadline:
    await asyncio.sleep(interval)
    status = await ingest_job.status()
    if status == JobStatus.complete:
        # Fetch result to detect worker-side exception.
        try:
            await ingest_job.result(timeout=5.0)
        except Exception:
            log.exception("backtest_auto_heal_ingest_failed", ingest_job_id=ingest_job_id)
            return AutoHealResult(outcome=AutoHealOutcome.INGEST_FAILED, ...)
        break
    if status == JobStatus.not_found:
        # arq retention ejected the result before we polled. The catalog
        # is the source of truth — skip result() (would raise ResultNotFound)
        # and go straight to coverage re-check. If the ingest succeeded,
        # coverage will pass; if it failed silently, coverage will catch it.
        log.info(
            "backtest_auto_heal_ingest_status_not_found_falling_through",
            ingest_job_id=ingest_job_id,
        )
        break
else:
    # loop fell through without break → timeout path
    return AutoHealResult(outcome=AutoHealOutcome.TIMEOUT, ...)
```

Note: `lock.release()` on the finally path needs to accept the current lock value (either placeholder, ingest_job_id, or someone else's) — release only if the current value is one of OUR two known values (placeholder OR ingest_job_id). Do NOT release if it's a stranger's value (race loss):

```python
finally:
    if acquired:
        current = await lock.get_holder(lock_key)
        if current in (placeholder, ingest_job_id):
            await lock.release(lock_key, holder_id=current)
```

---

#### REV B8-v2 — map each outcome to a distinct exception type

Replace the blanket `FileNotFoundError(result.reason_human)` with:

```python
# Map non-SUCCESS outcomes to exception types the classifier
# will re-tag correctly (classifier.py branches on isinstance).
_OUTCOME_TO_EXC: dict[AutoHealOutcome, type[BaseException]] = {
    AutoHealOutcome.GUARDRAIL_REJECTED: FileNotFoundError,   # stays MISSING_DATA
    AutoHealOutcome.COVERAGE_STILL_MISSING: FileNotFoundError,  # stays MISSING_DATA
    AutoHealOutcome.TIMEOUT: TimeoutError,                  # → FailureCode.TIMEOUT
    AutoHealOutcome.INGEST_FAILED: RuntimeError,            # → FailureCode.ENGINE_CRASH
}

...
if result.outcome == AutoHealOutcome.SUCCESS:
    continue
exc_type = _OUTCOME_TO_EXC.get(result.outcome, FileNotFoundError)
terminal_exc = exc_type(result.reason_human or result.outcome.value)
# Preserve the outcome on the exception so downstream code (tests,
# error_message audit trail) can inspect it beyond string-matching:
terminal_exc.auto_heal_outcome = result.outcome
break
```

For `GUARDRAIL_REJECTED` / `COVERAGE_STILL_MISSING` — which stay as MISSING_DATA via FileNotFoundError — the classifier's standard path will produce a Remediation with `auto_available=True` again. We must guard against a second heal attempt on guardrail-rejected rows. The retry-once cap in B8 (`attempt < 2`) already does this: second attempt won't call `run_auto_heal` again. Document this invariant explicitly in the B8 code.

Add a unit test: `test_auto_heal_outcome_translates_to_correct_exception_type` parametrizing each outcome → expected exception type.

---

#### REV B9-v2 — extend `BacktestHistoryItem` TS type + consume in list page

Already in Iter-1 revisions: `BacktestListItem` on the backend + endpoint population. Iter-2 adds: the TS type in `frontend/src/lib/api.ts:167` must also gain:

```typescript
export interface BacktestHistoryItem {
  // ...existing fields (id, strategy_id, status, start_date, end_date, created_at,
  // error_code?, error_public_message?)...
  phase?: "awaiting_data" | null;
  progress_message?: string | null;
}
```

Then in F2's list-page badge:

```tsx
{
  row.status === "running" && row.phase === "awaiting_data" && (
    <Badge
      data-testid="backtest-list-fetching-badge"
      variant="outline"
      className="ml-1 text-xs"
    >
      Fetching data…
    </Badge>
  );
}
```

This was in F2 already but depended on the type; now it typechecks.

---

#### REV F2-v2 — polling useEffect with correct `apiGet` call

```tsx
useEffect(() => {
  let active = true;
  let timerId: ReturnType<typeof setTimeout> | null = null;
  // iter-6 P1 fix: local counter (not React state) — useEffect deps are
  // [id, getToken] only, so we CANNOT use useState(resultsRetryCount)
  // here; poll() would capture a stale closure of the initial value (0)
  // and the exhaustion branch would never fire. A local `let` owned by
  // this effect instance persists across poll() invocations correctly.
  let resultsRetries = 0;

  const poll = async (): Promise<void> => {
    if (!active) return;
    try {
      const token = await getToken(); // iter-2 fix: fetch token first
      const fresh = await apiGet<BacktestStatusResponse>(
        `/api/v1/backtests/${id}/status`,
        token, // iter-2 fix: pass string, not object
      );
      if (!active) return;
      setStatus(fresh);
      // iter-3 P1 + iter-4 P1 + iter-5 P2 + iter-6 P1 fix: fetch /results on
      // running → completed. If /results transiently 404s (race between
      // status-commit and results-commit in the worker), keep polling up to a
      // bounded retry budget — MAX_RESULTS_RETRIES attempts × 3s = 30s wall-clock
      // window. Retry counter is a local `let resultsRetries` declared at the top
      // of this useEffect body (NOT React state — a useState hook would create a
      // stale closure because useEffect deps are [id, getToken] and never re-fire
      // on state updates; see iter-6 fix note below).
      // On success OR budget exhaustion, stop polling; exhaustion leaves
      // status="completed" visible and metrics=null until manual refresh
      // (expected; this is a deeper bug than a UI timeout can paper over, and
      // structured logs will have the backend failure context).
      if (fresh.status === "completed") {
        try {
          const results = await apiGet<BacktestResultsResponse>(
            `/api/v1/backtests/${id}/results`,
            token,
          );
          if (!active) return;
          setResults(results);
          return; // success — stop polling
        } catch {
          if (!active) return;
          // iter-6 P1 fix: local counter instead of React state to avoid stale
          // closure (useState+useEffect deps=[id, getToken] captured initial value,
          // retry check `>= MAX_RESULTS_RETRIES` never tripped → infinite retries).
          if (resultsRetries >= MAX_RESULTS_RETRIES) {
            return; // budget exhausted — stop polling; manual refresh will retry
          }
          resultsRetries += 1;
          timerId = setTimeout(poll, 3000);
          return;
        }
      }
      if (fresh.status === "failed") {
        return; // terminal — stop polling
      }
      if (fresh.status === "pending" || fresh.status === "running") {
        timerId = setTimeout(poll, 3000);
      }
    } catch {
      if (active) timerId = setTimeout(poll, 5000);
    }
  };

  void poll();
  return () => {
    active = false;
    if (timerId !== null) clearTimeout(timerId);
  };
}, [id, getToken]);
```

### Iter-3 (2026-04-21) — Claude + Codex in parallel

**Verdict:** PLAN NEEDS REVISION (minor). Trajectory: iter-1 10 findings → iter-2 7 findings → iter-3 2 findings — productive convergence per feedback memory `feedback_code_review_iteration_discipline`.

**P1 — applied:**

- **[iter-3 P1-a]** (Codex): REV F2-v2 polled only `/status` — never `/results` on running→completed transition. Result: user saw "Completed" but metrics=null until manual refresh. **Fix:** in the poll loop's `completed` branch, `apiGet<BacktestResultsResponse>(/results, token)` + `setResults(...)` before returning. Applied above in the F2-v2 code block (now the definitive iter-3 version). `BacktestResultsResponse` TS type already exists in `frontend/src/lib/api.ts` — no new TS definitions needed.

**P2 — applied:**

- **[iter-3 P2-a]** (Claude): REV B8-v2 set `terminal_exc.auto_heal_outcome = result.outcome` on a built-in exception instance. Runtime-safe (every exception has `__dict__`) but `mypy --strict` rejects it: `error: "FileNotFoundError" has no attribute "auto_heal_outcome"`. **Fix:** drop the attribute assignment. The classifier branches on `isinstance()` only — never reads `auto_heal_outcome`. Audit telemetry already carries the outcome name in `run_auto_heal`'s structured log events (`backtest_auto_heal_timeout`, `backtest_auto_heal_completed outcome=...`, etc.). Tests match on `isinstance(raised, <exc_type>)` + message text.

Revised iter-3 dispatch block (supersedes iter-2):

```python
_OUTCOME_TO_EXC: dict[AutoHealOutcome, type[BaseException]] = {
    AutoHealOutcome.GUARDRAIL_REJECTED: FileNotFoundError,
    AutoHealOutcome.COVERAGE_STILL_MISSING: FileNotFoundError,
    AutoHealOutcome.TIMEOUT: TimeoutError,
    AutoHealOutcome.INGEST_FAILED: RuntimeError,
}

if result.outcome == AutoHealOutcome.SUCCESS:
    continue
exc_cls = _OUTCOME_TO_EXC.get(result.outcome, FileNotFoundError)
terminal_exc = exc_cls(result.reason_human or result.outcome.value)
# iter-3 P2 fix: no ad-hoc attribute on built-in exceptions (mypy --strict);
# outcome is already captured in run_auto_heal's structured log events.
break
```

Both iter-3 fixes are local, additive, and introduce no new surface to re-review. Iter-4 will verify clean.

### Iter-4 (2026-04-21) — Claude + Codex in parallel

**Verdict:** Claude PLAN APPROVED (1 P3 doc-clarity nit, non-blocking); Codex PLAN NEEDS REVISION (1 P1 on the /results fetch). Applying Codex's P1. Trajectory: iter-3 2 findings → iter-4 1 finding. Still converging.

**P1 — applied:**

- **[iter-4 P1-a]** (Codex): The iter-3 fix for F2 polling caught /results fetch failures and then returned — stopping polling regardless. If /results 404s briefly (race between status-flip commit and results-commit in the backend), the detail page would be stuck showing "Completed" with `metrics=null` until a manual refresh. **Fix:** on /results fetch failure while `status === "completed"`, schedule another poll (3s interval); the next iteration retries /results. Only return after metrics are actually set. Updated in the F2-v2 polling block immediately above — the `catch` branch now calls `setTimeout(poll, 3000)` and returns, instead of returning silently.

**P3 — not applied (documentation nit, non-blocking):**

- **[iter-4 P3-a]** (Claude): The section labeled "Iter-1 Task Revisions — Definitive Code" contains the original REV B8 (iter-1 hardcoded-FileNotFoundError dispatch) which has been superseded twice (iter-2 REV B8-v2 map + attribute; iter-3 map without attribute). An implementation agent reading top-to-bottom following supersession notes resolves correctly — latest supersession wins. Not applied to avoid churning the plan's historical record; the iter-3 block's "supersedes iter-2" note plus this P3 acknowledgement is sufficient signal.

Iter-4 fix is a 6-line adjustment to F2-v2 polling. No new surface. Iter-5 will confirm clean.

### Iter-5 (2026-04-21) — Claude + Codex in parallel

**Verdict:** Claude PLAN APPROVED (0 new findings); Codex PLAN NEEDS REVISION (1 P2 — comment/code mismatch). Applying Codex's P2. Trajectory: iter-4 1 finding → iter-5 1 finding.

**P2 — applied:**

- **[iter-5 P2-a]** (Codex): The inline comment in F2-v2 said "polling stops after metrics load OR after a small bounded retry budget," but the actual code only bounded retries by component unmount. Infinite polling on a permanent backend results-commit failure would be possible (though practically subsecond per the race-window analysis). **Fix:** add an explicit bounded retry counter — `MAX_RESULTS_RETRIES = 10` attempts × 3s polling = 30-second wall-clock window after `status=completed` before giving up. On exhaustion, leave `status="completed"` visible with metrics=null; a manual refresh retries. Structured logs on the backend will have the root cause if results commit is permanently broken. Updated F2-v2 polling block immediately above.

Requires ONE module-level constant in the detail-page file:

```tsx
const MAX_RESULTS_RETRIES = 10; // 10 × 3s = 30s wall-clock window
```

No new `useState` hook — the counter is a local `let resultsRetries = 0` inside the `useEffect` body (see iter-6 fix below for why React state was wrong). No functional change to the success path.

Iter-5 fix is an additive 2-line change. No new surface.

### Iter-6 (2026-04-21) — Claude + Codex in parallel

**Verdict:** Both PLAN NEEDS REVISION (same finding — iter-5 fix had a stale React closure bug). Applied. Trajectory: iter-5 1 → iter-6 1. Same-severity narrow finding; the iter-5 attempt created a new bug in fixing the previous one — per feedback memory `feedback_code_review_iteration_discipline`, a single occurrence of fix-introduces-new-bug is acceptable; now fixed.

**P1 — applied (both reviewers caught the same issue):**

- **[iter-6 P1-a]** The iter-5 `useState` + `setResultsRetryCount` approach was a stale-closure trap. `useEffect(..., [id, getToken])` captures `resultsRetryCount === 0` at mount; `setResultsRetryCount(resultsRetryCount + 1)` schedules a re-render BUT the effect doesn't re-fire (deps unchanged); `poll` keeps the closure reference; the check `>= MAX_RESULTS_RETRIES` always sees `0` and never trips. Infinite retries were still possible. **Fix:** replace React state with a local `let resultsRetries = 0` declared at the top of the useEffect body. Every `poll()` invocation sees the current mutated value; the check works; useEffect deps remain `[id, getToken]` so the polling loop is not torn down on state changes. The `useState` hook from iter-5 is removed entirely. `MAX_RESULTS_RETRIES = 10` remains at module scope.

The F2-v2 polling code block above and the "Requires ONE module-level constant" note are both updated. Iter-5's state-hook requirement is rescinded.

Iter-6 fix is a direct one-for-one replacement of state with local binding. No new surface. Iter-7 will confirm clean.

### Iter-7 (2026-04-21) — Claude + Codex in parallel

**Verdict:** Codex PLAN APPROVED (0 findings); Claude PLAN NEEDS REVISION (1 P2 — stale comment in F2-v2 code block still said "Retry counter stored in `resultsRetryCount` state" even though iter-6 switched to a local `let resultsRetries`). Applied a comment-only fix. No new surface.

**P2 — applied:**

- **[iter-7 P2-a]** Comment inside the F2-v2 code block at the top of the `completed` branch referenced `resultsRetryCount` state after iter-6 moved the counter to a local `let`. An implementation agent reading the stale comment could reintroduce `useState` — exactly the bug iter-6 eliminated. **Fix:** updated the comment to explicitly say "Retry counter is a local `let resultsRetries` declared at the top of this useEffect body (NOT React state — a useState hook would create a stale closure...)". Code unchanged.

Trajectory: iter-6 1 finding → iter-7 1 finding (comment-only). Iter-8 should be clean.

### Iter-8 (2026-04-21) — Claude + Codex in parallel

**Verdict:** BOTH reviewers PLAN APPROVED. 0 findings. Iter-7 comment fix verified clean. **Plan review loop closes here.** Trajectory iter-1 10 → iter-2 7 → iter-3 2 → iter-4 1 → iter-5 1 → iter-6 1 → iter-7 1 → iter-8 0. 8 iterations; monotonically-narrowing convergence per feedback memory `feedback_code_review_iteration_discipline`. Foundation correct throughout — every late-iter finding was a narrow refinement on top of a valid structure (UI polling race → bounded retry → stale closure → comment correctness), never a foundation-level regression.

**Plan is ready for Phase 4 (TDD execution via subagent-driven-development).**

### Iter-1 Task Revisions — Definitive Code

The following replaces the corresponding code blocks in B2–B9 + F2. Implementation agents MUST use these versions, not the original drafts above.

---

#### REV B2 — `enqueue_ingest` returns the Job; existing tests updated

**File:** `backend/src/msai/core/queue.py` — change signature to return `arq.Job | None`:

```python
async def enqueue_ingest(
    pool: ArqRedis,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    *,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> "Job | None":
    """Enqueue ``run_ingest`` on the dedicated ``msai:ingest`` queue.

    Returns the ``arq.Job`` handle so callers can poll status via
    ``Job.status()``. Returns ``None`` iff arq declined to enqueue
    (e.g., job-id collision).
    """
    from msai.core.config import settings as _settings

    return await pool.enqueue_job(
        "run_ingest",
        asset_class=asset_class,
        symbols=symbols,
        start=start,
        end=end,
        provider=provider,
        dataset=dataset,
        schema=schema,
        _queue_name=_settings.ingest_queue_name,
    )
```

Add `from arq.jobs import Job` import under `TYPE_CHECKING` at the top.

**Test updates:** `backend/tests/unit/test_queue.py:90-99` and `:120-129` — replace the assertion kwargs to include `_queue_name=settings.ingest_queue_name`:

```python
pool.enqueue_job.assert_awaited_once_with(
    "run_ingest",
    asset_class=asset_class,
    symbols=symbols,
    start=start,
    end=end,
    provider="auto",
    dataset=None,
    schema=None,
    _queue_name=settings.ingest_queue_name,
)
```

Also, since `enqueue_ingest` no longer returns `None`, update both tests to assert the return type is a Job / awaitable (or simply `is not None`). Add a THIRD test asserting the routing kwarg specifically.

---

#### REV B3 — async `derive_asset_class` + SecurityMaster extension

**File `backend/src/msai/services/backtests/derive_asset_class.py`:**

```python
"""Server-authoritative asset_class derivation. [iter-1 P0-a — async.]"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

_FUTURES_PATTERNS = (
    re.compile(r"\.n\.0$"),
    re.compile(r"\.CME$"),
    re.compile(r"\.GLBX$"),
    re.compile(r"\.XCME$"),
    re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d\."),
)
_FOREX_PATTERNS = (re.compile(r"/.+\."),)
_OPTIONS_PATTERNS = (re.compile(r"\.OPRA$"),)
_STOCKS_PATTERNS = (
    re.compile(r"\.NASDAQ$"),
    re.compile(r"\.ARCA$"),
    re.compile(r"\.NYSE$"),
    re.compile(r"\.XNAS$"),
    re.compile(r"\.BATS$"),
)


def derive_asset_class_sync(symbols: list[str]) -> str:
    """Shape-only derivation — safe in any context (no DB access)."""
    if not symbols:
        return "stocks"
    first = symbols[0]
    for pattern in _OPTIONS_PATTERNS:
        if pattern.search(first):
            return "options"
    for pattern in _FUTURES_PATTERNS:
        if pattern.search(first):
            return "futures"
    for pattern in _FOREX_PATTERNS:
        if pattern.search(first):
            return "forex"
    for pattern in _STOCKS_PATTERNS:
        if pattern.search(first):
            return "stocks"
    log.warning("asset_class_derivation_fallback", symbol=first)
    return "stocks"


async def derive_asset_class(
    symbols: list[str],
    *,
    start: "date",
    db: "AsyncSession | None",
) -> str:
    """Async server-authoritative derivation — registry first, shape fallback."""
    if not symbols:
        return "stocks"
    if db is not None:
        try:
            from msai.services.nautilus.security_master.service import SecurityMaster

            master = SecurityMaster(db=db)
            resolved = await master.resolve_for_backtest(
                [symbols[0]], start=start.isoformat()
            )
            if resolved:
                asset_class = master.asset_class_for_alias(resolved[0])
                if asset_class:
                    return asset_class
        except Exception:  # noqa: BLE001 — registry failure never kills auto-heal
            log.warning(
                "asset_class_registry_lookup_failed",
                symbol=symbols[0],
                exc_info=True,
            )
    return derive_asset_class_sync(symbols)
```

**Test file:** parametrize `derive_asset_class_sync` directly for shape tests (no async fixture needed). Write one `@pytest.mark.asyncio` test for the registry-hit path using an `AsyncMock` for `SecurityMaster.resolve_for_backtest` + `.asset_class_for_alias`.

**Add to `backend/src/msai/services/nautilus/security_master/service.py`** (near the existing `_asset_class_for_instrument` helper at line ~540):

```python
def asset_class_for_alias(self, alias_str: str) -> str | None:
    """Public wrapper: canonical alias → asset_class name, or None on unknown shape."""
    try:
        spec = self._spec_from_canonical(alias_str)
    except Exception:  # noqa: BLE001
        return None
    # Reuse whatever spec → asset_class mapping already lives in this module.
    # If `_asset_class_for_instrument` is instance-bound with different
    # signature, inline the branching logic here (stocks/futures/options/forex/crypto).
    ...
```

**Wire into classifier** (`backend/src/msai/services/backtests/classifier.py:102-137`):

**NOTE:** `classify_worker_failure` is currently sync. Making it async would ripple through `_mark_backtest_failed` and every test. Instead, keep the classifier sync and use `derive_asset_class_sync` (shape-only) here. The full async-with-registry derivation happens in the `run_auto_heal` orchestrator path BEFORE the classifier is called on the orchestrator's failure branch, so the remediation string gets the correct asset_class. When `_mark_backtest_failed` runs as a direct fallback (no orchestrator invocation — e.g., for non-MISSING_DATA errors), the sync fallback is sufficient because `asset_class` is only used in the `msai ingest <asset_class> ...` remediation command.

Replace classifier line 107:

```python
from msai.services.backtests.derive_asset_class import derive_asset_class_sync
resolved_asset_class = (
    derive_asset_class_sync(instruments)
    or asset_class  # caller-supplied hint (from worker config)
    or (m.group(2) if m else None)
)
```

The async registry-authoritative derivation lives in `auto_heal.py::run_auto_heal` (see REV B7 below). Two code paths, same intent; sync in classifier (no DB session handy at `_mark_backtest_failed` time), async in orchestrator (has DB session).

---

#### REV B4 — fakeredis dev dep

**File:** `backend/pyproject.toml` — add under `[project.optional-dependencies].dev`:

```toml
"fakeredis[lua]>=2.20",
```

Run `cd backend && uv sync --all-extras` to install. Alternative: use `testcontainers.redis.RedisContainer` (already a dev dep) and move lock tests to `tests/integration/` — pick based on test runtime. `fakeredis` is simpler for unit tests.

---

#### REV B7 — Orchestrator with arq Job polling + single-increment attempt flow

**Critical structure:** `run_auto_heal` accepts the arq worker `ctx` (which carries the redis pool on `ctx["redis"]`) and a DB session factory; it does NOT create its own redis pool.

```python
"""Auto-heal orchestrator. [iter-1 P0-b + P0-c — arq Job polling + enqueue_ingest return value.]"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from arq.jobs import Job, JobStatus

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import enqueue_ingest
from msai.models.backtest import Backtest
from msai.services.backtests.auto_heal_guardrails import evaluate_guardrails
from msai.services.backtests.auto_heal_lock import AutoHealLock, build_lock_key
from msai.services.backtests.derive_asset_class import derive_asset_class

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path
    from arq.connections import ArqRedis


class AutoHealOutcome(StrEnum):
    SUCCESS = "success"
    GUARDRAIL_REJECTED = "guardrail_rejected"
    TIMEOUT = "timeout"
    INGEST_FAILED = "ingest_failed"
    COVERAGE_STILL_MISSING = "coverage_still_missing"


@dataclass(frozen=True, slots=True)
class AutoHealResult:
    outcome: AutoHealOutcome
    asset_class: str
    resolved_instrument_ids: list[str] | None
    reason_human: str | None
    gaps: list[tuple[str, list[tuple[int, int]]]] | None = None


log = get_logger(__name__)


async def run_auto_heal(
    *,
    backtest_id: str,
    instruments: list[str],
    start: "date",
    end: "date",
    catalog_root: "Path",
    caller_asset_class_hint: str | None,
    pool: "ArqRedis",
) -> AutoHealResult:
    """One bounded-lazy auto-heal cycle. Caller is backtest_job.py."""
    structlog.contextvars.bind_contextvars(backtest_id=backtest_id)
    try:
        # 1. Async asset_class derivation (registry + shape)
        async with async_session_factory() as db:
            asset_class = await derive_asset_class(instruments, start=start, db=db)
        asset_class = asset_class or caller_asset_class_hint or "stocks"

        log.info(
            "backtest_auto_heal_started",
            symbols=instruments,
            asset_class=asset_class,
            start=start.isoformat(),
            end=end.isoformat(),
        )

        # 2. Guardrails
        guardrails = evaluate_guardrails(
            asset_class=asset_class,
            symbols=instruments,
            start=start,
            end=end,
            max_years=settings.auto_heal_max_years,
            max_symbols=settings.auto_heal_max_symbols,
            allow_options=settings.auto_heal_allow_options,
        )
        if not guardrails.allowed:
            log.info(
                "backtest_auto_heal_guardrail_rejected",
                reason=guardrails.reason,
                details=guardrails.details,
            )
            return AutoHealResult(
                outcome=AutoHealOutcome.GUARDRAIL_REJECTED,
                asset_class=asset_class,
                resolved_instrument_ids=None,
                reason_human=guardrails.human_message,
            )

        # 3. Dedupe lock + enqueue (or wait for existing job)
        lock = AutoHealLock(pool)
        lock_key = build_lock_key(
            asset_class=asset_class, symbols=instruments, start=start, end=end,
        )

        ingest_job_id: str | None = None
        acquired = False

        # Two-phase acquire: first reserve with a placeholder, then write the
        # real job_id as the lock value AFTER enqueue succeeds. This way the
        # lock value IS the job_id and a second caller can poll the same job.
        placeholder = f"reserving:{backtest_id}"
        acquired = await lock.try_acquire(
            lock_key, ttl_s=settings.auto_heal_lock_ttl_seconds, holder_id=placeholder,
        )
        try:
            if acquired:
                job = await enqueue_ingest(
                    pool=pool,
                    asset_class=asset_class,
                    symbols=instruments,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                if job is None:
                    log.warning("backtest_auto_heal_ingest_enqueue_declined")
                    return AutoHealResult(
                        outcome=AutoHealOutcome.INGEST_FAILED,
                        asset_class=asset_class,
                        resolved_instrument_ids=None,
                        reason_human="Ingest queue declined the job (unlikely).",
                    )
                ingest_job_id = job.job_id
                # Replace placeholder with the real job_id so non-acquirers see it.
                await pool.set(
                    lock_key,
                    ingest_job_id,
                    ex=settings.auto_heal_lock_ttl_seconds,
                )
                dedupe_result = "acquired"
            else:
                existing_job_id = await lock.get_holder(lock_key)
                if existing_job_id and not existing_job_id.startswith("reserving:"):
                    ingest_job_id = existing_job_id
                else:
                    # Acquiring holder still in placeholder state — brief wait.
                    await asyncio.sleep(2)
                    existing_job_id = await lock.get_holder(lock_key)
                    ingest_job_id = (
                        existing_job_id
                        if existing_job_id and not existing_job_id.startswith("reserving:")
                        else None
                    )
                dedupe_result = (
                    f"wait_for_existing:{ingest_job_id}"
                    if ingest_job_id
                    else "wait_race_placeholder_lost"
                )

            log.info(
                "backtest_auto_heal_ingest_enqueued",
                ingest_job_id=ingest_job_id,
                lock_key=lock_key,
                dedupe_result=dedupe_result,
            )

            await _set_backtest_phase(
                backtest_id=backtest_id,
                phase="awaiting_data",
                progress_message=(
                    f"Downloading {asset_class} data for "
                    + ",".join(instruments[:3])
                    + ("..." if len(instruments) > 3 else "")
                ),
                heal_started_at=datetime.now(UTC),
                heal_job_id=ingest_job_id,
            )

            if ingest_job_id is None:
                return AutoHealResult(
                    outcome=AutoHealOutcome.INGEST_FAILED,
                    asset_class=asset_class,
                    resolved_instrument_ids=None,
                    reason_human="Could not determine ingest job id after dedupe race.",
                )

            # 4. Poll arq job status with wall-clock cap
            ingest_job = Job(
                ingest_job_id, redis=pool, _queue_name=settings.ingest_queue_name,
            )
            cap = settings.auto_heal_wall_clock_cap_seconds
            interval = settings.auto_heal_poll_interval_seconds
            deadline = time.monotonic() + cap
            ingest_ok = False
            ingest_start = time.monotonic()

            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                status = await ingest_job.status()
                if status == JobStatus.complete:
                    ingest_ok = True
                    break
                if status == JobStatus.not_found:
                    # arq removed the result — treat as completed (best effort).
                    ingest_ok = True
                    break

            if not ingest_ok:
                log.warning(
                    "backtest_auto_heal_timeout",
                    wall_clock_seconds=cap,
                    ingest_job_id=ingest_job_id,
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.TIMEOUT,
                    asset_class=asset_class,
                    resolved_instrument_ids=None,
                    reason_human=f"Data download exceeded {cap // 60}-minute cap.",
                )

            # Inspect result to distinguish success from worker failure
            try:
                await ingest_job.result(timeout=5.0)
                log.info(
                    "backtest_auto_heal_ingest_completed",
                    ingest_duration_seconds=int(time.monotonic() - ingest_start),
                )
            except Exception:  # noqa: BLE001 — ingest worker failure
                log.exception(
                    "backtest_auto_heal_ingest_failed",
                    ingest_job_id=ingest_job_id,
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.INGEST_FAILED,
                    asset_class=asset_class,
                    resolved_instrument_ids=None,
                    reason_human="Ingest provider returned an error; see worker logs.",
                )

            # 5. Coverage re-check
            from msai.services.nautilus.catalog_builder import verify_catalog_coverage

            # Canonicalize symbols for catalog lookup
            async with async_session_factory() as db:
                from msai.services.nautilus.security_master.service import SecurityMaster

                master = SecurityMaster(db=db)
                try:
                    resolved_ids = await master.resolve_for_backtest(
                        instruments, start=start.isoformat()
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "auto_heal_canonical_resolution_failed",
                        exc_info=True,
                    )
                    resolved_ids = list(instruments)

            gaps = verify_catalog_coverage(
                catalog_root=catalog_root,
                instrument_ids=resolved_ids,
                start=start,
                end=end,
            )
            if any(len(g) > 0 for _, g in gaps):
                log.warning(
                    "backtest_auto_heal_coverage_still_missing",
                    gaps=[{"instrument_id": iid, "gap_count": len(g)} for iid, g in gaps],
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.COVERAGE_STILL_MISSING,
                    asset_class=asset_class,
                    resolved_instrument_ids=resolved_ids,
                    reason_human="Provider returned a narrower range than requested.",
                    gaps=gaps,
                )

            log.info("backtest_auto_heal_completed", outcome="success")
            return AutoHealResult(
                outcome=AutoHealOutcome.SUCCESS,
                asset_class=asset_class,
                resolved_instrument_ids=resolved_ids,
                reason_human=None,
            )

        finally:
            # Release lock only if we acquired it — never steal.
            if acquired:
                # Value may have been overwritten to ingest_job_id; release
                # accepts either holder_id value seen since the last acquire.
                current = await lock.get_holder(lock_key)
                if current in (placeholder, ingest_job_id):
                    await lock.release(lock_key, holder_id=current or placeholder)
            await _set_backtest_phase(
                backtest_id=backtest_id,
                phase=None,
                progress_message=None,
                heal_started_at=None,
                heal_job_id=None,
            )
    finally:
        structlog.contextvars.unbind_contextvars("backtest_id")


async def _set_backtest_phase(
    *,
    backtest_id: str,
    phase: str | None,
    progress_message: str | None,
    heal_started_at: "datetime | None",
    heal_job_id: str | None,
) -> None:
    """Atomically update the 4 auto-heal columns on the backtest row."""
    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.phase = phase
            row.progress_message = progress_message
            if heal_started_at is not None:
                row.heal_started_at = heal_started_at
            if heal_job_id is not None:
                row.heal_job_id = heal_job_id
            if phase is None:
                row.heal_started_at = None
                row.heal_job_id = None
            await session.commit()
    except Exception:
        log.exception("backtest_auto_heal_phase_update_failed")
```

Signature change: `run_auto_heal` now accepts `pool: ArqRedis`. The `ctx` dict in the arq job carries `ctx["redis"]` — the backtest_job.py caller passes `ctx["redis"]` as `pool=`.

---

#### REV B8 — retry without double `_start_backtest`

Refactor `run_backtest_job` so `_start_backtest` runs ONCE; the retry-once loop only re-enters `_execute_backtest(snapshot, ...)` with the already-loaded row snapshot:

```python
async def run_backtest_job(ctx, backtest_id, strategy_path, config):
    # ... logging, parameters ...
    backtest_row = await _start_backtest(backtest_id)  # runs ONCE
    if backtest_row is None:
        return
    symbols = list(backtest_row["instruments"])
    # ... heartbeat task start ...
    try:
        attempt = 0
        terminal_exc: BaseException | None = None
        while attempt < 2:
            attempt += 1
            try:
                await _execute_backtest(
                    backtest_row=backtest_row,
                    backtest_id=backtest_id,
                    strategy_path=strategy_path,
                    config=config,
                    symbols=symbols,
                    asset_class=asset_class,
                )
                return  # happy path
            except FileNotFoundError as exc:
                if attempt == 1:
                    from msai.services.backtests.auto_heal import (
                        AutoHealOutcome,
                        run_auto_heal,
                    )

                    result = await run_auto_heal(
                        backtest_id=backtest_id,
                        instruments=symbols,
                        start=backtest_row["start_date"],
                        end=backtest_row["end_date"],
                        catalog_root=settings.nautilus_catalog_root,
                        caller_asset_class_hint=asset_class,
                        pool=ctx["redis"],
                    )
                    if result.outcome == AutoHealOutcome.SUCCESS:
                        continue  # re-enter _execute_backtest with snapshot
                    terminal_exc = FileNotFoundError(
                        result.reason_human or "Auto-heal failed"
                    )
                    break
                terminal_exc = exc
                break
            except Exception as exc:
                terminal_exc = exc
                break

        if terminal_exc is not None:
            await _handle_terminal_failure(
                backtest_id, symbols, asset_class, backtest_row, terminal_exc,
            )
    finally:
        stop_heartbeat.set()
        heartbeat_task.cancel()
```

`_execute_backtest` is the lifted body of the current try block (lines 103-205). It takes the already-loaded `backtest_row` dict and does NOT call `_start_backtest`. `_handle_terminal_failure` is the lifted error-logging + `_mark_backtest_failed` block.

---

#### REV B9 — `BacktestListItem.phase` explicit

Add to B9 scope:

```python
class BacktestListItem(BaseModel):
    # ... existing fields ...
    error_code: str | None = None
    error_public_message: str | None = None
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None
    model_config = {"from_attributes": True}
```

Update `list_backtests` endpoint (api/backtests.py:351-366) to populate `phase` + `progress_message`. Integration-test: assert a running-with-phase row serializes with the phase field.

---

#### REV F2 — detail-page polling + list-page nav link for running rows

**`frontend/src/app/backtests/[id]/page.tsx` — add polling:**

```tsx
useEffect(() => {
  let active = true;
  let timerId: ReturnType<typeof setTimeout> | null = null;

  const poll = async () => {
    if (!active) return;
    try {
      const fresh = await apiGet<BacktestStatusResponse>(
        `/api/v1/backtests/${id}/status`,
        { token: await getToken() },
      );
      if (!active) return;
      setStatus(fresh);
      if (fresh.status === "pending" || fresh.status === "running") {
        timerId = setTimeout(poll, 3000);
      }
    } catch {
      /* next poll will retry */
      if (active) timerId = setTimeout(poll, 5000);
    }
  };

  poll();
  return () => {
    active = false;
    if (timerId !== null) clearTimeout(timerId);
  };
}, [id, getToken]);
```

**`frontend/src/app/backtests/page.tsx` — extend row-link visibility:**

Replace the existing condition that gates the "View details" ExternalLink on `row.status === "completed" || row.status === "failed"` with: `row.status !== "pending"` (so `running` rows are clickable too — user can navigate to see the phase indicator).

---

## Known Assumptions / Open Decisions Flagged for Plan Review

1. **Poll strategy inside `run_auto_heal`:** current draft polls the lock key but the ingest worker does not release our lock. The revised approach (see B7 Step 3 note) is to poll the arq job status directly via `Job(ingest_job_id, pool).status()`. Plan review should confirm this is the right mechanism and that the dedupe case (second caller) reads the lock value as the `ingest_job_id` and polls the same job.

2. **Backtest status during heal** is `running` per council verdict — NOT a new top-level state. But the watchdog at `services/job_watchdog.py` has threshold logic on `running` rows with stale heartbeats. Confirm in plan review that the heartbeat task started in `run_backtest_job:149-159` continues to fire during heal — it should, because `_execute_backtest` hasn't entered yet when FNF fires, and the auto-heal orchestrator runs synchronously inside the arq job context. But on the second attempt re-entry, heartbeat restarts. Worth an explicit check.

3. **Re-entry path on SUCCESS:** the retry loop in B8 calls `_execute_backtest` a second time. But the first call already flipped `backtest.status = "running"` + `backtest.progress = 10` + `backtest.attempt += 1`. Second call does the same. `attempt` will count up twice — acceptable (the existing watchdog uses attempt for stale-job detection, not as a user-facing count). Confirm in plan review.

4. **Concurrency of `run_ingest` on ingest queue:** research brief §3 recommended leaving `run_ingest` registered on BOTH the default `WorkerSettings.functions` AND `IngestWorkerSettings.functions` during migration. After this PR ships, verify zero stale jobs on the default queue (Redis `LLEN arq:queue`) for one day, then cleanup PR drops the default-queue registration. Plan review: confirm this is the right migration approach or prefer a hard cutover.

5. **UI `BacktestListItem` extension:** Task F2 relies on list-page rows carrying `phase`. Task B9 above includes extending `BacktestStatusResponse` but not `BacktestListItem`. Plan review should confirm whether to:
   - (a) also add `phase` + `progress_message` to `BacktestListItem` + history endpoint (preferred — one polling client sees everything), OR
   - (b) the list-page "Fetching data…" badge is gated on `status === "running"` only (loses the distinction from normal-running) and the user has to click through to see the real phase.

   Recommend (a). Adjust B9 acceptance if so.

6. **`verify_catalog_coverage` with per-second timestamps:** Nautilus's `get_missing_intervals_for_request` expects nanosecond precision. The end_ns calculation uses `23:59:59` — verify this matches the inclusive end-date convention used elsewhere in the project. If the catalog tracks ns-precise timestamps for last bar of the day, this might leave a 1-second gap. Plan review or unit test clarifies.

---

## End-of-plan

**Plan saved to:** `docs/plans/2026-04-21-backtest-auto-ingest-on-missing-data.md`

**Two execution options:**

1. **Subagent-Driven (this session)** — dispatch fresh subagent per task, spec + quality review between tasks, fast iteration. Use `superpowers:subagent-driven-development`.
2. **Parallel Session (separate)** — open new session in this worktree with `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
