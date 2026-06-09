"""Migration test for the additive ``broker_accounts.account_class`` column
(dashboard-account-selector PR4, Task 2).

Runs ``alembic upgrade head`` against a GUARANTEED-fresh, dedicated PostgreSQL
testcontainer (mirrors ``test_broker_account_fk_migration.py``) and verifies the
ADDITIVE migration:

  * ``broker_accounts`` gains a NOT NULL ``account_class`` String(16) column with
    a ``server_default`` of ``'test'`` so existing rows backfill;
  * a pre-seeded paper row backfills to ``account_class='paper'`` and a pre-seeded
    live row backfills to ``account_class='test'`` (the one-time operator
    heuristic — NOT runtime inference; the fund is registered explicitly as
    ``'real'`` post-PR-3, plan D2/D5);
  * ``alembic downgrade -1`` cleanly drops the column.

The dedicated module-scoped container guarantees the "fresh database" check is
actually exercised and never reads ``DATABASE_URL`` — see the docstring of
``test_alembic_migrations.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._alembic_subprocess import run_alembic

if TYPE_CHECKING:
    from collections.abc import Iterator

# Parent revision = the head BEFORE this migration (the account_class migration's
# down_revision).
_DOWN_REVISION = "81e7efe6d772"


@pytest.fixture(scope="module")
def isolated_postgres_url_account_class() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL`` — see the
    ``test_alembic_migrations.py`` module docstring.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


async def _seed_two_accounts(url: str) -> None:
    # broker_accounts exists at _DOWN_REVISION (created by d87c2aa5f751). Insert
    # one paper + one live row BEFORE the account_class column exists.
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for ib_acct, mode, slot in [("DUTEST01", "paper", "slot-a"), ("UTEST01", "live", "slot-b")]:
            await conn.execute(
                sa.text(
                    "INSERT INTO broker_accounts "
                    "(id, ib_account_id, ib_login_key, status, gateway_slot, trading_mode, "
                    " credentials_backend, credentials_secret_ref, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :a, :k, 'active', :s, :m, 'env', 'ref', "
                    "now(), now())"
                ),
                {"a": ib_acct, "k": f"key-{ib_acct}", "s": slot, "m": mode},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_class_column_added_and_backfilled(
    isolated_postgres_url_account_class: str,
) -> None:
    url = isolated_postgres_url_account_class
    # ARRANGE: bring the schema up to the revision just below this migration, seed.
    run_alembic(url, "upgrade", _DOWN_REVISION)
    await _seed_two_accounts(url)
    # ACT: run this PR's migration.
    run_alembic(url, "upgrade", "head")

    def _account_class_col(sync_conn: sa.Connection) -> dict[str, object] | None:
        cols = {c["name"]: c for c in inspect(sync_conn).get_columns("broker_accounts")}
        return cols.get("account_class")

    # ASSERT (post-upgrade): column present + NOT NULL, and rows backfilled.
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        col = await conn.run_sync(_account_class_col)  # run_sync — inspect needs a sync conn
        assert col is not None, "account_class column missing after upgrade"
        assert col["nullable"] is False
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT trading_mode, account_class FROM broker_accounts ORDER BY trading_mode"
                )
            )
        ).all()
    by_mode = {tm: ac for tm, ac in rows}
    assert by_mode["paper"] == "paper"
    assert by_mode["live"] == "test"

    # ASSERT (post-downgrade): the additive column is gone, nothing else broke.
    run_alembic(url, "downgrade", "-1")
    async with engine.connect() as conn:
        assert await conn.run_sync(_account_class_col) is None, (
            "account_class column not dropped on downgrade"
        )
    await engine.dispose()
