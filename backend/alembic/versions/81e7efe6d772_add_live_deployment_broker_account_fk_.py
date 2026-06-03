"""add live_deployment broker_account fk + validation stamp

Revision ID: 81e7efe6d772
Revises: d97a64e13e4e
Create Date: 2026-06-02 20:41:26.795462

broker-account-spawn-wiring (Task 1): ADDITIVE, metadata-only migration that
links a ``live_deployments`` row to the ``broker_accounts`` control-plane row
that owns its credentials, plus two stamp columns recording which credential
version was validated at spawn time.

All three columns are NULLABLE — ``ADD COLUMN ... NULL`` is a metadata-only
change in PostgreSQL (no table rewrite, no row locks held for data), so legacy
and in-flight deployment rows survive untouched (``broker_account_id`` stays
NULL; no backfill). Old code from a prior release SHA simply ignores the new
columns, so a rollback within this release is safe (additive-only discipline,
``rules/database.md``).

The FK uses ``ondelete='RESTRICT'`` because broker accounts are SOFT-archived
(``broker_accounts.status``), never hard-deleted — a deployment must never be
silently orphaned or cascade-deleted by an account row going away.

NOTE on larger tables: for a table big enough that the implicit FK validation
scan would block writes, the zero-write-block alternative is a two-step
``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`` followed by a separate
``VALIDATE CONSTRAINT`` (which takes only a SHARE UPDATE EXCLUSIVE lock). This
``live_deployments`` table is a tiny control-plane table, so the plain
``create_foreign_key`` here is fine and we keep the migration simple.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "81e7efe6d772"
down_revision: str = "d97a64e13e4e"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Additive, nullable columns (metadata-only ADD COLUMN — no rewrite).
    op.add_column(
        "live_deployments",
        sa.Column("broker_account_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("credentials_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("credentials_validated_version", sa.String(128), nullable=True),
    )

    # 2. FK → broker_accounts.id; RESTRICT because accounts are soft-archived,
    #    never hard-deleted (a deployment must not be orphaned/cascaded away).
    op.create_foreign_key(
        "fk_live_deployments_broker_account_id",
        "live_deployments",
        "broker_accounts",
        ["broker_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 3. Index the FK column (ALWAYS index foreign keys — rules/database.md).
    op.create_index(
        "ix_live_deployments_broker_account_id",
        "live_deployments",
        ["broker_account_id"],
    )


def downgrade() -> None:
    # Reverse order: index → FK → columns.
    op.drop_index("ix_live_deployments_broker_account_id", table_name="live_deployments")
    op.drop_constraint(
        "fk_live_deployments_broker_account_id",
        "live_deployments",
        type_="foreignkey",
    )
    op.drop_column("live_deployments", "credentials_validated_version")
    op.drop_column("live_deployments", "credentials_validated_at")
    op.drop_column("live_deployments", "broker_account_id")
