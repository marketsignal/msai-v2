from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio import LivePortfolio
    from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy


class LivePortfolioRevision(Base, CreatedAtMixin):
    __tablename__ = "live_portfolio_revisions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "revision_number", name="uq_live_portfolio_revisions_number"),
        UniqueConstraint("portfolio_id", "composition_hash", name="uq_live_portfolio_revisions_hash"),
        Index(
            "uq_one_draft_per_portfolio",
            "portfolio_id",
            unique=True,
            postgresql_where=text("is_frozen = false"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("live_portfolios.id", ondelete="CASCADE"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    composition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    portfolio: Mapped[LivePortfolio] = relationship(back_populates="revisions", lazy="selectin")
    strategies: Mapped[list[LivePortfolioRevisionStrategy]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="LivePortfolioRevisionStrategy.order_index",
        lazy="selectin",
    )
