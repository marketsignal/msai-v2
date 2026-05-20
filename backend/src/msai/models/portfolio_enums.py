"""String enums for portfolio objectives and run status.

Kept in a dedicated module so Pydantic schemas, SQLAlchemy models, and
service-layer code can share a single source of truth without circular
imports.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SQLEnum


class PortfolioObjective(StrEnum):
    """How per-candidate weights are derived when not explicitly set.

    The operator picks one of these when creating a portfolio.  Every
    objective is handled by :func:`msai.services.portfolio.computation.heuristic_weight`;
    an unknown objective raises instead of silently falling back to 1.0.
    """

    EQUAL_WEIGHT = "equal_weight"
    MANUAL = "manual"
    MAXIMIZE_PROFIT = "maximize_profit"
    MAXIMIZE_SHARPE = "maximize_sharpe"
    MAXIMIZE_SORTINO = "maximize_sortino"
    MAXIMIZE_CALMAR = "maximize_calmar"
    MINIMIZE_MAX_DRAWDOWN = "minimize_max_drawdown"


class PortfolioRunStatus(StrEnum):
    """Lifecycle states of a :class:`PortfolioRun`.

    The state machine is::

        pending ──▶ running ──▶ completed
                     │
                     ├──▶ failed
                     │
                     └──▶ canceled

    Terminal states (``completed``, ``failed``, ``canceled``) are sticky —
    the service refuses to transition out of them to protect against arq
    retry loops. ``canceled`` is the explicit operator-cancel terminal
    state introduced for Quick/Full backtest mode (the kill path).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """True when the run has reached a final state."""
        return self in (
            PortfolioRunStatus.COMPLETED,
            PortfolioRunStatus.FAILED,
            PortfolioRunStatus.CANCELED,
        )


class BacktestMode(StrEnum):
    """Two backtest modes per PRD: Quick (single-shot) and Full (optimization).

    Stored as a Postgres ``backtestmode`` ENUM (see migration
    ``b063ef2dd543_portfolio_backtest_extensions``). The ``str`` mixin keeps
    raw-SQL parameter binding ergonomic and lets routers/JSON serialisers
    treat the value as a plain string.
    """

    QUICK = "quick"
    FULL = "full"


def _backtest_mode_values(enum_cls: type[BacktestMode]) -> list[str]:
    """Return the lowercase enum *values* (not names) for SQLAlchemy.

    SQLAlchemy's default behaviour with a Python ``Enum`` is to use the
    member *name* ("QUICK", "FULL") as the SQL label, which would clash
    with the migration's ``CREATE TYPE backtestmode AS ENUM ('quick',
    'full')`` and produce ``invalid input value for enum`` errors at
    runtime. ``values_callable`` forces SQLAlchemy to use the value side
    of the ``StrEnum`` so both paths agree on lowercase labels.
    """

    return [m.value for m in enum_cls]


# Shared SQLAlchemy column type. Defined once at module level so the two
# columns (``portfolios.default_mode`` and ``portfolio_runs.mode``) reuse
# the same instance — SQLAlchemy de-dupes ENUM creation in metadata when
# the type is shared, so ``metadata.create_all`` issues a single
# ``CREATE TYPE`` even for multi-table use.
BACKTEST_MODE_DB_TYPE: SQLEnum = SQLEnum(
    BacktestMode,
    name="backtestmode",
    values_callable=_backtest_mode_values,
    create_type=True,
)
