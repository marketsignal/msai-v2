"""Integration tests for ``GET /api/v1/live/status/{deployment_id}`` (Task 1.13).

Uses a dedicated Postgres testcontainer + a real ASGI client so the
route exercises the full SQL join (LiveDeployment → LiveNodeProcess).
404 + 200-with-process + 200-without-process + auth-required all
covered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.failure_kind import FailureKind
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    """Test client with DB + auth dependencies overridden."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_get_current_user() -> dict[str, Any]:
        return {"sub": "test-sub", "email": "test@example.com", "preferred_username": "test"}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_deployment_with_process(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    process_status: str = "running",
    with_process: bool = True,
    failure_kind: str | None = None,
    error_message: str | None = None,
    exit_code: int | None = None,
) -> tuple[LiveDeployment, LiveNodeProcess | None]:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"sub-{uuid4().hex}",
            email=f"t-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name=f"strat-{uuid4().hex[:8]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        dep = await make_live_deployment(
            session,
            user=user,
            strategy=strategy,
            status="running" if with_process else "created",
        )
        if with_process:
            dep.last_started_at = datetime.now(UTC)
        await session.flush()

        proc: LiveNodeProcess | None = None
        if with_process:
            proc = LiveNodeProcess(
                id=uuid4(),
                deployment_id=dep.id,
                gateway_session_key="msai-paper-primary:localhost:4002",
                pid=12345,
                host="test-host-1",
                started_at=datetime.now(UTC),
                last_heartbeat_at=datetime.now(UTC),
                status=process_status,
                exit_code=exit_code,
                error_message=error_message,
                failure_kind=failure_kind,
            )
            session.add(proc)

        await session.commit()
        return dep, proc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_by_id_returns_404_for_unknown_deployment(
    client: httpx.AsyncClient,
) -> None:
    """An unknown ``deployment_id`` → 404 with a descriptive detail."""
    unknown_id = uuid4()
    response = await client.get(f"/api/v1/live/status/{unknown_id}")
    assert response.status_code == 404
    assert str(unknown_id) in response.json()["detail"]


@pytest.mark.asyncio
async def test_status_by_id_returns_running_deployment_with_process_fields(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A known deployment with a live process row → 200 with both the
    logical fields (slug, strategy, instruments) and the per-run
    process fields (pid, host, heartbeat)."""
    dep, proc = await _seed_deployment_with_process(session_factory, process_status="running")
    assert proc is not None

    response = await client.get(f"/api/v1/live/status/{dep.id}")
    assert response.status_code == 200

    body = response.json()
    # Logical fields
    assert body["id"] == str(dep.id)
    assert body["strategy_id"] == str(dep.strategy_id)
    assert body["deployment_slug"] == dep.deployment_slug
    assert body["status"] == "running"
    assert body["paper_trading"] is True
    # ``instruments`` column was dropped from live_deployments in PR #29
    # Task 11; the endpoint now returns an empty list as a backward-
    # compatible shim. Real instrument lists live on
    # ``live_portfolio_revision_strategies`` and are surfaced via the
    # portfolio-scoped endpoints.
    assert body["instruments"] == []
    # Per-run process fields
    assert body["process_id"] == str(proc.id)
    assert body["pid"] == 12345
    assert body["host"] == "test-host-1"
    assert body["process_status"] == "running"
    assert body["last_heartbeat_at"] is not None
    assert body["exit_code"] is None
    assert body["failure_kind"] is None


@pytest.mark.asyncio
async def test_status_by_id_returns_200_with_null_process_when_never_ran(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment that has never spawned a process → 200 with
    process fields as ``None``. The logical row alone is enough to
    answer the query."""
    dep, _ = await _seed_deployment_with_process(session_factory, with_process=False)

    response = await client.get(f"/api/v1/live/status/{dep.id}")
    assert response.status_code == 200

    body = response.json()
    assert body["id"] == str(dep.id)
    assert body["status"] == "created"
    assert body["process_id"] is None
    assert body["pid"] is None
    assert body["host"] is None
    assert body["process_status"] is None
    assert body["last_heartbeat_at"] is None
    assert body["exit_code"] is None
    assert body["failure_kind"] is None


@pytest.mark.asyncio
async def test_status_by_id_returns_terminal_failed_with_failure_kind(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment whose latest process row is ``failed`` surfaces
    the structured ``failure_kind`` + ``error_message`` so the UI
    can show the diagnosis verbatim."""
    dep, proc = await _seed_deployment_with_process(
        session_factory,
        process_status="failed",
        failure_kind=FailureKind.RECONCILIATION_FAILED.value,
        error_message="trader.is_running=False; data_engine.check_connected()=False",
        exit_code=2,
    )
    assert proc is not None

    response = await client.get(f"/api/v1/live/status/{dep.id}")
    assert response.status_code == 200

    body = response.json()
    assert body["process_status"] == "failed"
    assert body["failure_kind"] == "reconciliation_failed"
    assert body["exit_code"] == 2
    assert "trader.is_running=False" in body["error_message"]


@pytest.mark.asyncio
async def test_status_by_id_returns_most_recent_process_row(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When multiple process rows exist (e.g. after a restart), the
    endpoint returns the MOST RECENT one — ordered by ``started_at``
    descending."""
    import asyncio as _asyncio

    dep, old_proc = await _seed_deployment_with_process(session_factory, process_status="stopped")
    assert old_proc is not None
    # Brief sleep so the two started_at timestamps are strictly distinct.
    await _asyncio.sleep(0.01)

    async with session_factory() as session:
        new_proc = LiveNodeProcess(
            id=uuid4(),
            deployment_id=dep.id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=99999,
            host="test-host-2",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="running",
        )
        session.add(new_proc)
        await session.commit()
        new_proc_id = new_proc.id

    response = await client.get(f"/api/v1/live/status/{dep.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["process_id"] == str(new_proc_id)
    assert body["pid"] == 99999
    assert body["host"] == "test-host-2"
    assert body["process_status"] == "running"


@pytest.mark.asyncio
async def test_status_by_id_invalid_uuid_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """FastAPI converts a non-UUID path parameter into a 422 before
    reaching the handler — documented via a regression test so any
    future route-decorator change that loosens the type won't
    silently regress."""
    response = await client.get("/api/v1/live/status/not-a-uuid")
    assert response.status_code == 422
