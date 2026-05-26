"""Portfolio model — a named collection of graduated strategies with capital allocation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin
from msai.models.portfolio_enums import BACKTEST_MODE_DB_TYPE, BacktestMode

if TYPE_CHECKING:
    from msai.models.user import User


# Partial unique index scoped to the smoke sentinel name. Code-review iter-1
# fix #3: the smoke runner's SELECT-or-CREATE bootstrap is racy without a
# database-level uniqueness constraint — two concurrent run_smoke calls can
# each miss the lookup and create their own canonical row. Operator-created
# portfolios are NOT affected (the partial-index WHERE clause restricts the
# constraint to the literal sentinel only). Mirrors the c3d4e5f6a7b8
# migration so Base.metadata.create_all (testcontainer schema) also enforces
# the constraint; the model is the single source of truth for this index.
_SMOKE_SENTINEL_NAME = "__msai_smoke__"

# The partial-index WHERE clause is built with ``text(...)`` so SQLAlchemy
# emits exactly the SQL fragment we want regardless of whether ``name``
# evaluates to a ``MappedColumn`` (class-body) or an
# ``InstrumentedAttribute`` (post-Mapper-configure) at metadata-build time.
_SMOKE_SENTINEL_WHERE = text(f"name = '{_SMOKE_SENTINEL_NAME}'")


class Portfolio(TimestampMixin, Base):
    """A portfolio that allocates capital across graduated strategy candidates.

    ``objective`` describes the optimization goal — one of
    :class:`msai.models.portfolio_enums.PortfolioObjective`
    (``equal_weight``, ``manual``, ``maximize_profit``, ``maximize_sharpe``,
    ``maximize_sortino``, ``maximize_calmar``, ``minimize_max_drawdown``).
    ``base_capital`` is the starting notional, and ``requested_leverage`` is
    the target leverage multiplier.

    Safety caps (``max_position_size``, ``max_drawdown_halt``) and the
    selected ``allocator_name`` are the safety/sizing knobs honored by both
    backtest and live paths. ``default_mode`` selects Quick (single-shot)
    vs. Full (walk-forward + optimizer) when ``/runs`` is called without an
    explicit override.
    """

    __tablename__ = "portfolios"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective: Mapped[str] = mapped_column(String(64), nullable=False)
    base_capital: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    requested_leverage: Mapped[float] = mapped_column(
        Numeric(8, 4), nullable=False, server_default="1.0"
    )
    downside_target: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # B3 safety caps + mode selection. ``max_position_size`` / ``max_drawdown_halt``
    # are fractions of base_capital (Numeric(8,4) -> values in [0, 9999.9999], but
    # the schema layer enforces (0, 1]).  ``default_mode`` is the Postgres
    # ``backtestmode`` ENUM (see migration b063ef2dd543).  ``allocator_name``
    # picks the weight rule honored by the portfolio backtest engine.
    max_position_size: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    max_drawdown_halt: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    # Shared BACKTEST_MODE_DB_TYPE so SQLAlchemy de-dupes the ENUM creation
    # across both Portfolio + PortfolioRun on ``Base.metadata.create_all``.
    # ``values_callable`` on the shared type forces lowercase labels to
    # match the migration's ``CREATE TYPE backtestmode AS ENUM
    # ('quick','full')``.
    default_mode: Mapped[BacktestMode] = mapped_column(
        BACKTEST_MODE_DB_TYPE,
        nullable=False,
        default=BacktestMode.QUICK,
        server_default=BacktestMode.QUICK.value,
    )
    allocator_name: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="equal_weight",
        server_default="equal_weight",
    )
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )

    # See module-level docstring for ``_SMOKE_SENTINEL_NAME``. Declared via
    # ``__table_args__`` (not ``mapped_column(unique=True)``) because the
    # constraint is PARTIAL — operator portfolios share the ``name`` column
    # without a uniqueness requirement, but two concurrent smoke bootstraps
    # race here on the database side so exactly one wins; the loser catches
    # IntegrityError and re-SELECTs in ``services/smoke/runner.py``.
    __table_args__ = (
        Index(
            "uq_portfolios_smoke_sentinel",
            "name",
            unique=True,
            postgresql_where=_SMOKE_SENTINEL_WHERE,
        ),
    )

    # Relationships
    creator: Mapped[User] = relationship(lazy="selectin")  # noqa: F821
