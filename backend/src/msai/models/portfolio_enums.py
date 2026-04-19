"""String enums for portfolio objectives and run status.

Kept in a dedicated module so Pydantic schemas, SQLAlchemy models, and
service-layer code can share a single source of truth without circular
imports.
"""

from __future__ import annotations

from enum import StrEnum


class PortfolioObjective(StrEnum):
    """How per-candidate weights are derived when not explicitly set.

    The operator picks one of these when creating a portfolio.  Every
    objective is handled by :func:`msai.services.portfolio_service._heuristic_weight`;
    an unknown objective raises instead of silently falling back to 1.0.
    """

    EQUAL_WEIGHT = "equal_weight"
    MANUAL = "manual"
    MAXIMIZE_PROFIT = "maximize_profit"
    MAXIMIZE_SHARPE = "maximize_sharpe"
    MAXIMIZE_SORTINO = "maximize_sortino"


class PortfolioRunStatus(StrEnum):
    """Lifecycle states of a :class:`PortfolioRun`.

    The state machine is::

        pending ──▶ running ──▶ completed
                     │
                     └──▶ failed

    Terminal states (``completed``, ``failed``) are sticky — the service
    refuses to transition out of them to protect against arq retry loops.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True when the run has reached a final state."""
        return self in (PortfolioRunStatus.COMPLETED, PortfolioRunStatus.FAILED)
