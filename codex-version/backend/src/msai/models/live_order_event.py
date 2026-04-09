from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class LiveOrderEvent(Base):
    __tablename__ = "live_order_events"
    __table_args__ = (
        Index("idx_live_order_events_deployment", "deployment_id"),
        Index("idx_live_order_events_strategy", "strategy_id"),
        Index("idx_live_order_events_ts_event", "ts_event"),
        Index("idx_live_order_events_client_order", "client_order_id"),
        Index("idx_live_order_events_venue_order", "venue_order_id"),
        Index("idx_live_order_events_type", "event_type"),
        UniqueConstraint("deployment_id", "event_id", name="uq_live_order_events_deployment_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    deployment_id: Mapped[str] = mapped_column(ForeignKey("live_deployments.id"), nullable=False)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    paper_trading: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    broker_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ts_event: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
