"""LiveDeployment model — a running (or stopped) instance of a strategy in live or paper mode."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LiveDeployment(Base):
    """A live or paper-trading deployment of a strategy.

    Tracks which strategy version is deployed (via ``strategy_code_hash`` and
    ``strategy_git_sha``), the runtime configuration, instrument universe,
    and lifecycle timestamps.

    Note: ``created_at`` only — deployments are immutable records.  A new
    deployment row is created each time a strategy is (re-)started.
    """

    __tablename__ = "live_deployments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="stopped")
    paper_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    strategy: Mapped["Strategy"] = relationship(lazy="selectin")  # noqa: F821
    starter: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
