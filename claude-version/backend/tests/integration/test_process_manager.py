"""Integration tests for ``ProcessManager`` (Phase 1 task 1.7).

Covers the INSERT-spawn-UPDATE pattern (decision #13, Codex v4 P0),
the halt-flag re-check (decision #16, Codex v4 P0), the reap loop
(decision #15, instant exit detection via the handle map), the stop
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
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.live_supervisor.process_manager import ProcessManager
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.failure_kind import FailureKind

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
    """Seed a LiveDeployment row so ProcessManager.spawn has something
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

        slug = uuid4().hex[:16]
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            strategy_code_hash="deadbeef" * 8,
            config={"fast": 10, "slow": 20},
            instruments=["AAPL.NASDAQ"],
            status="starting",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature="f" * 64,
            trader_id=f"MSAI-{slug}",
            strategy_id_full=f"EMACrossStrategy-{slug}",
            account_id="DU1234567",
            message_bus_stream=f"trader-MSAI-{slug}-stream",
            config_hash="cafebabe" * 8,
            instruments_signature="AAPL.NASDAQ",
        )
        session.add(dep)
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
) -> AsyncIterator[ProcessManager]:
    """ProcessManager wired with the test DB + Redis. The spawn_target
    is set to the in-file ``_sleep_target`` so tests that exercise the
    spawn path get a real live subprocess without launching Nautilus.
    """
    pm = ProcessManager(
        db=session_factory,
        redis=redis_client,
        spawn_target=_sleep_target,
    )
    yield pm
    # Clean up any live children the test left behind.
    for proc in list(pm.handles.values()):
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.join(timeout=2)


# ---------------------------------------------------------------------------
# Phase A: reserve-the-slot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_inserts_starting_row_with_pid(
    process_manager: ProcessManager,
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

    # Handle map now holds the mp.Process so stop() + reap_loop can find it.
    assert deployment.id in process_manager.handles


@pytest.mark.asyncio
async def test_spawn_idempotent_when_row_already_active(
    process_manager: ProcessManager,
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

    # Handle map stays empty (no new process was started).
    assert deployment.id not in process_manager.handles


@pytest.mark.asyncio
async def test_spawn_during_stop_returns_false_not_true(
    process_manager: ProcessManager,
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
    process_manager: ProcessManager,
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


# ---------------------------------------------------------------------------
# Phase B: halt-flag re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_blocked_by_halt_flag_marks_row_failed(
    process_manager: ProcessManager,
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
    assert deployment.id not in process_manager.handles


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
    pm = ProcessManager(
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
    proc = pm.handles[deployment.id]
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

    assert deployment.id not in pm.handles


@pytest.mark.asyncio
async def test_reap_loop_detects_nonzero_exit_marks_failed(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
    deployment: LiveDeployment,
) -> None:
    """A child that exits non-zero is marked failed with
    FailureKind.SPAWN_FAILED_PERMANENT and the real exit_code is
    recorded."""
    pm = ProcessManager(
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

    proc = pm.handles[deployment.id]
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
        assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
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
    pm = ProcessManager(
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

    proc = pm.handles[deployment.id]
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

    pm = ProcessManager(
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
    pm = ProcessManager(
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

    proc = pm.handles[deployment.id]
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
    pm = ProcessManager(
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

    proc = pm.handles[deployment.id]
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
    process_manager: ProcessManager,
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    """Stop flips the row to 'stopping', sends SIGTERM via the handle
    map, waits briefly for the child to exit, and returns True."""
    await process_manager.spawn(
        deployment_id=deployment.id,
        deployment_slug=deployment.deployment_slug,
        payload={},
        idempotency_key="k1",
    )
    proc = process_manager.handles[deployment.id]
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
    process_manager: ProcessManager,
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
    map. A subsequent stop() must read the pid from the DB row and
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
        # subprocess" case.
        async with session_factory() as session:
            row = LiveNodeProcess(
                id=uuid4(),
                deployment_id=deployment.id,
                pid=child.pid,
                host="preseed",
                started_at=datetime.now(UTC),
                last_heartbeat_at=datetime.now(UTC),
                status="running",
            )
            session.add(row)
            await session.commit()

        # Fresh ProcessManager with an EMPTY handle map (supervisor
        # just restarted — it doesn't know about this child yet).
        pm = ProcessManager(
            db=session_factory,
            redis=redis_client,
            spawn_target=_sleep_target,
        )
        assert not pm.handles

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
