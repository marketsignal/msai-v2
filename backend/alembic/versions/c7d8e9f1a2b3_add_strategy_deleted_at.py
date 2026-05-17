"""add strategy deleted_at for soft delete

Revision ID: c7d8e9f1a2b3
Revises: b6a7b8c9d0e1
Create Date: 2026-05-16

Adds a ``deleted_at TIMESTAMP NULL`` column to the ``strategies`` table to
support soft-delete (T3 of the 2026-05-16 ui-completeness plan). Archived
strategies stay addressable via their UUID so historical backtest +
deployment foreign keys keep resolving, but are filtered out of list and
new-operation paths by a global SQLAlchemy ``do_orm_execute`` listener
(``msai/core/soft_delete.py``).

A partial index ``ix_strategies_active`` over ``(id) WHERE deleted_at IS
NULL`` supports the common active-rows lookups (list, new-op dispatch)
without scanning archived rows once the table grows.

Migration shape: additive only (per ``rules/database.md``):

- ADD COLUMN ``deleted_at TIMESTAMP NULL`` — Postgres 16 metadata-only on
  an existing table; no rewrite required.
- CREATE INDEX ``ix_strategies_active`` (partial, ``WHERE deleted_at IS
  NULL``) — transparent to old code.

Rollback safety: forward-compatible — old code (which does not know about
``deleted_at``) sees the column as ``NULL`` for every existing row and
ignores it. The DELETE handler in old code still issues a hard
``DELETE FROM strategies`` which is destructive but bounded; rollback of
this PR's image would not re-introduce a soft-delete handler that
references the column.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c7d8e9f1a2b3"
down_revision = "b6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_strategies_active",
        "strategies",
        ["id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_strategies_active", table_name="strategies")
    op.drop_column("strategies", "deleted_at")
