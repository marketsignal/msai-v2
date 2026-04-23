"""Strategy model — a registered trading strategy available for backtesting and live deployment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from msai.models.user import User


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
    # The discovered ``*Config`` class's class name (not derivable from
    # ``strategy_class`` — e.g. EMACrossStrategy → EMACrossConfig BUT
    # FooStrategy → FooStrategyConfig or FooParams are all legal).
    # Server-side validation at POST /backtests/run uses this exact name
    # rather than re-deriving a suffix swap from ``strategy_class``.
    # Nullable for strategies with no matching config class.
    config_class: Mapped[str | None] = mapped_column(String(255), nullable=True)
    config_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    default_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Four-state enum. See ``services/nautilus/schema_hooks.ConfigSchemaStatus``.
    # Stored as String(32) to match the existing ``governance_status`` pattern
    # on this table — no DB CHECK constraint is added; the enum is enforced
    # at the application layer via ``ConfigSchemaStatus(value)``.
    config_schema_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="no_config_class", server_default="no_config_class"
    )
    # SHA256 of the strategy file's bytes. Populated by the discovery sync;
    # compared against disk to decide whether to recompute ``config_schema``.
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    governance_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="unchecked"
    )
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )

    # Relationships
    creator: Mapped[User] = relationship(lazy="selectin")  # noqa: F821
