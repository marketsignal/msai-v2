"""LivePortfolioRevision — immutable snapshot of a portfolio composition.

The warm-restart identity boundary: any change to members/weights/configs
creates a NEW revision; existing revisions are frozen at snapshot time
and never mutated thereafter.

Immutability is a two-layer guarantee:
(1) ``RevisionService.enforce_immutability`` raises at the service
    boundary for any caller trying to mutate a frozen revision's
    members.
(2) A partial unique index ``uq_one_draft_per_portfolio`` at the DB
    level ensures at most one ``is_frozen=false`` row per portfolio.

Immutable row → no ``updated_at`` column; ``created_at`` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from msai.models.live_portfolio_revision_strategy import (
        LivePortfolioRevisionStrategy,
    )


class LivePortfolioRevision(CreatedAtMixin, Base):
    """Immutable snapshot of a portfolio's composition."""

    __tablename__ = "live_portfolio_revisions"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "revision_number",
            name="uq_live_portfolio_revisions_number",
        ),
        UniqueConstraint(
            "portfolio_id",
            "composition_hash",
            name="uq_live_portfolio_revisions_hash",
        ),
        # Partial unique index — matches the Alembic migration
        # ``o3i4j5k6l7m8_add_live_portfolio_tables.py``. Declared inline
        # on the model so ``Base.metadata.create_all`` (used by the
        # testcontainer fixtures in the portfolio integration tests)
        # produces a schema that matches production. Mirrors the pattern
        # from ``LiveNodeProcess.uq_live_node_processes_active_deployment``.
        Index(
            "uq_one_draft_per_portfolio",
            "portfolio_id",
            unique=True,
            postgresql_where=text("is_frozen = false"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    portfolio_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    composition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    strategies: Mapped[list[LivePortfolioRevisionStrategy]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="LivePortfolioRevisionStrategy.order_index",
        lazy="selectin",
    )
