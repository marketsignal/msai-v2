"""LiveDeploymentStrategy — per-deployment materialized member row.

One row per strategy per deployment, written by the supervisor at spawn
time. Provides the read path (WebSocket snapshot, /live/positions)
with the concrete ``strategy_id_full`` for each running strategy.
Immutable on create.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LiveDeploymentStrategy(Base):
    """One strategy instance inside a live deployment."""

    __tablename__ = "live_deployment_strategies"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "revision_strategy_id",
            name="uq_lds_deployment_revision_strategy",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "live_portfolio_revision_strategies.id", ondelete="RESTRICT"
        ),
        nullable=False,
        index=True,
    )
    strategy_id_full: Mapped[str] = mapped_column(String(280), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    revision_strategy: Mapped["LivePortfolioRevisionStrategy"] = relationship(  # noqa: F821
        lazy="selectin"
    )
