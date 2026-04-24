# Databento registry bootstrap — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Iteration history:** v1 (2026-04-23) → review iter 1 (Claude: 4 P0 + 15 P1 + 9 P2; Codex: 1 P0 + 6 P1 + 4 P2) → v2 → review iter 2 (Claude: 0 P0 + 6 P1 + 3 P2; Codex: 1 P0 + 4 P1 + 3 P2 + 1 P3) → **v3 (this file) — iter-2 findings folded in**: `get_session_factory` added (new T2), `click.Choice` → Typer `Enum`, `Histogram.time()` → `perf_counter + observe()`, `exact_ids: dict[str, int]` → `dict[str, str]` with alias_string semantics, `_bootstrap_continuous_future` computes NOOP/ALIAS_ROTATED, UC-DRB-001/005 rewritten to real `BacktestRunRequest` shape + synthetic continuous-ID expectation, `E2E_RUN_DATABENTO` → `RUN_PAPER_E2E`, module-local `client` fixture with DB override, T9 fallback test asserts ordering, T8/T14 counter assertions use alphabetical labels + "1.0" values, T11 over-engineered summary-validator removed, `Base` import corrected to `from msai.models import Base`.

**Goal:** Ship an on-demand Databento path for populating the instrument registry — `POST /api/v1/instruments/bootstrap` + `msai instruments bootstrap` CLI — so cold-start environments can register equity, ETF, and futures symbols without an IB Gateway dependency. Databento-bootstrapped symbols are **backtest-discoverable only**; live graduation still requires an explicit `instruments refresh --provider interactive_brokers` step.

**Architecture:** API is primary (one write path, one source of truth); CLI bypasses `_api_call` (auto-fails on non-2xx) and calls the API directly via `httpx.request`. Reuses `fetch_definition_instruments` + `_upsert_definition_and_alias` plumbing. New `normalize_alias_for_registry(provider, alias_string) -> str` helper converts Nautilus MIC venues (`SPY.XARC`) to the registry's exchange-name convention (`SPY.ARCA`) at the write boundary for `provider="databento"`. Raw Databento venue preserved via additive `source_venue_raw` column on `instrument_aliases`. Rate-limit + advisory-lock hardening (stable `blake2b`-hashed key, NOT Python `hash()`) protects the registry from bootstrap-introduced races. One `AsyncSession` per symbol (not shared across `asyncio.gather`). Three explicit readiness states (`registered` / `backtest_data_available` / `live_qualified`) returned per symbol.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, PostgreSQL 16 (pg_advisory_xact_lock), Typer CLI, databento Python SDK (sync `get_range` wrapped in `asyncio.to_thread`), NautilusTrader 1.223 (`DatabentoDataLoader`), tenacity (new dep) **9.x**, hand-rolled metrics registry at `services/observability/metrics.py`.

---

## Cross-references

- **PRD:** `docs/prds/databento-registry-bootstrap.md`
- **Scope council:** `docs/decisions/databento-registry-bootstrap.md` (`1b+2b+3a+4a` + 7 blocking constraints)
- **Venue normalization sub-council:** `CONTINUITY.md` Workflow section — Option A + 3 blocking constraints
- **Research brief:** `docs/research/2026-04-23-databento-registry-bootstrap.md` (OQ-1 RESOLVED POSITIVE, OQ-3 RESOLVED FIXED, OQ-4 REQUIRED)
- **Plan review iter 1 findings:** in session log (this v2 folds every P0/P1/P2 in)

---

## Ground-truth pins (from iter-1 surveys — do not deviate)

These are the EXACT APIs/patterns to use. Every iter-1 finding traced to a drift away from one of these.

| Primitive                    | Where                                                                                                                                                                                                                                                                                                                                                              | Notes                                                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Metrics registry             | `services/observability/metrics.py` (hand-rolled, **NOT** `prometheus_client` directly). `_r = get_registry(); counter = _r.counter(name, help_text); counter.labels(key=val).inc()`. Histogram: `_r.histogram(name, help_text, buckets=tuple)`. Tests assert via `registry.render()` substring match. Pattern: `services/observability/trading_metrics.py:16-50`. | Import: `from msai.services.observability import get_registry`                                                             |
| Auth dependency              | `core/auth.py:92` — `get_current_user`; `core/auth.py:134` — `get_current_user_or_none`. **No `require_auth` exists.**                                                                                                                                                                                                                                             | Import: `from msai.core.auth import get_current_user`                                                                      |
| HTTP error envelope          | `api/backtests.py:92` — top-level `JSONResponse(status_code=X, content={"error": {"code", "message", "details"}})`. **NOT `HTTPException(detail={"error": ...})`**.                                                                                                                                                                                                | Pattern mirrored for all new 422/500 responses                                                                             |
| 207 Multi-Status precedent   | `api/live.py:881-882` — `JSONResponse(status_code=207, content=...)`                                                                                                                                                                                                                                                                                               | Direct precedent; response_model decorator must NOT fight this                                                             |
| CLI API helper               | `cli.py:136` — `_api_call(method, path, *, json_body=None, params=None, timeout=30.0)`. **`_api_call` calls `_fail` and exits on any non-2xx**. Kwarg is `json_body=`, NOT `json=`.                                                                                                                                                                                | CLI bootstrap MUST bypass `_api_call` for expected 207/422 handling — use `httpx.request` directly via an internal helper. |
| Asset-class taxonomy         | DB CHECK: `'equity','futures','fx','option','crypto'`. **ETFs store as `equity`**; futures is plural `futures`. Single source of truth: `continuous_futures.py:137` — `asset_class_for_instrument_type(instrument_type: str)` mapping from `__class__.__name__`.                                                                                                   | Reuse this helper — don't reinvent                                                                                         |
| Continuous-futures regex     | `databento_client.py:177` — `_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")`. Helper: `is_databento_continuous_pattern(symbol)` in `continuous_futures.py`.                                                                                                                                                                        | Reuse — no new regex                                                                                                       |
| Test fixture pattern         | `tests/integration/test_security_master_resolve_backtest.py:30-60`. `@pytest.fixture(scope="module") isolated_postgres_url` + `@pytest_asyncio.fixture async session_factory → async_sessionmaker[AsyncSession]` + `Base.metadata.create_all`.                                                                                                                     | Copy this pattern for every new integration test module                                                                    |
| Alembic test harness         | `tests/integration/test_alembic_migrations.py:61-130`. Uses `_run_alembic_upgrade(url, target)` subprocess, NOT programmatic `alembic.command.upgrade`.                                                                                                                                                                                                            | Alembic tests live in `tests/integration/`, NOT `tests/unit/`. Current head is `z4x5y6z7a8b9`.                             |
| Instrument.raw_symbol access | `continuous_futures.py:89` — `inst.raw_symbol.value` (it's a `Symbol` object, not str)                                                                                                                                                                                                                                                                             | `.value` access required                                                                                                   |
| Databento SDK call is SYNC   | `databento_client.py:142` — `client.timeseries.get_range(...)` is synchronous. tenacity `AsyncRetrying` wrapper must put the call in `asyncio.to_thread(...)` or it blocks the event loop.                                                                                                                                                                         | Mandatory for T5                                                                                                           |
| Databento error types        | `databento.common.error` — `BentoClientError(http_status: int, message: str, ...)` for 4xx (http_status is a required positional arg); `BentoServerError(http_status: int, message: str, ...)` for 5xx.                                                                                                                                                            | Tests must use real constructors; retry predicate must catch BOTH                                                          |

---

## File Structure

### New files

| Path                                                                                                  | Responsibility                                                                                                                                                                                                                       |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `backend/src/msai/services/nautilus/security_master/venue_normalization.py`                           | `normalize_alias_for_registry(provider, alias_string)` + closed MIC→exchange-name map + fail-loud on unknown                                                                                                                         |
| `backend/src/msai/services/nautilus/security_master/databento_bootstrap.py`                           | `DatabentoBootstrapService` — batch orchestration (session-per-symbol), ambiguity detection, readiness-state computation                                                                                                             |
| `backend/src/msai/services/data_sources/databento_errors.py`                                          | Typed `DatabentoError` hierarchy (`DatabentoUnauthorizedError`, `DatabentoRateLimitedError`, `DatabentoUpstreamError`) carrying `http_status` + `dataset` for structured classification (replaces string-match on RuntimeError text) |
| `backend/src/msai/schemas/instrument_bootstrap.py`                                                    | Pydantic `BootstrapRequest` / `BootstrapResponse` / `BootstrapResultItem` / `CandidateInfo`                                                                                                                                          |
| `backend/src/msai/api/instruments.py`                                                                 | FastAPI router — `POST /api/v1/instruments/bootstrap`                                                                                                                                                                                |
| `backend/alembic/versions/a5b6c7d8e9f0_add_source_venue_raw_to_instrument_aliases.py`                 | Additive column (chained off current head `z4x5y6z7a8b9`)                                                                                                                                                                            |
| `backend/tests/integration/test_alembic_databento_bootstrap_migration.py`                             | Subprocess round-trip test using existing `_run_alembic_upgrade` pattern                                                                                                                                                             |
| `backend/tests/unit/services/nautilus/security_master/test_venue_normalization.py`                    | Closed-map + fail-loud                                                                                                                                                                                                               |
| `backend/tests/integration/conftest_databento.py`                                                     | Reusable `session_factory` + `mock_databento` fixtures for Databento-bootstrap tests                                                                                                                                                 |
| `backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_equities.py`           | Bootstrap service unit tests                                                                                                                                                                                                         |
| `backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_continuous_futures.py` | Futures path NOT broken by additions                                                                                                                                                                                                 |
| `backend/tests/integration/test_security_master_advisory_lock.py`                                     | Advisory-lock race test                                                                                                                                                                                                              |
| `backend/tests/integration/test_registry_venue_divergence.py`                                         | Divergence counter test via `registry.render()` assertions                                                                                                                                                                           |
| `backend/tests/integration/test_api_instruments_bootstrap.py`                                         | API 200/207/422 contract                                                                                                                                                                                                             |
| `backend/tests/unit/test_databento_client_retry.py`                                                   | tenacity retry test with typed errors                                                                                                                                                                                                |
| `backend/tests/unit/test_databento_client_ambiguity.py`                                               | Multi-candidate dedup + raise                                                                                                                                                                                                        |
| `backend/tests/unit/test_cli_instruments_bootstrap.py`                                                | CLI wrapper tests                                                                                                                                                                                                                    |

### Modified files

| Path                                                                       | Change                                                                                                                                                                                  |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `backend/src/msai/services/data_sources/databento_client.py`               | tenacity retry wrapper using `asyncio.to_thread` + typed error classification + ambiguity detection on multi-candidate                                                                  |
| `backend/src/msai/services/nautilus/security_master/service.py` (~694-820) | `_upsert_definition_and_alias` gains: stable `blake2b`-hashed `pg_advisory_xact_lock` + pre-upsert divergence check + call to `normalize_alias_for_registry` + `source_venue_raw` param |
| `backend/src/msai/models/instrument_alias.py`                              | Add `source_venue_raw: Mapped[str                                                                                                                                                       | None]` |
| `backend/src/msai/cli.py`                                                  | New `instruments_bootstrap` subcommand; bypass `_api_call`; reuse `_api_base`+`_api_headers` helpers directly                                                                           |
| `backend/src/msai/main.py`                                                 | Register `instruments_router`                                                                                                                                                           |
| `backend/src/msai/services/observability/trading_metrics.py`               | Append 3 counters + 1 histogram using existing `_r = get_registry()` pattern                                                                                                            |
| `backend/pyproject.toml`                                                   | Add `tenacity>=9.1.0,<10`                                                                                                                                                               |
| `docs/prds/databento-registry-bootstrap.md`                                | Sync US-009 wording to post-normalization semantics                                                                                                                                     |

---

## Task dependency graph

```
T0 (survey+fixtures) ──┬── T1 (tenacity dep)        ──── T5 (retry wrapper)
                       ├── T3 (migration+model)    ──┬── T4 (normalization helper)
                       └── T14 (metrics registration) │
                                                       │
T5 ──── T6 (ambiguity)                                 │
                                                       │
T4 + T14 ──── T7 (_upsert adv lock + normalize)      ──┤
                                                       │
T7 ──── T8 (pre-upsert divergence check)               │
                                                       │
T5 + T6 + T7 ──── T9 (bootstrap service)               │
T9 ──── T10 (outcome distinction)                      │
                                                       │
T10 ──── T11 (schemas) ──── T12 (API) ──── T13 (CLI)   │
                                                       │
T12 ──── T15 (PRD US-009 sync)                         │
                                                       │
All done ──── UC-DRB-001..006 (Phase 5.4 E2E)         ┘
```

---

## Phase 0 — Pre-flight fixtures + dependencies

### Task 0: Create reusable test fixtures (do this FIRST — everything depends on it)

**Files:**

- Create: `backend/tests/integration/conftest_databento.py`

- [ ] **Step 1: Write the fixture module (copy-adapted from `test_security_master_resolve_backtest.py:30-80`)**

```python
# backend/tests/integration/conftest_databento.py
"""Reusable fixtures for Databento-bootstrap integration tests.

``session_factory`` gives each test a fresh testcontainers-backed
Postgres with the full schema applied via ``Base.metadata.create_all``.
``mock_databento`` returns a ``DatabentoClient``-shaped mock whose
``fetch_definition_instruments`` is pre-configured for common test
symbols (AAPL → single equity, BRK.B → ambiguous, ES.n.0 → single
continuous future)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from msai.models import Base  # iter-3: side-effect-imports every model so Base.metadata.create_all covers the full schema

if TYPE_CHECKING:
    pass


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_equity_instrument(raw_symbol: str, venue_mic: str):
    inst = MagicMock()
    inst.id = MagicMock()
    inst.id.value = f"{raw_symbol}.{venue_mic}"
    inst.raw_symbol = MagicMock()
    inst.raw_symbol.value = raw_symbol
    inst.__class__.__name__ = "Equity"
    return inst


@pytest.fixture
def mock_databento():
    client = MagicMock()
    client.api_key = "test-key"
    client.fetch_definition_instruments = AsyncMock()

    def _default_side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        if symbol == "AAPL":
            return [_make_equity_instrument("AAPL", "XNAS")]
        if symbol == "SPY":
            return [_make_equity_instrument("SPY", "XARC")]
        if symbol == "QQQ":
            return [_make_equity_instrument("QQQ", "XNAS")]
        if symbol in {"ES.n.0", "ES.c.0"}:
            # iter-3: continuous-futures happy-path. The _bootstrap_continuous_future
            # branch delegates to SecurityMaster.resolve_for_backtest which calls
            # its OWN path — this mock arm exists only so direct calls to
            # fetch_definition_instruments(symbol="ES.n.0", ...) don't raise
            # RuntimeError during isolated unit tests.
            fut = MagicMock()
            fut.id = MagicMock()
            fut.id.value = f"{symbol}.CME"
            fut.raw_symbol = MagicMock()
            fut.raw_symbol.value = symbol
            fut.__class__.__name__ = "FuturesContract"
            return [fut]
        if symbol == "BRK.B":
            from msai.services.data_sources.databento_client import AmbiguousDatabentoSymbolError
            raise AmbiguousDatabentoSymbolError(
                symbol="BRK.B",
                candidates=[
                    {"alias_string": "BRK.B.XNYS", "raw_symbol": "BRK.B", "asset_class": "equity", "dataset": dataset},
                    {"alias_string": "BRK.BP.XNYS", "raw_symbol": "BRK.BP", "asset_class": "equity", "dataset": dataset},
                ],
            )
        raise RuntimeError(f"Databento definition request failed for {symbol}")

    client.fetch_definition_instruments.side_effect = _default_side_effect
    return client
```

- [ ] **Step 2: Confirm fixture loads without errors + no `pytest_plugins` deprecation warning**

```bash
cd backend && uv run pytest --collect-only tests/integration/conftest_databento.py
```

Expected: no collection errors.

- [ ] **Step 2b: Verify `pytest_plugins = ["tests.integration.conftest_databento"]` pattern works**

The integration test modules declare `pytest_plugins` at module top (not in `conftest.py`). Pytest 7+ warns on non-conftest `pytest_plugins` declarations but still honors them. Run:

```bash
cd backend && uv run pytest tests/integration/test_security_master_advisory_lock.py --collect-only -W error::pytest.PytestDeprecationWarning 2>&1 | head -20
```

If this errors on `PytestDeprecationWarning`: move the fixtures from `conftest_databento.py` into `tests/integration/conftest.py` (merge) OR put them in a new sub-package `tests/integration/databento/conftest.py` that scopes pytest's auto-discovery. Otherwise the `pytest_plugins = [...]` at module top is silently-accepted and safe.

- [ ] **Step 2c: Pre-copy verification of Counter render format (iter-3 insurance)**

Before copying `... 1.0' in rendered` assertions into tests, verify the actual format:

```bash
cd backend && uv run python -c "
from msai.services.observability import get_registry
r = get_registry()
c = r.counter('test_counter_iter3', 'iter-3 probe')
c.labels(a='x', b='y').inc()
c.labels(a='x', b='y').inc(0.5)
print(repr(r.render()))
"
```

Expected output shows the exact format (`1.0` or `1.5` for mixed int+float; labels alphabetical). Copy the exact render format into T8/T14 test assertions.

**Note: No commit for Phase 0 files. Workflow gate blocks commits on an active-workflow branch until Phase 5 quality gates clear (feedback memory `feedback_workflow_gate_blocks_preflight_commits.md`).**

---

### Task 1: Add tenacity as explicit dependency

**Files:**

- Modify: `backend/pyproject.toml`

- [ ] **Step 1:** Add `"tenacity>=9.1.0,<10"` to `[project].dependencies`
- [ ] **Step 2:** `cd backend && uv sync && uv run python -c "import tenacity; print(tenacity.__version__)"` → expect 9.x
- [ ] **Step 3:** No commit (Phase 0 rule).

---

## Phase 1 — Schema migration

### Task 2: Expose `get_session_factory` FastAPI dependency

**Files:**

- Modify: `backend/src/msai/core/database.py`

The T12 endpoint needs an injectable session-factory so tests can override the DB target. `core/database.py` already exposes `async_session_factory` (instance); wrap it in a callable for FastAPI's `Depends()`.

- [ ] **Step 1: Append to `backend/src/msai/core/database.py`**

```python
# At module top, ensure async_sessionmaker is imported from sqlalchemy.ext.asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency returning the module-level async_session_factory.

    Wrapping the instance in a callable enables test-side overrides via
    ``app.dependency_overrides[get_session_factory] = lambda: test_factory``
    (mirrors the pattern used for ``get_db``). Added iter-3 for the
    Databento-bootstrap endpoint which needs session-per-symbol ownership
    via the factory, not a single pre-opened session.
    """
    return async_session_factory
```

- [ ] **Step 2: Verify the import + return works**

```bash
cd backend && uv run python -c "from msai.core.database import get_session_factory; print(get_session_factory())"
```

Expected: printed `async_sessionmaker` instance (not an error).

- [ ] **Step 3: No commit (Phase 0 rule).**

---

### Task 3: Alembic migration — add `source_venue_raw`

**Files:**

- Create: `backend/alembic/versions/a5b6c7d8e9f0_add_source_venue_raw_to_instrument_aliases.py`
- Modify: `backend/src/msai/models/instrument_alias.py`
- Modify (append): `backend/tests/integration/test_alembic_migrations.py`

- [ ] **Step 1: Write the failing test (append to existing integration file)**

```python
# in backend/tests/integration/test_alembic_migrations.py

@pytest.mark.asyncio
async def test_source_venue_raw_round_trip(isolated_postgres_url: str) -> None:
    """Upgrade from parent z4x5y6z7a8b9 → head adds source_venue_raw (nullable String(64));
    downgrade removes it cleanly."""
    _run_alembic_upgrade(isolated_postgres_url, target="z4x5y6z7a8b9")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            def _cols(sync_conn):
                return {c["name"]: c for c in inspect(sync_conn).get_columns("instrument_aliases")}
            before = await conn.run_sync(_cols)
        assert "source_venue_raw" not in before
    finally:
        await engine.dispose()

    _run_alembic_upgrade(isolated_postgres_url, target="head")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            def _cols(sync_conn):
                return {c["name"]: c for c in inspect(sync_conn).get_columns("instrument_aliases")}
            after = await conn.run_sync(_cols)
        assert "source_venue_raw" in after
        assert after["source_venue_raw"]["nullable"] is True
        assert str(after["source_venue_raw"]["type"]).startswith("VARCHAR(64)")
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "downgrade", "z4x5y6z7a8b9")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            def _cols(sync_conn):
                return {c["name"]: c for c in inspect(sync_conn).get_columns("instrument_aliases")}
            after_down = await conn.run_sync(_cols)
        assert "source_venue_raw" not in after_down
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run test — FAIL (migration doesn't exist)**

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_source_venue_raw_round_trip -v
```

- [ ] **Step 3: Create migration file**

```python
# backend/alembic/versions/a5b6c7d8e9f0_add_source_venue_raw_to_instrument_aliases.py
"""add source_venue_raw to instrument_aliases

Revision ID: a5b6c7d8e9f0
Revises: z4x5y6z7a8b9
Create Date: 2026-04-23 ...
"""
from alembic import op
import sqlalchemy as sa

revision = "a5b6c7d8e9f0"
down_revision = "z4x5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_aliases",
        sa.Column("source_venue_raw", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instrument_aliases", "source_venue_raw")
```

- [ ] **Step 4: Mirror in model**

In `backend/src/msai/models/instrument_alias.py`, after `venue_format: Mapped[str] = ...`:

```python
# Source-provider verbatim venue (Databento MIC code like "XNAS"
# before normalization to exchange-name). Nullable because IB writes
# don't need it — IB already emits exchange-name. Populated for
# provider="databento" writes as the lineage-preserving half of the
# Venue Council's Option A verdict.
source_venue_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

- [ ] **Step 5: Run test — PASS**

```bash
cd backend && uv run pytest tests/integration/test_alembic_migrations.py::test_source_venue_raw_round_trip -v
```

- [ ] **Step 6: Apply to dev DB**

```bash
cd backend && uv run alembic upgrade head
```

- [ ] **Step 7: No commit (Phase 0 rule). Progress tracked in CONTINUITY only.**

---

## Phase 2 — Venue normalization helper

### Task 4: `normalize_alias_for_registry` + closed MIC map (fail-loud)

**Files:**

- Create: `backend/src/msai/services/nautilus/security_master/venue_normalization.py`
- Create: `backend/tests/unit/services/nautilus/security_master/test_venue_normalization.py`

- [ ] **Step 1: Write failing tests** (9 cases: XNAS→NASDAQ, XNYS→NYSE, XARC→ARCA, ARCX→ARCA, XASE→AMEX, EPRL→PEARL, GLBX→CME, unknown MIC raises, no-dot-alias raises, IB passthrough)

```python
import pytest
from msai.services.nautilus.security_master.venue_normalization import (
    UnknownDatabentoVenueError,
    normalize_alias_for_registry,
)


@pytest.mark.parametrize("alias, expected", [
    ("AAPL.XNAS", "AAPL.NASDAQ"),
    ("SPY.XARC", "SPY.ARCA"),
    ("IWM.ARCX", "IWM.ARCA"),
    ("BRK.B.XNYS", "BRK.B.NYSE"),
    ("PEARL.EPRL", "PEARL.PEARL"),
    ("ESM6.GLBX", "ESM6.CME"),
])
def test_known_mic_normalizes(alias, expected):
    assert normalize_alias_for_registry("databento", alias) == expected


def test_ib_alias_passthrough():
    assert normalize_alias_for_registry("interactive_brokers", "AAPL.NASDAQ") == "AAPL.NASDAQ"


def test_unknown_mic_raises_loud():
    with pytest.raises(UnknownDatabentoVenueError) as exc_info:
        normalize_alias_for_registry("databento", "AAPL.FAKEMIC")
    assert "FAKEMIC" in str(exc_info.value)
    assert "AAPL.FAKEMIC" in str(exc_info.value)


def test_no_venue_suffix_raises():
    with pytest.raises(UnknownDatabentoVenueError):
        normalize_alias_for_registry("databento", "AAPL")
```

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement helper**

```python
# backend/src/msai/services/nautilus/security_master/venue_normalization.py
"""Provider-scoped venue normalization at the registry write boundary.

Nautilus's DatabentoDataLoader.from_dbn_file(use_exchange_as_venue=True)
emits MIC venue codes (AAPL.XNAS, SPY.XARC); IB's adapter emits
exchange-name venues (AAPL.NASDAQ, SPY.ARCA). The registry stores the
exchange-name convention; live-start's lookup_for_live does exact-match
on alias_string. This helper translates Databento aliases at the write
boundary so both provider rows use the same convention.

Raw Databento venue preserved separately in instrument_aliases.source_venue_raw
(Venue Council Constraint #3, 2026-04-23).

Unknown MICs FAIL LOUDLY — silent passthrough would write an alias
lookup_for_live can never find.
"""

from __future__ import annotations


class UnknownDatabentoVenueError(ValueError):
    """Databento alias contains a MIC not in the provider-scoped map.
    Extend _DATABENTO_MIC_TO_EXCHANGE_NAME and add a test."""


# Closed enumeration — verified against Databento's entitled datasets on
# Pablo's plan (2026-04-23 metadata.list_datasets probe). Includes MIAX
# Pearl (EPRL) per iter-1 review finding.
_DATABENTO_MIC_TO_EXCHANGE_NAME: dict[str, str] = {
    # Primary equity venues
    "XNAS": "NASDAQ",
    "XNYS": "NYSE",
    "XARC": "ARCA",
    "ARCX": "ARCA",
    "XASE": "AMEX",
    # Cboe family
    "BATS": "BATS",
    "BATY": "BATY",
    "EDGA": "EDGA",
    "EDGX": "EDGX",
    # Other equity venues
    "IEXG": "IEX",
    "XBOS": "BOSTON",
    "XPSX": "PSX",
    "XCHI": "CHX",
    "XCIS": "NSX",
    "MEMX": "MEMX",
    "EPRL": "PEARL",
    # Futures
    "GLBX": "CME",
}


def normalize_alias_for_registry(provider: str, alias_string: str) -> str:
    """Return the alias_string the registry should store for this provider.

    For provider="databento": splits on the LAST '.' to extract venue,
    looks up in closed MIC map, rebuilds "{symbol}.{exchange_name}".
    Symbol preserved verbatim (including internal dots like BRK.B).

    For provider != "databento": passthrough.

    Raises UnknownDatabentoVenueError on unknown MIC or missing suffix.
    """
    if provider != "databento":
        return alias_string
    if "." not in alias_string:
        raise UnknownDatabentoVenueError(
            f"Databento alias {alias_string!r} has no venue suffix "
            f"(expected '{{symbol}}.{{MIC}}')."
        )
    symbol, _, mic = alias_string.rpartition(".")
    exchange_name = _DATABENTO_MIC_TO_EXCHANGE_NAME.get(mic)
    if exchange_name is None:
        raise UnknownDatabentoVenueError(
            f"Databento alias {alias_string!r} has unmapped MIC {mic!r}. "
            f"Extend _DATABENTO_MIC_TO_EXCHANGE_NAME in "
            f"services/nautilus/security_master/venue_normalization.py "
            f"and add a test, then retry."
        )
    return f"{symbol}.{exchange_name}"
```

- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: `uv run ruff check` + `uv run mypy --strict` clean on new file**
- [ ] **Step 6: No commit.**

---

## Phase 3 — Databento client hardening

### Task 5: Typed errors + tenacity retry via `asyncio.to_thread`

**Files:**

- Create: `backend/src/msai/services/data_sources/databento_errors.py`
- Modify: `backend/src/msai/services/data_sources/databento_client.py`
- Create: `backend/tests/unit/test_databento_client_retry.py`

- [ ] **Step 1: Create typed error hierarchy**

```python
# backend/src/msai/services/data_sources/databento_errors.py
"""Typed exception hierarchy for Databento SDK failures.

Replaces RuntimeError string-matching with structured error carriers so
the bootstrap service can classify outcomes via isinstance() + http_status
instead of brittle `"401" in str(exc)` patterns.
"""

from __future__ import annotations


class DatabentoError(Exception):
    """Base class — all Databento-surfaced failures carry http_status + dataset."""
    def __init__(self, message: str, *, http_status: int | None = None, dataset: str | None = None) -> None:
        self.http_status = http_status
        self.dataset = dataset
        super().__init__(message)


class DatabentoUnauthorizedError(DatabentoError):
    """401/403 — API key missing or dataset not entitled."""


class DatabentoRateLimitedError(DatabentoError):
    """429 — rate-limit exhausted after retries."""


class DatabentoUpstreamError(DatabentoError):
    """5xx or network failure after retries."""
```

- [ ] **Step 2: Write failing retry tests (using REAL BentoClientError/BentoServerError)**

```python
# backend/tests/unit/test_databento_client_retry.py
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from databento.common.error import BentoClientError, BentoServerError

from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.databento_errors import (
    DatabentoUnauthorizedError,
    DatabentoRateLimitedError,
    DatabentoUpstreamError,
)


@pytest.mark.asyncio
async def test_retry_recovers_from_429(tmp_path):
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _mock_get_range(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            raise BentoClientError(http_status=429, message="Rate limited", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_mock_get_range)
        with patch("msai.services.data_sources.databento_client.DatabentoDataLoader"):
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            await client.fetch_definition_instruments(
                symbol="AAPL", start="2024-01-01", end="2024-01-02",
                dataset="XNAS.ITCH", target_path=target,
            )
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_429_exhaustion_raises_rate_limited(tmp_path):
    client = DatabentoClient(api_key="test-key")

    def _always_429(*args, **kwargs):
        raise BentoClientError(http_status=429, message="Rate limited", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_always_429)
        with pytest.raises(DatabentoRateLimitedError) as exc_info:
            await client.fetch_definition_instruments(
                symbol="AAPL", start="2024-01-01", end="2024-01-02",
                dataset="XNAS.ITCH", target_path=tmp_path / "out.dbn.zst",
            )
    assert exc_info.value.http_status == 429
    assert exc_info.value.dataset == "XNAS.ITCH"


@pytest.mark.asyncio
async def test_401_no_retry(tmp_path):
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _always_401(*args, **kwargs):
        call_count[0] += 1
        raise BentoClientError(http_status=401, message="Unauthorized", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_always_401)
        with pytest.raises(DatabentoUnauthorizedError) as exc_info:
            await client.fetch_definition_instruments(
                symbol="AAPL", start="2024-01-01", end="2024-01-02",
                dataset="XNAS.ITCH", target_path=tmp_path / "out.dbn.zst",
            )
    assert call_count[0] == 1
    assert exc_info.value.http_status == 401


@pytest.mark.asyncio
async def test_500_retries_then_succeeds(tmp_path):
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _mock(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise BentoServerError(http_status=500, message="Internal error", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_mock)
        with patch("msai.services.data_sources.databento_client.DatabentoDataLoader"):
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            await client.fetch_definition_instruments(
                symbol="AAPL", start="2024-01-01", end="2024-01-02",
                dataset="XNAS.ITCH", target_path=target,
            )
    assert call_count[0] == 2
```

- [ ] **Step 3: Run — FAIL**

- [ ] **Step 4: Modify `databento_client.py`**

At imports:

```python
import asyncio
from databento.common.error import BentoClientError, BentoServerError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from msai.services.data_sources.databento_errors import (
    DatabentoRateLimitedError,
    DatabentoUnauthorizedError,
    DatabentoUpstreamError,
)

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (BentoClientError, BentoServerError)):
        return getattr(exc, "http_status", None) in _RETRYABLE_STATUSES
    return False
```

Modify `fetch_definition_instruments` — wrap the sync SDK call in `asyncio.to_thread` inside each retry attempt:

```python
async def fetch_definition_instruments(
    self, symbol: str, start: str, end: str,
    *, dataset: str, target_path: Path,
) -> list[Instrument]:
    if not self.api_key:
        raise RuntimeError("DATABENTO_API_KEY is not configured")

    import databento as db
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    client = db.Historical(key=self.api_key)

    def _sync_download() -> None:
        client.timeseries.get_range(
            dataset=dataset,
            schema="definition",
            symbols=[symbol],
            start=start,
            end=end,
            stype_in=_databento_stype_in(symbol),
            stype_out="instrument_id",
            path=str(tmp_path),
        )

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=9),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                # Run the sync SDK call in a thread so the event loop isn't blocked.
                await asyncio.to_thread(_sync_download)
    except BentoClientError as exc:
        tmp_path.unlink(missing_ok=True)
        if exc.http_status in (401, 403):
            raise DatabentoUnauthorizedError(
                f"Databento unauthorized for {symbol} on {dataset}: {exc}",
                http_status=exc.http_status, dataset=dataset,
            ) from exc
        if exc.http_status == 429:
            raise DatabentoRateLimitedError(
                f"Databento rate-limited after retries for {symbol} on {dataset}: {exc}",
                http_status=exc.http_status, dataset=dataset,
            ) from exc
        raise DatabentoUpstreamError(
            f"Databento 4xx for {symbol} on {dataset}: {exc}",
            http_status=exc.http_status, dataset=dataset,
        ) from exc
    except BentoServerError as exc:
        tmp_path.unlink(missing_ok=True)
        raise DatabentoUpstreamError(
            f"Databento 5xx for {symbol} on {dataset}: {exc}",
            http_status=exc.http_status, dataset=dataset,
        ) from exc
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise DatabentoUpstreamError(
            f"Databento unexpected error for {symbol} on {dataset}: {exc}",
            dataset=dataset,
        ) from exc

    tmp_path.replace(target_path)

    loader = DatabentoDataLoader()
    instruments = list(loader.from_dbn_file(
        target_path, as_legacy_cython=False, use_exchange_as_venue=True,
    ))
    return instruments  # dedup + ambiguity detection in Task 6
```

- [ ] **Step 5: Run — PASS**
- [ ] **Step 6: No commit.**

---

### Task 6: Ambiguity detection + symbol dedup

**Files:**

- Modify: `backend/src/msai/services/data_sources/databento_client.py`
- Create: `backend/tests/unit/test_databento_client_ambiguity.py`

- [ ] **Step 1: Define `AmbiguousDatabentoSymbolError`** (re-export; inherits `DatabentoError`)

```python
# in databento_client.py
from msai.services.data_sources.databento_errors import DatabentoError


class AmbiguousDatabentoSymbolError(DatabentoError):
    """Databento returned multiple distinct instruments for a single symbol request."""
    def __init__(self, symbol: str, candidates: list[dict[str, str]], *, dataset: str | None = None) -> None:
        self.symbol = symbol
        self.candidates = candidates
        super().__init__(
            f"Databento returned {len(candidates)} distinct instruments for {symbol!r}",
            dataset=dataset,
        )
```

- [ ] **Step 2: Update `fetch_definition_instruments` signature to accept `exact_id` (for disambiguation retry) + add dedup-by-id + ambiguity raise**

iter-3 fix (Codex P1): when the caller (bootstrap service) has an `exact_id` in hand from a prior ambiguity 422, the client must filter BEFORE raising ambiguous — otherwise the "retry with exact_id" flow can never succeed.

Change the signature:

```python
async def fetch_definition_instruments(
    self, symbol: str, start: str, end: str,
    *, dataset: str, target_path: Path,
    exact_id: str | None = None,  # iter-3: pre-filter before ambiguity check
) -> list[Instrument]:
```

After the `loader.from_dbn_file(...)` call:

```python
    instruments = list(loader.from_dbn_file(
        target_path, as_legacy_cython=False, use_exchange_as_venue=True,
    ))

    # Dedup by canonical id (same instrument emitted across multiple time windows
    # will appear N times with the same id.value).
    seen: dict[str, object] = {}
    for inst in instruments:
        key = str(inst.id.value) if hasattr(inst, "id") else repr(inst)
        seen.setdefault(key, inst)
    distinct = list(seen.values())

    # iter-3: if caller provided an exact_id (from a prior ambiguity 422's
    # candidates[]), filter to that single alias BEFORE deciding ambiguous.
    # Lets the "retry with exact_id" flow resolve cleanly on the second pass.
    if exact_id is not None:
        distinct = [i for i in distinct if str(i.id.value) == exact_id]
        if not distinct:
            raise DatabentoUpstreamError(
                f"exact_id {exact_id!r} not in {symbol}'s candidates for {dataset}",
                http_status=None, dataset=dataset,
            )

    if len(distinct) > 1:
        candidates = []
        for inst in distinct:
            candidates.append({
                "alias_string": str(inst.id.value),
                "raw_symbol": inst.raw_symbol.value if hasattr(inst, "raw_symbol") and hasattr(inst.raw_symbol, "value") else symbol,
                "asset_class": inst.__class__.__name__,  # caller uses asset_class_for_instrument_type()
                "dataset": dataset,
            })
        raise AmbiguousDatabentoSymbolError(symbol=symbol, candidates=candidates, dataset=dataset)

    return distinct  # deduped (and exact_id-filtered if applicable) — single element
```

- [ ] **Step 3: Write ambiguity tests** (single, multi, duplicate-same-id-not-ambiguous) — same structure as v1 but using `inst.raw_symbol.value` and asserting `__class__.__name__` instead of a non-existent `asset_class` attribute

- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: No commit.**

---

## Phase 4 — SecurityMaster write-path hardening

### Task 7: Stable advisory lock + venue normalization + source_venue_raw in `_upsert_definition_and_alias`

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py` (~694-820)
- Create: `backend/tests/integration/test_security_master_advisory_lock.py`
- Modify: `backend/tests/integration/test_security_master_databento_bootstrap.py` (new, Task 7 adds initial coverage for provenance)

- [ ] **Step 1: Write race-condition test using session_factory**

```python
# backend/tests/integration/test_security_master_advisory_lock.py
from __future__ import annotations
import asyncio
import pytest
from sqlalchemy import select

from msai.models.instrument_alias import InstrumentAlias
from msai.services.nautilus.security_master.service import SecurityMaster

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_concurrent_databento_upserts_serialize_via_advisory_lock(session_factory):
    """Two concurrent alias-rotation calls for the same (raw_symbol, provider,
    asset_class) leave exactly ONE active alias. Without the advisory lock
    they can leave two rows with effective_to IS NULL."""

    async def _rotate_to(alias_mic: str) -> None:
        async with session_factory() as session:
            sm = SecurityMaster(db=session, databento_client=None)
            await sm._upsert_definition_and_alias(
                raw_symbol="SPY",
                listing_venue="ARCA",
                routing_venue="SMART",
                asset_class="equity",
                alias_string=f"SPY.{alias_mic}",
                provider="databento",
                venue_format="mic_code",
            )
            await session.commit()

    # Seed
    await _rotate_to("XARC")
    # Concurrent rotations
    await asyncio.gather(_rotate_to("BATS"), _rotate_to("EDGX"))

    async with session_factory() as session:
        result = await session.execute(
            select(InstrumentAlias)
            .where(InstrumentAlias.provider == "databento")
            .where(InstrumentAlias.effective_to.is_(None))
        )
        active = result.scalars().all()
    assert len(active) == 1, f"expected 1 active alias, got {len(active)}: {[a.alias_string for a in active]}"


@pytest.mark.asyncio
async def test_source_venue_raw_populated_on_databento_write(session_factory):
    """provider='databento' writes preserve the raw MIC in source_venue_raw
    even after alias_string is normalized to exchange-name."""
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="AAPL.XNAS",   # pre-normalization
            provider="databento",
            venue_format="mic_code",
        )
        await session.commit()

    async with session_factory() as session:
        row = (await session.execute(
            select(InstrumentAlias).where(InstrumentAlias.alias_string == "AAPL.NASDAQ")
        )).scalar_one()
    assert row.source_venue_raw == "XNAS"
```

- [ ] **Step 2: Run — FAIL (lock + normalization not present)**

- [ ] **Step 3: Modify `_upsert_definition_and_alias`**

At method signature, add `source_venue_raw` kwarg:

```python
async def _upsert_definition_and_alias(
    self, *,
    raw_symbol: str,
    listing_venue: str,
    routing_venue: str,
    asset_class: str,
    alias_string: str,
    provider: str = "interactive_brokers",
    venue_format: str = "exchange_name",
    source_venue_raw: str | None = None,
) -> None:
```

At method body TOP (before ANY SQL), add:

```python
    import hashlib
    from sqlalchemy import text
    from msai.services.nautilus.security_master.venue_normalization import (
        normalize_alias_for_registry,
    )

    # Existing FX normalization on raw_symbol — unchanged
    if asset_class == "fx" and "/" not in raw_symbol and raw_symbol.count(".") == 1:
        raw_symbol = raw_symbol.replace(".", "/")

    # Preserve raw Databento venue for lineage (Venue Council Constraint #3).
    # Caller may pre-set source_venue_raw; auto-derive from pre-normalization
    # alias_string when caller omits it.
    if provider == "databento" and source_venue_raw is None and "." in alias_string:
        source_venue_raw = alias_string.rsplit(".", 1)[1]

    # Normalize Databento MIC → exchange-name at the write boundary so the
    # registry has ONE canonical alias convention (Venue Council Constraint #1).
    # IB aliases pass through unchanged. Unknown MICs raise UnknownDatabentoVenueError
    # which surfaces to the bootstrap service as outcome=unmapped_venue.
    alias_string = normalize_alias_for_registry(provider, alias_string)

    # Stable cross-process advisory lock key — blake2b digest, NOT Python
    # hash() which is PYTHONHASHSEED-randomized. Serializes concurrent
    # upserts for the same (provider, raw_symbol, asset_class) on different
    # workers too. Digest truncated to 63 bits (signed bigint for pg).
    lock_digest = hashlib.blake2b(
        f"{provider}:{raw_symbol}:{asset_class}".encode(),
        digest_size=8,
    ).digest()
    lock_key = int.from_bytes(lock_digest, "big", signed=False) & 0x7FFFFFFFFFFFFFFF
    await self._db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": lock_key},
    )

    # ... existing code from original line ~744 onward (def_stmt, etc.)
```

At the alias INSERT statement, add `source_venue_raw` to values:

```python
    ins_stmt = (
        pg_insert(InstrumentAlias)
        .values(
            instrument_uid=instrument_uid,
            alias_string=alias_string,
            venue_format=venue_format,
            provider=provider,
            effective_from=today,
            source_venue_raw=source_venue_raw,
        )
        .on_conflict_do_nothing(constraint="uq_instrument_aliases_string_provider_from")
    )
    await self._db.execute(ins_stmt)
```

- [ ] **Step 4: Run race test + source_venue_raw test — PASS**
- [ ] **Step 5: Run ALL existing `tests/unit/services/nautilus/security_master/` + `tests/integration/test_security_master_*.py` — no regressions**
- [ ] **Step 6: No commit.**

---

### Task 8: Pre-upsert divergence detection on IB-refresh

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py`
- Modify: `backend/src/msai/services/observability/trading_metrics.py` (add counter)
- Create: `backend/tests/integration/test_registry_venue_divergence.py`

- [ ] **Step 1: Register the counter (in trading_metrics.py)**

```python
# Append to backend/src/msai/services/observability/trading_metrics.py
REGISTRY_VENUE_DIVERGENCE_TOTAL = _r.counter(
    "msai_registry_venue_divergence_total",
    "Fires when IB refresh writes an alias whose venue differs from a prior "
    "Databento-authored alias for the same instrument definition. "
    "Labels applied at increment: databento_venue, ib_venue.",
)
```

- [ ] **Step 2: Write failing test**

```python
# backend/tests/integration/test_registry_venue_divergence.py
import pytest
from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.observability import get_registry

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_divergence_counter_fires_on_mismatch(session_factory):
    # Seed a Databento row: SPY.XARC → normalized to SPY.ARCA
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY", listing_venue="ARCA", routing_venue="SMART",
            asset_class="equity", alias_string="SPY.XARC",
            provider="databento", venue_format="mic_code",
        )
        await session.commit()

    before = get_registry().render()
    # IB refresh with a DIFFERENT venue (hypothetical migration ARCA → BATS)
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY", listing_venue="BATS", routing_venue="SMART",
            asset_class="equity", alias_string="SPY.BATS",
            provider="interactive_brokers", venue_format="exchange_name",
        )
        await session.commit()

    after = get_registry().render()
    # iter-3: labels sorted alphabetically per metrics.py:61 _format_labels();
    # value is rendered as "1.0" for a Counter (float-typed).
    assert 'msai_registry_venue_divergence_total{databento_venue="ARCA",ib_venue="BATS"} 1.0' in after


@pytest.mark.asyncio
async def test_divergence_counter_silent_on_match(session_factory):
    # Seed Databento SPY.XARC → normalized ARCA
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY", listing_venue="ARCA", routing_venue="SMART",
            asset_class="equity", alias_string="SPY.XARC",
            provider="databento", venue_format="mic_code",
        )
        await session.commit()

    before = get_registry().render()
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY", listing_venue="ARCA", routing_venue="SMART",
            asset_class="equity", alias_string="SPY.ARCA",
            provider="interactive_brokers", venue_format="exchange_name",
        )
        await session.commit()

    after = get_registry().render()
    assert before == after, "counter fired on matching venues"
```

- [ ] **Step 3: Modify `_upsert_definition_and_alias` — INSERT divergence check BEFORE definition UPSERT**

After the advisory lock and normalization, BEFORE `pg_insert(InstrumentDefinition) ... ON CONFLICT DO UPDATE`:

```python
    from msai.models.instrument_definition import InstrumentDefinition
    from msai.models.instrument_alias import InstrumentAlias
    from msai.services.observability.trading_metrics import (
        REGISTRY_VENUE_DIVERGENCE_TOTAL,
    )

    # US-009 divergence detection: an IB refresh overwriting a prior
    # Databento-authored active alias with a different venue surfaces a
    # potential REAL migration (e.g., ARCA→BATS ETF relisting). Must run
    # BEFORE the definition UPSERT mutates listing_venue via ON CONFLICT
    # DO UPDATE.
    if provider == "interactive_brokers":
        from sqlalchemy import select
        prior_row = await self._db.execute(
            select(InstrumentAlias.alias_string)
            .join(InstrumentDefinition, InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid)
            .where(
                InstrumentDefinition.raw_symbol == raw_symbol,
                InstrumentDefinition.asset_class == asset_class,
                InstrumentAlias.provider == "databento",
                InstrumentAlias.effective_to.is_(None),
            )
        )
        prior_alias_string = prior_row.scalar_one_or_none()
        if prior_alias_string is not None and "." in prior_alias_string and "." in alias_string:
            prior_venue = prior_alias_string.rsplit(".", 1)[1]
            new_venue = alias_string.rsplit(".", 1)[1]
            if prior_venue != new_venue:
                REGISTRY_VENUE_DIVERGENCE_TOTAL.labels(
                    databento_venue=prior_venue,
                    ib_venue=new_venue,
                ).inc()
                log.warning(
                    "registry_bootstrap_divergence",
                    raw_symbol=raw_symbol,
                    asset_class=asset_class,
                    previous_provider="databento",
                    previous_venue=prior_venue,
                    new_provider="interactive_brokers",
                    new_venue=new_venue,
                )
```

- [ ] **Step 4: Run tests — PASS (2/2)**
- [ ] **Step 5: No commit.**

---

## Phase 5 — Bootstrap orchestration service

### Task 9: `DatabentoBootstrapService` with session-per-symbol + correct asset_class

**Files:**

- Create: `backend/src/msai/services/nautilus/security_master/databento_bootstrap.py`
- Create: `backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_equities.py`

**KEY FIXES vs iter 1:**

- Constructor takes `async_sessionmaker`, not `AsyncSession` — one session per symbol, safe under `asyncio.gather`
- Reuses `asset_class_for_instrument_type(inst.__class__.__name__)` — no new mapper
- Reuses `is_databento_continuous_pattern` — no new regex
- `inst.raw_symbol.value` attribute access
- Uses typed errors from T5, not string match

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_equities.py
import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path

from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapOutcome,
    DatabentoBootstrapService,
)

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_bootstrap_aapl_returns_created(session_factory, mock_databento):
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
        max_concurrent=3,
    )
    results = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert len(results) == 1
    r = results[0]
    assert r.symbol == "AAPL"
    assert r.outcome == BootstrapOutcome.CREATED
    assert r.registered is True
    assert r.live_qualified is False
    assert r.canonical_id == "AAPL.NASDAQ"  # post-normalization
    assert r.asset_class == "equity"


@pytest.mark.asyncio
async def test_bootstrap_ambiguous_per_symbol(session_factory, mock_databento):
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
    )
    results = await svc.bootstrap(symbols=["AAPL", "BRK.B"], asset_class_override=None, exact_ids=None)
    outcomes = {r.symbol: r.outcome for r in results}
    assert outcomes["AAPL"] == BootstrapOutcome.CREATED
    assert outcomes["BRK.B"] == BootstrapOutcome.AMBIGUOUS
    brk_b = next(r for r in results if r.symbol == "BRK.B")
    assert len(brk_b.candidates) >= 2


@pytest.mark.asyncio
async def test_bootstrap_dataset_fallback(session_factory, mock_databento):
    """First dataset 401s; second succeeds. iter-3: stronger assertions
    — verifies ordered fallback AND that the final result uses the fallback
    dataset with a success outcome."""
    from msai.services.data_sources.databento_errors import DatabentoUnauthorizedError

    call_log = []

    async def _side_effect(symbol, start, end, *, dataset, target_path):
        call_log.append(dataset)
        if dataset == "XNAS.ITCH":
            raise DatabentoUnauthorizedError("401", http_status=401, dataset=dataset)
        from tests.integration.conftest_databento import _make_equity_instrument
        return [_make_equity_instrument("UNKN", "XARC")]

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)
    svc = DatabentoBootstrapService(session_factory=session_factory, databento_client=mock_databento)
    results = await svc.bootstrap(symbols=["UNKN"], asset_class_override=None, exact_ids=None)

    # Exact ordered fallback: XNAS.ITCH first, then XNYS.PILLAR (which succeeds)
    assert call_log == ["XNAS.ITCH", "XNYS.PILLAR"]
    # Result reflects the successful dataset, not the first-tried one
    assert results[0].outcome == BootstrapOutcome.CREATED
    assert results[0].dataset == "XNYS.PILLAR"
    assert results[0].registered is True


@pytest.mark.asyncio
async def test_max_concurrent_3_cap_honored(session_factory, mock_databento):
    import asyncio

    in_flight = {"max": 0, "current": 0}
    real_fn = mock_databento.fetch_definition_instruments.side_effect

    async def _tracked(symbol, start, end, *, dataset, target_path):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        await asyncio.sleep(0.05)
        try:
            return real_fn(symbol, start, end, dataset=dataset, target_path=target_path)
        finally:
            in_flight["current"] -= 1

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_tracked)
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento, max_concurrent=3,
    )
    await svc.bootstrap(symbols=["AAPL", "SPY", "QQQ", "AAPL", "SPY"], asset_class_override=None, exact_ids=None)
    assert in_flight["max"] <= 3
```

- [ ] **Step 2: Implement `DatabentoBootstrapService`**

```python
# backend/src/msai/services/nautilus/security_master/databento_bootstrap.py
"""On-demand Databento registry bootstrap service.

Session-per-symbol: the service takes an ``async_sessionmaker`` and opens
one new session + transaction per symbol. This makes concurrent
``asyncio.gather`` safe — AsyncSession is NOT safe to share across tasks.

Contract (scope + venue councils 2026-04-23):
- Databento-bootstrapped rows are backtest-discoverable ONLY. Live
  graduation requires a separate IB refresh.
- Per-symbol outcomes: CREATED / NOOP / ALIAS_ROTATED / AMBIGUOUS /
  UPSTREAM_ERROR / UNAUTHORIZED / UNMAPPED_VENUE / RATE_LIMITED.
- Batch: max_concurrent hard-capped at 3 in v1.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from msai.services.data_sources.databento_client import DatabentoClient

log = get_logger(__name__)


class BootstrapOutcome(str, enum.Enum):
    CREATED = "created"
    NOOP = "noop"
    ALIAS_ROTATED = "alias_rotated"
    AMBIGUOUS = "ambiguous"
    UPSTREAM_ERROR = "upstream_error"
    UNAUTHORIZED = "unauthorized"
    UNMAPPED_VENUE = "unmapped_venue"
    RATE_LIMITED = "rate_limited"


@dataclass
class BootstrapResult:
    symbol: str
    outcome: BootstrapOutcome
    registered: bool
    backtest_data_available: bool | None
    live_qualified: bool
    canonical_id: str | None = None
    dataset: str | None = None
    asset_class: str | None = None
    candidates: list[dict[str, str]] = field(default_factory=list)
    diagnostics: str | None = None


# Equity dataset tier (per OQ-1: all entitled on current plan).
_EQUITY_DATASETS = ("XNAS.ITCH", "XNYS.PILLAR", "ARCX.PILLAR")
_FUTURES_DATASET = "GLBX.MDP3"


class DatabentoBootstrapService:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession]",
        databento_client: "DatabentoClient",
        max_concurrent: int = 3,
    ) -> None:
        if not 1 <= max_concurrent <= 3:
            raise ValueError("max_concurrent must be 1..3 in v1")
        self._session_factory = session_factory
        self._databento = databento_client
        self._sem = asyncio.Semaphore(max_concurrent)

    async def bootstrap(
        self,
        *,
        symbols: list[str],
        asset_class_override: str | None,
        # iter-3: exact_ids keyed by SYMBOL, value is canonical alias_string from
        # candidates[] returned by a prior ambiguity 422 (NOT a numeric instrument_id).
        exact_ids: dict[str, str] | None,
    ) -> list[BootstrapResult]:
        exact_ids = exact_ids or {}
        tasks = [self._bootstrap_one(sym, asset_class_override, exact_ids) for sym in symbols]
        return await asyncio.gather(*tasks)

    async def _bootstrap_one(
        self,
        symbol: str,
        asset_class_override: str | None,
        exact_ids: dict[str, str],  # iter-3: value is canonical alias_string, not numeric id
    ) -> BootstrapResult:
        async with self._sem:
            from msai.services.nautilus.security_master.continuous_futures import (
                is_databento_continuous_pattern,
            )
            if is_databento_continuous_pattern(symbol):
                return await self._bootstrap_continuous_future(symbol, asset_class_override)
            return await self._bootstrap_equity(symbol, asset_class_override, exact_ids.get(symbol))

    async def _bootstrap_equity(
        self,
        symbol: str,
        asset_class_override: str | None,
        exact_id: str | None,
    ) -> BootstrapResult:
        from msai.services.data_sources.databento_client import AmbiguousDatabentoSymbolError
        from msai.services.data_sources.databento_errors import (
            DatabentoUnauthorizedError,
            DatabentoRateLimitedError,
            DatabentoUpstreamError,
        )
        from msai.services.nautilus.security_master.continuous_futures import (
            asset_class_for_instrument_type,
        )
        from msai.services.nautilus.security_master.venue_normalization import (
            UnknownDatabentoVenueError,
        )
        from msai.services.nautilus.security_master.service import SecurityMaster
        from datetime import date, timedelta

        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        last_error: BootstrapResult | None = None

        for dataset in _EQUITY_DATASETS:
            with TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / f"{symbol}.definition.dbn.zst"
                try:
                    # iter-3: pass exact_id down so the client pre-filters before
                    # raising ambiguous. Lets the "422 ambiguous → retry with
                    # exact_id" flow succeed on the second call.
                    instruments = await self._databento.fetch_definition_instruments(
                        symbol=symbol, start=today, end=tomorrow,
                        dataset=dataset, target_path=target,
                        exact_id=exact_id,
                    )
                except AmbiguousDatabentoSymbolError as exc:
                    return BootstrapResult(
                        symbol=symbol, outcome=BootstrapOutcome.AMBIGUOUS,
                        registered=False, backtest_data_available=False, live_qualified=False,
                        candidates=exc.candidates, dataset=dataset,
                    )
                except DatabentoUnauthorizedError as exc:
                    last_error = BootstrapResult(
                        symbol=symbol, outcome=BootstrapOutcome.UNAUTHORIZED,
                        registered=False, backtest_data_available=False, live_qualified=False,
                        diagnostics=str(exc), dataset=dataset,
                    )
                    continue  # try next dataset
                except DatabentoRateLimitedError as exc:
                    return BootstrapResult(
                        symbol=symbol, outcome=BootstrapOutcome.RATE_LIMITED,
                        registered=False, backtest_data_available=False, live_qualified=False,
                        diagnostics=str(exc), dataset=dataset,
                    )
                except DatabentoUpstreamError as exc:
                    last_error = BootstrapResult(
                        symbol=symbol, outcome=BootstrapOutcome.UPSTREAM_ERROR,
                        registered=False, backtest_data_available=False, live_qualified=False,
                        diagnostics=str(exc), dataset=dataset,
                    )
                    continue

                if not instruments:
                    continue

                # iter-3: no post-filter needed — fetch_definition_instruments now
                # pre-filters by exact_id when supplied (see T6 Step 2).

                inst = instruments[0]
                alias_string = str(inst.id.value)
                raw_symbol_str = (
                    inst.raw_symbol.value
                    if hasattr(inst, "raw_symbol") and hasattr(inst.raw_symbol, "value")
                    else symbol
                )
                derived_asset_class = (
                    asset_class_override
                    if asset_class_override is not None
                    else asset_class_for_instrument_type(inst.__class__.__name__)
                )

                async with self._session_factory() as session:
                    sm = SecurityMaster(db=session, databento_client=self._databento)

                    # Pre-check existing active Databento alias to classify outcome
                    existing_alias = await self._find_active_databento_alias(session, raw_symbol_str, derived_asset_class)

                    try:
                        await sm._upsert_definition_and_alias(
                            raw_symbol=raw_symbol_str,
                            listing_venue=self._extract_venue(alias_string),
                            routing_venue="SMART",
                            asset_class=derived_asset_class,
                            alias_string=alias_string,
                            provider="databento",
                            venue_format="mic_code",
                        )
                        await session.commit()
                    except UnknownDatabentoVenueError as exc:
                        await session.rollback()
                        return BootstrapResult(
                            symbol=symbol, outcome=BootstrapOutcome.UNMAPPED_VENUE,
                            registered=False, backtest_data_available=False, live_qualified=False,
                            diagnostics=str(exc), dataset=dataset,
                        )

                    # Compute the post-normalization canonical for the response.
                    from msai.services.nautilus.security_master.venue_normalization import (
                        normalize_alias_for_registry,
                    )
                    canonical_id = normalize_alias_for_registry("databento", alias_string)

                    # Classify outcome
                    if existing_alias is None:
                        outcome = BootstrapOutcome.CREATED
                    elif existing_alias == canonical_id:
                        outcome = BootstrapOutcome.NOOP
                    else:
                        outcome = BootstrapOutcome.ALIAS_ROTATED

                    live_qualified = await self._check_live_qualified(session, raw_symbol_str)

                return BootstrapResult(
                    symbol=symbol, outcome=outcome,
                    registered=True, backtest_data_available=None,
                    live_qualified=live_qualified,
                    canonical_id=canonical_id, dataset=dataset,
                    asset_class=derived_asset_class,
                )

        return last_error or BootstrapResult(
            symbol=symbol, outcome=BootstrapOutcome.UPSTREAM_ERROR,
            registered=False, backtest_data_available=False, live_qualified=False,
            diagnostics=f"symbol not found in any entitled equity dataset: {_EQUITY_DATASETS}",
        )

    async def _bootstrap_continuous_future(
        self,
        symbol: str,
        asset_class_override: str | None,
    ) -> BootstrapResult:
        """Reuse SecurityMaster._resolve_databento_continuous — existing code
        path that already handles .n.N / .c.N continuous-contract resolution.
        """
        from msai.services.nautilus.security_master.service import SecurityMaster
        from datetime import date

        async with self._session_factory() as session:
            # Pre-query current active alias to classify CREATED/NOOP/ALIAS_ROTATED
            # parity with _bootstrap_equity (US-005 idempotency for futures).
            existing_alias = await self._find_active_databento_alias(session, symbol, "futures")

            sm = SecurityMaster(db=session, databento_client=self._databento)
            try:
                resolved = await sm.resolve_for_backtest(
                    [symbol],
                    start=date.today().isoformat(),
                    end=None,
                    dataset=_FUTURES_DATASET,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                return BootstrapResult(
                    symbol=symbol, outcome=BootstrapOutcome.UPSTREAM_ERROR,
                    registered=False, backtest_data_available=False, live_qualified=False,
                    diagnostics=str(exc), dataset=_FUTURES_DATASET,
                )

            # Continuous-futures canonical IDs are synthetic strings like
            # "ES.Z.5.CME" (see test_continuous_futures_synthesis.py). Comparison
            # logic is the same — string equality against the existing alias.
            new_canonical = resolved[0] if resolved else None
            if existing_alias is None:
                outcome = BootstrapOutcome.CREATED
            elif existing_alias == new_canonical:
                outcome = BootstrapOutcome.NOOP
            else:
                outcome = BootstrapOutcome.ALIAS_ROTATED

            live_qualified = await self._check_live_qualified(session, symbol)

        return BootstrapResult(
            symbol=symbol, outcome=outcome,
            registered=True, backtest_data_available=None,
            live_qualified=live_qualified,
            canonical_id=new_canonical,
            dataset=_FUTURES_DATASET,
            asset_class="futures",
        )

    @staticmethod
    def _extract_venue(alias_string: str) -> str:
        return alias_string.rsplit(".", 1)[1] if "." in alias_string else ""

    @staticmethod
    async def _find_active_databento_alias(session, raw_symbol: str, asset_class: str) -> str | None:
        from sqlalchemy import select
        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition

        stmt = (
            select(InstrumentAlias.alias_string)
            .join(InstrumentDefinition, InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid)
            .where(
                InstrumentDefinition.raw_symbol == raw_symbol,
                InstrumentDefinition.asset_class == asset_class,
                InstrumentAlias.provider == "databento",
                InstrumentAlias.effective_to.is_(None),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _check_live_qualified(session, raw_symbol: str) -> bool:
        from sqlalchemy import select
        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition

        stmt = (
            select(InstrumentAlias.id)
            .join(InstrumentDefinition, InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid)
            .where(
                InstrumentDefinition.raw_symbol == raw_symbol,
                InstrumentAlias.provider == "interactive_brokers",
                InstrumentAlias.effective_to.is_(None),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None
```

- [ ] **Step 3: Run tests — PASS**
- [ ] **Step 4: No commit.**

---

### Task 10: Cover ALIAS_ROTATED + NOOP in integration

**Files:**

- Append: `backend/tests/integration/test_security_master_databento_bootstrap.py`

- [ ] **Step 1: Write integration tests**

```python
# in backend/tests/integration/test_security_master_databento_bootstrap.py
@pytest.mark.asyncio
async def test_same_symbol_twice_returns_noop(session_factory, mock_databento):
    svc = DatabentoBootstrapService(session_factory=session_factory, databento_client=mock_databento)
    first = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert first[0].outcome == BootstrapOutcome.CREATED

    second = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert second[0].outcome == BootstrapOutcome.NOOP


@pytest.mark.asyncio
async def test_ambiguous_then_exact_id_resolves_to_single_candidate(session_factory, mock_databento):
    """End-to-end: first POST → 422 ambiguous; second POST with
    exact_ids={SYMBOL: chosen_alias} → 200 created.

    iter-3: closes the gap that exact_id dispatch had no integration coverage
    in iter-2."""
    svc = DatabentoBootstrapService(session_factory=session_factory, databento_client=mock_databento)

    # First pass: BRK.B is ambiguous (per mock_databento fixture default)
    first = await svc.bootstrap(symbols=["BRK.B"], asset_class_override=None, exact_ids=None)
    assert first[0].outcome == BootstrapOutcome.AMBIGUOUS
    chosen = first[0].candidates[0]["alias_string"]  # e.g. "BRK.B.XNYS"

    # Second pass: provide exact_ids to disambiguate. Reconfigure the mock
    # to return the SAME two candidates — the service should filter by the
    # chosen alias and write only that one.
    # iter-3: reconfigure mock to return ONLY the chosen candidate (simulates
    # fetch_definition_instruments' exact_id pre-filter producing a single match).
    from tests.integration.conftest_databento import _make_equity_instrument
    mock_databento.fetch_definition_instruments = AsyncMock(return_value=[
        _make_equity_instrument("BRK.B", "XNYS"),
    ])

    second = await svc.bootstrap(
        symbols=["BRK.B"],
        asset_class_override=None,
        exact_ids={"BRK.B": chosen},
    )
    assert second[0].outcome == BootstrapOutcome.CREATED
    assert second[0].registered is True
    # canonical_id is the POST-normalization alias (XNYS → NYSE per the venue map).
    # iter-3 Codex P2: assert against normalized form, not the raw candidate string.
    assert second[0].canonical_id == "BRK.B.NYSE"


@pytest.mark.asyncio
async def test_changed_mic_returns_alias_rotated(session_factory, mock_databento):
    svc = DatabentoBootstrapService(session_factory=session_factory, databento_client=mock_databento)
    # Seed SPY.XARC (→ SPY.ARCA)
    await svc.bootstrap(symbols=["SPY"], asset_class_override=None, exact_ids=None)
    # Reconfigure mock to return a different venue for SPY
    def _rotated(symbol, start, end, *, dataset, target_path):
        from tests.integration.conftest_databento import _make_equity_instrument
        if symbol == "SPY":
            return [_make_equity_instrument("SPY", "BATS")]
    mock_databento.fetch_definition_instruments.side_effect = _rotated
    second = await svc.bootstrap(symbols=["SPY"], asset_class_override=None, exact_ids=None)
    assert second[0].outcome == BootstrapOutcome.ALIAS_ROTATED
    assert second[0].canonical_id == "SPY.BATS"
```

- [ ] **Step 2: Run — PASS (implementation already in T9)**
- [ ] **Step 3: No commit.**

---

## Phase 6 — API surface

### Task 11: Pydantic schemas (taxonomy-correct)

**Files:**

- Create: `backend/src/msai/schemas/instrument_bootstrap.py`
- Create: `backend/tests/unit/test_schemas_instrument_bootstrap.py`

- [ ] **Step 1: Write failing tests** (valid request, empty-symbols rejected, >50 symbols rejected, max_concurrent >3 rejected, provider=polygon rejected, `asset_class_override` accepts `equity|futures|fx|option` — NOT `etf|future`)

- [ ] **Step 2: Implement schemas**

```python
# backend/src/msai/schemas/instrument_bootstrap.py
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CandidateInfo(BaseModel):
    alias_string: str
    raw_symbol: str
    asset_class: str  # Nautilus class name or registry taxonomy — string-typed
    dataset: str


class BootstrapRequest(BaseModel):
    provider: Literal["databento"] = "databento"
    symbols: list[str] = Field(min_length=1, max_length=50)
    # Registry DB CHECK: equity | futures | fx | option | crypto.
    # 'etf' stores as 'equity'; 'future' is invalid (plural 'futures').
    asset_class_override: Literal["equity", "futures", "fx", "option"] | None = None
    max_concurrent: int = Field(default=3, ge=1, le=3)
    # iter-3: keyed by SYMBOL; value is canonical alias_string from candidates[]
    # returned by a prior ambiguity 422 (NOT a numeric instrument_id).
    exact_ids: dict[str, str] | None = None

    @field_validator("symbols")
    @classmethod
    def _well_formed(cls, v: list[str]) -> list[str]:
        import re
        pat = re.compile(r"^[A-Za-z0-9._/-]+$")
        for sym in v:
            if not (1 <= len(sym) <= 32) or not pat.match(sym):
                raise ValueError(f"invalid symbol: {sym!r}")
        return v

    @model_validator(mode="after")
    def _exact_ids_subset(self) -> "BootstrapRequest":
        if self.exact_ids:
            extra = set(self.exact_ids) - set(self.symbols)
            if extra:
                raise ValueError(f"exact_ids keys not in symbols: {extra}")
        return self


class BootstrapResultItem(BaseModel):
    symbol: str
    outcome: Literal[
        "created", "noop", "alias_rotated", "ambiguous",
        "upstream_error", "unauthorized", "unmapped_venue", "rate_limited",
    ]
    registered: bool
    backtest_data_available: bool | None = None
    live_qualified: bool
    canonical_id: str | None = None
    dataset: str | None = None
    asset_class: str | None = None
    candidates: list[CandidateInfo] = Field(default_factory=list)
    diagnostics: str | None = None


class BootstrapSummary(BaseModel):
    total: int
    created: int
    noop: int
    alias_rotated: int
    failed: int  # union of ambiguous/upstream_error/unauthorized/unmapped_venue/rate_limited


class BootstrapResponse(BaseModel):
    """Bootstrap response envelope. Construct via ``build_bootstrap_response``
    helper; direct construction with a stale ``summary`` is the caller's
    responsibility (no validator to re-compute — iter-3 dropped as over-engineered)."""
    results: list[BootstrapResultItem]
    summary: BootstrapSummary


def build_bootstrap_response(items: list[BootstrapResultItem]) -> BootstrapResponse:
    """Helper for API construction — computes summary from items."""
    failed_outcomes = {"ambiguous", "upstream_error", "unauthorized", "unmapped_venue", "rate_limited"}
    summary = BootstrapSummary(
        total=len(items),
        created=sum(1 for r in items if r.outcome == "created"),
        noop=sum(1 for r in items if r.outcome == "noop"),
        alias_rotated=sum(1 for r in items if r.outcome == "alias_rotated"),
        failed=sum(1 for r in items if r.outcome in failed_outcomes),
    )
    return BootstrapResponse(results=items, summary=summary)
```

- [ ] **Step 3: Run — PASS**
- [ ] **Step 4: No commit.**

---

### Task 12: `POST /api/v1/instruments/bootstrap` endpoint

**Files:**

- Create: `backend/src/msai/api/instruments.py`
- Modify: `backend/src/msai/main.py`
- Create: `backend/tests/integration/test_api_instruments_bootstrap.py`

**Ambiguity-contract pin:** ALL symbols failed → 422. ANY success + any failure → 207. All success → 200. Matches US-004 response-code logic and matches existing `api/live.py:881` precedent.

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/integration/test_api_instruments_bootstrap.py
#
# iter-3: Module-local `client` fixture overrides BOTH `get_session_factory`
# (pointing at the testcontainers session_factory) AND DatabentoClient
# (via the mock_databento fixture from conftest_databento). Pattern mirrors
# `test_backtests_api_uses_registry.py:130-160`.
from __future__ import annotations
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from msai.main import app
from msai.core.database import get_session_factory
from msai.services.data_sources.databento_client import DatabentoClient

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    mock_databento,  # from conftest_databento.py
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with get_session_factory and DatabentoClient overridden
    for this module only. Leaves the root `_override_auth` fixture in place
    (auto-used from tests/conftest.py)."""
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    # Patch DatabentoClient() at import site in api/instruments.py via
    # monkey-patching the class constructor to return our mock.
    import msai.api.instruments as instruments_module
    original_cls = instruments_module.DatabentoClient
    instruments_module.DatabentoClient = lambda *a, **kw: mock_databento
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
    finally:
        instruments_module.DatabentoClient = original_cls
        app.dependency_overrides.pop(get_session_factory, None)


@pytest.mark.asyncio
async def test_all_success_returns_200(client):
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["AAPL"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["created"] == 1


@pytest.mark.asyncio
async def test_mixed_returns_207(client):
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["AAPL", "BRK.B"]},
    )
    assert resp.status_code == 207
    body = resp.json()
    outcomes = {r["symbol"]: r["outcome"] for r in body["results"]}
    assert outcomes["AAPL"] == "created"
    assert outcomes["BRK.B"] == "ambiguous"


@pytest.mark.asyncio
async def test_all_failed_returns_422(client):
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["BRK.B"]},  # single ambiguous
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["results"][0]["outcome"] == "ambiguous"


@pytest.mark.asyncio
async def test_unsupported_provider_returns_422_pydantic_envelope(client):
    """provider: Literal['databento'] rejects 'polygon' at Pydantic parse time.
    Returns Pydantic's default 422 envelope (NOT the project's
    {error:{code,message}} envelope). Documented here as an intentional
    boundary — business-logic failures below use the project envelope."""
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "polygon", "symbols": ["AAPL"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_symbols_returns_422(client):
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": []},
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Implement endpoint**

```python
# backend/src/msai/api/instruments.py
"""Instrument registry API — Databento bootstrap.

POST /api/v1/instruments/bootstrap registers equity/ETF/futures symbols
using the Databento definition schema. Per-symbol outcomes with three
explicit readiness-state flags. Status codes: 200 (all success), 207
(mixed), 422 (all failed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from msai.core.auth import get_current_user
from msai.core.database import get_session_factory
from msai.schemas.instrument_bootstrap import (
    BootstrapRequest,
    BootstrapResponse,
    BootstrapResultItem,
    CandidateInfo,
    build_bootstrap_response,
)
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapResult,
    DatabentoBootstrapService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])


@router.post(
    "/bootstrap",
    response_model=BootstrapResponse,
    responses={
        207: {"model": BootstrapResponse, "description": "Partial success"},
        422: {"description": "All symbols failed OR request validation error"},
    },
)
async def bootstrap_instruments(
    request: BootstrapRequest,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    _claims: dict[str, Any] = Depends(get_current_user),
) -> JSONResponse:
    """Bootstrap a batch of symbols into the registry via Databento."""
    databento_client = DatabentoClient()
    if not databento_client.api_key:
        return JSONResponse(
            status_code=500,
            content={"error": {
                "code": "DATABENTO_NOT_CONFIGURED",
                "message": "DATABENTO_API_KEY environment variable not set on server",
            }},
        )

    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=databento_client,
        max_concurrent=request.max_concurrent,
    )
    results = await svc.bootstrap(
        symbols=request.symbols,
        asset_class_override=request.asset_class_override,
        exact_ids=request.exact_ids,
    )

    response_items = [_to_item(r) for r in results]
    response = build_bootstrap_response(response_items)

    num_success = sum(1 for r in results if r.registered)
    num_failure = len(results) - num_success
    status_code = 200 if num_failure == 0 else (207 if num_success > 0 else 422)

    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


def _to_item(r: BootstrapResult) -> BootstrapResultItem:
    return BootstrapResultItem(
        symbol=r.symbol,
        outcome=r.outcome.value,
        registered=r.registered,
        backtest_data_available=r.backtest_data_available,
        live_qualified=r.live_qualified,
        canonical_id=r.canonical_id,
        dataset=r.dataset,
        asset_class=r.asset_class,
        candidates=[CandidateInfo(**c) for c in r.candidates],
        diagnostics=r.diagnostics,
    )
```

- [ ] **Step 3: Register router in `main.py`**

- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: No commit.**

---

## Phase 7 — CLI

### Task 13: `msai instruments bootstrap` — bypasses `_api_call` for 207/422 acceptance

**Files:**

- Modify: `backend/src/msai/cli.py`
- Create: `backend/tests/unit/test_cli_instruments_bootstrap.py`

**Why bypass `_api_call`:** it calls `_fail` on any non-2xx (cli.py:179), exiting the process. Bootstrap's 207 Multi-Status is the common mixed-batch case. A thin internal helper reuses `_api_base` + `_api_headers` without the exit-on-non-2xx behavior.

- [ ] **Step 1: Write failing CLI tests**

```python
# backend/tests/unit/test_cli_instruments_bootstrap.py
import json
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
from msai.cli import app

runner = CliRunner()


def test_required_flags():
    r = runner.invoke(app, ["instruments", "bootstrap"])
    assert r.exit_code == 2


def test_unsupported_provider_rejected():
    r = runner.invoke(app, ["instruments", "bootstrap", "--provider", "polygon", "--symbols", "AAPL"])
    assert r.exit_code != 0


def test_success_prints_json_and_exits_0():
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {
            "results": [{"symbol": "AAPL", "outcome": "created", "registered": True,
                          "backtest_data_available": None, "live_qualified": False,
                          "canonical_id": "AAPL.NASDAQ", "dataset": "XNAS.ITCH",
                          "asset_class": "equity", "candidates": [], "diagnostics": None}],
            "summary": {"total": 1, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 0},
        })
        r = runner.invoke(app, ["instruments", "bootstrap", "--provider", "databento", "--symbols", "AAPL"])
    assert r.exit_code == 0


def test_207_partial_exits_nonzero_but_prints_payload():
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(status_code=207, json=lambda: {
            "results": [
                {"symbol": "AAPL", "outcome": "created", "registered": True, "backtest_data_available": None, "live_qualified": False, "canonical_id": "AAPL.NASDAQ", "dataset": "XNAS.ITCH", "asset_class": "equity", "candidates": [], "diagnostics": None},
                {"symbol": "BRK.B", "outcome": "ambiguous", "registered": False, "backtest_data_available": False, "live_qualified": False, "canonical_id": None, "dataset": "XNYS.PILLAR", "asset_class": None, "candidates": [{"alias_string": "BRK.B.XNYS", "raw_symbol": "BRK.B", "asset_class": "Equity", "dataset": "XNYS.PILLAR"}], "diagnostics": None},
            ],
            "summary": {"total": 2, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 1},
        })
        r = runner.invoke(app, ["instruments", "bootstrap", "--provider", "databento", "--symbols", "AAPL,BRK.B"])
    assert r.exit_code != 0
    import json
    payload = json.loads(r.stdout)
    assert payload["summary"]["failed"] == 1


def test_exact_id_parses_alias_string():
    """--exact-id SYMBOL:ALIAS_STRING parses — mocked httpx so the CLI completes
    without trying a real network call. iter-3: was previously a tautology
    that didn't mock httpx; now exercises the parsing + request-body path."""
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {
            "results": [{"symbol": "BRK.B", "outcome": "created", "registered": True,
                          "backtest_data_available": None, "live_qualified": False,
                          "canonical_id": "BRK.B.NYSE", "dataset": "XNYS.PILLAR",
                          "asset_class": "equity", "candidates": [], "diagnostics": None}],
            "summary": {"total": 1, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 0},
        })
        r = runner.invoke(app, [
            "instruments", "bootstrap", "--provider", "databento",
            "--symbols", "BRK.B", "--exact-id", "BRK.B:BRK.B.XNYS",
        ])
    # The CLI must have sent exact_ids in the request body
    assert mock_req.call_count == 1
    sent_body = mock_req.call_args.kwargs["json"]
    assert sent_body["exact_ids"] == {"BRK.B": "BRK.B.XNYS"}
    assert r.exit_code == 0
```

- [ ] **Step 2: Add CLI subcommand (bypassing `_api_call`)**

```python
# backend/src/msai/cli.py — inside instruments_app block.
# iter-3 ADDITIONAL IMPORTS needed at module top (if not already present):
#   from enum import Enum

class _AssetClassChoice(str, Enum):
    """Typer-native choice constraint (iter-3: replaces `click.Choice` which
    required importing `click` separately)."""
    equity = "equity"
    futures = "futures"
    fx = "fx"
    option = "option"


@instruments_app.command("bootstrap")
def instruments_bootstrap(
    provider: str = typer.Option(..., "--provider", help="Provider: 'databento'"),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated symbols (e.g. AAPL,SPY,ES.n.0)"),
    asset_class: _AssetClassChoice | None = typer.Option(
        None, "--asset-class",
        help="Override auto-detection (registry taxonomy: equity|futures|fx|option)",
    ),
    max_concurrent: int = typer.Option(3, "--max-concurrent", min=1, max=3),
    exact_id: list[str] = typer.Option([], "--exact-id", help="SYMBOL:ALIAS_STRING (repeatable)"),
) -> None:
    """Bootstrap equity/ETF/futures symbols into the registry via Databento.

    Registers symbols as backtest-discoverable. Does NOT qualify live IB
    instruments — run `msai instruments refresh --provider interactive_brokers`
    before live deployment.
    """
    if provider != "databento":
        _fail(f"Unsupported provider {provider!r} for bootstrap. Supported: databento.")

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        _fail("no symbols provided")

    # iter-3: exact_ids value is a canonical alias_string (e.g., "BRK.B.XNYS")
    # from a prior ambiguity 422's candidates[] — NOT a numeric instrument_id.
    exact_ids: dict[str, str] = {}
    for pair in exact_id:
        sym, sep, alias_str = pair.rpartition(":")
        if not sep or not sym or not alias_str:
            _fail(f"--exact-id expects SYMBOL:ALIAS_STRING format, got {pair!r}")
        exact_ids[sym] = alias_str

    body: dict[str, Any] = {
        "provider": provider,
        "symbols": symbol_list,
        "max_concurrent": max_concurrent,
    }
    if asset_class is not None:
        body["asset_class_override"] = asset_class.value  # Enum → string for JSON
    if exact_ids:
        body["exact_ids"] = exact_ids

    # Bypass _api_call because it _fail()s on any non-2xx — bootstrap's 207
    # Multi-Status partial-success is the dominant case.
    url = f"{_api_base()}/api/v1/instruments/bootstrap"
    try:
        response = httpx.request(
            "POST", url, json=body, headers=_api_headers(), timeout=60.0,
        )
    except httpx.ConnectError:
        _fail(f"Connection refused — is the backend running at {_api_base()}?")
    except httpx.RequestError as exc:
        _fail(f"Request failed: {type(exc).__name__}: {exc}")

    if response.status_code not in (200, 207, 422):
        _fail(f"API error ({response.status_code}): {response.text}")

    payload = response.json()

    # Human-readable per-symbol summary on stderr
    for item in payload.get("results", []):
        sym = item["symbol"]
        out = item["outcome"]
        msg = f"{sym} → {out}"
        if item.get("canonical_id"):
            msg += f" ({item['canonical_id']})"
        if item.get("diagnostics"):
            msg += f" [{item['diagnostics']}]"
        typer.echo(msg, err=True)

    summary = payload.get("summary", {})
    typer.echo(
        f"\nSummary: {summary.get('total', 0)} total · "
        f"{summary.get('created', 0)} created · "
        f"{summary.get('noop', 0)} noop · "
        f"{summary.get('alias_rotated', 0)} rotated · "
        f"{summary.get('failed', 0)} failed",
        err=True,
    )

    # Structured JSON on stdout (house style)
    _emit_json(payload)

    if summary.get("failed", 0) > 0 or response.status_code != 200:
        raise typer.Exit(code=1)
```

- [ ] **Step 3: Run — PASS**
- [ ] **Step 4: No commit.**

---

## Phase 8 — Observability (remaining counters/histogram)

### Task 14: Register databento API-call counter + bootstrap counter + latency histogram

**Files:**

- Modify: `backend/src/msai/services/observability/trading_metrics.py`
- Modify: `backend/src/msai/services/data_sources/databento_client.py` (emit API-call counter)
- Modify: `backend/src/msai/services/nautilus/security_master/databento_bootstrap.py` (emit bootstrap counter + histogram)

- [ ] **Step 1: Append to `trading_metrics.py`**

```python
# Databento API observability
DATABENTO_API_CALLS_TOTAL = _r.counter(
    "msai_databento_api_calls_total",
    "Databento API calls partitioned by endpoint and outcome. "
    "Outcomes: success, rate_limited_recovered, rate_limited_failed, "
    "unauthorized, upstream_error.",
)

# Registry bootstrap outcomes (per-symbol)
REGISTRY_BOOTSTRAP_TOTAL = _r.counter(
    "msai_registry_bootstrap_total",
    "Registry bootstrap outcomes partitioned by provider, asset_class, outcome.",
)

# Bootstrap latency histogram (1 symbol end-to-end).
# iter-3: Histogram.__init__ takes `tuple[int, ...]` (see metrics.py:193, enforced
# by `mypy --strict` post-PR #43). Use milliseconds (int-typed) to match existing
# histogram callers' int convention (trading_metrics.py:62 uses byte-int buckets).
# Millisecond buckets: 100ms, 500ms, 1s, 2s, 5s, 10s, 30s.
_BOOTSTRAP_BUCKETS_MS = (100, 500, 1_000, 2_000, 5_000, 10_000, 30_000)
REGISTRY_BOOTSTRAP_DURATION_MS = _r.histogram(
    "msai_registry_bootstrap_duration_ms",
    "End-to-end latency per bootstrap operation (1 symbol), in milliseconds.",
    buckets=_BOOTSTRAP_BUCKETS_MS,
)
```

- [ ] **Step 2: Emit from `databento_client.py` tenacity block**

Inside the retry try/except, after each attempt, increment:

```python
from msai.services.observability.trading_metrics import DATABENTO_API_CALLS_TOTAL
# on success:
DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="success").inc()
# on 401/403:
DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="unauthorized").inc()
# on 429 (final failure):
DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="rate_limited_failed").inc()
# on 429 (recovered mid-retry): tenacity's before_sleep hook
# on 5xx (final failure):
DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="upstream_error").inc()
```

- [ ] **Step 3: Emit from bootstrap service**

iter-3: The project's hand-rolled `Histogram` (see `services/observability/metrics.py:178`) has `observe(value)` + `render()` — there is NO `.time()` context-manager helper. Use `time.perf_counter()` manually:

```python
import time
from msai.services.observability.trading_metrics import (
    REGISTRY_BOOTSTRAP_TOTAL,
    REGISTRY_BOOTSTRAP_DURATION_MS,
)


async def _bootstrap_one(self, ...) -> BootstrapResult:
    async with self._sem:
        start = time.perf_counter()
        try:
            result = await (...)  # existing dispatch to _bootstrap_equity / _bootstrap_continuous_future
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1_000)
            REGISTRY_BOOTSTRAP_DURATION_MS.observe(elapsed_ms)
        REGISTRY_BOOTSTRAP_TOTAL.labels(
            provider="databento",
            asset_class=result.asset_class or "unknown",
            outcome=result.outcome.value,
        ).inc()
        return result
```

- [ ] **Step 4: Write tests** that assert via `registry.render()`:

```python
def test_bootstrap_counter_emits_created():
    # ... run bootstrap on AAPL ...
    rendered = get_registry().render()
    # iter-3: label order is ALPHABETICAL per metrics.py:61 _format_labels() —
    # asset_class before outcome before provider.
    assert 'msai_registry_bootstrap_total{asset_class="equity",outcome="created",provider="databento"} 1.0' in rendered
```

- [ ] **Step 5: Run — PASS**
- [ ] **Step 6: No commit.**

---

## Phase 9 — PRD sync

### Task 15: Update PRD US-009 to reflect normalization

**Files:**

- Modify: `docs/prds/databento-registry-bootstrap.md`

- [ ] **Step 1:** Find US-009 section. Change "increments counter on IB/Databento venue mismatch" to "increments counter when the POST-NORMALIZATION venue differs from the IB venue — real migrations (e.g. SPY moves ARCA→BATS) only. Notation-only differences like XNAS vs NASDAQ no longer fire because normalization maps both to NASDAQ at write time."

- [ ] **Step 2:** No commit (Phase 0 rule).

---

## Phase 10 — E2E use cases

Interface type: `fullstack` per CLAUDE.md. All UCs below are API + CLI. ARRANGE through the API only; no DB reads; no direct file injection.

### UC-DRB-001: Bootstrap AAPL via API + immediate backtest (happy path)

**Interface:** API
**ARRANGE:** ensure DB has no AAPL rows via a GET against `/api/v1/instruments/bootstrap` NOT being available for pre-check; so treat the test as destructive — run it on a fresh DB or use a symbol guaranteed not to be in registry (or just re-run: subsequent calls return `noop` which is also a pass). Dev stack running.
**Steps:**

1. `POST /api/v1/instruments/bootstrap {"provider":"databento","symbols":["AAPL"]}`
2. `POST /api/v1/backtests/run` with `BacktestRunRequest` shape per `schemas/backtest.py:16` — iter-3 corrected field names:

   ```json
   {
     "strategy_id": "<EMA Cross strategy UUID>",
     "config": {},
     "instruments": ["AAPL.NASDAQ"],
     "start_date": "2024-01-02",
     "end_date": "2024-01-10"
   }
   ```

   Note: `instruments` takes canonical (post-normalization) alias strings, NOT bare tickers. `config` is strategy-specific and may be `{}` for EMA Cross defaults.

3. Poll `/api/v1/backtests/{id}/status` until `status=completed`.

**VERIFY:**

- Bootstrap response: HTTP 200 OR 207 (depending on state), `results[0].outcome ∈ {created, noop}`, `registered=true`, `canonical_id="AAPL.NASDAQ"`, `dataset="XNAS.ITCH"`, `live_qualified=false`.
- Backtest `/status` endpoint returns `completed` (not `failed_missing_data`).
- A subsequent `POST /api/v1/instruments/bootstrap` for AAPL returns `outcome="noop"`.

### UC-DRB-002: Bootstrap SPY via CLI

**Interface:** CLI (ARRANGE) + API (VERIFY)
**Steps:**

1. `msai instruments bootstrap --provider databento --symbols SPY`
2. Verify stderr: `SPY → created (SPY.ARCA)` or `SPY → noop (SPY.ARCA)`.
3. Verify stdout is valid JSON: `summary.total==1`.
4. Re-run `POST /api/v1/instruments/bootstrap` for SPY via API — response `outcome="noop"`.
   **VERIFY:** CLI exit code 0 on fresh DB; subsequent re-runs also exit 0.

### UC-DRB-003: Ambiguous symbol returns 422 via API

**Interface:** API
**Steps:** `POST /api/v1/instruments/bootstrap {"provider":"databento","symbols":["BRK.B"]}`
**VERIFY:**

- HTTP **422** (single-symbol all-failed).
- `results[0].outcome="ambiguous"`, `candidates[]` non-empty with `alias_string`/`raw_symbol`/`asset_class`/`dataset` per candidate.

### UC-DRB-004: Idempotent re-run outcome via API only (NO DB READS)

**Interface:** API
**Steps:**

1. Bootstrap AAPL.
2. Re-run same request.
   **VERIFY (API-only, no DB count):**

- First response: `outcome="created"`.
- Second response: `outcome="noop"`.

### UC-DRB-005: Bootstrap ES continuous futures

**Interface:** CLI + API
**Steps:**

1. `msai instruments bootstrap --provider databento --symbols ES.n.0`
2. Verify outcome `created` and `asset_class="futures"` and `dataset="GLBX.MDP3"`.
3. `POST /api/v1/backtests/run` with `BacktestRunRequest` shape:

   ```json
   {
     "strategy_id": "<strategy UUID>",
     "config": {},
     "instruments": ["ES.n.0.CME"],
     "start_date": "2024-06-01",
     "end_date": "2024-06-05"
   }
   ```

   iter-3 (post-Codex review): the existing `raw_symbol_from_request` helper at `continuous_futures.py:42` PRESERVES the caller's requested continuous symbol verbatim and only appends `.CME`. So `ES.n.0` → `ES.n.0.CME`, NOT a month-rewritten `ES.Z.5.CME`. Copy the exact canonical_id returned by the bootstrap response into the backtest `instruments` field.

**VERIFY:** Bootstrap `canonical_id == "ES.n.0.CME"` (raw_symbol-preserved + venue suffix). Backtest completes against GLBX.MDP3 bars.

### UC-DRB-006: Two-step graduation — live_qualified flag

**Interface:** API
**ARRANGE:** Bootstrap AAPL via Databento only.
**Steps:**

1. `POST /api/v1/live/start-portfolio` with a portfolio that includes AAPL.
2. Capture the HTTP error code and envelope.
3. `msai instruments refresh --provider interactive_brokers --symbols AAPL` (if IB Gateway is running; otherwise skip this UC per live-trading safety rails).
4. Re-bootstrap AAPL via Databento → verify `live_qualified=true`.
   **VERIFY:** step 2 returns a deterministic error code (capture the actual code from the first real run; the PRD assumes `422 UNKNOWN_SYMBOL` but whatever `lookup_for_live` actually emits is the contract — document in verify-e2e report). Step 4's `live_qualified=true` confirms the IB row is visible.

**Note:** UC-005 and UC-006 require Databento metadata calls that cost money; opt-in via `RUN_PAPER_E2E=1` env var (matches the existing smoke-test pattern at `tests/e2e/test_instruments_refresh_ib_smoke.py`).

---

## Dispatch Plan

| Task | Depends on  | Writes                                                                                                                                                                                       |
| ---- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T0   | —           | `backend/tests/integration/conftest_databento.py`                                                                                                                                            |
| T1   | —           | `backend/pyproject.toml`, `backend/uv.lock`                                                                                                                                                  |
| T2   | —           | `backend/src/msai/core/database.py` (append `get_session_factory`)                                                                                                                           |
| T3   | T0          | `backend/alembic/versions/a5b6c7d8e9f0_*.py`, `backend/src/msai/models/instrument_alias.py`, `backend/tests/integration/test_alembic_migrations.py` (append)                                 |
| T4   | —           | `backend/src/msai/services/nautilus/security_master/venue_normalization.py`, `backend/tests/unit/services/nautilus/security_master/test_venue_normalization.py`                              |
| T5   | T1          | `backend/src/msai/services/data_sources/databento_errors.py`, `backend/src/msai/services/data_sources/databento_client.py`, `backend/tests/unit/test_databento_client_retry.py`              |
| T6   | T5          | `backend/src/msai/services/data_sources/databento_client.py`, `backend/tests/unit/test_databento_client_ambiguity.py`                                                                        |
| T7   | T3, T4      | `backend/src/msai/services/nautilus/security_master/service.py`, `backend/tests/integration/test_security_master_advisory_lock.py`                                                           |
| T8   | T7, T14     | `backend/src/msai/services/nautilus/security_master/service.py`, `backend/src/msai/services/observability/trading_metrics.py`, `backend/tests/integration/test_registry_venue_divergence.py` |
| T9   | T5, T6, T7  | `backend/src/msai/services/nautilus/security_master/databento_bootstrap.py`, `backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_equities.py`                     |
| T10  | T9          | `backend/tests/integration/test_security_master_databento_bootstrap.py`                                                                                                                      |
| T11  | —           | `backend/src/msai/schemas/instrument_bootstrap.py`, `backend/tests/unit/test_schemas_instrument_bootstrap.py`                                                                                |
| T12  | T2, T9, T11 | `backend/src/msai/api/instruments.py`, `backend/src/msai/main.py`, `backend/tests/integration/test_api_instruments_bootstrap.py`                                                             |
| T13  | T12         | `backend/src/msai/cli.py`, `backend/tests/unit/test_cli_instruments_bootstrap.py`                                                                                                            |
| T14  | T1          | `backend/src/msai/services/observability/trading_metrics.py`, + emit sites in T5 & T9 files (already listed)                                                                                 |
| T15  | T8          | `docs/prds/databento-registry-bootstrap.md`                                                                                                                                                  |

**Parallel-safe sets:** (T1, T3, T4, T11, T14) can run concurrently after T0. (T5, T6) serial after T1. T7 requires T4+T3+T14. T9 requires T5+T6+T7. T12 requires T9+T11. T13 requires T12.

**Conflict note:** T7+T8 both modify `service.py` → must serialize. T14+T8 both modify `trading_metrics.py` → T14 first.

---

## Self-Review v2

- [x] **All iter-1 P0s addressed:**
  - P0 Metrics API drift → T14 uses `services/observability/metrics.py` + `trading_metrics.py` hand-rolled API; tests assert via `registry.render()`.
  - P0 `require_auth` → T12 uses `get_current_user` + `Depends(get_session_factory)`.
  - P0 `BentoClientError` constructor → T5 tests use real `BentoClientError(http_status=N, message=..., http_body=b"")`; retry predicate catches `BentoClientError`+`BentoServerError`.
  - P0 `_api_call` auto-fails on 207 → T13 CLI bypasses `_api_call` entirely, uses `httpx.request` directly.
  - P0 (Codex) Shared session race → T9 takes `async_sessionmaker`, opens new session per symbol inside `_bootstrap_equity`.

- [x] **All iter-1 P1s addressed:**
  - Alembic in integration/, chain from `z4x5y6z7a8b9`, uses `_run_alembic_upgrade`/`_run_alembic` — T3.
  - `hash()` randomness → T7 uses `hashlib.blake2b`.
  - Asset class taxonomy `equity|futures|fx|option|crypto` — T9 via `asset_class_for_instrument_type`, T11 schema literal.
  - `Instrument.raw_symbol.value` — T6 and T9.
  - `is_databento_continuous_pattern` reuse — T9.
  - Divergence counter placement BEFORE upsert — T8.
  - String-match error classification → typed errors — T5.
  - `--asset-class click.Choice` — T13.
  - Pydantic `mode="after"` for summary — T11.
  - Ambiguity contract pinned: 200/207/422 per success counts — T12 + UC-DRB-003.
  - E2E no DB reads — UC-DRB-004 verifies via API response only.
  - UC-DRB-006 captures actual error code at run-time, not asserted in advance.
  - tenacity `asyncio.to_thread` — T5.

- [x] **All iter-1 P2s addressed or tracked:**
  - T0 commits removed (Phase 0 no-commit rule per workflow gate).
  - MIC map adds EPRL.
  - Source venue raw test added in T7.
  - Dataset fallback test added in T9.
  - Tenacity retry outcome classification via except-clause (not per-attempt hook).
  - `SecurityMaster` single-instance per session (T9).

- [x] **Venue Council 3 constraints:** fail-loud map (T4), named helper `normalize_alias_for_registry` (T4), provenance via `source_venue_raw` column (T3+T7).
- [x] **Scope Council 7 constraints:** all mapped.

---

## Execution Handoff

**Plan v2 complete.** Proceed to Phase 3.3 iter 2: re-review (Claude + Codex). Expected findings: 0-3 P1/P2 (productive convergence per project history — iter 1 caught big structural mismatches, iter 2 narrows to detail polish).

After plan review passes:

- **Subagent-Driven (recommended)** — fresh subagent per task, review diff between.
- **Inline Execution** — batch checkpoints via `superpowers:executing-plans`.
