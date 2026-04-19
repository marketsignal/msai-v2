"""ResearchJob model — a parameter sweep or walk-forward optimization run."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class ResearchJob(Base):
    """A research job (parameter sweep, walk-forward, etc.) against a strategy.

    Tracks the overall sweep configuration, progress percentage, and aggregated
    results.  Individual trials are stored in :class:`ResearchTrial`.
    """

    __tablename__ = "research_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    progress_message: Mapped[str | None] = mapped_column(String(256), nullable=True)
    results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    best_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    best_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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

    # Job lifecycle fields (used by the watchdog to detect stale/orphaned jobs)
    queue_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    queue_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    strategy: Mapped["Strategy"] = relationship(lazy="selectin")  # noqa: F821
    creator: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
