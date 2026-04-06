"""SQLAlchemy declarative base and common mixins for MSAI v2."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


class TimestampMixin:
    """Mixin that adds ``created_at`` and ``updated_at`` columns.

    ``created_at`` is set automatically by the database on INSERT.
    ``updated_at`` is set on INSERT and refreshed on every UPDATE via
    SQLAlchemy's ``onupdate`` hook.
    """

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
