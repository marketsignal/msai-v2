"""add per-deployment ib client ids

Revision ID: 20260407_0006
Revises: 20260407_0005
Create Date: 2026-04-07 14:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260407_0006"
down_revision: str | None = "20260407_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("live_deployments", sa.Column("ib_data_client_id", sa.Integer(), nullable=True))
    op.add_column("live_deployments", sa.Column("ib_exec_client_id", sa.Integer(), nullable=True))
    op.create_index(
        "idx_live_deployments_ib_data_client_id",
        "live_deployments",
        ["ib_data_client_id"],
        unique=False,
    )
    op.create_index(
        "idx_live_deployments_ib_exec_client_id",
        "live_deployments",
        ["ib_exec_client_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_live_deployments_ib_exec_client_id", table_name="live_deployments")
    op.drop_index("idx_live_deployments_ib_data_client_id", table_name="live_deployments")
    op.drop_column("live_deployments", "ib_exec_client_id")
    op.drop_column("live_deployments", "ib_data_client_id")
