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


class AccountClass(StrEnum):
    PAPER = "paper"  # paper-trading account (no real funds; DU/DF prefix)
    TEST = "test"  # live IB account used for testing with limited capital (LVP/HVP)
    REAL = "real"  # the production fund — identity-echo gated, must never be hit by accident


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

    # Explicit real-money classification. NEVER inferred from ib_account_id
    # string prefix (PRD §6). String-backed StrEnum + server_default so the
    # column is additive and existing rows backfill (migration sets paper rows
    # to 'paper'; live rows stay 'test'; the fund is registered explicitly as
    # 'real' post-PR-3). See plan D1/D2/D5.
    account_class: Mapped[AccountClass] = mapped_column(
        String(16), nullable=False, default=AccountClass.TEST, server_default="test"
    )

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

    @property
    def is_real_money(self) -> bool:
        """True only for the production fund (``account_class == real``). The
        single fact the deploy identity-echo gate + UI real-money label read.

        Robust to the String-backed reload: this column is a plain ``String(16)``
        (status/trading_mode convention), so a refreshed row's ``account_class`` is
        a bare ``str``, not an ``AccountClass`` member. ``StrEnum`` equality makes
        ``"real" == AccountClass.REAL`` True, so this comparison holds for both the
        freshly-assigned enum and the reloaded str (iter-3 P1#2)."""
        return self.account_class == AccountClass.REAL
