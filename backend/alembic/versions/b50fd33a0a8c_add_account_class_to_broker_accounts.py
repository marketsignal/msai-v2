"""add account_class to broker_accounts

Revision ID: b50fd33a0a8c
Revises: 81e7efe6d772
Create Date: 2026-06-08 21:38:34.777722

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b50fd33a0a8c"
down_revision: str = "81e7efe6d772"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "broker_accounts",
        sa.Column(
            "account_class",
            sa.String(16),
            nullable=False,
            server_default="test",
        ),
    )
    # One-time backfill heuristic (operator-run, NOT runtime inference):
    # paper-mode rows are paper-class; existing live-mode rows are the
    # LVP/HVP test accounts (class 'test'). No 'real' row exists yet — the
    # fund is registered explicitly as 'real' post-PR-3 (plan D2/D5).
    op.execute("UPDATE broker_accounts SET account_class = 'paper' WHERE trading_mode = 'paper'")
    # live rows already default to 'test' via server_default; explicit for clarity:
    op.execute(
        "UPDATE broker_accounts SET account_class = 'test' "
        "WHERE trading_mode = 'live' AND account_class = 'test'"
    )


def downgrade() -> None:
    op.drop_column("broker_accounts", "account_class")
