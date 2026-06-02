"""Integration test for PR 2 Task 1 — restart-authority columns on
``live_node_processes``.

PR 2 of the multi-account broker fleet introduces a bounded auto-restart
policy for per-account supervisor processes. The policy's state (whether
auto-restart is paused, why, how many consecutive respawns have failed, and
when the last restart happened) is persisted on the ``live_node_processes``
row so it survives a container recreate. This task (T1) only adds the schema;
the policy logic lands in a later task.

Migration ``d4e5f6a7b8c9`` adds four ADDITIVE columns, chained to the current
head ``c3d4e5f6a7b8``:

- ``auto_restart_paused``           — BOOLEAN NOT NULL DEFAULT false
- ``auto_restart_pause_reason``     — TEXT NULL
- ``consecutive_respawn_failures``  — INTEGER NOT NULL DEFAULT 0
- ``last_restart_at``               — TIMESTAMPTZ NULL

Additive-only discipline (``.claude/rules/database.md``): the deploy pipeline
rolls back image SHAs but NOT schema, so old code must tolerate the new
schema. The two NOT-NULL columns carry server-side defaults so old code's
INSERTs (which never name these columns) still succeed. ``downgrade()`` drops
exactly the four columns and nothing else — the pre-existing
``gateway_session_key`` column and every other column are untouched.

We provision a DEDICATED Postgres testcontainer for this module (matching the
isolation rationale in ``test_alembic_migrations.py``) and invoke Alembic as a
subprocess so its ``asyncio.run(...)`` inside ``alembic/env.py`` doesn't clash
with pytest-asyncio's event loop — exactly how migrations run in production
(``uv run alembic upgrade head``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._alembic_subprocess import run_alembic as _run_alembic

if TYPE_CHECKING:
    from collections.abc import Iterator

# Revision under test and its parent (the current head before PR 2).
REV_PR2 = "d4e5f6a7b8c9"
PARENT_REV = "c3d4e5f6a7b8"

# The exact four columns this migration adds.
_NEW_COLUMNS = {
    "auto_restart_paused",
    "auto_restart_pause_reason",
    "consecutive_respawn_failures",
    "last_restart_at",
}

# T6 council #3 reaper-fix migration (chained after T1) and the two nullable
# reaper-authority columns it adds.
REV_T6 = "e5f6a7b8c9d0"
_T6_NEW_COLUMNS = {
    "stop_requested_at",
    "restart_dispatched_at",
}


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL`` — we never want this test to
    mutate a configured dev DB. See ``test_alembic_migrations.py`` for the
    full rationale.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


async def _fetch_new_columns(database_url: str) -> dict[str, sa.Row[object]]:
    """Query ``information_schema`` for the four new columns on
    ``live_node_processes``. Returns a name -> row map of
    (column_name, data_type, is_nullable, column_default).

    Asserting against the real on-disk shape (not SQLAlchemy reflection)
    is the strongest check that NOT-NULL + server defaults actually landed.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'live_node_processes' "
                    "AND column_name IN ("
                    "  'auto_restart_paused', 'auto_restart_pause_reason',"
                    "  'consecutive_respawn_failures', 'last_restart_at'"
                    ") ORDER BY column_name"
                )
            )
            return {row[0]: row for row in result}
    finally:
        await engine.dispose()


def _assert_columns_present(cols: dict[str, sa.Row[object]]) -> None:
    """Assert all four columns landed with correct type / nullability / default."""
    assert set(cols) == _NEW_COLUMNS, f"unexpected column set: {sorted(cols)}"

    paused = cols["auto_restart_paused"]
    assert paused[1] == "boolean", f"auto_restart_paused should be boolean, got {paused[1]}"
    assert paused[2] == "NO", f"auto_restart_paused must be NOT NULL, got is_nullable={paused[2]}"
    assert "false" in (paused[3] or "").lower(), (
        f"auto_restart_paused should default to false, got {paused[3]}"
    )

    reason = cols["auto_restart_pause_reason"]
    assert reason[1] == "text", f"auto_restart_pause_reason should be text, got {reason[1]}"
    assert reason[2] == "YES", (
        f"auto_restart_pause_reason must be nullable, got is_nullable={reason[2]}"
    )
    assert reason[3] is None, f"auto_restart_pause_reason should have no default, got {reason[3]}"

    failures = cols["consecutive_respawn_failures"]
    assert failures[1] == "integer", (
        f"consecutive_respawn_failures should be integer, got {failures[1]}"
    )
    assert failures[2] == "NO", (
        f"consecutive_respawn_failures must be NOT NULL, got is_nullable={failures[2]}"
    )
    assert (failures[3] or "").startswith("0"), (
        f"consecutive_respawn_failures should default to 0, got {failures[3]}"
    )

    last_restart = cols["last_restart_at"]
    assert last_restart[1] == "timestamp with time zone", (
        f"last_restart_at should be timestamptz, got {last_restart[1]}"
    )
    assert last_restart[2] == "YES", (
        f"last_restart_at must be nullable, got is_nullable={last_restart[2]}"
    )
    assert last_restart[3] is None, f"last_restart_at should have no default, got {last_restart[3]}"


@pytest.mark.asyncio
async def test_pr2_migration_adds_restart_authority_columns_additively(
    isolated_postgres_url: str,
) -> None:
    """``alembic upgrade head`` lands the four restart-authority columns with
    the correct types, nullability, and server defaults; the downgrade removes
    exactly those four and leaves every pre-existing column (notably
    ``gateway_session_key``) untouched; a re-upgrade re-lands them.
    """
    # Upgrade to head (includes the PR 2 migration) and assert all four columns.
    _run_alembic(isolated_postgres_url, "upgrade", "head")
    _assert_columns_present(await _fetch_new_columns(isolated_postgres_url))

    # Snapshot the full column set of live_node_processes so we can prove the
    # downgrade drops EXACTLY the four new columns and nothing else.
    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            cols_at_head = await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("live_node_processes")}
            )
    finally:
        await engine.dispose()

    # The columns that existed BELOW T1 (i.e. before both T1 and the later T6
    # migration that chains above it). Downgrading from head to T1's PARENT drops
    # T6's two columns AND T1's four; everything else must survive. (PR #1 added
    # gateway_session_key; neither T1 nor T6 may touch it.)
    pre_existing = cols_at_head - _NEW_COLUMNS - _T6_NEW_COLUMNS
    assert "gateway_session_key" in pre_existing, (
        "gateway_session_key should be a pre-existing column"
    )

    # Downgrade to T1's parent — walks T6 then T1 down, dropping all six columns.
    _run_alembic(isolated_postgres_url, "downgrade", PARENT_REV)

    cols_after_down = await _fetch_new_columns(isolated_postgres_url)
    assert cols_after_down == {}, (
        f"all four columns should be dropped after downgrade, got {sorted(cols_after_down)}"
    )

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            cols_after_down_full = await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("live_node_processes")}
            )
    finally:
        await engine.dispose()
    # Every pre-existing column survives the downgrade — additive-only proof.
    assert pre_existing == cols_after_down_full, (
        f"downgrade altered pre-existing columns; expected {sorted(pre_existing)}, "
        f"got {sorted(cols_after_down_full)}"
    )

    # Re-upgrade must re-land all four columns.
    _run_alembic(isolated_postgres_url, "upgrade", "head")
    _assert_columns_present(await _fetch_new_columns(isolated_postgres_url))


@pytest.mark.asyncio
async def test_pr2_model_round_trips_restart_authority_fields(
    isolated_postgres_url: str,
) -> None:
    """The ``LiveNodeProcess`` model maps the four new fields: a row inserted
    with all four set reads back with the same values, and the server defaults
    apply when they are omitted.

    This binds the ORM model to the migrated schema — if a later "helpful
    refactor" drops a ``mapped_column`` the round-trip breaks here.
    """
    from datetime import UTC, datetime

    from msai.models.live_deployment import LiveDeployment
    from msai.models.live_node_process import LiveNodeProcess
    from msai.models.live_portfolio import LivePortfolio
    from msai.models.live_portfolio_revision import LivePortfolioRevision
    from msai.models.user import User
    from msai.services.live.deployment_identity import (
        derive_message_bus_stream,
        derive_strategy_id_full,
        derive_trader_id,
        generate_deployment_slug,
    )

    _run_alembic(isolated_postgres_url, "upgrade", "head")

    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_async_engine(isolated_postgres_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Arrange — a deployment to satisfy the FK on live_node_processes.
        async with factory() as session:
            user = User(
                id=uuid4(),
                entra_id=f"pr2-{uuid4().hex}",
                email=f"pr2-{uuid4().hex}@test.com",
                role="trader",
            )
            session.add(user)
            await session.flush()

            portfolio = LivePortfolio(
                id=uuid4(),
                name=f"pr2-rt-{uuid4().hex[:8]}",
                description="restart-authority round-trip fixture",
                created_by=user.id,
            )
            session.add(portfolio)
            await session.flush()

            revision = LivePortfolioRevision(
                id=uuid4(),
                portfolio_id=portfolio.id,
                revision_number=1,
                composition_hash=uuid4().hex + uuid4().hex,
                is_frozen=True,
            )
            session.add(revision)
            await session.flush()

            slug = generate_deployment_slug()
            deployment = LiveDeployment(
                id=uuid4(),
                strategy_id=None,
                status="running",
                paper_trading=True,
                started_by=user.id,
                deployment_slug=slug,
                identity_signature=uuid4().hex + uuid4().hex,
                trader_id=derive_trader_id(slug),
                strategy_id_full=derive_strategy_id_full("EMACrossStrategy", slug),
                account_id="DU1234567",
                ib_login_key="msai-paper-primary",
                portfolio_revision_id=revision.id,
                message_bus_stream=derive_message_bus_stream(slug),
            )
            session.add(deployment)
            await session.flush()

            # Act — insert a process row with explicit restart-authority values.
            now = datetime.now(UTC)
            paused_proc = LiveNodeProcess(
                id=uuid4(),
                deployment_id=deployment.id,
                host="test-host",
                started_at=now,
                last_heartbeat_at=now,
                status="running",
                gateway_session_key="session-key-paused",
                auto_restart_paused=True,
                auto_restart_pause_reason="max respawn ceiling reached",
                consecutive_respawn_failures=3,
                last_restart_at=now,
            )
            session.add(paused_proc)

            # A second row that OMITS the defaulted columns — exercises the
            # server-side defaults (false / 0) the way old code would.
            # Use a TERMINAL status ('stopped') so it sits OUTSIDE the active
            # set guarded by the uq_live_node_processes_active_deployment
            # partial unique index — a deployment may have only one ACTIVE
            # process row at a time, but any number of terminal ones.
            default_proc = LiveNodeProcess(
                id=uuid4(),
                deployment_id=deployment.id,
                host="test-host-2",
                started_at=now,
                last_heartbeat_at=now,
                status="stopped",
                gateway_session_key="session-key-default",
            )
            session.add(default_proc)
            await session.commit()

            paused_id = paused_proc.id
            default_id = default_proc.id

        # Assert — read both rows back in a fresh session.
        async with factory() as session:
            reloaded_paused = await session.get(LiveNodeProcess, paused_id)
            assert reloaded_paused is not None
            assert reloaded_paused.auto_restart_paused is True
            assert reloaded_paused.auto_restart_pause_reason == "max respawn ceiling reached"
            assert reloaded_paused.consecutive_respawn_failures == 3
            assert reloaded_paused.last_restart_at is not None
            # gateway_session_key (pre-existing) is untouched by the new fields.
            assert reloaded_paused.gateway_session_key == "session-key-paused"

            reloaded_default = await session.get(LiveNodeProcess, default_id)
            assert reloaded_default is not None
            # Server defaults applied for the omitted NOT-NULL columns.
            assert reloaded_default.auto_restart_paused is False
            assert reloaded_default.auto_restart_pause_reason is None
            assert reloaded_default.consecutive_respawn_failures == 0
            assert reloaded_default.last_restart_at is None
            # T6 council #3 reaper-authority columns default to NULL.
            assert reloaded_default.stop_requested_at is None
            assert reloaded_default.restart_dispatched_at is None
    finally:
        await engine.dispose()


async def _fetch_t6_columns(database_url: str) -> dict[str, sa.Row[object]]:
    """Query ``information_schema`` for the two T6 reaper-authority columns."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'live_node_processes' "
                    "AND column_name IN ('stop_requested_at', 'restart_dispatched_at') "
                    "ORDER BY column_name"
                )
            )
            return {row[0]: row for row in result}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_t6_migration_adds_reaper_authority_columns_additively(
    isolated_postgres_url: str,
) -> None:
    """``alembic upgrade head`` lands ``stop_requested_at`` + ``restart_dispatched_at``
    as nullable TIMESTAMPTZ columns chained after T1; the downgrade removes
    exactly those two and leaves every pre-existing column (notably the four T1
    columns + ``gateway_session_key``) untouched; a re-upgrade re-lands them.
    """
    _run_alembic(isolated_postgres_url, "upgrade", "head")
    cols = await _fetch_t6_columns(isolated_postgres_url)
    assert set(cols) == _T6_NEW_COLUMNS, f"unexpected T6 column set: {sorted(cols)}"
    for name in _T6_NEW_COLUMNS:
        row = cols[name]
        assert row[1] == "timestamp with time zone", f"{name} should be timestamptz, got {row[1]}"
        assert row[2] == "YES", f"{name} must be nullable, got is_nullable={row[2]}"
        assert row[3] is None, f"{name} should have no default, got {row[3]}"

    # Snapshot the full column set so the downgrade is proven additive-only.
    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            cols_at_head = await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("live_node_processes")}
            )
    finally:
        await engine.dispose()
    pre_existing = cols_at_head - _T6_NEW_COLUMNS
    # The four T1 columns and gateway_session_key survive the T6 downgrade.
    assert pre_existing >= _NEW_COLUMNS
    assert "gateway_session_key" in pre_existing

    # Downgrade exactly one step to T1 — drops only the two T6 columns.
    _run_alembic(isolated_postgres_url, "downgrade", REV_PR2)
    assert await _fetch_t6_columns(isolated_postgres_url) == {}, (
        "both T6 columns should be dropped after downgrade -1"
    )
    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            cols_after_down = await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("live_node_processes")}
            )
    finally:
        await engine.dispose()
    assert pre_existing == cols_after_down, "T6 downgrade altered pre-existing columns"

    # Re-upgrade re-lands both columns.
    _run_alembic(isolated_postgres_url, "upgrade", "head")
    assert set(await _fetch_t6_columns(isolated_postgres_url)) == _T6_NEW_COLUMNS
