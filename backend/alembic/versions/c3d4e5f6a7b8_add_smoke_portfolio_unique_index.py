"""add partial unique index on portfolios.name for smoke sentinel

Code-review iter-1 fix #3 — the smoke runner's
``_get_or_create_canonical_portfolio`` SELECT-or-CREATE path is racy:
two concurrent ``run_smoke`` invocations (CLI + UI + scheduler can all
hit this) can both miss the sentinel-name lookup and create their own
``__msai_smoke__`` row, which leaves the table with duplicate canonical
portfolios. Subsequent ``scalar_one_or_none()`` calls then raise
``MultipleResultsFound``.

Fix: add a **partial** unique index on ``portfolios.name`` scoped to
the smoke sentinel only. Operator-created portfolios keep using the
existing non-unique ``name`` column (a sensible UX — users may want
"Tech basket" / "Tech basket v2" without uniqueness pain), but two
concurrent smoke bootstraps now race on the database-level constraint
and exactly one wins; the loser's ``flush`` raises ``IntegrityError``,
the runner catches it and re-SELECTs to find the winner's row.

Additive-only per ``.claude/rules/database.md``:

- ADD a partial unique index (transparent to old code that never
  inserted a duplicate sentinel-name row in the first place).
- No DROP / RENAME / data-type narrowing.

PRD: ``docs/prds/ingest-backtest-smoke-test.md`` v1.3.

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f7
Create Date: 2026-05-26
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_portfolios_smoke_sentinel"
_SENTINEL_NAME = "__msai_smoke__"


def upgrade() -> None:
    # Partial unique index keyed by the literal sentinel name. The WHERE
    # clause is what makes the constraint apply ONLY to the smoke
    # canonical row — operator-created portfolios with any other name
    # (including duplicates among themselves) are untouched. Alembic
    # accepts a raw SQL string here and emits it verbatim into the
    # ``CREATE UNIQUE INDEX ... WHERE <expr>`` DDL.
    op.create_index(
        _INDEX_NAME,
        "portfolios",
        ["name"],
        unique=True,
        postgresql_where=f"name = '{_SENTINEL_NAME}'",
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="portfolios")
