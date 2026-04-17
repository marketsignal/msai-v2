"""Control-plane definition for a tradable instrument.

Primary key is a stable logical UUID — NEVER a venue-qualified string.
Venue-qualified Nautilus ``InstrumentId`` strings live in
:class:`InstrumentAlias`, so a ticker change, listing-venue move, or
future MIC revision is a row update, not a PK migration.

See ``docs/prds/db-backed-strategy-registry.md`` §6 for the full
schema rationale.

**Coexistence note (2026-04-17):** the existing ``InstrumentCache``
model / table (``instrument_cache``) is NOT migrated in this PR. Follow-up
PR ``docs/plans/2026-04-XX-instrument-cache-migration.md`` handles that
once Nautilus-native cache durability (``CacheConfig(database=redis)``)
has proven out through a restart cycle in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 — SQLAlchemy Mapped[datetime] resolves at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
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

    instrument_uid: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
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
