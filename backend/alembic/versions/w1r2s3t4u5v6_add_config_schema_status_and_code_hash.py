"""Add config_schema_status + code_hash + config_class to strategies.

Revision ID: w1r2s3t4u5v6
Revises: v0q1r2s3t4u5
Create Date: 2026-04-20

Why:
----
The auto-generated strategy config form needs the backend to distinguish
four extraction outcomes: ``ready`` | ``unsupported`` | ``extraction_failed``
| ``no_config_class``. Using ``config_schema IS NULL`` conflates the
last three. Explicit enum column.

``code_hash`` is added so the discovery sync can skip re-running
``msgspec.json.schema(...)`` when a strategy file hasn't changed. Hot
path on ``GET /api/v1/strategies/`` (which imports every strategy on
every call).

``config_class`` stores the discovered ``*Config`` class name so
server-side validation at ``POST /api/v1/backtests/run`` targets the
actual discovered class (``EMACrossConfig``) rather than re-deriving
via suffix swap (which breaks for ``FooStrategyConfig`` / ``FooParams``
naming patterns that Nautilus ``StrategyConfig`` permits).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "w1r2s3t4u5v6"
down_revision = "v0q1r2s3t4u5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column(
            "config_schema_status",
            sa.String(32),
            nullable=False,
            server_default="no_config_class",
        ),
    )
    op.add_column(
        "strategies",
        sa.Column(
            "code_hash",
            sa.String(64),
            nullable=True,
        ),
    )
    op.add_column(
        "strategies",
        sa.Column(
            "config_class",
            sa.String(255),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_strategies_code_hash",
        "strategies",
        ["code_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_strategies_code_hash", table_name="strategies")
    op.drop_column("strategies", "config_class")
    op.drop_column("strategies", "code_hash")
    op.drop_column("strategies", "config_schema_status")
