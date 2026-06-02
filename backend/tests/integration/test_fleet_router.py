"""Integration tests for ``FleetRouter`` (Phase 1 task 1.7).

Covers the INSERT-spawn-UPDATE pattern (decision #13, Codex v4 P0),
the halt-flag re-check (decision #16, Codex v4 P0), the reap loop
(decision #15, instant exit detection via the node handle cache), the stop
path (with pid fallback for post-restart discovered subprocesses),
and the watchdog lock-first atomic kill (v9 Codex v8 P0+P1).

The trading subprocess is replaced by a trivial ``sleep`` target so
tests run fast and deterministically. Real subprocess spawning is
Task 1.8's charter.

SAFETY: dedicated Postgres + Redis testcontainers per module, same
pattern as the rest of the Phase 1 integration suite.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.live_supervisor.fleet_router import FleetRouter
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.failure_kind import FailureKind
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
async def redis_client(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    with contextlib.suppress(Exception):
        await client.delete("msai:risk:halt")
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.delete("msai:risk:halt")
        await client.aclose()


@pytest_asyncio.fixture
async def deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> LiveDeployment:
    """Seed a LiveDeployment row so FleetRouter.spawn has something
    to FOR UPDATE against."""
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"pm-{uuid4().hex}",
            email=f"pm-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name="pm-test",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        dep = await make_live_deployment(session, user=user, strategy=strategy, status="starting")
        await session.commit()
        return dep


def _sleep_target(seconds: float = 30.0) -> None:
    """Stand-in for the real trading subprocess — just sleeps so the
    spawn path has a live pid to observe and signal. Must be a
    top-level function so ``mp.Process`` can pickle it."""
    time.sleep(seconds)


def _exit_fast_target(code: int) -> None:
    """Exits immediately with the given code — used by reap-loop tests."""
    raise SystemExit(code)


@pytest_asyncio.fixture
async def process_manager(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> AsyncIterator[FleetRouter]:
    """FleetRouter wired with the test DB + Redis. The spawn_target
    is set to the in-file ``_sleep_target`` so tests that exercise the
    spawn path get a real live subprocess without launching Nautilus.
    """
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
    )
    yield pm
    # Clean up any live children the test left behind.
    for cached in list(pm.node_handle_cache.values()):
        with contextlib.suppress(Exception):
            cached.proc.terminate()
            cached.proc.join(timeout=2)


# ---------------------------------------------------------------------------
# Phase A: reserve-the-slot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_inserts_starting_row_with_pid(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """Happy path: spawn inserts a row, process.start() runs, phase C
    records the pid. The row ends up with a real pid on disk."""
    ok = await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    # The row exists with the live child's pid.
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "starting"
        assert row.pid is not None
        assert row.pid > 0
        assert row.error_message is None
        assert row.failure_kind is None

    # Handle cache now holds the mp.Process so stop() + reap_loop can find it.
    assert deployment.id in process_manager.node_handle_cache


@pytest.mark.asyncio
async def test_spawn_idempotent_when_row_already_active(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """Idempotency test #1: an already-active row means the second spawn
    returns True without creating a new row or starting a child."""
    # Pre-seed an active row
    async with session_factory() as session:
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=12345,
            host="preseed",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="running",
        )
        session.add(row)
        await session.commit()

    ok = await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
                )
            )
            .scalars()
            .all()
        )
        # Only the pre-seeded row — spawn did NOT insert a second.
        assert len(rows) == 1
        assert rows[0].pid == 12345

    # Handle cache stays empty (no new process was started).
    assert deployment.id not in process_manager.node_handle_cache


@pytest.mark.asyncio
async def test_spawn_during_stop_returns_false_not_true(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """Codex v4 P0: a 'start' arriving while the prior run is still in
    'stopping' MUST return False (not idempotent success), so the
    command sits in the PEL for XAUTOCLAIM retry after the stopping
    row reaches a terminal state."""
    async with session_factory() as session:
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=12345,
            host="preseed",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="stopping",
        )
        session.add(row)
        await session.commit()

    ok = await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is False  # Not ACKed → caller retries via PEL


@pytest.mark.asyncio
async def test_spawn_unknown_deployment_returns_false(
    process_manager: FleetRouter,
) -> None:
    """If the deployment_slug doesn't match any row, spawn returns
    False (hard failure) so the command stays in the PEL."""
    ok = await process_manager.spawn(
        deployment_id=uuid4(),
        deployment_slug="0000000000000000",
        payload={},
        idempotency_key="k1",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_spawn_drops_stale_start_for_terminal_deployment(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """PR 2 T4 stranded-START resurrection guard (review P2).

    A START left un-ACKed in the per-account stream after a
    ``/start-portfolio`` poll-timeout (the operator saw a 504 and walked
    away) must NOT resurrect a live TradingNode when a later supervisor
    re-attaches its consumer and XAUTOCLAIM re-delivers the stale START.
    The deployment row is in a terminal state (``failed``/``stopped``) by
    then, so spawn must ACK-and-drop (return True) WITHOUT spawning a
    subprocess or inserting a new ``live_node_processes`` row.
    """
    # Operator abandoned the deployment — its row is terminal.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, deployment.id)
        assert dep is not None
        dep.status = "failed"
        await session.commit()

    ok = await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    # ACK-and-drop: the stale START is removed from the PEL, not retried.
    assert ok is True

    # No subprocess spawned, no node-process row created.
    assert deployment.id not in process_manager.node_handle_cache
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# PR 2 F1 (review P1): same-gateway startup race — advisory-lock serialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_gateway_reservations_serialise_to_one(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PR 2 F1 (review P1): two START commands for DIFFERENT deployments that
    SHARE one IB Gateway must NOT both reserve a slot concurrently.

    T4 made the per-account command consumers run CONCURRENTLY, so two
    ``_phase_a_reserve_slot`` transactions for two deployments on the SAME
    ``gateway_session_key`` can race: each ``FOR UPDATE``s only ITS OWN
    deployment row, so without serialisation both can observe "no other
    starting row on this gateway", both INSERT a ``starting`` row, and both
    launch a TradingNode against the same gateway → client_id collision /
    silent disconnect (Nautilus gotcha #3).

    The transaction-level advisory lock keyed by the gateway makes the
    concurrent-startup check+insert ATOMIC per gateway: exactly ONE
    reservation wins (returns a real row id) and the other observes the
    winner's ``starting`` row → ``CONCURRENT_STARTUP``.

    Falsification (verified manually by removing the ``pg_advisory_xact_lock``
    statement in ``_phase_a_reserve_slot``): without the lock both calls can
    return a real row id (two ``starting`` rows on one gateway), which this
    assertion catches.
    """
    import asyncio

    from msai.live_supervisor.fleet_router import _PhaseAOutcome

    shared_gateway = "msai-paper-shared:localhost:4002"

    # Two DISTINCT deployments that share ONE gateway (the Shape-A / shared-
    # gateway config the guard exists for). Each gets its own auto-created
    # user + strategy so they're genuinely independent deployments.
    async with session_factory() as session:
        dep_a = await make_live_deployment(session, status="starting")
        dep_b = await make_live_deployment(session, status="starting")
        await session.commit()

    # Fire BOTH reservations concurrently against the SAME gateway. Both pass
    # ``gateway_session_key=shared_gateway`` so the per-gateway guard + the
    # per-gateway advisory lock both engage.
    results = await asyncio.gather(
        process_manager._phase_a_reserve_slot(
            deployment_id=dep_a.id,
            deployment_slug=dep_a.deployment_slug,
            gateway_session_key=shared_gateway,
        ),
        process_manager._phase_a_reserve_slot(
            deployment_id=dep_b.id,
            deployment_slug=dep_b.deployment_slug,
            gateway_session_key=shared_gateway,
        ),
    )

    reserved = [r for r in results if isinstance(r, UUID)]
    rejected = [r for r in results if r is _PhaseAOutcome.CONCURRENT_STARTUP]

    assert len(reserved) == 1, (
        "exactly ONE same-gateway reservation must win — got "
        f"{len(reserved)} (both starting against one gateway is the race F1 fixes)"
    )
    assert len(rejected) == 1, (
        f"the loser must get CONCURRENT_STARTUP, not a second reserved slot — got {results!r}"
    )

    # Exactly ONE active ``starting`` node row exists across BOTH deployments
    # on this gateway — never two nodes against one IB Gateway.
    async with session_factory() as session:
        starting_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(
                        LiveNodeProcess.gateway_session_key == shared_gateway,
                        LiveNodeProcess.status == "starting",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(starting_rows) == 1, (
            f"exactly one starting node row per shared gateway; saw {len(starting_rows)}"
        )


@pytest.mark.asyncio
async def test_concurrent_different_gateway_reservations_both_succeed(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PR 2 F1 (review P1) companion: the advisory lock keys off the GATEWAY, so
    two deployments on DIFFERENT gateways (distinct IB logins — today's Shape B)
    can still start concurrently. Different gateways → different lock keys → no
    cross-gateway contention. This pins that the serialisation fix does NOT
    regress the multi-login concurrency enabler.
    """
    import asyncio

    async with session_factory() as session:
        dep_a = await make_live_deployment(session, status="starting")
        dep_b = await make_live_deployment(session, status="starting")
        await session.commit()

    results = await asyncio.gather(
        process_manager._phase_a_reserve_slot(
            deployment_id=dep_a.id,
            deployment_slug=dep_a.deployment_slug,
            gateway_session_key="msai-login-1:localhost:4002",
        ),
        process_manager._phase_a_reserve_slot(
            deployment_id=dep_b.id,
            deployment_slug=dep_b.deployment_slug,
            gateway_session_key="msai-login-2:localhost:4002",
        ),
    )

    assert all(isinstance(r, UUID) for r in results), (
        "two deployments on DIFFERENT gateways must BOTH reserve concurrently "
        f"(distinct lock keys, no contention); got {results!r}"
    )


# ---------------------------------------------------------------------------
# Phase B: halt-flag re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_blocked_by_halt_flag_marks_row_failed(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Decision #16 / Codex v4 P0: re-check the halt flag AFTER the
    phase-A COMMIT. If set, mark the row failed with
    FailureKind.HALT_ACTIVE and return True (caller ACKs; no retry
    until /resume clears the flag)."""
    await redis_client.set("msai:risk:halt", "1")

    ok = await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True  # ACKed — no retry until /resume

    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.failure_kind == FailureKind.HALT_ACTIVE.value
        assert row.error_message is not None
        assert "halt" in row.error_message.lower()

    # No child was spawned.
    assert deployment.id not in process_manager.node_handle_cache


# ---------------------------------------------------------------------------
# Reap loop (decision #15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_loop_detects_zero_exit_and_marks_stopped(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A child that exits cleanly (code 0) is surfaced by reap_loop
    as status='stopped', failure_kind='none'."""
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_exit_fast_target,
        spawn_args=(0,),
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    # Wait for the child to exit, then run one reap iteration.
    proc = pm.node_handle_cache[deployment.id].proc
    proc.join(timeout=5)
    assert not proc.is_alive()
    await pm.reap_once()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "stopped"
        assert row.exit_code == 0
        assert row.failure_kind == FailureKind.NONE.value

    assert deployment.id not in pm.node_handle_cache


@pytest.mark.asyncio
async def test_reap_loop_detects_nonzero_exit_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A child whose OS process STARTED and then exited non-zero (without the
    subprocess writing its own terminal row) is marked failed with
    FailureKind.NODE_CRASHED — the node RAN (its process started), so the
    reaper classifies it as a RECOVERABLE runtime crash, NOT a pre-spawn
    SPAWN_FAILED_PERMANENT (PR 2 / F2: the failure-kind overload resolution).
    The real exit_code is recorded."""
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_exit_fast_target,
        spawn_args=(7,),
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    proc = pm.node_handle_cache[deployment.id].proc
    proc.join(timeout=5)
    await pm.reap_once()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.exit_code == 7
        assert row.failure_kind == FailureKind.NODE_CRASHED.value
        assert row.error_message is not None
        assert "7" in row.error_message


@pytest.mark.asyncio
async def test_watchdog_sigkills_wedged_starting_row_and_marks_build_timeout(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Codex batch 3 iter8 P1 regression: a row stuck in
    ``starting`` / ``building`` past ``startup_hard_timeout_s`` must
    be SIGKILLed by the watchdog and marked
    ``failed`` / ``FailureKind.BUILD_TIMEOUT``. Without this, a
    wedged ``node.build()`` would hold the active-row slot
    indefinitely (heartbeat keeps it fresh; HeartbeatMonitor excludes
    startup statuses by design) and block every future ``/start``
    for the deployment."""
    # Use a tiny timeout so the test runs in well under a second.
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        spawn_args=(30.0,),
        startup_hard_timeout_s=0.1,
        watchdog_poll_interval_s=0.05,
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    proc = pm.node_handle_cache[deployment.id].proc
    assert proc.is_alive()
    pid_before = proc.pid

    # Force the row to look "stuck building": the spawn path inserts
    # with status='starting'. The subprocess would normally flip it
    # to 'building' from inside, but our _sleep_target doesn't do
    # that — so the row stays at 'starting'. That's exactly the
    # state the watchdog needs to act on. We just need to age it.
    import asyncio as _asyncio

    await _asyncio.sleep(0.15)  # exceed the 0.1s timeout

    await pm.watchdog_once()

    # The watchdog should have SIGKILLed the child and marked the row
    proc.join(timeout=2)
    assert not proc.is_alive(), "watchdog did not kill the wedged child"

    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.failure_kind == FailureKind.BUILD_TIMEOUT.value
        assert row.error_message is not None
        assert "wedged" in row.error_message
        assert str(pid_before) in row.error_message


@pytest.mark.asyncio
async def test_watchdog_skips_rows_from_other_hosts(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Codex batch 3 iter9 P1 regression: in a multi-supervisor or
    rolling-restart deployment, a row whose ``host`` column doesn't
    match this supervisor's hostname must NOT be touched by the
    watchdog. ``row.pid`` from another supervisor's PID namespace
    is meaningless to ``os.kill`` here — flipping the row to failed
    without killing the actual wedged child would reopen the
    active-row slot while the original child is still alive on
    another host, allowing a duplicate spawn."""
    import socket as _socket

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        startup_hard_timeout_s=0.1,
        watchdog_poll_interval_s=0.05,
    )

    # Insert a stuck row that LOOKS like it was spawned by another
    # supervisor instance: stale started_at, status='building',
    # host='some-other-host'.
    foreign_row_id = uuid4()
    foreign_pid = 999_999  # very unlikely to exist
    async with session_factory() as session, session.begin():
        # Make a deep-past started_at so the timeout check fires.
        session.add(
            LiveNodeProcess(
                id=foreign_row_id,
                deployment_id=deployment.id,
                gateway_session_key="msai-paper-primary:localhost:4002",
                pid=foreign_pid,
                host="some-other-supervisor-host",
                started_at=datetime(2000, 1, 1, tzinfo=UTC),
                last_heartbeat_at=datetime.now(UTC),
                status="building",
            )
        )

    self_host = _socket.gethostname()
    assert self_host != "some-other-supervisor-host"

    # Run the watchdog — it must NOT touch the foreign row.
    await pm.watchdog_once()

    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, foreign_row_id)
        assert row is not None
        assert row.status == "building", (
            "watchdog touched a row from another supervisor host — "
            "row.pid from a different PID namespace is meaningless here, "
            "and flipping the row to failed without killing the original "
            "child would let a duplicate spawn through."
        )
        assert row.failure_kind is None


@pytest.mark.asyncio
async def test_watchdog_leaves_fresh_starting_rows_alone(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A row in ``starting`` whose age is BELOW the timeout must NOT
    be killed by the watchdog. Sanity check that the timeout actually
    matters."""
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        spawn_args=(30.0,),
        startup_hard_timeout_s=10.0,  # generous
        watchdog_poll_interval_s=1.0,
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    proc = pm.node_handle_cache[deployment.id].proc
    assert proc.is_alive()

    await pm.watchdog_once()

    # Child must still be alive and the row must still be 'starting'
    assert proc.is_alive(), "watchdog killed a fresh row it shouldn't have"
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "starting"
        assert row.failure_kind is None


@pytest.mark.asyncio
async def test_watchdog_syncs_parent_deployment_to_failed_and_is_rescan_recoverable(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """PR 2 F3 (review P2): when the watchdog SIGKILLs a wedged
    ``starting``/``building`` node and marks the NODE row ``failed`` /
    ``BUILD_TIMEOUT``, it must ALSO sync the parent ``LiveDeployment.status`` to
    ``failed`` in the SAME transaction.

    Without the sync the deployment lingers at its previous non-terminal value
    (``starting``), which has two real consequences:
      1. The bounded auto-restart re-scan candidate query requires BOTH the
         node row AND ``LiveDeployment.status == 'failed'`` — so a
         watchdog-killed deployment (no local reaper handle) is MISSED by the
         rescan and stays flat-and-unmonitored.
      2. ``/live/status`` keeps showing the deployment active though the child
         is dead.

    This test wedges a node, runs the watchdog, then asserts (a) the parent
    deployment is terminal (``failed``), and (b) the rescan candidate scan picks
    it up (a recoverable BUILD_TIMEOUT crash), proving it's recoverable.
    """
    from msai.live_supervisor.restart_policy import RestartPolicy

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        spawn_args=(30.0,),
        startup_hard_timeout_s=0.1,
        watchdog_poll_interval_s=0.05,
        restart_policy=RestartPolicy(),
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True
    proc = pm.node_handle_cache[deployment.id].proc
    assert proc.is_alive()

    import asyncio as _asyncio

    await _asyncio.sleep(0.15)  # exceed the 0.1s startup timeout
    await pm.watchdog_once()
    proc.join(timeout=2)

    # The node row is failed/BUILD_TIMEOUT (the existing behaviour) ...
    async with session_factory() as session:
        node_row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert node_row.status == "failed"
        assert node_row.failure_kind == FailureKind.BUILD_TIMEOUT.value
        # ... AND the parent deployment was synced terminal (the F3 fix).
        dep_row = await session.get(LiveDeployment, deployment.id)
        assert dep_row is not None
        assert dep_row.status == "failed", (
            "watchdog must sync the parent deployment to 'failed' — otherwise the "
            "rescan misses it and /live/status shows a dead deployment as active"
        )

    # The rescan candidate scan now PICKS IT UP (recoverable BUILD_TIMEOUT crash
    # + deployment failed) — proving the watchdog-killed node is recoverable.
    candidates = await pm._load_rescan_candidates()
    assert deployment.id in candidates, (
        "a watchdog-killed node must be recoverable by the bounded auto-restart "
        "re-scan (requires the parent deployment to be 'failed' — the F3 sync)"
    )


@pytest.mark.asyncio
async def test_watchdog_flips_dead_paused_stopreq_starting_row_unconditionally(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """CROSS-PATH AUDIT (Rule 1): the watchdog's FAILURE-MARKING flip of a dead
    wedged ``starting``/``building`` row must be UNCONDITIONAL on
    ``auto_restart_paused`` AND ``stop_requested_at`` — cleaning a dead row out
    of the active unique-index set is unconditional; respawn-eligibility is a
    separate decision.

    A wedged startup row that ALSO carries ``auto_restart_paused=True`` (a
    ceiling-tripping retry that then wedged) and ``stop_requested_at`` (a /stop
    that arrived mid-wedge) must STILL be SIGKILLed + flipped to ``failed`` —
    otherwise it stays stuck in the active set forever, blocking every future
    /start (partial unique index). The watchdog's WHERE filters only
    status/age/host, so it should already do this; this test PROVES it.
    """
    import socket as _socket

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        startup_hard_timeout_s=0.1,
        watchdog_poll_interval_s=0.05,
    )

    # A wedged starting row on THIS host (so the watchdog owns it), aged past the
    # timeout, with the pause latch set AND a durable stop intent. No live pid —
    # row.pid is None so the watchdog just flips it (ProcessLookupError path not
    # needed; pid_to_kill stays None and the row is flipped regardless).
    paused_row_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            LiveNodeProcess(
                id=paused_row_id,
                deployment_id=deployment.id,
                gateway_session_key="msai-paper-primary:localhost:4002",
                pid=None,
                host=_socket.gethostname(),
                started_at=datetime(2000, 1, 1, tzinfo=UTC),
                last_heartbeat_at=datetime.now(UTC),
                status="starting",
                auto_restart_paused=True,
                stop_requested_at=datetime.now(UTC),
            )
        )

    await pm.watchdog_once()

    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, paused_row_id)
        assert row is not None
        assert row.status == "failed", (
            "the watchdog must flip a DEAD wedged paused/stop-requested startup "
            "row to failed unconditionally so it leaves the active set"
        )
        assert row.failure_kind == FailureKind.BUILD_TIMEOUT.value
        # Cleanup preserves the latch + intent — it is a flip, not a reset.
        assert row.auto_restart_paused is True
        assert row.stop_requested_at is not None


@pytest.mark.asyncio
async def test_reap_loop_maps_exit_code_2_to_reconciliation_failed(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Codex batch 3 iter7 P2 regression: exit code 2 from
    ``run_subprocess_async`` (StartupHealthCheckFailed path) must be
    mapped to ``FailureKind.RECONCILIATION_FAILED`` by the reap loop,
    not collapsed to ``SPAWN_FAILED_PERMANENT``. This matters when
    the subprocess's own ``_mark_terminal`` write missed (e.g. a
    transient DB error in the finally block) — the structured exit
    code is the only way to preserve the diagnosis."""
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_exit_fast_target,
        spawn_args=(2,),
    )
    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    assert ok is True

    proc = pm.node_handle_cache[deployment.id].proc
    proc.join(timeout=5)
    await pm.reap_once()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.exit_code == 2
        assert row.failure_kind == FailureKind.RECONCILIATION_FAILED.value, (
            f"exit code 2 must map to RECONCILIATION_FAILED, got {row.failure_kind}"
        )


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_via_handle_map_signals_sigterm(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """Stop flips the row to 'stopping', sends SIGTERM via the handle
    cache, waits briefly for the child to exit, and returns True."""
    await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    proc = process_manager.node_handle_cache[deployment.id].proc
    assert proc.is_alive()

    ok = await process_manager.stop(deployment.id, reason="user")
    assert ok is True

    # Row was flipped to 'stopping' then the reap will flip it to
    # stopped/failed. We just verify stop flipped it at minimum.
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status in ("stopping", "stopped", "failed")

    # Give the child a beat to exit from the SIGTERM.
    proc.join(timeout=3)
    assert not proc.is_alive()


@pytest.mark.asyncio
async def test_stop_idempotent_when_no_active_row(
    process_manager: FleetRouter,
    deployment: LiveDeployment,
) -> None:
    """Calling stop when there's no active process is a successful no-op."""
    ok = await process_manager.stop(deployment.id, reason="user")
    assert ok is True


@pytest.mark.asyncio
async def test_stop_after_supervisor_restart_uses_row_pid(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Codex v5 P0 regression: a supervisor restart wipes the handle
    cache. A subsequent stop() must read the pid from the DB row and
    signal it directly — NOT silently succeed with no signal sent.
    """
    # Spawn a real sleeping child so we have a live pid.
    ctx = mp.get_context("spawn")
    child = ctx.Process(target=_sleep_target, args=(30,))
    child.start()
    assert child.pid is not None

    try:
        # Seed the row with status='running' and the real pid —
        # simulating the "post-supervisor-restart discovered
        # subprocess" case. Use socket.gethostname() as the ``host``
        # so the cross-host guard (PR#1 Codex P1) treats this as a
        # same-host row and proceeds to the kill.
        import socket as _socket

        async with session_factory() as session:
            row = LiveNodeProcess(
                id=uuid4(),
                deployment_id=deployment.id,
                gateway_session_key="msai-paper-primary:localhost:4002",
                pid=child.pid,
                host=_socket.gethostname(),
                started_at=datetime.now(UTC),
                last_heartbeat_at=datetime.now(UTC),
                status="running",
            )
            session.add(row)
            await session.commit()

        # Fresh FleetRouter with an EMPTY handle cache (supervisor
        # just restarted — it doesn't know about this child yet).
        pm = FleetRouter(
            db=session_factory,
            redis=redis_client,
            spawn_target=_sleep_target,
        )
        assert not pm.node_handle_cache

        ok = await pm.stop(deployment.id, reason="user")
        assert ok is True

        # The child must have actually received SIGTERM — wait briefly
        # then assert it's dead.
        child.join(timeout=5)
        assert not child.is_alive()
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=2)


@pytest.mark.asyncio
async def test_stop_refuses_cross_host_row(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """PR#1 Codex P1 regression: cross-host PID kill guard.

    The Phase 2 architecture has a trading VM + a compute VM
    sharing one Redis command stream. If a STOP command gets
    consumed by the wrong supervisor, the wrong supervisor must
    NOT signal a local PID — that local PID might happen to exist
    but belongs to a completely unrelated process while the real
    trading subprocess keeps running on its original host.

    Verify: a row with ``host != socket.gethostname()`` causes
    ``stop()`` to return False (no ACK → PEL redelivery) and NOT
    flip the row to ``stopping``.
    """
    import socket as _socket

    # Seed a row from ``other-supervisor-host`` with a BOGUS pid
    # that definitely doesn't correspond to anything on this host.
    # Using a very large number avoids ProcessLookupError masking
    # a real guard failure as ``ok=True``.
    bogus_pid = 2**24 + 7
    async with session_factory() as session:
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment.id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=bogus_pid,
            host="other-supervisor-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="running",
        )
        session.add(row)
        await session.commit()

    # Sanity check: we're clearly NOT on ``other-supervisor-host``.
    assert _socket.gethostname() != "other-supervisor-host"

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
    )
    assert not pm.node_handle_cache  # fresh PM, no local handle for this row

    ok = await pm.stop(deployment.id, reason="user")
    # NOT ACKed — the command stays in the PEL for the correct
    # supervisor to pick up via XAUTOCLAIM redelivery.
    assert ok is False

    # Row status must NOT have been flipped to 'stopping' — that
    # would be a cross-host state mutation. The right supervisor's
    # own stop flow owns the row's state transitions.
    async with session_factory() as session:
        refetched = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert refetched.status == "running", (
            "cross-host stop must NOT flip row.status — row state belongs "
            "to the supervisor that owns the process"
        )


# ---------------------------------------------------------------------------
# Payload factory (Phase 4 task #154 scope-B wiring)
# ---------------------------------------------------------------------------


def _echo_target(marker: str, pid_sink: str) -> None:
    """Spawn target that writes a marker + its pid to a file, then
    exits. Used to prove the payload factory's return tuple reached
    ``mp.Process(args=...)`` through the production path (as opposed
    to the static ``spawn_args`` fallback)."""
    import os
    from pathlib import Path

    Path(pid_sink).write_text(f"{marker}:{os.getpid()}")


@pytest.mark.asyncio
async def test_spawn_with_payload_factory_passes_returned_args_to_process(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
    tmp_path,
) -> None:
    """Phase 4 task #154 scope-B: when a ``payload_factory`` is
    configured, its return value becomes ``mp.Process(args=...)`` for
    this spawn — the static ``spawn_args`` is NOT used.

    Proven end-to-end by having the factory construct an args tuple
    whose values are unique to this test run and having the spawn
    target write them to a file the test reads back."""
    from uuid import UUID

    marker = f"factory-test-{uuid4().hex[:8]}"
    pid_sink = str(tmp_path / "echo.out")

    factory_calls: list[tuple[UUID, UUID, str, dict]] = []

    async def _payload_factory(
        row_id: UUID,
        deployment_id: UUID,
        deployment_slug: str,
        payload_dict: dict,
    ) -> tuple[str, str]:
        factory_calls.append((row_id, deployment_id, deployment_slug, payload_dict))
        return (marker, pid_sink)

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_echo_target,
        # Deliberately provide a different fallback — the test
        # asserts the factory return overrides this.
        spawn_args=("wrong-marker", "/nonexistent/path"),
        payload_factory=_payload_factory,
    )

    try:
        ok = await pm.spawn(
            deployment_id=deployment.id,
            deployment_slug=deployment.deployment_slug,
            payload={"from_api": "hello"},
            idempotency_key="factory-k1",
        )
        assert ok is True

        # Wait for the echo subprocess to finish + flush
        proc = pm.node_handle_cache[deployment.id].proc
        proc.join(timeout=5)
        assert not proc.is_alive()

        # Read the marker back — proves the factory tuple was used
        from pathlib import Path

        content = Path(pid_sink).read_text()
        assert content.startswith(f"{marker}:"), (
            f"expected marker {marker} at start of {content!r}, factory args "
            f"were not wired into mp.Process(args=...)"
        )
    finally:
        for cached in list(pm.node_handle_cache.values()):
            with contextlib.suppress(Exception):
                cached.proc.terminate()
                cached.proc.join(timeout=2)

    # Factory was called once with the expected identifiers
    assert len(factory_calls) == 1
    row_id, dep_id, slug, payload_dict = factory_calls[0]
    assert dep_id == deployment.id
    assert slug == deployment.deployment_slug
    assert payload_dict == {"from_api": "hello"}
    assert isinstance(row_id, UUID)


@pytest.mark.asyncio
async def test_spawn_payload_factory_permanent_error_marks_row_failed_and_acks(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A PERMANENT ``payload_factory`` exception (ValueError,
    ImportError, etc.) must NOT retry via XAUTOCLAIM: these are
    operator config bugs that retrying won't fix. The row is marked
    ``SPAWN_FAILED_PERMANENT`` and the command is ACKed so the PEL
    releases it.
    """

    async def _bad_factory(
        row_id,  # noqa: ARG001
        deployment_id,  # noqa: ARG001
        deployment_slug,  # noqa: ARG001
        payload_dict,  # noqa: ARG001
    ) -> tuple:
        # ValueError is our permanent-category exception — used by
        # the paper/live safety guard in the production payload
        # factory.
        raise ValueError("paper_trading mismatch: supervisor expects live")

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        payload_factory=_bad_factory,
    )

    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="bad-k1",
    )
    # Command must be ACKed (return True) so it doesn't loop forever
    assert ok is True
    # No process handle was registered
    assert deployment.id not in pm.node_handle_cache

    # Row is marked failed with SPAWN_FAILED_PERMANENT
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
        assert row.error_message is not None
        assert "paper_trading mismatch" in row.error_message


@pytest.mark.asyncio
async def test_spawn_payload_factory_transient_error_does_not_ack(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A TRANSIENT ``payload_factory`` exception (generic Exception,
    typically SQLAlchemy OperationalError when Postgres is briefly
    down) must NOT ACK the command — the caller returns False so
    the PEL redelivers via XAUTOCLAIM once the dependency recovers.
    Codex iter5 P2 regression.
    """

    async def _transient_factory(
        row_id,  # noqa: ARG001
        deployment_id,  # noqa: ARG001
        deployment_slug,  # noqa: ARG001
        payload_dict,  # noqa: ARG001
    ) -> tuple:
        # A generic RuntimeError stands in for SQLAlchemy
        # OperationalError / aioredis ConnectionError / network
        # timeout — all of these are transient dependency
        # failures, not operator config bugs.
        raise RuntimeError("connection to postgres timed out")

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        payload_factory=_transient_factory,
    )

    ok = await pm.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="transient-k1",
    )
    # Command must NOT be ACKed (return False) so XAUTOCLAIM
    # redelivers it once the transient dependency recovers.
    assert ok is False, (
        "transient payload factory failures must return False so the "
        "command stays in the PEL for redelivery"
    )
    # No process handle was registered
    assert deployment.id not in pm.node_handle_cache

    # Row is marked failed with SPAWN_FAILED_TRANSIENT (not
    # PERMANENT) so the endpoint can distinguish retryable
    # failures from terminal ones.
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == deployment.id)
            )
        ).scalar_one()
        assert row.status == "failed"
        assert row.failure_kind == FailureKind.SPAWN_FAILED_TRANSIENT.value
        assert row.error_message is not None
        assert "connection to postgres timed out" in row.error_message


@pytest.mark.asyncio
async def test_spawn_without_payload_factory_uses_static_spawn_args(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """Backward compat: when ``payload_factory`` is ``None`` (test
    path), the spawn must use the static ``spawn_args`` tuple set at
    ``__init__`` time. This is the path every existing process
    manager test implicitly relies on — a regression here would
    break the 14 pre-existing tests too."""
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
        spawn_args=(1.0,),  # short sleep so the test cleans up fast
        # no payload_factory
    )

    try:
        ok = await pm.spawn(
            deployment_id=deployment.id,
            deployment_slug=deployment.deployment_slug,
            payload={},
            idempotency_key="static-k1",
        )
        assert ok is True
        # A live process exists with the expected handle
        assert deployment.id in pm.node_handle_cache
        proc = pm.node_handle_cache[deployment.id].proc
        assert proc.is_alive() or proc.exitcode == 0
    finally:
        for cached in list(pm.node_handle_cache.values()):
            with contextlib.suppress(Exception):
                cached.proc.terminate()
                cached.proc.join(timeout=2)


# ---------------------------------------------------------------------------
# FIX 2 (Codex P1 #2 / pr-toolkit P2): atomic operator-stop re-check INSIDE
# Phase A. Phase A is the SINGLE chokepoint every respawn passes through; a
# /stop landing in the gap between the pre-respawn ``_operator_stop_requested``
# gate and the slot reservation must still win against the respawn.
# ---------------------------------------------------------------------------


async def _seed_failed_node_row(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id: UUID,
    *,
    stop_requested_at: datetime | None,
    gateway_session_key: str = "sess-fix2",
) -> UUID:
    """Seed a terminal ``failed`` node-process row for a deployment, optionally
    carrying a durable operator-stop intent — the prior-crash row a respawn
    carries forward from. Returns the row id."""
    import socket

    async with session_factory() as session:
        now = datetime.now(UTC)
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=deployment_id,
            pid=None,
            host=socket.gethostname(),
            started_at=now,
            last_heartbeat_at=now,
            status="failed",
            failure_kind=FailureKind.UNKNOWN.value,
            gateway_session_key=gateway_session_key,
            consecutive_respawn_failures=0,
            auto_restart_paused=False,
            stop_requested_at=stop_requested_at,
        )
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_phase_a_respawn_aborts_when_operator_stop_intent_set(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 gap-window (reaper path): a respawn reaches Phase A AFTER an
    operator /stop has stamped ``stop_requested_at`` on the latest failed row.
    Phase A must RE-READ that durable intent in the slot-reservation transaction
    and ABORT — returning ``OPERATOR_STOPPED`` and inserting NO new active row.

    This is the gap the pre/post-backoff ``_operator_stop_requested`` gates in
    ``_attempt_auto_restart`` cannot close: they commit a SEPARATE transaction
    and hand off to ``spawn_with_outcome`` → ``_phase_a_reserve_slot`` in a new
    transaction; a /stop landing in that handoff window would otherwise let
    Phase A insert a fresh ``starting`` row for a stopped account.

    Falsification (verified by reverting the FIX-2 re-check): Phase A returns a
    real UUID + inserts a second (``starting``) node row — R2 trading for a
    stopped account.
    """
    from msai.live_supervisor.fleet_router import _PhaseAOutcome, _RestartCarry

    async with session_factory() as session:
        dep = await make_live_deployment(session, status="failed")
        await session.commit()

    # The prior crash row, already stamped with the operator-stop intent (the
    # /stop committed in the handoff gap).
    await _seed_failed_node_row(session_factory, dep.id, stop_requested_at=datetime.now(UTC))

    carry = _RestartCarry(
        prior_consecutive_respawn_failures=0,
        prior_last_restart_at=None,
        prior_auto_restart_paused=False,
        prior_auto_restart_pause_reason=None,
    )
    outcome = await process_manager._phase_a_reserve_slot(
        deployment_id=dep.id,
        deployment_slug=dep.deployment_slug,
        gateway_session_key="sess-fix2",
        restart_carry=carry,
    )

    assert outcome is _PhaseAOutcome.OPERATOR_STOPPED, (
        "Phase A must abort the respawn reservation when the latest node row "
        f"carries a durable operator-stop intent; got {outcome!r}"
    )

    # No new active row created — exactly the one seeded failed row exists.
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == dep.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, f"no fresh active row may be reserved; saw {len(rows)} rows"
        assert rows[0].status == "failed"
        # The deployment row is NOT reset to ``starting`` (the abort happens
        # before the terminal-status reset).
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep.id))
        ).scalar_one()
        assert dep_status == "failed"


@pytest.mark.asyncio
async def test_phase_a_respawn_proceeds_when_no_operator_stop_intent(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 no-false-suppression: a NORMAL respawn (latest failed row carries
    NO stop intent) must still reserve a fresh slot and reset the deployment to
    ``starting`` — the atomic re-check must not suppress a legitimate restart.
    """
    from msai.live_supervisor.fleet_router import _RestartCarry

    async with session_factory() as session:
        dep = await make_live_deployment(session, status="failed")
        await session.commit()

    await _seed_failed_node_row(session_factory, dep.id, stop_requested_at=None)

    carry = _RestartCarry(
        prior_consecutive_respawn_failures=0,
        prior_last_restart_at=None,
        prior_auto_restart_paused=False,
        prior_auto_restart_pause_reason=None,
    )
    outcome = await process_manager._phase_a_reserve_slot(
        deployment_id=dep.id,
        deployment_slug=dep.deployment_slug,
        gateway_session_key="sess-fix2",
        restart_carry=carry,
    )

    assert isinstance(outcome, UUID), (
        f"a no-stop-intent respawn must reserve a fresh slot; got {outcome!r}"
    )
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == dep.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2, (
            "a fresh starting row must be reserved alongside the prior failed row"
        )
        statuses = sorted(r.status for r in rows)
        assert statuses == ["failed", "starting"]
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep.id))
        ).scalar_one()
        assert dep_status == "starting"


@pytest.mark.asyncio
async def test_phase_a_fresh_start_not_suppressed_by_historical_stop_intent(
    process_manager: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 scope: the atomic re-check is RESPAWN-only (``restart_carry`` set).
    A fresh operator /start (no ``restart_carry``) for a deployment whose latest
    historical node row carries an old stop intent must NOT be suppressed — the
    start endpoint resets the deployment to ``starting`` before publishing, so a
    stale per-row intent on a since-superseded row is irrelevant to a brand-new
    start.
    """
    from msai.live_supervisor.fleet_router import _PhaseAOutcome

    async with session_factory() as session:
        # A fresh start always targets a non-terminal (``starting``) deployment.
        dep = await make_live_deployment(session, status="starting")
        await session.commit()

    # A since-superseded historical row carrying an old stop intent.
    await _seed_failed_node_row(session_factory, dep.id, stop_requested_at=datetime.now(UTC))

    # No restart_carry → this is a fresh /start reservation.
    outcome = await process_manager._phase_a_reserve_slot(
        deployment_id=dep.id,
        deployment_slug=dep.deployment_slug,
        gateway_session_key="sess-fix2",
    )

    assert outcome is not _PhaseAOutcome.OPERATOR_STOPPED, (
        "a fresh /start (no restart_carry) must NOT be suppressed by a historical "
        f"stop intent on a superseded row; got {outcome!r}"
    )
    assert isinstance(outcome, UUID), (
        f"a fresh start must reserve a slot (the latest row is ``failed`` but the "
        f"deployment is ``starting`` so the stale-START guard does not fire); got {outcome!r}"
    )
