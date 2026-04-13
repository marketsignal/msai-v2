"""Portfolio model — a named collection of graduated strategies with capital allocation."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin


class Portfolio(TimestampMixin, Base):
    """A portfolio that allocates capital across graduated strategy candidates.

    ``objective`` describes the optimization goal (e.g. ``max_sharpe``,
    ``min_drawdown``, ``equal_weight``).  ``base_capital`` is the starting
    notional, and ``requested_leverage`` is the target leverage multiplier.
    """

    __tablename__ = "portfolios"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective: Mapped[str] = mapped_column(String(64), nullable=False)
    base_capital: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    requested_leverage: Mapped[float] = mapped_column(
        Numeric(8, 4), nullable=False, server_default="1.0"
    )
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )

    # Relationships
    creator: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
