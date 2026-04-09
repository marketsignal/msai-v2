from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class InstrumentDefinition(Base, TimestampMixin):
    __tablename__ = "instrument_definitions"

    instrument_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="interactive_brokers")
    raw_symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(64), nullable=False)
    security_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False, default="stocks")
    instrument_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    contract_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
