# DB-Backed Strategy Registry + Continuous Futures Implementation Plan (v3.0)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Revision history:**

- v1.0 (2026-04-17 AM) — initial plan, 25 tasks.
- v2.0 (2026-04-17 PM) — post-iter-1 plan-review rewrite. 22 tasks.
- v2.1 (2026-04-17 PM) — post-iter-2 plan-review rewrite. Codex iter-2 caught a P0 Claude missed: v2.0 added `SecurityMaster.resolve_for_live/backtest` but never wired them into `api/backtests.py:90`, `catalog_builder.py:99`, `workers/backtest_job.py:100`, or `live_supervisor/__main__.py:297-300` — v2.0 would have shipped operationally inert. v2.1 added Phase 6 "Production wiring" (new T22–T23). Also fixed mechanical issues: `InstrumentSpec.from_string` doesn't exist (replaced with helper); `instrument_to_payload` → `nautilus_instrument_to_cache_json` (alias-imported); `get_session_factory` → `async_session_factory`; `find_by_raw_symbol` ambiguity handling removed; Task 10 narrowed; Task 11 merged into Task 7; parity test rewritten per PRD US-001. Net: 23 tasks.
- v2.2 (2026-04-17 PM) — post-iter-3 plan-review rewrite. Architectural discovery: T23's "API-layer resolves + passes `resolved_instruments` through payload dict" plan is incompatible with supervisor's deliberate "ignore `payload_dict`, rebuild everything from DB" design (see `__main__.py:105` `# noqa: ARG001`). Supervisor has no `IBQualifier` and no way to construct one without major refactor, so API-side Option-A is the ONLY feasible place to call `SecurityMaster.resolve_for_live`. Fix: persist resolved canonicals on `live_portfolio_revision_strategies` via a new `canonical_instruments` JSONB column populated at revision-snapshot time in `RevisionService`; supervisor reads the new column (Option B). Adds T22.5 (schema migration + model column + `RevisionService` population) and rewrites T23 to read the new column. Plus iter-3 mechanical fixes: T8 `_spec_from_canonical` made an instance method (drop `@staticmethod`) and `_asset_class_for_instrument` defined inline; T9 hidden-state (`self._backtest_*`) replaced with explicit kwargs on `resolve_for_backtest`; T9 cold-miss now reuses T8's `_upsert_definition_and_alias` with `provider="databento"` kwarg for idempotency; T22 `SecurityMaster(qualifier=None, ...)` ctor relaxation; T22 worker-path change DELETED (worker already reads canonical IDs from `Backtest.instruments`); T22 catalog_builder change kept with explicit new-function spec; T6 test imports (`definition_window_bounds_from_details`, `continuous_needs_refresh_for_window`) added; T8 cold-miss closed-universe documented explicitly; T18 parity test `mock_databento` fixture removed; T22 regression claim caveat added re: futures registry pre-seed. Global renumbering: T1–T23 now sequential top-to-bottom. Net: 24 tasks (T22.5 added).
- **v3.0 (2026-04-17 PM) — post-iter-4 scope-back rewrite. After 4 review iterations couldn't settle live-wiring architecture (Option A killed by supervisor having no IBQualifier; Option B killed by `canonical_instruments` conflicting with immutable revision identity via `composition_hash` collapse), user decided to drop live-wiring from this PR. Ships backtest-only. Live wiring becomes follow-up PR with its own design pass + council (see expanded skeleton at end of this file). Dropped: T20 (schema migration for `canonical_instruments` on revision_strategy) and T21 (supervisor reads new column). Kept: registry schema + services + continuous-futures helpers + `msai instruments refresh` CLI + backtest wiring + split-brain normalization. Also fixes remaining iter-4 mechanical findings on the kept tasks. Net: 20 tasks (was 24).**

**Goal:** Ship a thin Postgres instrument control-plane (`InstrumentDefinition` + `instrument_alias`), port codex-version's Databento `.Z.N` continuous-futures synthesis, verify `CacheConfig(database=redis)` works end-to-end (already wired), add `msai instruments refresh` CLI, and normalize the `.XCME` → `.CME` split-brain.

**Architecture:** New tables hold control-plane metadata (UUID PK, raw_symbol, listing_venue, routing_venue, provider, roll_policy, refreshed_at, lifecycle_state) + a child `instrument_alias` table mapping venue-qualified IDs to the UUID. Existing `SecurityMaster` class is extended with two new async methods: `resolve_for_live(symbols)` and `resolve_for_backtest(symbols)`. Nautilus's own cache DB (already wired via `CacheConfig(database=redis)`) holds `Instrument` payloads. Existing `instrument_cache` table stays untouched in this PR; follow-up PR migrates it.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, Alembic, FastAPI, Pydantic v2, Typer, arq, NautilusTrader 1.223.0, Databento Python client, Redis 7, PostgreSQL 16.

**Worktree:** `.worktrees/db-backed-strategy-registry` (branch `feat/db-backed-strategy-registry`). All paths relative to this worktree.

**References:**

- PRD: `docs/prds/db-backed-strategy-registry.md`
- Discussion log: `docs/prds/db-backed-strategy-registry-discussion.md`
- Council verdict: `/tmp/msai-research/council/chairman-verdict.md`
- Plan-review iter-1: Claude pass (in-session), Codex pass at `/tmp/msai-research/plan-review-codex.md`
- Port source: `codex-version/backend/src/msai/services/nautilus/instrument_service.py` (lines **32–59** for dataclass, 440–605 for `.Z.N` helpers) and `codex-version/backend/src/msai/services/data_sources/databento_client.py:63-100` for `fetch_definition_instruments`.

**Existing classes to extend (read BEFORE modifying):**

- `SecurityMaster` at `claude-version/backend/src/msai/services/nautilus/security_master/service.py:84-93`: ctor signature is `(*, qualifier: IBQualifier, db: AsyncSession, cache_validity_days: int = DEFAULT_CACHE_VALIDITY_DAYS)`. Stores `self._qualifier`, `self._db`. **Do NOT rename** — extend in place.
- `IBQualifier` at `.../ib_qualifier.py:157-210`: `async def qualify(self, spec: InstrumentSpec) -> Instrument`. Single return, NOT a tuple. `primaryExchange` comes from `self._qualifier._provider.contract_details[instrument_id]`, not from a second tuple return.
- Strategy-registry module-level functions at `services/strategy_registry.py:109-183` (`discover_strategies`, `DiscoveredStrategy` dataclass). **Not touched in this PR** — split to follow-up.
- `InstrumentCache` model at `models/instrument_cache.py` — **not touched in this PR**. Coexists alongside new registry tables; follow-up PR migrates.

**Test-fixture convention (IMPORTANT):** claude-version integration tests use a per-file `session_factory`/`session` pattern from `PostgresContainer`, NOT a generic `db_session: AsyncSession`. Reference: `tests/integration/test_instrument_cache_model.py:26-45`, `tests/integration/test_security_master.py:41-59`.

---

## Phase Outline (20 tasks, 9 phases — canonical numbering, sequential top-to-bottom)

| Phase | Scope                                                                                        | Tasks     |
| ----- | -------------------------------------------------------------------------------------------- | --------- |
| 1     | Schema + models (pure additive, zero live-risk)                                              | T1 – T3   |
| 2     | Registry lookup layer                                                                        | T4        |
| 3     | Databento `.Z.N` continuous-futures helpers (moved BEFORE resolve-path to avoid forward-ref) | T5 – T7   |
| 4     | SecurityMaster async resolve extensions                                                      | T8 – T9   |
| 5     | Nautilus-native persistence verification                                                     | T10       |
| 6     | Backtest wiring (scope-back: live wiring deferred to follow-up PR)                           | T11       |
| 7     | CLI                                                                                          | T12 – T13 |
| 8     | Split-brain normalization                                                                    | T14 – T16 |
| 9     | Integration tests + docs + verify                                                            | T17 – T20 |

### Renumbering note

See the Phase Outline above for canonical numbering. All `### Task N:` headers are now sequential 1–20 from top to bottom. v3.0 dropped live-wiring tasks (old T20/T21) — those become a follow-up PR (see skeleton at end of file). Phase 6 now contains backtest wiring only (formerly T19 of v2.2). Phases 7–9 are renumbered accordingly.

---

## Phase 1: Schema + Models

### Task 1: Alembic migration for `instrument_definitions` + `instrument_aliases`

**Files:**

- Create: `claude-version/backend/alembic/versions/v0q1r2s3t4u5_instrument_registry.py`

**Step 1: Confirm chain head**

Run: `cd claude-version/backend && uv run alembic current`
Expected output contains: `u9p0q1r2s3t4` (enforce portfolio_revision_id NOT NULL)

**Step 2: Write the migration file**

```python
"""Add instrument_definitions + instrument_aliases control-plane tables.

Revision ID: v0q1r2s3t4u5
Revises: u9p0q1r2s3t4
Create Date: 2026-04-17 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "v0q1r2s3t4u5"
down_revision: str = "u9p0q1r2s3t4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_definitions",
        sa.Column(
            "instrument_uid",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("raw_symbol", sa.String(100), nullable=False),
        sa.Column("listing_venue", sa.String(32), nullable=False),
        sa.Column("routing_venue", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("roll_policy", sa.String(64), nullable=True),
        sa.Column("continuous_pattern", sa.String(32), nullable=True),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "lifecycle_state",
            sa.String(32),
            nullable=False,
            server_default="staged",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.CheckConstraint(
            "asset_class IN ('equity','futures','fx','option','crypto')",
            name="ck_instrument_definitions_asset_class",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('staged','active','retired')",
            name="ck_instrument_definitions_lifecycle_state",
        ),
        sa.CheckConstraint(
            "continuous_pattern IS NULL OR continuous_pattern ~ '^\\.[A-Za-z]\\.[0-9]+$'",
            name="ck_instrument_definitions_continuous_pattern_shape",
        ),
        sa.UniqueConstraint(
            "raw_symbol",
            "provider",
            "asset_class",
            name="uq_instrument_definitions_symbol_provider_asset",
        ),
    )
    op.create_index(
        "ix_instrument_definitions_raw_symbol",
        "instrument_definitions",
        ["raw_symbol"],
    )
    op.create_index(
        "ix_instrument_definitions_listing_venue",
        "instrument_definitions",
        ["listing_venue"],
    )
    op.create_index(
        "ix_instrument_definitions_routing_venue",
        "instrument_definitions",
        ["routing_venue"],
    )

    op.create_table(
        "instrument_aliases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "instrument_uid",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "instrument_definitions.instrument_uid", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("alias_string", sa.String(100), nullable=False),
        sa.Column("venue_format", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "venue_format IN ('exchange_name','mic_code','databento_continuous')",
            name="ck_instrument_aliases_venue_format",
        ),
        sa.UniqueConstraint(
            "alias_string",
            "provider",
            "effective_from",
            name="uq_instrument_aliases_string_provider_from",
        ),
    )
    op.create_index(
        "ix_instrument_aliases_uid", "instrument_aliases", ["instrument_uid"]
    )
    op.create_index(
        "ix_instrument_aliases_alias_string",
        "instrument_aliases",
        ["alias_string"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instrument_aliases_alias_string", table_name="instrument_aliases"
    )
    op.drop_index("ix_instrument_aliases_uid", table_name="instrument_aliases")
    op.drop_table("instrument_aliases")
    op.drop_index(
        "ix_instrument_definitions_routing_venue",
        table_name="instrument_definitions",
    )
    op.drop_index(
        "ix_instrument_definitions_listing_venue",
        table_name="instrument_definitions",
    )
    op.drop_index(
        "ix_instrument_definitions_raw_symbol",
        table_name="instrument_definitions",
    )
    op.drop_table("instrument_definitions")
```

**Note on seeds:** per PRD §47-48 ("lazy, empty at ship, populate on `/live/start` or ingest"), NO seed rows in this migration. Operators run `msai instruments refresh --symbols ES.Z.5,NQ.Z.5,...` after deployment (Task 13).

**Step 3: Upgrade + downgrade round-trip**

```bash
cd claude-version/backend
uv run alembic upgrade head
uv run alembic downgrade u9p0q1r2s3t4
uv run alembic upgrade head
```

Expected: all three succeed with no errors.

**Step 4: Commit**

```bash
git add claude-version/backend/alembic/versions/v0q1r2s3t4u5_instrument_registry.py
git commit -m "feat(registry): Alembic migration for instrument_definitions + instrument_aliases"
```

---

### Task 2: SQLAlchemy models for `InstrumentDefinition` + `InstrumentAlias`

**Files:**

- Create: `claude-version/backend/src/msai/models/instrument_definition.py`
- Create: `claude-version/backend/src/msai/models/instrument_alias.py`
- Modify: `claude-version/backend/src/msai/models/__init__.py` (re-export)

**Step 1: Write failing unit test**

Create `claude-version/backend/tests/unit/test_instrument_definition_model.py`:

```python
from __future__ import annotations

import uuid
from datetime import date

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition


def test_instrument_definition_accepts_basic_row():
    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="NASDAQ",
        asset_class="equity",
        provider="interactive_brokers",
    )
    assert idef.instrument_uid is None or isinstance(idef.instrument_uid, uuid.UUID)


def test_instrument_alias_accepts_basic_row():
    uid = uuid.uuid4()
    alias = InstrumentAlias(
        instrument_uid=uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
    )
    assert alias.alias_string == "AAPL.NASDAQ"
```

**Step 2: Run failing test**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_instrument_definition_model.py -v`
Expected: FAIL with `ModuleNotFoundError`.

**Step 3: Write `InstrumentDefinition` model**

Create `claude-version/backend/src/msai/models/instrument_definition.py`:

```python
"""Control-plane definition for a tradable instrument.

Primary key is a stable logical UUID — NEVER a venue-qualified string.
Venue-qualified Nautilus ``InstrumentId`` strings live in
:class:`InstrumentAlias`, so a ticker change, listing-venue move, or
future MIC revision is a row update, not a PK migration.

See ``docs/prds/db-backed-strategy-registry.md`` §6 for the full
schema rationale.

**Coexistence note (2026-04-17):** the existing ``InstrumentCache``
model / table (``instrument_cache``) is NOT migrated in this PR. Follow-up
PR ``docs/plans/2026-04-XX-instrument-cache-migration.md`` handles that
once Nautilus-native cache durability (``CacheConfig(database=redis)``)
has proven out through a restart cycle in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.instrument_alias import InstrumentAlias


class InstrumentDefinition(Base):
    __tablename__ = "instrument_definitions"

    __table_args__ = (
        CheckConstraint(
            "asset_class IN ('equity','futures','fx','option','crypto')",
            name="ck_instrument_definitions_asset_class",
        ),
        CheckConstraint(
            "lifecycle_state IN ('staged','active','retired')",
            name="ck_instrument_definitions_lifecycle_state",
        ),
        CheckConstraint(
            r"continuous_pattern IS NULL OR continuous_pattern ~ '^\.[A-Za-z]\.[0-9]+$'",
            name="ck_instrument_definitions_continuous_pattern_shape",
        ),
        UniqueConstraint(
            "raw_symbol",
            "provider",
            "asset_class",
            name="uq_instrument_definitions_symbol_provider_asset",
        ),
    )

    instrument_uid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    raw_symbol: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    listing_venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    routing_venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    roll_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continuous_pattern: Mapped[str | None] = mapped_column(String(32), nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="staged"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    aliases: Mapped[list["InstrumentAlias"]] = relationship(
        "InstrumentAlias",
        back_populates="definition",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
```

**Step 4: Write `InstrumentAlias` model**

Create `claude-version/backend/src/msai/models/instrument_alias.py`:

```python
"""Venue-qualified alias for an :class:`InstrumentDefinition`.

One definition can have many aliases — one per provider per
``effective_from`` date. Futures front-month rolls close the
expiring contract's alias row (setting ``effective_to``) and
insert a new alias for the next front month.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.instrument_definition import InstrumentDefinition


class InstrumentAlias(Base):
    __tablename__ = "instrument_aliases"

    __table_args__ = (
        CheckConstraint(
            "venue_format IN ('exchange_name','mic_code','databento_continuous')",
            name="ck_instrument_aliases_venue_format",
        ),
        UniqueConstraint(
            "alias_string",
            "provider",
            "effective_from",
            name="uq_instrument_aliases_string_provider_from",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instrument_uid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "instrument_definitions.instrument_uid", ondelete="CASCADE"
        ),
        nullable=False,
        index=True,
    )
    alias_string: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    venue_format: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    definition: Mapped["InstrumentDefinition"] = relationship(
        "InstrumentDefinition", back_populates="aliases"
    )
```

**Step 5: Re-export from `msai.models`**

Modify `claude-version/backend/src/msai/models/__init__.py` — append imports and add to `__all__` in alphabetical position:

```python
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
```

**Step 6: Run test — verify pass**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_instrument_definition_model.py -v`
Expected: PASS (2 tests).

**Step 7: Commit**

```bash
git add claude-version/backend/src/msai/models/instrument_definition.py \
        claude-version/backend/src/msai/models/instrument_alias.py \
        claude-version/backend/src/msai/models/__init__.py \
        claude-version/backend/tests/unit/test_instrument_definition_model.py
git commit -m "feat(registry): SQLAlchemy models for InstrumentDefinition + InstrumentAlias"
```

---

### Task 3: Integration test — CRUD + cascade-delete + constraint validation

**Files:**

- Create: `claude-version/backend/tests/integration/test_instrument_definition_crud.py`

**Fixture convention:** use the `session_factory` + per-test `session` pattern from `tests/integration/test_instrument_cache_model.py:26-45` — do NOT assume a shared `db_session` fixture exists.

**Step 1: Write failing integration test**

```python
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition

# `session_factory` fixture comes from tests/integration/conftest.py


@pytest.mark.asyncio
async def test_crud_roundtrip_with_cascade_delete(session_factory) -> None:
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="interactive_brokers",
            roll_policy="third_friday_quarterly",
        )
        session.add(idef)
        await session.flush()
        uid = idef.instrument_uid
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 17),
            )
        )
        await session.commit()

    async with session_factory() as session:
        idef = await session.get(InstrumentDefinition, uid)
        await session.delete(idef)
        await session.commit()

    async with session_factory() as session:
        aliases = (
            await session.execute(
                select(InstrumentAlias).where(InstrumentAlias.instrument_uid == uid)
            )
        ).scalars().all()
        assert aliases == []


@pytest.mark.asyncio
async def test_unique_alias_per_provider_per_effective_from(
    session_factory,
) -> None:
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="interactive_brokers",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_asset_class_check_rejects_invalid(session_factory) -> None:
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="X",
                listing_venue="Y",
                routing_venue="Y",
                asset_class="bond",
                provider="interactive_brokers",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_continuous_pattern_check_rejects_invalid_shape(
    session_factory,
) -> None:
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="ES",
                listing_venue="CME",
                routing_venue="CME",
                asset_class="futures",
                provider="databento",
                continuous_pattern=".Z.5",
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="NQ",
                listing_venue="CME",
                routing_venue="CME",
                asset_class="futures",
                provider="databento",
                continuous_pattern="Z5",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
```

**Step 2: Run failing test**

Run: `cd claude-version/backend && uv run pytest tests/integration/test_instrument_definition_crud.py -v`
Expected: tests fail initially if the test DB hasn't had `alembic upgrade head` run. If they pass immediately, the constraints are working as designed — the test was "failing" at the name level (didn't exist) until T1+T2 landed. **This is acceptable for a green-on-first-run schema test since the implementation is the migration + model, already committed in T1+T2.**

**Step 3: Commit**

```bash
git add claude-version/backend/tests/integration/test_instrument_definition_crud.py
git commit -m "test(registry): integration tests for InstrumentDefinition CRUD + constraints"
```

---

## Phase 2: Registry Lookup Layer

### Task 4: `InstrumentRegistry` with alias lookup + raw-symbol lookup

**Files:**

- Create: `claude-version/backend/src/msai/services/nautilus/security_master/registry.py`
- Test: `claude-version/backend/tests/integration/test_instrument_registry.py`

**API surface (per PRD §97-98):**

- `find_by_alias(alias_string, *, provider, as_of_date=None) -> InstrumentDefinition | None` — respects `effective_from <= as_of_date < effective_to` window.
- `find_by_raw_symbol(raw_symbol, *, provider, asset_class=None) -> InstrumentDefinition | None` — returns `None` on miss. Schema uniqueness `(raw_symbol, provider, asset_class)` means callers MUST specify `provider` — cross-provider dual-listings are supported by design (same raw symbol listed under both `interactive_brokers` and `databento` as distinct rows).
- `require_definition(...)` — fail-loud variant.

**Step 1: Write failing test**

```python
from __future__ import annotations

from datetime import date

import pytest

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.registry import (
    InstrumentRegistry,
    RegistryDefinitionNotFoundError,
)


@pytest.mark.asyncio
async def test_find_by_alias_honors_as_of_date(session_factory) -> None:
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="interactive_brokers",
        )
        session.add(idef)
        await session.flush()
        # Expired March contract
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESH6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2025, 12, 19),
                effective_to=date(2026, 3, 18),
            )
        )
        # Current June contract
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 18),
            )
        )
        await session.commit()

        registry = InstrumentRegistry(session)
        # As-of mid-Feb: March contract is active
        result_feb = await registry.find_by_alias(
            "ESH6.CME", provider="interactive_brokers", as_of_date=date(2026, 2, 15)
        )
        assert result_feb is not None
        # As-of mid-April: March contract is expired
        result_apr = await registry.find_by_alias(
            "ESH6.CME", provider="interactive_brokers", as_of_date=date(2026, 4, 15)
        )
        assert result_apr is None


@pytest.mark.asyncio
async def test_find_by_raw_symbol_requires_provider(session_factory) -> None:
    """Schema uniqueness is (raw_symbol, provider, asset_class). Cross-provider
    ambiguity is by design — callers must specify provider. This test proves
    both providers coexist as distinct rows and are retrievable independently."""
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="XYZ",
                listing_venue="NASDAQ",
                routing_venue="NASDAQ",
                asset_class="equity",
                provider="interactive_brokers",
            )
        )
        session.add(
            InstrumentDefinition(
                raw_symbol="XYZ",
                listing_venue="NASDAQ",
                routing_venue="NASDAQ",
                asset_class="equity",
                provider="databento",
            )
        )
        await session.commit()

        registry = InstrumentRegistry(session)
        ib_row = await registry.find_by_raw_symbol("XYZ", provider="interactive_brokers")
        db_row = await registry.find_by_raw_symbol("XYZ", provider="databento")
        assert ib_row is not None
        assert db_row is not None
        assert ib_row.provider == "interactive_brokers"
        assert db_row.provider == "databento"


@pytest.mark.asyncio
async def test_require_definition_raises_on_miss(session_factory) -> None:
    async with session_factory() as session:
        registry = InstrumentRegistry(session)
        with pytest.raises(RegistryDefinitionNotFoundError):
            await registry.require_definition(
                "ZZZZ.NASDAQ", provider="interactive_brokers"
            )
```

**Step 2: Run failing test**

Run: `cd claude-version/backend && uv run pytest tests/integration/test_instrument_registry.py -v`
Expected: FAIL — module missing.

**Step 3: Write registry**

Create `claude-version/backend/src/msai/services/nautilus/security_master/registry.py`:

```python
"""Async lookup layer over ``instrument_definitions`` + ``instrument_aliases``.

Owns: alias → definition resolution, raw_symbol → definition lookup,
effective-date window management for futures rolls, ambiguity detection
for dual-listings (PRD §97-98).

The strategy hot path does NOT touch this module — pre-warm happens at
``/live/start-portfolio`` / ``backtests/run``. Hot-path access is Nautilus's
own ``cache.instrument(instrument_id)`` sync dict lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition


class RegistryDefinitionNotFoundError(Exception):
    """Raised when a requested symbol has no matching registry row."""


@dataclass
class InstrumentRegistry:
    session: AsyncSession

    async def find_by_alias(
        self,
        alias_string: str,
        *,
        provider: str,
        as_of_date: date | None = None,
    ) -> InstrumentDefinition | None:
        """Return the definition whose alias is active on ``as_of_date``.

        Default ``as_of_date`` = today UTC. Windows are ``effective_from <= as_of < effective_to``
        (or ``effective_to IS NULL`` for the open-ended current alias).
        """
        as_of = as_of_date or datetime.now(timezone.utc).date()
        stmt = (
            select(InstrumentDefinition)
            .join(InstrumentAlias, InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid)
            .where(
                InstrumentAlias.alias_string == alias_string,
                InstrumentAlias.provider == provider,
                InstrumentAlias.effective_from <= as_of,
                or_(
                    InstrumentAlias.effective_to.is_(None),
                    InstrumentAlias.effective_to > as_of,
                ),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_raw_symbol(
        self,
        raw_symbol: str,
        *,
        provider: str,
        asset_class: str | None = None,
    ) -> InstrumentDefinition | None:
        """Return the definition for ``raw_symbol`` under ``provider`` (and
        optional ``asset_class``). Returns ``None`` on miss. Callers MUST
        specify ``provider`` — cross-provider dual-listings are by design
        (schema uniqueness is ``(raw_symbol, provider, asset_class)``)."""
        stmt = select(InstrumentDefinition).where(
            InstrumentDefinition.raw_symbol == raw_symbol,
            InstrumentDefinition.provider == provider,
        )
        if asset_class is not None:
            stmt = stmt.where(InstrumentDefinition.asset_class == asset_class)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def require_definition(
        self, alias_string: str, *, provider: str, as_of_date: date | None = None
    ) -> InstrumentDefinition:
        idef = await self.find_by_alias(
            alias_string, provider=provider, as_of_date=as_of_date
        )
        if idef is None:
            raise RegistryDefinitionNotFoundError(
                f"No registry row for alias {alias_string!r} under provider {provider!r}"
                + (f" as of {as_of_date}" if as_of_date else "")
            )
        return idef
```

**Step 4: Run tests — verify pass**

Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/security_master/registry.py \
        claude-version/backend/tests/integration/test_instrument_registry.py
git commit -m "feat(registry): InstrumentRegistry with effective-date windowing + ambiguity detection"
```

---

## Phase 3: Databento `.Z.N` Continuous-Futures Helpers

**Ordering note:** this phase runs BEFORE Phase 4 (SecurityMaster resolve extensions) because `resolve_for_backtest` depends on `is_databento_continuous_pattern` from T5 and `resolve_databento_continuous` building blocks from T6–T7.

### Task 5: Port `is_databento_continuous_pattern` + `raw_symbol_from_request`

**Source (verified):** `codex-version/backend/src/msai/services/nautilus/instrument_service.py:440-451`.

**Files:**

- Create: `claude-version/backend/src/msai/services/nautilus/security_master/continuous_futures.py`
- Test: `claude-version/backend/tests/unit/test_continuous_futures_helpers.py`

**Step 1: Write failing test**

```python
import pytest

from msai.services.nautilus.security_master.continuous_futures import (
    is_databento_continuous_pattern,
    raw_symbol_from_request,
)


@pytest.mark.parametrize("pattern", ["ES.Z.5", "NQ.Z.0", "RTY.c.2", "6E.H.1"])
def test_continuous_matches_valid(pattern):
    assert is_databento_continuous_pattern(pattern) is True


@pytest.mark.parametrize("pattern", ["ES", "AAPL.NASDAQ", "ESM6.CME", "ES.Z", "ES..5"])
def test_continuous_rejects_invalid(pattern):
    assert is_databento_continuous_pattern(pattern) is False


def test_raw_symbol_preserves_continuous():
    assert raw_symbol_from_request("ES.Z.5") == "ES.Z.5"


def test_raw_symbol_strips_concrete_venue():
    assert raw_symbol_from_request("AAPL.NASDAQ") == "AAPL"


def test_raw_symbol_passes_bare():
    assert raw_symbol_from_request("AAPL") == "AAPL"


def test_raw_symbol_rejects_empty():
    with pytest.raises(ValueError):
        raw_symbol_from_request("")
```

**Step 2: Run failing test**

Expected: FAIL — module missing.

**Step 3: Write the helper module**

```python
"""Databento continuous-futures symbology helpers.

Adapted from codex-version ``instrument_service.py:440-451``. The
Databento Python adapter in Nautilus 1.223.0 has no native continuous-
symbol normalization (verified: zero grep hits for
``continuous|\\.c\\.0|\\.Z\\.`` in ``nautilus_trader/adapters/databento/``),
so MSAI fills the gap.

Pattern: ``{root}.{c|Z}.{N}`` — e.g. ``ES.Z.5`` = ES continuous, 5th
forward-month.
"""

from __future__ import annotations

import re

from nautilus_trader.model.identifiers import InstrumentId

_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def is_databento_continuous_pattern(value: str) -> bool:
    return bool(_DATABENTO_CONTINUOUS_SYMBOL.match(value))


def raw_symbol_from_request(requested: str) -> str:
    value = requested.strip()
    if not value:
        raise ValueError("Instrument ID cannot be empty")
    if is_databento_continuous_pattern(value):
        return value
    if "." in value:
        return InstrumentId.from_str(value).symbol.value
    return value
```

**Step 4: Run test — verify pass**

Expected: PASS (9 tests).

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/security_master/continuous_futures.py \
        claude-version/backend/tests/unit/test_continuous_futures_helpers.py
git commit -m "feat(registry): port Databento .Z.N continuous regex + raw_symbol helper"
```

---

### Task 6: Port `resolved_databento_definition` + window helpers + `ResolvedInstrumentDefinition` dataclass

**Source (verified):** `codex-version/backend/src/msai/services/nautilus/instrument_service.py:32-59` (dataclass — 27 lines), `466-539` (synthesis function), `571-605` (window helpers).

**Adaptation note — dataclass shape:** codex's `ResolvedInstrumentDefinition` has `(instrument_id, raw_symbol, venue, instrument_type, security_type, asset_class, instrument_data: dict, contract_details: dict | None, provider)`. MSAI's PRD replaces single-venue with `listing_venue`/`routing_venue`, drops `instrument_data` JSONB (Nautilus cache owns it), and retains `contract_details` as a transport-only dict used during synthesis then discarded. Final shape below.

**Adaptation note — venue canonical:** per PRD exchange-name canonical, the synthetic continuous instrument_id should be `ES.Z.5.CME` (not `.GLBX`). Databento's `from_dbn_file(..., use_exchange_as_venue=True)` at Task 7 ensures instruments arrive with exchange-name venues already — the synthesis preserves whatever venue the underlying instruments carry.

**Files:**

- Modify: `claude-version/backend/src/msai/services/nautilus/security_master/continuous_futures.py` (append)
- Test: `claude-version/backend/tests/unit/test_continuous_futures_synthesis.py`

**Step 1: Write failing test**

```python
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from msai.services.nautilus.security_master.continuous_futures import (
    ResolvedInstrumentDefinition,
    continuous_needs_refresh_for_window,
    definition_window_bounds_from_details,
    resolved_databento_definition,
)


def _mock_futures_instrument(raw_symbol: str, venue: str, activation_ns: int, expiration_ns: int):
    """Build a mock Nautilus FuturesContract stand-in. Full Instrument
    instantiation requires all ~15 mandatory fields; a MagicMock is fine
    for testing the synthesis logic."""
    inst = MagicMock()
    inst.raw_symbol.value = raw_symbol
    inst.id.venue.value = venue
    # Accessed by synthesis via msgspec.to_builtins(inst) — patch that call
    # in the actual test if/as needed.
    return inst


def test_resolved_databento_definition_synthesizes_continuous_on_cme(
    monkeypatch,
):
    # Arrange — a single June-2026 ES contract loaded from Databento with
    # use_exchange_as_venue=True so the venue is "CME".
    mock_inst = _mock_futures_instrument("ESM6", "CME", 1000, 9000)
    payload_returned = {
        "type": "FuturesContract",
        "id": "ESM6.CME",
        "raw_symbol": "ESM6",
        "ts_init": 12345,
        "activation_ns": 1000,
        "expiration_ns": 9000,
    }
    # patch instrument_to_payload to return payload_returned (see
    # codex-version instrument_service.py for the helper's location)
    monkeypatch.setattr(
        "msai.services.nautilus.security_master.continuous_futures.instrument_to_payload",
        lambda _: dict(payload_returned),
    )

    resolved = resolved_databento_definition(
        raw_symbol="ES.Z.5",
        instruments=[mock_inst],
        dataset="GLBX.MDP3",
        start="2024-01-01",
        end="2024-12-31",
        definition_path="/tmp/fake.definition.dbn.zst",
    )
    # Synthetic ID preserves .Z.N + the underlying venue
    assert resolved.instrument_id == "ES.Z.5.CME"
    assert resolved.raw_symbol == "ES.Z.5"
    assert resolved.listing_venue == "CME"
    assert resolved.routing_venue == "CME"
    assert resolved.provider == "databento"
    assert resolved.contract_details["requested_symbol"] == "ES.Z.5"


def test_definition_window_bounds_extracts_from_contract_details():
    bounds = definition_window_bounds_from_details({
        "definition_start": "2024-01-01",
        "definition_end": "2024-12-31",
    })
    assert bounds == ("2024-01-01", "2024-12-31")


def test_continuous_needs_refresh_when_window_expands():
    # Cached window [2024-01-01, 2024-12-31]; request 2024-01-01..2025-06-30
    needs = continuous_needs_refresh_for_window(
        cached_start="2024-01-01",
        cached_end="2024-12-31",
        requested_start="2024-01-01",
        requested_end="2025-06-30",
    )
    assert needs is True


def test_continuous_no_refresh_when_window_covered():
    needs = continuous_needs_refresh_for_window(
        cached_start="2024-01-01",
        cached_end="2024-12-31",
        requested_start="2024-03-01",
        requested_end="2024-06-30",
    )
    assert needs is False
```

**Step 2: Run failing test** → expected FAIL (symbols undefined).

**Step 3: Port synthesis + dataclass + helpers**

Append to `continuous_futures.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nautilus_trader.model.instruments import Instrument

# instrument_to_payload — reuse the existing helper at
# security_master/parser.py:171 (`nautilus_instrument_to_cache_json`) via an
# import alias. No new module needed.
from msai.services.nautilus.security_master.parser import (
    nautilus_instrument_to_cache_json as instrument_to_payload,
)


@dataclass(frozen=True, slots=True)
class ResolvedInstrumentDefinition:
    """Transport object between ``resolved_databento_definition`` and the
    caller (``SecurityMaster.resolve_for_backtest``).

    Diverges from codex-version: MSAI uses ``listing_venue``/``routing_venue``
    (per PRD). ``instrument_data`` is NOT carried — Nautilus's cache DB
    holds payloads. ``contract_details`` is a transport-only dict used
    during synthesis.
    """

    instrument_id: str
    raw_symbol: str
    listing_venue: str
    routing_venue: str
    asset_class: str
    provider: str
    contract_details: dict[str, Any]


def resolved_databento_definition(
    *,
    raw_symbol: str,
    instruments: list[Instrument],
    dataset: str,
    start: str,
    end: str,
    definition_path: str | Path,
) -> ResolvedInstrumentDefinition:
    """Build a synthetic continuous-futures ``ResolvedInstrumentDefinition``
    from a Databento-loaded set of concrete-month instruments.

    Adapted from codex ``instrument_service.py:466-539``. Picks the
    instrument with the latest ``ts_init``/``ts_event`` as the representative.
    """
    matching = [
        inst for inst in instruments
        if inst.raw_symbol.value == raw_symbol
    ]
    if not matching and is_databento_continuous_pattern(raw_symbol):
        matching = instruments
    if not matching:
        raise ValueError(
            f"Databento definition data for {raw_symbol!r} did not decode into a Nautilus instrument"
        )

    selected = max(
        matching,
        key=lambda inst: str(
            instrument_to_payload(inst).get("ts_init")
            or instrument_to_payload(inst).get("ts_event")
            or ""
        ),
    )
    payload = instrument_to_payload(selected)
    venue = selected.id.venue.value

    # For continuous patterns, rewrite the ID to the synthetic form
    if is_databento_continuous_pattern(raw_symbol):
        synthetic_id = f"{raw_symbol}.{venue}"
        requested_symbol_for_details = raw_symbol
    else:
        synthetic_id = str(selected.id)
        requested_symbol_for_details = None

    instrument_type = str(payload.get("type", type(selected).__name__))

    return ResolvedInstrumentDefinition(
        instrument_id=synthetic_id,
        raw_symbol=raw_symbol,
        listing_venue=venue,
        routing_venue=venue,
        asset_class=_asset_class_for_instrument_type(instrument_type),
        provider="databento",
        contract_details={
            "dataset": dataset,
            "schema": "definition",
            "definition_start": start,
            "definition_end": end,
            "definition_file_path": str(definition_path),
            "requested_symbol": requested_symbol_for_details or raw_symbol,
            "underlying_instrument_id": str(selected.id),
            "underlying_raw_symbol": selected.raw_symbol.value,
        },
    )


def _asset_class_for_instrument_type(instrument_type: str) -> str:
    if instrument_type in {"FuturesContract", "FuturesSpread"}:
        return "futures"
    if instrument_type in {"OptionContract", "OptionSpread"}:
        return "option"
    if instrument_type == "CurrencyPair":
        return "fx"
    if instrument_type in {"CryptoFuture", "CryptoOption", "CryptoPerpetual", "PerpetualContract"}:
        return "crypto"
    return "equity"


def definition_window_bounds_from_details(
    details: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not isinstance(details, dict):
        return (None, None)
    s = details.get("definition_start")
    e = details.get("definition_end")
    if not isinstance(s, str) or not isinstance(e, str):
        return (None, None)
    return (s, e)


def continuous_needs_refresh_for_window(
    *,
    cached_start: str | None,
    cached_end: str | None,
    requested_start: str,
    requested_end: str,
) -> bool:
    if cached_start is None or cached_end is None:
        return True
    return requested_start < cached_start or requested_end > cached_end
```

**Step 4: Run test** → expected PASS (4 tests).

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/security_master/continuous_futures.py \
        claude-version/backend/tests/unit/test_continuous_futures_synthesis.py
git commit -m "feat(registry): port Databento continuous synthesis + ResolvedInstrumentDefinition dataclass"
```

---

### Task 7: Port `DatabentoClient.fetch_definition_instruments`

**Source (verified):** `codex-version/backend/src/msai/services/data_sources/databento_client.py:63-100`. Signature: `fetch_definition_instruments(self, symbol: str, start: str, end: str, *, dataset: str, target_path: Path) -> list[Instrument]`.

**Files:**

- Modify: `claude-version/backend/src/msai/services/data_sources/databento_client.py` (add method)
- Test: `claude-version/backend/tests/unit/test_databento_fetch_definition.py`

**Step 1: Confirm method doesn't already exist**

Run: `grep -n "fetch_definition" claude-version/backend/src/msai/services/data_sources/databento_client.py`
Expected: no matches.

**Step 2: Write failing test**

Test focuses on: (a) method exists with the right signature, (b) it calls `DatabentoDataLoader().from_dbn_file(path, use_exchange_as_venue=True)` — the per-call kwarg placement, (c) it creates `target_path.parent` if missing.

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from msai.services.data_sources.databento_client import DatabentoClient


@pytest.mark.asyncio
async def test_fetch_definition_instruments_calls_from_dbn_file_with_use_exchange_as_venue(
    tmp_path,
):
    mock_loader = MagicMock()
    mock_loader.from_dbn_file.return_value = iter([])
    client = DatabentoClient(api_key="test")
    target = tmp_path / "GLBX.MDP3" / "ES" / "x.definition.dbn.zst"
    with patch(
        "msai.services.data_sources.databento_client.DatabentoDataLoader",
        return_value=mock_loader,
    ):
        # Also patch whatever method pulls the file; e.g. bento_client.timeseries.get_range or similar
        # codex-version already has a download helper — reuse pattern
        ...
    mock_loader.from_dbn_file.assert_called_once()
    _, kwargs = mock_loader.from_dbn_file.call_args
    assert kwargs.get("use_exchange_as_venue") is True
```

**Step 3: Port the method**

Read `codex-version/backend/src/msai/services/data_sources/databento_client.py:63-100`, port verbatim with **two adaptations**:

1. The call site must use `loader.from_dbn_file(target_path, use_exchange_as_venue=True)` — per-call kwarg on `from_dbn_file`, **not** on the `DatabentoDataLoader()` constructor.
   ```python
   # use_exchange_as_venue=True on from_dbn_file() is the per-call kwarg per
   # Nautilus adapters/databento/loaders.py:119-128,154-156 — ensures CME
   # futures emit venue="CME" not "GLBX".
   instruments = loader.from_dbn_file(target_path, use_exchange_as_venue=True)
   ```
2. Parameter name: the codex signature is `symbol` (first positional) — preserve it. Do NOT rename to `raw_symbol`.

**v2.1 note — absorbed old Task 11:** v2.0 had a separate Task 11 that proposed updating all existing `from_dbn_file()` call sites to pass `use_exchange_as_venue=True`. A `grep -rn "from_dbn_file" claude-version/backend/src/` returns zero matches — the call site introduced here is the only one. Old T11 is merged into this task and the kwarg lives here where the only call site lives.

**Step 4: Run test — verify pass**

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/data_sources/databento_client.py \
        claude-version/backend/tests/unit/test_databento_fetch_definition.py
git commit -m "feat(registry): port DatabentoClient.fetch_definition_instruments with use_exchange_as_venue"
```

---

## Phase 4: SecurityMaster async resolve extensions

**Ordering note:** these tasks come AFTER Phase 3 so `resolve_for_backtest` can import `is_databento_continuous_pattern`, `resolved_databento_definition`, etc.

### Task 8: Extend `SecurityMaster` with `resolve_for_live(symbols)` method

**Files:**

- Modify: `claude-version/backend/src/msai/services/nautilus/security_master/service.py`
- Test: `claude-version/backend/tests/integration/test_security_master_resolve_live.py`

**Ctor change (iter-3 P0 fix):** relax `SecurityMaster.__init__` to make `qualifier` optional and accept an optional `databento_client`. This lets both backtest (no IB) and live (with IB) callers share one ctor without a separate `for_backtest()` factory. Any code path that actually needs the IB qualifier raises if `self._qualifier is None`.

- New signature: `SecurityMaster.__init__(*, qualifier: IBQualifier | None = None, db: AsyncSession, cache_validity_days: int = DEFAULT_CACHE_VALIDITY_DAYS, databento_client: DatabentoClient | None = None)`
- Stores `self._qualifier`, `self._db`, `self._databento`.
- Do NOT rename `qualifier` → `ib_qualifier`, or `db` → `session`.
- New instance attribute: `self._registry = InstrumentRegistry(self._db)` (created in `__init__`).
- `self._databento` is stored for `_resolve_databento_continuous` (Task 9). `None` is permitted; a continuous `.Z.N` symbol arriving at `resolve_for_backtest` with `self._databento is None` raises `ValueError("DatabentoClient required for continuous-futures resolution — construct SecurityMaster with databento_client=...").`

**Import added to `service.py`:**

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from msai.services.data_sources.databento_client import DatabentoClient
```

**Critical constraint — `IBQualifier.qualify()` signature:**

- Actual: `async def qualify(self, spec: InstrumentSpec) -> Instrument`. Single return, not a tuple.
- To get `primaryExchange`: after calling `qualify`, read `self._qualifier._provider.contract_details[instrument.id]` — a Nautilus adapter internal dict that maps `InstrumentId` → `IBContractDetails`. The `IBContractDetails` has `contract.primaryExchange`.
- Reference: `.venv/lib/.../adapters/interactive_brokers/providers.py:93` and codebase usage at `security_master/service.py:252-285` (existing `_trading_hours_for`).

**New imports required (added to `service.py`):**

```python
from msai.services.nautilus.live_instrument_bootstrap import canonical_instrument_id
```

Reason: v2.1 cold-miss path delegates to `canonical_instrument_id()` (the existing closed-universe front-month resolver) + `self.resolve(spec)` (the existing cache-first resolve), instead of calling `self._qualifier.qualify(...)` with a constructed `InstrumentSpec` directly. This reuses the already-tested resolution pipeline.

**Step 1: Write failing test**

```python
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.service import SecurityMaster


@pytest.mark.asyncio
async def test_resolve_for_live_warm_hit_does_not_call_ib(session_factory) -> None:
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="interactive_brokers",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock()
        sm = SecurityMaster(qualifier=mock_qualifier, db=session)

        ids = await sm.resolve_for_live(["AAPL"])

        assert ids == ["AAPL.NASDAQ"]
        mock_qualifier.qualify.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_for_live_cold_miss_calls_ib_and_upserts(
    session_factory,
) -> None:
    async with session_factory() as session:
        mock_qualifier = MagicMock()
        fake_instrument = MagicMock()
        fake_instrument.id = MagicMock()
        fake_instrument.id.__str__ = MagicMock(return_value="MSFT.NASDAQ")
        fake_instrument.id.venue.value = "NASDAQ"
        fake_instrument.raw_symbol.value = "MSFT"
        mock_qualifier.qualify = AsyncMock(return_value=fake_instrument)

        mock_provider = MagicMock()
        fake_details = MagicMock()
        fake_details.contract.primaryExchange = "NASDAQ"
        mock_provider.contract_details = {fake_instrument.id: fake_details}
        mock_qualifier._provider = mock_provider

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)
        ids = await sm.resolve_for_live(["MSFT"])

        assert ids == ["MSFT.NASDAQ"]
        mock_qualifier.qualify.assert_awaited_once()
```

**Step 2: Run failing test** → expected FAIL (`resolve_for_live` missing).

**Step 3: Add `resolve_for_live` + private `_upsert_from_ib` to `SecurityMaster`**

```python
    async def resolve_for_live(self, symbols: list[str]) -> list[str]:
        """Return canonical Nautilus ``InstrumentId`` strings for ``symbols``.

        Warm path: registry hit by alias OR raw_symbol → return the active alias.
        Cold path: IB qualify → upsert definition + alias → return.

        Non-hot-path; uses ``self._db`` + optional IB qualify round-trips.
        Callers must pre-warm before ``TradingNode.run()`` (gotchas #9, #11).

        Cold-miss scope (v2.2 — explicit):
            The cold-miss path currently delegates to the Phase-1 closed-universe
            ``canonical_instrument_id`` helper at
            ``live_instrument_bootstrap.py:123-170``. Symbols outside
            ``{AAPL, MSFT, SPY, EUR/USD, ES}`` will raise ``ValueError`` from
            that helper. To add a new symbol:
            (1) extend ``canonical_instrument_id``'s if-chain at
                ``live_instrument_bootstrap.py:123-170``;
            (2) extend ``_spec_from_canonical`` (below) with the new venue case;
            (3) pre-warm the registry via
                ``msai instruments refresh --symbols <NEW>`` so future hits go
                down the warm path instead of the cold path.
        """
        from datetime import datetime, timezone

        from msai.services.nautilus.live_instrument_bootstrap import (
            canonical_instrument_id,
        )
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        today = datetime.now(timezone.utc).date()
        out: list[str] = []
        for sym in symbols:
            # Caller passed an already-qualified string
            if "." in sym:
                idef = await registry.find_by_alias(
                    sym, provider="interactive_brokers"
                )
                if idef is not None:
                    out.append(sym)
                    continue
            # Caller passed a bare ticker
            idef = await registry.find_by_raw_symbol(
                sym, provider="interactive_brokers"
            )
            if idef is not None:
                active_alias = next(
                    (a for a in idef.aliases if a.effective_to is None), None
                )
                if active_alias is not None:
                    out.append(active_alias.alias_string)
                    continue
            # Cold miss — delegate to existing live_instrument_bootstrap
            # front-month rollover + existing SecurityMaster.resolve(spec).
            # Reason: live_instrument_bootstrap.canonical_instrument_id(...)
            # holds the closed-universe roll logic (ES -> ESM6.CME at spawn
            # today); we reuse it rather than reinventing. The returned
            # canonical alias string is then used to build an InstrumentSpec
            # via _spec_from_canonical() (new helper, Task 8 Step 3b below)
            # and the spec is resolved through the existing cache-first path.
            if self._qualifier is None:
                raise ValueError(
                    f"Cold-miss resolve for {sym!r} requires an IBQualifier — "
                    f"construct SecurityMaster with qualifier=... for live use."
                )
            canonical = canonical_instrument_id(sym, today=today)
            spec = self._spec_from_canonical(canonical)
            instrument = await self.resolve(spec)  # existing method — cache-first
            alias_str = str(instrument.id)
            routing_venue = instrument.id.venue.value
            listing_venue = routing_venue
            details = self._qualifier._provider.contract_details.get(instrument.id)
            if details is not None and details.contract is not None:
                primary = getattr(details.contract, "primaryExchange", None) or None
                if primary:
                    listing_venue = primary
            await self._upsert_definition_and_alias(
                raw_symbol=instrument.raw_symbol.value,
                listing_venue=listing_venue,
                routing_venue=routing_venue,
                asset_class=self._asset_class_for_instrument(instrument),
                alias_string=alias_str,
            )
            out.append(alias_str)
        return out

    @staticmethod
    def _asset_class_for_instrument(instrument: Any) -> str:
        """Derive the registry's ``asset_class`` column from a Nautilus
        :class:`Instrument` via its runtime class name. Mirrors codex's
        ``_asset_class_for_security_type`` at
        ``codex-version/backend/src/msai/services/nautilus/instrument_service.py:428-437``
        but keys on the Nautilus Python class name (which we already have
        post-qualify) rather than the IB ``secType`` string.
        """
        cls_name = instrument.__class__.__name__
        if cls_name in {"FuturesContract", "FuturesSpread"}:
            return "futures"
        if cls_name in {"OptionContract", "OptionSpread"}:
            return "option"
        if cls_name == "CurrencyPair":
            return "fx"
        if cls_name in {"CryptoFuture", "CryptoPerpetual", "CryptoOption"}:
            return "crypto"
        return "equity"
```

**Step 3b: Add `_spec_from_canonical()` helper + `_upsert_definition_and_alias()` helper to `SecurityMaster`**

```python
    def _spec_from_canonical(self, canonical: str) -> InstrumentSpec:
        """Parse an already-resolved canonical alias string into an
        :class:`InstrumentSpec` for downstream ``self.resolve(spec)``.

        Reuses the venue mapping established by
        ``live_instrument_bootstrap.canonical_instrument_id``. Closed universe:
        - ``AAPL.NASDAQ`` / ``MSFT.NASDAQ`` → equity / NASDAQ
        - ``SPY.ARCA`` → equity / ARCA
        - ``EUR/USD.IDEALPRO`` → forex / IDEALPRO
        - ``ESM6.CME`` (or similar) → future / CME

        Raises ValueError on unknown venue — callers should widen the closed
        universe by adding a case here first.
        """
        symbol, _, venue = canonical.rpartition(".")
        if not venue:
            raise ValueError(f"Canonical alias {canonical!r} has no venue suffix")
        if venue == "NASDAQ":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="NASDAQ")
        if venue == "ARCA":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="ARCA")
        if venue == "IDEALPRO":
            # symbol is "EUR/USD"; base = "EUR", quote = "USD"
            base, _, quote = symbol.partition("/")
            return InstrumentSpec(
                asset_class="forex", symbol=base, venue="IDEALPRO", currency=quote or "USD"
            )
        if venue == "CME":
            return InstrumentSpec(asset_class="future", symbol=symbol, venue="CME")
        raise ValueError(
            f"Unknown venue {venue!r} in canonical {canonical!r} — extend "
            f"SecurityMaster._spec_from_canonical for new venues."
        )

    async def _upsert_definition_and_alias(
        self,
        *,
        raw_symbol: str,
        listing_venue: str,
        routing_venue: str,
        asset_class: str,
        alias_string: str,
        provider: str = "interactive_brokers",
        venue_format: str = "exchange_name",
    ) -> None:
        """Idempotent upsert: ``InstrumentDefinition`` row + one active
        ``InstrumentAlias`` row. Called from both ``resolve_for_live`` cold
        path (provider defaults to ``interactive_brokers``, venue_format
        ``exchange_name``) and ``_resolve_databento_continuous`` (provider
        ``databento``, venue_format ``databento_continuous``).

        Idempotency: scoped to ``(raw_symbol, provider, asset_class)`` —
        matches the ``uq_instrument_definitions_symbol_provider_asset``
        unique constraint created by T1. A second call with the same
        tuple updates ``refreshed_at`` and skips the alias insert if it
        already exists.
        """
        from datetime import datetime, timezone

        from sqlalchemy import select

        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition

        stmt = select(InstrumentDefinition).where(
            InstrumentDefinition.raw_symbol == raw_symbol,
            InstrumentDefinition.provider == provider,
            InstrumentDefinition.asset_class == asset_class,
        )
        idef = (await self._db.execute(stmt)).scalar_one_or_none()
        if idef is None:
            idef = InstrumentDefinition(
                raw_symbol=raw_symbol,
                listing_venue=listing_venue,
                routing_venue=routing_venue,
                asset_class=asset_class,
                provider=provider,
                lifecycle_state="active",
            )
            self._db.add(idef)
            await self._db.flush()
        # Check alias exists already under the same provider
        alias_stmt = select(InstrumentAlias).where(
            InstrumentAlias.alias_string == alias_string,
            InstrumentAlias.provider == provider,
            InstrumentAlias.effective_to.is_(None),
        )
        existing_alias = (await self._db.execute(alias_stmt)).scalar_one_or_none()
        if existing_alias is None:
            self._db.add(
                InstrumentAlias(
                    instrument_uid=idef.instrument_uid,
                    alias_string=alias_string,
                    venue_format=venue_format,
                    provider=provider,
                    effective_from=datetime.now(timezone.utc).date(),
                )
            )
        idef.refreshed_at = datetime.now(timezone.utc)
        await self._db.flush()
```

**Step 4: Run test — verify pass** → expected PASS (2 tests).

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/security_master/service.py \
        claude-version/backend/tests/integration/test_security_master_resolve_live.py
git commit -m "feat(registry): SecurityMaster.resolve_for_live — warm-cache + IB fallback upsert"
```

---

### Task 9: Extend `SecurityMaster` with `resolve_for_backtest(symbols)` method

**Files:**

- Modify: `claude-version/backend/src/msai/services/nautilus/security_master/service.py`
- Test: `claude-version/backend/tests/integration/test_security_master_resolve_backtest.py`

**Semantics (per PRD):** backtest does NOT auto-resolve. Missing rows raise `DatabentoDefinitionMissing` with an operator-facing hint. EXCEPT `.Z.N` continuous patterns, which synthesize on-the-fly via Task 6's helper if Databento definition files are accessible.

**Step 1: Write failing test**

```python
import pytest

from msai.services.nautilus.security_master.service import (
    DatabentoDefinitionMissing,
    SecurityMaster,
)


@pytest.mark.asyncio
async def test_resolve_for_backtest_raises_on_empty_registry(session_factory):
    async with session_factory() as session:
        # v2.2: qualifier is optional — backtest construction omits it.
        sm = SecurityMaster(qualifier=None, db=session)
        with pytest.raises(DatabentoDefinitionMissing):
            await sm.resolve_for_backtest(["ZZZZ"])


@pytest.mark.asyncio
async def test_resolve_for_backtest_continuous_requires_databento_client(
    session_factory,
):
    """v2.2 P1: `.Z.N` input with ``databento_client=None`` raises a clear ValueError."""
    async with session_factory() as session:
        sm = SecurityMaster(qualifier=None, db=session, databento_client=None)
        with pytest.raises(ValueError, match="DatabentoClient required"):
            await sm.resolve_for_backtest(
                ["ES.Z.5"], start="2024-01-01", end="2024-12-31"
            )
```

Plus: a mocked continuous-futures happy-path test using `monkeypatch` of `resolved_databento_definition` that passes a mocked `DatabentoClient`.

**Step 2: Run failing test** → expected FAIL.

**Step 3: Add `resolve_for_backtest` + `DatabentoDefinitionMissing` exception**

```python
class DatabentoDefinitionMissing(Exception):
    """Raised when a backtest symbol has no persisted Databento definition."""


class SecurityMaster:
    # ... existing code ...

    async def resolve_for_backtest(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        dataset: str = "GLBX.MDP3",
    ) -> list[str]:
        """Resolve ``symbols`` for backtest use. Explicit kwargs (v2.2 P1 fix).

        Args:
            symbols: raw tickers (``"AAPL"``), pre-qualified aliases
                (``"AAPL.NASDAQ"``), or Databento continuous patterns
                (``"ES.Z.5"``).
            start: ``YYYY-MM-DD`` backtest start. Only consulted for
                ``.Z.N`` synthesis; threaded into
                ``_resolve_databento_continuous``. Defaults to None →
                helper uses a safe "last 1 year" fallback.
            end: ``YYYY-MM-DD`` backtest end. Same semantics as ``start``.
            dataset: Databento dataset slug for ``.Z.N`` synthesis.
                Defaults to ``"GLBX.MDP3"`` (CME futures).

        Returns:
            One alias_string per input symbol, in input order.

        Raises:
            DatabentoDefinitionMissing: the registry has no row for the
                requested symbol under ``provider="databento"``.
            ValueError: a continuous pattern was requested but the
                SecurityMaster was constructed without a
                ``databento_client``.
        """
        from msai.services.nautilus.security_master.continuous_futures import (
            is_databento_continuous_pattern,
        )
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        out: list[str] = []
        for sym in symbols:
            if is_databento_continuous_pattern(sym):
                out.append(
                    await self._resolve_databento_continuous(
                        sym, start=start, end=end, dataset=dataset
                    )
                )
                continue
            if "." in sym:
                idef = await registry.find_by_alias(sym, provider="databento")
                if idef is not None:
                    out.append(sym)
                    continue
            idef = await registry.find_by_raw_symbol(sym, provider="databento")
            if idef is None:
                raise DatabentoDefinitionMissing(
                    f"No Databento definition for {sym!r}. Run "
                    f"`msai instruments refresh --symbols {sym} --provider databento` "
                    f"first."
                )
            active = next(
                (a for a in idef.aliases if a.effective_to is None), None
            )
            if active is None:
                raise DatabentoDefinitionMissing(
                    f"Definition exists for {sym!r} but no active alias"
                )
            out.append(active.alias_string)
        return out

    async def _resolve_databento_continuous(
        self,
        sym: str,
        *,
        start: str | None,
        end: str | None,
        dataset: str,
    ) -> str:
        """Resolve a Databento continuous-futures pattern (e.g. ``ES.Z.5``).

        Args:
            sym: the raw continuous pattern (e.g. ``"ES.Z.5"``).
            start: backtest start ``YYYY-MM-DD`` (threaded from
                ``resolve_for_backtest``); ``None`` → use 1-year default.
            end: backtest end ``YYYY-MM-DD``; ``None`` → today.
            dataset: Databento dataset slug (default ``"GLBX.MDP3"``).

        Flow:
            1. Warm check: registry hit by ``raw_symbol`` under provider
               ``"databento"`` with an active alias → return alias_string.
            2. Cold path: requires ``self._databento`` (ValueError if
               ``None``). Fetch definition file, synthesize via
               ``resolved_databento_definition``, then upsert through
               ``self._upsert_definition_and_alias`` with
               ``provider="databento"`` + ``venue_format="databento_continuous"``.
               The upsert helper is idempotent on
               ``(raw_symbol, provider, asset_class)`` so repeated calls
               don't violate the ``uq_instrument_definitions_symbol_provider_asset``
               constraint.
        """
        from datetime import datetime, timezone

        from msai.core.config import settings
        from msai.services.nautilus.security_master.continuous_futures import (
            raw_symbol_from_request,
            resolved_databento_definition,
        )
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        raw = raw_symbol_from_request(sym)
        resolved_start = start or "2024-01-01"
        resolved_end = (
            end or datetime.now(timezone.utc).date().isoformat()
        )

        registry = InstrumentRegistry(self._db)
        idef = await registry.find_by_raw_symbol(raw, provider="databento")
        if idef is not None:
            active = next((a for a in idef.aliases if a.effective_to is None), None)
            if active is not None:
                # Window-based refresh logic (continuous_needs_refresh_for_window)
                # is staged for a follow-up PR once the alias carries its own
                # definition_window columns. For v2.2, any existing active
                # alias is accepted as-is.
                return active.alias_string

        # Cold path: fetch + synthesize + upsert (idempotent).
        if self._databento is None:
            raise ValueError(
                f"DatabentoClient required for continuous-futures resolution "
                f"of {sym!r} — construct SecurityMaster with databento_client=... "
                f"for backtest use."
            )

        safe_dataset = dataset.replace("/", "_")
        safe_start = resolved_start.replace(":", "-")
        safe_end = resolved_end.replace(":", "-")
        definition_path = (
            settings.databento_definition_root
            / safe_dataset
            / raw
            / f"{safe_start}_{safe_end}.definition.dbn.zst"
        )
        instruments = await self._databento.fetch_definition_instruments(
            raw,
            resolved_start,
            resolved_end,
            dataset=dataset,
            target_path=definition_path,
        )
        resolved = resolved_databento_definition(
            raw_symbol=raw,
            instruments=instruments,
            dataset=dataset,
            start=resolved_start,
            end=resolved_end,
            definition_path=definition_path,
        )

        # Reuse Task 8's idempotent upsert helper with provider="databento".
        # This prevents the IntegrityError that would occur on a second
        # cold-miss call for the same continuous pattern under the
        # ``uq_instrument_definitions_symbol_provider_asset`` unique constraint.
        await self._upsert_definition_and_alias(
            raw_symbol=raw,
            listing_venue=resolved.listing_venue,
            routing_venue=resolved.routing_venue,
            asset_class=resolved.asset_class,
            alias_string=resolved.instrument_id,
            provider="databento",
            venue_format="databento_continuous",
        )
        return resolved.instrument_id

    # NOTE (historical — v2.1): prior to v2.2 this function performed an
    # inline INSERT of InstrumentDefinition + InstrumentAlias, which
    # raised IntegrityError on the second invocation. The inline insert
    # was replaced with ``self._upsert_definition_and_alias(..., provider=
    # "databento", venue_format="databento_continuous")`` (Task 9 Step 3).
    # The legacy inline insert below is retained here as a diff anchor
    # for reviewers comparing v2.1 → v2.2. Do NOT resurrect — the
    # upsert helper is the canonical path.
    #
    # --- LEGACY v2.1 (do not use) ---
    # idef = InstrumentDefinition(..., provider="databento", ...)
    # self._db.add(idef); await self._db.flush()
    # self._db.add(InstrumentAlias(instrument_uid=idef.instrument_uid, ...))
    # await self._db.flush()
    # return resolved.instrument_id
```

**Continuous-pattern helper (unchanged from v2.1 — appended to `continuous_futures.py`):**

```python
def raw_continuous_suffix(raw: str) -> str:
    """From ``ES.Z.5`` return ``.Z.5``. Assumes ``is_databento_continuous_pattern(raw)``.

    Reserved for the follow-up PR that persists the ``.Z.N`` pattern on
    the ``instrument_definitions.continuous_pattern`` column for audit.
    Not used in v2.2 because ``_upsert_definition_and_alias`` doesn't
    set that column; the continuous nature is encoded in the alias's
    ``venue_format="databento_continuous"``.
    """
    parts = raw.split(".")
    return "." + parts[1] + "." + parts[2]
```

**Step 4: Run test — verify pass**

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/security_master/service.py \
        claude-version/backend/tests/integration/test_security_master_resolve_backtest.py
git commit -m "feat(registry): SecurityMaster.resolve_for_backtest — fail-loud + .Z.N synthesis"
```

---

## Phase 5: Nautilus-native persistence verification

### Task 10: Verify `Cache(database=redis_database)` Instrument survives close/reopen

**Scope narrowed in v2.1** — original plan proposed a full TradingNode subprocess restart test, which needs the Nautilus Rust kernel + exec clients + data clients + IB gateway + etc. Instead, test the cache layer directly: construct a `Cache` with a `DatabaseConfig(type='redis', host=..., port=...)` backing, call `add_instrument(...)`, close, reopen a NEW `Cache` with the same backing, assert `cache.instrument(id)` returns the same `Instrument`.

**Existing harness:** `tests/conftest.py:59,67` provisions Redis via testcontainers — reuse.

**Files:**

- Create: `claude-version/backend/tests/integration/test_cache_redis_instrument_roundtrip.py`

**Step 1: Write the test**

```python
from __future__ import annotations

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.test_kit.providers import TestInstrumentProvider


@pytest.mark.asyncio
async def test_instrument_persists_across_cache_recreate(redis_port):
    db_cfg = DatabaseConfig(type="redis", host="localhost", port=redis_port)
    cache_cfg = CacheConfig(database=db_cfg)
    cache_a = Cache(config=cache_cfg)
    inst = TestInstrumentProvider.aapl_equity()
    cache_a.add_instrument(inst)
    # Close the cache (flush any buffered writes)
    cache_a.flush_db()
    # Construct a NEW cache pointing at the same Redis
    cache_b = Cache(config=cache_cfg)
    cache_b.cache_instruments()  # load from backing DB
    retrieved = cache_b.instrument(inst.id)
    assert retrieved is not None
    assert retrieved.id == inst.id
```

**Step 2: Run**

Expected: PASS if `CacheConfig(database=redis)` wiring works as claimed.

**Step 3: Commit**

```bash
git add claude-version/backend/tests/integration/test_cache_redis_instrument_roundtrip.py
git commit -m "test(registry): verify Cache(database=redis) Instrument round-trip"
```

---

> **Note:** Old Task 11 ("update all existing `from_dbn_file()` call sites") was merged into Task 7 in v2.1 — there are no existing call sites in claude-version; the only call site is the one Task 7 creates inside `DatabentoClient.fetch_definition_instruments`. The `use_exchange_as_venue=True` kwarg now lives in Task 7. This note is informational — no T11 task body exists because numbering is sequential.

---

## Phase 6: Backtest wiring (scope-back: live wiring deferred to follow-up PR)

**v3.0 scope note:** Live-wiring tasks (v2.2's T20 schema migration + T21 supervisor read) are removed from this PR. After 4 review iterations, the live-wiring architecture could not settle:

- **Option A** — supervisor calls `SecurityMaster.resolve_for_live` inline. Killed by gotcha #3 (`ibg_client_id` collision) — supervisor has no `IBQualifier`.
- **Option B** — persist resolved canonicals on `live_portfolio_revision_strategies`. Killed because `canonical_instruments` is time-varying (futures rolls) but `composition_hash` is identity; `RevisionService.snapshot()` collapses post-roll revisions onto pre-roll, freezing stale canonicals forever. Also: `snapshot()` runs in FastAPI process with no IB connection + in a `SELECT FOR UPDATE` transaction where network calls are unsafe.
- **Option C** — payload-dict hint. Killed because supervisor deliberately ignores `payload_dict` (`# noqa: ARG001` at `__main__.py:105`).

**Follow-up PR (see skeleton at end of file)** will validate a new Option D via a dedicated council pass before any plan lands.

This phase wires only the backtest path (`resolve_for_backtest`). Live path (`/api/v1/live/start`, supervisor, revision snapshot) is untouched in this PR — existing `canonical_instrument_id()` closed-universe resolver continues to serve live callers unchanged.

### Task 11: Wire `SecurityMaster.resolve_for_backtest` into backtest path

**Files:**

- Modify: `claude-version/backend/src/msai/api/backtests.py:90` (currently: `canonical_instruments = [canonical_instrument_id(s) for s in body.instruments]`)
- Test: `claude-version/backend/tests/integration/test_backtests_api_uses_registry.py`

**Worker-path note (iter-3 P1 fix):** `workers/backtest_job.py:89` reads `symbols: list[str] = list(backtest_row["instruments"])` — the worker pulls instruments from the `Backtest.instruments` DB column, NOT from the job payload. Because Step 3 writes the resolved canonicals to that column via `api/backtests.py:90`, the worker automatically sees the resolved values with zero changes. **No `workers/backtest_job.py` modification needed.**

**Catalog-builder note (iter-4 P1 fix):** Per iter-4 Codex finding, the existing `build_catalog_for_symbol()` at `catalog_builder.py:99` already handles dotted canonical IDs via `resolve_instrument()` (which strips venue and resolves). **No `build_catalog_for_canonical_id()` helper is needed.** The backtest wiring task only needs to modify `api/backtests.py:90`. Everything downstream (worker, catalog_builder) stays unchanged.

**Step 1: Read current state**

```bash
sed -n '78,110p' claude-version/backend/src/msai/api/backtests.py
sed -n '60,110p' claude-version/backend/src/msai/services/nautilus/catalog_builder.py
sed -n '85,110p' claude-version/backend/src/msai/workers/backtest_job.py   # verify: reads from backtest_row["instruments"]
```

**Step 2: Write failing integration test**

Per `tests/conftest.py:33-37`, the HTTP fixture is named `client` (NOT `authenticated_client`). Per `api/backtests.py:42-47`, `POST /api/v1/backtests/run` returns `201 Created`, NOT 200.

```python
@pytest.mark.asyncio
async def test_backtest_run_resolves_via_security_master(
    client, session_factory, ...
):
    # Arrange — seed registry for AAPL under provider="databento"
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="databento",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="databento",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

    # Act — POST /api/v1/backtests/run with {"instruments": ["AAPL"]}
    resp = await client.post(
        "/api/v1/backtests/run",
        json={"strategy_id": "...", "instruments": ["AAPL"], "start_date": "...", "end_date": "..."},
    )
    assert resp.status_code == 201  # per api/backtests.py:42-47 — POST returns 201 Created
    backtest_id = resp.json()["id"]
    # Assert — DB row's instruments column contains "AAPL.NASDAQ"
    async with session_factory() as session:
        bt = await session.get(Backtest, uuid.UUID(backtest_id))
        assert bt.instruments == ["AAPL.NASDAQ"]
```

**Step 3: Modify `api/backtests.py:90`**

Replace the line:

```python
canonical_instruments = [canonical_instrument_id(s) for s in body.instruments]
```

with:

```python
    # v3.0: resolve via registry. SecurityMaster's backtest ctor omits
    # qualifier (no IB needed for backtest resolution). For the Databento
    # client we follow the existing pattern used at
    # `workers/nightly_ingest.py:256` and `services/data_ingestion.py:67`:
    # instantiate `DatabentoClient(settings.databento_api_key)` if the API
    # key is present; else None. If a `.Z.N` symbol is requested with
    # `databento_client=None`, `resolve_for_backtest` MUST raise a clear
    # error: "DATABENTO_API_KEY not configured — .Z.N continuous futures
    # require Databento".
    from msai.core.config import settings
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.service import SecurityMaster

    databento_client = (
        DatabentoClient(settings.databento_api_key)
        if settings.databento_api_key
        else None
    )
    security_master = SecurityMaster(
        qualifier=None,
        db=db,
        databento_client=databento_client,
    )
    canonical_instruments = await security_master.resolve_for_backtest(
        body.instruments,
        start=body.start_date.isoformat(),
        end=body.end_date.isoformat(),
    )
```

**Step 4: (removed — no catalog_builder changes needed; see iter-4 note above)**

The existing `build_catalog_for_symbol()` at `catalog_builder.py:99` already handles dotted canonical IDs via `resolve_instrument()`. `workers/backtest_job.py:89` reads from `Backtest.instruments` (the DB column we just wrote canonical IDs into), so every downstream caller sees the resolved values automatically.

**Step 5: Run test + regression sweep**

```bash
cd claude-version/backend && uv run pytest tests/ -q
```

**Regression caveat (v2.2 — P2 fix):** For futures symbols, the registry MUST be pre-seeded with `ES → ESM6.CME` (or appropriate front-month) BEFORE running the existing backtest tests that use `instruments=["ES"]`. If the registry is empty, `resolve_for_backtest(["ES"])` raises `DatabentoDefinitionMissing` rather than synthesizing, because `"ES"` (not `"ES.Z.5"`) is NOT a continuous pattern. The T11 integration test MUST seed the registry first; any other futures-backtest test that exercises this path MUST do the same. Equity backtests (AAPL, MSFT, SPY) remain regression-clean as long as the registry has those rows under `provider="databento"`.

**Step 6: Commit**

```bash
git add claude-version/backend/src/msai/api/backtests.py \
        claude-version/backend/tests/integration/test_backtests_api_uses_registry.py
git commit -m "feat(registry): wire SecurityMaster.resolve_for_backtest into backtest API"
```

---

## Phase 7: CLI

### Task 12: Create `instruments_app` Typer sub-app

**Reason:** `instruments_app` does not exist today. Task 13's `@instruments_app.command(...)` must be preceded by the sub-app's creation.

**Files:**

- Modify: `claude-version/backend/src/msai/cli.py` (add sub-app)

**Step 1: Read existing CLI sub-app pattern**

```bash
grep -n "typer.Typer\|add_typer" claude-version/backend/src/msai/cli.py | head -20
```

Observe how `strategy_app`, `backtest_app`, etc. are defined + registered.

**Step 2: Add the sub-app following the same pattern**

```python
instruments_app = typer.Typer(
    name="instruments",
    help="Instrument registry operations",
    rich_markup_mode="rich",
)
app.add_typer(instruments_app, name="instruments")
```

**Step 3: Verify `msai instruments --help` lists the sub-app**

```bash
cd claude-version/backend && uv run msai instruments --help
```

Expected: help text prints with no commands yet (added in T13).

**Step 4: Commit**

```bash
git add claude-version/backend/src/msai/cli.py
git commit -m "feat(cli): add instruments_app Typer sub-app"
```

---

### Task 13: `msai instruments refresh` subcommand

**PRD alignment:** this CLI is the primary pre-warm tool that PRD §47-48 assumes exists. The "lazy, empty at ship, populate on first use" semantics rely on operators being able to reach this CLI to pre-warm the registry before deploying strategies. It is the **only** code path in the system that legitimately constructs a short-lived `IBQualifier` outside the Nautilus subprocess (every other path runs in either the live supervisor, which has no qualifier, or the FastAPI process, which must not make blocking IB calls inside request handlers). The CLI runs one-shot: connect → resolve → write rows → disconnect.

**Files:**

- Modify: `claude-version/backend/src/msai/cli.py` (add command to `instruments_app`)
- Test: `claude-version/backend/tests/unit/test_cli_instruments_refresh.py`

**Supported providers:**

- `--provider interactive_brokers` — constructs a short-lived `IBQualifier` with a dedicated out-of-band `ibg_client_id` (e.g. `999`) so it never collides with live subprocesses (gotcha #3). Grep `claude-version/backend/src/msai/` for how `InteractiveBrokersInstrumentProvider` is constructed in existing non-subprocess code (e.g. tests). If no canonical pattern exists, use `ibg_client_id=999` as the out-of-band value and document the reservation in a code comment + in the refresh command's `--help` text.
- `--provider databento` — uses `DatabentoClient(settings.databento_api_key)` per the existing pattern at `workers/nightly_ingest.py:256` and `services/data_ingestion.py:67`. If a `.Z.N` symbol is requested and `DATABENTO_API_KEY` is unset, raise a clear CLI error: "DATABENTO_API_KEY not configured — .Z.N continuous futures require Databento".

**Step 1: Write failing tests (two parametrized cases)**

Use Typer's `CliRunner`. Cover both providers:

- Invoke `msai instruments refresh --symbols AAPL,ES --provider interactive_brokers`. Mock `IBQualifier.qualify` + `SecurityMaster.resolve_for_live` to return canonical IDs. Assert exit code 0.
- Invoke `msai instruments refresh --symbols ES.Z.5 --provider databento`. Mock `DatabentoClient.fetch_definition_instruments`. Assert exit code 0. Additionally: invoke with no `DATABENTO_API_KEY` env var set and assert exit code != 0 with the operator-hint error message.

**Step 2: Run failing tests** → expected FAIL.

**Step 3: Add command**

```python
@instruments_app.command("refresh")
def instruments_refresh(
    symbols: Annotated[str, typer.Option(help="Comma-separated list of symbols")],
    provider: Annotated[
        str,
        typer.Option(
            help=(
                "Registry provider. `interactive_brokers` uses a one-shot "
                "IBQualifier with dedicated ibg_client_id=999 (out-of-band, "
                "never collides with live subprocesses). `databento` uses "
                "DatabentoClient(settings.databento_api_key); unset API key "
                "raises a clear error for .Z.N continuous symbols."
            ),
        ),
    ] = "interactive_brokers",
) -> None:
    """Pre-warm the registry for the given symbols via the chosen provider.

    This is the PRD §47-48 pre-warm tool: lazy-populate semantics assume
    operators invoke this CLI before deploying a strategy that references
    a cold symbol. Without pre-warming, `/api/v1/live/start` (once wired
    in the follow-up PR) will surface a 422 with "Run `msai instruments
    refresh --symbols X --provider interactive_brokers` to pre-warm."
    """
    symbols_list = [s.strip() for s in symbols.split(",") if s.strip()]
    asyncio.run(_refresh_async(symbols_list, provider=provider))


async def _refresh_async(symbols: list[str], *, provider: str) -> None:
    from msai.core.config import settings
    from msai.core.database import async_session_factory
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.continuous_futures import (
        is_databento_continuous_pattern,
    )
    from msai.services.nautilus.security_master.service import SecurityMaster
    # `async_session_factory` is the module-level callable at
    # src/msai/core/database.py:24. Invoke it to get a new AsyncSession.
    async with async_session_factory() as session:
        if provider == "interactive_brokers":
            # Construct a short-lived IBQualifier with dedicated
            # ibg_client_id=999 (out-of-band so never collides with live
            # subprocesses — see nautilus.md gotcha #3). No production
            # IBQualifier construction exists in src/ today (only in
            # tests with MagicMock providers), so build one by copying
            # the provider-config shape from
            # `services/nautilus/live_instrument_bootstrap.py:251-296`
            # (the live subprocess's IB provider-config builder). Wrap
            # the resulting provider in `IBQualifier(provider)` per
            # `security_master/ib_qualifier.py:179`. Approximate shape:
            #   from nautilus_trader.adapters.interactive_brokers.factories import (
            #       get_cached_ib_client,
            #       get_cached_interactive_brokers_instrument_provider,
            #   )
            #   loop = asyncio.get_running_loop()
            #   client = get_cached_ib_client(
            #       loop=loop, msgbus=..., cache=..., clock=...,
            #       host=settings.ib_host, port=settings.ib_port_paper,
            #       client_id=999,
            #       request_timeout_secs=settings.ib_request_timeout_seconds,
            #   )
            #   provider = get_cached_interactive_brokers_instrument_provider(
            #       client, clock, InteractiveBrokersInstrumentProviderConfig(...)
            #   )
            #   await client.wait_until_ready(timeout=settings.ib_request_timeout_seconds)
            #   qualifier = IBQualifier(provider)
            # After `resolve_for_live` completes, call `client.disconnect()`
            # to release the client_id=999 slot.
            qualifier = ...
            sm = SecurityMaster(qualifier=qualifier, db=session)
            await sm.resolve_for_live(symbols)
        elif provider == "databento":
            if not settings.databento_api_key:
                # Only raise if the input actually needs Databento — i.e.
                # any `.Z.N` symbol. Plain equity/futures aliases can be
                # synthesized without the client.
                needs_databento = any(is_databento_continuous_pattern(s) for s in symbols)
                if needs_databento:
                    raise typer.BadParameter(
                        "DATABENTO_API_KEY not configured — .Z.N "
                        "continuous futures require Databento.",
                    )
                databento_client = None
            else:
                databento_client = DatabentoClient(settings.databento_api_key)
            sm = SecurityMaster(
                qualifier=None, db=session, databento_client=databento_client,
            )
            # Backtest-style resolve with a default window (last 30d).
            from datetime import date, timedelta
            today = date.today()
            await sm.resolve_for_backtest(
                symbols,
                start=(today - timedelta(days=30)).isoformat(),
                end=today.isoformat(),
            )
        else:
            raise typer.BadParameter(f"Unknown provider: {provider!r}")
```

**Step 4: Run tests — verify pass**

**Step 5: Commit**

```bash
git add claude-version/backend/src/msai/cli.py \
        claude-version/backend/tests/unit/test_cli_instruments_refresh.py
git commit -m "feat(cli): msai instruments refresh — pre-warm registry (IB + Databento)"
```

---

## Phase 8: Split-brain Normalization

**Non-TDD classification:** Tasks 14–16 are mechanical cleanup (docstring edits, test-fixture edits, deprecation markers). They do NOT follow red-green-refactor. They run full test suite AFTER edits to verify no regressions; that is the verification.

### Task 14: Normalize `.XCME` → `.CME` in source-file docstrings/examples

**Files to modify (from grep audit; re-run in Step 1 to confirm):**

- `claude-version/backend/src/msai/models/instrument_cache.py:4` (docstring comment)
- `claude-version/backend/src/msai/api/backtests.py:84` (comment)
- `claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py:82` (docstring)
- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py:21-22` (canonical format doc — main offender)
- `claude-version/backend/src/msai/services/nautilus/instruments.py:63` (docstring example)
- `claude-version/backend/src/msai/services/nautilus/backtest_runner.py:75` (docstring example)

**Keep:** `live_instrument_bootstrap.py:147` — legacy-input-accept line.

**Step 1: Re-run grep to confirm current count**

```bash
cd claude-version/backend && grep -rn "\.XCME" src/ | grep -v "live_instrument_bootstrap.py:147"
```

**Step 2: Replace `.XCME` → `.CME` in each identified occurrence** (docstrings/comments/examples ONLY; no runtime constants).

**Step 3: Run full test suite**

```bash
cd claude-version/backend && uv run pytest tests/unit/ -q
```

Expected: PASS (no semantic change, docstrings only).

**Step 4: Commit**

```bash
git add claude-version/backend/src/msai/
git commit -m "refactor(registry): normalize .XCME → .CME in source docstrings + canonical spec doc"
```

---

### Task 15: Normalize `.XCME` → `.CME` in test fixtures

**Step 1: Enumerate**

```bash
cd claude-version/backend && grep -rn "\.XCME" tests/
```

Expected: ~26 occurrences (tests only).

**Step 2: Replace in each file**

For each test assertion with literal `ESM5.XCME` / `AAPL.XCME` etc., replace with `.CME`. If a test is specifically exercising legacy-input-accept, annotate with `# legacy accept` comment.

**Step 3: Run affected tests**

```bash
cd claude-version/backend && uv run pytest tests/ -q
```

Expected: PASS.

**Step 4: Verify grep clean**

```bash
grep -rn "\.XCME" claude-version/backend/src/ claude-version/backend/tests/ | grep -v "live_instrument_bootstrap.py:147\|legacy accept"
```

Expected: no output.

**Step 5: Commit**

```bash
git add claude-version/backend/tests/
git commit -m "test(registry): normalize .XCME → .CME across test fixtures"
```

---

### Task 16: Deprecation docstrings on legacy `canonical_instrument_id` layers

**Files:**

- Modify: `claude-version/backend/src/msai/services/nautilus/instruments.py` (module docstring)
- Modify: `claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py` (module docstring)

**Step 1: Append deprecation notices** (per T5 of v1.0 plan — same language).

**Step 2: Run tests** → no regression expected.

**Step 3: Commit**

```bash
git add claude-version/backend/src/msai/services/nautilus/instruments.py \
        claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py
git commit -m "docs(registry): flag legacy canonical_instrument_id layers as deprecated for new callers"
```

---

## Phase 9: Integration tests + docs + verify

### Task 17: Full-lifecycle integration test — create + add + query

**Files:**

- Create: `claude-version/backend/tests/integration/test_instrument_registry_lifecycle.py`

**Coverage:**

- Create `InstrumentDefinition` for ES.
- Add `InstrumentAlias` for `ESM6.CME` (effective_from = today).
- Query via `InstrumentRegistry.find_by_alias("ESM6.CME", provider=..., as_of_date=today)` → returns definition.
- Roll: set `effective_to` on `ESM6.CME`, add new alias for `ESU6.CME`. Query by old alias with `as_of_date=yesterday` → returns. Query by old alias with `as_of_date=today+90d` → returns None. Query by new alias → returns.

**Step 1: Write + run + commit** (standard TDD not applicable — this tests Phase 2 work end-to-end).

---

### Task 18: Backtest ↔ live `InstrumentId` parity test (freezegun)

**Files:**

- Create: `claude-version/backend/tests/integration/test_backtest_live_parity.py`
- Add to `pyproject.toml` dev deps if missing: `freezegun`.

**Step 1: Write the test using freezegun**

```python
import pytest
from datetime import date
from freezegun import freeze_time

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.service import SecurityMaster


@pytest.mark.asyncio
@freeze_time("2026-04-17")
async def test_resolve_live_and_backtest_return_identical_ids(
    session_factory, mock_qualifier,
):
    """PRD US-001: same strategy sees same InstrumentId strings across
    backtest and live paths. For ``ES``, both should resolve to
    ``ESM6.CME`` (front-month via `canonical_instrument_id(today=...)`).

    v2.2: no ``mock_databento`` fixture — the AAPL and ES warm-path
    resolves hit the registry only; neither touches the Databento
    continuous-futures synthesis path."""
    async with session_factory() as session:
        # Seed registry for AAPL (equity) and ES-front-month
        for provider in ("interactive_brokers", "databento"):
            session.add(
                InstrumentDefinition(
                    raw_symbol="AAPL",
                    listing_venue="NASDAQ",
                    routing_venue="NASDAQ",
                    asset_class="equity",
                    provider=provider,
                    lifecycle_state="active",
                )
            )
            session.add(
                InstrumentDefinition(
                    raw_symbol="ES",
                    listing_venue="CME",
                    routing_venue="CME",
                    asset_class="futures",
                    provider=provider,
                    lifecycle_state="active",
                )
            )
        await session.flush()
        # Add aliases
        for row in (await session.execute(select(InstrumentDefinition))).scalars():
            alias_string = (
                "AAPL.NASDAQ" if row.raw_symbol == "AAPL" else "ESM6.CME"
            )
            session.add(
                InstrumentAlias(
                    instrument_uid=row.instrument_uid,
                    alias_string=alias_string,
                    venue_format="exchange_name",
                    provider=row.provider,
                    effective_from=date(2026, 3, 17),
                )
            )
        await session.commit()

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)
        live_ids = await sm.resolve_for_live(["AAPL", "ES"])
        backtest_ids = await sm.resolve_for_backtest(["AAPL", "ES"])

    assert live_ids == backtest_ids, (
        f"PRD US-001 parity violation: live={live_ids!r} vs backtest={backtest_ids!r}"
    )
    assert live_ids == ["AAPL.NASDAQ", "ESM6.CME"]
```

**Step 2: Run + commit**

---

### Task 19: Continuous-futures backtest integration test (Databento fixture)

**Files:**

- Create: `claude-version/backend/tests/integration/test_continuous_futures_backtest.py`
- May need: `tests/fixtures/databento/ES_Z_5_small.definition.dbn.zst` (skippable if fixture absent).

**Step 1: Write test (skip if fixture absent)**

```python
@pytest.mark.skipif(
    not (Path(__file__).parent / "../fixtures/databento/ES_Z_5_small.definition.dbn.zst").exists(),
    reason="Databento fixture not present — regenerate via msai data ingest",
)
@pytest.mark.asyncio
async def test_continuous_futures_backtest_produces_bars(...):
    ...
```

**Step 2: Run + commit**

---

### Task 20: Docs + full verify-app pass

**Files:**

- Modify: `claude-version/CLAUDE.md` — append CLI Tools section, add follow-up note.
- Modify: `docs/CHANGELOG.md` — append entry.

**Step 1: Update `claude-version/CLAUDE.md`**

- Append to CLI Tools: `msai instruments refresh --symbols ... --provider [interactive_brokers|databento]`.
- Add follow-up-note line: "Instrument registry: new tables `instrument_definitions` + `instrument_aliases`. Live path still uses closed-universe `canonical_instrument_id()` — wiring is follow-up PR (see `docs/plans/2026-04-XX-live-wiring-instrument-registry.md`). Existing `instrument_cache` table coexists; migration is separate follow-up PR (`docs/plans/2026-04-XX-instrument-cache-migration.md`)."

**Step 2: Update `docs/CHANGELOG.md`**

Append entry describing: new schema (`instrument_definitions` + `instrument_aliases`) + `SecurityMaster.resolve_for_backtest` extensions + Databento `.Z.N` continuous-futures helpers + `msai instruments refresh` CLI + backtest wiring (live wiring deferred) + split-brain normalization + three follow-up PRs queued (Strategy Config Schema API, InstrumentCache Migration, Live-Wiring Registry).

**Step 3: Full verify-app pass**

Invoke the `verify-app` subagent per `/new-feature` Phase 5.3. Expected: 1228+ unit pass, integration pass, ruff clean, mypy --strict clean.

**Step 4: Backtest-only manual verify (no live path)**

```bash
# Schema
cd claude-version/backend && uv run alembic upgrade head
uv run python -c "from msai.models.instrument_definition import InstrumentDefinition; print(InstrumentDefinition.__table__)"

# CLI smoke
uv run msai instruments refresh --symbols AAPL --provider interactive_brokers
uv run msai instruments refresh --symbols ES.Z.5 --provider databento

# Backtest wiring smoke — POST /api/v1/backtests/run and assert 201 + canonical IDs persisted
# (use existing backtest-run integration test as template)
```

**Note:** Live-path manual verify commands (`/api/v1/live/start`, `/api/v1/live-portfolios/*`) are deliberately OMITTED from v3.0 because live wiring is not part of this PR. The existing closed-universe resolver (`canonical_instrument_id()`) still serves all live callers unchanged — no behavioral change on the live path.

**Step 5: Commit**

```bash
git add claude-version/CLAUDE.md docs/CHANGELOG.md
git commit -m "docs(registry): backtest-only registry lands; live-wiring split off to follow-up PR"
```

---

## Plan Review Loop — iter-4 expected outcome

Run:

1. Claude pass — re-read every file, verify v3.0 scope-back landed and all iter-4 mechanical fixes applied.
2. Codex pass — `codex review` against this v3.0.

**Exit:** both reviewers P0/P1/P2-clean on same pass. If residual P2s remain, document in CONTINUITY and proceed (per workflow.md: P2 fix is required; P3 is optional).

---

## Execution Hand-off (after iter-4 clean)

**Recommended: Subagent-Driven (this session)** via `superpowers:subagent-driven-development`. 20 tasks; fresh subagent per task keeps context small.

---

## Split-off PR Skeleton — Strategy Config Schema Extraction + API

**File to create (not in this PR):** `docs/plans/2026-04-XX-strategy-config-schema-api.md`

**Goal:** expose each strategy's Nautilus `StrategyConfig` via `GET /api/v1/strategies/{id}` as `config_schema` + `config_defaults` so a future UI can render forms.

**Why split off:** per-iter-1 Codex review, current `strategy_registry.py` uses `discover_strategies()` (module-level, not a class), strategies use Nautilus `StrategyConfig` (NOT Pydantic `BaseModel`), and `schemas/strategy.py:18-20,29` already exposes `default_config` on UUID-keyed routes, not `config_defaults` on name-keyed ones. The iter-1 plan's Tasks 15/16 mis-targeted all three. Split-off allows a clean targeted PR.

**Tasks (skeleton, ~4):**

1. Extend `DiscoveredStrategy` dataclass with `config_schema: dict` + `config_defaults: dict` fields.
2. Extend `_find_config_class()` to handle both Nautilus `StrategyConfig` and Pydantic `BaseModel` (dual-path).
3. Extend `GET /api/v1/strategies/{id}` response schema.
4. Wire extraction + expose via API.

**Risk:** low — purely additive; no live path touched.

---

## Split-off PR Skeleton — InstrumentCache → Registry Migration

**File to create (not in this PR):** `docs/plans/2026-04-XX-instrument-cache-migration.md`

**Goal:** migrate content from existing `instrument_cache` table into new `instrument_definitions` + `instrument_aliases` (and a new `trading_hours` location), then drop `instrument_cache`.

**Why split off:** iter-1 review surfaced that `instrument_cache` holds (a) Nautilus payload JSONB (subsumed by `CacheConfig(database=redis)`), (b) `trading_hours` JSONB (USED by `services/nautilus/market_hours.py`), (c) `ib_contract_json`. Migrating requires rewriting 7 downstream call sites (`trading_node_subprocess.py`, `security_master/{specs,service,parser}.py`, `risk/risk_aware_strategy.py`, `market_hours.py`, and the service). Doing this plus the new registry in one PR = too much scope for one review surface + risk to live trading.

**Ordering constraint:** this follow-up PR MUST run after the present PR merges AND after `CacheConfig(database=redis)` is verified in production through at least one restart cycle.

**Tasks (skeleton, ~8):**

1. Add `trading_hours` column to `InstrumentDefinition` (or create `instrument_trading_hours` child table).
2. Alembic data migration: for each `instrument_cache` row, upsert `instrument_definition` (by canonical_id → raw_symbol+venue) + `instrument_alias` + copy trading_hours.
3. Rewrite `services/nautilus/market_hours.py` to read from new location.
4. Rewrite `SecurityMaster.resolve()` / `.bulk_resolve()` to use Nautilus cache DB for payload + registry for metadata.
5. Rewrite `trading_node_subprocess.py` instrument loading to use Nautilus cache DB hydration (not `nautilus_instrument_json`).
6. Rewrite `risk/risk_aware_strategy.py` instrument lookups.
7. Drop `InstrumentCache` model + Alembic migration to drop `instrument_cache` table.
8. Integration test: backtest + live full-cycle without `instrument_cache`.

**Risk:** medium — touches 7 call sites including live path; needs its own council review.

---

## Split-off PR Skeleton — Live-Wiring for Instrument Registry

**File to create (not in this PR):** `docs/plans/2026-04-XX-live-wiring-instrument-registry.md`

**Goal:** wire `SecurityMaster.resolve_for_live` into the live-trading path so strategies deployed via `/api/v1/live/start` use the registry for canonical resolution instead of the closed-universe `canonical_instrument_id()` helper.

**Why split off:** 4 review iterations in the parent PR (`2026-04-17-db-backed-strategy-registry.md`) couldn't settle the architecture. Three options were investigated + rejected:

- **Option A** — supervisor calls `SecurityMaster.resolve_for_live` inline. Killed because `live_supervisor/__main__.py` has no `IBQualifier` and constructing one there triggers `ibg_client_id` collision (nautilus.md gotcha #3).
- **Option B** — persist resolved canonicals on `live_portfolio_revision_strategies` via new JSONB column, populated at `RevisionService.snapshot()`. Killed because canonical_instruments is time-varying (futures rolls) but `composition_hash` is identity; `snapshot()` collapses post-roll revisions onto pre-roll, freezing stale canonicals forever. Also: `snapshot()` runs in FastAPI process with no IB connection + in a `SELECT FOR UPDATE` transaction where network calls are unsafe.
- **Option C** — API publishes resolved canonicals via Redis command payload; supervisor reads. Killed because supervisor deliberately ignores `payload_dict` (see `live_supervisor/__main__.py:105` `# noqa: ARG001`) and rebuilds payload from DB for reliability.

**Candidate Option D for the follow-up PR** (not yet validated by council):

- Add `canonical_instruments: list[str]` column to `LiveDeployment` (mutable, per-deploy, non-immutable — correct semantics).
- Resolve at `/api/v1/live/start` time in the API handler using `SecurityMaster(qualifier=None, db=db)` in **warm-cache-only mode** (no IB connection required in FastAPI process).
- Cold misses raise 422 with message: "Instrument X not in registry. Run `msai instruments refresh --symbols X --provider interactive_brokers` to pre-warm."
- Persist canonical_instruments on `LiveDeployment` row before publishing Redis spawn command.
- Supervisor reads from `LiveDeployment.canonical_instruments` column in the rebuild-from-DB payload factory.
- Backfill: existing `LiveDeployment` rows get `canonical_instruments` populated by running `canonical_instrument_id()` on each stored `instruments` entry during Alembic migration (acceptable because existing rows are closed-universe by definition).

**Architectural constraint:** the CLI `msai instruments refresh --provider interactive_brokers` is the ONLY place that spawns an ephemeral IBQualifier with a dedicated `ibg_client_id`. All other code paths must assume warm-cache-only resolution. This matches PRD §47-48 "lazy, empty at ship, populate on /live/start or ingest" — the "on /live/start" clause requires the CLI to have pre-warmed.

**Tasks (skeleton, ~6 estimated):**

1. Alembic migration — add `canonical_instruments: list[str]` column to `live_deployments` (server_default `'{}'::text[]`, NOT NULL).
2. Backfill data migration — for each existing `LiveDeployment` row, populate `canonical_instruments` by resolving each `instruments` entry via `canonical_instrument_id(sym, today=<migration_date>)`.
3. Extend `SecurityMaster.resolve_for_live` with explicit `warm_cache_only: bool = False` kwarg; when True, raise on cold miss with the operator-hint error.
4. Wire `/api/v1/live/start` (and `/api/v1/live-portfolios/{id}/snapshot` pre-deploy path) to call `resolve_for_live(warm_cache_only=True)`, persist result on `LiveDeployment`.
5. Modify `live_supervisor/__main__.py:297-300` to read `member.canonical_instruments` from DB via the existing rebuild-from-DB factory, remove inline `canonical_instrument_id()` comprehension. Keep a back-compat branch logged-once for any legacy row with empty column.
6. Integration test: seed registry with non-front-month futures alias (e.g. `ESH7.CME` in April 2026), deploy, verify supervisor payload carries `ESH7.CME` (not recomputed `ESM6.CME` from closed-universe).

**Pre-execution gate:** this follow-up PR MUST have its own 5-advisor council pass on Option D before a plan is written. The goal is to validate that warm-cache-only resolution is operationally acceptable (operators will always pre-warm via CLI before deploying).

**Risk level:** medium — touches live-trading path. Needs live paper drill before merge.
