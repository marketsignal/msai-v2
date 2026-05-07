"""Control-plane definition for a tradable instrument.

Primary key is a stable logical UUID — NEVER a venue-qualified string.
Venue-qualified Nautilus ``InstrumentId`` strings live in
:class:`InstrumentAlias`, so a ticker change, listing-venue move, or
future MIC revision is a row update, not a PK migration.

See ``docs/prds/db-backed-strategy-registry.md`` §6 for schema rationale.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — SQLAlchemy Mapped[datetime] resolves at runtime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.instrument_alias import InstrumentAlias


class InstrumentDefinition(Base):
    __tablename__ = "instrument_definitions"

    __table_args__ = (
        CheckConstraint(
            "asset_class IN ('equity','futures','fx','option','crypto')",
            name="ck_instrument_definitions_asset_class",
        ),
        CheckConstraint(
            "lifecycle_state IN ('staged','active','retired')",
            name="ck_instrument_definitions_lifecycle_state",
        ),
        CheckConstraint(
            r"continuous_pattern IS NULL OR continuous_pattern ~ '^\.[A-Za-z]\.[0-9]+$'",
            name="ck_instrument_definitions_continuous_pattern_shape",
        ),
        UniqueConstraint(
            "raw_symbol",
            "provider",
            "asset_class",
            name="uq_instrument_definitions_symbol_provider_asset",
        ),
    )

    instrument_uid: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    raw_symbol: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    listing_venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    routing_venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    roll_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continuous_pattern: Mapped[str | None] = mapped_column(String(32), nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="staged"
    )
    hidden_from_inventory: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default="false",
        default=False,
    )
    """Soft-delete flag for the symbol inventory page.
    True means the user has removed the symbol from the inventory view; the
    underlying definition + aliases stay intact so re-onboarding restores
    the row instead of creating a duplicate."""
    trading_hours: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    """Per-instrument RTH/ETH window data.
    Schema: ``{"timezone": str, "rth": [{"day", "open", "close"}], "eth": [...]}``.
    NULL means "no schedule data" — :class:`MarketHoursService` fail-opens
    (returns True) on NULL."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    aliases: Mapped[list[InstrumentAlias]] = relationship(
        "InstrumentAlias",
        back_populates="definition",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
