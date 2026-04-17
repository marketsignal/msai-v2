# Portfolio-Per-Account Live — PR #1 Implementation Plan (iter 2)

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Land the schema + domain models + services for the new live-composition layer. Zero live-risk — nothing in the live path uses these new tables yet.

**Architecture:** Four new tables (`live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `live_deployment_strategies`) and two new columns (`live_deployments.ib_login_key`, `live_node_processes.gateway_session_key`). Two services (`PortfolioService`, `RevisionService`) that enforce (a) only graduated strategies may be added and (b) revisions become immutable once frozen. The warm-restart identity boundary is the frozen revision's `composition_hash`.

**Tech Stack:** SQLAlchemy 2.0 + Alembic + PostgreSQL 16 + pytest + testcontainers.

**Design doc:** `docs/plans/2026-04-16-portfolio-per-account-live-design.md`.

**Working directory:** `.worktrees/portfolio-per-account-live/claude-version/backend/` (all paths below are relative).

---

## Iteration 2 changes vs iter 1

Plan-review loop iteration 1 findings addressed here:

- **P0 (FK cycle risk)**: dropped the `live_portfolios.latest_revision_id` FK entirely. Active-revision lookup becomes a simple query in `RevisionService.get_active_revision`. Eliminates the cycle, eliminates the cascade-delete workaround, keeps existing `drop_all/create_all` fixtures safe.
- **P1 (GraduationCandidate fixture fields)**: tests now supply `config={}` and `metrics={}` on `GraduationCandidate` (both are NOT NULL per `src/msai/models/graduation_candidate.py:33-34`).
- **P1 (immutability overclaim)**: dropped the "immutable once referenced by LiveDeployment" language — that's PR #2 scope. `is_frozen` is the single boundary in PR #1.
- **P1 (draft race)**: added partial unique index `uq_one_draft_per_portfolio` so the DB enforces "at most one unfrozen revision per portfolio". `PortfolioService._get_or_create_draft_revision` uses a read-then-insert pattern guarded by the partial unique index — if a concurrent caller wins the race, the second caller's flush raises `IntegrityError`, which propagates up (callers are expected to retry the whole operation). Defense-in-depth; service is not intended to be hammered concurrently on the same portfolio in PR #1.
- **P0 (snapshot race — iter 3 fix)**: `RevisionService.snapshot` now fetches the draft with `SELECT … FOR UPDATE` to serialize concurrent snapshot callers on the same portfolio. Without this, two callers could both see the draft unfrozen, the second could load the FIRST caller's just-frozen revision as "existing with matching hash", and delete it via `session.delete(draft)` — because it's the same row.
- **P1 (migration test below standard)**: added Task 11 — migration test in `tests/integration/test_alembic_migrations.py` following the existing file's pattern.
- **P1 (TimestampMixin convention)**: `LivePortfolio` uses `TimestampMixin` (matches `Portfolio`, `GraduationCandidate`). Revision/member/deployment-strategy rows are immutable-on-create, so they have `created_at` only (no `updated_at` — updates never happen).
- **P2 (UUID idiom)**: migration uses `sa.Uuid()` everywhere to match recent migrations (`h6b7c8d9e0f1`, `n2h3i4j5k6l7`).
- **P2 (private method in test)**: added public `PortfolioService.get_current_draft(portfolio_id)` helper; tests call it instead of `_get_draft_revision`.

---

## Scope boundary

In scope:

- Alembic migration (schema only, additive).
- SQLAlchemy models + relationships.
- Composition-hash utility.
- `PortfolioService` (create, add strategy, list, `get_current_draft`).
- `RevisionService` (snapshot → frozen revision, fetch active via query, immutability guard on `is_frozen`).
- Unit + integration tests.

Out of scope (deferred to PR #2):

- Portfolio CRUD API endpoints.
- `/api/v1/live/start` accepting `portfolio_revision_id`.
- Supervisor / subprocess changes.
- Read path (WebSocket / `/live/positions`) adaptation.
- Backfill migration of existing `LiveDeployment` rows.
- Column drops on `LiveDeployment`.
- Failure-isolation wrapper, cache namespacing, `load_state`/`save_state` verification.
- `LiveDeployment.portfolio_revision_id` FK (added in PR #2 — not now).

---

## Preflight

```bash
cd claude-version/backend
uv run pytest tests/unit -q 2>&1 | tail -3
uv run ruff check src/msai/models/ src/msai/services/live/ 2>&1 | tail -3
docker exec msai-claude-backend uv run alembic current 2>&1 | tail -1
```

Expected: 1209 unit tests pass, ruff clean on existing models/services paths, alembic head = `n2h3i4j5k6l7`.

---

### Task 1: Alembic migration (schema only)

**Files:**

- Create: `alembic/versions/o3i4j5k6l7m8_add_live_portfolio_tables.py`

**Step 1: Write the migration**

Create `alembic/versions/o3i4j5k6l7m8_add_live_portfolio_tables.py`:

```python
"""add live_portfolios, revisions, revision_strategies, deployment_strategies

Revision ID: o3i4j5k6l7m8
Revises: n2h3i4j5k6l7
Create Date: 2026-04-16 17:00:00.000000

PR #1 of the portfolio-per-account-live feature (design doc
docs/plans/2026-04-16-portfolio-per-account-live-design.md).

Adds the live-composition layer. No FK cycle — the "latest revision"
of a portfolio is computed on the fly via
``RevisionService.get_active_revision`` (order by ``revision_number``
desc + ``is_frozen=true``). This keeps the schema graph acyclic and
avoids cascade-delete-on-self semantics under existing
``drop_all/create_all`` fixtures.

Partial unique index ``uq_one_draft_per_portfolio`` enforces at
most one unfrozen revision per portfolio so concurrent
``add_strategy`` calls cannot race into two parallel drafts.

All ID columns use ``sa.Uuid()`` to match the convention from
``h6b7c8d9e0f1`` and ``n2h3i4j5k6l7``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "o3i4j5k6l7m8"
down_revision: str = "n2h3i4j5k6l7"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "live_portfolios",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_live_portfolios_name"),
    )

    op.create_table(
        "live_portfolio_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.Uuid(),
            sa.ForeignKey("live_portfolios.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("composition_hash", sa.String(64), nullable=False),
        sa.Column(
            "is_frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "revision_number",
            name="uq_live_portfolio_revisions_number",
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "composition_hash",
            name="uq_live_portfolio_revisions_hash",
        ),
    )
    # Partial unique index: at most one unfrozen (draft) revision per
    # portfolio. Prevents two concurrent add_strategy callers from
    # racing into two parallel drafts. Uses the Alembic-native
    # ``postgresql_where`` kwarg (idiomatic for partial indexes)
    # rather than raw SQL so autogenerate diffs stay clean.
    op.create_index(
        "uq_one_draft_per_portfolio",
        "live_portfolio_revisions",
        ["portfolio_id"],
        unique=True,
        postgresql_where=sa.text("is_frozen = false"),
    )

    op.create_table(
        "live_portfolio_revision_strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("live_portfolio_revisions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "strategy_id",
            sa.Uuid(),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("instruments", ARRAY(sa.String()), nullable=False),
        sa.Column("weight", sa.Numeric(8, 6), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "revision_id", "order_index", name="uq_lprs_revision_order"
        ),
        sa.UniqueConstraint(
            "revision_id", "strategy_id", name="uq_lprs_revision_strategy"
        ),
    )

    op.create_table(
        "live_deployment_strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "deployment_id",
            sa.Uuid(),
            sa.ForeignKey("live_deployments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "revision_strategy_id",
            sa.Uuid(),
            sa.ForeignKey(
                "live_portfolio_revision_strategies.id", ondelete="RESTRICT"
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("strategy_id_full", sa.String(280), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "deployment_id",
            "revision_strategy_id",
            name="uq_lds_deployment_revision_strategy",
        ),
    )

    op.add_column(
        "live_deployments",
        sa.Column("ib_login_key", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_live_deployments_ib_login_key",
        "live_deployments",
        ["ib_login_key"],
    )

    op.add_column(
        "live_node_processes",
        sa.Column("gateway_session_key", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_live_node_processes_gateway_session_key",
        "live_node_processes",
        ["gateway_session_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_node_processes_gateway_session_key",
        table_name="live_node_processes",
    )
    op.drop_column("live_node_processes", "gateway_session_key")

    op.drop_index(
        "ix_live_deployments_ib_login_key",
        table_name="live_deployments",
    )
    op.drop_column("live_deployments", "ib_login_key")

    op.drop_table("live_deployment_strategies")
    op.drop_table("live_portfolio_revision_strategies")
    op.drop_index(
        "uq_one_draft_per_portfolio",
        table_name="live_portfolio_revisions",
    )
    op.drop_table("live_portfolio_revisions")
    op.drop_table("live_portfolios")
```

**Step 2: Run the migration**

```bash
docker exec msai-claude-backend uv run alembic upgrade head
```

Expected: `Running upgrade n2h3i4j5k6l7 -> o3i4j5k6l7m8`.

**Step 3: Round-trip down + up**

```bash
docker exec msai-claude-backend uv run alembic downgrade n2h3i4j5k6l7
docker exec msai-claude-backend uv run alembic upgrade head
```

Expected: both succeed.

**Step 4: Commit**

```bash
git add alembic/versions/o3i4j5k6l7m8_add_live_portfolio_tables.py
git commit -m "feat(migration): add live_portfolio schema (no FK cycle, partial unique draft index)"
```

---

### Task 2: `LivePortfolio` model

**Files:**

- Create: `src/msai/models/live_portfolio.py`
- Modify: `src/msai/models/__init__.py`

**Step 1: Write the failing test**

Create `tests/unit/test_live_portfolio_model.py`:

```python
"""Unit tests for the LivePortfolio model."""

from __future__ import annotations

from uuid import uuid4


def test_live_portfolio_imports_and_instantiates() -> None:
    from msai.models import LivePortfolio

    portfolio = LivePortfolio(
        id=uuid4(),
        name="Growth Portfolio",
        description="Long-only momentum",
        created_by=None,
    )
    assert portfolio.name == "Growth Portfolio"


def test_live_portfolio_name_unique_and_required() -> None:
    from msai.models import LivePortfolio

    cols = {c.name: c for c in LivePortfolio.__table__.columns}
    assert cols["name"].nullable is False
    assert cols["description"].nullable is True
    # TimestampMixin columns must be present.
    assert "created_at" in cols
    assert "updated_at" in cols
```

**Step 2: Run — must fail**

```bash
uv run pytest tests/unit/test_live_portfolio_model.py -v
```

Expected: `ImportError`.

**Step 3: Write the model**

Create `src/msai/models/live_portfolio.py`:

```python
"""LivePortfolio model — mutable identity for a live trading portfolio.

A ``live_portfolios`` row names a portfolio. The actual composition —
which strategies, at what weights, with what configs — is captured on
immutable ``live_portfolio_revisions`` rows. Rebalancing creates a new
revision; it never mutates old ones.

The "active" revision is computed on the fly by
``RevisionService.get_active_revision`` (no denormalized
``latest_revision_id`` column — avoids FK cycle + cascade-delete
complexity, trivial-cost query on an indexed column).

See ``docs/plans/2026-04-16-portfolio-per-account-live-design.md``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin


class LivePortfolio(TimestampMixin, Base):
    """A named, mutable portfolio of graduated strategies."""

    __tablename__ = "live_portfolios"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    creator: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
```

**Step 4: Register in `src/msai/models/__init__.py`**

Add import (alphabetical, after `live_node_process`):

```python
from msai.models.live_portfolio import LivePortfolio
```

Add `"LivePortfolio"` to `__all__`.

**Step 5: Run — must pass**

```bash
uv run pytest tests/unit/test_live_portfolio_model.py -v
```

Expected: 2 tests pass.

**Step 6: Commit**

```bash
git add src/msai/models/live_portfolio.py src/msai/models/__init__.py tests/unit/test_live_portfolio_model.py
git commit -m "feat(models): add LivePortfolio (TimestampMixin, no latest_revision_id pointer)"
```

---

### Task 3: `LivePortfolioRevision` model

**Files:**

- Create: `src/msai/models/live_portfolio_revision.py`
- Modify: `src/msai/models/__init__.py`

**Step 1: Write the failing test**

Create `tests/unit/test_live_portfolio_revision_model.py`:

```python
"""Unit tests for the LivePortfolioRevision model."""

from __future__ import annotations

from uuid import uuid4


def test_live_portfolio_revision_imports() -> None:
    from msai.models import LivePortfolioRevision

    rev = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=uuid4(),
        revision_number=1,
        composition_hash="a" * 64,
        is_frozen=False,
    )
    assert rev.revision_number == 1
    assert rev.is_frozen is False


def test_revision_required_columns_and_immutable_timestamp_shape() -> None:
    from msai.models import LivePortfolioRevision

    cols = {c.name: c for c in LivePortfolioRevision.__table__.columns}
    for name in ("portfolio_id", "revision_number", "composition_hash", "is_frozen"):
        assert cols[name].nullable is False, f"{name} must be NOT NULL"
    # Immutable on create — only created_at, no updated_at.
    assert "created_at" in cols
    assert "updated_at" not in cols
```

**Step 2: Run — must fail**

```bash
uv run pytest tests/unit/test_live_portfolio_revision_model.py -v
```

Expected: `ImportError`.

**Step 3: Write the model**

Create `src/msai/models/live_portfolio_revision.py`:

```python
"""LivePortfolioRevision — immutable snapshot of a portfolio composition.

The warm-restart identity boundary: any change to members/weights/configs
creates a NEW revision; existing revisions are frozen at snapshot time
and never mutated thereafter.

Immutability is a two-layer guarantee:
(1) ``RevisionService.enforce_immutability`` raises at the service
    boundary for any caller trying to mutate a frozen revision's
    members.
(2) A partial unique index ``uq_one_draft_per_portfolio`` at the DB
    level ensures at most one ``is_frozen=false`` row per portfolio.

Immutable row → no ``updated_at`` column; ``created_at`` only.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LivePortfolioRevision(Base):
    """Immutable snapshot of a portfolio's composition."""

    __tablename__ = "live_portfolio_revisions"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "revision_number",
            name="uq_live_portfolio_revisions_number",
        ),
        UniqueConstraint(
            "portfolio_id",
            "composition_hash",
            name="uq_live_portfolio_revisions_hash",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    composition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    strategies: Mapped[list["LivePortfolioRevisionStrategy"]] = relationship(  # noqa: F821
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="LivePortfolioRevisionStrategy.order_index",
        lazy="selectin",
    )
```

**Step 4: Register in `__init__.py`**

**Step 5: Run — must pass**

```bash
uv run pytest tests/unit/test_live_portfolio_revision_model.py -v
```

Expected: 2 tests pass.

**Step 6: Commit**

```bash
git add src/msai/models/live_portfolio_revision.py src/msai/models/__init__.py tests/unit/test_live_portfolio_revision_model.py
git commit -m "feat(models): add LivePortfolioRevision (immutable on freeze)"
```

---

### Task 4: `LivePortfolioRevisionStrategy` model

**Files:**

- Create: `src/msai/models/live_portfolio_revision_strategy.py`
- Modify: `src/msai/models/__init__.py`

**Step 1: Write the failing test**

Create `tests/unit/test_live_portfolio_revision_strategy_model.py`:

```python
"""Unit tests for the M:N LivePortfolioRevisionStrategy bridge."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4


def test_revision_strategy_imports() -> None:
    from msai.models import LivePortfolioRevisionStrategy

    rs = LivePortfolioRevisionStrategy(
        id=uuid4(),
        revision_id=uuid4(),
        strategy_id=uuid4(),
        config={"fast": 10},
        instruments=["AAPL.NASDAQ"],
        weight=Decimal("0.25"),
        order_index=0,
    )
    assert rs.config == {"fast": 10}


def test_revision_strategy_required_columns() -> None:
    from msai.models import LivePortfolioRevisionStrategy

    cols = {c.name: c for c in LivePortfolioRevisionStrategy.__table__.columns}
    for name in ("revision_id", "strategy_id", "config", "instruments", "weight", "order_index"):
        assert cols[name].nullable is False
    # Immutable on create — created_at only, no updated_at.
    assert "updated_at" not in cols
```

**Step 2: Run — must fail**

```bash
uv run pytest tests/unit/test_live_portfolio_revision_strategy_model.py -v
```

Expected: `ImportError`.

**Step 3: Write the model**

Create `src/msai/models/live_portfolio_revision_strategy.py`:

```python
"""LivePortfolioRevisionStrategy — M:N membership row for a portfolio revision.

One row per strategy per revision. A strategy can appear in multiple
portfolios (and multiple revisions across portfolios); uniqueness is
scoped to the revision.

Immutable on create: created_at only, no updated_at.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from decimal import Decimal  # noqa: TC003
from uuid import UUID, uuid4

from sqlalchemy import (
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LivePortfolioRevisionStrategy(Base):
    """One strategy's participation in a portfolio revision."""

    __tablename__ = "live_portfolio_revision_strategies"
    __table_args__ = (
        UniqueConstraint("revision_id", "order_index", name="uq_lprs_revision_order"),
        UniqueConstraint(
            "revision_id", "strategy_id", name="uq_lprs_revision_strategy"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_portfolio_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    revision: Mapped["LivePortfolioRevision"] = relationship(  # noqa: F821
        back_populates="strategies", lazy="selectin"
    )
    strategy: Mapped["Strategy"] = relationship(lazy="selectin")  # noqa: F821
```

**Step 4: Register in `__init__.py`**

**Step 5: Run — must pass**

```bash
uv run pytest tests/unit/test_live_portfolio_revision_strategy_model.py -v
```

**Step 6: Commit**

```bash
git add src/msai/models/live_portfolio_revision_strategy.py src/msai/models/__init__.py tests/unit/test_live_portfolio_revision_strategy_model.py
git commit -m "feat(models): add LivePortfolioRevisionStrategy (M:N bridge)"
```

---

### Task 5: `LiveDeploymentStrategy` model

**Files:**

- Create: `src/msai/models/live_deployment_strategy.py`
- Modify: `src/msai/models/__init__.py`

**Step 1: Write the failing test**

Create `tests/unit/test_live_deployment_strategy_model.py`:

```python
"""Unit tests for LiveDeploymentStrategy."""

from __future__ import annotations

from uuid import uuid4


def test_live_deployment_strategy_imports() -> None:
    from msai.models import LiveDeploymentStrategy

    lds = LiveDeploymentStrategy(
        id=uuid4(),
        deployment_id=uuid4(),
        revision_strategy_id=uuid4(),
        strategy_id_full="EMACross-abcd1234abcd1234",
    )
    assert lds.strategy_id_full == "EMACross-abcd1234abcd1234"


def test_lds_required_columns() -> None:
    from msai.models import LiveDeploymentStrategy

    cols = {c.name: c for c in LiveDeploymentStrategy.__table__.columns}
    for name in ("deployment_id", "revision_strategy_id", "strategy_id_full"):
        assert cols[name].nullable is False
    # Immutable — created_at only.
    assert "updated_at" not in cols
```

**Step 2: Run — must fail.**

```bash
uv run pytest tests/unit/test_live_deployment_strategy_model.py -v
```

**Step 3: Write the model**

Create `src/msai/models/live_deployment_strategy.py`:

```python
"""LiveDeploymentStrategy — per-deployment materialized member row.

One row per strategy per deployment, written by the supervisor at spawn
time. Provides the read path (WebSocket snapshot, /live/positions)
with the concrete ``strategy_id_full`` for each running strategy.
Immutable on create.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LiveDeploymentStrategy(Base):
    """One strategy instance inside a live deployment."""

    __tablename__ = "live_deployment_strategies"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "revision_strategy_id",
            name="uq_lds_deployment_revision_strategy",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "live_portfolio_revision_strategies.id", ondelete="RESTRICT"
        ),
        nullable=False,
        index=True,
    )
    strategy_id_full: Mapped[str] = mapped_column(String(280), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    revision_strategy: Mapped["LivePortfolioRevisionStrategy"] = relationship(  # noqa: F821
        lazy="selectin"
    )
```

**Step 4: Register in `__init__.py`.**

**Step 5: Run — must pass.**

**Step 6: Commit**

```bash
git add src/msai/models/live_deployment_strategy.py src/msai/models/__init__.py tests/unit/test_live_deployment_strategy_model.py
git commit -m "feat(models): add LiveDeploymentStrategy"
```

---

### Task 6: Add `ib_login_key` + `gateway_session_key` columns

**Files:**

- Modify: `src/msai/models/live_deployment.py`
- Modify: `src/msai/models/live_node_process.py`

**Step 1: Write the failing test**

Create `tests/unit/test_live_deployment_multi_login_columns.py`:

```python
"""Unit tests for the new multi-login routing columns (PR#1)."""

from __future__ import annotations


def test_live_deployment_has_ib_login_key_column() -> None:
    from msai.models import LiveDeployment

    cols = {c.name: c for c in LiveDeployment.__table__.columns}
    assert "ib_login_key" in cols
    assert cols["ib_login_key"].nullable is True


def test_live_node_process_has_gateway_session_key_column() -> None:
    from msai.models import LiveNodeProcess

    cols = {c.name: c for c in LiveNodeProcess.__table__.columns}
    assert "gateway_session_key" in cols
    assert cols["gateway_session_key"].nullable is True
```

**Step 2: Run — must fail.**

**Step 3: Add the columns**

In `src/msai/models/live_deployment.py` after the `account_id` block:

```python
    ib_login_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    """IB username (TWS userid) used by this deployment. The supervisor
    multiplexes logical deployments that share an ``ib_login_key`` onto
    a single Nautilus subprocess via Nautilus's multi-account
    ``exec_clients`` feature (PR #3194, 1.225+). Nullable in PR #1 —
    populated by PR #2 at deploy time, enforced NOT NULL in PR #3."""
```

In `src/msai/models/live_node_process.py` alongside sibling columns:

```python
    gateway_session_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    """Stable identifier for the (IB Gateway host, port, TWS login)
    tuple this subprocess is bound to. Per-gateway-session spawn guard
    (PR #3) filters on this key. Nullable in PR #1; populated by PR #2
    and enforced NOT NULL in PR #3."""
```

**Step 4: Run — must pass.**

**Step 5: Commit**

```bash
git add src/msai/models/live_deployment.py src/msai/models/live_node_process.py tests/unit/test_live_deployment_multi_login_columns.py
git commit -m "feat(models): add ib_login_key + gateway_session_key (PR#1 additive)"
```

---

### Task 7: Composition hash utility

**Files:**

- Create: `src/msai/services/live/portfolio_composition.py`
- Create: `tests/unit/test_portfolio_composition.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_portfolio_composition.py`:

```python
"""Unit tests for portfolio composition hashing.

The composition hash is the warm-restart identity boundary. Any change
to members/configs/instruments/weights/order produces a different hash
→ forces a cold restart.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID


_S1 = UUID("11111111-1111-1111-1111-111111111111")
_S2 = UUID("22222222-2222-2222-2222-222222222222")


def _member(
    strategy_id: UUID,
    order_index: int,
    config: dict | None = None,
    instruments: list[str] | None = None,
    weight: Decimal = Decimal("0.5"),
) -> dict:
    return {
        "strategy_id": strategy_id,
        "config": config or {"fast": 10},
        "instruments": instruments or ["AAPL.NASDAQ"],
        "weight": weight,
        "order_index": order_index,
    }


def test_hash_deterministic() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    members = [_member(_S1, 0), _member(_S2, 1)]
    assert compute_composition_hash(members) == compute_composition_hash(members)


def test_hash_stable_across_unordered_input() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0), _member(_S2, 1)]
    b = [_member(_S2, 1), _member(_S1, 0)]
    assert compute_composition_hash(a) == compute_composition_hash(b)


def test_hash_differs_on_weight_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, weight=Decimal("0.5"))]
    b = [_member(_S1, 0, weight=Decimal("0.6"))]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_config_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, config={"fast": 10})]
    b = [_member(_S1, 0, config={"fast": 12})]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_instruments_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, instruments=["AAPL.NASDAQ"])]
    b = [_member(_S1, 0, instruments=["AAPL.NASDAQ", "MSFT.NASDAQ"])]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_strategy_added() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0)]
    b = [_member(_S1, 0), _member(_S2, 1)]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_order_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0), _member(_S2, 1)]
    b = [_member(_S1, 1), _member(_S2, 0)]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_empty_stable() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    assert compute_composition_hash([]) == compute_composition_hash([])


def test_hash_decimal_weights_normalize() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, weight=Decimal("0.5"))]
    b = [_member(_S1, 0, weight=Decimal("0.50"))]
    assert compute_composition_hash(a) == compute_composition_hash(b)
```

**Step 2: Run — must fail.**

**Step 3: Write the hash utility**

Create `src/msai/services/live/portfolio_composition.py`:

```python
"""Composition hash for LivePortfolioRevision.

The hash is the warm-restart identity boundary — two revisions with the
same hash represent the SAME composition, meaning the supervisor can
warm-restart into either without state loss.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


def compute_composition_hash(members: list[dict[str, Any]]) -> str:
    """64-char sha256 hex over the canonical JSON of the sorted member list.

    Each member must contain: ``strategy_id`` (UUID), ``order_index``
    (int), ``config`` (JSON-serializable dict), ``instruments``
    (list[str]), ``weight`` (Decimal).

    Canonicalization rules:
    - sort by ``order_index`` so caller order is irrelevant
    - ``strategy_id`` → 32-char UUID hex
    - ``instruments`` → sorted, de-duped
    - ``weight`` → normalized via ``Decimal.normalize()``, then ``format(..., "f")``
      so ``0.5`` and ``0.50`` hash identically
    - ``config`` → ``sort_keys=True`` at every level
    """
    canonical = [
        _canonicalize_member(m) for m in sorted(members, key=lambda m: m["order_index"])
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonicalize_member(member: dict[str, Any]) -> dict[str, Any]:
    weight: Decimal = member["weight"]
    return {
        "strategy_id": member["strategy_id"].hex,
        "order_index": int(member["order_index"]),
        "config": member["config"],
        "instruments": sorted(set(member["instruments"])),
        "weight": format(weight.normalize(), "f"),
    }
```

**Step 4: Run — must pass.**

```bash
uv run pytest tests/unit/test_portfolio_composition.py -v
```

**Step 5: Commit**

```bash
git add src/msai/services/live/portfolio_composition.py tests/unit/test_portfolio_composition.py
git commit -m "feat(services): add composition hash utility"
```

---

### Task 8: `PortfolioService` (create + add_strategy + list + get_current_draft)

**Files:**

- Create: `src/msai/services/live/portfolio_service.py`
- Create: `tests/integration/test_portfolio_service.py`

**Step 1: Write the failing integration tests**

Create `tests/integration/test_portfolio_service.py`:

```python
"""Integration tests for PortfolioService."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import (
    Base,
    GraduationCandidate,
    LivePortfolio,
    Strategy,
    User,
)
from msai.services.live.portfolio_service import (
    PortfolioService,
    StrategyNotGraduatedError,
)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer per module — matches the repo
    convention (`test_live_node_process_model.py`, `test_heartbeat_thread.py`)."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        id=uuid4(),
        entra_id=f"p-{uuid4().hex}",
        email=f"p-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_strategy(
    session: AsyncSession, user: User, *, graduated: bool
) -> Strategy:
    strategy = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()
    if graduated:
        # GraduationCandidate requires config + metrics NOT NULL —
        # empty dicts satisfy the constraint without faking metrics.
        session.add(
            GraduationCandidate(
                id=uuid4(),
                strategy_id=strategy.id,
                stage="promoted",
                config={},
                metrics={},
            )
        )
        await session.flush()
    return strategy


@pytest.mark.asyncio
async def test_create_portfolio_has_no_draft_initially(session: AsyncSession) -> None:
    user = await _seed_user(session)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(
        name="Growth-1", description=None, created_by=user.id
    )
    await session.commit()

    assert portfolio.name == "Growth-1"
    assert await svc.get_current_draft(portfolio.id) is None


@pytest.mark.asyncio
async def test_add_strategy_creates_draft_lazily(session: AsyncSession) -> None:
    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G2", description=None, created_by=user.id)
    await svc.add_strategy(
        portfolio_id=portfolio.id,
        strategy_id=strategy.id,
        config={"fast": 10},
        instruments=["AAPL.NASDAQ"],
        weight=Decimal("0.5"),
    )
    await session.commit()

    members = await svc.list_draft_members(portfolio.id)
    assert len(members) == 1
    assert members[0].strategy_id == strategy.id
    assert members[0].order_index == 0

    draft = await svc.get_current_draft(portfolio.id)
    assert draft is not None
    assert draft.is_frozen is False


@pytest.mark.asyncio
async def test_add_ungraduated_strategy_rejected(session: AsyncSession) -> None:
    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=False)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G3", description=None, created_by=user.id)

    with pytest.raises(StrategyNotGraduatedError):
        await svc.add_strategy(
            portfolio_id=portfolio.id,
            strategy_id=strategy.id,
            config={},
            instruments=["AAPL.NASDAQ"],
            weight=Decimal("1"),
        )


@pytest.mark.asyncio
async def test_second_add_assigns_next_order_index(session: AsyncSession) -> None:
    user = await _seed_user(session)
    s1 = await _seed_strategy(session, user, graduated=True)
    s2 = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G4", description=None, created_by=user.id)
    await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("0.5"))
    await svc.add_strategy(portfolio.id, s2.id, {}, ["MSFT.NASDAQ"], Decimal("0.5"))
    await session.commit()

    members = await svc.list_draft_members(portfolio.id)
    assert [m.order_index for m in members] == [0, 1]
    assert [m.strategy_id for m in members] == [s1.id, s2.id]


@pytest.mark.asyncio
async def test_add_same_strategy_twice_raises(session: AsyncSession) -> None:
    user = await _seed_user(session)
    s1 = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G5", description=None, created_by=user.id)
    await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("1"))
    await session.commit()

    with pytest.raises(ValueError, match="already a member"):
        await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("0.5"))
```

**Step 2: Run — must fail** (`ImportError: PortfolioService`).

**Step 3: Write the service**

Create `src/msai/services/live/portfolio_service.py`:

```python
"""Portfolio service — CRUD on LivePortfolio + draft-revision mutation.

Invariants enforced:
- Only graduated strategies (promoted ``GraduationCandidate`` exists)
  can be added.
- A strategy appears at most once per revision (DB UNIQUE + service
  pre-check for better error message).
- At most one draft (``is_frozen=false``) revision per portfolio
  (DB partial unique index ``uq_one_draft_per_portfolio``).
- ``order_index`` auto-increments in insertion order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from msai.models import (
    GraduationCandidate,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class StrategyNotGraduatedError(Exception):
    """Raised when adding a strategy that has no promoted
    :class:`GraduationCandidate`."""


class PortfolioService:
    """CRUD on LivePortfolio + draft-revision management."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_portfolio(
        self,
        *,
        name: str,
        description: str | None,
        created_by: UUID | None,
    ) -> LivePortfolio:
        """Create an empty portfolio — no draft revision yet (lazily
        created by :meth:`add_strategy`)."""
        portfolio = LivePortfolio(
            name=name, description=description, created_by=created_by
        )
        self._session.add(portfolio)
        await self._session.flush()
        return portfolio

    async def add_strategy(
        self,
        portfolio_id: UUID,
        strategy_id: UUID,
        config: dict,
        instruments: list[str],
        weight: Decimal,
    ) -> LivePortfolioRevisionStrategy:
        """Add a strategy to the portfolio's draft revision.

        Raises :class:`StrategyNotGraduatedError` if the strategy has
        no promoted :class:`GraduationCandidate`. Raises ``ValueError``
        if already a member.
        """
        if not await self._is_graduated(strategy_id):
            raise StrategyNotGraduatedError(
                f"Strategy {strategy_id} has no promoted GraduationCandidate"
            )

        draft = await self._get_or_create_draft_revision(portfolio_id)

        existing = await self._session.execute(
            select(LivePortfolioRevisionStrategy.id).where(
                LivePortfolioRevisionStrategy.revision_id == draft.id,
                LivePortfolioRevisionStrategy.strategy_id == strategy_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Strategy {strategy_id} is already a member of this draft")

        order_index = await self._next_order_index(draft.id)

        member = LivePortfolioRevisionStrategy(
            revision_id=draft.id,
            strategy_id=strategy_id,
            config=config,
            instruments=instruments,
            weight=weight,
            order_index=order_index,
        )
        self._session.add(member)
        await self._session.flush()
        return member

    async def list_draft_members(
        self, portfolio_id: UUID
    ) -> list[LivePortfolioRevisionStrategy]:
        """Return the draft-revision members in insertion order.
        Empty list if no draft yet."""
        draft = await self.get_current_draft(portfolio_id)
        if draft is None:
            return []
        result = await self._session.execute(
            select(LivePortfolioRevisionStrategy)
            .where(LivePortfolioRevisionStrategy.revision_id == draft.id)
            .order_by(LivePortfolioRevisionStrategy.order_index)
        )
        return list(result.scalars().all())

    async def get_current_draft(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision | None:
        """Public accessor — returns the portfolio's unfrozen revision,
        or ``None`` if no draft yet.

        The partial unique index ``uq_one_draft_per_portfolio``
        guarantees there is at most one.
        """
        result = await self._session.execute(
            select(LivePortfolioRevision).where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _is_graduated(self, strategy_id: UUID) -> bool:
        result = await self._session.execute(
            select(GraduationCandidate.id).where(
                GraduationCandidate.strategy_id == strategy_id,
                GraduationCandidate.stage == "promoted",
            )
        )
        return result.first() is not None

    async def _get_or_create_draft_revision(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision:
        """Return the existing draft, or create a new one.

        The partial unique index ``uq_one_draft_per_portfolio``
        guarantees at most one draft per portfolio; a concurrent caller
        losing the race will catch an ``IntegrityError`` on flush and
        the retry will find the winner's draft via
        :meth:`get_current_draft`. For PR #1 the service is not
        intended to be called concurrently on the same portfolio —
        the DB index is defense in depth.
        """
        existing = await self.get_current_draft(portfolio_id)
        if existing is not None:
            return existing

        max_number = (
            await self._session.execute(
                select(func.coalesce(func.max(LivePortfolioRevision.revision_number), 0))
                .where(LivePortfolioRevision.portfolio_id == portfolio_id)
            )
        ).scalar_one()

        draft = LivePortfolioRevision(
            portfolio_id=portfolio_id,
            revision_number=int(max_number) + 1,
            # Placeholder — replaced by real hash when RevisionService
            # snapshots the draft. Safe because no UNIQUE constraint
            # across ``composition_hash`` applies to unfrozen rows
            # (UNIQUE(portfolio_id, composition_hash) is enforced for
            # ALL rows, but the partial draft-uniqueness index ensures
            # at most one draft per portfolio, which in turn means at
            # most one placeholder hash per portfolio).
            composition_hash="0" * 64,
            is_frozen=False,
        )
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def _next_order_index(self, revision_id: UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(LivePortfolioRevisionStrategy.order_index), -1))
            .where(LivePortfolioRevisionStrategy.revision_id == revision_id)
        )
        return int(result.scalar_one()) + 1
```

**Step 4: Run — must pass.**

```bash
uv run pytest tests/integration/test_portfolio_service.py -v
```

**Step 5: Commit**

```bash
git add src/msai/services/live/portfolio_service.py tests/integration/test_portfolio_service.py
git commit -m "feat(services): add PortfolioService with graduated-strategy guard + public draft accessor"
```

---

### Task 9: `RevisionService` (snapshot + active + immutability guard)

**Files:**

- Create: `src/msai/services/live/revision_service.py`
- Create: `tests/integration/test_revision_service.py`

**Step 1: Design notes**

- `snapshot(portfolio_id)`:
  - Finds the current draft.
  - Computes composition hash.
  - If a frozen revision of the **same portfolio** already has that hash, **deletes the current draft** and returns the existing revision (identical composition collapses to one identity).
  - Otherwise writes the hash + flips `is_frozen=True`.

- `get_active_revision(portfolio_id)`:
  - Queries for the portfolio's highest `revision_number` where `is_frozen=true`. No denormalized pointer.

- `enforce_immutability(revision_id)`:
  - Raises `RevisionImmutableError` if `is_frozen`. Defensive — no mutation paths exist in PR #1, but the contract is pinned so PR #2 callers fail loud.

**Step 2: Write the failing tests**

Create `tests/integration/test_revision_service.py`:

```python
"""Integration tests for RevisionService."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, GraduationCandidate, Strategy, User
from msai.services.live.portfolio_service import PortfolioService
from msai.services.live.revision_service import (
    RevisionImmutableError,
    RevisionService,
)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer per module — matches the repo
    convention (`test_live_node_process_model.py`, `test_heartbeat_thread.py`)."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_portfolio_with_one_graduated_strategy(
    session: AsyncSession,
) -> tuple:
    user = User(
        id=uuid4(),
        entra_id=f"r-{uuid4().hex}",
        email=f"r-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    strategy = Strategy(
        id=uuid4(),
        name=f"r-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()
    session.add(
        GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="promoted",
            config={},
            metrics={},
        )
    )
    await session.flush()

    psvc = PortfolioService(session)
    portfolio = await psvc.create_portfolio(
        name=f"P-{uuid4().hex[:8]}", description=None, created_by=user.id
    )
    await psvc.add_strategy(
        portfolio.id, strategy.id, {"fast": 10}, ["AAPL.NASDAQ"], Decimal("1")
    )
    return portfolio, strategy, user


@pytest.mark.asyncio
async def test_snapshot_freezes_draft_and_advances_number(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)
    psvc = PortfolioService(session)

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    assert revision.is_frozen is True
    assert revision.composition_hash != "0" * 64
    assert len(revision.composition_hash) == 64
    assert revision.revision_number == 1

    # No more draft after snapshot.
    assert await psvc.get_current_draft(portfolio.id) is None


@pytest.mark.asyncio
async def test_snapshot_same_composition_returns_existing_revision(
    session: AsyncSession,
) -> None:
    """Two snapshots with identical composition collapse to the same
    revision (no duplicate frozen rows)."""
    portfolio, strategy, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)
    psvc = PortfolioService(session)

    first = await rsvc.snapshot(portfolio.id)
    await session.commit()

    await psvc.add_strategy(
        portfolio.id, strategy.id, {"fast": 10}, ["AAPL.NASDAQ"], Decimal("1")
    )
    await session.commit()

    second = await rsvc.snapshot(portfolio.id)
    await session.commit()

    assert second.id == first.id


@pytest.mark.asyncio
async def test_get_active_revision_returns_latest_frozen(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)

    assert await rsvc.get_active_revision(portfolio.id) is None

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    active = await rsvc.get_active_revision(portfolio.id)
    assert active is not None
    assert active.id == revision.id


@pytest.mark.asyncio
async def test_enforce_immutability_raises_for_frozen(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    with pytest.raises(RevisionImmutableError):
        await rsvc.enforce_immutability(revision.id)


@pytest.mark.asyncio
async def test_enforce_immutability_noop_for_draft(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    psvc = PortfolioService(session)
    rsvc = RevisionService(session)

    draft = await psvc.get_current_draft(portfolio.id)
    assert draft is not None

    await rsvc.enforce_immutability(draft.id)  # must not raise


@pytest.mark.asyncio
async def test_snapshot_raises_when_no_draft(session: AsyncSession) -> None:
    """Calling snapshot on a portfolio with no draft is a programming
    error, not a silent no-op."""
    user = User(
        id=uuid4(),
        entra_id=f"nd-{uuid4().hex}",
        email=f"nd-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    from msai.models import LivePortfolio

    portfolio = LivePortfolio(
        id=uuid4(), name="Empty", description=None, created_by=user.id
    )
    session.add(portfolio)
    await session.commit()

    rsvc = RevisionService(session)
    with pytest.raises(ValueError, match="no draft"):
        await rsvc.snapshot(portfolio.id)
```

**Step 3: Run — must fail.**

**Step 4: Write the service**

Create `src/msai/services/live/revision_service.py`:

```python
"""Revision service — snapshot (freeze) + active lookup + immutability guard.

No denormalized ``latest_revision_id`` pointer — the active revision
is computed on demand via a query ordered by ``revision_number`` desc
with ``is_frozen=true``. The FK would otherwise form a cycle against
``live_portfolio_revisions.portfolio_id`` and complicate
``Base.metadata.drop_all/create_all`` fixtures.

Immutability is two-layer: the ``is_frozen`` boolean drives
:meth:`enforce_immutability`, and a partial unique index at the DB
level ensures at most one unfrozen row per portfolio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from msai.models import LivePortfolioRevision, LivePortfolioRevisionStrategy
from msai.services.live.portfolio_composition import compute_composition_hash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RevisionImmutableError(Exception):
    """Raised when a caller attempts to mutate a frozen revision."""


class RevisionService:
    """Freeze drafts into immutable revisions + fetch active + guard."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, portfolio_id: UUID) -> LivePortfolioRevision:
        """Freeze the portfolio's draft into a hashed, numbered revision.

        If an existing frozen revision of the same portfolio has the
        same composition hash, the draft is deleted and the existing
        revision is returned (identical compositions collapse).

        Raises ``ValueError`` if there is no draft to snapshot.

        Concurrency: uses ``SELECT … FOR UPDATE`` on the draft row so
        two concurrent ``snapshot`` callers on the same portfolio
        serialize. Without this, caller B could load the draft while
        caller A is mid-flush, observe A's just-frozen row as
        "existing with matching hash", and delete it via
        ``session.delete(draft)`` — because it's the SAME row that's
        already been frozen.

        After the lock releases (A commits with ``is_frozen=True``),
        B's ``_lock_draft_revision`` query — which filters
        ``is_frozen = false`` — no longer matches the now-frozen row
        and returns ``None``. ``snapshot`` then raises ``ValueError``.
        The caller is expected to recover by calling
        :meth:`get_active_revision` to retrieve A's frozen revision;
        we deliberately do NOT silently return it here because a
        ``snapshot`` call that finds no draft to freeze is a semantic
        error, not a no-op.
        """
        draft = await self._lock_draft_revision(portfolio_id)
        if draft is None:
            # No unfrozen row — either the portfolio never had a draft
            # OR a concurrent snapshot already froze it. Surface a
            # clean error so the caller retries via
            # ``get_active_revision`` rather than silently treating
            # "nothing to snapshot" as success.
            raise ValueError(
                f"Portfolio {portfolio_id} has no draft revision to snapshot"
            )

        members = (
            (
                await self._session.execute(
                    select(LivePortfolioRevisionStrategy)
                    .where(LivePortfolioRevisionStrategy.revision_id == draft.id)
                    .order_by(LivePortfolioRevisionStrategy.order_index)
                )
            )
            .scalars()
            .all()
        )

        computed_hash = compute_composition_hash(
            [
                {
                    "strategy_id": m.strategy_id,
                    "order_index": m.order_index,
                    "config": m.config,
                    "instruments": list(m.instruments),
                    "weight": m.weight,
                }
                for m in members
            ]
        )

        existing = (
            await self._session.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id,
                    LivePortfolioRevision.is_frozen.is_(True),
                    LivePortfolioRevision.composition_hash == computed_hash,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            await self._session.delete(draft)
            await self._session.flush()
            return existing

        draft.composition_hash = computed_hash
        draft.is_frozen = True
        await self._session.flush()
        return draft

    async def get_active_revision(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision | None:
        """Return the portfolio's latest frozen revision, or ``None``."""
        result = await self._session.execute(
            select(LivePortfolioRevision)
            .where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(True),
            )
            .order_by(LivePortfolioRevision.revision_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def enforce_immutability(self, revision_id: UUID) -> None:
        """Raise :class:`RevisionImmutableError` if the revision is frozen.

        Call at the top of any method that mutates member rows under
        ``revision_id``. Drafts pass silently.
        """
        revision = await self._session.get(LivePortfolioRevision, revision_id)
        if revision is None:
            raise ValueError(f"Revision {revision_id} not found")
        if revision.is_frozen:
            raise RevisionImmutableError(
                f"Revision {revision_id} is frozen and cannot be mutated"
            )

    # ------------------------------------------------------------------

    async def _lock_draft_revision(
        self, portfolio_id: UUID
    ) -> LivePortfolioRevision | None:
        """``SELECT … FOR UPDATE`` on the portfolio's draft row.

        Blocks concurrent snapshot callers on the same portfolio
        until the current transaction commits. ``.with_for_update()``
        takes a row-level lock that's released on commit/rollback.
        """
        result = await self._session.execute(
            select(LivePortfolioRevision)
            .where(
                LivePortfolioRevision.portfolio_id == portfolio_id,
                LivePortfolioRevision.is_frozen.is_(False),
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()
```

**Step 5: Run — must pass.**

```bash
uv run pytest tests/integration/test_revision_service.py -v
```

**Step 6: Commit**

```bash
git add src/msai/services/live/revision_service.py tests/integration/test_revision_service.py
git commit -m "feat(services): add RevisionService (snapshot + active lookup + immutability guard)"
```

---

### Task 10: Full-lifecycle integration smoke test

**Files:**

- Create: `tests/integration/test_portfolio_full_lifecycle.py`

**Step 1: Write the test**

Create `tests/integration/test_portfolio_full_lifecycle.py`:

```python
"""Full-lifecycle integration test — exercises every PR#1 surface.

No FK cycle: the portfolio can be deleted and CASCADE removes
revisions + member rows in one step, without needing to null any
back-pointer first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import (
    Base,
    GraduationCandidate,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
    Strategy,
    User,
)
from msai.services.live.portfolio_service import PortfolioService
from msai.services.live.revision_service import RevisionService


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer per module — matches the repo
    convention (`test_live_node_process_model.py`, `test_heartbeat_thread.py`)."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_lifecycle_create_add_snapshot_rebalance(
    session: AsyncSession,
) -> None:
    user = User(
        id=uuid4(),
        entra_id=f"full-{uuid4().hex}",
        email=f"full-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategies = []
    for _ in range(3):
        strat = Strategy(
            id=uuid4(),
            name=f"s-{uuid4().hex[:8]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strat)
        await session.flush()
        session.add(
            GraduationCandidate(
                id=uuid4(),
                strategy_id=strat.id,
                stage="promoted",
                config={},
                metrics={},
            )
        )
        await session.flush()
        strategies.append(strat)

    psvc = PortfolioService(session)
    rsvc = RevisionService(session)

    portfolio = await psvc.create_portfolio(
        name="Full-Lifecycle", description="End-to-end", created_by=user.id
    )
    for i, strat in enumerate(strategies):
        await psvc.add_strategy(
            portfolio.id,
            strat.id,
            {"fast": 10 + i},
            [f"SYM{i}.NASDAQ"],
            Decimal("0.333333"),
        )
    await session.commit()

    rev1 = await rsvc.snapshot(portfolio.id)
    await session.commit()
    assert rev1.is_frozen is True
    assert rev1.revision_number == 1

    # Start a new draft: add 2 strategies with different weights.
    await psvc.add_strategy(
        portfolio.id, strategies[0].id, {"fast": 10}, ["SYM0.NASDAQ"], Decimal("0.5")
    )
    await psvc.add_strategy(
        portfolio.id, strategies[1].id, {"fast": 11}, ["SYM1.NASDAQ"], Decimal("0.5")
    )
    await session.commit()

    rev2 = await rsvc.snapshot(portfolio.id)
    await session.commit()
    assert rev2.id != rev1.id
    assert rev2.revision_number == 2
    assert rev2.composition_hash != rev1.composition_hash

    # get_active_revision returns the latest frozen.
    active = await rsvc.get_active_revision(portfolio.id)
    assert active is not None
    assert active.id == rev2.id

    # rev1 is preserved as audit history with its 3 members.
    rev1_members = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == rev1.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rev1_members) == 3


@pytest.mark.asyncio
async def test_deleting_portfolio_cascades_cleanly(session: AsyncSession) -> None:
    """FK ondelete=CASCADE removes revisions and their member rows in
    one DELETE. No pointer-nulling workaround needed — there's no FK
    cycle in this schema."""
    user = User(
        id=uuid4(),
        entra_id=f"casc-{uuid4().hex}",
        email=f"casc-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    strat = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strat)
    await session.flush()
    session.add(
        GraduationCandidate(
            id=uuid4(),
            strategy_id=strat.id,
            stage="promoted",
            config={},
            metrics={},
        )
    )
    await session.flush()

    psvc = PortfolioService(session)
    rsvc = RevisionService(session)
    portfolio = await psvc.create_portfolio(
        name="ToDelete", description=None, created_by=user.id
    )
    await psvc.add_strategy(
        portfolio.id, strat.id, {}, ["AAPL.NASDAQ"], Decimal("1")
    )
    rev = await rsvc.snapshot(portfolio.id)
    await session.commit()

    portfolio_id = portfolio.id
    revision_id = rev.id
    await session.delete(portfolio)
    await session.commit()

    remaining_revisions = (
        (
            await session.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining_revisions == []

    remaining_members = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == revision_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining_members == []
```

**Step 2: Run — must pass.**

**Step 3: Commit**

```bash
git add tests/integration/test_portfolio_full_lifecycle.py
git commit -m "test(integration): full-lifecycle test for PR#1 portfolio layer"
```

---

### Task 11: Alembic migration test (upgrade/downgrade round-trip)

**Files:**

- Modify: `tests/integration/test_alembic_migrations.py`

**Step 1: Read the existing harness**

```bash
sed -n '1,100p' tests/integration/test_alembic_migrations.py
```

Repo convention (iter-2 review finding):

- Fixture: module-scoped `isolated_postgres_url` with its own `PostgresContainer`.
- Runner: `_run_alembic(database_url, "upgrade", "head")` — **subprocess**, not in-process `command.upgrade()`. `alembic/env.py` calls `asyncio.run(...)` which clashes with `pytest-asyncio` if invoked in-process.
- Inspection: `create_async_engine(url)` + `async with engine.connect() as conn` + `await conn.run_sync(sa.inspect)` — not a sync inspect on a raw connection.

**Step 2: Append the test**

Append the following at the end of `tests/integration/test_alembic_migrations.py`:

```python
@pytest.mark.asyncio
async def test_o3_portfolio_schema_roundtrip(isolated_postgres_url: str) -> None:
    """PR #1 schema: new tables + new columns + partial unique index land
    on upgrade; downgrade removes them cleanly; re-upgrade works."""
    _run_alembic(isolated_postgres_url, "upgrade", "head")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            def _collect(sync_conn: sa.Connection) -> dict:
                insp = sa.inspect(sync_conn)
                return {
                    "tables": set(insp.get_table_names()),
                    "dep_cols": {c["name"] for c in insp.get_columns("live_deployments")},
                    "proc_cols": {c["name"] for c in insp.get_columns("live_node_processes")},
                    "rev_indexes": {
                        idx["name"]
                        for idx in insp.get_indexes("live_portfolio_revisions")
                    },
                }
            state = await conn.run_sync(_collect)
        assert "live_portfolios" in state["tables"]
        assert "live_portfolio_revisions" in state["tables"]
        assert "live_portfolio_revision_strategies" in state["tables"]
        assert "live_deployment_strategies" in state["tables"]
        assert "ib_login_key" in state["dep_cols"]
        assert "gateway_session_key" in state["proc_cols"]
        assert "uq_one_draft_per_portfolio" in state["rev_indexes"]
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "downgrade", "n2h3i4j5k6l7")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            tables_after_down = await conn.run_sync(
                lambda sc: set(sa.inspect(sc).get_table_names())
            )
        assert "live_portfolios" not in tables_after_down
        assert "live_portfolio_revisions" not in tables_after_down
        assert "live_portfolio_revision_strategies" not in tables_after_down
        assert "live_deployment_strategies" not in tables_after_down
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "upgrade", "head")
```

**Step 3: Run — must pass**

```bash
uv run pytest tests/integration/test_alembic_migrations.py::test_o3_portfolio_schema_roundtrip -v
```

**Step 4: Commit**

```bash
git add tests/integration/test_alembic_migrations.py
git commit -m "test(migration): add o3 portfolio-schema round-trip test (subprocess harness)"
```

---

### Task 12: Full sweep (lint + tests + mypy)

**Step 1: Run the full unit suite**

```bash
uv run pytest tests/unit -q
```

Expected: 1209 existing + ~28 new = ≈ 1237 pass.

**Step 2: Run the full integration suite**

```bash
uv run pytest tests/integration -q
```

Expected: pre-existing + 13 new (5 portfolio + 6 revision + 2 full lifecycle + 1 migration).

**Step 3: Lint + type check**

```bash
uv run ruff check src/msai/models/ src/msai/services/live/ tests/unit/test_live_portfolio_model.py tests/unit/test_live_portfolio_revision_model.py tests/unit/test_live_portfolio_revision_strategy_model.py tests/unit/test_live_deployment_strategy_model.py tests/unit/test_live_deployment_multi_login_columns.py tests/unit/test_portfolio_composition.py tests/integration/test_portfolio_service.py tests/integration/test_revision_service.py tests/integration/test_portfolio_full_lifecycle.py
uv run mypy --strict src/msai/models/live_portfolio.py src/msai/models/live_portfolio_revision.py src/msai/models/live_portfolio_revision_strategy.py src/msai/models/live_deployment_strategy.py src/msai/services/live/portfolio_composition.py src/msai/services/live/portfolio_service.py src/msai/services/live/revision_service.py
```

**Step 4: Final migration round-trip**

```bash
docker exec msai-claude-backend uv run alembic downgrade base
docker exec msai-claude-backend uv run alembic upgrade head
```

**Step 5: Commit any polish**

```bash
git add -A
git commit -m "chore: lint + type polish" || echo "nothing to polish"
```

---

## Done definition

- All 12 tasks complete, each its own commit.
- 28 new unit tests + 13 new integration tests passing.
- Alembic upgrade/downgrade round-trip clean on a fresh DB.
- `ruff check` clean on new files.
- `mypy --strict` clean on new files.
- No FK cycle. No changes to `/api/v1/live/*`, `supervisor`, `trading_node_subprocess`, `websocket`, or read-path code.
- Plan-review loop passes clean on re-review (iteration 2 fixes applied).

## Reference skills

- Red-Green-Refactor discipline: superpowers:test-driven-development
- Workflow state: `## Workflow` section in `CONTINUITY.md`
- Nautilus gotchas: none touched by this PR (DB + services only)
