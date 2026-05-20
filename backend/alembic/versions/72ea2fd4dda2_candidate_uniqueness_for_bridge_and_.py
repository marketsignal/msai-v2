"""candidate_uniqueness_for_bridge_and_live_synthesis

Codex bot iter-10 P1 on PR #73 — both candidate-creation paths (the F1c
strategy_ids bridge and the iter-3 promote-to-live live_candidate
synthesis) had read-then-insert TOCTOU races. Two concurrent callers for
the same strategy both observed "no existing row" and both inserted,
producing duplicates that would later break:

- ``_get_or_create_default_candidate``: subsequent calls hit
  ``MultipleResultsFound`` from ``scalar_one_or_none()`` on the
  ``stage='portfolio_default'`` lookup.
- ``materialize_from_backtest``: subsequent ``start-portfolio`` calls
  see two unlinked ``live_candidate`` rows for the same strategy and
  return ``BINDING_AMBIGUOUS``.

Fix shape: two partial UNIQUE indexes serialize concurrent inserts at
the database level. The application code catches ``IntegrityError`` and
re-reads to handle the conflict (handler logic lives in the application
layer, not here).

Migration discipline (per ``.claude/rules/database.md``):
- Additive-only (no DROP/RENAME on existing columns).
- Indexes use ``IF NOT EXISTS`` semantically via try/except in code; the
  Alembic ``op.create_index`` call is idempotent on re-run because we
  use a deterministic ``index_name``.
- Existing duplicates are pre-deduplicated KEEPING the oldest row (by
  ``created_at``), so the index can be added cleanly even on a
  long-running DB.

Revision ID: 72ea2fd4dda2
Revises: b063ef2dd543
Create Date: 2026-05-19 12:08:47.897679
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "72ea2fd4dda2"
down_revision: str | None = "b063ef2dd543"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Codex bot iter-11 P2 on PR #73: tie-break on ``id`` after ``created_at``
# so duplicates that share an exact timestamp (e.g., bulk inserts or
# same-tick server defaults) still get pruned down to exactly ONE row.
# A strict ``created_at > created_at`` comparison alone would leave both
# tied rows behind and the unique index creation would still fail.
# PostgreSQL's row-tuple comparison ``(a.col1, a.col2) > (b.col1, b.col2)``
# is lexicographic — same created_at falls through to the id comparison,
# which is a UUID (guaranteed distinct per row).
_DEDUPE_PORTFOLIO_DEFAULT = """
DELETE FROM graduation_candidates a
USING graduation_candidates b
WHERE a.stage = 'portfolio_default'
  AND b.stage = 'portfolio_default'
  AND a.strategy_id = b.strategy_id
  AND (a.created_at, a.id) > (b.created_at, b.id);
"""

_DEDUPE_UNLINKED_LIVE = """
DELETE FROM graduation_candidates a
USING graduation_candidates b
WHERE a.stage = 'live_candidate'
  AND b.stage = 'live_candidate'
  AND a.deployment_id IS NULL
  AND b.deployment_id IS NULL
  AND a.strategy_id = b.strategy_id
  AND (a.created_at, a.id) > (b.created_at, b.id);
"""


def upgrade() -> None:
    # Step 1: deduplicate any pre-existing duplicates so the unique
    # indexes can be added without violating the constraint. Keeps the
    # OLDEST row per (strategy_id, stage) tuple — that's the row the
    # bridge / promotion would have observed first, so reusing it is
    # consistent with the application's idempotency expectations.
    op.execute(_DEDUPE_PORTFOLIO_DEFAULT)
    op.execute(_DEDUPE_UNLINKED_LIVE)

    # Step 2: partial unique indexes. PostgreSQL ``CREATE UNIQUE INDEX
    # ... WHERE ...`` lets the constraint apply only to the relevant
    # subset of rows, so other graduation stages are unaffected.
    op.create_index(
        "uq_portfolio_default_candidate_per_strategy",
        "graduation_candidates",
        ["strategy_id"],
        unique=True,
        postgresql_where=sa.text("stage = 'portfolio_default'"),
    )
    op.create_index(
        "uq_unlinked_live_candidate_per_strategy",
        "graduation_candidates",
        ["strategy_id"],
        unique=True,
        postgresql_where=sa.text("stage = 'live_candidate' AND deployment_id IS NULL"),
    )


def downgrade() -> None:
    # Drop the indexes — does NOT restore the deleted duplicate rows.
    # That's intentional: the indexes are the safety guarantee; the
    # duplicates they prevented were ALREADY corrupting downstream
    # behavior before this migration ran.
    op.drop_index(
        "uq_unlinked_live_candidate_per_strategy",
        table_name="graduation_candidates",
    )
    op.drop_index(
        "uq_portfolio_default_candidate_per_strategy",
        table_name="graduation_candidates",
    )
