"""LivePortfolio model — mutable identity for a live trading portfolio.

A ``live_portfolios`` row names a portfolio. The actual composition —
which strategies, at what weights, with what configs — is captured on
immutable ``live_portfolio_revisions`` rows. Rebalancing creates a new
revision; it never mutates old ones.

The "active" revision is computed on the fly by
``RevisionService.get_active_revision`` (no denormalized
``latest_revision_id`` column — avoids FK cycle + cascade-delete
complexity, trivial-cost query on an indexed column).

See ``docs/plans/2026-04-16-portfolio-per-account-live-design.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from msai.models.user import User


class LivePortfolio(TimestampMixin, Base):
    """A named, mutable portfolio of graduated strategies."""

    __tablename__ = "live_portfolios"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    creator: Mapped[User] = relationship(lazy="selectin")
