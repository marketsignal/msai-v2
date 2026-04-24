"""relax effective_window CHECK to allow same-day alias rotations

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-04-23 00:00:00.000000

Background
----------
``ck_instrument_aliases_effective_window`` previously enforced
``effective_to > effective_from`` (strict). This crashed a legitimate
production path: when ``_upsert_definition_and_alias`` is called twice
in the same calendar day with different alias_strings (e.g. an ETF
venue migration caught the same day it was seeded), the closing UPDATE
sets ``effective_to = today`` on a row whose ``effective_from`` is also
``today`` — ``today > today`` is false and the CHECK rejects.

Relax to ``effective_to >= effective_from``. A zero-width window
``[F, F)`` is a valid audit row (instantaneously superseded); the
half-open interval contains no dates, so it's never selected as the
active alias — semantically clean.
"""

from __future__ import annotations

from alembic import op

revision = "b6c7d8e9f0a1"
down_revision = "a5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_instrument_aliases_effective_window",
        "instrument_aliases",
        type_="check",
    )
    op.create_check_constraint(
        "ck_instrument_aliases_effective_window",
        "instrument_aliases",
        "effective_to IS NULL OR effective_to >= effective_from",
    )


def downgrade() -> None:
    # Self-cleaning downgrade: Postgres re-validates existing rows when a
    # CHECK constraint is re-created. Rows written under the relaxed
    # ``>=`` invariant (``effective_to = effective_from`` zero-width audit
    # rows for same-day rotations) would violate the restored strict
    # ``>``, causing the downgrade to fail exactly in the scenario the
    # upgrade was meant to enable. Delete those rows before re-creating
    # the strict CHECK. A zero-width window never made a row "active" —
    # the half-open ``[F, F)`` interval contains no dates — so deleting
    # it has no impact on current or historical alias resolution.
    op.execute(
        "DELETE FROM instrument_aliases "
        "WHERE effective_to IS NOT NULL AND effective_to = effective_from"
    )
    op.drop_constraint(
        "ck_instrument_aliases_effective_window",
        "instrument_aliases",
        type_="check",
    )
    op.create_check_constraint(
        "ck_instrument_aliases_effective_window",
        "instrument_aliases",
        "effective_to IS NULL OR effective_to > effective_from",
    )
