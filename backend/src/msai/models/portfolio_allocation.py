"""PortfolioAllocation model — weight assignment of a graduation candidate within a portfolio."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class PortfolioAllocation(Base):
    """A single weight allocation of a graduation candidate within a portfolio.

    Each portfolio has one allocation per candidate (enforced by the unique
    constraint on ``(portfolio_id, candidate_id)``).  ``weight`` is a decimal
    between 0 and 1 representing the fraction of portfolio capital allocated.
    """

    __tablename__ = "portfolio_allocations"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "candidate_id", name="uq_portfolio_candidate"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("graduation_candidates.id"), index=True, nullable=False
    )
    # Nullable so callers can omit the weight and let the orchestration
    # service derive it heuristically from the candidate's metrics at run
    # time (see ``PortfolioObjective`` for the heuristics).  Manual-
    # objective portfolios still supply explicit weights.
    weight: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    portfolio: Mapped["Portfolio"] = relationship(lazy="selectin")  # noqa: F821
    candidate: Mapped["GraduationCandidate"] = relationship(lazy="selectin")  # noqa: F821
