"""Strategy model — a registered trading strategy available for backtesting and live deployment."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin


class Strategy(TimestampMixin, Base):
    """A trading strategy registered in the platform.

    Each strategy points to a Python file on disk (``file_path``) containing
    a NautilusTrader-compatible strategy class (``strategy_class``).  The
    optional ``config_schema`` stores a JSON Schema that describes the
    strategy's tunable parameters; ``default_config`` holds sensible defaults.
    """

    __tablename__ = "strategies"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    strategy_class: Mapped[str] = mapped_column(String(255), nullable=False)
    config_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    default_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )

    # Relationships
    creator: Mapped["User"] = relationship(lazy="selectin")  # noqa: F821
