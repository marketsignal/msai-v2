"""Add instrument_definitions + instrument_aliases control-plane tables.

Revision ID: v0q1r2s3t4u5
Revises: u9p0q1r2s3t4
Create Date: 2026-04-17 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "v0q1r2s3t4u5"
down_revision: str = "u9p0q1r2s3t4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_definitions",
        sa.Column(
            "instrument_uid",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("raw_symbol", sa.String(100), nullable=False),
        sa.Column("listing_venue", sa.String(32), nullable=False),
        sa.Column("routing_venue", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("roll_policy", sa.String(64), nullable=True),
        sa.Column("continuous_pattern", sa.String(32), nullable=True),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "lifecycle_state",
            sa.String(32),
            nullable=False,
            server_default="staged",
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
        sa.CheckConstraint(
            "asset_class IN ('equity','futures','fx','option','crypto')",
            name="ck_instrument_definitions_asset_class",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('staged','active','retired')",
            name="ck_instrument_definitions_lifecycle_state",
        ),
        sa.CheckConstraint(
            "continuous_pattern IS NULL OR continuous_pattern ~ '^\\.[A-Za-z]\\.[0-9]+$'",
            name="ck_instrument_definitions_continuous_pattern_shape",
        ),
        sa.UniqueConstraint(
            "raw_symbol",
            "provider",
            "asset_class",
            name="uq_instrument_definitions_symbol_provider_asset",
        ),
    )
    op.create_index(
        "ix_instrument_definitions_raw_symbol",
        "instrument_definitions",
        ["raw_symbol"],
    )
    op.create_index(
        "ix_instrument_definitions_listing_venue",
        "instrument_definitions",
        ["listing_venue"],
    )
    op.create_index(
        "ix_instrument_definitions_routing_venue",
        "instrument_definitions",
        ["routing_venue"],
    )

    op.create_table(
        "instrument_aliases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "instrument_uid",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "instrument_definitions.instrument_uid", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("alias_string", sa.String(100), nullable=False),
        sa.Column("venue_format", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "venue_format IN ('exchange_name','mic_code','databento_continuous')",
            name="ck_instrument_aliases_venue_format",
        ),
        sa.UniqueConstraint(
            "alias_string",
            "provider",
            "effective_from",
            name="uq_instrument_aliases_string_provider_from",
        ),
    )
    op.create_index(
        "ix_instrument_aliases_uid", "instrument_aliases", ["instrument_uid"]
    )
    op.create_index(
        "ix_instrument_aliases_alias_string",
        "instrument_aliases",
        ["alias_string"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instrument_aliases_alias_string", table_name="instrument_aliases"
    )
    op.drop_index("ix_instrument_aliases_uid", table_name="instrument_aliases")
    op.drop_table("instrument_aliases")
    op.drop_index(
        "ix_instrument_definitions_routing_venue",
        table_name="instrument_definitions",
    )
    op.drop_index(
        "ix_instrument_definitions_listing_venue",
        table_name="instrument_definitions",
    )
    op.drop_index(
        "ix_instrument_definitions_raw_symbol",
        table_name="instrument_definitions",
    )
    op.drop_table("instrument_definitions")
