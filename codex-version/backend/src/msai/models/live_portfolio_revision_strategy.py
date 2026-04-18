from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio_revision import LivePortfolioRevision
    from msai.models.strategy import Strategy


class LivePortfolioRevisionStrategy(Base, CreatedAtMixin):
    __tablename__ = "live_portfolio_revision_strategies"
    __table_args__ = (
        UniqueConstraint("revision_id", "order_index", name="uq_lprs_revision_order"),
        UniqueConstraint("revision_id", "strategy_id", name="uq_lprs_revision_strategy"),
        CheckConstraint("weight > 0 AND weight <= 1", name="ck_lprs_weight_range"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("live_portfolio_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    revision: Mapped[LivePortfolioRevision] = relationship(back_populates="strategies", lazy="selectin")
    strategy: Mapped[Strategy] = relationship(lazy="selectin")
