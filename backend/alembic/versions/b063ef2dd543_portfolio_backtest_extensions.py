"""portfolio_backtest_extensions — add Quick/Full mode, safety caps, optimizer trace.

Adds:
- 2 new PortfolioObjective enum values: MAXIMIZE_CALMAR, MINIMIZE_MAX_DRAWDOWN
- 1 new PortfolioRunStatus enum value: CANCELED
- 4 new Portfolio columns: max_position_size, max_drawdown_halt, default_mode, allocator_name
- 5 new PortfolioRun columns: mode, optimization_trace, walk_forward_payload,
  is_metric, oos_metric

Backwards-compatible: existing rows get sensible defaults (allocator_name='equal_weight' for
existing portfolios that have no explicit allocator). No data migration beyond defaults.

Note on enum value drops in downgrade: PostgreSQL has no ``ALTER TYPE ... DROP VALUE`` —
the new enum members survive a downgrade. Per project additive-only migration policy this
is acceptable: rolling back image SHAs leaves the broader enum harmlessly in place.

Revision ID: b063ef2dd543
Revises: c7d8e9f1a2b3
Create Date: 2026-05-18 13:43:36.389736
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b063ef2dd543"
down_revision: str | None = "c7d8e9f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOTE: PortfolioObjective + PortfolioRunStatus are stored as plain VARCHAR
    # columns (not Postgres ENUM types) — see Portfolio.objective: String(64)
    # and PortfolioRun.status: String(32). The new ``maximize_calmar``,
    # ``minimize_max_drawdown``, and ``canceled`` values are added at the
    # Python ``StrEnum`` layer only; the database stores any string. No
    # ``ALTER TYPE ... ADD VALUE`` is needed for those.

    # Create the new BacktestMode enum type — this one IS a Postgres ENUM so
    # the new ``default_mode``/``mode`` columns can be typed correctly and
    # protected at the DB layer.
    backtest_mode = postgresql.ENUM("quick", "full", name="backtestmode")
    backtest_mode.create(op.get_bind(), checkfirst=True)

    # Add Portfolio columns
    op.add_column(
        "portfolios",
        sa.Column("max_position_size", sa.Numeric(8, 4), nullable=True),
    )
    op.add_column(
        "portfolios",
        sa.Column("max_drawdown_halt", sa.Numeric(8, 4), nullable=True),
    )
    op.add_column(
        "portfolios",
        sa.Column(
            "default_mode",
            sa.Enum("quick", "full", name="backtestmode", create_type=False),
            nullable=False,
            server_default="quick",
        ),
    )
    op.add_column(
        "portfolios",
        sa.Column(
            "allocator_name",
            sa.String(32),
            nullable=False,
            server_default="equal_weight",
        ),
    )

    # Add PortfolioRun columns
    op.add_column(
        "portfolio_runs",
        sa.Column(
            "mode",
            sa.Enum("quick", "full", name="backtestmode", create_type=False),
            nullable=False,
            server_default="quick",
        ),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("optimization_trace", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("walk_forward_payload", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("is_metric", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("oos_metric", sa.Numeric(18, 6), nullable=True),
    )

    # Extend the existing ``ck_portfolio_runs_status`` CHECK constraint to
    # accept the new ``canceled`` terminal state.  The Python ``PortfolioRunStatus``
    # StrEnum gained ``CANCELED`` in this revision (B1), but the DB-level CHECK
    # constraint added by ``l0f1g2h3i4j5_portfolio_orchestration_columns.py``
    # still enumerates only ``pending|running|completed|failed`` — any UPDATE
    # of ``status`` to ``'canceled'`` raises a ``CheckViolationError`` and the
    # cancel endpoint returns 500.  Drop + recreate is the standard Postgres
    # pattern (``ALTER ... DROP CONSTRAINT`` then ``ALTER ... ADD CONSTRAINT``)
    # since CHECK constraints don't support in-place modification.
    op.execute("ALTER TABLE portfolio_runs DROP CONSTRAINT IF EXISTS ck_portfolio_runs_status")
    op.create_check_constraint(
        "ck_portfolio_runs_status",
        "portfolio_runs",
        "status IN ('pending', 'running', 'completed', 'failed', 'canceled')",
    )


def downgrade() -> None:
    # Restore the prior CHECK constraint shape (without ``canceled``).  If any
    # row has status='canceled' at downgrade time the recreation will fail —
    # callers MUST flip those rows to ``failed`` first (or accept the failure
    # and manually clean up).  Per the additive-only migration policy
    # (``rules/database.md``) and the file-level docstring this downgrade is
    # best-effort; the StrEnum value drop is not supported either way.
    op.execute("ALTER TABLE portfolio_runs DROP CONSTRAINT IF EXISTS ck_portfolio_runs_status")
    op.create_check_constraint(
        "ck_portfolio_runs_status",
        "portfolio_runs",
        "status IN ('pending', 'running', 'completed', 'failed')",
    )

    # Reverse-order drop of the columns. Enum value rollback is non-trivial in
    # PostgreSQL (no DROP VALUE) — we accept the new enum values as one-way
    # additive changes per project convention; the columns can still be dropped.
    op.drop_column("portfolio_runs", "oos_metric")
    op.drop_column("portfolio_runs", "is_metric")
    op.drop_column("portfolio_runs", "walk_forward_payload")
    op.drop_column("portfolio_runs", "optimization_trace")
    op.drop_column("portfolio_runs", "mode")
    op.drop_column("portfolios", "allocator_name")
    op.drop_column("portfolios", "default_mode")
    op.drop_column("portfolios", "max_drawdown_halt")
    op.drop_column("portfolios", "max_position_size")

    backtest_mode = postgresql.ENUM(name="backtestmode")
    backtest_mode.drop(op.get_bind(), checkfirst=True)
