"""drop instrument_cache after migrating rows to registry

Revision B of the instrument-cache → registry migration.

Steps:
    1. Reflect ``instrument_cache``, ``instrument_definitions``, and
       ``instrument_aliases`` via op.get_bind() (do NOT import the
       models — brittle pattern).
    2. Iterate every ``instrument_cache`` row, parse ``canonical_id``
       into ``raw_symbol`` + ``listing_venue``, upsert into
       ``instrument_definitions`` (ON CONFLICT DO UPDATE so pre-existing
       rows pick up migrated trading_hours), then upsert into
       ``instrument_aliases`` (ON CONFLICT DO NOTHING on the
       ``(alias_string, provider, effective_from)`` unique constraint).
    3. Carry forward ``trading_hours`` JSONB to
       ``instrument_definitions.trading_hours`` (added by Revision A).
    4. ``DROP TABLE instrument_cache``.

Drops:
    - ``ib_contract_json`` — IB authority, re-qualify on demand.
    - ``nautilus_instrument_json`` — Nautilus Cache(database=redis)
      is the runtime persistence layer.

Fail-loud on any row whose ``canonical_id`` does not parse cleanly
(no '.' separator), whose parsed listing_venue is not in the closed
known-venue allowlist (catches share-class tickers like ``BRK.B``),
or whose legacy ``asset_class`` has no registry equivalent — operator
inspects + fixes via psql, then re-runs the migration.

Effective-date sentinel: aliases are written with ``effective_from =
2000-01-01``. These rows migrate already-active state, not creating
roll-day rotations. A far-past sentinel sidesteps timezone-boundary
inconsistencies between this migration (UTC) and resolver windows
(``exchange_local_today()`` in Chicago) AND lets idempotent re-runs
collapse on the ``(alias_string, provider, effective_from)`` unique
constraint regardless of which calendar day they execute.

Reversibility:
    Downgrade is **schema-only**: recreates an empty
    ``instrument_cache`` table. Data is NOT restored — the operator
    MUST have a ``pg_dump`` checkpoint taken before ``alembic upgrade
    head``. The migration's docstring + the runbook
    (docs/runbooks/instrument-cache-migration.md) document this
    loudly.

Revision: e2f3g4h5i6j7
Revises: d1e2f3g4h5i6
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Source-of-truth import: pull the registry's normalized exchange-name set
# from venue_normalization so the share-class allowlist below cannot drift
# from PR #44's closed MIC map. Without this union, legitimate venues like
# AMEX / IEX / MEMX / PEARL would falsely trigger the share-class fail-loud
# trap on rows like ``BABA.AMEX``.
from msai.services.nautilus.security_master.venue_normalization import (
    _DATABENTO_MIC_TO_EXCHANGE_NAME,
)

revision = "e2f3g4h5i6j7"
down_revision = "d1e2f3g4h5i6"
branch_labels = None
depends_on = None


log = logging.getLogger("alembic.runtime.migration")


# Legacy instrument_cache.asset_class taxonomy → registry taxonomy.
# Cache uses (equity|future|forex|option|index); registry CHECK constraint
# ck_instrument_definitions_asset_class allows (equity|futures|fx|option|
# crypto). ``index`` has no registry equivalent and is fail-loud — operator
# inspects + decides.
_ASSET_CLASS_MAP: dict[str, str] = {
    "equity": "equity",
    "future": "futures",
    "forex": "fx",
    "option": "option",
}


# Closed allowlist of venue suffixes the registry recognizes. A
# canonical_id whose suffix is not in this set is almost always a
# share-class ticker (``BRK.B``, ``BF.B``, ``RDS.A``) that wasn't
# venue-suffixed by the legacy writer. Fail-loud rather than write a
# silently-corrupt registry row whose listing_venue (e.g. ``"B"``)
# would never IB-qualify.
#
# Built as the UNION of (a) hand-curated IB venues + MIC codes the legacy
# cache writer emitted directly, plus (b) every normalized exchange-name
# value in the venue_normalization map (PR #44 closed MIC map: AMEX, IEX,
# MEMX, PEARL, BOSTON, BATY, EDGA, EDGX, CHX, NSX, etc.). Hand-coding a
# parallel list is the exact drift hazard PR #44 was designed to close.
_DATABENTO_NORMALIZED_VENUES: frozenset[str] = frozenset(_DATABENTO_MIC_TO_EXCHANGE_NAME.values())
_IB_AND_MIC_VENUES: frozenset[str] = frozenset(
    {
        "ARCA",
        "BATS",
        "CBOE",
        "CBOT",
        "CME",
        "COMEX",
        "EUREX",
        "GLOBEX",
        "ICEFUTUSA",
        "IDEALPRO",
        "NASDAQ",
        "NYMEX",
        "NYSE",
        "OPRA",
        "SMART",
        "TSE",
        "XARC",
        "XCME",
        "XNAS",
        "XNYS",
    }
)
KNOWN_VENUES: frozenset[str] = _IB_AND_MIC_VENUES | _DATABENTO_NORMALIZED_VENUES


# Far-past effective_from sentinel. See module docstring for rationale.
_EFFECTIVE_FROM_SENTINEL: Final[date] = date(2000, 1, 1)


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()

    # Reflection — let SQLAlchemy load the actual current shape.
    cache = sa.Table("instrument_cache", metadata, autoload_with=bind)
    defs = sa.Table("instrument_definitions", metadata, autoload_with=bind)
    aliases = sa.Table("instrument_aliases", metadata, autoload_with=bind)

    rows = bind.execute(sa.select(cache)).mappings().all()
    log.info("copying %d instrument_cache rows → registry", len(rows))

    now = datetime.now(UTC)

    for row in rows:
        canonical_id = row["canonical_id"]
        if "." not in canonical_id:
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} does not parse: "
                f"no '.' separator. Inspect the row in psql and fix at source "
                f"before re-running."
            )
        raw_symbol, listing_venue = canonical_id.rsplit(".", 1)

        # Share-class trap: ``BRK.B``/``BF.B``/``RDS.A`` parse to
        # listing_venue=``B``/``A`` which would silently corrupt the
        # registry. The closed allowlist forces the operator to fix
        # the source row (proper venue suffix) before retrying.
        if listing_venue not in KNOWN_VENUES:
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} parsed "
                f"listing_venue={listing_venue!r} which is not in the known-venue "
                f"set. This is likely a share-class ticker (BRK.B / BF.B / RDS.A) "
                f"that wasn't venue-suffixed. Inspect the row + fix at source. "
                f"Known venues: {sorted(KNOWN_VENUES)!r}"
            )

        # Asset class taxonomy translation. Normalize whitespace + case
        # before lookup so case-typo rows fail-loud with the right
        # diagnosis ("not in map") rather than the wrong one ("no
        # registry equivalent" only fits ``index``).
        legacy_asset_class = (row["asset_class"] or "").strip().lower()
        asset_class = _ASSET_CLASS_MAP.get(legacy_asset_class)
        if asset_class is None and legacy_asset_class == "index":
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} has "
                f"asset_class='index' which has no registry equivalent. "
                f"Inspect + decide before re-running."
            )
        if asset_class is None:
            raise RuntimeError(
                f"instrument_cache row canonical_id={canonical_id!r} has "
                f"asset_class={row['asset_class']!r} (normalized: "
                f"{legacy_asset_class!r}); not in {_ASSET_CLASS_MAP!r}."
            )

        # Routing venue: prefer ib_contract_json["exchange"] (the routing
        # exchange IB used at qualification time, e.g. "SMART"), falling
        # back to the canonical-id suffix (e.g. "NASDAQ"). Listing venue
        # is the canonical-id suffix (the venue we'd subscribe data on).
        ib_contract_json = row.get("ib_contract_json") or {}
        routing_venue = ib_contract_json.get("exchange") or listing_venue

        trading_hours = row.get("trading_hours")  # may be None
        refreshed_at = row.get("last_refreshed_at", now)

        # Definition upsert: ON CONFLICT DO UPDATE so pre-existing
        # registry rows pick up the migrated trading_hours + refreshed_at.
        # COALESCE preserves existing trading_hours if the cache row's is
        # NULL but the registry already has data. RETURNING gives us the
        # actual ``instrument_uid`` (whether we inserted or hit the
        # conflict), saving a follow-up SELECT — mirrors the runtime
        # pattern in
        # ``security_master.service._upsert_definition_and_alias``.
        defs_stmt = postgresql.insert(defs).values(
            instrument_uid=uuid.uuid4(),
            raw_symbol=raw_symbol,
            provider="interactive_brokers",
            asset_class=asset_class,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
            lifecycle_state="active",
            trading_hours=trading_hours,
            refreshed_at=refreshed_at,
            created_at=now,
            updated_at=now,
        )
        existing_def = bind.execute(
            defs_stmt.on_conflict_do_update(
                index_elements=["raw_symbol", "provider", "asset_class"],
                set_={
                    # NULLIF guard: asyncpg + psycopg both bind Python
                    # ``None`` as the JSONB literal ``'null'`` (distinct
                    # from SQL NULL), so plain COALESCE silently
                    # overwrites the existing row when the cache row's
                    # ``trading_hours`` is None. NULLIF coerces the
                    # JSON ``null`` to SQL NULL so COALESCE picks up the
                    # existing value. Mirrors the runtime upsert in
                    # ``security_master.service._upsert_definition_and_alias``.
                    "trading_hours": sa.func.coalesce(
                        sa.func.nullif(
                            defs_stmt.excluded.trading_hours,
                            sa.text("'null'::jsonb"),
                        ),
                        defs.c.trading_hours,
                    ),
                    "refreshed_at": defs_stmt.excluded.refreshed_at,
                    "updated_at": now,
                },
            ).returning(defs.c.instrument_uid)
        ).scalar_one()

        # Belt+suspenders: close any prior active alias for this
        # ``(instrument_uid, provider)`` whose alias_string differs from
        # canonical_id. Mirrors the runtime rotation pattern in
        # ``SecurityMaster._upsert_definition_and_alias`` — prevents two
        # open windows when the migration is re-run after a manual fix
        # in the same row's history. With the far-past sentinel for the
        # NEW row this UPDATE only fires on a true alias mismatch (e.g.
        # mid-migration roll), not on idempotent same-row re-runs.
        #
        # Use ``now.date()`` (NOT _EFFECTIVE_FROM_SENTINEL) for the
        # close-prior ``effective_to`` so the resulting alias window is
        # ``[old_effective_from, now)``, satisfying the post-PR-#44 CHECK
        # ``effective_to IS NULL OR effective_to >= effective_from``. Any
        # prior alias from a real ``instruments refresh`` run has its OWN
        # ``effective_from`` post-2000-01-01, so stamping the sentinel
        # here would produce ``effective_to (2000-01-01) < effective_from
        # (e.g. 2026-04-15)`` → IntegrityError → migration aborts mid-loop.
        # The migrated row uses ``_EFFECTIVE_FROM_SENTINEL`` because IT
        # is the new alias being created (no prior ``effective_to`` to
        # honor); the closed-prior row has an established window.
        bind.execute(
            sa.update(aliases)
            .where(
                aliases.c.instrument_uid == existing_def,
                aliases.c.provider == "interactive_brokers",
                aliases.c.effective_to.is_(None),
                aliases.c.alias_string != canonical_id,
            )
            .values(effective_to=now.date())
        )

        # Alias upsert (idempotent on the
        # (alias_string, provider, effective_from) unique constraint).
        bind.execute(
            postgresql.insert(aliases)
            .values(
                id=uuid.uuid4(),
                instrument_uid=existing_def,
                alias_string=canonical_id,
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=_EFFECTIVE_FROM_SENTINEL,
                effective_to=None,
                created_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_instrument_aliases_string_provider_from",
            )
        )

    op.drop_table("instrument_cache")
    log.info("dropped instrument_cache table")


def downgrade() -> None:
    """Schema-only downgrade: recreate empty instrument_cache.

    DATA IS NOT RESTORED. The operator must restore from ``pg_dump`` if
    the rows are needed. This is documented loudly in the runbook
    (docs/runbooks/instrument-cache-migration.md).
    """
    op.create_table(
        "instrument_cache",
        sa.Column("canonical_id", sa.String(128), primary_key=True),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("ib_contract_json", postgresql.JSONB, nullable=False),
        sa.Column("nautilus_instrument_json", postgresql.JSONB, nullable=False),
        sa.Column("trading_hours", postgresql.JSONB, nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_instrument_cache_class_venue",
        "instrument_cache",
        ["asset_class", "venue"],
    )
