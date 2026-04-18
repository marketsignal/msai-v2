from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy


class LiveDeploymentStrategy(Base, CreatedAtMixin):
    __tablename__ = "live_deployment_strategies"
    __table_args__ = (
        UniqueConstraint("deployment_id", "revision_strategy_id", name="uq_lds_deployment_revision_strategy"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    deployment_id: Mapped[str] = mapped_column(ForeignKey("live_deployments.id", ondelete="CASCADE"), nullable=False)
    revision_strategy_id: Mapped[str] = mapped_column(
        ForeignKey("live_portfolio_revision_strategies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_id_full: Mapped[str] = mapped_column(String(280), nullable=False)

    revision_strategy: Mapped[LivePortfolioRevisionStrategy] = relationship(lazy="selectin")
