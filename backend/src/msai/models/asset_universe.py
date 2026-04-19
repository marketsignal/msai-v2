"""AssetUniverse model — tracked symbols with exchange and resolution for data ingestion."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class AssetUniverse(TimestampMixin, Base):
    """A symbol tracked for market-data ingestion.

    Each row represents a unique (symbol, exchange, resolution) tuple.  The
    ``enabled`` flag controls whether the data-ingestion pipeline includes
    this instrument in its next run.  ``last_ingested_at`` records when the
    most recent data was successfully fetched.
    """

    __tablename__ = "asset_universe"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", "resolution", name="uq_asset_symbol_exchange_res"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
