"""normalize live_deployments.message_bus_stream separator to colon

Revision ID: n2h3i4j5k6l7
Revises: m1g2h3i4j5k6
Create Date: 2026-04-16 16:45:00.000000

Bug B fix (2026-04-16): ``derive_message_bus_stream`` previously
returned ``trader-MSAI-{slug}-stream`` (hyphen separator before
``stream``) but Nautilus's Rust ``MessageBus`` actually writes to
``trader-MSAI-{slug}:stream`` (colon) when built with
``use_trader_prefix=True``, ``use_trader_id=True``,
``streams_prefix='stream'``. Result: the projection consumer
``XREADGROUP``ed an empty ``-stream`` key while every position /
order / account event piled up on the unread ``:stream``.

This migration rewrites the persisted ``message_bus_stream`` on
existing rows to match the new format. No data loss — the
``-stream`` keys referenced by the old values were empty on every
deployment checked (0 entries vs 64–2582 on the corresponding
``:stream`` keys).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "n2h3i4j5k6l7"
down_revision: str = "m1g2h3i4j5k6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE live_deployments
            SET message_bus_stream = substring(message_bus_stream from 1 for length(message_bus_stream) - 7) || chr(58) || 'stream'
            WHERE message_bus_stream LIKE '%-stream'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE live_deployments
            SET message_bus_stream = substring(message_bus_stream from 1 for length(message_bus_stream) - 7) || '-stream'
            WHERE message_bus_stream LIKE '%' || chr(58) || 'stream'
            """
        )
    )
