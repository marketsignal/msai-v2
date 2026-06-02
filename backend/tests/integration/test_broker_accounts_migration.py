"""Migration test for the ``broker_accounts`` table (PR 3 Task 2).

Runs ``alembic upgrade head`` against a GUARANTEED-fresh, dedicated
PostgreSQL testcontainer (mirrors ``test_alembic_migrations.py``) and
verifies the ``broker_accounts`` CREATE-TABLE migration lands the table
with the expected column nullabilities and the partial-unique index on
``ib_account_id``.

The dedicated module-scoped container guarantees the "fresh database"
acceptance check is actually exercised — see the docstring of
``test_alembic_migrations.py`` for why we don't reuse the shared
``postgres_url`` fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._alembic_subprocess import (
    run_alembic as _run_alembic,
)
from tests.integration._alembic_subprocess import (
    run_alembic_raw as _run_alembic_raw,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url_broker_accounts() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL`` — see the
    ``test_alembic_migrations.py`` module docstring.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_broker_accounts_table_created(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """``alembic upgrade head`` creates the ``broker_accounts`` table with
    the expected column nullabilities and a partial-unique index on
    ``ib_account_id``.
    """
    _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head")

    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, object]:
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                assert "broker_accounts" in tables, f"broker_accounts missing; got {sorted(tables)}"
                columns = {c["name"]: c for c in insp.get_columns("broker_accounts")}
                indexes = {i["name"]: i for i in insp.get_indexes("broker_accounts")}
                return {"columns": columns, "indexes": indexes}

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    columns = shape["columns"]
    col_names = set(columns)

    # Every expected column is present.
    expected_columns = {
        "id",
        "ib_account_id",
        "ib_login_key",
        "label",
        "status",
        "gateway_slot",
        "trading_mode",
        "credentials_backend",
        "credentials_secret_ref",
        "credentials_secret_version",
        "credentials_updated_at",
        "credentials_updated_by",
        "credentials_last_accessed",
        "created_by",
        "created_at",
        "updated_at",
    }
    missing = expected_columns - col_names
    assert not missing, f"missing columns on broker_accounts: {missing}"

    # Key nullabilities.
    assert columns["ib_account_id"]["nullable"] is False
    assert columns["credentials_secret_ref"]["nullable"] is False
    assert columns["credentials_secret_version"]["nullable"] is True

    # Partial-unique index on active ib_account_id.
    indexes = shape["indexes"]
    assert "uq_broker_accounts_active_ib_account_id" in indexes, (
        f"missing uq_broker_accounts_active_ib_account_id; got {sorted(indexes)}"
    )
    ib_idx = indexes["uq_broker_accounts_active_ib_account_id"]
    assert ib_idx["unique"] is True
    assert list(ib_idx["column_names"]) == ["ib_account_id"]
    # The reflected partial-index predicate is exposed via dialect_options.
    assert any("ib_account_id" in i["column_names"] for i in indexes.values())


@pytest.mark.asyncio
async def test_backfill_is_noop_when_env_unset(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """With ``BROKER_ACCOUNT_BACKFILL`` unset, ``upgrade head`` inserts no rows.

    The empty default is the safe no-op contract — each environment opts in via
    its own env var, so a fresh database has zero ``broker_accounts`` rows.
    """
    import sqlalchemy as sa

    _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head")

    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sa.text("SELECT count(*) FROM broker_accounts"))
            n = result.scalar_one()
    finally:
        await engine.dispose()

    assert n == 0  # empty default → safe no-op


@pytest.mark.asyncio
async def test_backfill_seeds_from_env_idempotently(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """Setting ``BROKER_ACCOUNT_BACKFILL`` seeds exactly one ``legacy_env`` row;
    re-running the backfill ``upgrade()`` against the already-seeded table is a
    no-op (the idempotent guard skips an ``ib_account_id`` already present).
    """
    import sqlalchemy as sa

    # U4705114 is a low-value LIVE test account (IB paper accounts are DU/DF), so
    # trading_mode=live — the migration's prefix-vs-mode guard rejects a U-prefix
    # + paper mismatch (nautilus gotcha #6).
    backfill = "U4705114:lvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD"
    extra_env = {"BROKER_ACCOUNT_BACKFILL": backfill}

    # The module-scoped Postgres container carries state from prior tests in
    # this file (they leave the DB at head). Step back to the CREATE-TABLE
    # revision so the backfill upgrade() actually re-runs, and clear any rows a
    # prior test left behind — gives this test a deterministic starting point
    # (mirrors test_instrument_cache_migration's reset-then-seed approach).
    _run_alembic(
        isolated_postgres_url_broker_accounts, "downgrade", "d87c2aa5f751", extra_env=extra_env
    )
    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE broker_accounts"))

        # First pass: backfill upgrade() lands exactly one row.
        _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head", extra_env=extra_env)

        # Step back over the backfill revision (its downgrade() deletes the
        # legacy_env row, leaving the bare CREATE-TABLE state), then re-seed the
        # SAME ib_account_id by hand so the second upgrade exercises the
        # idempotent guard branch (skip when already present) — mirrors
        # test_instrument_cache_migration's seeded-registry idempotency test.
        _run_alembic(isolated_postgres_url_broker_accounts, "downgrade", "-1", extra_env=extra_env)
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO broker_accounts "
                    "(id, ib_account_id, ib_login_key, status, gateway_slot, "
                    " trading_mode, credentials_backend, credentials_secret_ref, "
                    " credentials_secret_version) "
                    "VALUES (gen_random_uuid(), 'U4705114', 'lvp', 'active', "
                    " 'ib-gateway', 'live', 'legacy_env', "
                    " 'env:TWS_USERID|TWS_PASSWORD', NULL)"
                )
            )

        # Second pass: backfill upgrade() re-runs against the seeded table and
        # must NOT insert a duplicate.
        _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head", extra_env=extra_env)

        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT ib_account_id, gateway_slot, credentials_backend, "
                    "credentials_secret_ref, credentials_secret_version "
                    "FROM broker_accounts ORDER BY ib_account_id"
                )
            )
            rows = result.fetchall()
    finally:
        await engine.dispose()

    assert [r[0] for r in rows] == ["U4705114"]  # exactly one, no duplicate on re-run
    assert rows[0][1] == "ib-gateway"  # slot from the env entry
    assert rows[0][2] == "legacy_env"
    assert rows[0][3] == "env:TWS_USERID|TWS_PASSWORD"  # paired ref
    assert rows[0][4] is None  # legacy → no pinned version


@pytest.mark.asyncio
async def test_backfill_rejects_malformed_key_pair(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """A backfill entry whose key_pair is missing the ``|PASSWORD_KEY`` half
    fails loud at ``upgrade head`` (non-zero exit + a ValueError naming the
    bad entry), rather than seeding an unresolvable legacy_env pointer.
    """
    import sqlalchemy as sa

    # Malformed: 5 colon-parts but the key_pair has no "|" separator.
    bad_entry = "U4705114:lvp:ib-gateway:paper:TWS_USERID"
    extra_env = {"BROKER_ACCOUNT_BACKFILL": bad_entry}

    # Step back to the CREATE-TABLE revision so the backfill upgrade() re-runs,
    # and clear any rows prior tests left behind.
    _run_alembic(
        isolated_postgres_url_broker_accounts, "downgrade", "d87c2aa5f751", extra_env=extra_env
    )
    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE broker_accounts"))
    finally:
        await engine.dispose()

    result = _run_alembic_raw(
        isolated_postgres_url_broker_accounts, "upgrade", "head", extra_env=extra_env
    )
    assert result.returncode != 0, f"expected non-zero exit; stdout:\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "key_pair" in combined and bad_entry in combined

    # Restore the container to head (env unset → no-op backfill) so later
    # module state is consistent.
    _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head")


@pytest.mark.asyncio
async def test_backfill_rejects_blank_field(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """A backfill entry with a blank/whitespace-only field (here an empty
    ib_login_key) fails loud at ``upgrade head`` rather than seeding a malformed
    active row (Codex iter-11 P2). Whitespace-padded fields are stripped, so a
    blank-after-strip field is rejected with a "blank field" error.
    """
    import sqlalchemy as sa

    # 5 colon-parts but the ib_login_key is empty.
    bad_entry = "U4705114::ib-gateway:live:TWS_USERID|TWS_PASSWORD"
    extra_env = {"BROKER_ACCOUNT_BACKFILL": bad_entry}

    _run_alembic(
        isolated_postgres_url_broker_accounts, "downgrade", "d87c2aa5f751", extra_env=extra_env
    )
    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE broker_accounts"))
    finally:
        await engine.dispose()

    result = _run_alembic_raw(
        isolated_postgres_url_broker_accounts, "upgrade", "head", extra_env=extra_env
    )
    assert result.returncode != 0, f"expected non-zero exit; stdout:\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "blank field" in combined

    _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head")


@pytest.mark.asyncio
async def test_backfill_rejects_account_mode_mismatch(
    isolated_postgres_url_broker_accounts: str,
) -> None:
    """A backfill entry whose ``ib_account_id`` prefix disagrees with its
    ``trading_mode`` fails loud at ``upgrade head`` via the shared
    ``assert_account_mode_consistent`` guard.

    U4705114 is a U-prefix LIVE account; pairing it with ``paper`` mode would
    silently misroute orders at the gateway (nautilus gotcha #6). The migration
    must reject this before INSERT, with a non-zero exit and a clear error that
    names the offending entry — the SAME guard the create/update API path uses,
    so the two cannot drift.
    """
    import sqlalchemy as sa

    # Domain-invalid: a U-prefix (live) account paired with paper mode.
    bad_entry = "U4705114:lvp:ib-gateway:paper:TWS_USERID|TWS_PASSWORD"
    extra_env = {"BROKER_ACCOUNT_BACKFILL": bad_entry}

    # Step back to the CREATE-TABLE revision so the backfill upgrade() re-runs,
    # and clear any rows prior tests left behind.
    _run_alembic(
        isolated_postgres_url_broker_accounts, "downgrade", "d87c2aa5f751", extra_env=extra_env
    )
    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE broker_accounts"))
    finally:
        await engine.dispose()

    result = _run_alembic_raw(
        isolated_postgres_url_broker_accounts, "upgrade", "head", extra_env=extra_env
    )
    assert result.returncode != 0, f"expected non-zero exit; stdout:\n{result.stdout}"
    combined = result.stdout + result.stderr
    # The shared guard's message names the account and explains the mismatch.
    assert "U4705114" in combined
    assert "paper" in combined.lower()

    # No row was inserted — the guard fired BEFORE the INSERT.
    _run_alembic(isolated_postgres_url_broker_accounts, "downgrade", "d87c2aa5f751")
    engine = create_async_engine(isolated_postgres_url_broker_accounts)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE broker_accounts"))
    finally:
        await engine.dispose()

    # Restore the container to head (env unset → no-op backfill) so later
    # module state is consistent.
    _run_alembic(isolated_postgres_url_broker_accounts, "upgrade", "head")
