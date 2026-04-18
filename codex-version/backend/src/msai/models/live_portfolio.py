from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio_revision import LivePortfolioRevision


class LivePortfolio(Base, TimestampMixin):
    __tablename__ = "live_portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    revisions: Mapped[list[LivePortfolioRevision]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="LivePortfolioRevision.revision_number",
        lazy="selectin",
    )
