"""Venue-qualified alias for an :class:`InstrumentDefinition`.

One definition can have many aliases — one per provider per
``effective_from`` date. Futures front-month rolls close the
expiring contract's alias row (setting ``effective_to``) and
insert a new alias for the next front month.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime  # noqa: TC003 — SQLAlchemy Mapped[...] resolves at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.instrument_definition import InstrumentDefinition


class InstrumentAlias(Base):
    __tablename__ = "instrument_aliases"

    __table_args__ = (
        CheckConstraint(
            "venue_format IN ('exchange_name','mic_code','databento_continuous')",
            name="ck_instrument_aliases_venue_format",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_instrument_aliases_effective_window",
        ),
        UniqueConstraint(
            "alias_string",
            "provider",
            "effective_from",
            name="uq_instrument_aliases_string_provider_from",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    instrument_uid: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey(
            "instrument_definitions.instrument_uid", ondelete="CASCADE"
        ),
        nullable=False,
        index=True,
    )
    alias_string: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # String(32): must accommodate the longest literal allowed by the
    # ``ck_instrument_aliases_venue_format`` CHECK constraint — currently
    # ``'databento_continuous'`` (20 chars). Leaving headroom for future
    # venue formats (e.g. ``'mic_code_with_segment'``).
    venue_format: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    definition: Mapped[InstrumentDefinition] = relationship(
        "InstrumentDefinition", back_populates="aliases"
    )
