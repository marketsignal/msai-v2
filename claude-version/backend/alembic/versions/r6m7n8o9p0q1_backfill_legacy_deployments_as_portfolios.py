"""backfill legacy deployments as single-strategy portfolios

Revision ID: r6m7n8o9p0q1
Revises: q5l6m7n8o9p0
Create Date: 2026-04-16 23:30:00.000000

PR#2 portfolio-per-account-live, Task 10+21: data migration that wraps
each legacy ``live_deployments`` row (``portfolio_revision_id IS NULL``)
into a synthetic single-strategy portfolio with one revision, one member,
and one deployment-strategy bridge row.

Idempotent: rows where ``portfolio_revision_id IS NOT NULL`` are skipped.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import sqlalchemy as sa

from alembic import op


revision: str = "r6m7n8o9p0q1"
down_revision: str = "q5l6m7n8o9p0"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(
        sa.text(
            "SELECT id, deployment_slug, strategy_id, strategy_id_full "
            "FROM live_deployments "
            "WHERE portfolio_revision_id IS NULL"
        )
    ).fetchall()

    if not rows:
        return

    from msai.services.live.portfolio_composition import compute_composition_hash

    for row in rows:
        deployment_id = row.id
        deployment_slug = row.deployment_slug
        strategy_id = row.strategy_id
        strategy_id_full = row.strategy_id_full

        portfolio_id = uuid4()
        revision_id = uuid4()
        member_id = uuid4()
        lds_id = uuid4()

        conn.execute(
            sa.text(
                "INSERT INTO live_portfolios (id, name, description, created_by) "
                "VALUES (:id, :name, :desc, NULL)"
            ),
            {
                "id": portfolio_id,
                "name": f"Legacy-{deployment_slug}",
                "desc": "Auto-created by backfill migration",
            },
        )

        member_dict = {
            "strategy_id": strategy_id,
            "order_index": 0,
            "config": {},
            "instruments": [],
            "weight": Decimal("1.0"),
        }
        composition_hash = compute_composition_hash([member_dict])

        conn.execute(
            sa.text(
                "INSERT INTO live_portfolio_revisions "
                "(id, portfolio_id, revision_number, composition_hash, is_frozen) "
                "VALUES (:id, :pid, 1, :hash, true)"
            ),
            {"id": revision_id, "pid": portfolio_id, "hash": composition_hash},
        )

        conn.execute(
            sa.text(
                "INSERT INTO live_portfolio_revision_strategies "
                "(id, revision_id, strategy_id, config, instruments, weight, order_index) "
                "VALUES (:id, :rid, :sid, '{}'::jsonb, '{}'::text[], :weight, 0)"
            ),
            {
                "id": member_id,
                "rid": revision_id,
                "sid": strategy_id,
                "weight": Decimal("1.0"),
            },
        )

        conn.execute(
            sa.text("UPDATE live_deployments SET portfolio_revision_id = :rid WHERE id = :did"),
            {"rid": revision_id, "did": deployment_id},
        )

        conn.execute(
            sa.text(
                "INSERT INTO live_deployment_strategies "
                "(id, deployment_id, revision_strategy_id, strategy_id_full) "
                "VALUES (:id, :did, :rsid, :sif)"
            ),
            {"id": lds_id, "did": deployment_id, "rsid": member_id, "sif": strategy_id_full or ""},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM live_deployment_strategies WHERE deployment_id IN "
            "(SELECT id FROM live_deployments WHERE portfolio_revision_id IS NOT NULL)"
        )
    )
    conn.execute(sa.text("UPDATE live_deployments SET portfolio_revision_id = NULL"))
    conn.execute(sa.text("DELETE FROM live_portfolios WHERE name LIKE 'Legacy-%'"))
