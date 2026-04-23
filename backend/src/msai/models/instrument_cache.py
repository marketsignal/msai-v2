"""InstrumentCache model — cached IB qualification results (Phase 2 task 2.2).

One row per fully-resolved instrument. The primary key is the Nautilus
canonical instrument ID (``AAPL.NASDAQ``, ``ESM5.CME``, ...) so two
callers that resolve the same logical instrument share a cache entry
and Phase 2's SecurityMaster service can do a cache-first lookup.

Columns:

- ``canonical_id`` — PK, Nautilus ``InstrumentId`` string in IB simplified
  symbology (see :class:`msai.services.nautilus.security_master.specs.InstrumentSpec`
  ``canonical_id()``).
- ``asset_class`` — one of ``equity``/``future``/``option``/``forex``/``index``
  (indexed for Phase 2's bulk-resolve paths that filter by class).
- ``venue`` — IB venue acronym, indexed for per-venue queries
  (e.g. "all CME futures").
- ``ib_contract_json`` — full IB ``Contract`` fields as JSONB. Lets the
  SecurityMaster refresh the Nautilus ``Instrument`` without re-hitting
  IB, which matters because IB rate-limits ``reqContractDetails`` to
  ≤50 msg/sec (gotcha #11 — don't load on critical path).
- ``nautilus_instrument_json`` — serialized ``Instrument`` object so
  the live subprocess can rebuild the exact same Nautilus object the
  backtest used (backtest/live parity — the whole point of Phase 2).
- ``trading_hours`` — JSONB schema documented below. Populated by
  Task 2.4 from the IB ``ContractDetails.tradingHours`` / ``liquidHours``
  strings. Phase 4 task 4.3's market-hours guard reads this column.
- ``last_refreshed_at`` — wall-clock timestamp of the last successful
  IB qualification. SecurityMaster uses it for staleness checks
  (refresh in background if older than N days).

``trading_hours`` JSONB schema (Phase 2 decision):

.. code-block:: json

    {
        "timezone": "America/New_York",
        "rth": [
            {"day": "MON", "open": "09:30", "close": "16:00"},
            ...
        ],
        "eth": [
            {"day": "MON", "open": "04:00", "close": "20:00"},
            ...
        ]
    }

Where ``rth`` = regular trading hours and ``eth`` = extended hours.
Nullable because some instruments (forex, continuous futures on 24h
venues) don't have a meaningful trading-hours window.
"""

from __future__ import annotations

from typing import Any

from datetime import datetime  # noqa: TC003 — required at runtime for SQLAlchemy Mapped[]

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class InstrumentCache(Base, TimestampMixin):
    """Cache of IB-qualified instruments indexed by canonical Nautilus ID.

    See module docstring for the ``trading_hours`` JSONB schema.
    """

    __tablename__ = "instrument_cache"

    canonical_id: Mapped[str] = mapped_column(
        String(128),  # max-length option spreads can produce long ids
        primary_key=True,
    )

    asset_class: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
    )
    """``equity`` / ``future`` / ``option`` / ``forex`` / ``index``."""

    venue: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )
    """IB venue acronym (``NASDAQ``, ``CME``, ``IDEALPRO``, ``SMART``)."""

    ib_contract_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Full IB ``Contract`` fields as JSONB. Used by SecurityMaster to
    rebuild the IB contract without hitting IB again."""

    nautilus_instrument_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """Serialized Nautilus ``Instrument`` so the live subprocess +
    backtest runner rebuild the SAME Nautilus object. Parity matters
    here — any drift between serialization formats breaks the
    backtest/live contract Phase 2 exists to enforce."""

    trading_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """See module docstring for schema. NULL for 24h instruments
    (forex, continuous futures on CME)."""

    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    """Wall-clock timestamp of the last successful IB qualification.
    SecurityMaster reads this for staleness checks."""

    __table_args__ = (
        # Composite index for "resolve everything on venue X of class Y"
        # queries Phase 2's bulk-resolve path hits during startup.
        Index(
            "ix_instrument_cache_class_venue",
            "asset_class",
            "venue",
        ),
    )
