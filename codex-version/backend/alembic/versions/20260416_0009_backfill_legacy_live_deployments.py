"""backfill legacy live deployments as single-member portfolios

Revision ID: 20260416_0009
Revises: 20260416_0008
Create Date: 2026-04-16 23:30:00.000000
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_EVEN
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "20260416_0009"
down_revision: str | None = "20260416_0008"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_WEIGHT_SCALE = Decimal("0.000001")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT id, deployment_slug, strategy_id, strategy_id_full, config, instruments
            FROM live_deployments
            WHERE portfolio_revision_id IS NULL
              AND strategy_id IS NOT NULL
            """
        )
    ).mappings().all()

    for row in rows:
        portfolio_id = str(uuid4())
        revision_id = str(uuid4())
        revision_strategy_id = str(uuid4())

        deployment_slug = str(row["deployment_slug"] or row["id"])
        config = dict(row["config"] or {})
        instruments = [str(value) for value in (row["instruments"] or [])]
        strategy_id = str(row["strategy_id"])
        strategy_id_full = str(row["strategy_id_full"] or "")

        conn.execute(
            sa.text(
                """
                INSERT INTO live_portfolios (id, name, description, created_at, updated_at)
                VALUES (:id, :name, :description, NOW(), NOW())
                """
            ),
            {
                "id": portfolio_id,
                "name": f"Legacy-{deployment_slug}",
                "description": (
                    "Auto-created during 20260416_0009 legacy live deployment backfill "
                    f"for deployment {row['id']}"
                ),
            },
        )

        conn.execute(
            sa.text(
                """
                INSERT INTO live_portfolio_revisions
                    (id, portfolio_id, revision_number, composition_hash, is_frozen, created_at)
                VALUES (:id, :portfolio_id, 1, :composition_hash, true, NOW())
                """
            ),
            {
                "id": revision_id,
                "portfolio_id": portfolio_id,
                "composition_hash": _composition_hash(
                    strategy_id=strategy_id,
                    config=config,
                    instruments=instruments,
                ),
            },
        )

        conn.execute(
            sa.text(
                """
                INSERT INTO live_portfolio_revision_strategies
                    (id, revision_id, strategy_id, config, instruments, weight, order_index, created_at)
                VALUES (
                    :id,
                    :revision_id,
                    :strategy_id,
                    CAST(:config AS JSONB),
                    CAST(:instruments AS VARCHAR[]),
                    :weight,
                    0,
                    NOW()
                )
                """
            ),
            {
                "id": revision_strategy_id,
                "revision_id": revision_id,
                "strategy_id": strategy_id,
                "config": json.dumps(config, sort_keys=True),
                "instruments": instruments,
                "weight": Decimal("1.0"),
            },
        )

        conn.execute(
            sa.text(
                """
                INSERT INTO live_deployment_strategies
                    (id, deployment_id, revision_strategy_id, strategy_id_full, created_at)
                VALUES (:id, :deployment_id, :revision_strategy_id, :strategy_id_full, NOW())
                """
            ),
            {
                "id": str(uuid4()),
                "deployment_id": str(row["id"]),
                "revision_strategy_id": revision_strategy_id,
                "strategy_id_full": strategy_id_full,
            },
        )

        conn.execute(
            sa.text(
                """
                UPDATE live_deployments
                SET portfolio_revision_id = :revision_id
                WHERE id = :deployment_id
                """
            ),
            {
                "revision_id": revision_id,
                "deployment_id": str(row["id"]),
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    legacy_revision_rows = conn.execute(
        sa.text(
            """
            SELECT lpr.id AS revision_id
            FROM live_portfolio_revisions lpr
            JOIN live_portfolios lp ON lp.id = lpr.portfolio_id
            WHERE lp.name LIKE 'Legacy-%'
            """
        )
    ).mappings().all()
    revision_ids = [str(row["revision_id"]) for row in legacy_revision_rows]
    if not revision_ids:
        return

    conn.execute(
        sa.text(
            """
            DELETE FROM live_deployment_strategies
            WHERE revision_strategy_id IN (
                SELECT id
                FROM live_portfolio_revision_strategies
                WHERE revision_id = ANY(CAST(:revision_ids AS VARCHAR[]))
            )
            """
        ),
        {"revision_ids": revision_ids},
    )
    conn.execute(
        sa.text(
            """
            UPDATE live_deployments
            SET portfolio_revision_id = NULL
            WHERE portfolio_revision_id = ANY(CAST(:revision_ids AS VARCHAR[]))
            """
        ),
        {"revision_ids": revision_ids},
    )
    conn.execute(
        sa.text("DELETE FROM live_portfolios WHERE name LIKE 'Legacy-%'")
    )


def _composition_hash(*, strategy_id: str, config: dict[str, object], instruments: list[str]) -> str:
    canonical = [
        {
            "strategy_id": strategy_id,
            "order_index": 0,
            "config": config,
            "instruments": sorted(set(instruments)),
            "weight": format(
                Decimal("1.0").quantize(_WEIGHT_SCALE, rounding=ROUND_HALF_EVEN).normalize(),
                "f",
            ),
        }
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
