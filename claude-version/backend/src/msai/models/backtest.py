"""Backtest model — a historical simulation run for a given strategy and configuration."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import func

from msai.models.base import Base


class Backtest(Base):
    """A single backtest execution record.

    Tracks the strategy version (via ``strategy_code_hash`` and optional
    ``strategy_git_sha``), the configuration used, date range, execution
    status, and resulting performance metrics.

    Note: This model intentionally uses ``created_at`` only (no ``updated_at``)
    because backtests are immutable after creation — status transitions are
    append-only state changes, not logical edits.
    """

    __tablename__ = "backtests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    strategy: Mapped["Strategy"] = relationship(lazy="selectin")  # noqa: F821
    creator: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
