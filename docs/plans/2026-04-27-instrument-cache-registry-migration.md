# Instrument Cache → Registry Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the legacy `instrument_cache` Postgres table and the closed-universe `canonical_instrument_id()` helper, leaving `instrument_definitions` + `instrument_aliases` as the SOLE registry for instrument metadata — with the runtime aligned to the Symbol Onboarding PRD's "single source of truth" promise.

**Architecture:** One PR ships (a) two Alembic revisions — additive `trading_hours JSONB NULL` column on `instrument_definitions`, then data migration + `DROP TABLE instrument_cache`; (b) hard cutover of every runtime reader (`SecurityMaster`, `MarketHoursService`, CLI `instruments refresh`); (c) full deletion of both `canonical_instrument_id()` definition sites + the `SecurityMaster.resolve_for_live` cold-miss canonicalization path; (d) preflight script + runbook + branch-local restart drill; (e) replacement structural-guard test that fails CI on any future re-import of legacy symbols. CLI `instruments refresh` replaces the closed-universe map with per-asset-class `IBContract` factories that delegate venue resolution to IB's qualifier — closing Simplifier's circular-CLI catch.

**Tech Stack:** SQLAlchemy 2.0 (asyncio) · Alembic >=1.14 (single-direction data migration via `op.get_bind()` + `pg_insert(...).on_conflict_do_nothing()` over reflected tables) · NautilusTrader 1.222+ (`InteractiveBrokersInstrumentProvider`, `CacheConfig(database=redis)`) · ib_async via Nautilus IB adapter · PostgreSQL 16 (additive JSONB column is metadata-only, no table rewrite) · Python 3.12 stdlib `ast` + `zoneinfo` · ruff `flake8-tidy-imports.banned-api` (optional belt+suspenders).

---

## Approach Comparison

### Chosen Default

**Combined PR — single hard cutover.** Cache-cutover + full `canonical_instrument_id()` removal ship in one branch. Two Alembic revisions inside the branch (additive column, then data + DROP). Code rewrites + migrations + worker restart ship as one coordinated cut.

### Best Credible Alternative

**Split PR — cache cutover ships first, `canonical_instrument_id()` removal ships second after one paper-week soak.** Smaller blast radius per merge, easier diagnostic isolation if live behavior breaks, but knowingly leaves a release boundary where one of the two parallel data paths still exists.

### Scoring (fixed axes)

| Axis                  | Combined | Split |
| --------------------- | -------- | ----- |
| Complexity            | M        | L     |
| Blast Radius          | M        | L     |
| Reversibility         | L        | M     |
| Time to Validate      | M        | L     |
| User/Correctness Risk | L        | M     |

### Cheapest Falsifying Test

`grep -rn "canonical_instrument_id\|InstrumentCache" backend/src` post-merge. If non-zero hits remain, the architecture is half-migrated and the council's clean-end-state directive is violated. < 30 sec.

## Contrarian Verdict

**COUNCIL** (full 5-advisor council ran 2026-04-27, Codex xhigh chairman).

5 advisors returned CONDITIONAL on the migration scope. Q1 (combined vs split) split 2/2/1 across advisors; chairman ruled **(a) combined** on the basis that splitting "would knowingly ship a release boundary where either `instrument_cache` or `canonical_instrument_id()` still acts as a second truth source," and that the right mitigation for blast radius is preflight + restart drill, not a half-migrated architecture. The Live-Trading Safety Officer's split argument was **overruled on sequencing** but its **safety mechanisms adopted in full** (preflight gate, branch-local restart drill, dead-code removal). The Simplifier's **circular-CLI catch on Q9 is binding**: the CLI replacement uses direct provider/root normalization + IB qualification, NOT a registry lookup, because the CLI is what _seeds_ the registry. The Maintainer's **cold-miss-removal extension on Q9 is binding**: `SecurityMaster.resolve_for_live` cold-miss canonicalization is removed entirely so live resolution is registry-read-only.

Full verdict + minority report persisted in [`docs/prds/instrument-cache-registry-migration-discussion.md`](../prds/instrument-cache-registry-migration-discussion.md) and PRD §9 ([`docs/prds/instrument-cache-registry-migration.md`](../prds/instrument-cache-registry-migration.md)).

---

## Files

### Files to create

| Path                                                                                   | Responsibility                                                                                                                                                 |
| -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/alembic/versions/d1e2f3g4h5i6_add_trading_hours_to_instrument_definitions.py` | Revision A — additive `trading_hours JSONB NULL` column on `instrument_definitions`. Reversible (downgrade drops col).                                         |
| `backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py`                       | Revision B — copy `instrument_cache` rows → registry, then `DROP TABLE instrument_cache`. Schema-only downgrade.                                               |
| `backend/scripts/preflight_cache_migration.py`                                         | Operator preflight — validates active deployments' `LivePortfolioRevisionStrategy.instruments` resolve through registry via `lookup_for_live`; aborts on miss. |
| `backend/tests/integration/test_instrument_cache_migration.py`                         | Migration round-trip test (upgrade + downgrade A); preflight pass-case + fail-case.                                                                            |
| `backend/tests/unit/services/nautilus/test_market_hours_registry_backed.py`            | New unit test for `MarketHoursService` reading `instrument_definitions.trading_hours`.                                                                         |
| `docs/runbooks/instrument-cache-migration.md`                                          | Operator runbook (`pg_dump` → preflight → `alembic upgrade head` → restart workers → smoke).                                                                   |

### Files to modify

| Path                                                                             | Change                                                                                                                                                                                                                                                                                                                                 |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/src/msai/models/instrument_definition.py`                               | Add `trading_hours: Mapped[dict[str, Any] \| None]` JSONB column. Drop the "Coexistence note" docstring once the migration ships.                                                                                                                                                                                                      |
| `backend/src/msai/services/nautilus/market_hours.py`                             | Rewire `MarketHoursService.prime()` to `select InstrumentDefinition.trading_hours`. Drop `instrument_cache` import + docstring references.                                                                                                                                                                                             |
| `backend/src/msai/services/nautilus/security_master/service.py`                  | Delete `_read_cache`, `_read_cache_bulk`, `_write_cache`, `_instrument_from_cache_row`, `_ROLL_SENSITIVE_ROOTS`. Rewrite `resolve()`, `bulk_resolve()`, `resolve_for_live()` registry-only with fail-loud on cold-miss. Rewire `_trading_hours_for` to write `instrument_definitions.trading_hours` on `_upsert_definition_and_alias`. |
| `backend/src/msai/services/nautilus/instruments.py`                              | Delete `canonical_instrument_id()` definition. Inline its body into `default_bar_type()`. Remove docstring references to deprecated closed-universe path.                                                                                                                                                                              |
| `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`                | Delete `canonical_instrument_id()` + `_es_front_month_local_symbol()` (its only caller). Keep `current_quarterly_expiry`, `_FUT_MONTH_CODES`, `phase_1_paper_symbols`, `_STATIC_SYMBOLS`, `build_ib_instrument_provider_config` — still used by the closed-universe live-supervisor smoke path until that's replaced separately.       |
| `backend/src/msai/cli.py` (lines 730–860)                                        | Replace `accepted` map (built via `canonical_instrument_id`) with per-asset-class `_build_ib_contract_for_symbol(symbol, asset_class, today)` factories. Add `--asset-class {stk,fut,cash}` flag (default `stk`).                                                                                                                      |
| `backend/src/msai/models/__init__.py`                                            | Remove `InstrumentCache` from `__all__` + import.                                                                                                                                                                                                                                                                                      |
| `backend/src/msai/models/instrument_cache.py`                                    | **DELETE** the entire file.                                                                                                                                                                                                                                                                                                            |
| `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` | Rewrite as `test_legacy_symbol_isolation.py` — broaden AST walk to all of `backend/src/msai/`, expand forbidden-name list to `{canonical_instrument_id, InstrumentCache, _read_cache, _read_cache_bulk, _write_cache, _instrument_from_cache_row, _ROLL_SENSITIVE_ROOTS}`, allowlist Alembic migrations + this test file.              |
| `backend/tests/integration/test_security_master.py`                              | Migrate fixtures: replace `instrument_cache` writes with `instrument_definitions` + `instrument_aliases` writes via the existing `_upsert_definition_and_alias` helper.                                                                                                                                                                |
| `backend/tests/integration/test_security_master_resolve_live.py`                 | Migrate fixtures + adjust assertions for fail-loud cold-miss (new `RegistryMissError` instead of `canonical_instrument_id` fall-through).                                                                                                                                                                                              |
| `backend/tests/integration/test_security_master_resolve_backtest.py`             | Already registry-only; minor edits to drop any residual `instrument_cache` fixture writes.                                                                                                                                                                                                                                             |
| `backend/tests/e2e/test_security_master_phase2.py`                               | Migrate fixtures to registry semantics.                                                                                                                                                                                                                                                                                                |
| `backend/tests/unit/test_live_instrument_bootstrap.py`                           | Delete tests for `canonical_instrument_id` (lines 209–228 per recon). Keep tests for `current_quarterly_expiry`, `phase_1_paper_symbols`, `build_ib_instrument_provider_config`.                                                                                                                                                       |
| `backend/tests/unit/test_instruments.py`                                         | Delete tests calling `canonical_instrument_id` (line 42, 45). Keep `resolve_instrument` + `default_bar_type` tests, adjusting for inlined behavior.                                                                                                                                                                                    |
| `backend/tests/unit/test_cli_instruments_refresh.py`                             | Migrate test cases that assert on `canonical_instrument_id("ES")` (line 189) — replace with assertions on the new `_build_ib_contract_for_symbol` factory output.                                                                                                                                                                      |

### Files to delete

| Path                                                       | Reason                       |
| ---------------------------------------------------------- | ---------------------------- |
| `backend/src/msai/models/instrument_cache.py`              | Model retired in Revision B. |
| `backend/tests/integration/test_instrument_cache_model.py` | Tests for retired model.     |

---

## Task Dispatch Plan

> Default = **sequential**. Most tasks share `services/nautilus/security_master/service.py` or have ordering dependencies. Parallel exceptions noted explicitly.

| Task ID | Depends on     | Writes (concrete file paths)                                                                                                                                                                                                       |
| ------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T0      | —              | `CONTINUITY.md`, `docs/plans/2026-04-27-instrument-cache-registry-migration.md` (this file already exists; T0 is the workflow-tracking commit)                                                                                     |
| T1      | T0             | `backend/alembic/versions/d1e2f3g4h5i6_add_trading_hours_to_instrument_definitions.py`, `backend/src/msai/models/instrument_definition.py`                                                                                         |
| T2      | T1             | `backend/src/msai/services/nautilus/market_hours.py`, `backend/tests/unit/services/nautilus/test_market_hours_registry_backed.py`                                                                                                  |
| T3      | T1             | `backend/src/msai/services/nautilus/security_master/service.py` (registry-only `resolve` + `bulk_resolve`; delete cache IO)                                                                                                        |
| T4      | T3             | `backend/src/msai/services/nautilus/security_master/service.py` (delete `_ROLL_SENSITIVE_ROOTS`, fail-loud cold-miss in `resolve_for_live`)                                                                                        |
| T5      | T4             | `backend/src/msai/cli.py` (lines 730–860 — `_build_ib_contract_for_symbol`; `--asset-class` flag), `backend/tests/unit/test_cli_instruments_refresh.py`                                                                            |
| T6      | T5             | `backend/src/msai/services/nautilus/instruments.py`, `backend/tests/unit/test_instruments.py`                                                                                                                                      |
| T7      | T5, T6         | `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`, `backend/tests/unit/test_live_instrument_bootstrap.py`                                                                                                          |
| T8      | T1             | `backend/scripts/preflight_cache_migration.py` (parallel-eligible with T2–T7 — independent file)                                                                                                                                   |
| T9      | T2, T3, T4, T7 | `backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py` (data migration + DROP)                                                                                                                                           |
| T10     | T9             | `backend/src/msai/models/instrument_cache.py` (DELETE), `backend/src/msai/models/__init__.py`                                                                                                                                      |
| T11     | T9, T10        | `backend/tests/integration/{test_security_master.py, test_security_master_resolve_live.py, test_security_master_resolve_backtest.py, test_instrument_cache_model.py (DELETE)}`, `backend/tests/e2e/test_security_master_phase2.py` |
| T12     | T7, T10        | `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` (rewrite)                                                                                                                                         |
| T13     | T8, T9         | `backend/tests/integration/test_instrument_cache_migration.py` (parallel-eligible with T11–T12)                                                                                                                                    |
| T14     | T9             | `docs/runbooks/instrument-cache-migration.md` (parallel-eligible with T9–T13 — docs only)                                                                                                                                          |

**Parallel windows:**

- T1 + T8 + T14 (drafting): no shared files.
- T2 + T8: independent files (`market_hours.py` vs `scripts/preflight_cache_migration.py`).
- T11 + T13 + T14: T11 touches integration tests; T13 creates a new integration test file; T14 is docs only.
- T3, T4, T5, T6, T7 are **strictly sequential** — they share files or have hard ordering dependencies.

**E2E use cases (Phase 3.2b)** — see "E2E Use Cases" section at the end of this plan. Designed in this plan; executed in Phase 5.4.

---

## Implementation Notes

### Pattern: Alembic data migration via reflected `Table` + `pg_insert.on_conflict_do_nothing()`

Per research finding #1 + PR #44 plan-review iter-3: do NOT import the `InstrumentDefinition` model from inside the migration's `upgrade()` body. Importing the model is brittle (Alembic loads each migration in a context where the model imports may pick up forward-ref types at the wrong time). Use SQLAlchemy reflection over `op.get_bind()` instead:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    cache = sa.Table("instrument_cache", metadata, autoload_with=bind)
    defs = sa.Table("instrument_definitions", metadata, autoload_with=bind)
    aliases = sa.Table("instrument_aliases", metadata, autoload_with=bind)

    rows = bind.execute(sa.select(cache)).mappings().all()
    for row in rows:
        # ... build definition dict, alias dict ...
        bind.execute(
            postgresql.insert(defs).values(...).on_conflict_do_nothing(
                index_elements=["raw_symbol", "provider", "asset_class"]
            )
        )
        bind.execute(
            postgresql.insert(aliases).values(...).on_conflict_do_nothing(
                constraint="uq_instrument_aliases_string_provider_from"
            )
        )
    op.drop_table("instrument_cache")
```

### Pattern: per-asset-class `IBContract` factories (T5)

Per research finding #2 + Simplifier's circular-CLI catch: the CLI cannot read from the registry to canonicalize symbols (because the CLI is what _seeds_ the registry — circular). Instead, normalize at the asset-class level + delegate venue resolution to IB:

```python
def _build_ib_contract_for_symbol(
    symbol: str,
    *,
    asset_class: str,  # one of "stk" / "fut" / "cash"
    today: date,
    primary_exchange: str = "NASDAQ",  # STK only — caller's --primary-exchange flag
) -> IBContract:
    if asset_class == "stk":
        return IBContract(secType="STK", symbol=symbol, exchange="SMART",
                          primaryExchange="NASDAQ", currency="USD")
    if asset_class == "fut":
        return IBContract(secType="FUT", symbol=symbol, exchange="CME",
                          lastTradeDateOrContractMonth=current_quarterly_expiry(today),
                          currency="USD")
    if asset_class == "cash":
        base, quote = (symbol.split("/", 1) if "/" in symbol else (symbol, "USD"))
        return IBContract(secType="CASH", symbol=base, exchange="IDEALPRO",
                          currency=quote)
    raise ValueError(f"Unknown asset class {asset_class!r}")
```

IB's qualifier resolves the canonical alias (`AAPL.NASDAQ`, `ESM6.CME`, `EUR/USD.IDEALPRO`) at qualification time. The CLI doesn't pre-canonicalize; IB does it.

### Pattern: registry-only fail-loud cold-miss (T4)

Per Maintainer's binding objection: `SecurityMaster.resolve_for_live` cold-miss path is removed entirely. Registry miss raises a typed error with operator-action hint:

```python
class RegistryMissError(Exception):
    """Raised by SecurityMaster.resolve_for_live when the registry has
    no active alias for a requested symbol. Operator action: pre-warm
    the registry via `msai instruments refresh --symbols X --provider
    interactive_brokers` (or --provider databento for backtest)."""

    def __init__(self, symbol: str, provider: str = "interactive_brokers") -> None:
        self.symbol = symbol
        self.provider = provider
        super().__init__(
            f"No registry row for {symbol!r} under provider {provider!r}. "
            f"Pre-warm via `msai instruments refresh --symbols {symbol} "
            f"--provider {provider}`."
        )
```

### Pattern: fixture-data direct write to `instrument_definitions.trading_hours`

After T1, tests can write to the new column directly via the model's mapper. After T9, the column is also backfilled from `instrument_cache` for any production data. Tests use the explicit-write path; production uses the migration backfill.

### TDD discipline reminder

Each task follows Red → Green → Refactor → Commit. The test must FAIL with a meaningful message BEFORE the implementation lands. If the test passes accidentally, that's a hint the implementation already exists or the test is wrong.

### Workflow gate reminder

Per `feedback_workflow_gate_blocks_preflight_commits.md`: the project's PreToolUse hook blocks `git commit` until Phase 5 quality gates clear. Task commit steps below are aspirational — they may execute mid-phase or land later as a single squash. Don't `--no-verify`; let the gate behave normally.

---

## Task 0: Persist plan + workflow tracking

**Goal:** Phase 3 done; Phase 4 starting. Update CONTINUITY checklist + ensure plan file is on disk.

**Files:**

- Modify: `CONTINUITY.md` (Workflow checklist — check off "Plan written")

- [ ] **Step 1: Update CONTINUITY checklist**

Edit `CONTINUITY.md` Workflow section: change "Phase: 3 — Design / Next step: write implementation plan" → "Phase: 4 — Execute / Next step: T1 (Alembic Revision A)". Check off "Plan written" with one-line summary referencing this plan file.

- [ ] **Step 2: Verify plan file exists + lints**

Run: `ls -la docs/plans/2026-04-27-instrument-cache-registry-migration.md`
Expected: file exists, > 1500 lines.

Run: `python -c "import re; t = open('docs/plans/2026-04-27-instrument-cache-registry-migration.md').read(); assert '## Files' in t; assert '## Task Dispatch Plan' in t; assert 'TBD' not in t and 'TODO' not in t.upper()"`
Expected: exits 0 (plan has required sections + no placeholders).

- [ ] **Step 3: Do NOT commit**

Per workflow-gate hook, no commits until Phase 5. T0 work stays on disk uncommitted.

---

## Task 1: Alembic Revision A — add `trading_hours JSONB NULL` to `instrument_definitions`

**Goal:** Additive schema change. New column lives alongside the legacy `instrument_cache.trading_hours` until T9 backfills + T2 rewires the reader. Reversible via `alembic downgrade -1`.

**Files:**

- Create: `backend/alembic/versions/d1e2f3g4h5i6_add_trading_hours_to_instrument_definitions.py`
- Modify: `backend/src/msai/models/instrument_definition.py`
- Test: `backend/tests/integration/test_instrument_cache_migration.py` (created here — extended in T13)

- [ ] **Step 1: Find current Alembic head**

Run: `cd backend && uv run alembic heads`
Expected: prints the most recent revision id (likely `c7d8e9f0a1b2` from PR #45 — confirm).

Record the head as `<PRIOR_HEAD>` for the new revision's `down_revision`.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/integration/test_instrument_cache_migration.py`:

```python
"""Integration tests for instrument-cache → registry migration revisions.

Revision A: d1e2f3g4h5i6 (additive trading_hours column).
Revision B: e2f3g4h5i6j7 (data migration + DROP instrument_cache).

Uses the project's testcontainers Postgres pattern from
test_instrument_cache_model.py / test_security_master.py — per-module
session_factory + isolated_postgres_url fixtures.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REV_A = "d1e2f3g4h5i6"


def _run_alembic(cmd: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    """Run an alembic CLI command against a specific DB URL."""
    import os
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run(
        ["uv", "run", "alembic", *cmd],
        cwd="backend",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.asyncio
async def test_revision_a_adds_trading_hours_column_to_instrument_definitions(
    isolated_postgres_url: str,
) -> None:
    # Arrange — migrate up to PRIOR head, confirm column doesn't exist
    _run_alembic(["upgrade", f"{REV_A}^"], isolated_postgres_url)
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='instrument_definitions' "
            "AND column_name='trading_hours'"
        ))
        assert result.scalar_one_or_none() is None, "column should not exist before rev A"

    # Act — migrate up to revision A
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)

    # Assert — column exists with correct type + nullable
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='instrument_definitions' AND column_name='trading_hours'"
        ))
        row = result.one_or_none()
        assert row is not None, "trading_hours column missing after rev A"
        assert row[0] == "jsonb", f"expected jsonb, got {row[0]}"
        assert row[1] == "YES", f"expected nullable, got {row[1]}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_a_downgrade_removes_trading_hours_column(
    isolated_postgres_url: str,
) -> None:
    # Arrange — at revision A
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)

    # Act — downgrade
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)

    # Assert — column is gone
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='instrument_definitions' AND column_name='trading_hours'"
        ))
        assert result.scalar_one_or_none() is None, "trading_hours column should be dropped"
    await engine.dispose()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py::test_revision_a_adds_trading_hours_column_to_instrument_definitions -v`
Expected: FAIL (revision `d1e2f3g4h5i6` doesn't exist yet).

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/d1e2f3g4h5i6_add_trading_hours_to_instrument_definitions.py`:

```python
"""add trading_hours JSONB to instrument_definitions

Revision A of the instrument-cache → registry migration. Additive only —
adds the nullable JSONB column. Data is backfilled from the legacy
`instrument_cache.trading_hours` column in Revision B; until then, the
new column reads NULL for all rows and `MarketHoursService` fail-opens
(returns "always tradeable") on missing data, preserving today's
behavior.

Reversible: downgrade drops the column. Safe to run on a populated
instrument_definitions table — Postgres 16 ADD COLUMN with no default
is metadata-only and does not rewrite the table.

Revision: d1e2f3g4h5i6
Revises: <PRIOR_HEAD>
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d1e2f3g4h5i6"
down_revision = "<PRIOR_HEAD>"  # SUBSTITUTE the prior alembic head from Step 1
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_definitions",
        sa.Column("trading_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instrument_definitions", "trading_hours")
```

Substitute `<PRIOR_HEAD>` with the value recorded in Step 1.

- [ ] **Step 5: Add the column to the SQLAlchemy model**

Edit `backend/src/msai/models/instrument_definition.py` — add after the existing column declarations (preserve docstring + class structure):

```python
trading_hours: Mapped[dict[str, Any] | None] = mapped_column(
    JSONB,
    nullable=True,
)
"""Per-instrument RTH/ETH window data (legacy schema from
``instrument_cache.trading_hours`` — migrated here on 2026-04-27).
Schema: ``{"timezone": str, "rth": [{"day", "open", "close"}], "eth": [...]}``.
NULL means "no schedule data" — :class:`MarketHoursService` fail-opens
(returns True) on NULL, preserving the legacy behavior."""
```

Also add the imports if not already present at the top of the file:

```python
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
```

Drop the "Coexistence note" docstring that mentions `instrument_cache` (legacy guidance — no longer accurate post-migration).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py -v`
Expected: both tests PASS.

Run: `cd backend && uv run alembic heads`
Expected: prints `d1e2f3g4h5i6` as the head.

Run: `cd backend && uv run mypy src/msai/models/instrument_definition.py --strict`
Expected: 0 errors.

- [ ] **Step 7: Commit (best-effort)**

```bash
git add backend/alembic/versions/d1e2f3g4h5i6_add_trading_hours_to_instrument_definitions.py \
        backend/src/msai/models/instrument_definition.py \
        backend/tests/integration/test_instrument_cache_migration.py
git commit -m "feat(registry): add trading_hours JSONB column to instrument_definitions"
```

If the workflow-gate hook blocks the commit, that's expected — the work stays on disk.

---

## Task 2: Rewire `MarketHoursService.prime()` to read from `instrument_definitions.trading_hours`

**Goal:** `MarketHoursService` no longer reads `instrument_cache.trading_hours`. Behavior preserved — same RTH/ETH window logic, fail-open semantics on missing data.

**Files:**

- Modify: `backend/src/msai/services/nautilus/market_hours.py`
- Test: `backend/tests/unit/services/nautilus/test_market_hours_registry_backed.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/services/nautilus/test_market_hours_registry_backed.py`:

```python
"""Unit test for MarketHoursService reading instrument_definitions.trading_hours.

Replaces the legacy `instrument_cache.trading_hours` read path. Verifies:

1. prime() loads via the registry (instrument_definitions + instrument_aliases).
2. is_in_rth/eth fail-open on NULL (preserves legacy behavior).
3. is_in_rth correctly evaluates a window in the column's stored timezone.

Uses the project's testcontainers Postgres pattern.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentAlias, InstrumentDefinition
from msai.services.nautilus.market_hours import MarketHoursService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture
async def session_factory(isolated_postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.mark.asyncio
async def test_prime_loads_trading_hours_from_instrument_definitions(
    session: AsyncSession,
) -> None:
    # Arrange — seed AAPL.NASDAQ with NYSE-style trading hours
    aapl_uid = uuid4()
    session.add(InstrumentDefinition(
        instrument_uid=aapl_uid,
        raw_symbol="AAPL",
        provider="interactive_brokers",
        asset_class="equity",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        lifecycle_state="active",
        trading_hours={
            "timezone": "America/New_York",
            "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}],
            "eth": [{"day": "MON", "open": "04:00", "close": "20:00"}],
        },
    ))
    session.add(InstrumentAlias(
        id=uuid4(),
        instrument_uid=aapl_uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
        effective_to=None,
    ))
    await session.commit()

    # Act — prime the service
    svc = MarketHoursService()
    await svc.prime(session, ["AAPL.NASDAQ"])

    # Assert — Monday 10:00 NY is RTH; Monday 03:00 is not even ETH; Monday 05:00 is ETH not RTH
    monday_10am_et = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)  # 10:00 ET
    monday_3am_et = datetime(2026, 4, 27, 7, 0, tzinfo=UTC)    # 03:00 ET
    monday_5am_et = datetime(2026, 4, 27, 9, 0, tzinfo=UTC)    # 05:00 ET
    assert svc.is_in_rth("AAPL.NASDAQ", monday_10am_et) is True
    assert svc.is_in_rth("AAPL.NASDAQ", monday_5am_et) is False
    assert svc.is_in_eth("AAPL.NASDAQ", monday_5am_et) is True
    assert svc.is_in_eth("AAPL.NASDAQ", monday_3am_et) is False


@pytest.mark.asyncio
async def test_prime_fail_open_on_missing_alias(session: AsyncSession) -> None:
    # Arrange — registry is empty
    # Act
    svc = MarketHoursService()
    await svc.prime(session, ["UNKNOWN.NASDAQ"])

    # Assert — fail-open: never primed → True regardless of timestamp
    assert svc.is_in_rth("UNKNOWN.NASDAQ", datetime(2026, 4, 27, 14, 0, tzinfo=UTC)) is True


@pytest.mark.asyncio
async def test_prime_fail_open_on_null_trading_hours(session: AsyncSession) -> None:
    # Arrange — alias exists but trading_hours is NULL (e.g. forex on 24h venue)
    eur_uid = uuid4()
    session.add(InstrumentDefinition(
        instrument_uid=eur_uid,
        raw_symbol="EUR/USD",
        provider="interactive_brokers",
        asset_class="fx",
        listing_venue="IDEALPRO",
        routing_venue="IDEALPRO",
        lifecycle_state="active",
        trading_hours=None,  # NULL — 24h venue
    ))
    session.add(InstrumentAlias(
        id=uuid4(),
        instrument_uid=eur_uid,
        alias_string="EUR/USD.IDEALPRO",
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
        effective_to=None,
    ))
    await session.commit()

    # Act
    svc = MarketHoursService()
    await svc.prime(session, ["EUR/USD.IDEALPRO"])

    # Assert — NULL → fail-open
    sunday_3am_utc = datetime(2026, 4, 26, 3, 0, tzinfo=UTC)  # forex closed on Sunday morning
    assert svc.is_in_rth("EUR/USD.IDEALPRO", sunday_3am_utc) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_market_hours_registry_backed.py -v`
Expected: FAIL — `MarketHoursService.prime()` still reads from `InstrumentCache`, so `AAPL.NASDAQ` won't be loaded.

- [ ] **Step 3: Rewrite `MarketHoursService.prime()`**

Edit `backend/src/msai/services/nautilus/market_hours.py`:

Replace the module docstring's first sentence and update the `MarketHoursService` class docstring + the `prime()` method. The full new `prime()` implementation:

```python
async def prime(self, session: AsyncSession, canonical_ids: list[str]) -> None:
    """Pre-load trading hours for ``canonical_ids`` from the
    ``instrument_definitions`` table (joined via the active alias
    in ``instrument_aliases``). Call once at deployment startup
    with the strategy's universe so the synchronous read path
    never blocks on a DB call.

    Cold-miss canonical_ids that don't resolve to a registry alias
    today are recorded as ``None`` in the in-memory cache so the
    synchronous reader fails-open without re-querying.
    """
    from sqlalchemy import select

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    # Join: alias_string IN (canonical_ids) → instrument_uid → trading_hours
    # We only return the active alias (effective_to IS NULL) per
    # canonical_id; if there are multiple matches due to provider
    # split, we accept any provider's row since trading_hours is
    # the same instrument regardless of provider.
    stmt = (
        select(InstrumentAlias.alias_string, InstrumentDefinition.trading_hours)
        .join(
            InstrumentDefinition,
            InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid,
        )
        .where(InstrumentAlias.alias_string.in_(canonical_ids))
        .where(InstrumentAlias.effective_to.is_(None))
    )
    result = await session.execute(stmt)
    seen: set[str] = set()
    for canonical_id, trading_hours in result:
        # Multiple providers can map the same alias_string; first wins
        # (any provider's trading_hours is identical for the same instrument).
        if canonical_id not in seen:
            self._cache[canonical_id] = trading_hours
            seen.add(canonical_id)

    # Anything we asked for but didn't find — record as
    # "no data" so the synchronous reader doesn't keep
    # logging cache-miss warnings.
    for canonical_id in canonical_ids:
        if canonical_id not in self._cache:
            self._cache[canonical_id] = None
```

Also update the module docstring (lines 1–35) — the first sentence:

```python
"""MarketHoursService — read trading hours from the instrument
registry (``instrument_definitions.trading_hours`` joined via
``instrument_aliases``) and answer "is this instrument tradeable
right now?" (Phase 4 task 4.3).
```

And the `MarketHoursService` class docstring (line 132–139):

```python
class MarketHoursService:
    """Per-process service that loads trading hours from the
    instrument registry and answers RTH/ETH questions.

    Construction is async because the cache primer hits the
    DB. The instance is then used synchronously from the hot
    path (Nautilus strategy callbacks).
    """
```

Drop the "Phase 2's instrument cache rarely changes" sentence (lines 14–17) — replace with: "Trading hours change rarely (DST transitions, exchange schedule revisions); the in-memory snapshot is good enough until a future periodic-refresh task lands."

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_market_hours_registry_backed.py -v`
Expected: 3 PASS.

Run: `cd backend && uv run ruff check src/msai/services/nautilus/market_hours.py`
Expected: clean.

Run: `cd backend && uv run mypy src/msai/services/nautilus/market_hours.py --strict`
Expected: 0 errors.

- [ ] **Step 5: Verify no regression in pre-existing market_hours tests**

Run: `cd backend && uv run pytest tests/unit/services/nautilus/test_market_hours.py -v`
(If the file exists.) Expected: PASS.

Run: `cd backend && rg -n "instrument_cache\|InstrumentCache" src/msai/services/nautilus/market_hours.py`
Expected: 0 matches.

- [ ] **Step 6: Commit (best-effort)**

```bash
git add backend/src/msai/services/nautilus/market_hours.py \
        backend/tests/unit/services/nautilus/test_market_hours_registry_backed.py
git commit -m "refactor(market_hours): read trading_hours from instrument_definitions"
```

---

## Task 3: `SecurityMaster.resolve` + `bulk_resolve` registry-only — delete cache IO

**Goal:** Strip every `instrument_cache` read/write from `SecurityMaster`. `resolve(spec)` becomes a registry alias lookup → IB qualify on miss → upsert via `_upsert_definition_and_alias`. `bulk_resolve(specs)` is the bulk variant. Adds the missing `InstrumentRegistry.find_by_aliases_bulk` method first so `bulk_resolve` has its dependency.

**Iter-1 fix:** uses `InstrumentSpec(asset_class=, symbol=, venue=, ...)` — the actual fields per `specs.py:76-103`. Earlier draft used non-existent `raw_symbol/listing_venue/routing_venue` on the spec. The model-level `listing_venue`/`routing_venue` are derived at upsert time from the qualified instrument's IB contract-details (matches the existing pattern at `service.py:402-415`).

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py`
- Modify: `backend/src/msai/services/nautilus/security_master/registry.py` (add `find_by_aliases_bulk`)
- Test: `backend/tests/integration/test_security_master.py` (partial — full migration in T11)
- Test: `backend/tests/integration/test_instrument_registry.py` (new test for `find_by_aliases_bulk`)

- [ ] **Step 1: Write the failing test for `find_by_aliases_bulk`**

Append to `backend/tests/integration/test_instrument_registry.py`:

```python
@pytest.mark.asyncio
async def test_find_by_aliases_bulk_returns_dict_of_active_aliases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """find_by_aliases_bulk maps every active alias to its definition.
    Misses are absent from the dict so callers can use `in` for membership.

    Uses the per-module `session_factory` fixture pattern (the only fixture
    defined in this test module — no shared `session` fixture exists).
    """
    today = date(2026, 4, 27)

    async with session_factory() as session:
        # Seed AAPL.NASDAQ + MSFT.NASDAQ (active); seed FOO.NASDAQ effective_to in past
        for raw, alias_str, eff_to in (
            ("AAPL", "AAPL.NASDAQ", None),
            ("MSFT", "MSFT.NASDAQ", None),
            ("FOO", "FOO.NASDAQ", date(2026, 1, 1)),  # closed alias
        ):
            uid = uuid4()
            session.add(InstrumentDefinition(
                instrument_uid=uid, raw_symbol=raw, provider="interactive_brokers",
                asset_class="equity", listing_venue="NASDAQ", routing_venue="SMART",
                lifecycle_state="active",
            ))
            session.add(InstrumentAlias(
                id=uuid4(), instrument_uid=uid, alias_string=alias_str,
                venue_format="exchange_name", provider="interactive_brokers",
                effective_from=date(2026, 1, 1), effective_to=eff_to,
            ))
        await session.commit()

        registry = InstrumentRegistry(session)
        result = await registry.find_by_aliases_bulk(
            ["AAPL.NASDAQ", "MSFT.NASDAQ", "FOO.NASDAQ", "MISS.NASDAQ"],
            provider="interactive_brokers",
            as_of_date=today,
        )

    # Assert
    assert set(result.keys()) == {"AAPL.NASDAQ", "MSFT.NASDAQ"}, (
        "FOO.NASDAQ has effective_to in the past — should be absent. "
        "MISS.NASDAQ has no row — should be absent."
    )
    assert result["AAPL.NASDAQ"].raw_symbol == "AAPL"
    assert result["MSFT.NASDAQ"].raw_symbol == "MSFT"


@pytest.mark.asyncio
async def test_find_by_aliases_bulk_empty_input_returns_empty_dict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        registry = InstrumentRegistry(session)
        result = await registry.find_by_aliases_bulk(
            [], provider="interactive_brokers", as_of_date=date.today(),
        )
    assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_instrument_registry.py -v -k "find_by_aliases_bulk"`
Expected: FAIL — `find_by_aliases_bulk` doesn't exist.

- [ ] **Step 3: Add `InstrumentRegistry.find_by_aliases_bulk`**

Edit `backend/src/msai/services/nautilus/security_master/registry.py`. Add a new method after `find_by_alias` (around line 110):

```python
async def find_by_aliases_bulk(
    self,
    alias_strings: list[str],
    *,
    provider: str,
    as_of_date: date,
) -> dict[str, InstrumentDefinition]:
    """Return a dict mapping ``alias_string`` → :class:`InstrumentDefinition`
    for every alias in ``alias_strings`` that has an active row at
    ``as_of_date`` under ``provider``.

    Misses are absent from the dict (NOT mapped to ``None``), so callers
    can use ``alias_string in result`` for warm-hit membership checks.

    One SELECT for the entire batch — bounded by the input size.
    """
    if not alias_strings:
        return {}
    stmt = (
        select(InstrumentAlias, InstrumentDefinition)
        .join(
            InstrumentDefinition,
            InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid,
        )
        .where(InstrumentAlias.alias_string.in_(alias_strings))
        .where(InstrumentAlias.provider == provider)
        .where(InstrumentAlias.effective_from <= as_of_date)
        .where(
            or_(
                InstrumentAlias.effective_to.is_(None),
                InstrumentAlias.effective_to > as_of_date,
            )
        )
    )
    rows = await self.session.execute(stmt)
    out: dict[str, InstrumentDefinition] = {}
    for alias, idef in rows:
        out[alias.alias_string] = idef
    return out
```

- [ ] **Step 4: Run test — should now pass**

Run: `cd backend && uv run pytest tests/integration/test_instrument_registry.py -v -k "find_by_aliases_bulk"`
Expected: 2 PASS.

- [ ] **Step 5: Write the failing test for `SecurityMaster.resolve` registry-only**

Add to `backend/tests/integration/test_security_master.py`:

```python
@pytest.mark.asyncio
async def test_resolve_with_registry_warm_hit_does_not_call_qualifier(
    session: AsyncSession,
) -> None:
    """resolve(spec) should NOT call IBQualifier when the registry has
    an active alias for the spec's canonical_id."""
    # Arrange — seed AAPL.NASDAQ in the registry
    aapl_uid = uuid4()
    session.add(InstrumentDefinition(
        instrument_uid=aapl_uid,
        raw_symbol="AAPL",
        provider="interactive_brokers",
        asset_class="equity",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        lifecycle_state="active",
        trading_hours={"timezone": "America/New_York", "rth": [], "eth": []},
    ))
    session.add(InstrumentAlias(
        id=uuid4(),
        instrument_uid=aapl_uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
        effective_to=None,
    ))
    await session.commit()

    # qualifier should NOT be called
    qualifier = AsyncMock()
    qualifier.qualify = AsyncMock(side_effect=AssertionError("qualifier should not be called"))

    sm = SecurityMaster(qualifier=qualifier, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")

    # Act
    instrument = await sm.resolve(spec)

    # Assert
    assert str(instrument.id) == "AAPL.NASDAQ"
    qualifier.qualify.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_cold_miss_qualifies_and_upserts_registry(
    session: AsyncSession,
) -> None:
    """resolve(spec) on registry miss → qualify via IB → upsert registry → return."""
    qualifier = AsyncMock()
    fake_aapl = TestInstrumentProvider.equity(symbol="AAPL", venue="NASDAQ")
    qualifier.qualify = AsyncMock(return_value=fake_aapl)
    qualifier._provider = MagicMock(contract_details={})

    sm = SecurityMaster(qualifier=qualifier, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")

    instrument = await sm.resolve(spec)
    await session.commit()

    qualifier.qualify.assert_called_once_with(spec)
    registry = InstrumentRegistry(session)
    found = await registry.find_by_alias(
        "AAPL.NASDAQ", provider="interactive_brokers", as_of_date=date.today()
    )
    assert found is not None


@pytest.mark.asyncio
async def test_resolve_cold_miss_without_qualifier_raises(session: AsyncSession) -> None:
    sm = SecurityMaster(qualifier=None, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
    with pytest.raises(ValueError, match="requires an IBQualifier"):
        await sm.resolve(spec)


@pytest.mark.asyncio
async def test_bulk_resolve_one_select_for_warm_batch(session: AsyncSession) -> None:
    # Seed two definitions
    for raw in ("AAPL", "MSFT"):
        uid = uuid4()
        session.add(InstrumentDefinition(
            instrument_uid=uid, raw_symbol=raw, provider="interactive_brokers",
            asset_class="equity", listing_venue="NASDAQ", routing_venue="SMART",
            lifecycle_state="active",
        ))
        session.add(InstrumentAlias(
            id=uuid4(), instrument_uid=uid, alias_string=f"{raw}.NASDAQ",
            venue_format="exchange_name", provider="interactive_brokers",
            effective_from=date(2026, 1, 1), effective_to=None,
        ))
    await session.commit()

    qualifier = AsyncMock()
    qualifier.qualify = AsyncMock(side_effect=AssertionError("not called for warm hits"))

    sm = SecurityMaster(qualifier=qualifier, db=session)
    specs = [
        InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
        InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),
    ]
    results = await sm.bulk_resolve(specs)
    assert [str(r.id) for r in results] == ["AAPL.NASDAQ", "MSFT.NASDAQ"]
    qualifier.qualify.assert_not_called()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_security_master.py -v -k "registry_warm_hit or cold_miss or bulk_resolve_one_select"`
Expected: FAIL — current `resolve()` reads from `instrument_cache`.

- [ ] **Step 7: Rewrite `SecurityMaster.resolve` and `bulk_resolve` + delete cache IO**

Edit `backend/src/msai/services/nautilus/security_master/service.py`:

**7a.** Delete `_read_cache`, `_read_cache_bulk`, `_write_cache`, `_instrument_from_cache_row` (lines ~1080–1255).

**7b.** Delete `from msai.models.instrument_cache import InstrumentCache` (top of file).

**7c.** Replace `resolve()`:

```python
async def resolve(self, spec: InstrumentSpec) -> Instrument:
    """Resolve a single spec via the registry.

    Warm path: registry has an active alias for the spec's canonical_id
    → reconstruct the Nautilus :class:`Instrument` from the spec
    (via :meth:`_build_instrument_from_spec`).

    Cold path: registry miss → qualify via the IB qualifier, upsert the
    definition + alias row (with extracted trading_hours), return the
    qualified instrument. ``listing_venue`` is derived from the IB
    contract details' ``primaryExchange``; ``routing_venue`` is the
    Nautilus-resolved venue (e.g. ``SMART`` for stocks). Mirrors the
    pattern that previously lived in ``resolve_for_live`` cold-miss
    (deleted in T4).

    Raises:
        ValueError: registry miss and ``self._qualifier`` is ``None``.
    """
    from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
    from msai.services.nautilus.security_master.registry import InstrumentRegistry

    canonical_id = spec.canonical_id()
    registry = InstrumentRegistry(self._db)
    # Use exchange-local (America/Chicago) date for IB alias windowing;
    # `date.today()` would disagree with the supervisor's spawn-time
    # `lookup_for_live(as_of_date=spawn_today)` and could resolve a
    # different futures contract on roll-day.
    today = exchange_local_today()

    idef = await registry.find_by_alias(
        canonical_id, provider="interactive_brokers", as_of_date=today,
    )
    if idef is not None:
        return self._build_instrument_from_spec(spec)

    if self._qualifier is None:
        raise ValueError(
            f"Registry miss for spec {spec!r} requires an IBQualifier — "
            "construct SecurityMaster with qualifier=... or pre-warm the "
            "registry via `msai instruments refresh`."
        )

    instrument = await self._qualifier.qualify(spec)
    trading_hours_json = self._trading_hours_for(canonical_id=canonical_id)

    # Derive listing_venue + routing_venue from the qualified instrument's
    # IB contract details. Routing venue = Nautilus-resolved venue
    # (e.g. SMART). Listing venue = IB primaryExchange when present
    # (e.g. NASDAQ), falling back to routing venue.
    routing_venue = instrument.id.venue.value
    listing_venue = routing_venue
    provider = self._qualifier._provider
    details = (
        provider.contract_details.get(instrument.id) if provider is not None else None
    )
    if details is not None and getattr(details, "contract", None) is not None:
        primary = getattr(details.contract, "primaryExchange", None) or None
        if primary:
            listing_venue = primary

    await self._upsert_definition_and_alias(
        raw_symbol=instrument.raw_symbol.value,
        listing_venue=listing_venue,
        routing_venue=routing_venue,
        asset_class=self._asset_class_for_instrument(instrument),
        alias_string=str(instrument.id),
        trading_hours=trading_hours_json,
    )
    return instrument
```

**7d.** Replace `bulk_resolve()`:

```python
async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]:
    """Bulk resolve via the registry — one SELECT for all warm hits,
    then per-spec qualification on the residual cold-misses.
    """
    if not specs:
        return []

    from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
    from msai.services.nautilus.security_master.registry import InstrumentRegistry

    registry = InstrumentRegistry(self._db)
    today = exchange_local_today()  # exchange-local for IB alias windowing
    canonical_ids = [spec.canonical_id() for spec in specs]
    warm_aliases = await registry.find_by_aliases_bulk(
        canonical_ids, provider="interactive_brokers", as_of_date=today,
    )

    results: list[Instrument] = []
    for spec, canonical_id in zip(specs, canonical_ids, strict=True):
        if canonical_id in warm_aliases:
            results.append(self._build_instrument_from_spec(spec))
            continue
        results.append(await self.resolve(spec))
    return results
```

**7e.** Add `_build_instrument_from_spec`:

```python
def _build_instrument_from_spec(self, spec: InstrumentSpec) -> Instrument:
    """Construct a Nautilus Instrument from the spec WITHOUT consulting
    a Postgres payload blob.

    Scoped to ``equity`` and ``forex`` for v1. Live preload at
    ``InteractiveBrokersInstrumentProviderConfig(load_contracts=...)``
    in ``live_node_config.py:509`` is the production hydration path for
    futures + options at runtime (Nautilus's IB provider builds the
    Instrument from the qualified contract). Callers that need a
    Nautilus :class:`Instrument` for a future/option/index without a
    live IB connection should use :func:`live_resolver.lookup_for_live`
    + the registry's ``ResolvedInstrument`` shape directly — that's the
    canonical primitive post-PR-#37.

    Raises :class:`NotImplementedError` for unsupported asset classes
    with an operator-action hint pointing at ``lookup_for_live``.

    ``spec.asset_class`` uses the LEGACY taxonomy (``equity``/``future``/
    ``forex``/``option``/``index``) per :class:`InstrumentSpec`. The
    registry-side asset_class translation happens later, at
    :meth:`_upsert_definition_and_alias` via
    :meth:`_asset_class_for_instrument`.
    """
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    if spec.asset_class == "equity":
        return TestInstrumentProvider.equity(symbol=spec.symbol, venue=spec.venue)
    if spec.asset_class == "forex":
        # Nautilus default_fx_ccy expects "BASE/QUOTE" form; spec.symbol is
        # the base, spec.currency is the quote.
        pair = f"{spec.symbol}/{spec.currency}"
        return TestInstrumentProvider.default_fx_ccy(symbol=pair, venue=spec.venue)
    raise NotImplementedError(
        f"_build_instrument_from_spec does not support asset_class="
        f"{spec.asset_class!r} in v1 (only equity + forex). For futures, "
        f"options, and indexes, use `live_resolver.lookup_for_live` "
        f"directly — it returns a ResolvedInstrument from the registry "
        f"that the live preload can hydrate via "
        f"InteractiveBrokersInstrumentProviderConfig(load_contracts=...). "
        f"Test fixtures that previously called `bulk_resolve` with a "
        f"future spec must migrate (see T11)."
    )
```

**7f.** Update `_upsert_definition_and_alias` (existing signature already takes `provider="interactive_brokers"` + `venue_format="exchange_name"` defaults — see service.py:850-861). Add a new kwarg `trading_hours: dict[str, Any] | None = None` and switch the **definition** upsert from `on_conflict_do_nothing` to `on_conflict_do_update` so existing rows pick up new trading_hours + refreshed_at:

```python
# In _upsert_definition_and_alias, where the definition upsert is built:
defs_stmt = (
    pg_insert(InstrumentDefinition)
    .values(
        instrument_uid=uuid.uuid4(),
        raw_symbol=raw_symbol,
        provider=provider,
        asset_class=asset_class,
        listing_venue=listing_venue,
        routing_venue=routing_venue,
        lifecycle_state="active",
        trading_hours=trading_hours,
        refreshed_at=datetime.now(UTC),
    )
)
defs_upsert = defs_stmt.on_conflict_do_update(
    index_elements=["raw_symbol", "provider", "asset_class"],
    set_={
        # Only update trading_hours if caller passed a non-NULL value;
        # ON CONFLICT DO UPDATE with COALESCE keeps existing rows intact
        # when caller has nothing to write.
        "trading_hours": sa.func.coalesce(
            defs_stmt.excluded.trading_hours, InstrumentDefinition.trading_hours
        ),
        "refreshed_at": defs_stmt.excluded.refreshed_at,
        "updated_at": sa.func.now(),
    },
)
await self._db.execute(defs_upsert)
```

The alias upsert keeps `on_conflict_do_nothing` on `uq_instrument_aliases_string_provider_from` — same-day re-upsert is intentionally a no-op.

**7g.** Delete the `refresh()` method (NotImplementedError-only per Simplifier's catch from research finding #2).

**7h.** Delete the entire "Cache IO" section + `_instrument_from_cache_row` helper.

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/integration/test_security_master.py tests/integration/test_instrument_registry.py -v`
Expected: new tests PASS. Pre-existing `instrument_cache`-coupled tests fail — those get migrated in T11.

Run: `cd backend && uv run ruff check src/msai/services/nautilus/security_master/`
Expected: clean.

- [ ] **Step 9: Commit (best-effort)**

```bash
git add backend/src/msai/services/nautilus/security_master/service.py \
        backend/src/msai/services/nautilus/security_master/registry.py \
        backend/tests/integration/test_security_master.py \
        backend/tests/integration/test_instrument_registry.py
git commit -m "refactor(security_master): registry-only resolve + bulk_resolve, delete cache IO"
```

---

## Task 4: `SecurityMaster.resolve_for_live` cold-miss removal + `_ROLL_SENSITIVE_ROOTS` deletion

**Goal:** `resolve_for_live` becomes registry-read-only — no `canonical_instrument_id()` fallback, no `_spec_from_canonical` cold-miss chain. Registry miss raises `live_resolver.RegistryMissError` (existing class, do NOT duplicate). `_ROLL_SENSITIVE_ROOTS` constant + the staleness-comparison block deleted as dead code.

**Iter-1 fix:** earlier draft introduced a duplicate `RegistryMissError` in `service.py`. There is already one at `live_resolver.py:159` (subclasses `LiveResolverError → ValueError`), and `live_supervisor/process_manager.py:293` dispatches on it for `FailureKind.REGISTRY_MISS`. Reuse it — do NOT re-introduce a parallel exception type.

**Iter-1 architectural note:** full deletion of `SecurityMaster.resolve_for_live` (per architectural concern about parallel resolution stacks) is deferred to T11, after T5 removes the CLI's call site and T11 migrates the parity test. T4 leaves `resolve_for_live` as a registry-only wrapper for the interim.

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py`
- Test: extend `backend/tests/integration/test_security_master_resolve_live.py` (full migration in T11)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_security_master_resolve_live.py`:

```python
@pytest.mark.asyncio
async def test_resolve_for_live_cold_miss_raises_registry_miss_error(
    session: AsyncSession,
) -> None:
    """resolve_for_live on registry miss must raise the existing
    live_resolver.RegistryMissError — no canonical_instrument_id()
    fallback, no spec-from-canonical construction, no IB cold-miss
    qualification.
    """
    from msai.services.nautilus.security_master.live_resolver import RegistryMissError
    from msai.services.nautilus.security_master.service import SecurityMaster

    # Arrange — empty registry; qualifier present but should not be called
    qualifier = AsyncMock()
    qualifier.qualify = AsyncMock(side_effect=AssertionError("qualifier should not be called"))
    sm = SecurityMaster(qualifier=qualifier, db=session)

    # Act + Assert
    with pytest.raises(RegistryMissError) as exc:
        await sm.resolve_for_live(["AAPL"])
    assert "AAPL" in str(exc.value)
    qualifier.qualify.assert_not_called()


def test_security_master_service_drops_canonical_instrument_id_and_roll_sensitive_roots() -> None:
    """Per Maintainer's binding objection on Q9, the cold-miss path is
    removed and `_ROLL_SENSITIVE_ROOTS` (its dead consumer) deleted.
    Source-text grep proves the deletions before structural-guard test
    in T12 enforces it across all of backend/src/msai/."""
    import importlib
    import pathlib

    mod = importlib.import_module("msai.services.nautilus.security_master.service")
    src = pathlib.Path(mod.__file__).read_text()
    assert "canonical_instrument_id" not in src, (
        "canonical_instrument_id still referenced in security_master.service. "
        "T4 must delete the cold-miss path + the import."
    )
    assert "_ROLL_SENSITIVE_ROOTS" not in src, (
        "_ROLL_SENSITIVE_ROOTS dead code not deleted in T4."
    )
    assert "_spec_from_canonical" not in src, (
        "_spec_from_canonical was only used by the deleted cold-miss path."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_security_master_resolve_live.py::test_resolve_for_live_cold_miss_raises_registry_miss_error -v`
Expected: FAIL (current code falls through to `canonical_instrument_id()` cold path, doesn't raise).

- [ ] **Step 3: Rewrite `resolve_for_live` registry-only**

Edit `backend/src/msai/services/nautilus/security_master/service.py`:

**3a.** Add an import at the top of the file (or inside the method to avoid circular import — the existing pattern uses local imports for `InstrumentRegistry`):

```python
# Inside resolve_for_live, alongside the InstrumentRegistry import:
from msai.services.nautilus.security_master.live_resolver import RegistryMissError
```

DO NOT define a new `RegistryMissError` class in `service.py` — reuse the existing one.

**3b.** Replace `resolve_for_live` (lines 294–417) with the registry-only implementation:

```python
async def resolve_for_live(self, symbols: list[str]) -> list[str]:
    """Return canonical Nautilus ``InstrumentId`` strings for ``symbols`` —
    registry-only.

    Warm path A — caller passed an already-qualified dotted alias:
    :meth:`InstrumentRegistry.find_by_alias` under
    ``provider="interactive_brokers"`` → return the input string.

    Warm path B — caller passed a bare ticker:
    :meth:`InstrumentRegistry.find_by_raw_symbol` under
    ``provider="interactive_brokers"`` → return the active alias string.

    Cold path: REMOVED per council verdict 2026-04-27. Registry miss
    raises :class:`live_resolver.RegistryMissError` (the canonical
    error type that ``live_supervisor.process_manager`` dispatches on
    for ``FailureKind.REGISTRY_MISS``).

    Non-hot-path; uses ``self._db`` only — no IB round-trips. Callers
    must pre-warm via ``msai instruments refresh`` BEFORE calling this
    method (gotchas #9, #11).

    Note: This function is **transitional** — full deletion happens in
    T11 once T5 removes the CLI call site and the parity test migrates
    to ``live_resolver.lookup_for_live`` directly.

    Raises:
        live_resolver.RegistryMissError: any symbol does not resolve
            through the registry today.
    """
    from msai.services.nautilus.security_master.live_resolver import RegistryMissError
    from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
    from msai.services.nautilus.security_master.registry import InstrumentRegistry

    registry = InstrumentRegistry(self._db)
    # Exchange-local (America/Chicago) date matches the supervisor's
    # spawn-time `lookup_for_live(as_of_date=spawn_today)` so warm-path
    # comparisons agree on the same futures contract on roll-day.
    today = exchange_local_today()
    out: list[str] = []
    for sym in symbols:
        # Warm path A — dotted alias.
        if "." in sym:
            idef = await registry.find_by_alias(
                sym, provider="interactive_brokers", as_of_date=today,
            )
            if idef is not None:
                out.append(sym)
                continue
            raise RegistryMissError(symbols=[sym], as_of_date=today)

        # Warm path B — bare ticker.
        idef = await registry.find_by_raw_symbol(sym, provider="interactive_brokers")
        if idef is not None:
            active_alias = next(
                (a for a in idef.aliases if a.effective_to is None), None,
            )
            if active_alias is not None:
                out.append(active_alias.alias_string)
                continue

        raise RegistryMissError(symbols=[sym], as_of_date=today)
    return out
```

(The constructor signature for `live_resolver.RegistryMissError` is `(symbols: list[str], as_of_date: date)` — see `live_resolver.py:648` for the canonical call shape.)

**3c.** Delete `_ROLL_SENSITIVE_ROOTS` (lines 130–135) entirely. The roll-sensitive logic moves to operator action: when a futures roll happens, the operator runs `msai instruments refresh --symbols ES --provider interactive_brokers --asset-class fut` and the registry's alias-window discipline (PR #32 + #37) handles the rest.

**3d.** Delete `_spec_from_canonical` (entire method, ~50 LOC) — was only used by the deleted cold-miss path.

**3e.** Delete the `from msai.services.nautilus.live_instrument_bootstrap import canonical_instrument_id, exchange_local_today` import that lived inside `resolve_for_live` (it's no longer used).

**3f.** Update the module docstring (top of `service.py`) — drop the "v2.1 cold-miss path delegates to canonical_instrument_id" reference and the "Phase-1 closed-universe" framing. New phrasing: registry-only resolve with operator-driven refresh.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/integration/test_security_master_resolve_live.py -v -k "cold_miss"`
Expected: PASS.

Run: `cd backend && uv run pytest tests/integration/test_security_master_resolve_live.py::test_security_master_service_drops_canonical_instrument_id_and_roll_sensitive_roots -v`
Expected: PASS.

Run: `cd backend && uv run ruff check src/msai/services/nautilus/security_master/service.py`
Expected: clean.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/src/msai/services/nautilus/security_master/service.py \
        backend/tests/integration/test_security_master_resolve_live.py
git commit -m "refactor(security_master): fail-loud cold-miss in resolve_for_live, delete _ROLL_SENSITIVE_ROOTS + _spec_from_canonical"
```

---

## Task 5: CLI `instruments refresh` — per-asset-class `IBContract` factories

**Goal:** Replace `cli.py:766–822` closed-universe `accepted` map (built via `canonical_instrument_id`) with per-asset-class `IBContract` factories. Add `--asset-class {stk,fut,cash}` flag (default `stk`). Closes Simplifier's circular-CLI catch.

**Iter-1 fixes:**

- Promote private `current_quarterly_expiry` → public `current_quarterly_expiry` in `live_instrument_bootstrap.py` (was Codex P2 — private import across module boundary).
- Add new `IBQualifier.qualify_contract(contract: IBContract) -> Instrument` method (was Codex P1 — method didn't exist; existing `qualify(spec)` builds contract internally).
- `_run_ib_resolve_for_live` signature changes from `list[str]` to `list[IBContract]`; internal loop uses `qualify_contract` directly.

**Files:**

- Modify: `backend/src/msai/services/nautilus/live_instrument_bootstrap.py` (rename leading-underscore `_current_quarterly_expiry` to public `current_quarterly_expiry`)
- Modify: `backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (add `qualify_contract`)
- Modify: `backend/src/msai/cli.py`
- Test: `backend/tests/unit/test_cli_instruments_refresh.py`
- Test: `backend/tests/unit/test_live_instrument_bootstrap.py` (rename test + import)
- Test: extend `backend/tests/integration/test_security_master.py` for `qualify_contract`

- [ ] **Step 0a: Promote `_current_quarterly_expiry` → `current_quarterly_expiry`**

Edit `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`:

1. Rename the function definition `def _current_quarterly_expiry(today: date) -> str:` to `def current_quarterly_expiry(today: date) -> str:` (drop leading underscore).
2. Update the call site inside `phase_1_paper_symbols` (line 263 in pre-edit file): `lastTradeDateOrContractMonth=current_quarterly_expiry(resolved_today),`.
3. Update the call site inside `_es_front_month_local_symbol` (line 140): `expiry = current_quarterly_expiry(today)`. (T7 deletes `_es_front_month_local_symbol` shortly; this rename keeps the file compiling between T5 and T7.)

Edit `backend/tests/unit/test_live_instrument_bootstrap.py`:

1. Update the import: `from msai.services.nautilus.live_instrument_bootstrap import current_quarterly_expiry, ...` (drop leading underscore).
2. Update every `_current_quarterly_expiry(...)` call (lines ~147, 154, 158, 164, 168, 172) to `current_quarterly_expiry(...)`.

Run: `cd backend && rg "_current_quarterly_expiry" backend/src backend/tests`
Expected: 0 matches (the rename is complete).

- [ ] **Step 0b: Add `IBQualifier.qualify_contract`**

Edit `backend/src/msai/services/nautilus/security_master/ib_qualifier.py`. Add a new method on `IBQualifier`:

```python
async def qualify_contract(self, contract: IBContract) -> Instrument:
    """Qualify a pre-built ``IBContract`` directly — for callers that
    already have the contract shape (e.g. CLI's per-asset-class
    factories) and don't need to go through :class:`InstrumentSpec`.

    Delegates to ``self._provider.get_instrument(contract)`` (same path
    :meth:`qualify` uses internally after spec→contract translation).
    Raises :class:`ValueError` on provider miss with the same message
    shape as :meth:`qualify`.
    """
    instrument = await self._provider.get_instrument(contract)
    if instrument is None:
        raise ValueError(
            f"Nautilus provider returned None for contract {contract!r} — "
            "check filter_sec_types or IB contract definition"
        )
    return instrument
```

Add a unit test in `backend/tests/integration/test_security_master.py` (or a new unit test alongside `qualify`):

```python
@pytest.mark.asyncio
async def test_ib_qualifier_qualify_contract_delegates_to_provider(monkeypatch) -> None:
    """qualify_contract calls provider.get_instrument(contract) directly."""
    from nautilus_trader.adapters.interactive_brokers.common import IBContract
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier

    contract = IBContract(secType="STK", symbol="AAPL", exchange="SMART",
                          primaryExchange="NASDAQ", currency="USD")
    fake_aapl = TestInstrumentProvider.equity(symbol="AAPL", venue="NASDAQ")

    provider = AsyncMock()
    provider.get_instrument = AsyncMock(return_value=fake_aapl)
    qualifier = IBQualifier(provider)

    result = await qualifier.qualify_contract(contract)
    assert str(result.id) == "AAPL.NASDAQ"
    provider.get_instrument.assert_called_once_with(contract)


@pytest.mark.asyncio
async def test_ib_qualifier_qualify_contract_raises_on_provider_miss() -> None:
    from nautilus_trader.adapters.interactive_brokers.common import IBContract
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier

    contract = IBContract(secType="STK", symbol="BOGUS", exchange="SMART", currency="USD")
    provider = AsyncMock()
    provider.get_instrument = AsyncMock(return_value=None)
    qualifier = IBQualifier(provider)

    with pytest.raises(ValueError, match="returned None for contract"):
        await qualifier.qualify_contract(contract)
```

Run: `cd backend && uv run pytest tests/integration/test_security_master.py -v -k "qualify_contract"`
Expected: 2 PASS.

- [ ] **Step 1: Write the failing test for the CLI factory**

Edit `backend/tests/unit/test_cli_instruments_refresh.py` — replace the test that calls `canonical_instrument_id("ES")` (line 189) with new factory-based test:

```python
def test_build_ib_contract_for_stk() -> None:
    """STK factory builds an SMART-routed equity contract."""
    from msai.cli import _build_ib_contract_for_symbol

    contract = _build_ib_contract_for_symbol("AAPL", asset_class="stk", today=date(2026, 4, 27))
    assert contract.secType == "STK"
    assert contract.symbol == "AAPL"
    assert contract.exchange == "SMART"
    assert contract.primaryExchange == "NASDAQ"
    assert contract.currency == "USD"


def test_build_ib_contract_for_fut() -> None:
    """FUT factory builds a CME futures contract with quarterly expiry."""
    from msai.cli import _build_ib_contract_for_symbol

    # June 2026 third-friday is 2026-06-19; on April 27 we should still be in June quarter
    contract = _build_ib_contract_for_symbol("ES", asset_class="fut", today=date(2026, 4, 27))
    assert contract.secType == "FUT"
    assert contract.symbol == "ES"
    assert contract.exchange == "CME"
    assert contract.lastTradeDateOrContractMonth == "202606"  # June quarterly
    assert contract.currency == "USD"


def test_build_ib_contract_for_cash() -> None:
    """CASH factory builds an IDEALPRO forex contract."""
    from msai.cli import _build_ib_contract_for_symbol

    contract = _build_ib_contract_for_symbol("EUR/USD", asset_class="cash", today=date(2026, 4, 27))
    assert contract.secType == "CASH"
    assert contract.symbol == "EUR"  # base
    assert contract.exchange == "IDEALPRO"
    assert contract.currency == "USD"  # quote


def test_build_ib_contract_unknown_asset_class_raises() -> None:
    """Unknown asset_class raises ValueError."""
    from msai.cli import _build_ib_contract_for_symbol

    with pytest.raises(ValueError, match="Unknown asset class"):
        _build_ib_contract_for_symbol("XYZ", asset_class="bogus", today=date(2026, 4, 27))


def test_cli_instruments_refresh_accepts_asset_class_flag(tmp_path) -> None:
    """The CLI exposes --asset-class and passes it through to the builder."""
    from typer.testing import CliRunner
    from msai.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["instruments", "refresh", "--help"])
    assert "--asset-class" in result.output, "--asset-class flag missing from help output"


def test_cli_instruments_refresh_builds_contracts_for_supported_fut_roots(monkeypatch) -> None:
    """A run with --asset-class fut --symbols ES,NQ builds two CME FUT contracts.

    v1 scopes FUT to the closed quarterly CME E-mini set (ES, NQ, RTY, YM)
    because ``current_quarterly_expiry`` is only correct for that cycle
    (see live_instrument_bootstrap.py:87-93). Other futures roots
    (e.g. CL/NYMEX, GC/COMEX, ZB/CBOT) require operator overrides
    that v1 does not surface — they raise from `_build_ib_contract_for_symbol`.
    """
    from typer.testing import CliRunner
    from msai.cli import app

    captured_contracts: list = []

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        captured_contracts.extend(contracts)
        return [{"symbol": "ES", "canonical": "ESM6.CME"}, {"symbol": "NQ", "canonical": "NQM6.CME"}]

    monkeypatch.setattr("msai.cli._run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["instruments", "refresh", "--provider", "interactive_brokers",
         "--symbols", "ES,NQ", "--asset-class", "fut"],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_contracts) == 2
    assert all(c.secType == "FUT" and c.exchange == "CME" for c in captured_contracts)


def test_cli_instruments_refresh_rejects_unsupported_fut_root() -> None:
    """v1 rejects non-CME-quarterly futures roots (CL, GC, ZB, etc.)."""
    from msai.cli import _build_ib_contract_for_symbol

    with pytest.raises(ValueError, match=r"v1 supports.*ES.*NQ.*RTY.*YM"):
        _build_ib_contract_for_symbol("CL", asset_class="fut", today=date(2026, 4, 27))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_cli_instruments_refresh.py -v -k "build_ib_contract or asset_class_flag"`
Expected: FAIL (`_build_ib_contract_for_symbol` doesn't exist; `--asset-class` flag doesn't exist).

- [ ] **Step 3: Add `_build_ib_contract_for_symbol` + the `--asset-class` flag**

Edit `backend/src/msai/cli.py`:

**3a.** Add the new helper above the `instruments refresh` command:

```python
def _build_ib_contract_for_symbol(
    symbol: str,
    *,
    asset_class: str,  # one of "stk" / "fut" / "cash"
    today: date,
    primary_exchange: str = "NASDAQ",  # STK only — caller's --primary-exchange flag
) -> IBContract:
    """Build an IBContract for an operator-supplied symbol + asset class.

    Per-asset-class normalization replaces the closed-universe
    canonical_instrument_id() helper. IB's qualifier resolves the
    canonical alias (``AAPL.NASDAQ``, ``ESM6.CME``, ``EUR/USD.IDEALPRO``)
    at qualification time — the CLI doesn't pre-canonicalize.

    Args:
        symbol: Operator-facing root symbol (e.g. ``"AAPL"``, ``"ES"``,
            ``"EUR/USD"``). FX takes ``"BASE/QUOTE"`` form; STK/FUT take
            the bare root.
        asset_class: One of ``"stk"`` (equity/ETF), ``"fut"`` (CME E-mini
            quarterly futures — closed set ``{ES, NQ, RTY, YM}``),
            ``"cash"`` (forex).
        today: Used by ``"fut"`` to derive quarterly expiry via
            :func:`current_quarterly_expiry`.

    Raises:
        ValueError: ``asset_class`` is not in the supported set, OR
            ``"fut"`` symbol is not in the v1 closed quarterly set.
    """
    from msai.services.nautilus.live_instrument_bootstrap import current_quarterly_expiry

    # v1 closed quarterly CME E-mini set — the only futures roots whose
    # third-Friday-of-March/June/September/December schedule
    # `current_quarterly_expiry` correctly handles (see
    # live_instrument_bootstrap.py:87-93). CL/GC/ZB/etc. follow
    # different expiry cycles + venues; they need operator-specified
    # exchange + expiry that the v1 CLI does not yet surface.
    _FUT_QUARTERLY_ROOTS: frozenset[str] = frozenset({"ES", "NQ", "RTY", "YM"})

    if asset_class == "stk":
        return IBContract(
            secType="STK",
            symbol=symbol,
            exchange="SMART",
            # primaryExchange disambiguates SMART. Operator passes via
            # `--primary-exchange` flag (default NASDAQ); SPY/VTI use ARCA,
            # NYSE-listed stocks use NYSE, etc.
            primaryExchange=primary_exchange,
            currency="USD",
        )
    if asset_class == "fut":
        if symbol not in _FUT_QUARTERLY_ROOTS:
            raise ValueError(
                f"--asset-class fut: v1 supports the closed CME E-mini "
                f"quarterly set {sorted(_FUT_QUARTERLY_ROOTS)!r}; got "
                f"{symbol!r}. Other futures (CL/NYMEX, GC/COMEX, etc.) "
                f"need exchange + expiry overrides that v1 does not "
                f"surface — schedule a follow-up CLI flag."
            )
        return IBContract(
            secType="FUT",
            symbol=symbol,
            exchange="CME",
            lastTradeDateOrContractMonth=current_quarterly_expiry(today),
            currency="USD",
        )
    if asset_class == "cash":
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
        else:
            base, quote = symbol, "USD"
        return IBContract(
            secType="CASH",
            symbol=base,
            exchange="IDEALPRO",
            currency=quote,
        )
    raise ValueError(
        f"Unknown asset class {asset_class!r} — supported: stk, fut, cash."
    )
```

**3b.** Add the `--asset-class` flag to the `instruments refresh` command's signature:

```python
@instruments_app.command("refresh")
def instruments_refresh(
    symbols: str = typer.Option(..., "--symbols"),
    provider: str = typer.Option("databento", "--provider"),
    asset_class: str = typer.Option(
        "stk",
        "--asset-class",
        help=(
            "Asset class for IB qualification: stk (equity/ETF, default), "
            "fut (CME E-mini quarterly: ES/NQ/RTY/YM), cash (forex). "
            "Ignored when --provider databento."
        ),
    ),
    primary_exchange: str = typer.Option(
        "NASDAQ",
        "--primary-exchange",
        help=(
            "STK primaryExchange for SMART routing disambiguation. "
            "NASDAQ-listed (AAPL/MSFT) is the default; ARCA-listed "
            "ETFs (SPY/VTI) need `--primary-exchange ARCA`; NYSE-listed "
            "stocks need `--primary-exchange NYSE`. Ignored for FUT/CASH."
        ),
    ),
    # ... existing flags
) -> None:
```

**3c.** Replace the closed-universe `accepted` block (lines 766–822) with the new flow:

```python
    if provider == "interactive_brokers":
        # Per-asset-class IBContract factories — IB resolves canonical
        # aliases at qualification time. No closed-universe map.
        if asset_class not in {"stk", "fut", "cash"}:
            _fail(
                f"--asset-class {asset_class!r} is not supported. "
                f"Use one of: stk, fut, cash."
            )

        from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today

        today = exchange_local_today()
        contracts: list[IBContract] = []
        for sym in symbol_list:
            try:
                contracts.append(_build_ib_contract_for_symbol(
                    sym,
                    asset_class=asset_class,
                    today=today,
                    primary_exchange=primary_exchange,
                ))
            except ValueError as exc:
                _fail(str(exc))

        # Port/account mode consistency (gotcha #6 guard).
        from msai.services.nautilus.ib_port_validator import validate_port_account_consistency
        try:
            validate_port_account_consistency(settings.ib_port, settings.ib_account_id)
        except ValueError as exc:
            _fail(str(exc))

        typer.echo(
            f"Pre-warming IB registry: host={settings.ib_host} "
            f"port={settings.ib_port} "
            f"account={settings.ib_account_id.strip()} "
            f"asset_class={asset_class} "
            f"client_id={settings.ib_instrument_client_id} "
            f"connect_timeout={settings.ib_connect_timeout_seconds}s "
            f"request_timeout={settings.ib_request_timeout_seconds}s",
            err=True,
        )

        try:
            resolved = asyncio.run(_run_ib_resolve_for_live(contracts))
        except _IBGatewayUnreachableError as exc:
            _fail(str(exc))
        _emit_json({"provider": provider, "asset_class": asset_class, "resolved": resolved})
        return
```

**3d.** Update `_run_ib_resolve_for_live` signature to accept `list[IBContract]` (was `list[str]`). The internal loop that previously called `canonical_instrument_id` no longer does so — instead, it hands each `IBContract` directly to `IBQualifier.qualify_contract(contract)` and writes the resolved canonical to the registry.

If `_run_ib_resolve_for_live` is in `cli.py`, edit it directly. If it's in another module, update both the CLI call site and the function's body.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_cli_instruments_refresh.py -v`
Expected: all PASS (the new tests + any pre-existing tests that don't depend on `canonical_instrument_id`).

Run: `cd backend && uv run ruff check src/msai/cli.py`
Expected: clean.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/src/msai/cli.py backend/tests/unit/test_cli_instruments_refresh.py
git commit -m "refactor(cli): instruments refresh uses per-asset-class IBContract factories"
```

---

## Task 6: Delete `canonical_instrument_id` from `services/nautilus/instruments.py`

**Goal:** Remove the closed-universe Phase-1 helper from `services/nautilus/instruments.py`. Inline its body into the only remaining caller (`default_bar_type`).

**Files:**

- Modify: `backend/src/msai/services/nautilus/instruments.py`
- Modify: `backend/tests/unit/test_instruments.py`

- [ ] **Step 1: Write the failing test**

Edit `backend/tests/unit/test_instruments.py` — delete tests at line 42, 45 (they assert on `canonical_instrument_id`). Keep tests for `resolve_instrument` and `default_bar_type`. Adjust the `default_bar_type` test to assert on the inlined behavior:

```python
def test_default_bar_type_inlines_resolve_instrument() -> None:
    """default_bar_type returns canonical-id-shaped string without
    calling the deleted canonical_instrument_id helper."""
    from msai.services.nautilus.instruments import default_bar_type

    assert default_bar_type("AAPL") == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    assert default_bar_type("VOD", venue="LSE") == "VOD.LSE-1-MINUTE-LAST-EXTERNAL"


def test_canonical_instrument_id_is_not_importable() -> None:
    """Per council verdict + structural guard: canonical_instrument_id
    is not exported from this module any more."""
    import msai.services.nautilus.instruments as mod

    assert not hasattr(mod, "canonical_instrument_id"), (
        "canonical_instrument_id must be deleted from instruments.py"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_instruments.py::test_canonical_instrument_id_is_not_importable -v`
Expected: FAIL — current module still exports `canonical_instrument_id`.

- [ ] **Step 3: Delete `canonical_instrument_id` + inline its body**

Edit `backend/src/msai/services/nautilus/instruments.py`:

Replace the entire file with:

```python
"""Resolve ticker symbols to NautilusTrader ``Instrument`` objects.

Synchronous wrapper around ``TestInstrumentProvider`` for catalog-builder
+ backtest-worker call sites that don't need the full async
:class:`SecurityMaster` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.test_kit.providers import TestInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


DEFAULT_EQUITY_VENUE = "NASDAQ"
"""Default venue for a bare ticker. Callers resolving instruments on
other venues pass ``venue=...`` explicitly."""


def resolve_instrument(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> Instrument:
    """Turn a raw ticker symbol (or canonical Nautilus ID) into an
    ``Instrument`` pinned to a real IB venue.

    Accepts either a bare symbol like ``"AAPL"`` or a fully-qualified
    Nautilus identifier like ``"AAPL.NASDAQ"``. A dotted identifier's
    suffix wins over ``venue``.
    """
    if "." in symbol_or_id:
        raw_symbol, parsed_venue = symbol_or_id.split(".", 1)
        resolved_venue = parsed_venue
    else:
        raw_symbol = symbol_or_id
        resolved_venue = venue
    return TestInstrumentProvider.equity(symbol=raw_symbol, venue=resolved_venue)


def default_bar_type(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> str:
    """Return the default 1-minute last-external bar type for a symbol.

    MSAI ingests minute bars; the bar type is hard-wired to
    ``1-MINUTE-LAST-EXTERNAL``.
    """
    return f"{resolve_instrument(symbol_or_id, venue=venue).id}-1-MINUTE-LAST-EXTERNAL"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_instruments.py -v`
Expected: PASS.

Run: `cd backend && uv run ruff check src/msai/services/nautilus/instruments.py`
Expected: clean.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/src/msai/services/nautilus/instruments.py \
        backend/tests/unit/test_instruments.py
git commit -m "refactor: delete canonical_instrument_id from services/nautilus/instruments.py"
```

---

## Task 7: Delete `canonical_instrument_id` from `live_instrument_bootstrap.py`

**Goal:** Remove the second `canonical_instrument_id` definition + its only private helper `_es_front_month_local_symbol`. Keep `current_quarterly_expiry`, `_FUT_MONTH_CODES`, `phase_1_paper_symbols`, `_STATIC_SYMBOLS`, `build_ib_instrument_provider_config` — those are still used by the closed-universe live-supervisor smoke path (separate cleanup if/when that path retires).

**Files:**

- Modify: `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`
- Modify: `backend/tests/unit/test_live_instrument_bootstrap.py`

- [ ] **Step 1: Write the failing test**

Edit `backend/tests/unit/test_live_instrument_bootstrap.py` — delete the tests at lines 209–228 that assert on `canonical_instrument_id("ES.CME", today=...)`. Replace with:

```python
def test_canonical_instrument_id_is_not_importable() -> None:
    """Per council verdict 2026-04-27: canonical_instrument_id is deleted
    from live_instrument_bootstrap.py. CLI seeding now uses per-asset-class
    IBContract factories at cli.py:_build_ib_contract_for_symbol."""
    import msai.services.nautilus.live_instrument_bootstrap as mod

    assert not hasattr(mod, "canonical_instrument_id"), (
        "canonical_instrument_id must be deleted — CLI uses IBContract "
        "factories now (see cli.py:_build_ib_contract_for_symbol)."
    )


def test_es_front_month_local_symbol_is_deleted() -> None:
    """_es_front_month_local_symbol was only called by canonical_instrument_id.
    Both deleted."""
    import msai.services.nautilus.live_instrument_bootstrap as mod

    assert not hasattr(mod, "_es_front_month_local_symbol")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_live_instrument_bootstrap.py::test_canonical_instrument_id_is_not_importable -v`
Expected: FAIL.

- [ ] **Step 3: Delete the function + helper**

Edit `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`:

**3a.** Delete the `_es_front_month_local_symbol` function (lines 132–143).

**3b.** Delete the `canonical_instrument_id` function (lines 146–194).

**3c.** Update the module docstring to drop the `canonical_instrument_id and phase_1_paper_symbols` reference (line 67).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_live_instrument_bootstrap.py -v`
Expected: PASS.

Run: `cd backend && rg -n "canonical_instrument_id" backend/src`
Expected: 0 matches (all imports + definitions are now gone).

Run: `cd backend && uv run ruff check src/msai/services/nautilus/live_instrument_bootstrap.py`
Expected: clean.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/src/msai/services/nautilus/live_instrument_bootstrap.py \
        backend/tests/unit/test_live_instrument_bootstrap.py
git commit -m "refactor: delete canonical_instrument_id from live_instrument_bootstrap.py"
```

---

## Task 8: Preflight script `scripts/preflight_cache_migration.py`

**Goal:** Operator-facing script that validates every active `LiveDeployment`'s instrument set (drawn from its frozen portfolio revision strategies) resolves through the registry today via `live_resolver.lookup_for_live`; aborts with operator-action hint on any miss. Required precondition before `alembic upgrade head` per US-002.

**Iter-1 fix:** earlier draft assumed `LiveDeployment.canonical_instruments` was a column. It is not — the source-of-truth instrument list lives on `LivePortfolioRevisionStrategy.instruments: list[str]` (see `models/live_portfolio_revision_strategy.py:59`), traversed via `LiveDeployment.portfolio_revision_id → LivePortfolioRevision → LivePortfolioRevisionStrategy.revision_id`. The supervisor itself does this lookup at spawn time (`live_supervisor/__main__.py:289-307`) before calling `lookup_for_live(member.instruments, ...)`. Active statuses are `starting` and `running` (no `paused` per current state machine). Field name is `deployment_slug`, not `slug`.

**Files:**

- Create: `backend/scripts/preflight_cache_migration.py`
- Test: extend `backend/tests/integration/test_instrument_cache_migration.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_instrument_cache_migration.py`:

```python
@pytest.mark.asyncio
async def test_preflight_passes_when_registry_covers_active_deployments(
    isolated_postgres_url: str, session_factory,
) -> None:
    """Preflight exits 0 when every active deployment's strategy
    instruments resolve through the registry today."""
    import os
    import subprocess

    # Arrange — seed registry with AAPL.NASDAQ; create a portfolio
    # revision with one strategy whose instruments=['AAPL']; create
    # a LiveDeployment in 'running' state pointing at that revision.
    async with session_factory() as session:
        from msai.models.instrument_definition import InstrumentDefinition
        from msai.models.instrument_alias import InstrumentAlias
        from tests.integration._deployment_factory import make_live_deployment

        aapl_uid = uuid4()
        session.add(InstrumentDefinition(
            instrument_uid=aapl_uid, raw_symbol="AAPL",
            provider="interactive_brokers", asset_class="equity",
            listing_venue="NASDAQ", routing_venue="SMART",
            lifecycle_state="active",
        ))
        session.add(InstrumentAlias(
            id=uuid4(), instrument_uid=aapl_uid, alias_string="AAPL.NASDAQ",
            venue_format="exchange_name", provider="interactive_brokers",
            effective_from=date(2026, 1, 1), effective_to=None,
        ))
        await session.commit()

        # make_live_deployment creates the User + Strategy + Portfolio +
        # Revision + RevisionStrategy + LiveDeployment chain. Pass
        # instruments=["AAPL"] so the revision-strategy member references
        # the symbol we just seeded into the registry.
        await make_live_deployment(
            session, status="running", member_instruments=["AAPL"],
        )
        await session.commit()

    # Act
    result = subprocess.run(
        ["uv", "run", "python", "scripts/preflight_cache_migration.py"],
        cwd="backend",
        capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": isolated_postgres_url},
    )

    # Assert
    assert result.returncode == 0, (
        f"preflight should pass; stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "preflight passed" in result.stdout.lower(), result.stdout


@pytest.mark.asyncio
async def test_preflight_fails_with_operator_hint_on_missing_alias(
    isolated_postgres_url: str, session_factory,
) -> None:
    """Preflight exits non-zero with `msai instruments refresh` hint
    when an active deployment's strategy instruments has a miss."""
    import os
    import subprocess

    # Arrange — registry has NO alias for ES; deployment references it.
    async with session_factory() as session:
        from tests.integration._deployment_factory import make_live_deployment
        await make_live_deployment(
            session, status="running", member_instruments=["ES"],
        )
        await session.commit()

    # Act
    result = subprocess.run(
        ["uv", "run", "python", "scripts/preflight_cache_migration.py"],
        cwd="backend",
        capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": isolated_postgres_url},
    )

    # Assert
    assert result.returncode != 0, f"preflight should fail; stdout:\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "ES" in combined
    assert "msai instruments refresh" in combined
```

**Factory extension required (REVISED iter-2):** `make_live_deployment` (at `tests/integration/_deployment_factory.py:45`) currently does NOT accept `member_instruments` and does NOT create `LivePortfolioRevisionStrategy` rows. T8 step 0 (below) extends the factory in this PR. Without that extension, the preflight test cannot seed a realistic state.

- [ ] **Step 0: Extend `make_live_deployment` to create LivePortfolioRevisionStrategy rows + auto-default user/strategy**

Edit `backend/tests/integration/_deployment_factory.py`:

1. Add a new `member_instruments: list[str] | None = None` kwarg to `make_live_deployment` (default `["AAPL"]` if None).
2. **Make user/strategy auto-default when neither `(user, strategy)` nor `(user_id, strategy_id)` are passed.** Today the factory raises if neither tuple is provided (line 82). Add an "auto-create defaults" branch: if both tuples are missing, create a fresh `User(email=f"{uuid4()}@test.com", ...)` and a fresh `Strategy(...)` row, `session.add(...)` then `await session.flush()` to populate FK ids, then use those. Do NOT `commit` — per the existing helper contract (`_deployment_factory.py:67-68`): "does NOT commit; caller owns the transaction boundary". The auto-default rows live in the same transaction the caller drives.
3. After creating the `LivePortfolio` + `LivePortfolioRevision`, create a `LivePortfolioRevisionStrategy` row with:
   - `revision_id=revision.id`
   - `strategy_id=<the strategy used>`
   - `instruments=member_instruments` (defaulted to `["AAPL"]`)
   - `weight=Decimal("1.0")`
   - `order_index=0`
   - `config={}` (or some sensible default)
4. Verify the existing callers (8+ test files per recon) still work — they pass `(user, strategy)` explicitly, so the auto-default branch is non-breaking. They also don't pass `member_instruments`, so the default kicks in.

The auto-default branch is what lets the new T8 preflight tests call `make_live_deployment(session, status="running", member_instruments=["AAPL"])` without spelling out a User + Strategy fixture. Existing callers' explicit user/strategy continue to work.

Run: `cd backend && uv run pytest tests/integration -v -k "deployment_factory or heartbeat_monitor or order_attempt_audit"`
Expected: PASS — additive change.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py -v -k "preflight"`
Expected: FAIL — `scripts/preflight_cache_migration.py` doesn't exist.

- [ ] **Step 3: Write the preflight script**

Create `backend/scripts/preflight_cache_migration.py`:

```python
#!/usr/bin/env python
"""Preflight gate for the instrument-cache → registry migration.

Operator step BEFORE `alembic upgrade head`. Validates that every active
LiveDeployment's strategy-member instrument list resolves through the
registry today. Source-of-truth instrument list lives on
``LivePortfolioRevisionStrategy.instruments`` (NOT on LiveDeployment
itself — there's no canonical_instruments column on the deployment row).
The supervisor does the same lookup at spawn time via
``live_resolver.lookup_for_live(member.instruments, ...)``.

Exits 0 on success, non-zero on miss with operator-action hint.

Per US-002 (council-ratified): a miss is an active-deployment breakage
waiting to happen on the next supervisor restart, NOT harmless legacy
dirt. Operator must run
``msai instruments refresh --symbols X --provider interactive_brokers
--asset-class <stk|fut|cash>`` to seed the missing alias, then re-run
preflight.

Usage:
    cd backend && uv run python scripts/preflight_cache_migration.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.config import settings
from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import (
    LivePortfolioRevisionStrategy,
)

# Active deployments per the current state machine. The supervisor's
# spawn-time check uses {starting, running} (see live_supervisor/
# __main__.py around line 240). `paused` is NOT in the live-state
# vocabulary today.
ACTIVE_STATUSES = ("starting", "running")

log = logging.getLogger("preflight_cache_migration")


async def _check_active_deployments() -> int:
    """Return exit code: 0 if all active deployments resolve, 1 otherwise."""
    # Local import — `live_resolver` pulls in heavy dependencies (Nautilus
    # adapter modules) that should not load if `--help` is queried.
    from msai.services.nautilus.security_master.live_resolver import (
        LiveResolverError,
        lookup_for_live,
    )

    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        # Step 1 — count legacy instrument_cache rows (informational).
        try:
            cache_count = (
                await session.execute(text("SELECT count(*) FROM instrument_cache"))
            ).scalar_one()
            print(f"[info] instrument_cache row count: {cache_count}")
        except Exception as exc:  # noqa: BLE001
            # Table may already be dropped (post-migration re-run); not an error.
            print(f"[info] instrument_cache table missing or unreadable: {exc}")

        # Step 2 — JOIN active deployments → portfolio revision → strategy
        # members. Each deployment can have multiple member rows
        # (multi-strategy portfolio); each member has its own
        # `instruments` list.
        stmt = (
            select(
                LiveDeployment.deployment_slug,
                LivePortfolioRevisionStrategy.instruments,
            )
            .join(
                LivePortfolioRevision,
                LiveDeployment.portfolio_revision_id == LivePortfolioRevision.id,
            )
            .join(
                LivePortfolioRevisionStrategy,
                LivePortfolioRevisionStrategy.revision_id == LivePortfolioRevision.id,
            )
            .where(LiveDeployment.status.in_(ACTIVE_STATUSES))
        )
        rows = (await session.execute(stmt)).all()
        print(
            f"[info] active deployment members (status in {ACTIVE_STATUSES}): "
            f"{len(rows)}"
        )

        # Per-deployment empty-member check: enumerate active deployments
        # INDEPENDENTLY and fail loud on ANY deployment with zero member
        # rows — even if other deployments are healthy (mixed state). The
        # supervisor treats per-deployment empty-members as fatal at spawn
        # time (live_supervisor/__main__.py:216-220), so pre-cutover we
        # should surface it.
        all_active_slugs = (
            await session.execute(
                select(LiveDeployment.deployment_slug).where(
                    LiveDeployment.status.in_(ACTIVE_STATUSES)
                )
            )
        ).scalars().all()
        slugs_with_members = {slug for slug, _ in rows}
        empty_slugs = [s for s in all_active_slugs if s not in slugs_with_members]
        if empty_slugs:
            print()
            print(
                f"[FAIL] {len(empty_slugs)} active deployment(s) have ZERO "
                f"`live_portfolio_revision_strategies` rows. This is a "
                f"corrupt state — the supervisor would crash on next spawn. "
                f"Investigate before migrating."
            )
            for slug in empty_slugs:
                print(f"  - deployment {slug!r}")
            await engine.dispose()
            return 1

        if not rows:
            print(
                "[ok] No active deployments — no strategy instruments to validate. "
                "Preflight passed."
            )
            await engine.dispose()
            return 0

        from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
        # Exchange-local (America/Chicago) date matches what the supervisor
        # passes to `lookup_for_live(as_of_date=spawn_today)` — same alias
        # window evaluation ensures preflight agrees with runtime.
        today = exchange_local_today()
        misses: list[tuple[str, str, str]] = []  # (deployment_slug, sym, error_kind)

        empty_instrument_members: list[str] = []  # deployment_slugs with member.instruments=[]

        for deployment_slug, instruments in rows:
            # Per supervisor (live_supervisor/__main__.py:295): an empty
            # member.instruments is fatal at spawn — surface pre-cutover.
            # Also defends against `lookup_for_live` raising plain
            # ValueError("symbols cannot be empty") on empty input
            # (live_resolver.py:490) which would otherwise crash this
            # preflight script instead of producing operator-readable output.
            if not instruments:
                empty_instrument_members.append(deployment_slug)
                continue

            try:
                await lookup_for_live(
                    list(instruments), as_of_date=today, session=session,
                )
            except LiveResolverError as exc:
                # Best-effort attribution: pin every instrument in the member
                # set to the same error so the operator sees the full set.
                kind = type(exc).__name__
                for sym in instruments:
                    misses.append((deployment_slug, sym, kind))
            except ValueError as exc:
                # Defensive: lookup_for_live's "symbols cannot be empty" guard
                # is the only known plain-ValueError path; any other ValueError
                # is unexpected so surface it loudly instead of swallowing.
                misses.append((deployment_slug, "<bare-ValueError>", str(exc)))

        if empty_instrument_members:
            print()
            print(
                f"[FAIL] {len(empty_instrument_members)} active "
                f"`live_portfolio_revision_strategies` member row(s) have an "
                f"EMPTY `instruments` list. The supervisor rejects this as a "
                f"fatal portfolio-revision freeze bug. Investigate before "
                f"migrating."
            )
            for slug in empty_instrument_members:
                print(f"  - deployment {slug!r}")
            await engine.dispose()
            return 1

        await engine.dispose()

        if not misses:
            print(
                f"[ok] All {len(rows)} active deployment-member rows' instruments "
                f"resolve through the registry. Preflight passed."
            )
            return 0

        # Fail-loud with operator-action hint
        print()
        print("[FAIL] Preflight failed — registry misses on active deployments:")
        seen: set[tuple[str, str]] = set()
        for slug, sym, kind in misses:
            key = (slug, sym)
            if key in seen:
                continue
            seen.add(key)
            root = sym.split(".", 1)[0] if "." in sym else sym
            print(f"  - deployment {slug!r}: {sym!r} ({kind})")
            print(
                f"    Run: msai instruments refresh --symbols {root} "
                f"--provider interactive_brokers --asset-class <stk|fut|cash>"
            )
        print()
        print("After seeding the missing aliases, re-run this preflight.")
        print("Do NOT run `alembic upgrade head` until preflight exits 0.")
        return 1


def main() -> None:
    sys.exit(asyncio.run(_check_active_deployments()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py -v -k "preflight"`
Expected: 2 PASS.

Run: `cd backend && chmod +x scripts/preflight_cache_migration.py && uv run python scripts/preflight_cache_migration.py`
Expected: prints `[ok] No active deployments — no strategy instruments to validate.` and exits 0 (assuming a fresh dev DB).

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/scripts/preflight_cache_migration.py backend/tests/integration/test_instrument_cache_migration.py
git commit -m "feat(scripts): preflight gate for instrument-cache migration"
```

---

## Task 9: Alembic Revision B — data migration + DROP `instrument_cache`

**Goal:** The cutover migration. Copies every `instrument_cache` row into `instrument_definitions` + `instrument_aliases` (with `trading_hours` carried forward to the column added in T1), then `DROP TABLE instrument_cache`. Fail-loud on any malformed row (per Q6=fail-loud).

**Files:**

- Create: `backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py`
- Test: extend `backend/tests/integration/test_instrument_cache_migration.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/integration/test_instrument_cache_migration.py`:

```python
REV_B = "e2f3g4h5i6j7"


@pytest.mark.asyncio
async def test_revision_b_migrates_cache_rows_to_registry_then_drops_table(
    isolated_postgres_url: str,
) -> None:
    """Revision B: copy instrument_cache → registry, then DROP instrument_cache."""
    # Arrange — migrate to revision A, seed instrument_cache with one row
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO instrument_cache
              (canonical_id, asset_class, venue, ib_contract_json,
               nautilus_instrument_json, trading_hours, last_refreshed_at,
               created_at, updated_at)
            VALUES (
              'AAPL.NASDAQ', 'equity', 'NASDAQ',
              '{"secType":"STK","symbol":"AAPL","exchange":"SMART"}',
              '{"type":"Equity","id":"AAPL.NASDAQ"}',
              '{"timezone":"America/New_York","rth":[],"eth":[]}',
              now(), now(), now()
            )
        """))

    # Act — apply revision B
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Assert — cache table dropped + registry has the row
    async with engine.connect() as conn:
        # Table is gone
        tables = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name='instrument_cache'"
        ))
        assert tables.scalar_one_or_none() is None, "instrument_cache should be dropped"

        # instrument_definitions has the migrated row
        defs = await conn.execute(text("""
            SELECT raw_symbol, asset_class, listing_venue, routing_venue, trading_hours
            FROM instrument_definitions
            WHERE raw_symbol='AAPL'
        """))
        row = defs.one_or_none()
        assert row is not None
        assert row[0] == "AAPL"
        assert row[1] == "equity"
        assert row[2] == "NASDAQ"
        assert row[4] == {"timezone": "America/New_York", "rth": [], "eth": []}

        # instrument_aliases has the matching alias
        aliases = await conn.execute(text("""
            SELECT alias_string, provider FROM instrument_aliases
            WHERE alias_string='AAPL.NASDAQ'
        """))
        alias_row = aliases.one_or_none()
        assert alias_row is not None
        assert alias_row[1] == "interactive_brokers"

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_b_idempotent_when_rerun_against_seeded_registry(
    isolated_postgres_url: str,
) -> None:
    """Revision B uses ON CONFLICT DO NOTHING — running it against a
    registry that already has the row is a no-op."""
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    engine = create_async_engine(isolated_postgres_url, future=True)
    aapl_uid = uuid4()
    async with engine.begin() as conn:
        # Pre-seed registry with AAPL
        await conn.execute(text("""
            INSERT INTO instrument_definitions
              (instrument_uid, raw_symbol, provider, asset_class,
               listing_venue, routing_venue, lifecycle_state, created_at, updated_at)
            VALUES (:uid, 'AAPL', 'interactive_brokers', 'equity',
                    'NASDAQ', 'SMART', 'active', now(), now())
        """), {"uid": str(aapl_uid)})
        await conn.execute(text("""
            INSERT INTO instrument_aliases
              (id, instrument_uid, alias_string, venue_format, provider,
               effective_from, created_at)
            VALUES (:aid, :uid, 'AAPL.NASDAQ', 'exchange_name',
                    'interactive_brokers', '2026-01-01', now())
        """), {"aid": str(uuid4()), "uid": str(aapl_uid)})
        # Seed instrument_cache with the same canonical
        await conn.execute(text("""
            INSERT INTO instrument_cache
              (canonical_id, asset_class, venue, ib_contract_json,
               nautilus_instrument_json, last_refreshed_at, created_at, updated_at)
            VALUES ('AAPL.NASDAQ', 'equity', 'NASDAQ', '{}', '{}',
                    now(), now(), now())
        """))

    # Act — rev B should NOT raise on the duplicate
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Assert — registry still has exactly one AAPL definition
    async with engine.connect() as conn:
        count = await conn.execute(text(
            "SELECT count(*) FROM instrument_definitions WHERE raw_symbol='AAPL'"
        ))
        assert count.scalar_one() == 1
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py::test_revision_b_migrates_cache_rows_to_registry_then_drops_table -v`
Expected: FAIL — revision B doesn't exist.

- [ ] **Step 3: Write the migration**

Create `backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py`:

```python
"""drop instrument_cache after migrating rows to registry

Revision B of the instrument-cache → registry migration.

Steps:
    1. Reflect ``instrument_cache``, ``instrument_definitions``, and
       ``instrument_aliases`` via op.get_bind() (do NOT import the
       models — brittle pattern per PR #44 plan-review iter-3).
    2. Iterate every ``instrument_cache`` row, parse ``canonical_id``
       into ``raw_symbol`` + ``venue``, upsert into
       ``instrument_definitions`` (ON CONFLICT DO NOTHING on
       ``(raw_symbol, provider, asset_class)``), then upsert into
       ``instrument_aliases`` (ON CONFLICT DO NOTHING on
       ``(alias_string, provider, effective_from)``).
    3. Carry forward ``trading_hours`` JSONB to
       ``instrument_definitions.trading_hours`` (added by Revision A).
    4. ``DROP TABLE instrument_cache``.

Drops:
    - ``ib_contract_json`` — IB authority, re-qualify on demand.
    - ``nautilus_instrument_json`` — Nautilus Cache(database=redis)
      is the runtime persistence layer.

Fail-loud on any row whose ``canonical_id`` does not parse cleanly
(no '.' separator, etc.) — operator inspects + fixes via psql, then
re-runs the migration.

Reversibility:
    Downgrade is **schema-only**: recreates an empty
    ``instrument_cache`` table. Data is NOT restored — the operator
    MUST have a ``pg_dump`` checkpoint taken before ``alembic upgrade
    head``. The migration's docstring + the runbook
    (docs/runbooks/instrument-cache-migration.md) document this
    loudly.

Revision: e2f3g4h5i6j7
Revises: d1e2f3g4h5i6
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2f3g4h5i6j7"
down_revision = "d1e2f3g4h5i6"
branch_labels = None
depends_on = None


# Legacy `instrument_cache.asset_class` taxonomy → registry taxonomy.
# Cache uses (equity|future|forex|option|index); registry CHECK constraint
# `ck_instrument_definitions_asset_class` allows (equity|futures|fx|option|
# crypto). `index` has no registry equivalent and is fail-loud per council
# Q6 — operator inspects + decides.
_ASSET_CLASS_MAP: dict[str, str] = {
    "equity": "equity",
    "future": "futures",
    "forex": "fx",
    "option": "option",
}


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()

    # Reflection — let SQLAlchemy load the actual current shape.
    cache = sa.Table("instrument_cache", metadata, autoload_with=bind)
    defs = sa.Table("instrument_definitions", metadata, autoload_with=bind)
    aliases = sa.Table("instrument_aliases", metadata, autoload_with=bind)

    rows = bind.execute(sa.select(cache)).mappings().all()
    print(f"[migration] copying {len(rows)} instrument_cache rows → registry")

    now = datetime.now(timezone.utc)
    today = date.today()

    for row in rows:
        canonical_id = row["canonical_id"]
        if "." not in canonical_id:
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} does not parse: "
                f"no '.' separator. Inspect the row in psql and fix at source "
                f"before re-running. Per council Q6=fail-loud."
            )
        raw_symbol, listing_venue = canonical_id.rsplit(".", 1)

        # Asset class taxonomy translation (council Q6 fail-loud on `index`
        # or any unrecognized value — operator inspects + decides).
        legacy_asset_class = row["asset_class"]
        asset_class = _ASSET_CLASS_MAP.get(legacy_asset_class)
        if asset_class is None:
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} has "
                f"asset_class={legacy_asset_class!r} which has no registry "
                f"equivalent. Known mappings: {_ASSET_CLASS_MAP!r}. "
                f"Inspect + delete or relabel the row before re-running."
            )

        # Routing venue: prefer `ib_contract_json["exchange"]` (the routing
        # exchange IB used at qualification time, e.g. "SMART"), falling
        # back to the canonical-id suffix (e.g. "NASDAQ"). Listing venue
        # is the canonical-id suffix (the venue we'd subscribe data on).
        ib_contract_json = row.get("ib_contract_json") or {}
        routing_venue = ib_contract_json.get("exchange") or listing_venue

        trading_hours = row.get("trading_hours")  # may be None
        refreshed_at = row.get("last_refreshed_at", now)

        # Definition upsert: ON CONFLICT DO UPDATE so pre-existing
        # registry rows pick up the migrated trading_hours + refreshed_at.
        # COALESCE preserves existing trading_hours if the cache row's is
        # NULL but the registry already has data.
        defs_stmt = postgresql.insert(defs).values(
            instrument_uid=uuid.uuid4(),
            raw_symbol=raw_symbol,
            provider="interactive_brokers",
            asset_class=asset_class,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
            lifecycle_state="active",
            trading_hours=trading_hours,
            refreshed_at=refreshed_at,
            created_at=now,
            updated_at=now,
        )
        bind.execute(
            defs_stmt.on_conflict_do_update(
                index_elements=["raw_symbol", "provider", "asset_class"],
                set_={
                    "trading_hours": sa.func.coalesce(
                        defs_stmt.excluded.trading_hours,
                        defs.c.trading_hours,
                    ),
                    "refreshed_at": defs_stmt.excluded.refreshed_at,
                    "updated_at": now,
                },
            )
        )

        # Re-fetch the actual UID (ON CONFLICT means ours may not have been used)
        existing_def = bind.execute(
            sa.select(defs.c.instrument_uid).where(
                defs.c.raw_symbol == raw_symbol,
                defs.c.provider == "interactive_brokers",
                defs.c.asset_class == asset_class,
            )
        ).scalar_one()

        # Alias upsert (idempotent on (alias_string, provider, effective_from))
        bind.execute(
            postgresql.insert(aliases).values(
                id=uuid.uuid4(),
                instrument_uid=existing_def,
                alias_string=canonical_id,
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=today,
                effective_to=None,
                created_at=now,
            ).on_conflict_do_nothing(
                constraint="uq_instrument_aliases_string_provider_from",
            )
        )

    op.drop_table("instrument_cache")
    print("[migration] dropped instrument_cache table")


def downgrade() -> None:
    """Schema-only downgrade: recreate empty instrument_cache.

    DATA IS NOT RESTORED. The operator must restore from `pg_dump` if
    the rows are needed. This is documented loudly in the runbook
    (docs/runbooks/instrument-cache-migration.md).
    """
    op.create_table(
        "instrument_cache",
        sa.Column("canonical_id", sa.String(128), primary_key=True),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("ib_contract_json", postgresql.JSONB, nullable=False),
        sa.Column("nautilus_instrument_json", postgresql.JSONB, nullable=False),
        sa.Column("trading_hours", postgresql.JSONB, nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_instrument_cache_class_venue",
        "instrument_cache",
        ["asset_class", "venue"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py -v`
Expected: all PASS (revisions A + B both work, idempotent re-run is a no-op).

Run: `cd backend && uv run alembic heads`
Expected: prints `e2f3g4h5i6j7`.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py \
        backend/tests/integration/test_instrument_cache_migration.py
git commit -m "feat(alembic): drop instrument_cache after migrating rows to registry"
```

---

## Task 10: Delete `models/instrument_cache.py` + remove from `models/__init__.py`

**Goal:** With T9 having dropped the table and all readers (T2/T3/T4) having moved to the registry, the `InstrumentCache` SQLAlchemy 2.0 model is dead code.

**Files:**

- Delete: `backend/src/msai/models/instrument_cache.py`
- Modify: `backend/src/msai/models/__init__.py`

- [ ] **Step 1: Verify no remaining imports**

Run: `cd backend && rg -n "from msai.models.*import.*InstrumentCache\|InstrumentCache" src tests`
Expected: matches only in `tests/integration/test_instrument_cache_model.py` (which T11 deletes) and `models/instrument_cache.py` itself + `models/__init__.py`.

- [ ] **Step 2: Delete the model file**

```bash
git rm backend/src/msai/models/instrument_cache.py
```

- [ ] **Step 3: Update `models/__init__.py`**

Edit `backend/src/msai/models/__init__.py`:

```python
# Remove these lines:
# from msai.models.instrument_cache import InstrumentCache  # noqa: F401

# Remove "InstrumentCache" from __all__ if it's there.
```

- [ ] **Step 4: Verify import structure intact**

Run: `cd backend && uv run python -c "from msai.models import Base; print('ok')"`
Expected: prints `ok`.

Run: `cd backend && uv run python -c "from msai.models import InstrumentCache"`
Expected: `ImportError: cannot import name 'InstrumentCache' from 'msai.models'`.

Run: `cd backend && uv run pytest tests/unit -v -k "models"`
Expected: PASS.

- [ ] **Step 5: Commit (best-effort)**

```bash
git add backend/src/msai/models/__init__.py
git commit -m "refactor: delete InstrumentCache model — registry is sole instrument metadata store"
```

---

## Task 11: Migrate test fixtures + delete `SecurityMaster.resolve_for_live` + delete cache-only tests

**Goal:** Final cleanup pass. Migrate the 5 cache-touching test files to registry semantics. **Delete `SecurityMaster.resolve_for_live` entirely from `service.py`** (per architectural concern + Maintainer's binding objection — the registry is the sole authority for live resolution; supervisor + future callers go through `live_resolver.lookup_for_live` directly). Migrate `test_backtest_live_parity.py:122` (the last non-test caller) to `lookup_for_live`. `test_instrument_cache_model.py` deleted.

**Iter-2 fix:** earlier draft deferred `resolve_for_live` deletion to T11 but didn't actually schedule it in T11's task body. This iteration explicitly lists `service.py` in T11's writes and adds a deletion step.

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py` (DELETE `resolve_for_live` method entirely + the transitional docstring)
- Modify: `backend/tests/integration/test_backtest_live_parity.py` (migrate `sm.resolve_for_live(["AAPL", "ES"])` → `lookup_for_live(...)` directly)
- Delete: `backend/tests/integration/test_instrument_cache_model.py`
- Modify: `backend/tests/integration/test_security_master.py`
- Modify: `backend/tests/integration/test_security_master_resolve_live.py` (DELETE this entire file once `resolve_for_live` is gone — its raison d'être is testing that method)
- Modify: `backend/tests/integration/test_security_master_resolve_backtest.py`
- Modify: `backend/tests/e2e/test_security_master_phase2.py` (migrate any `bulk_resolve(future_spec)` usages — `_build_instrument_from_spec` raises `NotImplementedError` for futures in v1; use `lookup_for_live` directly)

- [ ] **Step 0: Delete `SecurityMaster.resolve_for_live` from `service.py` + migrate the parity test**

After T5 removed the CLI's `sm.resolve_for_live(...)` call site, the remaining callers are tests. Migrate the parity test, then delete the method.

**0a — Migrate `test_backtest_live_parity.py`:**

Edit `backend/tests/integration/test_backtest_live_parity.py:122`. Replace:

```python
# OLD:
live_ids = await sm.resolve_for_live(["AAPL", "ES"])

# NEW (lookup_for_live is the canonical post-PR-#37 primitive):
from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
from msai.services.nautilus.security_master.live_resolver import lookup_for_live

resolved = await lookup_for_live(
    ["AAPL", "ES"], as_of_date=exchange_local_today(), session=session,
)
live_ids = [r.canonical_id for r in resolved]
```

**0b — Delete `SecurityMaster.resolve_for_live` from `service.py`:**

Delete the entire method body. Drop the transitional docstring + the import of `live_resolver.RegistryMissError` that T4 added. Per the architectural concern: the registry is the sole authority for live resolution; `lookup_for_live` is the canonical primitive (used by the supervisor at `live_supervisor/__main__.py:302-305`); a parallel `SecurityMaster.resolve_for_live` API is dead weight.

**0c — Delete `tests/integration/test_security_master_resolve_live.py` entirely:**

```bash
git rm backend/tests/integration/test_security_master_resolve_live.py
```

The file's tests (cold-miss raise, structural assertions on `service.py`) become moot once `resolve_for_live` is gone. The structural-guard test at T12 covers the "no canonical_instrument_id reference" invariant for the whole runtime tree.

**0d — Verify the deletion:**

Run: `cd backend && rg -n "resolve_for_live\(" backend/src backend/tests`
Expected: 0 matches in `backend/src/`. Tests directory may still mention it in test names being deleted in this same task.

- [ ] **Step 1: Delete `test_instrument_cache_model.py`**

```bash
git rm backend/tests/integration/test_instrument_cache_model.py
```

- [ ] **Step 2: Migrate `test_security_master.py`**

Replace every fixture that does `session.add(InstrumentCache(canonical_id=..., ...))` with the registry equivalent:

```python
# OLD (delete):
session.add(InstrumentCache(
    canonical_id="AAPL.NASDAQ",
    asset_class="equity",
    venue="NASDAQ",
    ib_contract_json={...},
    nautilus_instrument_json={...},
    trading_hours={...},
    last_refreshed_at=now,
))

# NEW:
aapl_uid = uuid4()
session.add(InstrumentDefinition(
    instrument_uid=aapl_uid,
    raw_symbol="AAPL",
    provider="interactive_brokers",
    asset_class="equity",
    listing_venue="NASDAQ",
    routing_venue="SMART",
    lifecycle_state="active",
    trading_hours={"timezone": "America/New_York", "rth": [], "eth": []},
))
session.add(InstrumentAlias(
    id=uuid4(),
    instrument_uid=aapl_uid,
    alias_string="AAPL.NASDAQ",
    venue_format="exchange_name",
    provider="interactive_brokers",
    effective_from=date(2026, 1, 1),
    effective_to=None,
))
```

Tests that previously asserted `_read_cache(canonical_id)` returns a row → assert `registry.find_by_alias(canonical_id, ...)` returns a definition.

- [ ] **Step 3: ~~Migrate~~ `test_security_master_resolve_live.py` was already DELETED in Step 0c — skip**

(Reconciliation note: earlier draft of T11 had this as a "migrate" step. Step 0c deletes the file because its raison d'être — testing `SecurityMaster.resolve_for_live` — is gone after that method's deletion. This step intentionally has no work.)

- [ ] **Step 4: Migrate `test_security_master_resolve_backtest.py`**

This file is already mostly registry-only (per recon). Drop any residual `InstrumentCache` fixture writes; verify all tests pass.

- [ ] **Step 5: Migrate `test_security_master_phase2.py` (e2e)**

Replace the fixture pattern as in Step 2. Additionally: at `tests/e2e/test_security_master_phase2.py:135-152` the test exercises `bulk_resolve` with a future spec (`asset_class="future"`). Per T3's `_build_instrument_from_spec` v1 scope (equity + forex only), this test must migrate to use `live_resolver.lookup_for_live(["ES"], as_of_date=exchange_local_today(), session=session)` directly and assert on the returned `ResolvedInstrument.canonical_id` instead of calling `bulk_resolve` for futures.

- [ ] **Step 6: Migrate `test_instruments_refresh_ib_smoke.py:144`**

Edit `backend/tests/e2e/test_instruments_refresh_ib_smoke.py` line 144. Replace `await sm.resolve_for_live(["AAPL"])` with `await lookup_for_live(["AAPL"], as_of_date=exchange_local_today(), session=session)` (importing both helpers). The smoke test now exercises the canonical primitive that the supervisor uses, not the deleted `SecurityMaster.resolve_for_live` shim.

- [ ] **Step 7: Comprehensive migration of `tests/unit/test_cli_instruments_refresh.py`**

Three classes of changes are needed in this file. Each is mechanical but the file as a whole must compile + pass after the migration.

**7a — Delete the closed-universe test cluster** (lines 178, 247, 273 per recon). These tests assert behaviors of the `accepted` map / closed-universe rejection logic that T5 deletes (`SPY.NASDAQ silently normalizing to SPY` rejection, `not in the closed universe` error message, etc.). After T5, those code paths don't exist. Delete:

- `test_cli_instruments_refresh_rejects_unknown_symbol` (around line 178)
- `test_cli_instruments_refresh_rejects_malformed_aliases` (around line 247)
- `test_cli_instruments_refresh_accepts_dotted_and_futures_aliases` (around line 273)

The `_FUT_QUARTERLY_ROOTS` rejection path in T5 step 1 covers the modern "unknown root" case for FUT. STK accepts any symbol now (IB resolves via SMART + primaryExchange).

**7b — Migrate the `SecurityMaster.resolve_for_live` mocks at lines 391 + 462**. Both currently `monkeypatch.setattr("msai.services.nautilus.security_master.service.SecurityMaster.resolve_for_live", ...)`. After T5/T11's deletion of that method, these patches target a non-existent attribute.

The cleaner fix here: instead of swapping the patch target (which then needs to also stub DB `execute`/`flush` because T5's `_run_ib_resolve_for_live` writes registry rows), **stub `_run_ib_resolve_for_live` itself** as a higher-level seam. That function's CLI-facing shape is `(contracts: list[IBContract]) -> list[dict]` (per T5 step 0d), and the existing `monkeypatch.setattr("msai.cli._run_ib_resolve_for_live", fake)` pattern from T5 step 1's last test (`test_cli_instruments_refresh_builds_contracts_for_supported_fut_roots`) is exactly the right shape to reuse:

```python
async def fake_run_ib_resolve_for_live(contracts):
    # No DB writes; return the resolved canonicals the CLI prints.
    return [{"symbol": c.symbol, "canonical": f"{c.symbol}.NASDAQ"} for c in contracts]

monkeypatch.setattr("msai.cli._run_ib_resolve_for_live", fake_run_ib_resolve_for_live)
```

This avoids the DB-stub trap (existing fake sessions only stub `commit`/`rollback`, not `execute`/`flush`) by replacing the function whose body would need DB access. Apply at the call sites that previously patched `resolve_for_live`.

**7c — Drop the now-irrelevant `_accepted_alias_cases` parametrize fixture** (if present in the file — the closed-universe alias-acceptance map is gone).

**Verify:** `cd backend && uv run pytest tests/unit/test_cli_instruments_refresh.py -v`
Expected: PASS — every remaining test exercises either `_build_ib_contract_for_symbol` directly OR the CLI command with `_run_ib_resolve_for_live` stubbed at the function boundary.

- [ ] **Step 8: Run all migrated tests**

Run: `cd backend && uv run pytest tests/integration/test_security_master.py tests/integration/test_security_master_resolve_backtest.py tests/e2e/test_security_master_phase2.py tests/e2e/test_instruments_refresh_ib_smoke.py tests/unit/test_cli_instruments_refresh.py -v`
Expected: all PASS. (Note: `test_security_master_resolve_live.py` is gone — not in the run list.)

- [ ] **Step 7: Commit (best-effort)**

```bash
git add backend/tests/
git commit -m "test: migrate cache-touching fixtures to registry semantics"
```

---

## Task 12: Strengthen structural-guard test

**Goal:** Replace the narrow `test_canonical_instrument_id_runtime_isolation.py` (which only covered 2 runtime files) with a broader forbidden-name guard that scans all of `backend/src/msai/` for any reintroduction of legacy symbols. Allowlist Alembic migrations + this test file itself.

**Files:**

- Rewrite: `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` (rename to `test_legacy_symbol_isolation.py` for clarity, OR keep name + extend scope)

- [ ] **Step 1: Rewrite the structural-guard test**

Replace the entire content of `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py`:

```python
"""Structural guard — prevents architectural backsliding.

Per council verdict 2026-04-27 (Q10 stronger replacement strategy):
the registry is the SOLE source of truth for instrument metadata.
This test walks the AST of every Python file under
``backend/src/msai/`` and fails if any forbidden legacy symbol is
reintroduced — by definition, by import, by attribute access, or by
``Name`` reference.

Forbidden symbols (council-ratified):
    - canonical_instrument_id  (Phase-1 closed-universe helper, deleted T6+T7)
    - InstrumentCache          (legacy model, deleted T10)
    - _read_cache              (cache-IO method, deleted T3)
    - _read_cache_bulk         (cache-IO method, deleted T3)
    - _write_cache             (cache-IO method, deleted T3)
    - _instrument_from_cache_row (cache helper, deleted T3)
    - _ROLL_SENSITIVE_ROOTS    (dead-code constant, deleted T4)

Allowed:
    - Alembic migrations under backend/alembic/versions/ (their docstrings
      legitimately reference the legacy symbols for historical context).
    - This test file itself (defines the forbidden list).
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "canonical_instrument_id",
    "InstrumentCache",
    "_read_cache",
    "_read_cache_bulk",
    "_write_cache",
    "_instrument_from_cache_row",
    "_ROLL_SENSITIVE_ROOTS",
})

ALLOWLIST: frozenset[str] = frozenset({
    # This test file — defines the forbidden list.
    "tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py",
})


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).parents[4]


def _scan_python_files() -> list[pathlib.Path]:
    """Return every .py under backend/src/msai/ minus the allowlist."""
    root = _repo_root()
    src = root / "backend" / "src" / "msai"
    files = sorted(src.rglob("*.py"))
    # Strip __pycache__ / .pyc artifacts (rglob doesn't return them but be safe)
    return [f for f in files if "__pycache__" not in f.parts]


def _find_forbidden_references(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Walk the AST of `path`. Return (line, kind, symbol) for every
    forbidden reference."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as exc:
        return [(0, "syntax_error", str(exc))]

    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            hits.append((node.lineno, "name_ref", node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            hits.append((node.lineno, "attr_access", node.attr))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    hits.append((node.lineno, "import_from", alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES or alias.name.endswith(
                    tuple(f".{n}" for n in FORBIDDEN_NAMES)
                ):
                    hits.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.FunctionDef) and node.name in FORBIDDEN_NAMES:
            hits.append((node.lineno, "function_def", node.name))
        elif isinstance(node, ast.ClassDef) and node.name in FORBIDDEN_NAMES:
            hits.append((node.lineno, "class_def", node.name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in FORBIDDEN_NAMES:
                    hits.append((node.lineno, "assignment", target.id))
    return hits


def test_no_legacy_symbols_in_runtime_source() -> None:
    """Every .py under backend/src/msai/ MUST NOT reference any legacy
    symbol. Allowlist applies only to this test file."""
    root = _repo_root()
    violations: dict[str, list[tuple[int, str, str]]] = {}
    for path in _scan_python_files():
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWLIST:
            continue
        hits = _find_forbidden_references(path)
        if hits:
            violations[rel] = hits

    assert not violations, (
        f"Forbidden legacy symbols still referenced in runtime source:\n"
        f"{violations!r}\n\n"
        f"Per council verdict 2026-04-27 (Q9 + Q10): the registry is the SOLE "
        f"source of truth for instrument metadata. Replace any legacy reference "
        f"with the registry-backed equivalent (see PRD §9 binding decisions in "
        f"docs/prds/instrument-cache-registry-migration.md)."
    )


def test_alembic_migrations_are_not_scanned() -> None:
    """Sanity check: Alembic migrations live under backend/alembic/versions/
    and are NOT under backend/src/msai/, so they're outside the scan scope.
    This test asserts that fact so a future move doesn't accidentally
    pull them in."""
    root = _repo_root()
    alembic_dir = root / "backend" / "alembic" / "versions"
    src_dir = root / "backend" / "src" / "msai"
    # Symbolic check
    assert not str(alembic_dir).startswith(str(src_dir)), (
        "alembic/versions accidentally moved under src/msai — the structural "
        "guard would falsely flag the migration's docstring references to "
        "instrument_cache. Move alembic back to backend/alembic."
    )
```

- [ ] **Step 2: Run the structural guard**

Run: `cd backend && uv run pytest tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py -v`
Expected: PASS (after T6+T7 deleted the definitions, T3+T4 deleted the constants/methods, T10 deleted the model).

If FAIL — the failure message names every unmigrated reference. Fix before continuing.

- [ ] **Step 3: Optional belt+suspenders — ruff banned-api**

Add to `backend/pyproject.toml` under `[tool.ruff.lint.flake8-tidy-imports.banned-api]`:

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"msai.models.instrument_cache" = { msg = "Deleted 2026-04-27 — registry is sole source of truth. Use msai.models.{InstrumentDefinition, InstrumentAlias}." }
"msai.services.nautilus.live_instrument_bootstrap.canonical_instrument_id" = { msg = "Deleted 2026-04-27 — use live_resolver.lookup_for_live(...) directly, or per-asset-class IBContract factories at cli.py:_build_ib_contract_for_symbol for IB seeding." }
"msai.services.nautilus.instruments.canonical_instrument_id" = { msg = "Deleted 2026-04-27 — use resolve_instrument(symbol, venue=...).id directly." }
"msai.services.nautilus.security_master.service.SecurityMaster.resolve_for_live" = { msg = "Deleted 2026-04-27 (T11) — use live_resolver.lookup_for_live(...) directly. The supervisor at live_supervisor/__main__.py:302 is the canonical caller pattern." }
```

Run: `cd backend && uv run ruff check src/`
Expected: clean.

- [ ] **Step 4: Commit (best-effort)**

```bash
git add backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py backend/pyproject.toml
git commit -m "test: structural guard against legacy instrument-cache + canonical_instrument_id symbols"
```

---

## Task 13: Migration round-trip integration tests (consolidation)

**Goal:** All migration tests in one file, exercising upgrade-A → upgrade-B → downgrade-B → downgrade-A → re-upgrade-B. Plus the preflight pass-case + fail-case.

**Files:**

- Already created: `backend/tests/integration/test_instrument_cache_migration.py` (extended in T1, T8, T9)
- This task is consolidation — verify all tests pass together as a regression-stable suite.

- [ ] **Step 1: Add the round-trip test**

Append:

```python
@pytest.mark.asyncio
async def test_full_round_trip_upgrade_a_b_downgrade_b_a_re_upgrade(
    isolated_postgres_url: str,
) -> None:
    """End-to-end: A up → B up → B down (data lost, schema-only) →
    A down → A up → B up → still works."""
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)  # back to A
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)  # back to PRIOR_HEAD
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Final state assertion
    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        head = await conn.execute(text("SELECT version_num FROM alembic_version"))
        assert head.scalar_one() == REV_B
    await engine.dispose()
```

- [ ] **Step 2: Run the full migration suite**

Run: `cd backend && uv run pytest tests/integration/test_instrument_cache_migration.py -v`
Expected: all PASS.

- [ ] **Step 3: Commit (best-effort)**

```bash
git add backend/tests/integration/test_instrument_cache_migration.py
git commit -m "test: round-trip integration test for instrument_cache migration"
```

---

## Task 14: Operator runbook

**Goal:** Document the operator's exact command sequence for the maintenance window. Includes `pg_dump` checkpoint, preflight, alembic upgrade, worker restart, smoke test.

**Files:**

- Create: `docs/runbooks/instrument-cache-migration.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/instrument-cache-migration.md`:

```markdown
# Runbook: Instrument Cache → Registry Migration

**PR:** #TBD (squash-merge into `main`)
**Migration revisions:** A (`d1e2f3g4h5i6` — additive `trading_hours` column) → B (`e2f3g4h5i6j7` — data migration + DROP `instrument_cache`)
**Maintenance window:** 5–15 minutes. Stack must be DOWN during the data migration.

## Preconditions

- [ ] All active live deploys are STOPPED (or expected to restart after the migration). Use `msai live status` to inventory.
- [ ] You have a `pg_dump` checkpoint of `instrument_cache`, `instrument_definitions`, and `instrument_aliases`.
- [ ] You have read PRD §9 binding decisions: [docs/prds/instrument-cache-registry-migration.md](../prds/instrument-cache-registry-migration.md).

## Step 1 — `pg_dump` checkpoint (REQUIRED — downgrade is data-lossy)

The Alembic downgrade is **schema-only**. Data restoration requires `pg_dump`. Without this checkpoint, a rollback to before this PR loses every `instrument_cache` row that wasn't already in the registry.

\`\`\`bash
docker exec -it msai-postgres-dev pg_dump \
 -U msai \
 -t instrument_cache \
 -t instrument_definitions \
 -t instrument_aliases \
 msai > /tmp/pre-cache-mig-$(date +%Y%m%d-%H%M).sql
\`\`\`

Verify the file is non-empty.

## Step 2 — Preflight gate

\`\`\`bash
cd backend && uv run python scripts/preflight_cache_migration.py
\`\`\`

**Expected:** `[ok] All N active deployment-member rows' instruments resolve through the registry. Preflight passed.` and exit 0.

**If preflight fails:** the script prints `msai instruments refresh --symbols X --provider interactive_brokers` for every missing alias. Run the suggested commands, then re-run preflight. Do NOT proceed to Step 3 until preflight exits 0.

## Step 3 — Stop the stack

\`\`\`bash
docker compose -f docker-compose.dev.yml down
\`\`\`

## Step 4 — Apply the migration

Bring up just postgres + redis so alembic can connect:

\`\`\`bash
docker compose -f docker-compose.dev.yml up -d postgres redis
cd backend && uv run alembic upgrade head
\`\`\`

**Expected output:**
\`\`\`
INFO [alembic.runtime.migration] Running upgrade <prior> -> d1e2f3g4h5i6, add trading_hours...
INFO [alembic.runtime.migration] Running upgrade d1e2f3g4h5i6 -> e2f3g4h5i6j7, drop instrument_cache
[migration] copying N instrument_cache rows → registry
[migration] dropped instrument_cache table
\`\`\`

**If migration aborts on a malformed row** (per Q6=fail-loud): the error message names the offending `canonical_id`. Inspect with:

\`\`\`bash
docker exec -it msai-postgres-dev psql -U msai -d msai -c "SELECT canonical_id, asset_class, venue FROM instrument_cache WHERE canonical_id LIKE '%<bad>%'"
\`\`\`

Fix at source (manual `UPDATE` or `DELETE`), then re-run `alembic upgrade head`.

## Step 5 — Bring up the rest of the stack

\`\`\`bash
docker compose -f docker-compose.dev.yml up -d
./scripts/restart-workers.sh
\`\`\`

The `restart-workers.sh` step is mandatory per `feedback_restart_workers_after_merges.md` — long-running worker containers cache imported modules at startup; without restart, they keep the OLD `models/instrument_cache.py` import in memory and crash on first DB call.

## Step 6 — Smoke test

\`\`\`bash
curl -sf http://localhost:8800/health
\`\`\`
Expected: `200 OK`.

\`\`\`bash
docker exec -it msai-postgres-dev psql -U msai -d msai -c "SELECT count(_) FROM instrument_definitions; SELECT count(_) FROM instrument_aliases;"
\`\`\`
Expected: counts ≥ pre-migration `instrument_cache` row count.

\`\`\`bash
docker exec -it msai-postgres-dev psql -U msai -d msai -c "SELECT table_name FROM information_schema.tables WHERE table_name='instrument_cache'"
\`\`\`
Expected: empty result (table dropped).

## Step 7 — Branch-local restart drill (per US-005)

This is the evidence required to satisfy the council's Q8 verification gate.

1. Spawn a paper deploy that holds at least one open position (use `msai live start-portfolio` or `msai live status` to confirm an existing one).
2. Run Steps 3–6 above (compose down → migrate → compose up → workers restart).
3. Verify:
   - Deploy resumes (or is cleanly stopped via `msai live kill-all`).
   - Open position rehydrates correctly via `position_reader.py` → check `msai live positions`.
   - No log line in container logs references `instrument_cache` after restart:
     \`\`\`bash
     docker compose -f docker-compose.dev.yml logs backend worker | grep -i "instrument_cache" || echo "clean"
     \`\`\`
     Expected: prints `clean`.

Capture the drill output (stdout of each command above) and paste into the PR description as the Q8 evidence.

## Rollback (data-lossy — only if Step 4 or later fails fatally)

\`\`\`bash

# Restore the pg_dump from Step 1

docker exec -i msai-postgres-dev psql -U msai -d msai < /tmp/pre-cache-mig-YYYYMMDD-HHMM.sql

# Then re-apply the prior alembic head

cd backend && uv run alembic downgrade <PRIOR_HEAD>
\`\`\`

## Post-merge tasks

- [ ] Update CHANGELOG with the migration note.
- [ ] Mark CONTINUITY's `## Done (cont'd N)` section.
- [ ] If a real-money deploy is on this stack, schedule a paper-week soak before the next live drill.
```

- [ ] **Step 2: Verify the runbook is well-formed**

Run: `cat docs/runbooks/instrument-cache-migration.md | wc -l`
Expected: > 100 lines.

- [ ] **Step 3: Commit (best-effort)**

```bash
git add docs/runbooks/instrument-cache-migration.md
git commit -m "docs(runbook): instrument-cache → registry migration operator playbook"
```

---

## E2E Use Cases (Phase 3.2b)

Six use cases drive the verify-e2e agent in Phase 5.4. Project type = `fullstack` per CLAUDE.md, but **all six are API/CLI use cases** — no UI surface. Sanctioned interfaces only (no raw DB writes for VERIFY).

### UC-ICR-001 — Migration applies cleanly on a populated dev stack

**Interface:** CLI + API (HTTP)

**Setup (ARRANGE):**

- Start the dev stack on the pre-migration commit (the parent of this branch's HEAD).
- Seed `instrument_cache` with at least one row via `msai instruments refresh --provider interactive_brokers --symbols AAPL --asset-class stk` against IB Gateway paper.

**Steps:**

1. `cd backend && uv run python scripts/preflight_cache_migration.py` → expect exit 0.
2. `pg_dump -t instrument_cache -t instrument_definitions -t instrument_aliases msai > /tmp/checkpoint.sql`.
3. `docker compose -f docker-compose.dev.yml down`.
4. `docker compose -f docker-compose.dev.yml up -d postgres redis`.
5. `cd backend && uv run alembic upgrade head`.
6. `docker compose -f docker-compose.dev.yml up -d && ./scripts/restart-workers.sh`.

**Verification (VERIFY):**

- `curl -sf http://localhost:8800/health` returns 200.
- API `GET /api/v1/instruments/registry?symbol=AAPL` returns the migrated definition (active alias `AAPL.NASDAQ`, asset_class `equity`).
- `docker exec msai-postgres-dev psql -U msai -d msai -c "\\d instrument_cache"` returns "Did not find any relation" (table dropped).
- Container logs do not contain `instrument_cache` after restart.

**Persistence:** Restart the backend container; re-run the API call. Same row.

### UC-ICR-002 — Preflight fails loud on missing alias for active deployment

**Interface:** CLI

**Setup:**

- Pre-migration stack up.
- Active `LiveDeployment` whose portfolio revision has a `LivePortfolioRevisionStrategy.instruments=['BOGUS']` member, in `running` state (use `msai live start-portfolio` against a portfolio referencing a strategy whose `instruments` list contains a symbol intentionally NOT in the registry).

**Steps:**

1. Run `cd backend && uv run python scripts/preflight_cache_migration.py`.

**Verification:**

- Exit code is non-zero.
- stdout contains the line: `deployment <slug>: 'BOGUS.XNYS'` and `Run: msai instruments refresh --symbols BOGUS --provider interactive_brokers`.
- The migration is NOT applied (alembic head is unchanged).

**Persistence:** The active deployment row is unchanged.

### UC-ICR-003 — `MarketHoursService` answers RTH question correctly post-migration

**Interface:** API

**Setup:**

- Post-migration stack up.
- Registry has AAPL.NASDAQ with NYSE-style trading hours (migrated in step 5 of UC-001).

**Steps:**

1. Submit a backtest via `POST /api/v1/backtests/run` against AAPL.NASDAQ during a backtest window that includes both pre-market and RTH bars.
2. Wait for the backtest to complete.

**Verification:**

- `GET /api/v1/backtests/{id}/results` returns `series_status: ready`.
- `GET /api/v1/backtests/{id}/trades` shows trades only during RTH (no pre-market trades unless `allow_eth=True`).

**Persistence:** Re-fetch the trades API. Same answer.

### UC-ICR-004 — `lookup_for_live` fail-loud cold-miss on `/live/start-portfolio`

**Interface:** API

**Setup:**

- Post-migration stack up.
- Registry has AAPL.NASDAQ; does NOT have GOOG.NASDAQ.

**Steps:**

1. Submit `POST /api/v1/live/start-portfolio` with a portfolio whose strategy `instruments` list references GOOG.

**Verification:**

- API returns 422 with body containing `RegistryMissError` (the existing `live_resolver.RegistryMissError` raised by `lookup_for_live` at supervisor spawn time, surfaced through the API's failure-kind dispatch) and an operator-action hint pointing at `msai instruments refresh --symbols GOOG --provider interactive_brokers --asset-class stk`.

**Persistence:** No `LiveDeployment` row in `running` state was created (deployment may transition to `starting` then immediately `failed` per supervisor's permanent-catch path; verify via `GET /api/v1/live/status` shows GOOG deployment as failed with `FailureKind.REGISTRY_MISS`).

### UC-ICR-005 — `msai instruments refresh` per-asset-class factories work end-to-end (paper IB)

**Interface:** CLI

**Setup:**

- Post-migration stack up + IB Gateway paper reachable (broker compose profile up).

**Steps:**

1. `msai instruments refresh --provider interactive_brokers --asset-class fut --symbols ES`.
2. `msai instruments refresh --provider interactive_brokers --asset-class stk --symbols AAPL`.
3. `msai instruments refresh --provider interactive_brokers --asset-class cash --symbols EUR/USD`.

**Verification:**

- Each command exits 0.
- Output JSON contains `"resolved": [{"symbol": "ES", "canonical": "ESM6.CME"}, ...]` (or the actual front-month for ES today).
- Subsequent `GET /api/v1/instruments/registry?symbol=ES` returns the row.

**Persistence:** Re-run command 1 — second invocation is a no-op (idempotent).

**Note:** This UC requires `RUN_PAPER_E2E=1` opt-in. Default skips when IB Gateway is not on the broker profile.

### UC-ICR-006 — Branch-local restart drill (operator step, captured in PR description)

**Interface:** CLI + API + manual

**Setup:**

- Pre-migration stack up.
- Spawn a paper deploy via `msai live start-portfolio` against a symbol the registry has covered (e.g. AAPL).
- Wait for at least one trade to fire (or use a long-running test strategy that doesn't trade yet — open positions optional).

**Steps:** As documented in `docs/runbooks/instrument-cache-migration.md` Step 7.

**Verification:** Per runbook Step 7 + structured log `position_reader_rehydrated` after restart with the deployment's open positions intact.

**Persistence:** Stop + restart backend container; positions still hydrate.

**This UC produces the Phase 5.4 evidence** required by US-005.

---

## Self-Review

### Spec coverage

- US-001 (migrate cache rows + drop table) → T1 + T9 + T10
- US-002 (preflight gate) → T8 + T13
- US-003 (stop reading instrument_cache everywhere) → T2 + T3 (security_master) + T11 (test fixture migration)
- US-004 (delete `canonical_instrument_id()` fully) → T4 + T5 + T6 + T7 + T12 (structural guard)
- US-005 (branch-local restart drill) → T14 (runbook) + UC-ICR-006 (E2E)
- US-006 (structural guard + test fixture migration) → T11 + T12

Every PRD §9 binding decision is honored:

- Q1 (combined PR): all tasks ship in this branch.
- Q2 (preflight gate): T8.
- Q3 (trading_hours JSONB column on instrument_definitions): T1.
- Q4 (delete nautilus_instrument_json): T9 (column not migrated forward).
- Q5 (drop ib_contract_json entirely): T9 (column not migrated forward).
- Q6 (fail-loud orphans): T9's data migration aborts on malformed canonical_id.
- Q7 (hard cutover, no shim): no shim anywhere; T9 drops the table.
- Q8 (skeleton precondition cleared + branch-local restart proof): T14 runbook + UC-ICR-006.
- Q9 (full canonical removal expanded scope): T4 (resolve_for_live cold-miss removed), T5 (CLI direct factories), T6 + T7 (definitions deleted), T4 (`_ROLL_SENSITIVE_ROOTS` deleted).
- Q10 (stronger structural guard test): T12.

### Placeholder scan

- No "TBD", "TODO", "implement later" in tasks T1–T14.
- No "fill in details" / "add appropriate error handling" / "similar to Task N" patterns.
- Every code-changing step shows the exact code body to write.
- Every test step shows the exact test code.
- Every command step shows the exact command + expected output.

### Type consistency

- `RegistryMissError` defined once in T4; referenced in T11 fixtures + UC-ICR-004.
- `_build_ib_contract_for_symbol(symbol, *, asset_class, today)` defined once in T5; signature consistent across T5 tests + UC-ICR-005.
- `MarketHoursService.prime(session, canonical_ids)` signature unchanged (only the internal implementation changed) — T2.
- Alembic revision IDs `d1e2f3g4h5i6` (T1) and `e2f3g4h5i6j7` (T9) consistent across migration files + tests.
- Forbidden-name list in T12 matches the symbols deleted in T3, T4, T6, T7, T10.

---

## Execution Handoff

**Plan complete and saved to `docs/plans/2026-04-27-instrument-cache-registry-migration.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

> **Note:** The plan-review loop (Phase 3.3) runs BEFORE execution begins — Claude + Codex review the plan against the actual code, iterating until 0 P0/P1/P2. Only after review converges does the execution choice apply.
