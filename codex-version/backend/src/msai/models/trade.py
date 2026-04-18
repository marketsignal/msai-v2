from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint(
            "(backtest_id IS NOT NULL AND deployment_id IS NULL) OR (backtest_id IS NULL AND deployment_id IS NOT NULL)",
            name="chk_trades_source",
        ),
        Index("idx_trades_backtest", "backtest_id"),
        Index("idx_trades_deployment", "deployment_id"),
        Index("idx_trades_strategy", "strategy_id"),
        Index("idx_trades_strategy_full", "strategy_id_full"),
        Index("idx_trades_executed", "executed_at"),
        Index("idx_trades_instrument", "instrument"),
        Index("idx_trades_broker_trade", "broker_trade_id"),
        UniqueConstraint("deployment_id", "broker_trade_id", name="uq_trades_deployment_broker_trade"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    backtest_id: Mapped[str | None] = mapped_column(ForeignKey("backtests.id"), nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("live_deployments.id"), nullable=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    strategy_id_full: Mapped[str | None] = mapped_column(String(280), nullable=True)
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument: Mapped[str] = mapped_column(String(100), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    commission: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    broker_trade_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    broker_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
