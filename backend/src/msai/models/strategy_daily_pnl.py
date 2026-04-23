"""StrategyDailyPnl model — daily aggregated P&L for live strategy deployments."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class StrategyDailyPnl(Base):
    """Daily P&L snapshot for a strategy deployment.

    Aggregated at end-of-day for each active deployment.  The unique
    constraint on ``(strategy_id, deployment_id, date)`` prevents duplicate
    entries.  The composite index on ``(strategy_id, date)`` supports
    efficient cross-deployment P&L queries.
    """

    __tablename__ = "strategy_daily_pnl"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "deployment_id",
            "date",
            name="uq_strategy_deployment_date",
        ),
        Index("ix_strategy_daily_pnl_strategy_date", "strategy_id", "date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("live_deployments.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    cumulative_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    capital_used: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    num_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    strategy: Mapped[Strategy] = relationship(lazy="selectin")  # noqa: F821
    deployment: Mapped[LiveDeployment] = relationship(lazy="selectin")  # noqa: F821
