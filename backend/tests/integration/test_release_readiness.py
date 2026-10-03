"""Release readiness SQL/HTTP contract against a disposable PostgreSQL database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api.live_deps import get_command_bus
from msai.core.database import get_db
from msai.main import app
from msai.models import Base, LiveNodeProcess
from msai.services.live_command_bus import LiveCommandBus
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from msai.models.live_deployment import LiveDeployment


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def engine(isolated_postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    async def _db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    # Status also reads Redis. Readiness itself must depend only on the SQL snapshot.
    bus = MagicMock(spec=LiveCommandBus)
    bus._redis = MagicMock()  # noqa: SLF001
    bus._redis.exists = AsyncMock(return_value=0)  # noqa: SLF001
    bus.read_router_heartbeat_age_s = AsyncMock(return_value=None)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_command_bus] = lambda: bus
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_command_bus, None)


def _process(
    deployment: LiveDeployment,
    *,
    status: str = "failed",
    started_at: datetime | None = None,
    stop_requested_at: datetime | None = None,
    failure_kind: str | None = None,
    auto_restart_paused: bool = False,
) -> LiveNodeProcess:
    now = datetime.now(UTC)
    return LiveNodeProcess(
        id=uuid4(),
        deployment_id=deployment.id,
        host="release-test",
        started_at=started_at or now,
        last_heartbeat_at=now,
        status=status,
        gateway_session_key=deployment.ib_login_key,
        stop_requested_at=stop_requested_at,
        failure_kind=failure_kind,
        auto_restart_paused=auto_restart_paused,
    )


async def _readiness(client: httpx.AsyncClient, **counts: int) -> dict:
    response = await client.get("/api/v1/live/release-readiness")
    assert response.status_code == 200, response.text
    expected = {"blocking_deployments": 0, "blocking_processes": 0, "restart_blockers": 0}
    expected.update(counts)
    assert response.json() == {
        "contract_version": 1,
        "scope": "fleet",
        "complete": True,
        "ready": not any(expected.values()),
        **expected,
    }
    return response.json()


async def test_empty_fleet_is_ready_in_one_sql_statement(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        await _readiness(client)
        assert len(statements) == 1, statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def test_active_status_includes_stopping_behind_newer_terminal_rows(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        ids = set()
        for state in ("starting", "building", "ready", "running", "stopping"):
            dep = await make_live_deployment(session, status=state)
            dep.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
            ids.add(str(dep.id))
        for _ in range(55):
            await make_live_deployment(session, status="stopped")
        await session.commit()

    response = await client.get("/api/v1/live/status?active_only=true")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["deployments"]} == ids
    await _readiness(client, blocking_deployments=5)


async def test_every_nonterminal_status_blocks_including_terminal_parent_processes(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        for state in ("starting", "building", "ready", "running", "stopping", "unknown"):
            await make_live_deployment(session, status=state)
            for parent_state in ("stopped", "failed"):
                parent = await make_live_deployment(session, status=parent_state)
                session.add(_process(parent, status=state))
        await session.commit()
    await _readiness(client, blocking_deployments=6, blocking_processes=12)
    # An account selector must never hide the rest of the fleet.
    response = await client.get("/api/v1/live/release-readiness?account_id=NO-SUCH-ACCOUNT")
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "fleet"
    assert response.json()["blocking_deployments"] == 6
    assert response.json()["blocking_processes"] == 12


async def test_readiness_counts_are_not_capped(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        for _ in range(55):
            await make_live_deployment(session, status="stopping")
        parent = await make_live_deployment(session, status="stopped")
        session.add_all([_process(parent, status="unknown") for _ in range(1005)])
        await session.commit()
    await _readiness(client, blocking_deployments=55, blocking_processes=1005)


@pytest.mark.parametrize("table", ["live_deployments", "live_node_processes"])
async def test_null_status_fails_closed(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], table: str
) -> None:
    async with session_factory() as session:
        parent = await make_live_deployment(session, status="stopped")
        session.add(_process(parent, status="stopped"))
        await session.flush()
        # Robustness fixture only: production columns are NOT NULL. The disposable
        # database is recreated for every test; no migration or shared DB changes.
        await session.execute(text(f"ALTER TABLE {table} ALTER COLUMN status DROP NOT NULL"))
        await session.execute(text(f"UPDATE {table} SET status = NULL"))
        await session.commit()
    await _readiness(
        client,
        blocking_deployments=int(table == "live_deployments"),
        blocking_processes=int(table == "live_node_processes"),
    )


@pytest.mark.parametrize(
    ("stop_requested", "failure_kind", "started_offset", "last_start_known", "expected"),
    [
        (False, "node_crashed", 0, True, 1),
        (False, "spawn_failed_permanent", 0, True, 1),
        (True, "node_crashed", 0, True, 0),
        (True, "spawn_failed_transient", 0, True, 1),
        (True, "spawn_failed_transient", 1, True, 1),
        (True, "spawn_failed_transient", -1, True, 0),
        (True, "spawn_failed_transient", -1, False, 1),
    ],
)
async def test_failed_latest_process_restart_authority(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    stop_requested: bool,
    failure_kind: str,
    started_offset: int,
    last_start_known: bool,
    expected: int,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        parent = await make_live_deployment(session, status="failed")
        parent.last_started_at = now if last_start_known else None
        session.add(
            _process(
                parent,
                started_at=now + timedelta(seconds=started_offset),
                stop_requested_at=now if stop_requested else None,
                failure_kind=failure_kind,
                auto_restart_paused=True,  # Pausing/retry policy is not durable stop intent.
            )
        )
        await session.commit()
    await _readiness(client, restart_blockers=expected)


async def test_tied_latest_failures_count_parent_once_and_never_hide_restart(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        parent = await make_live_deployment(session, status="failed")
        session.add_all(
            [
                _process(parent, started_at=now),
                _process(parent, started_at=now),
                _process(parent, status="stopped", started_at=now),
            ]
        )
        await session.commit()
    await _readiness(client, restart_blockers=1)


async def test_terminal_fleet_with_no_current_restart_candidate_is_ready(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await make_live_deployment(session, status="failed")  # No process to restart.
        parent = await make_live_deployment(session, status="failed")
        session.add(_process(parent, started_at=now - timedelta(days=1)))
        session.add(_process(parent, status="stopped", started_at=now))
        stopped = await make_live_deployment(session, status="stopped")
        session.add(_process(stopped))  # Ordinary restart requires a failed parent.
        await session.commit()
    await _readiness(client)
    await _readiness(client)  # A fresh read must retain the persisted result.
