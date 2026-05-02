"""add hidden_from_inventory to instrument_definitions

Revision ID: 1e2d728f1b32
Revises: e2f3g4h5i6j7
Create Date: 2026-05-01 13:43:25.642528

"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1e2d728f1b32"
down_revision: str | None = "e2f3g4h5i6j7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOTE: autogenerate also surfaced pre-existing drift (instrument_aliases
    # index rename, order_attempt_audits unique-constraint vs unique-index,
    # symbol_onboarding_runs index removal). Those changes are unrelated to
    # this task (B6a) and were stripped — they should be addressed in a
    # dedicated drift-reconciliation migration.
    op.add_column(
        "instrument_definitions",
        sa.Column(
            "hidden_from_inventory",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("instrument_definitions", "hidden_from_inventory")
