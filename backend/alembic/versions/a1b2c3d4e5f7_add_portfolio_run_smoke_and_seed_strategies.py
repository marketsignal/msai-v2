"""add portfolio_run.smoke column + seed canonical smoke strategies

Adds a boolean ``smoke`` flag to ``portfolio_runs`` (indexed for filter
queries) and bulk-seeds four canonical Strategy rows under the
``__smoke__/`` sentinel namespace so the operational smoke runner can
resolve them deterministically without operator intervention.

The seeded Strategy rows are system-created — ``created_by`` is left NULL
(made nullable by migration a1b2c3d4e5f6 — see
``backend/src/msai/models/strategy.py``).

Migration discipline (per ``.claude/rules/database.md``):

- Additive-only: ADD column with ``server_default='false'`` (old code sees
  ``False`` if rolled back); ADD index (transparent); INSERT rows under a
  sentinel name that operator-created strategies never use.
- Idempotent seed: each insert is gated by a ``SELECT id`` existence check
  so re-running the migration on a partially-seeded DB is a no-op for the
  rows that already exist.

PRD: ``docs/prds/ingest-backtest-smoke-test.md`` v1.3.

Revision ID: a1b2c3d4e5f7
Revises: 72ea2fd4dda2
Create Date: 2026-05-26
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "72ea2fd4dda2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Canonical smoke Strategy rows. Names use the ``__smoke__/`` sentinel
# prefix so the runner can look them up deterministically and operator-
# created strategies can never collide. NOT NULL columns on
# ``strategies``: ``name``, ``file_path``, ``strategy_class``. Optional:
# ``config_class``, ``default_config``. ``created_by`` is left NULL (system
# seed; was made nullable by migration a1b2c3d4e5f6).
SMOKE_STRATEGIES: list[dict[str, object]] = [
    {
        "name": "__smoke__/smoke_market_order/AAPL",
        "file_path": "strategies/example/smoke_market_order.py",
        "strategy_class": "SmokeMarketOrderStrategy",
        "config_class": "SmokeMarketOrderConfig",
        "default_config": {
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
    },
    {
        "name": "__smoke__/smoke_market_order/SPY",
        "file_path": "strategies/example/smoke_market_order.py",
        "strategy_class": "SmokeMarketOrderStrategy",
        "config_class": "SmokeMarketOrderConfig",
        "default_config": {
            "instrument_id": "SPY.NASDAQ",
            "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
    },
    {
        "name": "__smoke__/ema_cross/AAPL",
        "file_path": "strategies/example/ema_cross.py",
        "strategy_class": "EMACrossStrategy",
        "config_class": "EMACrossConfig",
        "default_config": {
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 20,
            "trade_size": 1,
        },
    },
    {
        "name": "__smoke__/ema_cross/SPY",
        "file_path": "strategies/example/ema_cross.py",
        "strategy_class": "EMACrossStrategy",
        "config_class": "EMACrossConfig",
        "default_config": {
            "instrument_id": "SPY.NASDAQ",
            "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 20,
            "trade_size": 1,
        },
    },
]


def upgrade() -> None:
    # 1. portfolio_runs.smoke column + index.
    op.add_column(
        "portfolio_runs",
        sa.Column(
            "smoke",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_portfolio_runs_smoke",
        "portfolio_runs",
        ["smoke"],
        unique=False,
    )

    # 2. Seed canonical smoke Strategy rows (idempotent on re-run).
    conn = op.get_bind()
    for entry in SMOKE_STRATEGIES:
        existing = conn.execute(
            sa.text("SELECT id FROM strategies WHERE name = :name"),
            {"name": entry["name"]},
        ).first()
        if existing is not None:
            continue
        conn.execute(
            sa.text(
                """
                INSERT INTO strategies (
                    id, name, file_path, strategy_class, config_class,
                    default_config, created_at, updated_at
                )
                VALUES (
                    :id, :name, :file_path, :strategy_class, :config_class,
                    CAST(:cfg AS JSONB), now(), now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "name": entry["name"],
                "file_path": entry["file_path"],
                "strategy_class": entry["strategy_class"],
                "config_class": entry["config_class"],
                "cfg": json.dumps(entry["default_config"]),
            },
        )


def downgrade() -> None:
    # Drop the column first — that is the load-bearing schema reversal and
    # always succeeds. The seeded-strategy cleanup below is best-effort.
    op.drop_index("ix_portfolio_runs_smoke", table_name="portfolio_runs")
    op.drop_column("portfolio_runs", "smoke")

    # Remove the four seeded smoke Strategy rows by EXACT name match.
    #
    # Codex PR review P2: a ``LIKE '__smoke__/%'`` pattern treats ``_`` as a
    # single-character wildcard, so it would match unrelated operator
    # strategies whose names happen to fit the shape (e.g. ``XYsmokeAB/foo``)
    # and delete them on rollback. Bind the four seeded names explicitly.
    #
    # Best-effort + savepoint-guarded: once the smoke has actually run, the
    # canonical ``__msai_smoke__`` portfolio bootstrap creates default
    # ``graduation_candidates`` that FK-reference these strategy rows, so a
    # plain DELETE raises ForeignKeyViolationError. That runtime data is NOT
    # this migration's to cascade-delete, and the column drop above already
    # reversed the schema. So we attempt the seed cleanup inside a SAVEPOINT
    # and tolerate an FK violation (leaving the four sentinel rows in place)
    # rather than abort the whole downgrade. A clean dev DB with no candidates
    # deletes them; a used one keeps them harmlessly (sentinel-named, unused
    # once the smoke feature code is gone).
    conn = op.get_bind()
    seeded_names = [entry["name"] for entry in SMOKE_STRATEGIES]
    try:
        with conn.begin_nested():
            conn.execute(
                sa.text("DELETE FROM strategies WHERE name IN :names").bindparams(
                    sa.bindparam("names", expanding=True)
                ),
                {"names": seeded_names},
            )
    except sa.exc.IntegrityError:
        # Runtime graduation_candidates reference the seed rows; leave them.
        pass
