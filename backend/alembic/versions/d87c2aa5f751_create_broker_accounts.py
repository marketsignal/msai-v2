"""create broker_accounts

Revision ID: d87c2aa5f751
Revises: e5f6a7b8c9d0
Create Date: 2026-06-01 22:48:09.535865

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d87c2aa5f751"
down_revision: str = "e5f6a7b8c9d0"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("ib_account_id", sa.String(32), nullable=False),
        sa.Column("ib_login_key", sa.String(64), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("gateway_slot", sa.String(64), nullable=False),
        sa.Column("trading_mode", sa.String(16), nullable=False, server_default="paper"),
        sa.Column("credentials_backend", sa.String(32), nullable=False),
        sa.Column("credentials_secret_ref", sa.String(256), nullable=False),
        sa.Column("credentials_secret_version", sa.String(128), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credentials_updated_by", sa.String(256), nullable=True),
        sa.Column("credentials_last_accessed", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index("ix_broker_accounts_login", "broker_accounts", ["ib_login_key"])
    op.create_index("ix_broker_accounts_created_by", "broker_accounts", ["created_by"])
    # one ACTIVE row per ib_account_id; archived rows don't block re-add
    op.create_index(
        "uq_broker_accounts_active_ib_account_id",
        "broker_accounts",
        ["ib_account_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )
    # one ACTIVE row per gateway_slot (slot is exclusively allocated)
    op.create_index(
        "uq_broker_accounts_active_gateway_slot",
        "broker_accounts",
        ["gateway_slot"],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )


def downgrade() -> None:
    op.drop_table("broker_accounts")
