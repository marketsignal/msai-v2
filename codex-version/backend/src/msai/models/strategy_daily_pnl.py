from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class StrategyDailyPnl(Base):
    __tablename__ = "strategy_daily_pnl"
    __table_args__ = (
        UniqueConstraint("strategy_id", "deployment_id", "date", name="uq_strategy_daily_pnl"),
        Index("idx_daily_pnl_strategy", "strategy_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("live_deployments.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    pnl: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    cumulative_pnl: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    capital_used: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    num_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    loss_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_drawdown: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
