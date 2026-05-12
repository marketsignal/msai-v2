"""backdate time-invariant alias effective_from to far-past anchor

Revision ID: b6a7b8c9d0e1
Revises: a5y6z7a8b9c0
Create Date: 2026-05-12

NautilusTrader's instrument model gives time-bounded asset classes
(futures, options, *_spreads) an ``expiration_ns`` field but leaves
equity / FX (CurrencyPair) / crypto-perpetual / index lifecycle-free —
those identities are time-invariant. Empirically verified in
``nautilus_trader/model/instruments/{equity,futures_contract,
currency_pair}.pyx``.

Our registry's ``instrument_aliases.effective_from/effective_to`` window
models the lifecycle correctly for futures (``ESH4.GLBX`` IS bounded by
contract expiry) but is wrong for equities (``AAPL.NASDAQ`` applies to
all time). The original bootstrap stamped ``effective_from=today`` for
all asset classes, so historical backtests with
``start_date < bootstrap_date`` cold-missed the alias windowing filter
even though the alias is conceptually always active. Surfaced when the
first end-to-end prod AAPL backtest with ``start_date=2025-11-03``
422-ed against an alias bootstrapped today.

This migration backdates the ``effective_from`` of every existing alias
on a time-invariant asset class (equity / fx / crypto) to a far-past
anchor ``1900-01-01``. New aliases inserted by ``_upsert_definition_and_alias``
get the same anchor going forward (asset-class-aware logic in
``service.py``).

Migration shape: additive in semantics — narrows the existing window's
LEFT bound, never widens the right (``effective_to`` is untouched).
Old code that filters ``effective_from <= as_of`` keeps working; new
code returns more rows for historical dates. Rollback safety: the
``downgrade`` resets only equity/fx/crypto aliases that were backdated
TO the anchor, so a re-deploy of the prior image continues to find
"today-anchored" rows (the prior image only knows how to read them).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b6a7b8c9d0e1"
down_revision = "a5y6z7a8b9c0"
branch_labels = None
depends_on = None

# Mirror of ``_TIME_INVARIANT_ASSET_CLASSES`` in
# ``services/nautilus/security_master/service.py``. Kept in sync via the
# unit test ``test_backdate_time_invariant_aliases_migration_matches_runtime_constant``.
_TIME_INVARIANT_ASSET_CLASSES: tuple[str, ...] = ("equity", "fx", "crypto")


def upgrade() -> None:
    # Backdate ``effective_from`` to the far-past anchor for every
    # active (not-yet-closed) alias whose parent definition is on a
    # time-invariant asset class. Closed aliases (``effective_to IS
    # NOT NULL``) are untouched — they represent historical venue
    # migrations the registry still needs to differentiate.
    #
    # Two-step guard against ``uq_instrument_aliases_string_provider_from``
    # unique-constraint violations:
    #
    # 1. **Pre-existing-anchor guard.** If any sibling row with the
    #    same ``(alias_string, provider)`` already has
    #    ``effective_from = 1900-01-01`` (legitimate post-migration
    #    state on a re-run, or a test fixture seeding the anchor
    #    directly), skip the backdate. The resolver's path-2c
    #    ``find_by_raw_symbol`` fallback still finds the unchanged row
    #    via the definition link.
    #
    # 2. **Same-statement collision guard.** If multiple active rows
    #    exist for the same ``(alias_string, provider)`` — a legitimate
    #    transient state during a same-day venue swap AND a common
    #    test-fixture shape — backdating ALL of them to ``1900-01-01``
    #    in a single statement collides on the second row even though
    #    no pre-existing anchor row exists. Use ``ctid`` (Postgres's
    #    physical-row identifier) + ``ROW_NUMBER`` to pick ONE row per
    #    group (the row with the lowest current ``effective_from``,
    #    ties broken by ``ctid`` for determinism) and only backdate
    #    that. The other duplicates retain their original
    #    ``effective_from`` and remain discoverable via raw_symbol
    #    lookup.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT a.ctid AS row_id,
                       ROW_NUMBER() OVER (
                         PARTITION BY a.alias_string, a.provider
                         ORDER BY a.effective_from ASC, a.ctid ASC
                       ) AS rn
                FROM instrument_aliases a
                JOIN instrument_definitions d ON a.instrument_uid = d.instrument_uid
                WHERE a.effective_to IS NULL
                  AND d.asset_class IN ('equity', 'fx', 'crypto')
                  AND a.effective_from > DATE '1900-01-01'
                  AND NOT EXISTS (
                      SELECT 1 FROM instrument_aliases sib
                      WHERE sib.alias_string = a.alias_string
                        AND sib.provider = a.provider
                        AND sib.effective_from = DATE '1900-01-01'
                  )
            )
            UPDATE instrument_aliases a
            SET effective_from = DATE '1900-01-01'
            FROM ranked
            WHERE a.ctid = ranked.row_id
              AND ranked.rn = 1
            """
        )
    )


def downgrade() -> None:
    # Forward-only deploy pipeline doesn't roll DB schemas back, but
    # provide a best-effort downgrade for local dev: reset the
    # backdated rows to today's date so the prior image's
    # ``effective_from=today`` semantics continue to fire (the
    # ``find_by_alias`` filter would then miss historical dates,
    # matching pre-migration behavior).
    op.execute(
        sa.text(
            """
            UPDATE instrument_aliases AS a
            SET effective_from = CURRENT_DATE
            FROM instrument_definitions AS d
            WHERE a.instrument_uid = d.instrument_uid
              AND a.effective_to IS NULL
              AND d.asset_class IN ('equity', 'fx', 'crypto')
              AND a.effective_from = DATE '1900-01-01'
            """
        )
    )
