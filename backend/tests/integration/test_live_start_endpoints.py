"""Integration tests for ``POST /api/v1/live/start`` and ``/stop``
(Phase 1 Task 1.14 — command-bus wiring + idempotency reservation).

Exercises the full new flow through the ASGI client:

1. Idempotency-Key SETNX reservation (Reserved / InFlight /
   CachedOutcome / BodyMismatchReservation branches)
2. Halt-flag short-circuit (non-cacheable 503)
3. Identity-based warm-restart upsert
4. Active-process short-circuit (``already_active``, 200)
5. Publish to ``LiveCommandBus`` (verified via stream read)
6. Poll ``live_node_processes`` for ready/failed with timeout
7. Permanent-failure classification via ``FailureKind.parse_or_unknown``

The supervisor is stubbed by a background "fake supervisor" task that
watches the live_node_processes table and flips rows from ``starting``
to the test-specified terminal state. This gives the endpoint a
deterministic ready/failed signal without running a real supervisor.

SAFETY: dedicated PostgresContainer + RedisContainer per module.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api import live as live_module
from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.idempotency import IdempotencyStore
from msai.services.live_command_bus import (
    LIVE_COMMAND_STREAM,
    LiveCommandBus,
)
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="module")
def isolated_redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


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
async def redis_binary(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=False)
    with contextlib.suppress(Exception):
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def redis_text(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def test_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> User:
    """Pre-seed a single user so /start has a stable started_by value."""
    async with session_factory() as session, session.begin():
        user = User(
            id=uuid4(),
            entra_id="test-sub-stable",
            email="test@example.com",
            role="operator",
        )
        session.add(user)
    return user


@pytest_asyncio.fixture
async def test_strategy(
    session_factory: async_sessionmaker[AsyncSession],
    test_user: User,
    tmp_path_factory: pytest.TempPathFactory,
) -> Strategy:
    """Seed a strategy row + a real source file on disk so the
    strategy_code_hash resolves to a deterministic value."""
    strat_dir = tmp_path_factory.mktemp("strategies")
    strat_file = strat_dir / "smoke.py"
    strat_file.write_text("# smoke strategy source\n")

    async with session_factory() as session, session.begin():
        strategy = Strategy(
            id=uuid4(),
            name="smoke",
            file_path=str(strat_file),
            strategy_class="SmokeStrategy",
            default_config={},
            created_by=test_user.id,
        )
        session.add(strategy)
    return strategy


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    redis_binary: AsyncRedis,
    redis_text: AsyncRedis,
    test_user: User,
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with DB + Redis dependencies overridden to the
    testcontainer fixtures. The current_user dependency is also
    stubbed to return the pre-seeded ``test_user``.
    """

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> dict[str, Any]:
        return {"sub": test_user.entra_id, "email": test_user.email}

    # Register a fake ``live-supervisor`` consumer so
    # ``_supervisor_is_alive`` (drill 2026-04-15 P0-A) sees an
    # active consumer and doesn't short-circuit /start with 503.
    # Real integration with a supervisor consumer loop is out of
    # scope for these endpoint tests — we stub it at the Redis
    # layer the same way the fake supervisor task stubs the DB
    # side.
    shared_bus = LiveCommandBus(redis=redis_text)
    await shared_bus.ensure_group()
    with contextlib.suppress(Exception):
        await redis_text.xgroup_createconsumer(
            LIVE_COMMAND_STREAM, "live-supervisor", "fake-supervisor-1"
        )

    async def _override_command_bus() -> LiveCommandBus:
        return shared_bus

    async def _override_idempotency_store() -> IdempotencyStore:
        return IdempotencyStore(redis=redis_binary)

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user
    app.dependency_overrides[get_command_bus] = _override_command_bus
    app.dependency_overrides[get_idempotency_store] = _override_idempotency_store

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_command_bus, None)
    app.dependency_overrides.pop(get_idempotency_store, None)


# ---------------------------------------------------------------------------
# Fake supervisor helpers
# ---------------------------------------------------------------------------


async def _fake_supervisor_ready(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id_fut: asyncio.Future[UUID],
    *,
    final_status: str = "ready",
    failure_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    """Background task that polls ``live_node_processes`` for a row
    created by ``/start``, then flips it to ``final_status``.

    The endpoint creates the ``live_deployments`` row but NOT the
    ``live_node_processes`` row — the supervisor does that in
    production. In tests, we insert the row ourselves the moment we
    see a matching deployment_id, then flip it to ``final_status``
    after a brief delay so ``_poll_for_terminal`` has a chance to
    observe the transition.
    """
    deployment_id = await deployment_id_fut
    # Wait a moment so the endpoint's poll loop has started.
    await asyncio.sleep(0.05)

    async with session_factory() as session, session.begin():
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment_id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=12345,
            host="fake-supervisor-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status=final_status,
            failure_kind=failure_kind,
            error_message=error_message,
        )
        session.add(row)


async def _fake_supervisor_never_ready(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id_fut: asyncio.Future[UUID],
) -> None:
    """Supervisor that never transitions the row — used for the
    api_poll_timeout test. Inserts a row in status='starting' so
    /start's active-process dedup doesn't fire on the next retry,
    but never flips it."""
    deployment_id = await deployment_id_fut
    await asyncio.sleep(0.05)
    async with session_factory() as session, session.begin():
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment_id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=12345,
            host="fake-supervisor-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="starting",
        )
        session.add(row)


async def _drive_start_with_supervisor(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    test_strategy: Strategy,
    *,
    headers: dict[str, str] | None = None,
    supervisor_fn=_fake_supervisor_ready,
    supervisor_kwargs: dict[str, Any] | None = None,
) -> httpx.Response:
    """Drive a /start call with a concurrent fake supervisor.

    The fake supervisor needs the newly-created ``deployment_id`` to
    flip its row — but we only learn that id AFTER /start returns.
    We work around this by having the supervisor poll the
    ``live_deployments`` table for a row created "just now".
    """
    body = {
        "strategy_id": str(test_strategy.id),
        "config": {},
        "instruments": ["AAPL"],
        "paper_trading": True,
    }

    deployment_id_fut: asyncio.Future[UUID] = asyncio.get_event_loop().create_future()

    async def _watch_for_deployment() -> None:
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            async with session_factory() as session:
                row = (
                    await session.execute(
                        select(LiveDeployment)
                        .where(LiveDeployment.strategy_id == test_strategy.id)
                        .order_by(LiveDeployment.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    deployment_id_fut.set_result(row.id)
                    return
            await asyncio.sleep(0.02)

    watcher = asyncio.create_task(_watch_for_deployment())
    supervisor = asyncio.create_task(
        supervisor_fn(
            session_factory,
            deployment_id_fut,
            **(supervisor_kwargs or {}),
        )
    )

    try:
        response = await client.post(
            "/api/v1/live/start",
            json=body,
            headers=headers or {},
        )
    finally:
        watcher.cancel()
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await supervisor

    return response


# ---------------------------------------------------------------------------
# Tests — tighten poll interval for fast test runs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the module-level poll timeouts so tests run in seconds,
    not minutes."""
    monkeypatch.setattr(live_module, "START_POLL_TIMEOUT_S", 3.0)
    monkeypatch.setattr(live_module, "STOP_POLL_TIMEOUT_S", 3.0)
    monkeypatch.setattr(live_module, "START_POLL_INTERVAL_S", 0.05)


# ---------------------------------------------------------------------------
# /start — happy path
# ---------------------------------------------------------------------------
async def test_stop_returns_200_immediately_when_no_active_row(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    test_user: User,
    test_strategy: Strategy,
    redis_text: AsyncRedis,
) -> None:
    """Idempotent /stop: if no live_node_processes row is active,
    return 200 with status='stopped' immediately."""
    # Seed a deployment with NO live_node_processes rows.
    async with session_factory() as session, session.begin():
        dep = await make_live_deployment(
            session,
            user=test_user,
            strategy=test_strategy,
            status="stopped",
            strategy_class="SmokeStrategy",
        )
        dep_id = dep.id

    # Trim instead of delete so the fake-supervisor consumer
    # registration from the ``client`` fixture (drill 2026-04-15
    # P0-A — /start now returns 503 if no consumer is active)
    # survives. DELETE would drop the entire stream + group.
    await redis_text.xtrim(LIVE_COMMAND_STREAM, maxlen=0)

    response = await client.post("/api/v1/live/stop", json={"deployment_id": str(dep_id)})
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"

    # No command was published (idempotent short-circuit).
    entries = await redis_text.xrange(LIVE_COMMAND_STREAM, count=10)
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_stop_returns_404_for_unknown_deployment(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/live/stop", json={"deployment_id": str(uuid4())})
    assert response.status_code == 404
