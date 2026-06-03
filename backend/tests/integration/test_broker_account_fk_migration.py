"""Migration test for the ``live_deployments`` broker_account FK + validation
stamp columns (broker-account-spawn-wiring PR, Task 1).

Runs ``alembic upgrade head`` against a GUARANTEED-fresh, dedicated
PostgreSQL testcontainer (mirrors ``test_broker_accounts_migration.py``) and
verifies the ADDITIVE migration:

  * ``live_deployments`` gains a NULLABLE ``broker_account_id`` column with a
    FK to ``broker_accounts.id`` and ``ondelete='RESTRICT'``;
  * NULLABLE ``credentials_validated_at`` (tz-aware DateTime);
  * NULLABLE ``credentials_validated_version`` (String);
  * a pre-inserted legacy ``live_deployments`` row (``broker_account_id`` NULL)
    SURVIVES the upgrade (the migration is purely additive — metadata-only ADD
    COLUMN nullable, no rewrite, no backfill);
  * ``alembic downgrade -1`` cleanly drops the index, the FK, and the three
    columns, leaving a legacy row still readable.

The dedicated module-scoped container guarantees the "fresh database"
acceptance check is actually exercised — see the docstring of
``test_alembic_migrations.py`` for why we don't reuse the shared
``postgres_url`` fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._alembic_subprocess import run_alembic as _run_alembic

if TYPE_CHECKING:
    from collections.abc import Iterator


# Revision that ADDS the FK + validation-stamp columns (this PR, Task 1).
_FK_REVISION = "head"
# The revision immediately BELOW the FK migration (current head before this PR).
_DOWN_REVISION = "d97a64e13e4e"


@pytest.fixture(scope="module")
def isolated_postgres_url_broker_account_fk() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL`` — see the
    ``test_alembic_migrations.py`` module docstring.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


_LEGACY_ENTRA_ID = "entra-legacy-fk-test"


def _placeholder_for_column(col: dict[str, object], known: dict[str, object]) -> object:
    """Return a SQL-insertable bind value for a NOT NULL ``live_deployments``
    column that has no server default, driven by the reflected column TYPE.

    ``live_deployments`` has drifted across migrations (PR#2 dropped
    ``config``/``instruments``/``strategy_code_hash``; later PRs added
    ``account_id``/``identity_signature``/``portfolio_revision_id``/
    ``ib_login_key``), so we build the INSERT from the live schema rather than a
    hard-coded column list that would silently go stale as the chain evolves.

    FK/identity columns are supplied from ``known`` (real parent ids seeded
    above); everything else gets a type-appropriate literal.
    """
    name = col["name"]
    if name in known:
        return known[name]
    type_str = str(col["type"]).upper()
    if "JSON" in type_str:
        return sa.text("'{}'::jsonb")
    if "[]" in type_str or "ARRAY" in type_str:
        return sa.text("ARRAY[]::varchar[]")
    if "BOOL" in type_str:
        return sa.text("false")
    if "INT" in type_str or "NUMERIC" in type_str or "DECIMAL" in type_str:
        return sa.text("0")
    if "TIMESTAMP" in type_str or "DATETIME" in type_str:
        return sa.text("now()")
    if "UUID" in type_str:
        # A NOT NULL UUID with no FK we recognize: a fresh value is safe.
        return sa.text("gen_random_uuid()")
    # Default: a short string literal (covers String/Text columns).
    return sa.text("'legacy'")


async def _seed_legacy_deployment(engine: object) -> str:
    """Seed a minimal user + portfolio→revision parent chain, then insert a
    legacy ``live_deployments`` row (``broker_account_id`` NULL) so the
    additive-migration survival check has a real pre-existing row to assert
    against. Returns the deployment id (text).

    The ``live_deployments`` INSERT is built from the REFLECTED schema at the
    pre-FK revision so it survives the table's migration drift. Recognized
    FK / identity columns are filled with the real parent ids seeded here; all
    other NOT NULL columns without a server default get a type-appropriate
    literal.
    """
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        # Parent user.
        user_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO users (id, entra_id, email, role) "
                    "VALUES (gen_random_uuid(), :entra, 'legacy-fk@test.local', 'admin') "
                    "RETURNING id"
                ),
                {"entra": _LEGACY_ENTRA_ID},
            )
        ).scalar_one()

        # Parent portfolio → revision (satisfies the NOT NULL FK
        # ``live_deployments.portfolio_revision_id`` introduced by PR#2).
        portfolio_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO live_portfolios (id, name, created_by) "
                    "VALUES (gen_random_uuid(), 'legacy-fk-portfolio', :uid) RETURNING id"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO live_portfolio_revisions "
                    "(id, portfolio_id, revision_number, composition_hash) "
                    "VALUES (gen_random_uuid(), :pid, 1, 'deadbeef') RETURNING id"
                ),
                {"pid": portfolio_id},
            )
        ).scalar_one()

        def _columns(sync_conn: object) -> list[dict[str, object]]:
            return inspect(sync_conn).get_columns("live_deployments")

        columns = await conn.run_sync(_columns)

        # FK / identity columns we satisfy with real values.
        known: dict[str, object] = {
            "started_by": user_id,
            "portfolio_revision_id": revision_id,
        }

        assignments: dict[str, object] = {"id": sa.text("gen_random_uuid()")}
        for col in columns:
            name = col["name"]
            if name == "id":
                continue
            if col["nullable"]:
                continue  # leave NULL — includes the post-PR2 nullable strategy_id
            if col.get("default") is not None:
                continue  # server default covers it (status, created_at, ...)
            assignments[name] = _placeholder_for_column(col, known)

        cols_sql = ", ".join(assignments)
        vals_sql = ", ".join(
            v.text if isinstance(v, sa.TextClause) else f":{k}" for k, v in assignments.items()
        )
        bind_params = {k: v for k, v in assignments.items() if not isinstance(v, sa.TextClause)}
        dep_id = (
            await conn.execute(
                sa.text(
                    f"INSERT INTO live_deployments ({cols_sql}) "  # noqa: S608 — names from reflection
                    f"VALUES ({vals_sql}) RETURNING id"
                ),
                bind_params,
            )
        ).scalar_one()
    return str(dep_id)


@pytest.mark.asyncio
async def test_live_deployment_broker_account_fk_added(
    isolated_postgres_url_broker_account_fk: str,
) -> None:
    """``alembic upgrade head`` adds the nullable ``broker_account_id`` FK
    (RESTRICT) + the two validation-stamp columns to ``live_deployments``.
    """
    url = isolated_postgres_url_broker_account_fk
    _run_alembic(url, "upgrade", _FK_REVISION)

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, object]:
                insp = inspect(sync_conn)
                columns = {c["name"]: c for c in insp.get_columns("live_deployments")}
                indexes = {i["name"]: i for i in insp.get_indexes("live_deployments")}
                fks = insp.get_foreign_keys("live_deployments")
                return {"columns": columns, "indexes": indexes, "fks": fks}

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    columns = shape["columns"]
    col_names = set(columns)

    # All three new columns are present and NULLABLE (additive, metadata-only).
    assert "broker_account_id" in col_names, f"missing broker_account_id; got {sorted(col_names)}"
    assert "credentials_validated_at" in col_names
    assert "credentials_validated_version" in col_names
    assert columns["broker_account_id"]["nullable"] is True
    assert columns["credentials_validated_at"]["nullable"] is True
    assert columns["credentials_validated_version"]["nullable"] is True

    # credentials_validated_at is tz-aware.
    assert getattr(columns["credentials_validated_at"]["type"], "timezone", False) is True

    # FK on broker_account_id → broker_accounts.id with ondelete RESTRICT.
    fk = next(
        (f for f in shape["fks"] if f["constrained_columns"] == ["broker_account_id"]),
        None,
    )
    assert fk is not None, f"no FK on broker_account_id; got {shape['fks']}"
    assert fk["referred_table"] == "broker_accounts"
    assert fk["referred_columns"] == ["id"]
    assert fk["options"].get("ondelete", "").upper() == "RESTRICT"

    # Index on broker_account_id exists.
    assert "ix_live_deployments_broker_account_id" in shape["indexes"], (
        f"missing ix_live_deployments_broker_account_id; got {sorted(shape['indexes'])}"
    )


@pytest.mark.asyncio
async def test_legacy_row_survives_upgrade_and_downgrade(
    isolated_postgres_url_broker_account_fk: str,
) -> None:
    """A legacy ``live_deployments`` row (``broker_account_id`` NULL) survives
    the upgrade, and ``downgrade -1`` cleanly drops the index, FK, and the three
    columns while leaving the legacy row intact.
    """
    url = isolated_postgres_url_broker_account_fk

    # Reset to BELOW the FK migration so we start from a row WITHOUT the new
    # columns, seed a legacy row, then upgrade across the FK migration.
    _run_alembic(url, "downgrade", _DOWN_REVISION)
    engine = create_async_engine(url)
    try:
        # Clean slate for deterministic seeding (FK-safe delete order:
        # deployments → revisions → portfolios → strategies → users).
        async with engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM live_deployments"))
            await conn.execute(sa.text("DELETE FROM live_portfolio_revisions"))
            await conn.execute(sa.text("DELETE FROM live_portfolios"))
            await conn.execute(sa.text("DELETE FROM strategies"))
            await conn.execute(
                sa.text("DELETE FROM users WHERE entra_id = :entra"),
                {"entra": _LEGACY_ENTRA_ID},
            )
        dep_id = await _seed_legacy_deployment(engine)

        # Upgrade across the additive FK migration — the legacy row must survive
        # with broker_account_id defaulting to NULL.
        _run_alembic(url, "upgrade", _FK_REVISION)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT id, broker_account_id, credentials_validated_at, "
                        "credentials_validated_version FROM live_deployments WHERE id = :id"
                    ),
                    {"id": dep_id},
                )
            ).one()
        assert str(row[0]) == dep_id  # row survived the upgrade
        assert row[1] is None  # broker_account_id NULL (no backfill)
        assert row[2] is None
        assert row[3] is None

        # Downgrade one step — drops index, FK, and the three columns cleanly.
        _run_alembic(url, "downgrade", "-1")
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, object]:
                insp = inspect(sync_conn)
                columns = {c["name"] for c in insp.get_columns("live_deployments")}
                indexes = {i["name"] for i in insp.get_indexes("live_deployments")}
                return {"columns": columns, "indexes": indexes}

            shape = await conn.run_sync(_inspect)
            survivor = (
                await conn.execute(
                    sa.text("SELECT id FROM live_deployments WHERE id = :id"),
                    {"id": dep_id},
                )
            ).scalar_one()

        assert "broker_account_id" not in shape["columns"]
        assert "credentials_validated_at" not in shape["columns"]
        assert "credentials_validated_version" not in shape["columns"]
        assert "ix_live_deployments_broker_account_id" not in shape["indexes"]
        assert str(survivor) == dep_id  # legacy row still readable after downgrade
    finally:
        await engine.dispose()
        # Restore the container to head so later module state is consistent.
        _run_alembic(url, "upgrade", _FK_REVISION)
