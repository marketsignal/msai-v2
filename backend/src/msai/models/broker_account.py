"""BrokerAccount model — operator-managed IB broker account identity.

The ``broker_accounts`` row is the system of record for an Interactive
Brokers account: its identity (``ib_account_id`` / ``ib_login_key``),
its exclusive gateway-slot binding, its trading mode, and credential
METADATA (the backend, secret reference, and audit columns). The secret
itself is NEVER stored here — it lives in the credentials backend
(Azure Key Vault in prod, a file-backed env store in dev).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin
from msai.models.user import User


class BrokerAccountStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CredentialsBackend(StrEnum):
    AZURE_KV = "azure_kv"  # prod: secret written to Azure Key Vault
    ENV = "env"  # dev: file-backed writable store
    LEGACY_ENV = "legacy_env"  # migrated LVP/HVP: points at existing env keys, NULL version


class BrokerAccount(TimestampMixin, Base):
    """Operator-managed IB broker account. System of record for account identity,
    gateway-slot binding, and credential METADATA (never the secret itself)."""

    __tablename__ = "broker_accounts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # IB account identifier (U.../DU...). IMMUTABLE after create.
    ib_account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ib_login_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[BrokerAccountStatus] = mapped_column(
        String(32), nullable=False, default=BrokerAccountStatus.ACTIVE
    )
    gateway_slot: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")

    credentials_backend: Mapped[CredentialsBackend] = mapped_column(String(32), nullable=False)
    credentials_secret_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    credentials_secret_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # tz-aware to match the migration's DateTime(timezone=True) + the service's
    # aware-UTC writes (Codex iter-7 P2)
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credentials_updated_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    credentials_last_accessed: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    creator: Mapped[User | None] = relationship(lazy="selectin")
