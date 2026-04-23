"""PortfolioRun model — a combined backtest or analysis run across all strategies in a portfolio."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.portfolio import Portfolio
    from msai.models.user import User


class PortfolioRun(Base):
    """A portfolio-level backtest or analysis run.

    Runs the combined performance of all allocated strategies over a date range.
    ``metrics`` stores aggregated portfolio-level statistics; ``report_path``
    points to the generated QuantStats HTML report on disk.  ``series`` holds
    the equity/drawdown curve, ``allocations`` captures the per-candidate
    results, and ``heartbeat_at`` tracks worker liveness for stale-job
    detection.
    """

    __tablename__ = "portfolio_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(
        ForeignKey("portfolios.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    series: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    allocations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    max_parallelism: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    portfolio: Mapped[Portfolio] = relationship(lazy="selectin")  # noqa: F821
    creator: Mapped[User] = relationship(lazy="selectin")  # noqa: F821
