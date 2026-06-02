"""Integration: per-account restart-authority health on /live/status (PR 2 T8).

US-3: an operator must be able to read each account's restart-authority
health from ``GET /api/v1/live/status`` (and the per-deployment detail
``GET /api/v1/live/status/{deployment_id}``) WITHOUT querying the DB or
Redis directly. The fields are additive and READ-ONLY:

* per-deployment: ``auto_restart_paused`` (+ ``auto_restart_pause_reason``),
  ``consecutive_respawn_failures``, ``last_restart_at``,
  ``last_heartbeat_at`` (+ a derived ``last_heartbeat_age_s``), and the
  halt-latch state (``fleet_halted`` + ``account_halted``).
* top-level: ``router_heartbeat_age_s`` — the supervisor-liveness signal,
  read from the SAME ``router_heartbeat`` Redis key the ``/start-portfolio``
  503 gate (T4) and the SPOF alert (T9) use. NOT a second heartbeat source.

These come off the latest ``live_node_processes`` row per deployment + the
fleet/account halt latches + the router heartbeat — i.e. DB + Redis, NOT a
one-shot in-memory flag — so a follow-up re-request still shows them. The
re-request assertion is the persistence proof (UC-API-1 Persistence).

SAFETY: dedicated PostgresContainer + RedisContainer per module.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api.live_deps import get_command_bus
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.main import app
from msai.models import Base, LiveNodeProcess
from msai.services.live_command_bus import LiveCommandBus
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
async def redis_text(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    with contextlib.suppress(Exception):
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def bus(redis_text: AsyncRedis) -> LiveCommandBus:
    return LiveCommandBus(redis=redis_text, min_idle_ms=0, recovery_interval_s=60)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    bus: LiveCommandBus,
) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_get_bus() -> LiveCommandBus:
        return bus

    async def _override_user() -> dict:
        return {"sub": "test-operator", "oid": str(uuid4())}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus
    app.dependency_overrides[get_current_user] = _override_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_deployment_with_paused_restart(
    session: AsyncSession,
    *,
    account_id: str,
    auto_restart_paused: bool,
    pause_reason: str | None,
    consecutive_respawn_failures: int,
    last_restart_at: datetime | None,
    last_heartbeat_at: datetime,
    status: str = "running",
) -> tuple[object, object]:
    """Seed a deployment + an active node-process row carrying the
    restart-authority columns (T1) so the status endpoint can surface them."""
    deployment = await make_live_deployment(session, account_id=account_id, status=status)
    proc = LiveNodeProcess(
        id=uuid4(),
        deployment_id=deployment.id,
        host="test-host",
        started_at=datetime.now(UTC),
        last_heartbeat_at=last_heartbeat_at,
        status=status,
        gateway_session_key=deployment.ib_login_key,
        auto_restart_paused=auto_restart_paused,
        auto_restart_pause_reason=pause_reason,
        consecutive_respawn_failures=consecutive_respawn_failures,
        last_restart_at=last_restart_at,
    )
    session.add(proc)
    await session.commit()
    return deployment, proc


# ---------------------------------------------------------------------------
# /live/status (list) — per-account restart-authority + top-level router age
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_status_surfaces_per_account_restart_authority_and_router_age(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The operator reads per-account restart-authority health + a fresh
    ``router_heartbeat_age_s`` from /live/status, and a re-request still
    shows them (read from DB + Redis, not a one-shot in-memory flag)."""
    # Two accounts: one healthy, one with a tripped restart ceiling.
    healthy_account = "DUP-HEALTHY"
    paused_account = "DUP-PAUSED"
    restart_ts = datetime.now(UTC) - timedelta(minutes=5)
    heartbeat_ts = datetime.now(UTC) - timedelta(seconds=3)

    async with session_factory() as session:
        healthy_dep, _ = await _seed_deployment_with_paused_restart(
            session,
            account_id=healthy_account,
            auto_restart_paused=False,
            pause_reason=None,
            consecutive_respawn_failures=0,
            last_restart_at=None,
            last_heartbeat_at=heartbeat_ts,
        )
        paused_dep, _ = await _seed_deployment_with_paused_restart(
            session,
            account_id=paused_account,
            auto_restart_paused=True,
            pause_reason="max respawn ceiling reached",
            consecutive_respawn_failures=5,
            last_restart_at=restart_ts,
            last_heartbeat_at=heartbeat_ts,
        )
        healthy_id = str(healthy_dep.id)
        paused_id = str(paused_dep.id)

    # Supervisor is alive — stamp the router heartbeat so the top-level age is fresh.
    await bus.publish_router_heartbeat()

    response = await client.get("/api/v1/live/status")
    assert response.status_code == 200, response.text
    body = response.json()

    # Top-level supervisor-liveness signal — small (supervisor just stamped).
    assert "router_heartbeat_age_s" in body
    assert body["router_heartbeat_age_s"] is not None
    assert 0.0 <= body["router_heartbeat_age_s"] < 30.0

    by_id = {d["id"]: d for d in body["deployments"]}
    assert healthy_id in by_id
    assert paused_id in by_id

    healthy = by_id[healthy_id]
    paused = by_id[paused_id]

    # Every deployment row carries the additive restart-authority fields.
    for row in (healthy, paused):
        assert "auto_restart_paused" in row
        assert "auto_restart_pause_reason" in row
        assert "consecutive_respawn_failures" in row
        assert "last_restart_at" in row
        assert "last_heartbeat_at" in row
        assert "last_heartbeat_age_s" in row
        assert "fleet_halted" in row
        assert "account_halted" in row

    # Healthy account: nothing tripped.
    assert healthy["auto_restart_paused"] is False
    assert healthy["auto_restart_pause_reason"] is None
    assert healthy["consecutive_respawn_failures"] == 0
    assert healthy["last_restart_at"] is None
    assert healthy["fleet_halted"] is False
    assert healthy["account_halted"] is False

    # Paused account: the operator can SPOT it needs attention.
    assert paused["auto_restart_paused"] is True
    assert paused["auto_restart_pause_reason"] == "max respawn ceiling reached"
    assert paused["consecutive_respawn_failures"] == 5
    assert paused["last_restart_at"] is not None
    # Heartbeat age is derived from last_heartbeat_at — fresh row → small age.
    assert paused["last_heartbeat_age_s"] is not None
    assert paused["last_heartbeat_age_s"] >= 0.0

    # Persistence: a follow-up request still carries the fields (DB + Redis,
    # not a one-shot in-memory flag).
    response2 = await client.get("/api/v1/live/status")
    assert response2.status_code == 200, response2.text
    body2 = response2.json()
    assert body2["router_heartbeat_age_s"] is not None
    by_id2 = {d["id"]: d for d in body2["deployments"]}
    assert by_id2[paused_id]["auto_restart_paused"] is True
    assert by_id2[paused_id]["consecutive_respawn_failures"] == 5
    assert by_id2[paused_id]["auto_restart_pause_reason"] == "max respawn ceiling reached"


@pytest.mark.asyncio
async def test_live_status_reflects_fleet_and_account_halt_latches(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Halt-latch state is read live from Redis: an account-scoped drain
    shows ``account_halted=True`` for THAT account only; a fleet halt shows
    ``fleet_halted=True`` for every row."""
    drained_account = "DUP-DRAINED"
    other_account = "DUP-OTHER"
    heartbeat_ts = datetime.now(UTC)

    async with session_factory() as session:
        drained_dep, _ = await _seed_deployment_with_paused_restart(
            session,
            account_id=drained_account,
            auto_restart_paused=False,
            pause_reason=None,
            consecutive_respawn_failures=0,
            last_restart_at=None,
            last_heartbeat_at=heartbeat_ts,
        )
        other_dep, _ = await _seed_deployment_with_paused_restart(
            session,
            account_id=other_account,
            auto_restart_paused=False,
            pause_reason=None,
            consecutive_respawn_failures=0,
            last_restart_at=None,
            last_heartbeat_at=heartbeat_ts,
        )
        drained_id = str(drained_dep.id)
        other_id = str(other_dep.id)

    # Account-scoped halt latch on the drained account only.
    await redis_text.set(account_halt_key(drained_account), "1")

    response = await client.get("/api/v1/live/status")
    assert response.status_code == 200, response.text
    by_id = {d["id"]: d for d in response.json()["deployments"]}

    assert by_id[drained_id]["account_halted"] is True
    assert by_id[drained_id]["fleet_halted"] is False
    # The OTHER account is unaffected by the per-account drain.
    assert by_id[other_id]["account_halted"] is False
    assert by_id[other_id]["fleet_halted"] is False

    # Now a fleet-wide halt — every row shows fleet_halted.
    await redis_text.set(fleet_halt_key(), "1")
    response2 = await client.get("/api/v1/live/status")
    assert response2.status_code == 200, response2.text
    by_id2 = {d["id"]: d for d in response2.json()["deployments"]}
    assert by_id2[drained_id]["fleet_halted"] is True
    assert by_id2[other_id]["fleet_halted"] is True
    # Account-scoped latch persisted across the re-request (Redis, not in-memory).
    assert by_id2[drained_id]["account_halted"] is True
    assert by_id2[other_id]["account_halted"] is False


# ---------------------------------------------------------------------------
# /live/status/{deployment_id} (detail) — same restart-authority fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_deployment_detail_carries_restart_authority(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UC-API-1 drill-in: the per-deployment detail GET returns the same
    account's restart-authority fields so an account with
    ``consecutive_respawn_failures > 0`` is identifiable + drillable."""
    account_id = "DUP-DETAIL"
    restart_ts = datetime.now(UTC) - timedelta(minutes=2)
    heartbeat_ts = datetime.now(UTC) - timedelta(seconds=1)

    async with session_factory() as session:
        dep, _ = await _seed_deployment_with_paused_restart(
            session,
            account_id=account_id,
            auto_restart_paused=True,
            pause_reason="ib reconcile timeout",
            consecutive_respawn_failures=3,
            last_restart_at=restart_ts,
            last_heartbeat_at=heartbeat_ts,
        )
        dep_id = str(dep.id)

    response = await client.get(f"/api/v1/live/status/{dep_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["auto_restart_paused"] is True
    assert body["auto_restart_pause_reason"] == "ib reconcile timeout"
    assert body["consecutive_respawn_failures"] == 3
    assert body["last_restart_at"] is not None
    assert body["last_heartbeat_age_s"] is not None
    assert body["last_heartbeat_age_s"] >= 0.0
    assert body["fleet_halted"] is False
    assert body["account_halted"] is False
