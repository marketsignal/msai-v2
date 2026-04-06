from __future__ import annotations

from uuid import uuid4

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    strategy_class: Mapped[str] = mapped_column(String(255), nullable=False)
    config_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    default_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
