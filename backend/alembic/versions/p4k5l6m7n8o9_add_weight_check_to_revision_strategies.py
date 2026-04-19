"""add weight range CHECK to live_portfolio_revision_strategies

Revision ID: p4k5l6m7n8o9
Revises: o3i4j5k6l7m8
Create Date: 2026-04-16 22:00:00.000000

Closes a P1 type-design finding on PR#1 code-review: the ``weight``
column was ``Numeric(8, 6)`` with no range check, so the row
``weight = Decimal("-0.5")`` or ``weight = Decimal("1.5")`` would
persist. Portfolio weights in this codebase are always positive
allocation fractions; a negative or >1 weight is a programming error
that the supervisor cannot translate into a sensible trading decision.

Enforce ``weight > 0 AND weight <= 1`` at the DB layer so the
invariant cannot be bypassed by any future caller that skips the
service-level validation.
"""

from __future__ import annotations

from alembic import op

revision: str = "p4k5l6m7n8o9"
down_revision: str = "o3i4j5k6l7m8"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_lprs_weight_range",
        "live_portfolio_revision_strategies",
        "weight > 0 AND weight <= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_lprs_weight_range",
        "live_portfolio_revision_strategies",
        type_="check",
    )
