"""order_attempt_audits: tighten deployment/backtest check to XOR

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-04-07 19:30:00.000000

The Phase 1 task 1.2 model documentation says an ``order_attempt_audits``
row MUST belong to either a live deployment OR a backtest. The original
CHECK constraint only enforced "at least one" (``deployment_id IS NOT NULL
OR backtest_id IS NOT NULL``), which accepted rows with BOTH foreign
keys populated — an ambiguous state downstream reconciliation and
analytics cannot classify (Codex Task 1.2 iter2 P2 fix).

This migration replaces the constraint with a true XOR in Postgres
(``!=`` on booleans): exactly one of ``deployment_id`` /
``backtest_id`` must be non-NULL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT_NAME = "ck_order_attempt_audits_deployment_or_backtest"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "order_attempt_audits", type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "order_attempt_audits",
        "(deployment_id IS NOT NULL) != (backtest_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "order_attempt_audits", type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "order_attempt_audits",
        "(deployment_id IS NOT NULL) OR (backtest_id IS NOT NULL)",
    )
