"""End-to-end migration test: run ``alembic upgrade head`` against a fresh
database and verify every expected table and index lands.

This is the "implementation" of the acceptance criterion for Phase 1 task
1.1 ("``alembic upgrade head`` succeeds on a fresh database"). Additional
Phase 1 migrations (1.1b, 1.2, ...) will add their own assertions here.

We invoke Alembic as a subprocess so its own ``asyncio.run(...)`` inside
``alembic/env.py`` doesn't clash with pytest-asyncio's event loop. That
also matches how migrations actually run in production
(``uv run alembic upgrade head``).

SAFETY (Codex review of Task 1.1, P2):
This test provisions its OWN dedicated PostgreSQL testcontainer rather
than reusing the session-scoped ``postgres_url`` fixture. Two reasons:

1. The ``test_live_node_process_model.py`` fixture calls
   ``Base.metadata.create_all()`` on the shared database, which leaves
   it in a state that Alembic doesn't recognize (no ``alembic_version``
   row, tables already present). If that module ran before this test
   on the shared fixture, ``alembic upgrade head`` would either fail
   or silently no-op against tables Alembic didn't create.
2. The conftest ``postgres_url`` prefers an existing ``DATABASE_URL``
   env var. We don't want this test to mutate a configured dev DB
   even if the env var is set.

The dedicated container here guarantees the advertised
"fresh database" acceptance check is actually exercised.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL``. See module docstring.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        yield url


def _run_alembic(
    database_url: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Run an arbitrary ``alembic`` subcommand as a subprocess.

    The project's ``alembic/env.py`` reads ``settings.database_url`` which
    in turn reads the ``DATABASE_URL`` env var, so setting it in the
    subprocess env is sufficient to override the default. ``extra_env``
    lets callers override other pydantic-settings fields (e.g.
    ``STRATEGIES_ROOT``, ``IB_ACCOUNT_ID``) the same way.
    """
    backend_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _run_alembic_upgrade(
    database_url: str,
    target: str = "head",
    *,
    extra_env: dict[str, str] | None = None,
) -> None:
    _run_alembic(database_url, "upgrade", target, extra_env=extra_env)


@pytest.mark.asyncio
async def test_alembic_upgrade_head_creates_live_node_processes(
    isolated_postgres_url: str,
) -> None:
    """``alembic upgrade head`` runs cleanly on a GUARANTEED-fresh
    database and the ``live_node_processes`` table + expected indexes
    exist afterwards.
    """
    _run_alembic_upgrade(isolated_postgres_url)

    # Verify the table exists and has the expected columns and indexes.
    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, list[str]]:
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                assert "live_node_processes" in tables, (
                    f"live_node_processes missing; got {sorted(tables)}"
                )

                columns = {col["name"]: col for col in insp.get_columns("live_node_processes")}
                indexes = {idx["name"]: idx for idx in insp.get_indexes("live_node_processes")}
                return {"columns": list(columns), "indexes": list(indexes)}

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    expected_columns = {
        "id",
        "deployment_id",
        "pid",
        "host",
        "started_at",
        "last_heartbeat_at",
        "status",
        "exit_code",
        "error_message",
        "failure_kind",
        "created_at",
        "updated_at",
    }
    actual_columns = set(shape["columns"])
    missing = expected_columns - actual_columns
    assert not missing, f"missing columns on live_node_processes: {missing}"

    # The FK index and the partial unique index must both exist.
    expected_indexes = {
        "ix_live_node_processes_deployment_id",
        "uq_live_node_processes_active_deployment",
    }
    actual_indexes = set(shape["indexes"])
    missing_indexes = expected_indexes - actual_indexes
    assert not missing_indexes, f"missing indexes: {missing_indexes}"


# ---------------------------------------------------------------------------
# Phase 1 task 1.1b — live_deployments stable identity
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_collision() -> Iterator[str]:
    """Fourth dedicated container for the collision-detection test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_1_1b_refuses_to_upgrade_with_duplicate_identity_rows(
    isolated_postgres_url_collision: str,
) -> None:
    """Codex Task 1.1b P1 fix — the upgrade must abort BEFORE any
    schema changes if pre-existing rows would collide on identity_signature.

    The old /start code inserted a fresh row on every restart, so a
    populated prod DB can legitimately contain two rows with the same
    (user, strategy, code_hash, config, account, paper, instruments)
    tuple. The new unique index would otherwise be created against
    already-colliding data and fail partway through upgrade(). We want
    to fail fast with a clear operator message listing the IDs.

    Strategy:
    1. Upgrade to b1c2d3e4f5a6 (pre-1.1b schema)
    2. Insert TWO live_deployments rows with identical identity-tuple fields
    3. Run alembic upgrade head
    4. Assert it fails
    5. Assert the stderr message mentions both row IDs so an operator can act
    """
    _run_alembic_upgrade(isolated_postgres_url_collision, target="b1c2d3e4f5a6")

    engine = create_async_engine(isolated_postgres_url_collision)
    from uuid import uuid4

    user_id = uuid4()
    strategy_id = uuid4()
    dup_a = uuid4()
    dup_b = uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO users (id, entra_id, email, role, created_at, updated_at)
                    VALUES (:id, :entra, :email, 'operator', NOW(), NOW())
                    """
                ),
                {
                    "id": user_id,
                    "entra": f"dup-{user_id.hex}",
                    "email": f"dup-{user_id.hex}@example.com",
                },
            )
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO strategies (
                        id, name, file_path, strategy_class, created_by,
                        created_at, updated_at
                    )
                    VALUES (:id, :name, :fp, :cls, :uid, NOW(), NOW())
                    """
                ),
                {
                    "id": strategy_id,
                    "name": "dup-test",
                    "fp": "strategies/dup.py",
                    "cls": "DupStrategy",
                    "uid": user_id,
                },
            )
            # Two rows with identical identity-tuple fields — same user,
            # same strategy, same code_hash, same config, same paper flag,
            # same instruments. Only the PK differs.
            for dep_id in (dup_a, dup_b):
                await conn.execute(
                    sa.text(
                        """
                        INSERT INTO live_deployments (
                            id, strategy_id, strategy_code_hash, config, instruments,
                            status, paper_trading, started_by, created_at, started_at
                        )
                        VALUES (
                            :id, :sid, :hash, CAST(:cfg AS JSONB),
                            CAST(:instr AS VARCHAR[]),
                            'stopped', true, :uid, NOW(), NOW()
                        )
                        """
                    ),
                    {
                        "id": dep_id,
                        "sid": strategy_id,
                        "hash": "deadbeef" * 8,
                        "cfg": '{"fast": 10, "slow": 20}',
                        "instr": ["AAPL.NASDAQ"],
                        "uid": user_id,
                    },
                )
    finally:
        await engine.dispose()

    # Running head must FAIL with a message about the collision
    import pytest as _pytest

    with _pytest.raises(AssertionError) as exc_info:
        _run_alembic_upgrade(isolated_postgres_url_collision)

    stderr = str(exc_info.value)
    assert "Cannot create unique index" in stderr
    assert "identity_signature" in stderr
    # Both colliding deployment IDs must appear in the operator message so
    # they can merge/delete the duplicates and re-run the migration.
    assert str(dup_a) in stderr
    assert str(dup_b) in stderr


# ---------------------------------------------------------------------------
# Phase 1 task 1.2 — order_attempt_audits
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_oa() -> Iterator[str]:
    """Dedicated container for the task 1.2 migration test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_alembic_upgrade_head_creates_order_attempt_audits(
    isolated_postgres_url_oa: str,
) -> None:
    """Phase 1 task 1.2: ``alembic upgrade head`` creates the
    ``order_attempt_audits`` table with all expected columns and the
    six query indexes the audit hook needs.
    """
    _run_alembic_upgrade(isolated_postgres_url_oa)

    engine = create_async_engine(isolated_postgres_url_oa)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, list[str]]:
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                assert "order_attempt_audits" in tables, (
                    f"order_attempt_audits missing; got {sorted(tables)}"
                )
                columns = {col["name"]: col for col in insp.get_columns("order_attempt_audits")}
                indexes = {idx["name"]: idx for idx in insp.get_indexes("order_attempt_audits")}
                return {"columns": list(columns), "indexes": list(indexes)}

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    expected_columns = {
        "id",
        "client_order_id",
        "deployment_id",
        "backtest_id",
        "strategy_id",
        "strategy_code_hash",
        "strategy_git_sha",
        "instrument_id",
        "side",
        "quantity",
        "price",
        "order_type",
        "ts_attempted",
        "status",
        "reason",
        "broker_order_id",
        "is_live",
        "created_at",
        "updated_at",
    }
    actual_columns = set(shape["columns"])
    missing = expected_columns - actual_columns
    assert not missing, f"missing columns on order_attempt_audits: {missing}"

    expected_indexes = {
        "ix_order_attempt_audits_client_order_id",
        "ix_order_attempt_audits_deployment_id",
        "ix_order_attempt_audits_backtest_id",
        "ix_order_attempt_audits_strategy_id",
        "ix_order_attempt_audits_instrument_id",
        "ix_order_attempt_audits_broker_order_id",
    }
    actual_indexes = set(shape["indexes"])
    missing_indexes = expected_indexes - actual_indexes
    assert not missing_indexes, f"missing indexes: {missing_indexes}"


@pytest.mark.asyncio
async def test_o3_portfolio_schema_roundtrip(isolated_postgres_url: str) -> None:
    """PR #1 schema: new tables + new columns + partial unique index land
    on upgrade; downgrade removes them cleanly; re-upgrade works."""
    _run_alembic(isolated_postgres_url, "upgrade", "head")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:

            def _collect(sync_conn: sa.Connection) -> dict:
                insp = sa.inspect(sync_conn)
                return {
                    "tables": set(insp.get_table_names()),
                    "dep_cols": {c["name"] for c in insp.get_columns("live_deployments")},
                    "proc_cols": {c["name"] for c in insp.get_columns("live_node_processes")},
                    "rev_indexes": {
                        idx["name"] for idx in insp.get_indexes("live_portfolio_revisions")
                    },
                }

            state = await conn.run_sync(_collect)
        assert "live_portfolios" in state["tables"]
        assert "live_portfolio_revisions" in state["tables"]
        assert "live_portfolio_revision_strategies" in state["tables"]
        assert "live_deployment_strategies" in state["tables"]
        assert "ib_login_key" in state["dep_cols"]
        assert "gateway_session_key" in state["proc_cols"]
        assert "uq_one_draft_per_portfolio" in state["rev_indexes"]
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "downgrade", "n2h3i4j5k6l7")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            tables_after_down = await conn.run_sync(
                lambda sc: set(sa.inspect(sc).get_table_names())
            )
        assert "live_portfolios" not in tables_after_down
        assert "live_portfolio_revisions" not in tables_after_down
        assert "live_portfolio_revision_strategies" not in tables_after_down
        assert "live_deployment_strategies" not in tables_after_down
    finally:
        await engine.dispose()

    _run_alembic(isolated_postgres_url, "upgrade", "head")


# ---------------------------------------------------------------------------
# PR#2 Task 10 — backfill legacy deployments as single-strategy portfolios
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_backfill_portfolios() -> Iterator[str]:
    """Dedicated container for the portfolio backfill migration test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_backfill_creates_portfolio_for_legacy_deployment(
    isolated_postgres_url_backfill_portfolios: str,
) -> None:
    """PR#2 Task 10: ``alembic upgrade head`` wraps each legacy deployment
    (``portfolio_revision_id IS NULL``) into a synthetic single-strategy
    portfolio with a frozen revision, a member row, and a deployment-strategy
    bridge row.

    Strategy:
    1. Upgrade to q5l6m7n8o9p0 (schema has portfolio tables + nullable FK).
    2. Insert a user, strategy, and deployment with no portfolio_revision_id.
    3. Upgrade to head (runs r6m7n8o9p0q1 backfill).
    4. Verify: portfolio, revision, member, deployment_strategy all created;
       deployment.portfolio_revision_id is set.
    """
    from uuid import uuid4

    # Step 1: upgrade to the revision just before the backfill
    _run_alembic_upgrade(
        isolated_postgres_url_backfill_portfolios,
        target="q5l6m7n8o9p0",
    )

    # Step 2: insert pre-existing data
    engine = create_async_engine(isolated_postgres_url_backfill_portfolios)
    user_id = uuid4()
    strategy_id = uuid4()
    deployment_id = uuid4()
    slug = "abcdef0123456789"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO users (id, entra_id, email, role, created_at, updated_at) "
                    "VALUES (:id, :entra, :email, 'operator', NOW(), NOW())"
                ),
                {
                    "id": user_id,
                    "entra": f"bf-{user_id.hex}",
                    "email": f"bf-{user_id.hex}@example.com",
                },
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO strategies ("
                    "  id, name, file_path, strategy_class, created_by,"
                    "  created_at, updated_at"
                    ") VALUES ("
                    "  :id, :name, :fp, :cls, :uid, NOW(), NOW()"
                    ")"
                ),
                {
                    "id": strategy_id,
                    "name": "backfill-portfolio-test",
                    "fp": "strategies/bf.py",
                    "cls": "BfStrategy",
                    "uid": user_id,
                },
            )
            # Insert deployment with all identity columns but NO portfolio_revision_id
            await conn.execute(
                sa.text(
                    "INSERT INTO live_deployments ("
                    "  id, strategy_id, strategy_code_hash, config, instruments,"
                    "  status, paper_trading, started_by, created_at,"
                    "  deployment_slug, identity_signature, trader_id,"
                    "  strategy_id_full, account_id, message_bus_stream,"
                    "  config_hash, instruments_signature"
                    ") VALUES ("
                    "  :id, :sid, :hash, CAST(:cfg AS JSONB),"
                    "  CAST(:instr AS VARCHAR[]),"
                    "  'stopped', true, :uid, NOW(),"
                    "  :slug, :sig, :tid,"
                    "  :sidf, :acct, :mbs,"
                    "  :cfgh, :isig"
                    ")"
                ),
                {
                    "id": deployment_id,
                    "sid": strategy_id,
                    "hash": "deadbeef" * 8,
                    "cfg": '{"fast": 10, "slow": 20}',
                    "instr": ["AAPL.NASDAQ", "MSFT.NASDAQ"],
                    "slug": slug,
                    "sig": "a" * 64,
                    "tid": f"MSAI-{slug}",
                    "sidf": f"BfStrategy-{slug}",
                    "acct": "DU0000000",
                    "mbs": f"trader-MSAI-{slug}-stream",
                    "cfgh": "b" * 64,
                    "isig": "AAPL.NASDAQ,MSFT.NASDAQ",
                    "uid": user_id,
                },
            )
    finally:
        await engine.dispose()

    # Step 3: upgrade to head — runs the backfill migration
    _run_alembic_upgrade(isolated_postgres_url_backfill_portfolios)

    # Step 4: verify the backfill created all expected rows
    engine = create_async_engine(isolated_postgres_url_backfill_portfolios)
    try:
        async with engine.connect() as conn:
            # Check deployment now has portfolio_revision_id set
            dep_row = (
                await conn.execute(
                    sa.text("SELECT portfolio_revision_id FROM live_deployments WHERE id = :id"),
                    {"id": deployment_id},
                )
            ).one()
            assert dep_row.portfolio_revision_id is not None

            revision_id = dep_row.portfolio_revision_id

            # Check the portfolio was created
            portfolio_row = (
                await conn.execute(
                    sa.text(
                        "SELECT lp.id, lp.name FROM live_portfolios lp "
                        "JOIN live_portfolio_revisions lpr ON lpr.portfolio_id = lp.id "
                        "WHERE lpr.id = :rid"
                    ),
                    {"rid": revision_id},
                )
            ).one()
            assert portfolio_row.name == f"Legacy-{slug}"

            # Check the revision
            rev_row = (
                await conn.execute(
                    sa.text(
                        "SELECT revision_number, is_frozen, composition_hash "
                        "FROM live_portfolio_revisions WHERE id = :rid"
                    ),
                    {"rid": revision_id},
                )
            ).one()
            assert rev_row.revision_number == 1
            assert rev_row.is_frozen is True
            assert rev_row.composition_hash is not None
            assert len(rev_row.composition_hash) == 64

            # Check the revision strategy member
            member_row = (
                await conn.execute(
                    sa.text(
                        "SELECT strategy_id, config, instruments, weight, order_index "
                        "FROM live_portfolio_revision_strategies WHERE revision_id = :rid"
                    ),
                    {"rid": revision_id},
                )
            ).one()
            assert member_row.strategy_id == strategy_id
            # Backfill intentionally starts the portfolio-member row with
            # empty config + empty instruments (r6m7n8o9p0q1 line 92 inserts
            # '{}'::jsonb, '{}'::text[]). Legacy config is not carried
            # forward — operators re-declare strategies under the new model.
            assert member_row.config == {}
            assert list(member_row.instruments) == []
            assert float(member_row.weight) == 1.0
            assert member_row.order_index == 0

            # Check the deployment strategy bridge row
            ds_row = (
                await conn.execute(
                    sa.text(
                        "SELECT deployment_id, strategy_id_full "
                        "FROM live_deployment_strategies "
                        "WHERE deployment_id = :did"
                    ),
                    {"did": deployment_id},
                )
            ).one()
            assert ds_row.strategy_id_full == f"BfStrategy-{slug}"
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def isolated_postgres_url_backfill_idempotent() -> Iterator[str]:
    """Dedicated container for the backfill idempotency test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_backfill_is_idempotent_skips_already_set(
    isolated_postgres_url_backfill_idempotent: str,
) -> None:
    """The backfill skips deployments that already have portfolio_revision_id
    set (e.g. deployments created via the new /live/start-portfolio endpoint
    after the portfolio tables were added but before the backfill ran).
    An empty live_deployments table is also a no-op.
    """
    # Upgrade all the way — the backfill runs on an empty table (no-op)
    _run_alembic_upgrade(isolated_postgres_url_backfill_idempotent)

    engine = create_async_engine(isolated_postgres_url_backfill_idempotent)
    try:
        async with engine.connect() as conn:
            # No Legacy-* portfolios should have been created
            count = (
                await conn.execute(
                    sa.text("SELECT COUNT(*) FROM live_portfolios WHERE name LIKE 'Legacy-%'")
                )
            ).scalar_one()
            assert count == 0
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def isolated_postgres_url_backfill_downgrade() -> Iterator[str]:
    """Dedicated container for the backfill downgrade test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_backfill_downgrade_removes_legacy_portfolios(
    isolated_postgres_url_backfill_downgrade: str,
) -> None:
    """Downgrading the backfill migration removes the synthetic Legacy-*
    portfolios and nulls out portfolio_revision_id on deployments.
    """
    from uuid import uuid4

    # Step 1: upgrade to just before the backfill
    _run_alembic_upgrade(
        isolated_postgres_url_backfill_downgrade,
        target="q5l6m7n8o9p0",
    )

    # Step 2: insert a legacy deployment
    engine = create_async_engine(isolated_postgres_url_backfill_downgrade)
    user_id = uuid4()
    strategy_id = uuid4()
    deployment_id = uuid4()
    slug = "fedcba9876543210"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO users (id, entra_id, email, role, created_at, updated_at) "
                    "VALUES (:id, :entra, :email, 'operator', NOW(), NOW())"
                ),
                {
                    "id": user_id,
                    "entra": f"dg-{user_id.hex}",
                    "email": f"dg-{user_id.hex}@example.com",
                },
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO strategies ("
                    "  id, name, file_path, strategy_class, created_by,"
                    "  created_at, updated_at"
                    ") VALUES ("
                    "  :id, :name, :fp, :cls, :uid, NOW(), NOW()"
                    ")"
                ),
                {
                    "id": strategy_id,
                    "name": "downgrade-test",
                    "fp": "strategies/dg.py",
                    "cls": "DgStrategy",
                    "uid": user_id,
                },
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO live_deployments ("
                    "  id, strategy_id, strategy_code_hash, config, instruments,"
                    "  status, paper_trading, started_by, created_at,"
                    "  deployment_slug, identity_signature, trader_id,"
                    "  strategy_id_full, account_id, message_bus_stream,"
                    "  config_hash, instruments_signature"
                    ") VALUES ("
                    "  :id, :sid, :hash, CAST(:cfg AS JSONB),"
                    "  CAST(:instr AS VARCHAR[]),"
                    "  'stopped', true, :uid, NOW(),"
                    "  :slug, :sig, :tid,"
                    "  :sidf, :acct, :mbs,"
                    "  :cfgh, :isig"
                    ")"
                ),
                {
                    "id": deployment_id,
                    "sid": strategy_id,
                    "hash": "deadbeef" * 8,
                    "cfg": '{"x": 1}',
                    "instr": ["SPY.ARCA"],
                    "slug": slug,
                    "sig": "c" * 64,
                    "tid": f"MSAI-{slug}",
                    "sidf": f"DgStrategy-{slug}",
                    "acct": "DU1111111",
                    "mbs": f"trader-MSAI-{slug}-stream",
                    "cfgh": "d" * 64,
                    "isig": "SPY.ARCA",
                    "uid": user_id,
                },
            )
    finally:
        await engine.dispose()

    # Step 3: upgrade to head (backfill runs)
    _run_alembic_upgrade(isolated_postgres_url_backfill_downgrade)

    # Verify backfill happened
    engine = create_async_engine(isolated_postgres_url_backfill_downgrade)
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.text("SELECT COUNT(*) FROM live_portfolios WHERE name LIKE 'Legacy-%'")
                )
            ).scalar_one()
            assert count == 1
    finally:
        await engine.dispose()

    # Step 4: downgrade back to q5l6m7n8o9p0 (removes the backfill)
    _run_alembic(
        isolated_postgres_url_backfill_downgrade,
        "downgrade",
        "q5l6m7n8o9p0",
    )

    # Step 5: verify the downgrade cleaned up
    engine = create_async_engine(isolated_postgres_url_backfill_downgrade)
    try:
        async with engine.connect() as conn:
            # No Legacy-* portfolios
            count = (
                await conn.execute(
                    sa.text("SELECT COUNT(*) FROM live_portfolios WHERE name LIKE 'Legacy-%'")
                )
            ).scalar_one()
            assert count == 0

            # Deployment's portfolio_revision_id is NULL again
            dep_row = (
                await conn.execute(
                    sa.text("SELECT portfolio_revision_id FROM live_deployments WHERE id = :id"),
                    {"id": deployment_id},
                )
            ).one()
            assert dep_row.portfolio_revision_id is None

            # No deployment_strategies for this deployment
            ds_count = (
                await conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM live_deployment_strategies WHERE deployment_id = :did"
                    ),
                    {"did": deployment_id},
                )
            ).scalar_one()
            assert ds_count == 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# PR#2 Task 11 — drop legacy columns, enforce portfolio_revision_id NOT NULL
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_drop_legacy() -> Iterator[str]:
    """Dedicated container for the Task 11 drop-legacy-columns test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_migration_drops_legacy_columns_keeps_identity(
    isolated_postgres_url_drop_legacy: str,
) -> None:
    """PR#2 Task 11: after ``alembic upgrade head`` (which includes the
    s7n8o9p0q1r2 migration), the 5 legacy per-strategy columns are gone,
    ``identity_signature`` is kept (P0-2 fix), ``portfolio_revision_id``
    is NOT NULL, ``strategy_id`` is nullable, and the new composite
    unique constraint ``uq_live_deployments_revision_account`` exists.
    """
    _run_alembic_upgrade(isolated_postgres_url_drop_legacy)

    engine = create_async_engine(isolated_postgres_url_drop_legacy)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict:
                insp = inspect(sync_conn)
                columns = {c["name"]: c for c in insp.get_columns("live_deployments")}
                indexes = {idx["name"]: idx for idx in insp.get_indexes("live_deployments")}
                unique_constraints = {
                    uc["name"]: uc for uc in insp.get_unique_constraints("live_deployments")
                }
                return {
                    "columns": columns,
                    "indexes": indexes,
                    "unique_constraints": unique_constraints,
                }

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    columns = shape["columns"]
    col_names = set(columns)

    # P0-2: identity_signature MUST be kept
    assert "identity_signature" in col_names, (
        "identity_signature was dropped but must be kept — upsert target depends on it"
    )

    # Dropped columns must be gone
    assert "config_hash" not in col_names, "config_hash should have been dropped"
    assert "instruments" not in col_names, "instruments should have been dropped"
    assert "instruments_signature" not in col_names, (
        "instruments_signature should have been dropped"
    )
    assert "strategy_code_hash" not in col_names, "strategy_code_hash should have been dropped"
    assert "config" not in col_names, "config should have been dropped"

    # portfolio_revision_id was made NOT NULL by u9p0q1r2s3t4 (PR #31) after
    # the legacy /start endpoint was deprecated. Every live deployment now
    # lives under a portfolio revision.
    assert columns["portfolio_revision_id"]["nullable"] is False, (
        "portfolio_revision_id should be NOT NULL after the u9p0q1r2s3t4 migration"
    )

    # strategy_id must be nullable
    assert columns["strategy_id"]["nullable"] is True, (
        "strategy_id should be nullable after Task 11 migration"
    )

    # The composite unique constraint must exist
    unique_constraints = shape["unique_constraints"]
    assert "uq_live_deployments_revision_account" in unique_constraints, (
        f"missing uq_live_deployments_revision_account; got {sorted(unique_constraints)}"
    )
    uc = unique_constraints["uq_live_deployments_revision_account"]
    assert set(uc["column_names"]) == {"portfolio_revision_id", "account_id"}


# ---------------------------------------------------------------------------
# PR — backtest auto-ingest on missing data: Task B0
# y3s4t5u6v7w8 adds 4 additive nullable auto-heal columns to backtests.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_auto_heal() -> Iterator[str]:
    """Dedicated container for the Task B0 auto-heal columns migration test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_y3_backtest_auto_heal_columns_roundtrip(
    isolated_postgres_url_auto_heal: str,
) -> None:
    """y3s4t5u6v7w8 adds phase/progress_message/heal_started_at/heal_job_id,
    all nullable. Upgrade -> inspect -> downgrade -> re-upgrade must all
    succeed cleanly.
    """
    # Upgrade to head (includes y3s4t5u6v7w8)
    _run_alembic_upgrade(isolated_postgres_url_auto_heal)

    engine = create_async_engine(isolated_postgres_url_auto_heal)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, tuple[bool, str]]:
                insp = inspect(sync_conn)
                cols = {c["name"]: c for c in insp.get_columns("backtests")}
                return {
                    name: (cols[name]["nullable"], str(cols[name]["type"]).lower())
                    for name in (
                        "phase",
                        "progress_message",
                        "heal_started_at",
                        "heal_job_id",
                    )
                    if name in cols
                }

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    assert set(shape) == {
        "phase",
        "progress_message",
        "heal_started_at",
        "heal_job_id",
    }, f"missing auto-heal columns: {shape}"

    # All 4 must be nullable
    for name, (nullable, _type) in shape.items():
        assert nullable is True, f"{name} must be nullable, got nullable={nullable}"

    # Type spot-checks (Postgres type names as emitted by SQLAlchemy reflection)
    assert "varchar" in shape["phase"][1]
    assert "text" in shape["progress_message"][1]
    assert "timestamp" in shape["heal_started_at"][1]
    assert "varchar" in shape["heal_job_id"][1]

    # Downgrade past y3s4t5u6v7w8 (drops the 4 columns), then re-upgrade.
    # Use an explicit revision target rather than ``-1`` so later migrations
    # chained after y3 don't silently change what this "one step down" means.
    _run_alembic(isolated_postgres_url_auto_heal, "downgrade", "x2r3s4t5u6v7")

    engine = create_async_engine(isolated_postgres_url_auto_heal)
    try:
        async with engine.connect() as conn:
            cols_after_down = await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("backtests")}
            )
    finally:
        await engine.dispose()
    assert "phase" not in cols_after_down
    assert "progress_message" not in cols_after_down
    assert "heal_started_at" not in cols_after_down
    assert "heal_job_id" not in cols_after_down

    # Re-upgrade must re-land the columns.
    _run_alembic_upgrade(isolated_postgres_url_auto_heal)


# ---------------------------------------------------------------------------
# Backtest series + series_status columns (z4x5y6z7a8b9):
#   - series JSONB NULL (canonical daily-normalized payload)
#   - series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'
#     with CHECK constraint limiting values to the 3-value Literal set.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url_series() -> Iterator[str]:
    """Dedicated container for the series columns migration test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


async def _fetch_series_columns(database_url: str) -> list[sa.Row]:
    """Query ``information_schema`` for the 2 new series columns on
    ``backtests``. Returns rows of (column_name, data_type, is_nullable,
    column_default) sorted by column_name.

    Used to assert real on-disk shape (NOT SQLAlchemy reflection) for both
    the initial upgrade AND the re-upgrade after a round-trip.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'backtests' "
                    "AND column_name IN ('series', 'series_status') "
                    "ORDER BY column_name"
                )
            )
            return list(result)
    finally:
        await engine.dispose()


async def _fetch_series_status_check_constraint(database_url: str) -> str | None:
    """Return the ``pg_get_constraintdef(oid)`` for ``ck_backtests_series_status``,
    or ``None`` if the constraint doesn't exist.

    Used to verify the CHECK invariant survives upgrade/downgrade/re-upgrade —
    the column default alone doesn't stop a SQL UPDATE from poisoning reads.
    """
    engine = create_async_engine(database_url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) "
                    "FROM pg_constraint "
                    "WHERE conname = 'ck_backtests_series_status'"
                )
            )
            row = result.first()
            return row[0] if row is not None else None
    finally:
        await engine.dispose()


def _assert_series_columns_present(rows: list[sa.Row]) -> None:
    """Assert both series + series_status landed with correct types + constraints."""
    assert len(rows) == 2, f"expected both series + series_status columns, got {rows}"

    series = next(r for r in rows if r[0] == "series")
    assert series[1] == "jsonb", f"series should be jsonb, got {series[1]}"
    assert series[2] == "YES", f"series should be nullable, got is_nullable={series[2]}"
    assert series[3] is None, f"series should have no default, got {series[3]}"

    status_col = next(r for r in rows if r[0] == "series_status")
    assert status_col[1] == "character varying", (
        f"series_status should be varchar, got {status_col[1]}"
    )
    assert status_col[2] == "NO", (
        f"series_status should be NOT NULL, got is_nullable={status_col[2]}"
    )
    assert "'not_materialized'" in (status_col[3] or ""), (
        f"series_status should default to 'not_materialized', got {status_col[3]}"
    )


@pytest.mark.asyncio
async def test_migration_z4x5y6z7a8b9_adds_series_columns(
    isolated_postgres_url_series: str,
) -> None:
    """Migration z4x5y6z7a8b9 adds ``series`` JSONB NULL and
    ``series_status`` VARCHAR(32) NOT NULL DEFAULT 'not_materialized'
    on ``backtests``. Both are nullable-safe / metadata-only on Postgres 16
    — no table rewrite, no backfill required.

    Verify via information_schema so we assert the real on-disk shape,
    not SQLAlchemy's reflected interpretation. Round-trips
    upgrade -> downgrade -> re-upgrade (mirrors the sibling
    ``test_y3_backtest_auto_heal_columns_roundtrip``).
    """
    # Upgrade to head (includes z4x5y6z7a8b9) and assert both columns present
    _run_alembic_upgrade(isolated_postgres_url_series)
    _assert_series_columns_present(await _fetch_series_columns(isolated_postgres_url_series))

    # The CHECK constraint must exist after upgrade. Values outside the
    # 3-value set are rejected at write-time instead of producing a 500
    # at read-time through the API's Pydantic Literal narrowing.
    check_def = await _fetch_series_status_check_constraint(isolated_postgres_url_series)
    assert check_def is not None, "ck_backtests_series_status missing after upgrade"
    for allowed in ("ready", "not_materialized", "failed"):
        assert allowed in check_def, f"allowed value {allowed!r} missing from CHECK def {check_def}"

    # Critical: attempt an actual rejecting INSERT. The metadata check
    # above would pass even if the constraint was defined as
    # ``CHECK (series_status IN (...) OR TRUE)`` or scoped to the wrong
    # column. This end-to-end write exercises the real enforcement.
    engine_reject = create_async_engine(
        isolated_postgres_url_series.replace("postgresql://", "postgresql+asyncpg://"),
    )
    try:
        # Seed the FK parent so the poisoned insert reaches the CHECK
        # (FK violations fire before CHECKs and would mask the test).
        async with engine_reject.begin() as seed_conn:
            strategy_id_row = await seed_conn.execute(
                sa.text(
                    "INSERT INTO strategies "
                    "(id, name, file_path, strategy_class, "
                    "config_schema_status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), 'ck-test', "
                    "'/nonexistent.py', 'Test', "
                    "'no_config_class', now(), now()) "
                    "RETURNING id"
                )
            )
            strategy_id = strategy_id_row.scalar_one()

        # The poisoned INSERT must raise IntegrityError pointing at
        # ``ck_backtests_series_status``.
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            async with engine_reject.begin() as reject_conn:
                await reject_conn.execute(
                    sa.text(
                        "INSERT INTO backtests "
                        "(id, strategy_id, strategy_code_hash, config, "
                        "instruments, start_date, end_date, series_status) "
                        "VALUES (gen_random_uuid(), :sid, :sch, "
                        "'{}'::jsonb, ARRAY['x'], "
                        "'2024-01-01'::date, '2024-01-02'::date, 'bogus')"
                    ),
                    {"sid": strategy_id, "sch": "x" * 64},
                )
        assert "ck_backtests_series_status" in str(excinfo.value), (
            f"constraint name missing from IntegrityError: {excinfo.value}"
        )
    finally:
        await engine_reject.dispose()

    # Downgrade ONE step to y3s4t5u6v7w8 (immediate parent of z4x5y6z7a8b9)
    # to drop both columns. Explicit revision target — NOT ``-1`` — so later
    # migrations chained after z4 don't silently change what "one step" means.
    _run_alembic(isolated_postgres_url_series, "downgrade", "y3s4t5u6v7w8")

    rows_after_down = await _fetch_series_columns(isolated_postgres_url_series)
    assert rows_after_down == [], (
        f"series + series_status should be dropped after downgrade, got {rows_after_down}"
    )
    check_after_down = await _fetch_series_status_check_constraint(isolated_postgres_url_series)
    assert check_after_down is None, (
        f"ck_backtests_series_status should be dropped after downgrade, got {check_after_down}"
    )

    # Re-upgrade must re-land both columns and the constraint.
    _run_alembic_upgrade(isolated_postgres_url_series)
    _assert_series_columns_present(await _fetch_series_columns(isolated_postgres_url_series))
    check_after_reup = await _fetch_series_status_check_constraint(isolated_postgres_url_series)
    assert check_after_reup is not None, "ck_backtests_series_status missing after re-upgrade"
