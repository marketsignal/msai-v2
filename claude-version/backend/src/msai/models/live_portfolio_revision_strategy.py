"""LivePortfolioRevisionStrategy — M:N membership row for a portfolio revision.

One row per strategy per revision. A strategy can appear in multiple
portfolios (and multiple revisions across portfolios); uniqueness is
scoped to the revision.

Immutable on create: created_at only, no updated_at.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TC003 — SQLAlchemy Mapped[Decimal] resolves at runtime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio_revision import LivePortfolioRevision
    from msai.models.strategy import Strategy


class LivePortfolioRevisionStrategy(CreatedAtMixin, Base):
    """One strategy's participation in a portfolio revision."""

    __tablename__ = "live_portfolio_revision_strategies"
    __table_args__ = (
        UniqueConstraint("revision_id", "order_index", name="uq_lprs_revision_order"),
        UniqueConstraint("revision_id", "strategy_id", name="uq_lprs_revision_strategy"),
        # Weight is an allocation fraction ∈ (0, 1]. DB-level CHECK so
        # the invariant survives any future caller that skips the
        # service-level validation (PR#1 code-review, 2026-04-16).
        CheckConstraint("weight > 0 AND weight <= 1", name="ck_lprs_weight_range"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_portfolio_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    revision: Mapped[LivePortfolioRevision] = relationship(
        back_populates="strategies", lazy="selectin"
    )
    strategy: Mapped[Strategy] = relationship(lazy="selectin")
