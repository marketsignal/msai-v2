"""normalize strategy file paths relative to strategies root

Revision ID: f6a7b8c9d0e1
Revises: b50fd33a0a8c
Create Date: 2026-10-02 19:52:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str = "b50fd33a0a8c"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def _normalize_path(value: str) -> str:
    """Return the portable portion below the nearest ``strategies`` root."""
    portable = value.replace("\\", "/")
    while portable.startswith("./"):
        portable = portable[2:]
    if portable.startswith("strategies/"):
        return portable.removeprefix("strategies/")
    marker = "/strategies/"
    if marker in portable:
        return portable.rsplit(marker, maxsplit=1)[1]
    return value


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, file_path FROM strategies")).mappings().all()
    update = sa.text("UPDATE strategies SET file_path = :file_path WHERE id = :id")
    for row in rows:
        normalized = _normalize_path(str(row["file_path"]))
        if normalized != row["file_path"]:
            bind.execute(update, {"id": row["id"], "file_path": normalized})


def downgrade() -> None:
    # Host-specific absolute prefixes are intentionally unrecoverable.  Relative
    # paths remain valid for both the old and new runtime readers.
    pass
