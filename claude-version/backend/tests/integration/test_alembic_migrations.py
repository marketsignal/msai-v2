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
def isolated_postgres_url_b() -> Iterator[str]:
    """Second dedicated container for the 1.1b tests so they're isolated
    from the 1.1 test above. Each test gets a guaranteed-fresh DB."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_alembic_upgrade_head_creates_live_deployments_identity_columns(
    isolated_postgres_url_b: str,
) -> None:
    """Phase 1 task 1.1b: ``alembic upgrade head`` creates the v9 stable
    identity columns on ``live_deployments`` and drops the old
    ``started_at`` / ``stopped_at`` columns.
    """
    _run_alembic_upgrade(isolated_postgres_url_b)

    engine = create_async_engine(isolated_postgres_url_b)
    try:
        async with engine.connect() as conn:

            def _inspect(sync_conn: object) -> dict[str, list[str]]:
                insp = inspect(sync_conn)
                columns = {col["name"]: col for col in insp.get_columns("live_deployments")}
                indexes = {idx["name"]: idx for idx in insp.get_indexes("live_deployments")}
                return {"columns": list(columns), "indexes": list(indexes)}

            shape = await conn.run_sync(_inspect)
    finally:
        await engine.dispose()

    # New v9 columns must all be present
    expected_new_columns = {
        "deployment_slug",
        "identity_signature",
        "trader_id",
        "strategy_id_full",
        "account_id",
        "message_bus_stream",
        "config_hash",
        "instruments_signature",
        "last_started_at",
        "last_stopped_at",
        "startup_hard_timeout_s",
    }
    actual = set(shape["columns"])
    missing = expected_new_columns - actual
    assert not missing, f"missing identity columns on live_deployments: {missing}"

    # The OLD started_at / stopped_at columns must be GONE
    assert "started_at" not in actual, (
        "old started_at column should have been dropped by 1.1b migration"
    )
    assert "stopped_at" not in actual, (
        "old stopped_at column should have been dropped by 1.1b migration"
    )

    # Unique indexes for the identity contract
    expected_indexes = {
        "ix_live_deployments_deployment_slug",
        "ix_live_deployments_identity_signature",
    }
    missing_idx = expected_indexes - set(shape["indexes"])
    assert not missing_idx, f"missing identity indexes: {missing_idx}"


@pytest.fixture(scope="module")
def isolated_postgres_url_backfill() -> Iterator[str]:
    """Third dedicated container for the backfill test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_1_1b_backfills_pre_existing_rows(
    isolated_postgres_url_backfill: str,
) -> None:
    """Plan acceptance criterion: ``alembic upgrade head`` succeeds on a
    database with PRE-EXISTING rows AND those rows get a backfilled
    ``identity_signature``, ``deployment_slug``, and all the derived
    columns.

    Strategy:
    1. Run alembic upgrade to b1c2d3e4f5a6 (the revision BEFORE 1.1b).
       This is the schema state with the old started_at/stopped_at.
    2. Insert a synthetic user/strategy/live_deployment using raw SQL.
    3. Run alembic upgrade head (which runs c1d2e3f4a5b6 / task 1.1b).
    4. Inspect the row and verify every backfilled column is populated
       and the deployment_slug + identity_signature are unique values
       (not NULL or placeholder strings).
    """
    # Step 1: upgrade to the previous head (pre-1.1b)
    _run_alembic_upgrade(isolated_postgres_url_backfill, target="b1c2d3e4f5a6")

    # Step 2: insert a pre-existing row using the OLD schema
    engine = create_async_engine(isolated_postgres_url_backfill)
    from uuid import uuid4

    user_id = uuid4()
    strategy_id = uuid4()
    deployment_id = uuid4()
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
                    "entra": f"backfill-{user_id.hex}",
                    "email": f"backfill-{user_id.hex}@example.com",
                },
            )
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO strategies (
                        id, name, file_path, strategy_class, created_by,
                        created_at, updated_at
                    )
                    VALUES (
                        :id, :name, :fp, :cls, :uid, NOW(), NOW()
                    )
                    """
                ),
                {
                    "id": strategy_id,
                    "name": "backfill-test",
                    "fp": "strategies/backfill.py",
                    "cls": "BackfillStrategy",
                    "uid": user_id,
                },
            )
            # NOTE: live_deployments at b1c2d3e4f5a6 still has started_at / stopped_at
            # and DOES NOT have any of the v9 identity columns yet.
            # Cast the JSONB and ARRAY explicitly so asyncpg knows the types.
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
                    "id": deployment_id,
                    "sid": strategy_id,
                    "hash": "deadbeef" * 8,
                    "cfg": '{"fast": 10, "slow": 20}',
                    "instr": ["AAPL.NASDAQ", "MSFT.NASDAQ"],
                    "uid": user_id,
                },
            )
    finally:
        await engine.dispose()

    # Step 3: upgrade to head (runs c1d2e3f4a5b6 / task 1.1b backfill)
    _run_alembic_upgrade(isolated_postgres_url_backfill)

    # Step 4: inspect the row
    engine = create_async_engine(isolated_postgres_url_backfill)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    """
                    SELECT
                        deployment_slug,
                        identity_signature,
                        trader_id,
                        strategy_id_full,
                        account_id,
                        message_bus_stream,
                        config_hash,
                        instruments_signature,
                        last_started_at,
                        last_stopped_at,
                        startup_hard_timeout_s
                    FROM live_deployments
                    WHERE id = :id
                    """
                ),
                {"id": deployment_id},
            )
            row = result.one()
    finally:
        await engine.dispose()

    # All 8 NOT NULL columns must be populated
    assert row.deployment_slug is not None
    assert len(row.deployment_slug) == 16
    assert row.identity_signature is not None
    assert len(row.identity_signature) == 64
    assert row.trader_id == f"MSAI-{row.deployment_slug}"
    assert row.strategy_id_full == f"BackfillStrategy-{row.deployment_slug}"
    assert row.account_id == "DU0000000"  # placeholder for backfilled rows
    assert row.message_bus_stream == f"trader-MSAI-{row.deployment_slug}-stream"
    assert row.config_hash is not None
    assert len(row.config_hash) == 64
    # instruments are sorted in the signature
    assert row.instruments_signature == "AAPL.NASDAQ,MSFT.NASDAQ"
    # last_started_at copied from old started_at
    assert row.last_started_at is not None
    # last_stopped_at copied from old stopped_at (NULL in this case)
    assert row.last_stopped_at is None
    # startup_hard_timeout_s is intentionally nullable
    assert row.startup_hard_timeout_s is None


@pytest.fixture(scope="module")
def isolated_postgres_url_collision() -> Iterator[str]:
    """Fourth dedicated container for the collision-detection test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="module")
def isolated_postgres_url_legacy_hash() -> Iterator[str]:
    """Fifth dedicated container for the legacy-hash-normalization test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_1_1b_preserves_legacy_strategy_code_hash(
    isolated_postgres_url_legacy_hash: str,
) -> None:
    """Codex Task 1.1b iteration 4, P1 fix: the backfill MUST NOT
    rewrite a legacy ``strategy_code_hash`` (e.g. the ``'live'``
    placeholder) with a fresh sha256 of the current file. The file
    may have been edited after the row last ran; hashing today's bytes
    would assign the row the wrong code version and let the next
    /start warm-restart persisted state created under older code onto
    a mismatched file.

    The correct behavior is: keep the legacy placeholder as-is, hash
    it into the identity_signature, and let the first post-migration
    restart cold-start cleanly (because ``/start`` computes the real
    sha256 which will not match).
    """
    from uuid import uuid4

    _run_alembic_upgrade(isolated_postgres_url_legacy_hash, target="b1c2d3e4f5a6")

    engine = create_async_engine(isolated_postgres_url_legacy_hash)
    user_id = uuid4()
    strategy_id = uuid4()
    deployment_id = uuid4()
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
                    "entra": f"lh-{user_id.hex}",
                    "email": f"lh-{user_id.hex}@example.com",
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
                    "name": "legacy-hash",
                    "fp": "strategies/legacy/ema.py",
                    "cls": "LegacyStrategy",
                    "uid": user_id,
                },
            )
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO live_deployments (
                        id, strategy_id, strategy_code_hash, config, instruments,
                        status, paper_trading, started_by, created_at, started_at
                    )
                    VALUES (
                        :id, :sid, 'live', CAST(:cfg AS JSONB),
                        CAST(:instr AS VARCHAR[]),
                        'stopped', true, :uid, NOW(), NOW()
                    )
                    """
                ),
                {
                    "id": deployment_id,
                    "sid": strategy_id,
                    "cfg": "{}",
                    "instr": ["AAPL.NASDAQ"],
                    "uid": user_id,
                },
            )
    finally:
        await engine.dispose()

    _run_alembic_upgrade(isolated_postgres_url_legacy_hash)

    engine = create_async_engine(isolated_postgres_url_legacy_hash)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT strategy_code_hash, identity_signature "
                    "FROM live_deployments WHERE id = :id"
                ),
                {"id": deployment_id},
            )
            row = result.one()
    finally:
        await engine.dispose()

    # The backfill MUST leave the placeholder untouched — the historical
    # hash is unrecoverable, and fabricating one risks warm-restarting
    # onto incompatible persisted state.
    assert row.strategy_code_hash == "live"
    assert row.identity_signature is not None
    assert len(row.identity_signature) == 64


@pytest.fixture(scope="module")
def isolated_postgres_url_default_cfg() -> Iterator[str]:
    """Sixth dedicated container for the default-config normalization test."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_1_1b_backfill_merges_strategy_default_config(
    isolated_postgres_url_default_cfg: str,
) -> None:
    """Codex Task 1.1b iteration 4, P2 fix: a legacy row whose stored
    ``config`` is ``{}`` and whose strategy's ``default_config`` is
    ``{"fast": 10}`` must backfill to the SAME ``config_hash`` that
    ``/start`` would compute for an identical request (which applies
    the same merge via ``normalize_request_config``).

    Verified by hashing two rows: row A has ``config={}``, row B has
    ``config={"fast": 10, "slow": 30}`` (the exact default). They MUST
    end up with the same ``config_hash`` after backfill, because they
    are semantically identical once defaults are merged.
    """
    from uuid import uuid4

    _run_alembic_upgrade(isolated_postgres_url_default_cfg, target="b1c2d3e4f5a6")

    engine = create_async_engine(isolated_postgres_url_default_cfg)
    user_id = uuid4()
    strategy_a_id = uuid4()
    strategy_b_id = uuid4()
    row_a_id = uuid4()
    row_b_id = uuid4()
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
                    "entra": f"cfg-{user_id.hex}",
                    "email": f"cfg-{user_id.hex}@example.com",
                },
            )
            # Two DIFFERENT strategies (so the rows don't collide on
            # strategy_id and get flagged by _detect_identity_collisions)
            # but with the same default_config.
            for sid, suffix in ((strategy_a_id, "A"), (strategy_b_id, "B")):
                await conn.execute(
                    sa.text(
                        """
                        INSERT INTO strategies (
                            id, name, file_path, strategy_class,
                            default_config, created_by, created_at, updated_at
                        )
                        VALUES (
                            :id, :name, :fp, :cls, CAST(:def AS JSONB),
                            :uid, NOW(), NOW()
                        )
                        """
                    ),
                    {
                        "id": sid,
                        "name": f"cfgtest-{suffix}",
                        "fp": f"strategies/cfg/{suffix}.py",
                        "cls": f"CfgStrategy{suffix}",
                        "def": '{"fast": 10, "slow": 30}',
                        "uid": user_id,
                    },
                )
            # Row A: config={} — fully relies on defaults
            # Row B: config={"fast": 10, "slow": 30} — explicit defaults
            for row_id, sid, cfg_json in (
                (row_a_id, strategy_a_id, "{}"),
                (row_b_id, strategy_b_id, '{"fast": 10, "slow": 30}'),
            ):
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
                        "id": row_id,
                        "sid": sid,
                        "hash": "deadbeef" * 8,
                        "cfg": cfg_json,
                        "instr": ["AAPL.NASDAQ"],
                        "uid": user_id,
                    },
                )
    finally:
        await engine.dispose()

    _run_alembic_upgrade(isolated_postgres_url_default_cfg)

    engine = create_async_engine(isolated_postgres_url_default_cfg)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT id, config_hash FROM live_deployments WHERE id IN (:a, :b)"),
                {"a": row_a_id, "b": row_b_id},
            )
            rows = {r.id: r.config_hash for r in result.all()}
    finally:
        await engine.dispose()

    # Both rows must end up with the same config_hash — the merge with
    # default_config makes the empty-config row semantically identical
    # to the explicit-defaults row.
    assert rows[row_a_id] == rows[row_b_id], (
        f"config_hash should match after default merge; got A={rows[row_a_id]}, B={rows[row_b_id]}"
    )


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
