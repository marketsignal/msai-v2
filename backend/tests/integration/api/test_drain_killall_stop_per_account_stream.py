"""Integration: per-account command-stream wiring for /stop, /drain, /kill-all
+ START (PR 2 T4).

Verifies the migration off the GLOBAL command stream:

* ``POST /api/v1/live/start-portfolio`` publishes START onto
  ``command_stream_for_account(account_id)``.
* ``POST /api/v1/live/stop`` publishes STOP_AND_REPORT_FLATNESS onto the
  deployment's per-account stream.
* ``POST /api/v1/live/drain/{account_id}`` publishes STOP_AND_REPORT_FLATNESS
  onto the drained account's per-account stream.
* ``POST /api/v1/live/kill-all`` publishes STOP_AND_REPORT_FLATNESS onto EACH
  active deployment's per-account stream (joining live_node_processes →
  live_deployments to recover account_id).

In every case the GLOBAL stream stays empty (none stranded), and a per-account
consumer drains + ACKs each command with the GLOBAL consumer NEVER started.

The endpoints' flatness-report + terminal-poll waits are patched so the
endpoint returns without a real supervisor — the publish itself hits the real
testcontainers Redis, which is the contract under test.

SAFETY: dedicated PostgresContainer + RedisContainer per module.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api import live as live_module
from msai.api.live_deps import get_command_bus
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.models import Base, LiveDeployment, LiveNodeProcess
from msai.services.live_command_bus import (
    LIVE_COMMAND_STREAM,
    LiveCommandBus,
    LiveCommandType,
    command_stream_for_account,
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
    # min_idle_ms=0 so the per-account consumer's XAUTOCLAIM PEL sweep can
    # pick up an entry immediately in-test (no 30s idle wait).
    return LiveCommandBus(redis=redis_text, min_idle_ms=0, recovery_interval_s=60)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    bus: LiveCommandBus,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    # Patch the report + terminal polling so endpoints return without a
    # real supervisor. The publish to the per-account stream still happens.
    async def _fake_poll_stop_report(*_a: object, **_k: object) -> dict:
        return {"broker_flat": True, "remaining_positions": []}

    async def _fake_poll_for_terminal(*_a: object, **_k: object) -> object:
        # Truthy row with a terminal status so the endpoints treat the
        # deployment as stopped.
        from unittest.mock import MagicMock

        row = MagicMock()
        row.status = "stopped"
        return row

    monkeypatch.setattr(live_module, "poll_stop_report", _fake_poll_stop_report)
    monkeypatch.setattr(live_module, "_poll_for_terminal", _fake_poll_for_terminal)

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


def _noop_spawn_target() -> None:
    """Picklable no-op spawn target for the ``FleetRouter`` constructor.

    The reap loop is never run in these tests and the suppressed-stop path
    never spawns — the target just needs to be a valid module-level callable.
    """
    return None


async def _seed_deployment_with_process(
    session: AsyncSession, *, account_id: str
) -> tuple[object, object]:
    """Seed a running deployment + an active live_node_processes row."""
    deployment = await make_live_deployment(session, account_id=account_id, status="running")
    proc = LiveNodeProcess(
        id=uuid4(),
        deployment_id=deployment.id,
        host="test-host",
        started_at=datetime.now(UTC),
        last_heartbeat_at=datetime.now(UTC),
        status="running",
        gateway_session_key=deployment.ib_login_key,
    )
    session.add(proc)
    await session.commit()
    return deployment, proc


async def _drain_account_stream(
    bus: LiveCommandBus, account_id: str, *, expected: int, timeout_s: float = 5.0
) -> list:
    """Run a per-account consumer until it drains+ACKs ``expected`` commands.

    The GLOBAL consumer is NEVER started here — this proves the commands are
    reachable purely via the per-account stream.
    """
    stream = command_stream_for_account(account_id)
    out: list = []
    stop_event = asyncio.Event()

    async def _drain() -> None:
        async for cmd in bus.consume("test-consumer", stop_event, stream=stream):
            out.append(cmd)
            await bus.ack(cmd.entry_id, stream=stream)
            if len(out) >= expected:
                stop_event.set()
                return

    await asyncio.wait_for(_drain(), timeout=timeout_s)
    return out


# ---------------------------------------------------------------------------
# /stop → per-account stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_publishes_to_per_account_stream_global_empty(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = "DUP-STOP-1"
    async with session_factory() as session:
        deployment, _proc = await _seed_deployment_with_process(session, account_id=account_id)

    response = await client.post("/api/v1/live/stop", json={"deployment_id": str(deployment.id)})
    assert response.status_code == 200, response.text

    # The command landed on the per-account stream, NOT the global one.
    assert await redis_text.xlen(command_stream_for_account(account_id)) == 1
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # A per-account consumer drains + ACKs it (global consumer never started).
    drained = await _drain_account_stream(bus, account_id, expected=1)
    assert len(drained) == 1
    assert drained[0].command_type is LiveCommandType.STOP_AND_REPORT_FLATNESS
    assert drained[0].deployment_id == deployment.id
    pending = await redis_text.xpending(command_stream_for_account(account_id), "live-supervisor")
    assert int(pending["pending"]) == 0


# ---------------------------------------------------------------------------
# /stop publish→consume gap (REAL-MONEY P1, adversarial-safety-review fix)
#
# The durable operator-stop suppression (``stop_requested_at``) was previously
# set ONLY by ``FleetRouter.stop()`` — which runs when the supervisor CONSUMES
# the STOP off the per-account stream. Between the API publishing STOP and the
# supervisor consuming it there is a window: if the node self-crashes
# (non-zero exit) in that window, the reaper's ``_on_child_exit`` saw
# ``stop_requested_at IS NULL`` → classified it as a non-operator-stop failure
# and AUTO-RESTARTED the node the operator just stopped (plain /stop sets no
# halt latch, so the halt gate is no backstop). The fix mirrors /kill-all and
# /drain: the API stamps ``stop_requested_at`` SYNCHRONOUSLY (under a row lock,
# committed) BEFORE publishing the STOP, so the reaper sees the committed
# intent even if the node crashes before the supervisor consumes the command.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_api_stamps_stop_intent_before_publish_so_crash_in_gap_not_restarted(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The publish→consume-gap exploit, closed: POST /stop stamps
    ``stop_requested_at`` on the active node row SYNCHRONOUSLY (committed)
    BEFORE publishing the STOP command — then a node self-crash (non-zero
    exit) in the gap before the supervisor consumes the STOP must NOT be
    auto-restarted by the reaper.

    The inverse of ``test_stop_sets_intent_before_signal`` (which covers
    ``FleetRouter.stop()`` having already committed the intent). Here the
    supervisor has NOT yet consumed the command — only the API-layer stamp
    stands between the crash and a resurrected real-money node.
    """
    import socket

    from msai.live_supervisor.fleet_router import FleetRouter
    from msai.live_supervisor.restart_policy import RestartPolicy

    account_id = "DUP-STOP-GAP"
    async with session_factory() as session:
        deployment, proc = await _seed_deployment_with_process(session, account_id=account_id)
        # Same-host row so the reaper's classify path applies cleanly.
        proc.host = socket.gethostname()
        await session.commit()
        dep_id = deployment.id
        proc_id = proc.id

    # Operator calls /stop. The supervisor has NOT consumed the STOP yet.
    response = await client.post("/api/v1/live/stop", json={"deployment_id": str(dep_id)})
    assert response.status_code == 200, response.text

    # The API committed the durable operator-stop intent on the active row
    # BEFORE publishing — the whole point of the fix.
    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, proc_id)
        assert row is not None
        assert row.stop_requested_at is not None, (
            "POST /stop must stamp stop_requested_at on the active node row "
            "synchronously before publishing the STOP command"
        )

    # Now simulate the node self-crashing IN THE GAP (before the supervisor
    # consumed the STOP): the subprocess wrote its own terminal ``failed`` row.
    async with session_factory() as session, session.begin():
        crashed = await session.get(LiveNodeProcess, proc_id)
        assert crashed is not None
        crashed.status = "failed"
        dep = await session.get(LiveDeployment, dep_id)
        if dep is not None:
            dep.status = "failed"

    # The reaper classifies the crash. Without the API-layer stamp it would
    # respawn (no halt latch from a plain /stop). With the stamp it suppresses.
    router = FleetRouter(
        db=session_factory,
        redis=redis_text,
        spawn_target=_noop_spawn_target,
        restart_policy=RestartPolicy(),
    )
    await router._on_child_exit(dep_id, exit_code=1)

    # No second node row was created (no respawn) and the deployment stays
    # ``failed`` — NOT reset to ``starting`` by an auto-restart.
    async with session_factory() as session:
        node_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess.id).where(LiveNodeProcess.deployment_id == dep_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(node_rows) == 1, (
            "a /stop'd node that crashed in the publish→consume gap must NOT be "
            "auto-restarted (no second node-process row)"
        )
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        assert dep_status == "failed", "the deployment must stay failed, not reset to starting"


@pytest.mark.asyncio
async def test_stop_stamps_intent_on_rescan_eligible_failed_row_without_sentinel(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 1 (Codex P1 #1): POST /stop on a deployment whose latest row is
    ``failed`` with ``restart_dispatched_at IS NULL`` (a supervisor-outage-window
    crash the PERIODIC RESCAN will pick up — no reaper sentinel) must STILL stamp
    ``stop_requested_at`` so a subsequent ``rescan_for_restart`` does NOT respawn
    the stopped deployment.

    The prior code required ``restart_dispatched_at IS NOT NULL`` to stamp, so
    /stop returned "stopped" without recording intent on a rescan-eligible row →
    the rescan (which gates only ``stop_requested_at IS NULL``, NOT the sentinel)
    later resurrected the node the operator stopped.

    Falsification (verified by restoring the ``restart_dispatched_at IS NOT NULL``
    filter): /stop leaves ``stop_requested_at`` NULL and the rescan respawns the
    deployment (a second node row + the deployment reset to ``starting``).
    """
    import socket

    from msai.live_supervisor.fleet_router import FleetRouter
    from msai.live_supervisor.restart_policy import RestartPolicy
    from msai.services.live.failure_kind import FailureKind

    account_id = "DUP-STOP-RESCAN"
    # No active row exists — the deployment is ``failed`` and its LATEST node row
    # is a recoverable crash with NO reaper sentinel (restart_dispatched_at NULL).
    async with session_factory() as session:
        deployment = await make_live_deployment(session, account_id=account_id, status="failed")
        now = datetime.now(UTC)
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            pid=None,
            host=socket.gethostname(),
            started_at=now,
            last_heartbeat_at=now,
            status="failed",
            failure_kind=FailureKind.UNKNOWN.value,  # recoverable → rescan-eligible
            gateway_session_key=deployment.ib_login_key,
            consecutive_respawn_failures=0,
            auto_restart_paused=False,
            restart_dispatched_at=None,  # NO sentinel — the gap FIX 1 closes
            stop_requested_at=None,
        )
        session.add(row)
        await session.commit()
        dep_id = deployment.id
        row_id = row.id

    # Operator stops the (already-failed) deployment.
    response = await client.post("/api/v1/live/stop", json={"deployment_id": str(dep_id)})
    assert response.status_code == 200, response.text

    # FIX 1: the durable intent is stamped on the rescan-eligible failed row.
    async with session_factory() as session:
        stamped = await session.get(LiveNodeProcess, row_id)
        assert stamped is not None
        assert stamped.stop_requested_at is not None, (
            "POST /stop must stamp stop_requested_at on a rescan-eligible failed "
            "latest row even when restart_dispatched_at IS NULL"
        )
        # Intent only — the failed row is NOT resurrected, deployment stays failed.
        assert stamped.status == "failed"
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        assert dep_status == "failed"

    # The periodic rescan must now DECLINE to respawn the stopped deployment.
    router = FleetRouter(
        db=session_factory,
        redis=redis_text,
        spawn_target=_noop_spawn_target,
        restart_policy=RestartPolicy(),
        rescan_stale_seconds=5,
    )
    restarted = await router.rescan_for_restart()
    assert restarted == 0, "the rescan must NOT respawn an operator-stopped deployment"

    async with session_factory() as session:
        node_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess.id).where(LiveNodeProcess.deployment_id == dep_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(node_rows) == 1, (
            "no fresh node row may be reserved — the rescan honored the stop intent"
        )
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        assert dep_status == "failed", "the deployment must stay failed, not reset to starting"


# ---------------------------------------------------------------------------
# /drain → per-account stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_publishes_to_per_account_stream_global_empty(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = "DUP-DRAIN-1"
    async with session_factory() as session:
        dep1, _ = await _seed_deployment_with_process(session, account_id=account_id)
        dep2, _ = await _seed_deployment_with_process(session, account_id=account_id)

    response = await client.post(f"/api/v1/live/drain/{account_id}")
    assert response.status_code == 200, response.text

    # Two deployments under the account → two STOPs on the account stream.
    assert await redis_text.xlen(command_stream_for_account(account_id)) == 2
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    drained = await _drain_account_stream(bus, account_id, expected=2)
    drained_dep_ids = {c.deployment_id for c in drained}
    assert drained_dep_ids == {dep1.id, dep2.id}
    pending = await redis_text.xpending(command_stream_for_account(account_id), "live-supervisor")
    assert int(pending["pending"]) == 0


# ---------------------------------------------------------------------------
# /kill-all → each deployment's per-account stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_all_publishes_to_each_per_account_stream_global_empty(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    acc_a = "DUP-KILL-A"
    acc_b = "DUP-KILL-B"
    async with session_factory() as session:
        dep_a, _ = await _seed_deployment_with_process(session, account_id=acc_a)
        dep_b, _ = await _seed_deployment_with_process(session, account_id=acc_b)

    response = await client.post("/api/v1/live/kill-all")
    assert response.status_code in (200, 207), response.text

    # Each account's STOP lands on ITS per-account stream; global is empty.
    assert await redis_text.xlen(command_stream_for_account(acc_a)) == 1
    assert await redis_text.xlen(command_stream_for_account(acc_b)) == 1
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    drained_a = await _drain_account_stream(bus, acc_a, expected=1)
    drained_b = await _drain_account_stream(bus, acc_b, expected=1)
    assert drained_a[0].deployment_id == dep_a.id
    assert drained_b[0].deployment_id == dep_b.id


# ---------------------------------------------------------------------------
# START → per-account stream (via /start-portfolio publish path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_publish_targets_per_account_stream(
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
) -> None:
    """The START producer path (``publish_start(account_id=...)``) the
    endpoint uses lands on the per-account stream and a per-account consumer
    drains it with the global consumer off (attach-late safe)."""
    account_id = "DUP-START-1"
    dep_id = uuid4()

    # Publish BEFORE the consumer attaches — must still be consumed.
    await bus.publish_start(
        dep_id,
        {"deployment_slug": "p-start"},
        account_id=account_id,
    )
    assert await redis_text.xlen(command_stream_for_account(account_id)) == 1
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    drained = await _drain_account_stream(bus, account_id, expected=1)
    assert drained[0].command_type is LiveCommandType.START
    assert drained[0].deployment_id == dep_id


# ---------------------------------------------------------------------------
# Stranded-START flip guard (PR 2 T4 review P1): the /start-portfolio
# poll-timeout flip must NOT orphan a LIVE real-money node. A node that is
# slow to build/reconcile (IB connect/reconcile legitimately takes time —
# nautilus gotchas #10/#19) has an ACTIVE live_node_processes row; flipping
# LiveDeployment.status='failed' would make it invisible to
# /live/status?active_only=true AND to the deploy gate, so a routine deploy
# could recreate the supervisor while a real-money node is unaccounted-for.
# The flip is allowed ONLY when the START genuinely stranded (no active row).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_node_process_exists_true_for_slow_building_node(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment whose node row is still ``building`` (slow IB reconcile)
    has an ACTIVE process — the stranded-START flip helper must report True
    so the API does NOT mark the deployment ``failed``."""
    async with session_factory() as session:
        deployment = await make_live_deployment(
            session, account_id="DUP-SLOW-BUILD", status="starting"
        )
        proc = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            host="test-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="building",
            gateway_session_key=deployment.ib_login_key,
        )
        session.add(proc)
        await session.commit()
        dep_id = deployment.id

    async with session_factory() as session:
        assert await live_module._active_node_process_exists(session, dep_id) is True


@pytest.mark.asyncio
async def test_active_node_process_exists_false_when_genuinely_stranded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment with NO live_node_processes row (the START stranded — no
    consumer attached) reports False, so the API IS allowed to flip the
    deployment ``failed`` (fail-closed, gated)."""
    async with session_factory() as session:
        deployment = await make_live_deployment(
            session, account_id="DUP-STRANDED", status="starting"
        )
        await session.commit()
        dep_id = deployment.id

    async with session_factory() as session:
        assert await live_module._active_node_process_exists(session, dep_id) is False


@pytest.mark.asyncio
async def test_active_node_process_exists_false_when_row_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A node row that already reached a terminal status (``failed``) is NOT
    active — the helper reports False (the node genuinely died, so the
    deployment flip is correct)."""
    async with session_factory() as session:
        deployment = await make_live_deployment(
            session, account_id="DUP-TERMINAL", status="starting"
        )
        proc = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            host="test-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="failed",
            gateway_session_key=deployment.ib_login_key,
        )
        session.add(proc)
        await session.commit()
        dep_id = deployment.id

    async with session_factory() as session:
        assert await live_module._active_node_process_exists(session, dep_id) is False
