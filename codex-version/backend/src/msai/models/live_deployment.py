from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class LiveDeployment(Base):
    __tablename__ = "live_deployments"
    __table_args__ = (
        Index("ix_live_deployments_identity_signature", "identity_signature", unique=True),
        Index("ix_live_deployments_deployment_slug", "deployment_slug", unique=True),
        Index("ix_live_deployments_portfolio_revision_id", "portfolio_revision_id"),
        Index("ix_live_deployments_account_id", "account_id"),
        UniqueConstraint("portfolio_revision_id", "account_id", name="uq_live_deployments_revision_account"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    portfolio_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("live_portfolio_revisions.id"),
        nullable=True,
    )
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    identity_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_slug: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy_id_full: Mapped[str | None] = mapped_column(String(280), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="stopped", nullable=False)
    paper_trading: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ib_data_client_id: Mapped[int | None] = mapped_column(nullable=True)
    ib_exec_client_id: Mapped[int | None] = mapped_column(nullable=True)
    process_pid: Mapped[int | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
