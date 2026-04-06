"""User model for MSAI v2 authentication and authorization."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    """Platform user authenticated via Microsoft Entra ID.

    Roles:
        - ``admin``: Full platform access including user management.
        - ``trader``: Can create/run strategies and deployments.
        - ``viewer``: Read-only access to dashboards and reports.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entra_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="viewer")
