"""ResearchTrial model — a single trial within a research job (one config evaluation)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.backtest import Backtest
    from msai.models.research_job import ResearchJob


class ResearchTrial(Base):
    """One trial of a research job — a single backtest with a specific config.

    Each trial maps to at most one :class:`Backtest` row (``backtest_id``).
    ``objective_value`` stores the scalar metric being optimized (e.g. Sharpe
    ratio) for Optuna-compatible ranking.
    """

    __tablename__ = "research_trials"
    __table_args__ = (
        UniqueConstraint("research_job_id", "trial_number", name="uq_trial_job_number"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    research_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    trial_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    objective_value: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    backtest_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("backtests.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    research_job: Mapped[ResearchJob] = relationship(lazy="selectin")  # noqa: F821
    backtest: Mapped[Backtest] = relationship(lazy="selectin")  # noqa: F821
