"""Integration tests for the US-2 auto-restart against REAL Postgres rows.

The unit tests in ``tests/unit/live_supervisor/test_auto_restart.py`` /
``test_startup_rescan.py`` stub the DB-loading seams (``_load_restart_context``
/ ``_load_rescan_candidates``) and the spawn, so they cannot catch the
cross-generation counter read, the Phase-A ``NOT_STARTABLE`` interaction, or
the stale-active re-scan query (prior-review P2). These tests drive the REAL
``FleetRouter`` against a dedicated Postgres testcontainer + Redis, with a
trivial fast-exit spawn target, and assert the row state the production paths
actually produce:

1. **Terminal-deployment reset (prior-review P0).** When a node crashes, its
   own ``_mark_terminal`` flips the deployment to ``failed`` BEFORE the reaper
   runs. The respawn must RESET that terminal deployment back to ``starting``
   (not refuse it as a stale START) and create a fresh ``starting`` node row.

2. **Counter carry-forward + ceiling trip (prior-review P1/P2).** The
   crash-loop counter is keyed to the logical deployment: it must survive the
   per-spawn row recreate (each respawn INSERTs a new row), climb across
   generations, and trip ``auto_restart_paused`` at the ceiling — after which
   no further respawn is issued.

3. **Stale-active re-scan (prior-review P1).** A node that died while the
   supervisor was DOWN keeps a ``running`` row with a STALE heartbeat (the
   exit was never observed → never flipped to ``failed``). The startup
   re-scan must pick it up, flip it to ``failed``, and respawn it.

4. **Healthy-reconcile reset (prior-review P1).** ``_mark_running`` (the node
   reached ``is_running`` + reconciled) clears the streak via the equivalent
   of ``record_success``, so an independent later crash starts a fresh window.

SAFETY: dedicated Postgres + Redis testcontainers per module.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select

from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.live_supervisor.fleet_router import FleetRouter
from msai.live_supervisor.restart_policy import RestartPolicy
from msai.models import Base, LiveDeployment, LiveNodeProcess
from msai.services.live.failure_kind import FailureKind
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from uuid import UUID

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_ACCOUNT_ID = "DU1234567"


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
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
    # Start clean — no halt latches set.
    with contextlib.suppress(Exception):
        await client.delete(fleet_halt_key(), account_halt_key(_ACCOUNT_ID))
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.delete(fleet_halt_key(), account_halt_key(_ACCOUNT_ID))
        await client.aclose()


def _noop_target() -> None:
    """Spawn target that returns immediately. The reap loop is NOT run in
    these tests — we drive ``_maybe_auto_restart`` / ``rescan_for_restart``
    directly and inspect the resulting rows, so the child just needs to be
    picklable and start cleanly."""
    return None


@pytest_asyncio.fixture
async def fleet_router(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> AsyncIterator[FleetRouter]:
    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=RestartPolicy(),
        # Tight stale window so the stale-active re-scan test doesn't have to
        # wait 30s — a heartbeat 5s old is "dead" here.
        rescan_stale_seconds=5,
    )
    yield pm
    for cached in list(pm.node_handle_cache.values()):
        with contextlib.suppress(Exception):
            cached.proc.terminate()
            cached.proc.join(timeout=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_crashed_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    deployment_status: str = "failed",
    node_status: str = "failed",
    consecutive: int = 0,
    last_restart_at: datetime | None = None,
    auto_restart_paused: bool = False,
    last_heartbeat_at: datetime | None = None,
    stop_requested_at: datetime | None = None,
    restart_dispatched_at: datetime | None = None,
    host: str | None = None,
    gateway_session_key: str = "sess-1",
    account_id: str = _ACCOUNT_ID,
    ib_login_key: str = "msai-paper-primary",
) -> tuple[UUID, UUID]:
    """Seed a deployment + a single node-process row in the given state.

    Returns ``(deployment_id, node_row_id)``. Models the post-crash row state
    a real reaper / heartbeat-monitor leaves behind.

    ``host`` defaults to ``socket.gethostname()`` (the same-host case). Pass an
    explicit value to model a row left behind by a DIFFERENT (now-dead)
    supervisor container — the re-scan must recover those too (the Docker
    container-recreate gives the returning supervisor a fresh hostname while
    the orphan rows carry the old one).
    """
    import socket

    async with session_factory() as session:
        dep = await make_live_deployment(
            session,
            account_id=account_id,
            ib_login_key=ib_login_key,
            status=deployment_status,
        )
        now = datetime.now(UTC)
        row = LiveNodeProcess(
            deployment_id=dep.id,
            pid=None,
            host=host if host is not None else socket.gethostname(),
            started_at=now,
            last_heartbeat_at=last_heartbeat_at or now,
            status=node_status,
            gateway_session_key=gateway_session_key,
            consecutive_respawn_failures=consecutive,
            last_restart_at=last_restart_at,
            auto_restart_paused=auto_restart_paused,
            stop_requested_at=stop_requested_at,
            restart_dispatched_at=restart_dispatched_at,
        )
        session.add(row)
        await session.commit()
        return dep.id, row.id


async def _latest_node_row(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> LiveNodeProcess:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(LiveNodeProcess)
                .where(LiveNodeProcess.deployment_id == deployment_id)
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
        ).scalar_one()
        return row


async def _node_row_count(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> int:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess.id).where(LiveNodeProcess.deployment_id == deployment_id)
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def _deployment_status(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> str:
    async with session_factory() as session:
        return (
            await session.execute(
                select(LiveDeployment.status).where(LiveDeployment.id == deployment_id)
            )
        ).scalar_one()


async def _deployment_slug(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> str:
    async with session_factory() as session:
        return (
            await session.execute(
                select(LiveDeployment.deployment_slug).where(LiveDeployment.id == deployment_id)
            )
        ).scalar_one()


async def _set_latest_failure_kind(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id: UUID,
    failure_kind: str,
) -> None:
    """Stamp ``failure_kind`` on the latest node-process row (models the kind
    the spawn path wrote when the row was flipped to ``failed``)."""
    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(LiveNodeProcess)
                .where(LiveNodeProcess.deployment_id == deployment_id)
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
        ).scalar_one()
        row.failure_kind = failure_kind


async def _mark_latest_failed(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> None:
    """Simulate the next crash: flip the latest (running) node row + the
    deployment back to ``failed``, the way ``_mark_terminal`` would."""
    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(LiveNodeProcess)
                .where(LiveNodeProcess.deployment_id == deployment_id)
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
        ).scalar_one()
        row.status = "failed"
        dep = await session.get(LiveDeployment, deployment_id)
        if dep is not None:
            dep.status = "failed"


# ---------------------------------------------------------------------------
# 1. Terminal-deployment reset (prior-review P0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_restart_resets_terminal_deployment_and_spawns_new_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The headline P0: a crashed node whose deployment is already ``failed``
    (its own ``_mark_terminal`` flipped it) is GENUINELY respawned — a new
    ``starting`` node row is created and the deployment row is reset to
    ``starting`` (NOT refused as a stale START → NOT_STARTABLE no-op)."""
    dep_id, _row_id = await _seed_crashed_deployment(session_factory)

    restarted = await fleet_router._maybe_auto_restart(dep_id)

    assert restarted is True, "auto-restart must genuinely respawn the failed node"
    # A brand-new node-process row exists (the respawn).
    assert await _node_row_count(session_factory, dep_id) == 2
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.status == "starting", "the respawned node row is reserved as starting"
    # The deployment row was reset off ``failed`` so the new node can run.
    assert await _deployment_status(session_factory, dep_id) == "starting"
    # This was the FIRST restart attempt — counted on the NEW row.
    assert latest.consecutive_respawn_failures == 1


# ---------------------------------------------------------------------------
# 2. Counter carry-forward + ceiling trip (prior-review P1/P2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counter_carries_forward_across_respawns_and_trips_ceiling(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A node that crash-loops is respawned with the counter CARRIED across
    the per-spawn row recreate, climbing each generation until the ceiling
    (``MAX_RESTART_ATTEMPTS=5``) trips ``auto_restart_paused`` — after which
    no further respawn is issued (the crash-loop brake engages).

    Without the carry the new row would default to 0 each time and the brake
    would never engage (prior-review P0/P1 — respawned forever, zero backoff).
    Backoff is neutralised here by pushing ``last_restart_at`` far enough back
    that ``decide`` always returns RESTART (the carry/ceiling logic is what we
    pin, not the wall-clock wait).
    """
    from msai.live_supervisor.restart_policy import MAX_RESTART_ATTEMPTS

    dep_id, _row_id = await _seed_crashed_deployment(session_factory)

    counters_seen: list[int] = []
    for generation in range(MAX_RESTART_ATTEMPTS + 2):
        # Push the prior attempt's timestamp back so the backoff has elapsed
        # — we want every eligible generation to RESTART immediately.
        async with session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(LiveNodeProcess.deployment_id == dep_id)
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            if row.last_restart_at is not None:
                row.last_restart_at = datetime.now(UTC) - timedelta(seconds=600)

        restarted = await fleet_router._maybe_auto_restart(dep_id)
        latest = await _latest_node_row(session_factory, dep_id)
        counters_seen.append(latest.consecutive_respawn_failures)

        if latest.auto_restart_paused:
            # Ceiling tripped — the next call must NOT respawn.
            assert restarted is True, (
                f"generation {generation}: the attempt that REACHED the "
                "ceiling still ran (it's the 5th), tripping the latch"
            )
            break

        assert restarted is True, f"generation {generation} should still restart"
        # Simulate the respawned node crashing again before the next loop.
        await _mark_latest_failed(session_factory, dep_id)

    # The counter climbed monotonically across generations (carry-forward),
    # reaching the ceiling.
    assert counters_seen == list(range(1, MAX_RESTART_ATTEMPTS + 1)), (
        f"counter must climb 1..{MAX_RESTART_ATTEMPTS} across respawns "
        f"(carry-forward); saw {counters_seen}"
    )
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.auto_restart_paused is True
    assert latest.consecutive_respawn_failures == MAX_RESTART_ATTEMPTS

    # The brake is engaged: another crash does NOT respawn.
    await _mark_latest_failed(session_factory, dep_id)
    rows_before = await _node_row_count(session_factory, dep_id)
    restarted = await fleet_router._maybe_auto_restart(dep_id)
    assert restarted is False, "a PAUSED deployment must not respawn"
    assert await _node_row_count(session_factory, dep_id) == rows_before, (
        "no new node row when the ceiling has tripped"
    )


# ---------------------------------------------------------------------------
# 3. Stale-active re-scan (prior-review P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rescan_restarts_stale_active_row_supervisor_was_down(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The supervisor-was-DOWN scenario the re-scan exists for: the node died
    while the supervisor was down, so its exit was never observed — the latest
    node row is still ``running`` (NOT ``failed``) with a STALE heartbeat, and
    the deployment row is still ``running``. The re-scan must (a) flip the
    stale row to ``failed``, and (b) genuinely respawn it. Previously the
    re-scan only matched ``failed`` rows → this candidate was never selected
    (prior-review P1).
    """
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=stale_hb,
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 1, "the stale-active node must be restarted by the re-scan"
    # The stale row was flipped to failed (it leaves the active set so the
    # respawn's Phase A doesn't see it as ALREADY_ACTIVE).
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed"
    # A fresh node row is reserved + the deployment is back to starting.
    assert await _node_row_count(session_factory, dep_id) == 2
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.status == "starting"
    assert latest.consecutive_respawn_failures == 1
    assert await _deployment_status(session_factory, dep_id) == "starting"


@pytest.mark.asyncio
async def test_rescan_recovers_orphan_rows_from_a_dead_container_other_host(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex P1 — the re-scan must be host-AGNOSTIC.

    In the Docker Compose deployment the ``live-supervisor`` container has no
    stable hostname: a container recreate gives the RETURNING supervisor a
    fresh ``socket.gethostname()`` while the orphaned ``live_node_processes``
    rows still carry the OLD (now-dead) container's hostname. The deploy
    contract guarantees a SINGLE supervisor (F4 excludes the broker profile
    from routine deploys; F5 refuses a deploy while any live deployment is
    active), and after a container recreate ALL prior node processes are dead
    (the old container is gone) — so EVERY active/stale/failed row is an orphan
    THIS supervisor must recover.

    Before the fix the ``_load_rescan_candidates`` host predicate EXCLUDED
    exactly these cross-host orphans → the account stayed failed + flat and
    unmonitored until manual intervention. This test seeds BOTH candidate
    classes (a stale-active row AND a terminal-failed row) under a dead
    container's hostname and asserts BOTH are recovered (restarted).
    """
    other_host = "old-dead-container-hostname"

    # Class-2 candidate: a node that died while the supervisor was down — its
    # row is still ``running`` with a STALE heartbeat, under the OLD hostname.
    # Distinct gateway sessions so the two respawns don't collide on the
    # same-gateway CONCURRENT_STARTUP guard (in the real cross-host scenario
    # the orphans belong to different accounts on different IB logins).
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    stale_dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=stale_hb,
        host=other_host,
        gateway_session_key="sess-stale",
    )
    # Class-1 candidate: a terminal ``failed`` row whose exit WAS observed
    # before the old container died — also under the OLD hostname.
    failed_dep_id, _failed_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        host=other_host,
        gateway_session_key="sess-failed",
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 2, (
        "both cross-host orphans (stale-active + terminal-failed) must be "
        "recovered by the host-agnostic re-scan after a container recreate"
    )

    # The stale-active row was flipped to failed and a fresh node row spawned.
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed"
    assert await _node_row_count(session_factory, stale_dep_id) == 2
    stale_latest = await _latest_node_row(session_factory, stale_dep_id)
    assert stale_latest.status == "starting"
    assert await _deployment_status(session_factory, stale_dep_id) == "starting"

    # The terminal-failed orphan was respawned too.
    assert await _node_row_count(session_factory, failed_dep_id) == 2
    failed_latest = await _latest_node_row(session_factory, failed_dep_id)
    assert failed_latest.status == "starting"
    assert await _deployment_status(session_factory, failed_dep_id) == "starting"


@pytest.mark.asyncio
async def test_rescan_ignores_stop_requested_failed_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #3 F5 carried to the startup re-scan: a node the operator
    ``/stop``'d while the supervisor was down (``stop_requested_at`` set, then the
    graceful shutdown crashed → terminal ``failed``) must NOT be resurrected by
    the re-scan. The re-scan is a sibling of the reaper and must honour the same
    durable operator-stop intent — otherwise a supervisor restart would silently
    re-trade an account the operator deliberately stopped (real-money)."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        stop_requested_at=datetime.now(UTC),
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a /stop'd (stop_requested_at) failed row must not be rescanned"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_flips_stop_requested_stale_active_row_but_does_not_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stale-active half of the F5 invariant, REVISED per council 2026-05-31
    to FLIP-BUT-SUPPRESS: a node ``/stop``'d while the supervisor was down —
    intent set, exit never observed, so the row is still ``running`` with a STALE
    heartbeat — is FLIPPED to ``failed`` by the rescan Step-1 cleanup (so it
    leaves the active unique-index set; a dead node must not masquerade as
    ``running`` on the real-money dashboard) but is NOT respawned by Step-2 (the
    durable operator-stop intent is honored — never resurrected).

    Previous contract (now wrong): the row stayed ``running`` because the Step-1
    cleanup EXCLUDED ``stop_requested_at`` rows, leaving a dead node stuck-active
    forever. The don't-resurrect INTENT is preserved (Step-2 still suppresses);
    only the terminal state changes."""
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=stale_hb,
        stop_requested_at=datetime.now(UTC),
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a /stop'd stale-active row must NOT be respawned (Step-2 suppresses)"
    assert await _node_row_count(session_factory, dep_id) == 1
    # The stale row is FLIPPED to failed by the Step-1 cleanup (leaves the active
    # set) — but Step-2 does NOT resurrect it (stop_requested_at intent honored).
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed", (
            "the Step-1 cleanup must flip a dead stop_requested stale-active row to "
            "'failed' so a dead node never masquerades as 'running' on the dashboard"
        )


@pytest.mark.asyncio
async def test_rescan_ignores_fresh_active_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A healthy ``running`` node with a FRESH heartbeat (the supervisor
    restarted but the child survived) must NOT be touched by the re-scan —
    only genuinely-stale rows are candidates."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC),  # fresh
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a fresh running node is not a restart candidate"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "running"


# ---------------------------------------------------------------------------
# 4. Halt gate against the REAL Redis latch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fleet_halt_latch_suppresses_real_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """With the REAL fleet halt latch set (``/kill-all``), the crashed node is
    NOT respawned — no new node row, deployment stays ``failed``."""
    dep_id, _row_id = await _seed_crashed_deployment(session_factory)
    await redis_client.set(fleet_halt_key(), "1")

    restarted = await fleet_router._maybe_auto_restart(dep_id)

    assert restarted is False
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_operator_stop_intent_suppresses_real_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FINDING 1 (P1), test (b): the restart path re-reads ``stop_requested_at``
    before respawn and SKIPS when it is set.

    A crashed node's latest row carries a durable operator-stop intent
    (``stop_requested_at`` set — stamped by /stop even though no halt latch is
    set). ``_attempt_auto_restart`` must read that intent and abort the respawn:
    no new node row, deployment stays ``failed``. Falsification (pre-fix, no
    stop-intent gate): the node IS respawned (a 2nd row, deployment back to
    ``starting``)."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        stop_requested_at=datetime.now(UTC),
        # A pending reaper dispatch is what makes this a real "fight the
        # operator" scenario, but the gate must suppress regardless.
        restart_dispatched_at=datetime.now(UTC),
    )

    restarted = await fleet_router._maybe_auto_restart(dep_id)

    assert restarted is False, "an operator-stopped deployment must NOT be respawned"
    assert await _node_row_count(session_factory, dep_id) == 1, (
        "no fresh node row may be created when stop_requested_at is set"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed", (
        "the deployment must stay failed — never resurrected to starting"
    )


@pytest.mark.asyncio
async def test_inflight_restart_task_aborts_when_stop_intent_stamped_during_backoff(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FINDING 1 (P1), test (a): an in-flight ``_run_restart_task`` that is
    backing off ABORTS when an operator /stop stamps ``stop_requested_at`` on the
    failed-with-pending-restart row DURING the backoff — no respawn, no new
    active row.

    Models the never-fight-an-operator-stop race: the reaper classified the
    crash, dispatched the auto-restart (``restart_dispatched_at`` set), and the
    detached task is backing off. The operator then issues a plain /stop (which
    sets NO halt latch) — its handler stamps the durable ``stop_requested_at`` on
    that failed row. The task's post-backoff stop-intent re-check must observe it
    and abort.

    Falsification (pre-fix, no post-backoff stop re-check): the backoff elapses
    and the node is respawned — a 2nd node row appears and the deployment goes
    back to ``starting`` even though the operator received a stop.
    """
    # A pending-restart failed row in BACKOFF state (consecutive=1 +
    # last_restart_at set so RestartPolicy.decide returns BACKOFF, not RESTART).
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        consecutive=1,
        last_restart_at=datetime.now(UTC),
        restart_dispatched_at=datetime.now(UTC),
    )

    backoff_entered = asyncio.Event()
    release_backoff = asyncio.Event()

    async def _fake_backoff(_delay_s: float, _account_id: str | None) -> bool:
        # Model the operator /stop landing DURING the (real) backoff window:
        # signal we're "in the backoff", wait for the test to stamp the intent,
        # then return True ("backoff elapsed, proceed to respawn") so the
        # post-backoff stop-intent re-check is what must suppress the respawn.
        backoff_entered.set()
        await release_backoff.wait()
        return True

    fleet_router._cancellable_backoff = _fake_backoff  # type: ignore[assignment]

    # Schedule the REAL restart task (drives the real _attempt_auto_restart).
    fleet_router._schedule_restart_task(dep_id, _ACCOUNT_ID)
    await asyncio.wait_for(backoff_entered.wait(), timeout=2.0)

    # Operator /stop stamps the durable stop intent on the failed-pending row
    # WHILE the task is backing off (the API handler's failed-with-pending-
    # restart branch does exactly this).
    async with session_factory() as session, session.begin():
        failed_row = (
            await session.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == dep_id,
                    LiveNodeProcess.status == "failed",
                    LiveNodeProcess.restart_dispatched_at.is_not(None),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one()
        failed_row.stop_requested_at = datetime.now(UTC)

    # Let the backoff complete → the task proceeds to the post-backoff
    # stop-intent re-check, which must abort the respawn.
    release_backoff.set()
    await asyncio.wait_for(fleet_router._await_restart_tasks_for_test(), timeout=3.0)

    assert await _node_row_count(session_factory, dep_id) == 1, (
        "the in-flight restart must ABORT when stop_requested_at is stamped during "
        "the backoff — no respawn row"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed", (
        "the deployment must stay failed — the operator stop wins over the pending restart"
    )


# ---------------------------------------------------------------------------
# 5. Healthy-reconcile reset via _mark_running (prior-review P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_running_resets_restart_counter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``_mark_running`` (node reached ``is_running`` + reconciled) clears the
    consecutive-failure streak + the pause latch — the ``record_success``
    behaviour the plan/model docstring mandate, which was previously dead code
    (prior-review P1). A node that recovers healthily starts a fresh window.
    """
    from msai.services.nautilus.trading_node_subprocess import _mark_running

    # A node row mid-recovery that carried a non-zero streak forward.
    dep_id, row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="ready",
        consecutive=3,
        last_restart_at=datetime.now(UTC),
        auto_restart_paused=False,
    )

    await _mark_running(session_factory, row_id)

    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        assert row.status == "running"
        # Healthy reconcile reset the streak + pause latch.
        assert row.consecutive_respawn_failures == 0
        assert row.auto_restart_paused is False
        assert row.auto_restart_pause_reason is None
    # And the deployment was forward-synced to running.
    assert await _deployment_status(session_factory, dep_id) == "running"


# ---------------------------------------------------------------------------
# 6. Transient respawn must NOT strand at 'starting' (prior-review P2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_respawn_refails_and_stays_rescannable(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Prior-review P2: an auto-restart respawn that hits a TRANSIENT failure in
    Phase B (after Phase A already reset the deployment to ``starting``) must
    end ``failed`` — NOT stranded at ``starting`` with no live node — so the
    next rescan / heartbeat path can retry it.

    The reaper-driven respawn calls ``spawn_with_outcome`` directly with no PEL
    entry behind it, so the transient ``(False, False)`` outcome has no caller
    that re-drives it. We reproduce the exact double-blip strand the review
    describes: a transient payload-factory failure (``RuntimeError`` — NOT in
    the permanent ``(ValueError, ImportError, …)`` tuple, so it routes through
    the transient branch) AND the in-spawn best-effort ``_mark_failed`` cleanup
    ALSO failing (the most likely transient is a Postgres outage — the same
    outage that blocks ``_mark_failed``). With ``_mark_failed`` unable to flip
    the row, Phase A's ``starting`` reset is all that landed — so WITHOUT the
    ``_refail_stranded_restart`` backstop the deployment lingers at ``starting``
    with no live node. The backstop (which runs on its own fresh session, after
    the spawn returns) re-fails it so it stays rescannable.
    """

    async def _transient_factory(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise RuntimeError("simulated transient payload-factory failure (e.g. Postgres blip)")

    fleet_router._payload_factory = _transient_factory  # type: ignore[assignment]

    # Make the in-spawn best-effort cleanup ALSO fail (the second blip), so the
    # node row genuinely strands at ``starting`` and only the post-spawn
    # ``_refail_stranded_restart`` backstop can recover it. ``_mark_failed`` is
    # restored before the follow-up restart below.
    original_mark_failed = fleet_router._mark_failed

    async def _mark_failed_blip(**_kwargs: object) -> None:
        raise RuntimeError("simulated Postgres blip during in-spawn _mark_failed cleanup")

    fleet_router._mark_failed = _mark_failed_blip  # type: ignore[assignment]

    dep_id, _row_id = await _seed_crashed_deployment(session_factory)

    restarted = await fleet_router._maybe_auto_restart(dep_id)

    # No process started → not reported as a restart.
    assert restarted is False
    # A new node row WAS reserved by Phase A (the respawn attempt), but the
    # post-spawn backstop re-failed it — it is NOT stuck at ``starting``.
    assert await _node_row_count(session_factory, dep_id) == 2
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.status == "failed", (
        "the transient respawn's node row must be re-failed by the backstop, not left 'starting'"
    )
    # The deployment is re-failed — NOT left at ``starting`` (the strand bug).
    assert await _deployment_status(session_factory, dep_id) == "failed", (
        "deployment must be rescannable (failed), not stranded at 'starting'"
    )

    # And it is genuinely rescannable: with both blips cleared, a follow-up
    # restart attempt now succeeds end-to-end.
    fleet_router._payload_factory = None  # type: ignore[assignment]
    fleet_router._mark_failed = original_mark_failed  # type: ignore[assignment]
    restarted_again = await fleet_router._maybe_auto_restart(dep_id)
    assert restarted_again is True
    assert await _deployment_status(session_factory, dep_id) == "starting"


# ---------------------------------------------------------------------------
# 6b. Transient spawn-failure → redelivered START must re-spawn (Codex P2)
# ---------------------------------------------------------------------------
#
# When the payload factory (or a Redis/DB lookup) raises a TRANSIENT error,
# the spawn path marks the row + parent deployment ``failed`` with
# ``SPAWN_FAILED_TRANSIENT`` and returns ``False`` so the Redis command entry
# is REDELIVERED (the no-ACK retry path). On redelivery there's no active
# process row, so the terminal-deployment stale-START guard fires — and before
# the fix it ACK-DROPPED the START as stale, silently killing the retry the
# no-ACK path promised. The guard must EXEMPT transient-failure rows.


@pytest.mark.asyncio
async def test_redelivered_start_after_transient_failure_respawns(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A START redelivered for a deployment whose LATEST node row is
    ``SPAWN_FAILED_TRANSIENT`` (a Postgres/Redis/import blip during the prior
    attempt) must PROCEED to re-spawn — NOT be ACK-dropped as a stale START.

    Drives the real ``spawn_with_outcome`` with ``restart_carry=None`` (exactly
    how the per-account consumer re-drives a redelivered command). Asserts a
    process genuinely started: a fresh ``starting`` node row appears and the
    deployment is reset off ``failed``.
    """
    # The prior transient attempt left the deployment ``failed`` with a
    # ``failed`` node row carrying the transient kind.
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(
        session_factory, dep_id, FailureKind.SPAWN_FAILED_TRANSIENT.value
    )
    slug = await _deployment_slug(session_factory, dep_id)

    ack, process_started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="redelivered-1",
        restart_carry=None,
    )

    assert ack is True
    assert process_started is True, (
        "a START redelivered after a SPAWN_FAILED_TRANSIENT must re-spawn, "
        "not be ACK-dropped as a stale START"
    )
    # A fresh node row was reserved + the deployment reset off ``failed``.
    assert await _node_row_count(session_factory, dep_id) == 2
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.status in ("starting", "failed"), (
        "the re-spawn reserved a fresh row (the noop child may have exited "
        "before assertion — either way it is NOT the old terminal row)"
    )
    assert latest.id != _row_id


@pytest.mark.asyncio
async def test_redelivered_start_after_permanent_failure_is_dropped(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The negative half: a START redelivered for a deployment whose LATEST node
    row is ``SPAWN_FAILED_PERMANENT`` (an operator-config bug retrying won't
    fix) is STILL ACK-dropped as a stale START — no re-spawn, no churn.

    Without this the transient exemption would over-reach and resurrect nodes
    the operator must fix first."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(
        session_factory, dep_id, FailureKind.SPAWN_FAILED_PERMANENT.value
    )
    slug = await _deployment_slug(session_factory, dep_id)

    ack, process_started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="redelivered-2",
        restart_carry=None,
    )

    # ACK-and-drop: the command leaves the PEL, no process started, no new row.
    assert ack is True
    assert process_started is False, "a permanent-failure stale START must NOT re-spawn"
    assert await _node_row_count(session_factory, dep_id) == 1, (
        "no fresh node row — the stale START was dropped, not respawned"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed"


# ---------------------------------------------------------------------------
# 7. Council #3 — COMMON-CRASH reaper fix (the REAL-MONEY P1)
# ---------------------------------------------------------------------------
#
# These drive the REAL ``_on_child_exit`` (NOT ``_maybe_auto_restart`` directly)
# against real Postgres rows — the bug was in ``_on_child_exit``'s SELECT, which
# matched only the ACTIVE status set and so missed the already-``failed`` row the
# subprocess wrote LAST in its ``finally`` ("Terminal write LAST"). Before the
# fix the common crash never reached the restart dispatch at runtime.


@pytest.mark.asyncio
async def test_on_child_exit_restarts_already_failed_common_crash(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #3 case 1 (the reproduced P1): the node-process row is ALREADY
    ``status='failed'`` (the subprocess wrote it last). ``_on_child_exit(exit
    _code=1)`` must STILL auto-restart — a 2nd node row appears and the
    deployment is reset to ``starting``. Before the fix the active-set-only
    SELECT returned ``None`` here → no respawn."""
    dep_id, failed_row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )

    await fleet_router._on_child_exit(dep_id, exit_code=1)
    # F1: ``_on_child_exit`` now SCHEDULES the restart as a detached per-account
    # task (so the reaper never blocks on the backoff). Drain it so the
    # respawn-completed assertions below are deterministic.
    await fleet_router._await_restart_tasks_for_test()

    # The headline assertion: a genuine respawn happened off the terminal row.
    assert await _node_row_count(session_factory, dep_id) == 2, (
        "common crash (row already 'failed') must STILL produce a respawn"
    )
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.id != failed_row_id
    assert latest.status == "starting"
    assert latest.consecutive_respawn_failures == 1
    assert await _deployment_status(session_factory, dep_id) == "starting"


@pytest.mark.asyncio
async def test_on_child_exit_does_not_restart_clean_stopped_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #3 case 2: a graceful ``/stop`` whose subprocess wrote ``stopped``
    (exit 0) LAST is NOT restarted."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="stopped", node_status="stopped"
    )

    await fleet_router._on_child_exit(dep_id, exit_code=0)

    assert await _node_row_count(session_factory, dep_id) == 1, "a clean stop must not respawn"
    assert await _deployment_status(session_factory, dep_id) == "stopped"


@pytest.mark.asyncio
async def test_on_child_exit_stop_then_crash_not_restarted(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #3 case 3 (F5): ``/stop``-then-crash. ``stop_requested_at`` was set
    (durable operator-stop intent), then the graceful shutdown crashed (non-zero
    exit → terminal ``failed``). The reaper must NOT resurrect it even though the
    halt gate is unset and the exit is non-zero — the durable intent suppresses
    the restart through the subprocess's terminal write."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        stop_requested_at=datetime.now(UTC),
    )

    await fleet_router._on_child_exit(dep_id, exit_code=1)

    assert await _node_row_count(session_factory, dep_id) == 1, (
        "a /stop whose shutdown crashed must NOT be resurrected (stop_requested_at)"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_on_child_exit_fleet_halt_suppresses_terminal_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """Council #3 case 5: a fleet (``/kill-all``) halt still suppresses the
    now-terminal-row path. The row is already ``failed`` (common crash) but the
    fleet latch is set, so no respawn."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await redis_client.set(fleet_halt_key(), "1")
    try:
        await fleet_router._on_child_exit(dep_id, exit_code=1)
    finally:
        await redis_client.delete(fleet_halt_key())

    assert await _node_row_count(session_factory, dep_id) == 1, (
        "a fleet kill-all must suppress the terminal-row restart path"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_on_child_exit_account_halt_suppresses_terminal_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """Council #3 case 5 (account half): a ``/drain`` (account latch) also
    suppresses the terminal-row restart path."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await redis_client.set(account_halt_key(_ACCOUNT_ID), "1")
    try:
        await fleet_router._on_child_exit(dep_id, exit_code=1)
    finally:
        await redis_client.delete(account_halt_key(_ACCOUNT_ID))

    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_on_child_exit_idempotency_sentinel_blocks_second_dispatch(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #3 case 6: TWO reaper passes that BOTH see the SAME ``failed``
    terminal row as latest (a redelivered reap / reap_once race, BEFORE any
    respawn lands a new row) must dispatch AT MOST once. The reaper stamps
    ``restart_dispatched_at`` under its ``FOR UPDATE`` lock BEFORE the dispatch;
    the second pass sees the sentinel on the (still-latest) terminal row and
    skips — no double-dispatch, no double-count.

    We stub the dispatch target (``_attempt_auto_restart`` — the seam the
    detached restart task calls, council #4 OPT C Part 2) to a no-op COUNTER so
    no new row lands between the passes (in production the new ``starting`` row
    would become latest after a successful respawn, ending this exact-same-row
    window). This isolates the DB-level classify+sentinel decision the council
    condition pins.
    """
    from msai.live_supervisor.fleet_router import _RestartOutcome

    dispatches: list[UUID] = []

    async def _count_only(dep: UUID) -> _RestartOutcome:
        dispatches.append(dep)
        return _RestartOutcome.RESTARTED

    fleet_router._attempt_auto_restart = _count_only  # type: ignore[assignment]

    dep_id, failed_row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )

    # First reap → dispatch (via the detached F1 task) + sentinel stamped on the
    # terminal row (the sentinel write happens SYNCHRONOUSLY in _on_child_exit,
    # under the FOR UPDATE lock, before the task is scheduled).
    await fleet_router._on_child_exit(dep_id, exit_code=1)
    await fleet_router._await_restart_tasks_for_test()
    assert dispatches == [dep_id]
    async with session_factory() as session:
        terminal = await session.get(LiveNodeProcess, failed_row_id)
        assert terminal is not None
        assert terminal.restart_dispatched_at is not None, (
            "the reaper must stamp the idempotency sentinel before dispatch"
        )

    # Second reap sees the SAME terminal row with the sentinel set → no 2nd dispatch.
    await fleet_router._on_child_exit(dep_id, exit_code=1)
    await fleet_router._await_restart_tasks_for_test()
    assert dispatches == [dep_id], (
        "the idempotency sentinel must block a second dispatch on the same terminal row"
    )
    # And no extra node row was created by the reaper itself.
    assert await _node_row_count(session_factory, dep_id) == 1


@pytest.mark.asyncio
async def test_stop_sets_intent_before_signal(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """Council #3 case 4 (set-intent-BEFORE-signal): ``FleetRouter.stop`` must set
    ``stop_requested_at`` (committed, under a ``FOR UPDATE`` row lock) BEFORE it
    sends SIGTERM, so a coincident self-crash whose reaper classifies the row
    under its own ``FOR UPDATE`` either sees the committed intent or blocks until
    ``/stop`` commits it. We assert the intent is durably committed after stop
    (the row has no live pid, so the signal step is a documented no-op here)."""
    import socket

    dep_id, row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
    )
    # Make sure the seeded row is same-host (passes the cross-host guard) with
    # no pid so stop() reaches the intent write + the no-pid signal no-op.
    async with session_factory() as session, session.begin():
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        row.host = socket.gethostname()
        row.pid = None

    pm = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=RestartPolicy(),
    )

    ok = await pm.stop(dep_id, reason="user")
    assert ok is True

    async with session_factory() as session:
        reloaded = await session.get(LiveNodeProcess, row_id)
        assert reloaded is not None
        assert reloaded.status == "stopping"
        assert reloaded.stop_requested_at is not None, (
            "stop() must durably set stop_requested_at before signalling"
        )

    # Now the durable intent suppresses a subsequent self-crash respawn even
    # though the row is non-zero-exit failed and no halt latch is set.
    async with session_factory() as session, session.begin():
        crashed = await session.get(LiveNodeProcess, row_id)
        assert crashed is not None
        crashed.status = "failed"  # the graceful shutdown then crashed
        dep = await session.get(LiveDeployment, dep_id)
        if dep is not None:
            dep.status = "failed"

    await pm._on_child_exit(dep_id, exit_code=1)
    assert await _node_row_count(session_factory, dep_id) == 1, (
        "the committed stop intent must suppress the coincident self-crash respawn"
    )


# ---------------------------------------------------------------------------
# Council #4 OPT C — periodic reconciling rescan invariants (Part 1)
#
# The periodic rescan re-uses ``rescan_for_restart`` / ``_maybe_auto_restart``
# unchanged; these tests pin that a REPEATED reconciliation honours every
# suppressor and never churns / stampedes, against real Postgres + Redis.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rescan_suppressed_by_account_halt_latch(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """Council #4 test 3 (account-halt half): with the REAL account-halt latch
    set (``/drain``), the periodic rescan must NOT respawn the failed deployment
    — the same fleet-OR-account fail-closed gate the reaper uses suppresses it."""
    dep_id, _row_id = await _seed_crashed_deployment(session_factory)
    await redis_client.set(account_halt_key(_ACCOUNT_ID), "1")
    try:
        restarted = await fleet_router.rescan_for_restart()
    finally:
        await redis_client.delete(account_halt_key(_ACCOUNT_ID))

    assert restarted == 0, "an account-drained deployment must not be rescan-restarted"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_suppressed_by_fleet_halt_latch(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """Council #4 test 3 (fleet-halt half): with the REAL fleet halt latch set
    (``/kill-all``), the periodic rescan must NOT respawn — even though the
    account-halt key is unset (the gate is fleet-OR-account)."""
    dep_id, _row_id = await _seed_crashed_deployment(session_factory)
    await redis_client.set(fleet_halt_key(), "1")

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a fleet kill-all must suppress rescan recovery"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_suppressed_by_auto_restart_paused(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #4 test 3 (paused half): a deployment whose ceiling already
    tripped ``auto_restart_paused`` is excluded from the rescan candidate query
    — repeated reconciliation never re-drives a paused deployment."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        auto_restart_paused=True,
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a paused deployment must not be rescan-restarted"
    assert await _node_row_count(session_factory, dep_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("node_status", ["running", "starting"])
async def test_rescan_flips_dead_paused_stale_active_row_but_does_not_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    node_status: str,
) -> None:
    """FIX 1 (P1): the rescan Step-1 cleanup of a DEAD stale-active row (flip to
    ``failed`` so it leaves the active unique-index set) MUST be UNCONDITIONAL on
    ``auto_restart_paused``.

    The scenario: a ceiling-tripping restart attempt died (SIGKILL/OOM, the
    subprocess never reached its own terminal write) while the supervisor /
    container was DOWN, so the row is still in an active status (``running`` or a
    Phase-A ``starting``) with a STALE heartbeat, and it carries
    ``auto_restart_paused=True`` (the latch the ceiling set). Before the fix the
    Step-1 flip carried an ``auto_restart_paused.is_(False)`` predicate, so this
    dead PAUSED row was NEVER flipped → it stayed stuck in the active set forever,
    blocking every future manual ``/start`` (partial unique index) and showing
    active on ``/live/status``.

    The invariant: CLEANUP (flip a dead row out of the active set) is
    unconditional; RESPAWN-eligibility (paused) is a separately-gated decision.
    So after the fix the dead PAUSED row is flipped to ``failed`` (leaves the
    active set; parent deployment synced to ``failed``), but Step-2 (which still
    gates ``auto_restart_paused.is_(False)``) does NOT respawn it.

    Falsification (pre-fix): with the predicate present the row stays
    ``node_status`` (active) and the deployment stays active — assert it is
    flipped instead.
    """
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status=node_status,
        node_status=node_status,
        last_heartbeat_at=stale_hb,
        auto_restart_paused=True,
        consecutive=5,
    )

    restarted = await fleet_router.rescan_for_restart()

    # NOT respawned — Step-2 still skips paused.
    assert restarted == 0, "a paused deployment must NOT be respawned by the rescan"
    assert await _node_row_count(session_factory, dep_id) == 1, (
        "no fresh node row — the paused deployment is cleaned, not restarted"
    )
    # CLEANED — the dead PAUSED stale-active row is flipped out of the active set.
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed", (
            "a DEAD paused stale-active row MUST be flipped to failed so it leaves "
            "the active unique-index set (cleanup is unconditional on pause)"
        )
        # The pause latch is preserved — this is a cleanup, not a reset.
        assert stale_row.auto_restart_paused is True
    # Parent deployment synced so /live/status reflects the terminal state.
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_suppressed_by_stop_requested_at(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #4 test 3 (stop-intent half): a deployment with durable
    ``stop_requested_at`` is excluded from the rescan candidate query — a
    repeated reconciliation never resurrects an operator-stopped node."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        stop_requested_at=datetime.now(UTC),
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "an operator-stopped deployment must not be rescan-restarted"
    assert await _node_row_count(session_factory, dep_id) == 1


@pytest.mark.asyncio
async def test_repeated_rescan_does_not_churn_a_paused_deployment(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #4 test 4: once the RestartPolicy ceiling has tripped
    ``auto_restart_paused``, REPEATED reconciliation (the periodic rescan firing
    every interval) does NOT churn the deployment — no new node row is ever
    created across many passes (PAUSED, no respawn).

    This is the property the periodic loop must preserve: turning the one-shot
    rescan into a recurring one must NOT manufacture crash-loop churn against a
    deliberately-paused deployment."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory,
        auto_restart_paused=True,
        consecutive=5,
    )

    rows_before = await _node_row_count(session_factory, dep_id)
    # Many reconciliation passes (the periodic loop firing repeatedly).
    for _ in range(5):
        restarted = await fleet_router.rescan_for_restart()
        assert restarted == 0, "a paused deployment must never be respawned by reconciliation"

    assert await _node_row_count(session_factory, dep_id) == rows_before, (
        "repeated reconciliation must NOT churn a paused deployment (no new node rows)"
    )


@pytest.mark.asyncio
async def test_rescan_does_not_stampede_shared_gateway(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council #4 test 5: when multiple accounts on the SAME gateway fail
    together, ONE rescan pass respawns AT MOST ONE per gateway — the Phase-A
    ``CONCURRENT_STARTUP`` guard (keyed by ``gateway_session_key``) serialises
    the second so two TradingNodes never connect the same IB gateway at once.

    Two failed deployments, distinct accounts, SAME ``gateway_session_key``: a
    single ``rescan_for_restart`` pass must report exactly one genuine respawn;
    the sibling on the same gateway is held off (it recovers on a LATER pass
    once the first reaches ``running``)."""
    shared_gw = "shared-sess"
    dep_a, _ = await _seed_crashed_deployment(
        session_factory,
        account_id="DU1111111",
        ib_login_key="shared-login",
        gateway_session_key=shared_gw,
    )
    dep_b, _ = await _seed_crashed_deployment(
        session_factory,
        account_id="DU2222222",
        ib_login_key="shared-login",
        gateway_session_key=shared_gw,
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 1, (
        "one rescan pass must respawn AT MOST ONE node per shared gateway "
        f"(CONCURRENT_STARTUP serialisation); saw {restarted}"
    )
    # Exactly one of the two got a fresh (second) node row this pass.
    a_rows = await _node_row_count(session_factory, dep_a)
    b_rows = await _node_row_count(session_factory, dep_b)
    assert {a_rows, b_rows} == {1, 2}, (
        "exactly one shared-gateway deployment respawned this pass; the other waits "
        f"(rows a={a_rows}, b={b_rows})"
    )


@pytest.mark.asyncio
async def test_rescan_skips_a_backoff_candidate_without_blocking_others(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PR 2 F2 (review P2): a candidate sitting in its ``RestartPolicy`` BACKOFF
    window must be SKIPPED by the rescan (no inline sleep) and must NOT delay the
    evaluation / restart of the OTHER candidates.

    Before F2 the rescan awaited ``_maybe_auto_restart`` INLINE, which slept the
    cancellable backoff (up to 300s) in-band — so one crash-looping account left
    every unrelated account flat-and-unmonitored for up to the backoff cap. With
    ``skip_backoff=True`` the rescan declines the backoff candidate this pass
    (the next pass retries once the window elapses) and restarts the healthy
    candidate WITHOUT waiting.

    We seed one deployment in a fresh backoff window (consecutive>=1 +
    last_restart_at = now → ``decide`` returns BACKOFF) and one ordinary failed
    deployment (consecutive=0 → RESTART). A single rescan pass must (a) return
    PROMPTLY, (b) restart exactly the non-backoff one, and (c) leave the backoff
    candidate un-respawned (it recovers on a later pass).
    """
    # Backoff candidate: just attempted a restart, so it's inside the backoff
    # window — distinct gateway so it can't be the same-gateway sibling held off.
    backoff_dep, _ = await _seed_crashed_deployment(
        session_factory,
        account_id="DU3333333",
        ib_login_key="backoff-login",
        gateway_session_key="backoff-sess",
        consecutive=2,
        last_restart_at=datetime.now(UTC),
    )
    # Healthy failed candidate: no prior attempt → RESTART now.
    fresh_dep, _ = await _seed_crashed_deployment(
        session_factory,
        account_id="DU4444444",
        ib_login_key="fresh-login",
        gateway_session_key="fresh-sess",
        consecutive=0,
    )

    # The rescan must finish promptly — never blocking on the backoff window.
    restarted = await asyncio.wait_for(fleet_router.rescan_for_restart(), timeout=5.0)

    assert restarted == 1, (
        "exactly the non-backoff candidate restarts this pass; the backoff one is "
        f"skipped (not slept on). saw {restarted}"
    )
    # The backoff candidate was NOT respawned (still its single original row).
    assert await _node_row_count(session_factory, backoff_dep) == 1, (
        "a candidate in its backoff window is skipped this pass — no new node row"
    )
    # The healthy candidate WAS respawned without waiting for the other's backoff.
    assert await _node_row_count(session_factory, fresh_dep) == 2
    fresh_latest = await _latest_node_row(session_factory, fresh_dep)
    assert fresh_latest.status == "starting"
    assert await _deployment_status(session_factory, fresh_dep) == "starting"


# ---------------------------------------------------------------------------
# PR 2 / F1 — the rescan must recover stale ``starting`` orphans
# ---------------------------------------------------------------------------
#
# A ``starting`` row inserted by Phase A whose supervisor/container died BEFORE
# the child self-wrote ``building`` is recovered by NOTHING otherwise:
# ``watchdog_once`` is host-scoped (skips the dead container's host),
# ``HeartbeatMonitor`` excludes startup statuses, and the rescan previously
# skipped ``starting``. That left the partial-unique active ``starting`` row
# stuck FOREVER, blocking all future starts for that deployment.


@pytest.mark.asyncio
async def test_rescan_recovers_stale_starting_orphan(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1: a STALE ``starting`` orphan (Phase-A reserved the slot, then the
    owning supervisor/container died before the child wrote ``building``, so
    the heartbeat never advanced past the staleness threshold) is recovered by
    the rescan — flipped to ``failed`` and respawned. Without F1 the partial-
    unique active ``starting`` row would block every future start forever."""
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        last_heartbeat_at=stale_hb,
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 1, "the stale 'starting' orphan must be recovered by the rescan"
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed", "the stale 'starting' orphan is flipped to failed"
    # A fresh node row is reserved + the deployment is back to starting.
    assert await _node_row_count(session_factory, dep_id) == 2
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.id != stale_row_id
    assert latest.status == "starting"
    assert latest.consecutive_respawn_failures == 1
    assert await _deployment_status(session_factory, dep_id) == "starting"


@pytest.mark.asyncio
async def test_rescan_leaves_fresh_starting_row_alone(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1 (the safety half): a FRESH ``starting`` row — THIS supervisor just
    inserted it in Phase A and the child is genuinely starting up RIGHT NOW
    (recent heartbeat) — must NOT be reaped. Only orphaned ``starting`` rows
    whose heartbeat has not advanced past the staleness threshold are
    candidates; a node legitimately mid-startup is left to finish."""
    dep_id, fresh_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        last_heartbeat_at=datetime.now(UTC),  # fresh — mid-startup right now
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a fresh 'starting' row (mid-startup now) must not be touched"
    assert await _node_row_count(session_factory, dep_id) == 1
    async with session_factory() as session:
        fresh_row = await session.get(LiveNodeProcess, fresh_row_id)
        assert fresh_row is not None
        assert fresh_row.status == "starting", "the fresh starting row is left untouched"
    assert await _deployment_status(session_factory, dep_id) == "starting"


# ---------------------------------------------------------------------------
# PR 2 / F2 — the rescan + reaper must EXCLUDE pre-spawn / never-ran failures
# ---------------------------------------------------------------------------
#
# The terminal-``failed`` candidate set must be restricted to RECOVERABLE
# runtime crashes (a node that RAN then crashed). A pre-spawn permanent-config
# failure or a halt-blocked START that NEVER ran must NOT be re-driven by the
# rescan — re-driving churns a permanent error to the ceiling, or re-trades a
# halt-blocked account after the halt clears. The reaper applies the SAME
# ``is_recoverable_crash`` predicate so the two agree.


@pytest.mark.asyncio
async def test_rescan_recovers_node_crashed_runtime_crash(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 (positive): a ``running``-then-crashed deployment (NODE_CRASHED — the
    node RAN and then its trading loop exited non-zero) IS rescan-eligible and
    is recovered."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(session_factory, dep_id, FailureKind.NODE_CRASHED.value)

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 1, "a recoverable runtime crash (NODE_CRASHED) must be rescan-recovered"
    assert await _node_row_count(session_factory, dep_id) == 2
    assert await _deployment_status(session_factory, dep_id) == "starting"


@pytest.mark.asyncio
async def test_rescan_does_not_churn_pre_spawn_permanent_failure(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 (the headline negative + falsification target): a pre-spawn
    permanent-config ``failed`` row (SPAWN_FAILED_PERMANENT — the node NEVER
    ran: ``process.start()`` failed, a permanent payload-config error) is NOT
    rescan-eligible. No respawn, no churn — REPEATED reconciliation never
    manufactures a new node row. The operator must fix config and re-issue the
    start."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(
        session_factory, dep_id, FailureKind.SPAWN_FAILED_PERMANENT.value
    )

    rows_before = await _node_row_count(session_factory, dep_id)
    # Many reconciliation passes (the periodic loop firing repeatedly).
    for _ in range(5):
        restarted = await fleet_router.rescan_for_restart()
        assert restarted == 0, "a pre-spawn permanent-config failure must never be rescanned"

    assert await _node_row_count(session_factory, dep_id) == rows_before, (
        "no new node row — a never-ran permanent failure must not churn"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_excludes_registry_miss_failure(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2: a registry-resolution ``failed`` row (REGISTRY_MISS — a pre-spawn
    config error; the node never ran) is excluded from the rescan candidate
    set, exactly like the permanent-config case."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(session_factory, dep_id, FailureKind.REGISTRY_MISS.value)

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a pre-spawn registry-resolution failure must not be rescanned"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_does_not_autostart_halt_blocked_start_after_halt_clears(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """F2 (the kill-all → /resume → supervisor-restart case, now PRINCIPLED):
    a START that was BLOCKED by a halt (marked HALT_ACTIVE — the node NEVER
    ran) must NOT be auto-started by the rescan EVEN AFTER the halt latch
    clears. The operator re-issues the start; the rescan does not resurrect a
    never-ran halt-blocked deployment.

    This is the negative the prior iter-3 P3 documented as a known-limitation —
    now resolved by the F2 eligibility split."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(session_factory, dep_id, FailureKind.HALT_ACTIVE.value)

    # The halt has since CLEARED (operator /resume): no latch set in Redis.
    assert await redis_client.exists(fleet_halt_key()) == 0

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, (
        "a halt-blocked START that never ran must NOT be auto-started by the "
        "rescan after the halt clears — the operator re-issues the start"
    )
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_rescan_does_not_autostart_account_halt_blocked_start(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> None:
    """F2 (account-halt half): an ACCOUNT_HALT_ACTIVE never-ran START is also
    excluded from rescan recovery after the /drain latch clears."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(session_factory, dep_id, FailureKind.ACCOUNT_HALT_ACTIVE.value)
    assert await redis_client.exists(account_halt_key(_ACCOUNT_ID)) == 0

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "an account-halt-blocked never-ran START must not be auto-started"
    assert await _node_row_count(session_factory, dep_id) == 1
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_reaper_suppresses_pre_spawn_permanent_failure(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 (reaper + rescan AGREE): the reaper ``_on_child_exit`` applies the
    SAME ``is_recoverable_crash`` predicate. A terminal ``failed`` row carrying
    a pre-spawn SPAWN_FAILED_PERMANENT kind (the subprocess wrote it on a
    pre-``_mark_running`` engine/import failure) is NOT auto-restarted by the
    reaper even on a non-zero exit — it never ran."""
    dep_id, _row_id = await _seed_crashed_deployment(
        session_factory, deployment_status="failed", node_status="failed"
    )
    await _set_latest_failure_kind(
        session_factory, dep_id, FailureKind.SPAWN_FAILED_PERMANENT.value
    )

    await fleet_router._on_child_exit(dep_id, exit_code=1)
    await fleet_router._await_restart_tasks_for_test()

    assert await _node_row_count(session_factory, dep_id) == 1, (
        "the reaper must NOT restart a pre-spawn permanent failure that never ran"
    )
    assert await _deployment_status(session_factory, dep_id) == "failed"


@pytest.mark.asyncio
async def test_reaper_classifies_generic_crash_as_node_crashed_and_restarts(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 (reaper classify + recover): a stale-ACTIVE ``running`` row whose
    subprocess never wrote its own terminal row — SIGKILL/OOM, with the
    deployment already flipped ``failed`` by ``_mark_terminal`` — reaped with a
    generic non-zero exit is classified NODE_CRASHED (the node RAN) and
    auto-restarted. Confirms the SPAWN_FAILED_PERMANENT-overload resolution
    does NOT break the council-#3 common-crash restart for a node that ran."""
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        # The realistic SIGKILL/OOM shape: the node's ``_mark_terminal`` flipped
        # the DEPLOYMENT to ``failed`` but the supervisor's reaper hadn't yet
        # written the NODE row's terminal state (it's still active ``running``).
        deployment_status="failed",
        node_status="running",
    )
    # The reaper observed exit code 1 (a generic crash, not a clean stop).
    await fleet_router._on_child_exit(dep_id, exit_code=1)
    await fleet_router._await_restart_tasks_for_test()

    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed"
        assert stale_row.failure_kind == FailureKind.NODE_CRASHED.value, (
            "a generic non-zero exit on a still-active row that RAN is NODE_CRASHED, "
            "not SPAWN_FAILED_PERMANENT"
        )
    assert await _node_row_count(session_factory, dep_id) == 2, (
        "a runtime crash (NODE_CRASHED) must still be auto-restarted by the reaper"
    )
    # The restart reset the terminal deployment back to ``starting`` for the
    # in-flight respawn (restart_carry path).
    assert await _deployment_status(session_factory, dep_id) == "starting"
    latest = await _latest_node_row(session_factory, dep_id)
    assert latest.id != stale_row_id
    assert latest.status == "starting"


# ---------------------------------------------------------------------------
# PR 2 / F3 — durable prior-operation evidence probe (the boot-SPOF
# first-ever-vs-expired-after-outage discriminator) against REAL Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_prior_operation_evidence_false_on_empty_install(
    fleet_router: FleetRouter,
) -> None:
    """F3: a brand-new install (no live_deployments / live_node_processes rows)
    has NO prior-operation evidence — the boot handshake treats a None heartbeat
    as a genuine first-ever boot (no spurious SPOF)."""
    assert await fleet_router.has_prior_operation_evidence() is False


@pytest.mark.asyncio
async def test_has_prior_operation_evidence_true_when_any_row_exists(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: once ANY deployment/node-process row exists, the probe returns True —
    a None heartbeat at boot is then an EXPIRED-after-outage gap (fire SPOF),
    not a first-ever boot."""
    await _seed_crashed_deployment(session_factory)
    assert await fleet_router.has_prior_operation_evidence() is True


# ---------------------------------------------------------------------------
# FIX 2 (P2) — give-up cleanup targets the OWNED row, not "the latest row".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_giveup_cleanup_targets_owned_row_and_leaves_newer_active_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 (P2): ``_clear_sentinel_and_refail_after_giveup`` must act on the
    SPECIFIC owned row the reaper stamped — NOT "the latest row".

    Scenario the bug clobbers: the reaper stamped ``restart_dispatched_at`` on a
    dead row and scheduled a restart task. The restart task exhausts its
    transient retries and reaches the give-up cleanup. Meanwhile a CONCURRENT
    rescan / operator-retry already started a FRESH row for the same deployment
    (now the latest, active). Targeting "the latest row" would clobber that fresh
    running row to ``failed`` without signalling its child — dropping a running
    node from the active set and risking a duplicate start.

    After the fix the give-up cleanup, given the OWNED (old) row id:
      - clears the owned row's sentinel + (it's already terminal) leaves it
        ``failed``,
      - leaves the NEWER active row UNTOUCHED (still active),
      - does NOT re-fail the parent deployment (a concurrent restart already
        brought it back to active).

    Falsification (pre-fix): the latest-row select clobbers the NEWER active row
    to ``failed`` — assert it stays active instead.
    """
    import socket

    # Owned (old) row: the reaper-stamped, now-terminal failed row.
    dep_id, owned_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",  # a concurrent restart reset it to active
        node_status="failed",
        restart_dispatched_at=datetime.now(UTC),
    )

    # NEWER active row for the SAME deployment (the concurrent restart's fresh
    # row). started_at strictly later so it is "the latest row".
    newer_row_id = None
    async with session_factory() as session, session.begin():
        newer = LiveNodeProcess(
            deployment_id=dep_id,
            pid=None,
            host=socket.gethostname(),
            started_at=datetime.now(UTC) + timedelta(seconds=5),
            last_heartbeat_at=datetime.now(UTC),
            status="running",
            gateway_session_key="sess-fresh",
            restart_dispatched_at=None,
        )
        session.add(newer)
        await session.flush()
        newer_row_id = newer.id

    # Give-up cleanup for the OWNED (old) row.
    await fleet_router._clear_sentinel_and_refail_after_giveup(dep_id, owned_row_id)

    async with session_factory() as session:
        owned = await session.get(LiveNodeProcess, owned_row_id)
        assert owned is not None
        # Owned row's sentinel cleared; it was already terminal so it stays failed.
        assert owned.restart_dispatched_at is None
        assert owned.status == "failed"

        newer_row = await session.get(LiveNodeProcess, newer_row_id)
        assert newer_row is not None
        assert newer_row.status == "running", (
            "the give-up cleanup must NOT clobber a NEWER active row (a concurrent "
            "restart's fresh running node) — it must target only the owned row"
        )

    # The parent deployment stays active — a concurrent restart already revived it.
    assert await _deployment_status(session_factory, dep_id) == "starting"


@pytest.mark.asyncio
async def test_giveup_cleanup_owned_row_failed_and_refails_deployment_when_no_newer_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 (P2) — the no-concurrent-restart path still works: when the owned
    row is the ONLY (and latest) row, the give-up cleanup clears its sentinel,
    flips a non-terminal owned row to ``failed``, and re-fails the parent
    deployment so a later reap/rescan can re-drive it."""
    # Owned row stranded at ``starting`` (the respawn never reserved a live slot)
    # with the reaper sentinel set; no newer row exists.
    dep_id, owned_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        restart_dispatched_at=datetime.now(UTC),
    )

    await fleet_router._clear_sentinel_and_refail_after_giveup(dep_id, owned_row_id)

    async with session_factory() as session:
        owned = await session.get(LiveNodeProcess, owned_row_id)
        assert owned is not None
        assert owned.restart_dispatched_at is None
        assert owned.status == "failed", (
            "a stranded non-terminal owned row is flipped to failed so the rescan "
            "candidate query (which requires failed) can re-drive it"
        )
    assert await _deployment_status(session_factory, dep_id) == "failed", (
        "with no newer active row, the parent deployment is re-failed so it is rescan-recoverable"
    )


# ---------------------------------------------------------------------------
# Council 2026-05-31 — reaper own-by-row-id (F3 fix). The reaper MUST classify
# and terminal-write the row it OWNS (threaded from spawn), never "the latest
# row" — a concurrent periodic rescan can insert a fresher ``starting`` row for
# the same deployment, and a latest-row reaper would clobber THAT (the F3 bug).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_owns_its_row_leaves_concurrent_fresh_starting_row_untouched(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: the reaper terminal-writes the OWNED (old, dead) row and leaves a
    CONCURRENT fresh ``starting`` row (R2, the latest) UNTOUCHED.

    Scenario (respecting the partial unique index — only ONE active row per
    deployment): the OLD node-process row is the one the reaper observed dying;
    by reap time its subprocess already wrote it terminal (``failed``) and a
    concurrent periodic rescan INSERTed a fresh ``starting`` R2 for the SAME
    deployment (the single current active row, the latest by ``started_at``).
    The reaper is invoked for the OLD process with ``owned_row_id`` = the OLD
    row id and its idempotency sentinel already set (so this test isolates ROW
    TARGETING, not the dispatch decision).

    After the fix the reaper:
      - classifies + locks the OWNED (old) row — leaves it ``failed`` (terminal,
        already written by the subprocess),
      - leaves R2 (the newer ``starting`` row) UNTOUCHED — NOT failed, child not
        orphaned.

    Falsification (pre-fix, the F3 bug): the reaper's ``ORDER BY started_at DESC
    LIMIT 1`` SELECT matched R2 (the latest), wrongly classified R2 (and would
    flip it / dispatch off it) — assert R2 stays ``starting`` instead.
    """
    import socket

    # OLD owned row: already terminal ``failed`` (subprocess wrote it last) with
    # the idempotency sentinel set; the deployment was revived to ``starting`` by
    # the concurrent rescan that created R2.
    dep_id, old_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="failed",
        restart_dispatched_at=datetime.now(UTC),
    )

    # Concurrent fresh ``starting`` R2 for the SAME deployment (the single active
    # row). started_at strictly later so it is "the latest row".
    async with session_factory() as session, session.begin():
        r2 = LiveNodeProcess(
            deployment_id=dep_id,
            pid=None,
            host=socket.gethostname(),
            started_at=datetime.now(UTC) + timedelta(seconds=5),
            last_heartbeat_at=datetime.now(UTC) + timedelta(seconds=5),
            status="starting",
            gateway_session_key="sess-fresh",
        )
        session.add(r2)
        await session.flush()
        r2_id = r2.id

    # Reaper for the OLD process, owning the OLD row by id. exit_code=1 (crash).
    await fleet_router._on_child_exit(dep_id, exit_code=1, owned_row_id=old_row_id, proc_pid=4242)
    await fleet_router._await_restart_tasks_for_test()

    async with session_factory() as session:
        old_row = await session.get(LiveNodeProcess, old_row_id)
        assert old_row is not None
        assert old_row.status == "failed", (
            "the reaper must classify the OWNED (old) row and leave it failed"
        )

        r2_row = await session.get(LiveNodeProcess, r2_id)
        assert r2_row is not None
        assert r2_row.status == "starting", (
            "the reaper must NOT touch a concurrent fresh 'starting' row (R2) — "
            "the F3 bug clobbered it by classifying 'the latest row'"
        )
    # No respawn row was created off R2 (the owned row's sentinel was already set,
    # and even if it dispatched, Phase A would idempotently no-op against R2).
    assert await _node_row_count(session_factory, dep_id) == 2


@pytest.mark.asyncio
async def test_reaper_pid_legacy_fallback_targets_matching_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the reaper has NO owned_row_id but a ``proc_pid`` that matches a row
    for the deployment, it falls back to ``(deployment_id, pid == proc_pid)`` and
    emits the ``reap_pid_legacy_fallback`` structured log."""
    import logging
    import socket

    dep_id, row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC),
    )
    # Stamp a real pid on the row so the pid fallback can match it.
    async with session_factory() as session, session.begin():
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        row.pid = 31337
        _ = socket.gethostname()

    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await fleet_router._on_child_exit(dep_id, exit_code=1, owned_row_id=None, proc_pid=31337)
    await fleet_router._await_restart_tasks_for_test()

    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        assert row.status == "failed", (
            "the pid legacy fallback must terminal-write the pid-matched row"
        )
    assert any(r.message == "reap_pid_legacy_fallback" for r in caplog.records), (
        "the reaper must emit reap_pid_legacy_fallback when it falls back to pid matching"
    )


@pytest.mark.asyncio
async def test_reaper_no_identity_no_pid_match_writes_nothing(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the reaper has NO owned_row_id AND a ``proc_pid`` that matches NO row
    for the deployment, it must write NOTHING (touch no active row) and emit
    ``reap_no_owned_row_no_pid_match`` so watchdog/HeartbeatMonitor/rescan can
    reconcile.

    Falsification: a latest-row / latest-with-NULL-pid fallback would clobber the
    active row — assert the active row is LEFT UNTOUCHED instead.
    """
    import logging

    dep_id, row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC),
    )
    # The row has pid=None (default); proc_pid below matches nothing.
    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await fleet_router._on_child_exit(dep_id, exit_code=1, owned_row_id=None, proc_pid=999999)
    await fleet_router._await_restart_tasks_for_test()

    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        assert row.status == "running", (
            "with no owned row and no pid match, the reaper must NOT touch any "
            "active row (no latest-row fallback — that is the F3 bug)"
        )
    assert await _node_row_count(session_factory, dep_id) == 1, "no respawn must be dispatched"
    assert await _deployment_status(session_factory, dep_id) == "running"
    assert any(r.message == "reap_no_owned_row_no_pid_match" for r in caplog.records), (
        "the reaper must emit reap_no_owned_row_no_pid_match when it can't identify its row"
    )


# ---------------------------------------------------------------------------
# Council 2026-05-31 — _refail_stranded_restart own-by-id (2425). After Phase A
# creates the row, spawn_with_outcome crosses await boundaries; a concurrent
# rescan / operator-retry can insert a NEWER active row in that window. The
# refail-stranded path must act on the OWNED row, never "the latest".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refail_stranded_restart_owns_its_row_leaves_newer_active_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``_refail_stranded_restart`` must act ONLY on the owned row by id and
    leave a NEWER concurrent active row + the parent deployment untouched.

    Scenario (respecting the partial unique index — only ONE active row per
    deployment): the owned row this respawn reserved was already flipped to
    ``failed`` (the transient ``_mark_failed`` cleanup, or a SIGKILL crash) by
    the time ``_refail_stranded_restart`` runs, and a concurrent rescan /
    operator-retry has since started a fresher ``running`` row (the single
    active row, later ``started_at``). Targeting "the latest row" would clobber
    THAT ``running`` row to ``failed`` and re-fail the live deployment.

    After the fix (own-by-id): the owned (already-``failed``) row is left as-is,
    the NEWER ``running`` row is UNTOUCHED, and the parent deployment is NOT
    re-failed (the newer-active guard yields).

    Falsification (pre-fix, latest-row select): the ``running`` row is flipped
    to ``failed`` — assert it stays ``running`` instead.
    """
    import socket

    # Owned row: already terminal ``failed`` (the transient cleanup flipped it);
    # the deployment was revived to ``running`` by the concurrent restart.
    dep_id, owned_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="failed",
        last_heartbeat_at=datetime.now(UTC),
    )

    # NEWER concurrent active row (a rescan / operator-retry started it) — the
    # single active row.
    async with session_factory() as session, session.begin():
        newer = LiveNodeProcess(
            deployment_id=dep_id,
            pid=None,
            host=socket.gethostname(),
            started_at=datetime.now(UTC) + timedelta(seconds=5),
            last_heartbeat_at=datetime.now(UTC) + timedelta(seconds=5),
            status="running",
            gateway_session_key="sess-fresh",
        )
        session.add(newer)
        await session.flush()
        newer_row_id = newer.id

    await fleet_router._refail_stranded_restart(dep_id, owned_row_id=owned_row_id)

    async with session_factory() as session:
        owned = await session.get(LiveNodeProcess, owned_row_id)
        assert owned is not None
        assert owned.status == "failed", "the owned row stays failed (terminal)"

        newer_row = await session.get(LiveNodeProcess, newer_row_id)
        assert newer_row is not None
        assert newer_row.status == "running", (
            "_refail_stranded_restart must NOT clobber a NEWER concurrent active row"
        )
    assert await _deployment_status(session_factory, dep_id) == "running", (
        "the parent deployment must NOT be re-failed when a newer active row exists"
    )


@pytest.mark.asyncio
async def test_refail_stranded_restart_skips_entirely_when_no_owned_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FINDING 2 (P2): when ``owned_row_id is None`` (this attempt reserved NO
    row — ``CONCURRENT_STARTUP`` / ``NO_DEPLOYMENT`` / a pre-Phase-A payload
    failure), ``_refail_stranded_restart`` must SKIP the row mutation AND the
    deployment re-fail entirely — it owns nothing, so there is nothing for it to
    clean up.

    Scenario: a no-ACK respawn reserved no row, but a CONCURRENT operator-retry
    / periodic rescan has since created a fresh ``running`` row (now the latest,
    the single active row) and revived the deployment. The OLD latest-row
    fallback (``ORDER BY started_at DESC LIMIT 1``) would flip THAT live
    ``running`` row to ``failed`` and re-fail the deployment — with the child
    still running. The fix makes the no-reservation case a strict no-op.

    Falsification (pre-fix, latest-row fallback): the concurrent ``running`` row
    is flipped to ``failed`` and the deployment re-failed — assert both stay
    untouched instead.
    """
    import socket

    # No owned row for this attempt. The deployment is currently ``running``
    # (a concurrent restart revived it) with one fresh ``running`` node row.
    dep_id, _seed_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC),
    )

    # A second, fresher concurrent ``running`` row would violate the partial
    # unique index, so model the realistic shape: a SINGLE fresh ``running`` row
    # is the latest. (The seed row above IS that running row.) Record its id so
    # we can assert the latest-row fallback would have targeted it.
    latest = await _latest_node_row(session_factory, dep_id)
    latest_row_id = latest.id
    assert latest.host == socket.gethostname()

    await fleet_router._refail_stranded_restart(dep_id, owned_row_id=None)

    async with session_factory() as session:
        latest_after = await session.get(LiveNodeProcess, latest_row_id)
        assert latest_after is not None
        assert latest_after.status == "running", (
            "_refail_stranded_restart with no owned row must NOT flip the "
            "concurrent live 'running' row to failed"
        )
    assert await _deployment_status(session_factory, dep_id) == "running", (
        "_refail_stranded_restart with no owned row must NOT re-fail the deployment"
    )


# ---------------------------------------------------------------------------
# Council 2026-05-31 — F5 flip-but-suppress. A stale ``starting``/``building``
# row with ``stop_requested_at`` set must be FLIPPED to ``failed`` by the rescan
# Step-1 cleanup (so it leaves the active unique-index set — a dead node must
# not masquerade as active on the real-money dashboard) but must NOT be respawned
# by Step-2 (operator-stop intent honored — never resurrected).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rescan_flips_stop_requested_stale_starting_row_but_does_not_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F5 flip-but-suppress (council 2026-05-31): a stale ``starting`` row whose
    ``stop_requested_at`` is set is FLIPPED to ``failed`` by the rescan Step-1
    cleanup (leaving the active set) yet NOT respawned by Step-2 (durable
    operator-stop intent is honored).

    Falsification (old contract): the Step-1 cleanup excluded
    ``stop_requested_at`` rows, leaving a dead node stuck-active (masquerading
    as ``starting`` on the dashboard) forever — assert it is flipped to
    ``failed`` instead, while still asserting ``restarted == 0``.
    """
    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    dep_id, stale_row_id = await _seed_crashed_deployment(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        last_heartbeat_at=stale_hb,
        stop_requested_at=datetime.now(UTC),
    )

    restarted = await fleet_router.rescan_for_restart()

    assert restarted == 0, "a /stop'd stale row must NOT be respawned (Step-2 suppresses)"
    assert await _node_row_count(session_factory, dep_id) == 1, "no respawn row created"
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed", (
            "the Step-1 cleanup must FLIP a dead stop_requested stale-active row to "
            "'failed' so it leaves the active unique-index set — a dead node must not "
            "masquerade as 'starting' on the real-money dashboard"
        )
    assert await _deployment_status(session_factory, dep_id) == "failed", (
        "the parent deployment is synced to 'failed' alongside the flipped node row"
    )
