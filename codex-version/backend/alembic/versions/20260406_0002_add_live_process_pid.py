"""add live deployment process pid

Revision ID: 20260406_0002
Revises: 20260226_0001
Create Date: 2026-04-06 18:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20260406_0002"
down_revision: Union[str, Sequence[str], None] = "20260226_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("live_deployments", sa.Column("process_pid", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("live_deployments", "process_pid")
