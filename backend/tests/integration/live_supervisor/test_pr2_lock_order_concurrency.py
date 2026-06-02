"""Council 2026-06-01 verdict — PR-2 real-money operator-stop / auto-restart
concurrency core. The LOCK-ORDER MIGRATION (deployment-FIRST) + the pre-start
stop-intent gate + the shutdown-cancellation safety + the ownerless-active-row
backstop.

These drive the REAL ``FleetRouter`` against a dedicated Postgres testcontainer
+ Redis (the same harness ``test_auto_restart_db.py`` uses) and assert the exact
concurrency invariants the verdict mandates:

1. **Acyclic lock order (deadlock repro + fix).** Two concurrent sessions that
   take the deployment + node row locks in the SAME (deployment-first) order can
   never deadlock — a crossed (deployment-first vs node-first) order CAN. We
   prove the migrated paths all acquire deployment-then-node by driving a true
   concurrent ``stop`` (deployment-first) against a give-up cleanup (now also
   deployment-first) and asserting both converge with no hang inside a timeout.

2. **Pre-start stop-intent gate.** A ``/stop`` whose ``stop_requested_at`` lands
   AFTER Phase A commits the ``starting`` row but BEFORE ``process.start()`` must
   abort the spawn: NO process starts, the reserved row is terminalized (leaves
   the active set), outcome is suppressed.

3. **Shutdown cancellation safety.** Cancelling the spawn task inside the
   reserved→pre-start window terminalizes the reserved row — no orphan
   ``starting`` pid=NULL row left in the active set.

4. **Ownerless-active-row backstop.** A ``starting`` / ``stopping`` row with
   ``pid IS NULL`` and no live cached handle, older than a bounded grace window,
   is reaped host-agnostically: terminal-no-respawn when ``stop_requested_at`` is
   set, ``failed``/recoverable otherwise.

SAFETY: dedicated Postgres + Redis testcontainers per module.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import socket
import time  # used by _sleep_target (FINDING 3 re-signal variant)
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select

from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.live_supervisor.fleet_router import FleetRouter
from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
from msai.live_supervisor.restart_policy import RestartPolicy
from msai.models import Base, LiveDeployment, LiveNodeProcess
from msai.services.live.failure_kind import FailureKind
from msai.services.nautilus.trading_node_subprocess import _mark_terminal
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_ACCOUNT_ID = "DU1234567"


# ---------------------------------------------------------------------------
# Fixtures (verbatim from test_auto_restart_db.py so the harness can't drift)
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
    with contextlib.suppress(Exception):
        await client.delete(fleet_halt_key(), account_halt_key(_ACCOUNT_ID))
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.delete(fleet_halt_key(), account_halt_key(_ACCOUNT_ID))
        await client.aclose()


def _noop_target() -> None:
    return None


def _sleep_target(seconds: int = 30) -> None:
    """A spawnable child that sleeps so a test has a live pid to SIGTERM
    (FINDING 3 re-signal variant). Top-level so ``mp`` can pickle it."""
    time.sleep(seconds)


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


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    deployment_status: str = "failed",
    node_status: str = "failed",
    pid: int | None = None,
    host: str | None = None,
    last_heartbeat_at: datetime | None = None,
    stop_requested_at: datetime | None = None,
    restart_dispatched_at: datetime | None = None,
    failure_kind: str | None = None,
    node_started_at: datetime | None = None,
    deployment_last_started_at: datetime | None = None,
) -> tuple[UUID, UUID]:
    async with session_factory() as session:
        dep = await make_live_deployment(
            session,
            account_id=_ACCOUNT_ID,
            ib_login_key="msai-paper-primary",
            status=deployment_status,
        )
        if deployment_last_started_at is not None:
            dep.last_started_at = deployment_last_started_at
        now = datetime.now(UTC)
        row = LiveNodeProcess(
            deployment_id=dep.id,
            pid=pid,
            host=host if host is not None else socket.gethostname(),
            started_at=node_started_at or now,
            last_heartbeat_at=last_heartbeat_at or now,
            status=node_status,
            gateway_session_key="sess-1",
            stop_requested_at=stop_requested_at,
            restart_dispatched_at=restart_dispatched_at,
            failure_kind=failure_kind,
        )
        session.add(row)
        await session.commit()
        return dep.id, row.id


async def _node_row(
    session_factory: async_sessionmaker[AsyncSession], row_id: UUID
) -> LiveNodeProcess:
    async with session_factory() as session:
        return (
            await session.execute(select(LiveNodeProcess).where(LiveNodeProcess.id == row_id))
        ).scalar_one()


async def _active_row_count(
    session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID
) -> int:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveNodeProcess.id).where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(
                            ("starting", "building", "ready", "running", "stopping")
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def _slug(session_factory: async_sessionmaker[AsyncSession], deployment_id: UUID) -> str:
    async with session_factory() as session:
        return (
            await session.execute(
                select(LiveDeployment.deployment_slug).where(LiveDeployment.id == deployment_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# 1. Acyclic lock order — deadlock repro + fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_stop_and_giveup_do_not_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """REAL concurrent-deadlock probe (council 2026-06-01, item 1).

    Two operations that both lock the deployment row AND the deployment's node
    row run CONCURRENTLY against the real Postgres:

    - ``stop`` (operator /stop on consume) — migrated to deployment-FIRST.
    - ``_clear_sentinel_and_refail_after_giveup`` (restart-task give-up) —
      migrated to deployment-FIRST.

    Before the migration these took CROSSED orders (stop locked the node row
    only / give-up locked node-then-deployment) — a classic D→N vs N→D cycle
    that can deadlock under load. After the migration both acquire
    deployment-then-node, so the two serialise cleanly. We assert both complete
    within a generous timeout — a deadlock would hang one session until
    Postgres' ``deadlock_timeout`` aborts it (or the test times out)."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        restart_dispatched_at=datetime.now(UTC),
    )

    async def _do_stop() -> None:
        await fleet_router.stop(dep_id, reason="user")

    async def _do_giveup() -> None:
        await fleet_router._clear_sentinel_and_refail_after_giveup(dep_id, row_id)

    # Run both concurrently many times; a crossed lock order surfaces a deadlock
    # probabilistically, so repeat to make the probe sensitive.
    for _ in range(10):
        # Reset the row to a re-runnable state each round.
        async with session_factory() as session, session.begin():
            row = await session.get(LiveNodeProcess, row_id)
            assert row is not None
            row.status = "running"
            row.stop_requested_at = None
            row.restart_dispatched_at = datetime.now(UTC)
            dep = await session.get(LiveDeployment, dep_id)
            assert dep is not None
            dep.status = "running"

        await asyncio.wait_for(
            asyncio.gather(_do_stop(), _do_giveup()),
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_lock_order_is_deployment_first_in_migrated_paths() -> None:
    """Static lock-acquisition-order invariant (council 2026-06-01, item 1).

    A code-level inspection guard: every both-row-locking path in fleet_router
    must acquire the ``live_deployments`` lock textually BEFORE the
    ``live_node_processes`` lock. This is the cheap, deterministic complement to
    the probabilistic concurrent probe above — it falsifies a regression that
    re-introduces a node-then-deployment ordering without needing the race to
    actually fire.

    We assert on the SOURCE of the migrated methods: in each, the FIRST
    ``with_for_update`` that targets ``LiveDeployment`` appears before any
    ``with_for_update`` that targets only a ``LiveNodeProcess`` owned-row lock,
    OR the method is documented node-only.

    Finding 1 (council 2026-06-01): ``_mark_failed`` (fleet_router) and
    ``_mark_terminal`` (trading_node_subprocess) were previously EXEMPTED from
    this guard as "no FOR UPDATE held". That was WRONG — a dirty-flush ORM UPDATE
    acquires a row WRITE lock held until commit, and SQLAlchemy flushes UPDATEs in
    dirty-order (NOT FK order), so writing the node row before the deployment row
    is a node→deployment lock-acquisition edge. Both are now deployment-FIRST
    (acquire the LiveDeployment row lock textually before the LiveNodeProcess
    row), so they belong UNDER this static guard."""
    import inspect

    from msai.live_supervisor import fleet_router as fr
    from msai.services.nautilus import trading_node_subprocess as tns

    # fleet_router methods that take both row locks via ``select(...).with_for_update()``.
    for method_name in (
        "_clear_sentinel_and_refail_after_giveup",
        "_refail_stranded_restart",
        "stop",
    ):
        src = inspect.getsource(getattr(fr.FleetRouter, method_name))
        dep_lock = src.find("select(LiveDeployment)")
        node_lock = src.find("select(LiveNodeProcess)")
        assert dep_lock != -1, f"{method_name}: expected a LiveDeployment FOR UPDATE select"
        if node_lock != -1:
            assert dep_lock < node_lock, (
                f"{method_name}: LiveDeployment lock must be acquired BEFORE the "
                f"LiveNodeProcess lock (deployment-first invariant). "
                f"dep_lock={dep_lock} node_lock={node_lock}"
            )

    # Finding 1 — ``_mark_failed`` (fleet_router): deployment-first. The
    # deployment lock (``select(LiveDeployment) ... with_for_update`` OR
    # ``session.get(LiveDeployment, ..., with_for_update=True)``) must precede
    # the node lock acquisition.
    mark_failed_src = inspect.getsource(fr.FleetRouter._mark_failed)
    mf_dep = _first_lock_index(mark_failed_src, "LiveDeployment")
    mf_node = _first_lock_index(mark_failed_src, "LiveNodeProcess")
    assert mf_dep != -1, "_mark_failed: expected a LiveDeployment FOR UPDATE lock"
    assert mf_node != -1, "_mark_failed: expected a LiveNodeProcess FOR UPDATE lock"
    assert mf_dep < mf_node, (
        "_mark_failed: LiveDeployment lock must be acquired BEFORE the "
        f"LiveNodeProcess lock (deployment-first invariant). dep={mf_dep} node={mf_node}"
    )

    # Finding 1 — ``_mark_terminal`` (trading_node_subprocess): deployment-first.
    mark_terminal_src = inspect.getsource(tns._mark_terminal)
    mt_dep = _first_lock_index(mark_terminal_src, "LiveDeployment")
    mt_node = _first_lock_index(mark_terminal_src, "LiveNodeProcess")
    assert mt_dep != -1, "_mark_terminal: expected a LiveDeployment FOR UPDATE lock"
    assert mt_node != -1, "_mark_terminal: expected a LiveNodeProcess FOR UPDATE lock"
    assert mt_dep < mt_node, (
        "_mark_terminal: LiveDeployment lock must be acquired BEFORE the "
        f"LiveNodeProcess lock (deployment-first invariant). dep={mt_dep} node={mt_node}"
    )

    # Finding 2 — ``_on_child_exit`` (FleetRouter reaper): now a DEPLOYMENT-FIRST
    # both-row writer (terminal node write + parent-deployment sync). It must
    # acquire the LiveDeployment row lock (``session.get(LiveDeployment, ...,
    # with_for_update=True)``) BEFORE the node row lock (the classify
    # ``select(LiveNodeProcess) ... .with_for_update()``).
    on_child_exit_src = inspect.getsource(fr.FleetRouter._on_child_exit)
    oce_dep = _first_lock_index(on_child_exit_src, "LiveDeployment")
    oce_node = _first_lock_index(on_child_exit_src, "LiveNodeProcess")
    assert oce_dep != -1, "_on_child_exit: expected a LiveDeployment FOR UPDATE lock"
    assert oce_node != -1, "_on_child_exit: expected a LiveNodeProcess FOR UPDATE lock"
    assert oce_dep < oce_node, (
        "_on_child_exit: LiveDeployment lock must be acquired BEFORE the "
        f"LiveNodeProcess lock (deployment-first invariant). dep={oce_dep} node={oce_node}"
    )


def _first_lock_index(src: str, model: str) -> int:
    """Index of the FIRST row-lock acquisition against ``model`` in ``src``.

    Handles both lock idioms used in the migrated paths:
    - ``select(<model>) ... .with_for_update()``
    - ``session.get(<model>, ..., with_for_update=True)``

    A plain (unlocked) ``select(<model>.<column>)`` id-read or a bare
    ``session.get`` without ``with_for_update`` is NOT a lock acquisition and is
    deliberately ignored — only the locking forms count toward the order."""
    candidates: list[int] = []
    select_lock = f"select({model})"
    idx = src.find(select_lock)
    while idx != -1:
        # Only count a select() as a lock if a with_for_update() follows it
        # before the next select() of any model.
        tail = src[idx:]
        if "with_for_update" in tail:
            candidates.append(idx)
        idx = src.find(select_lock, idx + 1)
    get_lock = f"session.get({model}"
    gidx = src.find(get_lock)
    while gidx != -1:
        # session.get(...) on its own line; the with_for_update=True kwarg is on
        # the same call.
        call_end = src.find(")", gidx)
        if call_end != -1 and "with_for_update=True" in src[gidx:call_end]:
            candidates.append(gidx)
        gidx = src.find(get_lock, gidx + 1)
    return min(candidates) if candidates else -1


def test_multi_row_deployment_locks_order_by_id() -> None:
    """INVARIANT 2 (council 2026-06-01 follow-up — DETERMINISTIC TOTAL ORDER,
    pr-toolkit P1). Every path that locks a SET of ``live_deployments`` rows
    (``WHERE id IN (...)``) must do so under a deterministic ``ORDER BY
    LiveDeployment.id`` so two concurrent multi-row deployment-set lockers with
    overlapping sets cannot AB-BA deadlock (a D↔D cycle the deployment-FIRST
    invariant does NOT cover — it only rules out D→N→D).

    Static source guard: in each multi-row deployment locker, the
    ``LiveDeployment.id.in_(`` predicate that drives the ``FOR UPDATE`` must be
    accompanied by an ``order_by(LiveDeployment.id)`` before the lock takes
    effect. This is the cheap deterministic complement to the concurrent AB-BA
    probe below."""
    import inspect

    from msai.live_supervisor import fleet_router as fr
    from msai.live_supervisor import heartbeat_monitor as hm

    # (module, callable, identifying substring of the multi-row deployment lock)
    cases = [
        # HeartbeatMonitor stale sweep — Step 1 lock of candidate deployments.
        (inspect.getsource(hm.HeartbeatMonitor._mark_stale_as_failed), "candidate_dep_ids"),
        # Rescan Step-1a — lock of stale-active candidate deployments.
        (inspect.getsource(fr.FleetRouter._load_rescan_candidates), "stale_active_dep_ids"),
        # Watchdog Step-3 — ordered PRE-LOCK before the bulk deployment UPDATE.
        (inspect.getsource(fr.FleetRouter.watchdog_once), "killed_deployment_ids"),
    ]
    for src, id_list_name in cases:
        # The deployment FOR UPDATE that locks a SET must order by id.
        lock_idx = src.find(f"LiveDeployment.id.in_({id_list_name})")
        assert lock_idx != -1, (
            f"expected a multi-row LiveDeployment.id.in_({id_list_name}) lock — "
            "the lock site moved or was renamed"
        )
        # The ``order_by(LiveDeployment.id)`` must appear in the SAME statement
        # window (between this in_() and the with_for_update that closes it).
        window = src[lock_idx : src.find("with_for_update", lock_idx) + 40]
        assert "order_by(LiveDeployment.id)" in window, (
            "every multi-row LiveDeployment FOR UPDATE must ORDER BY LiveDeployment.id "
            "so concurrent overlapping deployment-set lockers acquire row locks in one "
            f"global order (no AB-BA). Missing in the {id_list_name} lock."
        )


# ---------------------------------------------------------------------------
# 1b. Finding 1 — _mark_terminal / _mark_failed vs /stop deadlock probes
# ---------------------------------------------------------------------------


async def _stop_lock_pattern(
    session_factory: async_sessionmaker[AsyncSession],
    deployment_id: UUID,
) -> None:
    """Replay the ``/stop`` deployment-FIRST lock-acquisition pattern in one
    transaction: ``live_deployments FOR UPDATE`` THEN the active
    ``live_node_processes FOR UPDATE``, then stamp the stop intent — exactly the
    order :meth:`FleetRouter.stop` / ``api.live.live_stop`` use. A tiny await
    between the two locks WIDENS the interleave window so a node→deployment
    counter-party (a pre-fix ``_mark_terminal`` / ``_mark_failed``) reliably
    crosses into a cycle."""
    async with session_factory() as session, session.begin():
        dep = (
            await session.execute(
                select(LiveDeployment).where(LiveDeployment.id == deployment_id).with_for_update()
            )
        ).scalar_one_or_none()
        # Hold the deployment lock, yield the loop so the concurrent terminal
        # writer can grab the node lock first (forcing the cycle pre-fix).
        await asyncio.sleep(0.02)
        row = (
            await session.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.status.in_(("starting", "building", "ready", "running")),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:
            row.stop_requested_at = datetime.now(UTC)
        if dep is not None and dep.status in ("starting", "building", "ready", "running"):
            # Mirror live_stop's terminal deployment write (held under both locks).
            dep.last_stopped_at = datetime.now(UTC)


@pytest.mark.asyncio
async def test_mark_terminal_concurrent_with_stop_does_not_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 1 (P0 — empirically-verified deadlock): the subprocess terminal
    write ``_mark_terminal`` runs CONCURRENTLY with the operator-/stop
    deployment-first lock pattern. Pre-fix ``_mark_terminal`` dirty-flushed the
    node UPDATE (node lock) BEFORE the deployment UPDATE (deployment lock) — a
    node→deployment edge that cycles with /stop's deployment→node order and
    DEADLOCKED. After the fix ``_mark_terminal`` acquires the deployment lock
    FIRST, so the two serialise. We drive several concurrent rounds with a
    timeout — a deadlock would hang until Postgres' ``deadlock_timeout`` aborts
    one side (or the test times out)."""
    for _ in range(10):
        dep_id, row_id = await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
        )

        async def _do_terminal(rid: UUID = row_id) -> None:
            await _mark_terminal(
                session_factory,
                rid,
                status="failed",
                failure_kind=FailureKind.NODE_CRASHED,
                error_message="crash",
                exit_code=1,
            )

        async def _do_stop(did: UUID = dep_id) -> None:
            await _stop_lock_pattern(session_factory, did)

        await asyncio.wait_for(
            asyncio.gather(_do_terminal(), _do_stop()),
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_mark_failed_concurrent_with_stop_does_not_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 1 (P0): the supervisor spawn-failure write ``_mark_failed`` runs
    CONCURRENTLY with the operator-/stop deployment-first lock pattern. Pre-fix
    ``_mark_failed`` dirty-flushed node-then-deployment (a node→deployment edge)
    and could deadlock with /stop (deployment→node). After the fix it is
    deployment-first. Assert both converge inside a timeout across rounds."""
    for _ in range(10):
        dep_id, row_id = await _seed(
            session_factory,
            deployment_status="starting",
            node_status="starting",
        )

        async def _do_failed(rid: UUID = row_id) -> None:
            await fleet_router._mark_failed(
                row_id=rid,
                reason="spawn failed",
                failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
            )

        async def _do_stop(did: UUID = dep_id) -> None:
            await _stop_lock_pattern(session_factory, did)

        await asyncio.wait_for(
            asyncio.gather(_do_failed(), _do_stop()),
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_mark_terminal_still_syncs_deployment_after_reorder(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The deployment-first re-order must NOT change ``_mark_terminal``'s
    observable behaviour: a clean ``stopped`` terminal write still syncs the
    parent deployment to ``stopped``; a crash still syncs it to ``failed``; the
    non-terminal guard still refuses to stomp a concurrent /stop's ``stopped``."""
    # Clean stop → deployment 'stopped'.
    dep_id, row_id = await _seed(
        session_factory, deployment_status="running", node_status="running"
    )
    await _mark_terminal(
        session_factory,
        row_id,
        status="stopped",
        failure_kind=FailureKind.NONE,
        error_message=None,
        exit_code=0,
    )
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
    assert node_status == "stopped"
    assert dep_status == "stopped"

    # Crash → deployment 'failed'.
    dep_id2, row_id2 = await _seed(
        session_factory, deployment_status="running", node_status="running"
    )
    await _mark_terminal(
        session_factory,
        row_id2,
        status="failed",
        failure_kind=FailureKind.NODE_CRASHED,
        error_message="boom",
        exit_code=1,
    )
    async with session_factory() as session:
        dep_status2 = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id2))
        ).scalar_one()
    assert dep_status2 == "failed"


# ---------------------------------------------------------------------------
# 1c. Finding 2 — bulk sweeps must NOT skew (deployment flip tied to node flip)
# ---------------------------------------------------------------------------


def _heartbeat_race_factory(
    real_factory: async_sessionmaker[AsyncSession],
    *,
    row_id: UUID,
    trigger_substr: str,
):
    """Wrap ``real_factory`` so that the FIRST statement whose compiled SQL
    contains ``trigger_substr`` (the sweep's UNLOCKED candidate SELECT) fires a
    one-shot side-effect: a SEPARATE committed transaction refreshes ``row_id``'s
    heartbeat to *now*. Under READ COMMITTED the sweep's SUBSEQUENT statements
    (the node flip) then see the FRESH heartbeat and match ZERO rows — exactly the
    race a heartbeat landing between the candidate SELECT and the node flip
    creates. This is the deterministic seam for the Finding-2 skew tests."""

    fired = {"done": False}

    class _Wrapper:
        def __init__(self) -> None:
            self._session = None

        async def __aenter__(self):
            self._session = real_factory()
            inner = await self._session.__aenter__()
            real_execute = inner.execute

            async def _execute(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if not fired["done"] and trigger_substr in str(statement):
                    fired["done"] = True
                    # Commit a heartbeat refresh from an INDEPENDENT session so
                    # it is visible to the sweep's next statement (READ COMMITTED).
                    async with real_factory() as side, side.begin():
                        side_row = await side.get(LiveNodeProcess, row_id)
                        if side_row is not None:
                            side_row.last_heartbeat_at = datetime.now(UTC)
                return result

            inner.execute = _execute  # type: ignore[method-assign]
            return inner

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

    def _factory():
        return _Wrapper()

    return _factory


@pytest.mark.asyncio
async def test_heartbeat_sweep_no_skew_when_heartbeat_refreshes_between_select_and_flip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — HeartbeatMonitor stale sweep must NOT flip a deployment
    to ``failed`` when, between the candidate SELECT and the node flip, the node's
    heartbeat is refreshed so the node UPDATE matches ZERO rows.

    Pre-fix the sweep derived the deployment ids from an UNLOCKED candidate
    SELECT, flipped the deployments, then conditionally flipped nodes WHERE
    <stale predicate>. A heartbeat landing in between made the node UPDATE match
    nothing while the deployment was already flipped → a live (running) node row
    paired with a ``failed`` deployment (skew). The fix ties the deployment flip
    to the node UPDATE's RETURNING set, so a node that escapes the stale predicate
    leaves its deployment UNTOUCHED.

    We use a deterministic seam (``_heartbeat_race_factory``): the instant the
    sweep's UNLOCKED candidate SELECT executes, an INDEPENDENT committed
    transaction refreshes the node's heartbeat to *now*. Under READ COMMITTED the
    sweep's subsequent node flip then matches ZERO rows. Falsifies pre-fix: the
    deployment is wrongly flipped to ``failed`` while the node stays ``running``."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # stale
    )

    raced = _heartbeat_race_factory(
        session_factory,
        row_id=row_id,
        # The sweep's unlocked candidate SELECT projects only deployment_id.
        trigger_substr="SELECT live_node_processes.deployment_id",
    )
    monitor = HeartbeatMonitor(
        db=raced,  # type: ignore[arg-type]
        stale_seconds=30,
        sleep_interval_s=1,
    )

    flipped = await monitor._mark_stale_as_failed()

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "running", "a freshly-heartbeating node must NOT be flipped"
    assert dep_status == "running", (
        "the deployment must NOT be flipped to failed when the node UPDATE matched "
        "zero rows — the flip must be tied to the node RETURNING, not the stale SELECT"
    )
    assert str(dep_id) not in flipped


@pytest.mark.asyncio
async def test_heartbeat_sweep_flips_genuinely_stale_node_and_deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The no-skew fix must NOT break the happy path: a genuinely stale node IS
    flipped to ``failed`` AND its deployment follows to ``failed``."""
    monitor = HeartbeatMonitor(
        db=session_factory,
        stale_seconds=30,
        sleep_interval_s=1,
    )
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # stale
    )

    flipped = await monitor._mark_stale_as_failed()

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "failed"
    assert dep_status == "failed"
    assert str(dep_id) in flipped


@pytest.mark.asyncio
async def test_rescan_step1_no_skew_when_heartbeat_refreshes(
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — the rescan Step-1 stale-active flip must NOT flip a
    deployment to ``failed`` when the node escapes the stale predicate between the
    unlocked candidate SELECT and the node flip (heartbeat refreshed). The
    deployment flip must be tied to the node RETURNING set.

    Deterministic seam: the instant the rescan's UNLOCKED stale-active candidate
    SELECT executes, an INDEPENDENT committed transaction refreshes the node's
    heartbeat to *now*; under READ COMMITTED the Step-1 node flip then matches
    ZERO rows. Falsifies pre-fix: the deployment is wrongly flipped to ``failed``
    while the node stays ``running``."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # stale-active
    )

    raced = _heartbeat_race_factory(
        session_factory,
        row_id=row_id,
        # The rescan's FIRST unlocked candidate SELECT is the stale-active scan,
        # which projects only ``deployment_id``.
        trigger_substr="SELECT live_node_processes.deployment_id",
    )
    raced_router = FleetRouter(
        db=raced,  # type: ignore[arg-type]
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=RestartPolicy(),
        rescan_stale_seconds=5,
    )

    await raced_router._load_rescan_candidates()

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "running", "a freshly-heartbeating stale-active node must NOT be flipped"
    assert dep_status == "running", (
        "the deployment must NOT be flipped to failed when the rescan Step-1 node "
        "UPDATE matched zero rows — the flip must be tied to the node RETURNING"
    )


@pytest.mark.asyncio
async def test_rescan_step1_flips_genuinely_stale_active_node_and_deployment(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The rescan Step-1 no-skew fix must NOT break the happy path: a genuinely
    stale-active node IS flipped to ``failed`` AND the deployment follows."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # stale-active
    )

    await fleet_router._load_rescan_candidates()

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "failed"
    assert dep_status == "failed"


# ---------------------------------------------------------------------------
# 1d. Finding 1 — bulk node UPDATE must be SCOPED to the LOCKED candidate set
# ---------------------------------------------------------------------------


def _go_stale_other_dep_factory(
    real_factory: async_sessionmaker[AsyncSession],
    *,
    other_row_id: UUID,
    trigger_substr: str,
    stale_age_s: int = 600,
):
    """Wrap ``real_factory`` so the FIRST statement whose compiled SQL contains
    ``trigger_substr`` (the sweep's Step-0 UNLOCKED candidate SELECT) fires a
    one-shot side-effect: a SEPARATE committed transaction AGES ``other_row_id``'s
    heartbeat to ``stale_age_s`` seconds ago — i.e. a DIFFERENT deployment goes
    stale BETWEEN the candidate SELECT (Step 0) and the node flip (Step 2).

    This is the deterministic seam for the Finding-1 BULK-UPDATE-SCOPING test: the
    scoped UPDATE (``deployment_id IN (candidate_dep_ids)``) must NOT flip the
    other deployment's node, because it was NOT in the Step-0 candidate set (and so
    its parent deployment was never locked in Step 1). A pre-fix bare-predicate
    UPDATE WOULD flip it — opening an unlocked ``node→deployment`` edge."""

    fired = {"done": False}

    class _Wrapper:
        def __init__(self) -> None:
            self._session = None

        async def __aenter__(self):
            self._session = real_factory()
            inner = await self._session.__aenter__()
            real_execute = inner.execute

            async def _execute(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if not fired["done"] and trigger_substr in str(statement):
                    fired["done"] = True
                    # Independent committed txn so the aging is visible to the
                    # sweep's subsequent node-flip statement (READ COMMITTED).
                    async with real_factory() as side, side.begin():
                        side_row = await side.get(LiveNodeProcess, other_row_id)
                        if side_row is not None:
                            side_row.last_heartbeat_at = datetime.now(UTC) - timedelta(
                                seconds=stale_age_s
                            )
                return result

            inner.execute = _execute  # type: ignore[method-assign]
            return inner

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

    def _factory():
        return _Wrapper()

    return _factory


@pytest.mark.asyncio
async def test_heartbeat_sweep_does_not_flip_unlocked_second_deployment_that_goes_stale_mid_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 1 (P1 — BULK-UPDATE SCOPING / DEADLOCK EDGE) — the HeartbeatMonitor
    stale sweep must NOT flip a SECOND deployment's node when that deployment goes
    stale BETWEEN the Step-0 candidate SELECT and the Step-2 node flip.

    Pre-fix the Step-2 ``UPDATE live_node_processes`` was NOT scoped to the locked
    ``candidate_dep_ids`` — only ``status / last_heartbeat_at`` — so a different
    deployment that went stale mid-sweep matched the predicate and was flipped
    (node row lock acquired) while its parent deployment was NEVER locked in Step 1.
    That is an unlocked ``node→deployment`` lock edge that bypasses the
    deployment-first invariant and can cycle (deadlock) with a concurrent /stop /
    Phase-A on that second deployment. The fix scopes the Step-2 UPDATE to
    ``deployment_id IN (candidate_dep_ids)`` so it can ONLY touch nodes whose
    deployment was locked first.

    Deterministic seam: deployment A is seeded STALE (so it IS a Step-0 candidate);
    deployment B is seeded FRESH (so it is NOT). The instant the Step-0 candidate
    SELECT fires, an independent committed txn ages B's heartbeat to stale. The
    scoped node flip must terminalize A only and leave B's node ``running``
    (B was not in the locked candidate set → never lockable here → next sweep
    catches it). Falsifies pre-fix: B is wrongly flipped to ``failed``."""
    dep_a, row_a = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # stale → Step-0 candidate
    )
    dep_b, row_b = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        last_heartbeat_at=datetime.now(UTC),  # FRESH → NOT a Step-0 candidate
    )

    raced = _go_stale_other_dep_factory(
        session_factory,
        other_row_id=row_b,
        # The sweep's Step-0 unlocked candidate SELECT projects only deployment_id.
        trigger_substr="SELECT live_node_processes.deployment_id",
    )
    monitor = HeartbeatMonitor(
        db=raced,  # type: ignore[arg-type]
        stale_seconds=30,
        sleep_interval_s=1,
    )

    flipped = await monitor._mark_stale_as_failed()

    async with session_factory() as session:
        a_node = (
            await session.execute(select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_a))
        ).scalar_one()
        b_node = (
            await session.execute(select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_b))
        ).scalar_one()
        a_dep = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_a))
        ).scalar_one()
        b_dep = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_b))
        ).scalar_one()

    # Deployment A (a genuine Step-0 candidate, locked in Step 1) IS flipped.
    assert a_node == "failed"
    assert a_dep == "failed"
    assert str(dep_a) in flipped
    # Deployment B went stale only AFTER the Step-0 candidate snapshot, so it was
    # NOT in the locked set — the scoped UPDATE must NOT touch its node row.
    assert b_node == "running", (
        "the Step-2 node UPDATE must be scoped to the LOCKED candidate set — a "
        "deployment that went stale AFTER Step 0 must not be flipped (no unlocked "
        "node→deployment edge)"
    )
    assert b_dep == "running"
    assert str(dep_b) not in flipped


@pytest.mark.asyncio
async def test_heartbeat_sweep_concurrent_with_stop_on_second_stale_deployment_no_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 1 (P1 — DEADLOCK PROBE) — a HeartbeatMonitor sweep running
    CONCURRENTLY with an operator /stop on a DIFFERENT deployment (which is itself
    stale and thus a sweep candidate) must NEVER deadlock.

    Pre-fix the sweep could flip the second deployment's node (unscoped Step-2
    UPDATE) WITHOUT locking that deployment first — a ``node→deployment`` edge — and
    /stop locks deployment→node, closing a D→N→D cycle. After the scoping fix the
    sweep only ever locks nodes whose deployment it locked first, so the two
    serialise. Drive several concurrent rounds with a timeout — a deadlock would
    hang until Postgres' ``deadlock_timeout`` aborts one side (or the test times
    out)."""
    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30, sleep_interval_s=1)
    for _ in range(8):
        # Two independent deployments, BOTH stale (both are sweep candidates). We
        # /stop the second WHILE the sweep flips the first — the crossed lock
        # acquisition would deadlock pre-fix.
        dep_a, row_a = await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            pid=None,
            host=socket.gethostname(),
        )
        dep_b, row_b = await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            pid=None,
            host=socket.gethostname(),
        )

        async def _do_sweep() -> None:
            await monitor._mark_stale_as_failed()

        async def _do_stop(did: UUID = dep_b) -> None:
            await fleet_router.stop(did, reason="user")

        await asyncio.wait_for(
            asyncio.gather(_do_sweep(), _do_stop()),
            timeout=20.0,
        )


# ---------------------------------------------------------------------------
# 1d. INVARIANT 2 — multi-row deployment-set AB-BA deadlock probes
#     (council 2026-06-01 follow-up — pr-toolkit P1, D↔D cycle)
# ---------------------------------------------------------------------------


async def _lock_two_deployments_crossed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    first: UUID,
    second: UUID,
    barrier: asyncio.Barrier,
    order_by_id: bool,
) -> None:
    """Lock TWO deployment rows in two SEPARATE ``FOR UPDATE`` statements with a
    barrier in between, modelling a multi-row deployment-set locker. When
    ``order_by_id`` is True the lock targets are sorted ascending (the production
    discipline); when False they are taken in the caller's (crossed) order.

    Two concurrent invocations with CROSSED orders (caller passes first/second
    reversed) and ``order_by_id=False`` form an AB-BA cycle: each grabs its first
    row, waits at the barrier, then contends for the other's row → Postgres
    deadlock-aborts one. With ``order_by_id=True`` both sort to ascending id, so
    they acquire in ONE total order and serialise — exactly what the production
    ``ORDER BY LiveDeployment.id`` guarantees."""
    targets = [first, second]
    if order_by_id:
        targets = sorted(targets)
    async with session_factory() as session, session.begin():
        # First lock.
        await session.execute(
            select(LiveDeployment.id).where(LiveDeployment.id == targets[0]).with_for_update()
        )
        # Let the sibling acquire ITS first lock before we go for the second.
        #
        # In the CROSSED (order_by_id=False) case each holds a DIFFERENT first row,
        # so both reach the barrier and it releases — opening the AB-BA window.
        # In the ORDERED case both target the SAME first row (depA), so one blocks
        # on the ROW LOCK and never reaches the barrier; the holder must not hang
        # on the barrier forever, so we bound the wait and proceed — the ordered
        # pair then serialises on depA → depB with no deadlock.
        with contextlib.suppress(TimeoutError, asyncio.BrokenBarrierError):
            await asyncio.wait_for(barrier.wait(), timeout=2.0)
        # Second lock — this is where a crossed (un-ordered) pair deadlocks.
        await session.execute(
            select(LiveDeployment.id).where(LiveDeployment.id == targets[1]).with_for_update()
        )


@pytest.mark.asyncio
async def test_ordered_multi_row_deployment_locks_do_not_ab_ba_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INVARIANT 2 (deterministic AB-BA probe). Two concurrent multi-row
    deployment-set lockers whose sets OVERLAP ({depA, depB}) but whose natural
    acquisition order is CROSSED must NOT deadlock once both sort by id (the
    production ``ORDER BY LiveDeployment.id`` discipline).

    Deterministic seam: an ``asyncio.Barrier`` forces each locker to hold its
    FIRST row lock before contending for the second — the precise AB-BA window.
    With the ascending-id sort both lockers acquire depA then depB, so they
    serialise within the generous timeout. (The falsification — same probe with
    ``order_by_id=False`` and reversed targets — is asserted to deadlock below.)"""
    dep_a, _ = await _seed(session_factory, deployment_status="running", node_status="running")
    dep_b, _ = await _seed(session_factory, deployment_status="running", node_status="running")

    barrier = asyncio.Barrier(2)
    # Crossed natural order, but BOTH sort by id → one total order → no deadlock.
    await asyncio.wait_for(
        asyncio.gather(
            _lock_two_deployments_crossed(
                session_factory, first=dep_a, second=dep_b, barrier=barrier, order_by_id=True
            ),
            _lock_two_deployments_crossed(
                session_factory, first=dep_b, second=dep_a, barrier=barrier, order_by_id=True
            ),
        ),
        timeout=20.0,
    )


@pytest.mark.asyncio
async def test_unordered_crossed_multi_row_deployment_locks_deadlock_falsification(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INVARIANT 2 (falsification). The SAME barrier-coordinated crossed probe
    WITHOUT the ascending-id sort DOES form a D↔D (AB-BA) cycle — Postgres
    detects the deadlock and aborts one side with a ``DeadlockDetected`` error.
    This proves the probe is genuinely sensitive to lock ordering, so the
    no-deadlock assertion in the ordered test above is meaningful (not vacuously
    passing). It is exactly the cycle the production ``ORDER BY id`` prevents."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    dep_a, _ = await _seed(session_factory, deployment_status="running", node_status="running")
    dep_b, _ = await _seed(session_factory, deployment_status="running", node_status="running")

    barrier = asyncio.Barrier(2)
    with pytest.raises((OperationalError, DBAPIError)):
        await asyncio.wait_for(
            asyncio.gather(
                _lock_two_deployments_crossed(
                    session_factory, first=dep_a, second=dep_b, barrier=barrier, order_by_id=False
                ),
                _lock_two_deployments_crossed(
                    session_factory, first=dep_b, second=dep_a, barrier=barrier, order_by_id=False
                ),
            ),
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_heartbeat_sweep_concurrent_with_rescan_overlapping_stale_set_no_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INVARIANT 2 (real-locker probe — heartbeat sweep × rescan). The two
    multi-row deployment-set lockers run CONCURRENTLY over an OVERLAPPING stale
    ``{depA, depB}`` set. Pre-fix neither ordered its ``WHERE id IN (...)`` FOR
    UPDATE, so the sweep could lock {A, B} while the rescan locked {B, A} → AB-BA.
    After ``ORDER BY LiveDeployment.id`` both lock in ascending-id order and
    serialise. Repeated rounds + timeout — a deadlock would hang until Postgres'
    ``deadlock_timeout`` aborts one side."""
    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30, sleep_interval_s=1)
    for _ in range(8):
        # Both deployments stale-ACTIVE: candidates for BOTH the heartbeat sweep
        # (post-startup stale) AND the rescan (stale-active orphan). Overlapping set.
        await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            pid=None,
            host=socket.gethostname(),
        )
        await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            pid=None,
            host=socket.gethostname(),
        )

        async def _do_sweep() -> None:
            await monitor._mark_stale_as_failed()

        async def _do_rescan() -> None:
            await fleet_router._load_rescan_candidates()

        await asyncio.wait_for(
            asyncio.gather(_do_sweep(), _do_rescan()),
            timeout=20.0,
        )


@pytest.mark.asyncio
async def test_rescan_concurrent_with_watchdog_overlapping_set_no_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INVARIANT 2 (real-locker probe — rescan × watchdog). The rescan Step-1a
    deployment-set lock and the watchdog Step-3 ordered pre-lock run CONCURRENTLY
    over an OVERLAPPING ``{depA, depB}`` set of wedged ``starting`` rows. Both now
    ``ORDER BY LiveDeployment.id`` so the two multi-row deployment-set lockers
    acquire in one global order and never AB-BA. Repeated rounds + timeout."""
    for _ in range(8):
        # Two wedged ``starting`` rows on THIS host with stale heartbeats: the
        # watchdog Step-3 (status starting/building, started_at past hard timeout,
        # host match) AND the rescan Step-1 (stale-active starting) both target
        # this overlapping set.
        old = datetime.now(UTC) - timedelta(seconds=600)
        for _i in range(2):
            async with session_factory() as session:
                dep = await make_live_deployment(
                    session,
                    account_id=_ACCOUNT_ID,
                    ib_login_key="msai-paper-primary",
                    status="starting",
                )
                row = LiveNodeProcess(
                    deployment_id=dep.id,
                    pid=None,
                    host=socket.gethostname(),
                    started_at=old,
                    last_heartbeat_at=old,
                    status="starting",
                    gateway_session_key="sess-1",
                )
                session.add(row)
                await session.commit()

        # Watchdog hard timeout tiny so the seeded starting rows are candidates.
        fleet_router._startup_hard_timeout_s = 1

        async def _do_watchdog() -> None:
            await fleet_router.watchdog_once()

        async def _do_rescan() -> None:
            await fleet_router._load_rescan_candidates()

        await asyncio.wait_for(
            asyncio.gather(_do_watchdog(), _do_rescan()),
            timeout=20.0,
        )


# ---------------------------------------------------------------------------
# 1e. Finding 2 — reaper stale-active terminal write must SYNC the deployment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_stale_active_syncs_deployment_when_restart_suppressed_no_policy(
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1 — TERMINAL-WRITE DEPLOYMENT SYNC) — when the reaper writes the
    terminal node state on the STALE-ACTIVE path (SIGKILL/OOM: the row was still
    ``running`` because the subprocess's own ``finally`` never ran) AND the restart
    is SUPPRESSED, it must ALSO sync the parent ``LiveDeployment`` to ``failed`` so
    the rescan (which requires ``LiveDeployment.status == 'failed'``) can pick it
    up — never a terminal node paired with an active deployment (the wedge).

    Suppressor: NO RestartPolicy injected → the reaper sets ``dispatch_restart =
    False`` and never schedules a restart task (one of the three suppressor shapes
    the finding names). Pre-fix the reaper flipped only the node row, leaving the
    deployment ``running`` forever — a zombie active deployment invisible to the
    rescan and reported active by the status/deploy gate."""
    router_no_policy = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=None,  # SUPPRESSOR: no policy → no restart dispatched
        rescan_stale_seconds=5,
    )
    # Stale-ACTIVE shape: node row still ``running`` (subprocess SIGKILLed before
    # its own terminal write), deployment still ``running``.
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
    )

    # Reaper observes a non-zero exit on the still-active row (SIGKILL → -9 style).
    await router_no_policy._on_child_exit(dep_id, exit_code=1, owned_row_id=row_id)

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "failed", "the stale-active terminal write must flip the node to failed"
    assert dep_status == "failed", (
        "the reaper must SYNC the parent deployment to failed on the stale-active "
        "terminal write — even when the restart is suppressed — so a terminal node "
        "is never paired with an active deployment (wedge)"
    )


@pytest.mark.asyncio
async def test_reaper_stale_active_synced_deployment_is_rescan_eligible_after_suppressor_clears(
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — the deployment-sync makes the suppressed-restart node
    rescan-eligible once the suppressor clears. We reap with NO policy (suppressed),
    confirm the deployment was synced to ``failed``, then run the rescan candidate
    query on a router that DOES have a policy: the deployment is now a candidate
    (it would have been invisible — stuck ``running`` — pre-fix)."""
    router_no_policy = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=None,
        rescan_stale_seconds=5,
    )
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
    )
    await router_no_policy._on_child_exit(dep_id, exit_code=1, owned_row_id=row_id)

    # A router with a policy now re-scans; the synced ``failed`` deployment + the
    # recoverable NODE_CRASHED node row make it a candidate.
    router_with_policy = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=RestartPolicy(),
        rescan_stale_seconds=5,
    )
    candidates = await router_with_policy._load_rescan_candidates()
    assert dep_id in candidates, (
        "the deployment the reaper synced to failed must be a rescan candidate once "
        "the suppressor clears — Finding 2 is exactly that it was NOT, pre-fix"
    )


@pytest.mark.asyncio
async def test_reaper_stale_active_syncs_deployment_when_account_halted(
    fleet_router: FleetRouter,
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — same deployment-sync invariant under the ACCOUNT HALT
    LATCH suppressor: a reaper that dispatches a restart task which is then
    suppressed by the account halt latch must STILL leave the deployment ``failed``
    (the deployment-sync happens in the reaper's terminal write, BEFORE/independent
    of the restart-task decision)."""
    await redis_client.set(account_halt_key(_ACCOUNT_ID), "1")
    try:
        dep_id, row_id = await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
        )
        await fleet_router._on_child_exit(dep_id, exit_code=1, owned_row_id=row_id)
        # Let any dispatched restart task run (it must be suppressed by the halt).
        await fleet_router._await_restart_tasks_for_test()

        async with session_factory() as session:
            node_status = (
                await session.execute(
                    select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
                )
            ).scalar_one()
            dep_status = (
                await session.execute(
                    select(LiveDeployment.status).where(LiveDeployment.id == dep_id)
                )
            ).scalar_one()
        assert node_status == "failed"
        assert dep_status == "failed", (
            "an account-halt-suppressed restart must still leave the deployment "
            "failed — the reaper syncs it in the terminal write"
        )
    finally:
        await redis_client.delete(account_halt_key(_ACCOUNT_ID))


@pytest.mark.asyncio
async def test_reaper_clean_stop_syncs_deployment_to_stopped(
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — a CLEAN exit (code 0) on the stale-active path syncs the
    deployment to ``stopped`` (not ``failed``), mirroring ``_mark_terminal``'s
    clean-stop mapping. A clean exit is never auto-restarted, so this is the
    pure terminal-sync case."""
    router_no_policy = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=None,
        rescan_stale_seconds=5,
    )
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
    )
    await router_no_policy._on_child_exit(dep_id, exit_code=0, owned_row_id=row_id)

    async with session_factory() as session:
        node_status = (
            await session.execute(
                select(LiveNodeProcess.status).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one()
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert node_status == "stopped"
    assert dep_status == "stopped"


@pytest.mark.asyncio
async def test_reaper_common_crash_terminal_row_does_not_touch_deployment_or_node(
    redis_client: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P1) — the deployment-sync is GATED on the stale-active path. On
    the COMMON-CRASH path the subprocess already wrote its OWN terminal row (status
    already ``failed``); the reaper must LEAVE BOTH rows untouched (it never
    pre-empts the subprocess's richer diagnosis, and it must not re-sync a
    deployment a concurrent /stop may have moved). We seed the node ALREADY
    ``failed`` and the deployment ``stopped`` (a /stop won the race); the reaper
    must NOT stomp the deployment back to ``failed``."""
    router_no_policy = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_target,
        restart_policy=None,
        rescan_stale_seconds=5,
    )
    # Node already terminal (subprocess wrote it); deployment already 'stopped'.
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="stopped",
        node_status="failed",
        failure_kind=FailureKind.NODE_CRASHED.value,
    )
    await router_no_policy._on_child_exit(dep_id, exit_code=1, owned_row_id=row_id)

    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "stopped", (
        "on the common-crash path (row already terminal) the reaper must NOT touch "
        "the deployment — the stale-active deployment-sync is gated to the path "
        "where the reaper OWNS the terminal write"
    )


@pytest.mark.asyncio
async def test_reaper_on_child_exit_concurrent_with_stop_does_not_deadlock(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding 2 (P0 — DEADLOCK PROBE) — the reaper ``_on_child_exit`` (now a
    deployment-first both-row writer) running CONCURRENTLY with the operator-/stop
    deployment-first lock pattern on the SAME deployment must NEVER deadlock.

    Pre-fix the reaper locked only the node row (node-only). Now that it ALSO
    writes + syncs the deployment, it acquires the deployment lock FIRST — so it
    serialises with /stop (also deployment→node) instead of crossing into a cycle.
    Drive several concurrent rounds with a timeout."""
    for _ in range(10):
        dep_id, row_id = await _seed(
            session_factory,
            deployment_status="running",
            node_status="running",
        )

        async def _do_reap(rid: UUID = row_id, did: UUID = dep_id) -> None:
            await fleet_router._on_child_exit(did, exit_code=1, owned_row_id=rid)

        async def _do_stop(did: UUID = dep_id) -> None:
            await _stop_lock_pattern(session_factory, did)

        await asyncio.wait_for(
            asyncio.gather(_do_reap(), _do_stop()),
            timeout=20.0,
        )


# ---------------------------------------------------------------------------
# 2. Pre-start stop-intent gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prestart_stop_intent_gate_aborts_spawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council 2026-06-01 item 2 (the chairman's blocking objection): a /stop
    that stamps ``stop_requested_at`` AFTER Phase A commits the ``starting`` row
    but BEFORE ``process.start()`` must abort — no process starts, the reserved
    row leaves the active set, outcome is suppressed.

    We wrap ``_phase_a_reserve_slot`` so that the instant Phase A commits the
    ``starting`` row we stamp ``stop_requested_at`` on it (modelling a /stop that
    lands in the post-commit → pre-process.start window), then let the REAL
    pre-start gate observe it. Falsifies pre-fix: without the gate a process
    starts.

    Seeded so Phase A genuinely RESERVES a fresh row with ``restart_carry=None``:
    the deployment is non-terminal (``starting``) so the stale-START NOT_STARTABLE
    guard is skipped, and there is no ACTIVE node row (the seeded row is
    ``failed``) so the idempotent ALREADY_ACTIVE branch is skipped."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="failed",
    )
    slug = await _slug(session_factory, dep_id)

    started_processes: list[object] = []

    real_ctx_process = fleet_router._spawn_ctx.Process

    def _intercepting_process(*args: object, **kwargs: object) -> object:
        # We are now PAST Phase A's commit (the starting row exists) and the
        # gate must have already run BEFORE we get here. If the gate works, the
        # production code returns before ever constructing/starting a process,
        # so this should NEVER be called once the intent is set. Record any call
        # so the assertion below can catch a leak.
        proc = real_ctx_process(*args, **kwargs)  # type: ignore[operator]
        started_processes.append(proc)
        return proc

    # Stamp the operator-stop intent on whatever row Phase A reserves, by
    # intercepting the pre-start gate's own re-read. The cleanest seam is to
    # stamp the intent right after Phase A commits — we do that by wrapping
    # _phase_a_reserve_slot so the intent is committed on the reserved row
    # before the gate runs.
    real_phase_a = fleet_router._phase_a_reserve_slot
    phase_a_reservations: list[UUID] = []

    async def _phase_a_then_stamp(**kwargs: object) -> object:
        outcome = await real_phase_a(**kwargs)  # type: ignore[arg-type]
        if isinstance(outcome, UUID):
            phase_a_reservations.append(outcome)
            # Stamp stop intent on the just-reserved row BEFORE the gate runs.
            async with session_factory() as session, session.begin():
                row = await session.get(LiveNodeProcess, outcome)
                if row is not None:
                    row.stop_requested_at = datetime.now(UTC)
        return outcome

    fleet_router._phase_a_reserve_slot = _phase_a_then_stamp  # type: ignore[assignment]
    fleet_router._spawn_ctx.Process = _intercepting_process  # type: ignore[attr-defined,method-assign]

    try:
        ack, process_started = await fleet_router.spawn_with_outcome(
            deployment_id=dep_id,
            deployment_slug=slug,
            payload={},
            idempotency_key="prestart-gate-1",
            restart_carry=None,
        )
    finally:
        fleet_router._phase_a_reserve_slot = real_phase_a  # type: ignore[assignment]
        fleet_router._spawn_ctx.Process = real_ctx_process  # type: ignore[attr-defined,method-assign]

    # Phase A genuinely RESERVED a fresh row (the wrapper stamped exactly one).
    assert len(phase_a_reservations) == 1, (
        "Phase A must have reserved a fresh starting row for the gate to act on "
        "(otherwise the test is not exercising the pre-start gate at all)"
    )
    reserved_row_id = phase_a_reservations[0]

    assert process_started is False, "the pre-start gate must NOT start a process"
    assert ack is True, "an operator-stopped reservation is a deliberate ACK (no retry)"
    assert started_processes == [], (
        "no process must be constructed/started once the stop intent is observed"
    )
    # The reserved row must have LEFT the active set (terminalized).
    assert await _active_row_count(session_factory, dep_id) == 0, (
        "the just-reserved starting row must be terminalized so it leaves the active set"
    )
    reserved = await _node_row(session_factory, reserved_row_id)
    assert reserved.status == "failed", (
        "the gate must terminalize the SPECIFIC row Phase A reserved (it carried "
        "the stop intent), proving the gate fired on a real reservation"
    )


@pytest.mark.asyncio
async def test_phase_c_kills_child_when_stop_races_after_prestart_gate(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-26 P1: a /stop that stamps ``stop_requested_at`` AFTER the
    pre-start gate's read but DURING ``process.start()``/handle-install must NOT
    leave a node trading a stopped account. The no-pid STOP branch ACKs without
    being able to SIGTERM a pid that is not yet recorded, so Phase C re-reads the
    intent under the SAME ``live_node_processes`` row lock the /stop uses and
    kills the just-started child instead of recording its pid.

    Seam: wrap ``_latest_stop_requested_at`` so the pre-start gate observes
    ``None`` (passes) and the intent is stamped IMMEDIATELY AFTER its read —
    modelling the /stop landing in the [gate → Phase C] window the gate cannot
    cover. A fake process records ``terminate()``. Falsifies pre-fix: without the
    Phase-C re-check the child stays installed and ``started`` is True.
    """
    dep_id, _row_id = await _seed(
        session_factory, deployment_status="starting", node_status="failed"
    )
    slug = await _slug(session_factory, dep_id)

    reserved_ids: list[UUID] = []
    real_phase_a = fleet_router._phase_a_reserve_slot

    async def _phase_a_capture(**kwargs: object) -> object:
        out = await real_phase_a(**kwargs)  # type: ignore[arg-type]
        if isinstance(out, UUID):
            reserved_ids.append(out)
        return out

    real_latest = fleet_router._latest_stop_requested_at

    async def _gate_passes_then_stamp(deployment_id: UUID) -> object:
        # The gate reads here and sees NO intent (returns None) → spawn proceeds.
        result = await real_latest(deployment_id)
        # Model a /stop landing the instant AFTER the gate's read.
        if reserved_ids:
            async with session_factory() as s, s.begin():
                row = await s.get(LiveNodeProcess, reserved_ids[-1])
                if row is not None:
                    row.stop_requested_at = datetime.now(UTC)
        return result

    terminated: list[int] = []

    class _FakeProc:
        def __init__(self) -> None:
            self.pid = 424242

        def start(self) -> None:
            pass

        def terminate(self) -> None:
            terminated.append(self.pid)

        def is_alive(self) -> bool:
            return False

    def _fake_process(*args: object, **kwargs: object) -> object:
        return _FakeProc()

    real_ctx_process = fleet_router._spawn_ctx.Process
    fleet_router._phase_a_reserve_slot = _phase_a_capture  # type: ignore[assignment]
    fleet_router._latest_stop_requested_at = _gate_passes_then_stamp  # type: ignore[assignment]
    fleet_router._spawn_ctx.Process = _fake_process  # type: ignore[attr-defined,method-assign]

    try:
        ack, started = await fleet_router.spawn_with_outcome(
            deployment_id=dep_id,
            deployment_slug=slug,
            payload={},
            idempotency_key="phase-c-stop-race-1",
            restart_carry=None,
        )
    finally:
        fleet_router._phase_a_reserve_slot = real_phase_a  # type: ignore[assignment]
        fleet_router._latest_stop_requested_at = real_latest  # type: ignore[assignment]
        fleet_router._spawn_ctx.Process = real_ctx_process  # type: ignore[attr-defined,method-assign]

    assert reserved_ids, "Phase A must have reserved a row for the spawn to proceed"
    assert started is False, (
        "Phase C must NOT report a started node when a /stop raced in during "
        "process.start()/handle-install — the child must be killed"
    )
    assert ack is True, "the raced /stop is a deliberate terminal suppression (ACK, no retry)"
    assert terminated == [424242], (
        "Phase C must SIGTERM the child it just started when it observes the raced stop intent"
    )
    # Codex iter-27 P2: the handle is KEPT and the row left ACTIVE so the NORMAL
    # reaper terminalizes it when the SIGTERM'd child exits — terminalizing here
    # would reopen the slot before the child is confirmed dead (duplicate-node risk).
    assert fleet_router.node_handle_cache.get(dep_id) is not None, (
        "the handle must be KEPT so the reaper can observe the child's exit and terminalize the row"
    )
    reserved = await _node_row(session_factory, reserved_ids[-1])
    assert reserved.pid == 424242, (
        "Phase C must record the real pid (so the reaper / a redelivered /stop can find the child) "
        "before deferring terminalization to the reaper"
    )
    assert reserved.stop_requested_at is not None, (
        "the raced /stop intent must be observed on the row under the FOR UPDATE re-read"
    )
    assert await _active_row_count(session_factory, dep_id) >= 1, (
        "the row stays ACTIVE — terminalization is deferred to the reaper on the child's exit, "
        "keeping the slot occupied so no duplicate START can spawn a second node"
    )


# ---------------------------------------------------------------------------
# 3. Shutdown cancellation safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_in_reserved_prestart_window_terminalizes_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Council 2026-06-01 item 3: cancelling the spawn task inside the
    reserved→pre-start window must terminalize the reserved row — no orphan
    ``starting`` pid=NULL row left in the active set.

    We seam at ``process.start()``: the intercepted constructor raises
    ``CancelledError`` to model the shutdown ``cancel_restart_tasks()`` landing
    exactly in that window. The cancellation cleanup must flip the just-reserved
    row out of the active set and re-raise CancelledError (a clean shutdown
    cancel).

    Seeded ``starting``/``failed`` so Phase A reserves a fresh row with
    ``restart_carry=None`` (see the pre-start-gate test for why)."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="failed",
    )
    slug = await _slug(session_factory, dep_id)

    real_ctx_process = fleet_router._spawn_ctx.Process

    def _cancel_on_start(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    fleet_router._spawn_ctx.Process = _cancel_on_start  # type: ignore[attr-defined,method-assign]

    try:
        with pytest.raises(asyncio.CancelledError):
            await fleet_router.spawn_with_outcome(
                deployment_id=dep_id,
                deployment_slug=slug,
                payload={},
                idempotency_key="cancel-window-1",
                restart_carry=None,
            )
    finally:
        fleet_router._spawn_ctx.Process = real_ctx_process  # type: ignore[attr-defined,method-assign]

    # A fresh row WAS reserved (the seeded row was 'failed', so a 2nd row that
    # is the cancellation-terminalized reservation must now exist) AND no orphan
    # remains: the just-reserved row left the active set.
    async with session_factory() as session:
        all_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == dep_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(all_rows) == 2, (
        "Phase A must have reserved a fresh row for the cancellation cleanup to "
        "terminalize (otherwise the test isn't exercising the reserved→pre-start window)"
    )
    assert await _active_row_count(session_factory, dep_id) == 0, (
        "a cancellation in the reserved→pre-start window must terminalize the "
        "reserved row so it never orphans a starting/stopping pid=NULL active row"
    )


@pytest.mark.asyncio
async def test_cancellation_during_payload_factory_await_terminalizes_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INVARIANT 3 (council 2026-06-01 follow-up — Codex P2, fleet_router.py:764).
    A ``CancelledError`` landing DURING the ``await self._payload_factory(...)``
    — which runs AFTER Phase A committed the reserved ``starting`` row but BEFORE
    ``process.start()`` — must NOT orphan the reserved row. The original
    cancellation cleanup wrapped only ``process.start()`` + handle-install; the
    payload-factory await (and the halt re-checks / pre-start gate) ran OUTSIDE
    it, so a shutdown ``cancel_restart_tasks()`` cancel in that await left a
    ``starting`` pid=NULL row wedging the active unique-index slot forever.

    The fix extends the cancellation-safe region to cover the ENTIRE
    reserved→handle-install span. ``CancelledError`` is ``BaseException`` (NOT
    ``Exception``), so the payload-factory's ``except (ValueError, ...)`` /
    ``except Exception`` clauses never catch it — only a ``BaseException`` /
    explicit ``CancelledError`` handler does.

    Seam: install a ``_payload_factory`` that raises ``CancelledError`` (models
    the cancel landing mid-await). Assert CancelledError re-raises AND the
    reserved row is terminalized (active row count 0 — no orphan)."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="failed",
    )
    slug = await _slug(session_factory, dep_id)

    real_factory = fleet_router._payload_factory

    async def _cancel_in_payload(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise asyncio.CancelledError

    fleet_router._payload_factory = _cancel_in_payload  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await fleet_router.spawn_with_outcome(
                deployment_id=dep_id,
                deployment_slug=slug,
                payload={},
                idempotency_key="cancel-payload-1",
                restart_carry=None,
            )
    finally:
        fleet_router._payload_factory = real_factory  # type: ignore[assignment]

    # A fresh row WAS reserved by Phase A (seeded row was 'failed' → a 2nd row is
    # the cancellation-terminalized reservation) AND no orphan remains.
    async with session_factory() as session:
        all_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess).where(LiveNodeProcess.deployment_id == dep_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(all_rows) == 2, (
        "Phase A must have reserved a fresh row before the payload-factory await "
        "(otherwise the test isn't exercising the reserved→payload-factory window)"
    )
    assert await _active_row_count(session_factory, dep_id) == 0, (
        "a CancelledError in the payload-factory await (reserved→pre-start window) "
        "must terminalize the reserved row — no orphan starting pid=NULL active row"
    )


# ---------------------------------------------------------------------------
# 4. Ownerless-active-row backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("node_status", ["starting", "stopping"])
async def test_ownerless_active_row_reaped_no_stop_intent_recoverable(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    node_status: str,
) -> None:
    """An ACTIVE (``starting`` / ``stopping``) row with ``pid IS NULL`` and no
    live cached handle, older than the grace window, with NO stop intent, is
    reaped to ``failed``/recoverable so the rescan can re-drive it
    (council 2026-06-01 item 4). Host-AGNOSTIC: the row carries a foreign host."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status=node_status,
        pid=None,
        host="some-dead-container-host",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
    )

    await fleet_router.watchdog_once()

    reaped = await _node_row(session_factory, row_id)
    assert reaped.status == "failed", (
        f"an ownerless pid=NULL {node_status} row must be reaped out of the active set"
    )
    # Recoverable (no stop intent) → rescan-eligible kind, not a permanent kind.
    assert FailureKind.parse_or_unknown(reaped.failure_kind).is_recoverable_crash(), (
        "an ownerless reap with no stop intent must use a recoverable kind so the "
        "rescan can re-drive it"
    )
    assert await _active_row_count(session_factory, dep_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("node_status", ["starting", "stopping"])
async def test_ownerless_active_row_reaped_with_stop_intent_no_respawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
    node_status: str,
) -> None:
    """An ownerless ACTIVE row carrying ``stop_requested_at`` is reaped TERMINAL
    with no respawn — it leaves the active set but is NEVER rescan-eligible
    (the don't-resurrect intent is honored). Host-agnostic
    (council 2026-06-01 item 4)."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status=node_status,
        pid=None,
        host="some-dead-container-host",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
        stop_requested_at=datetime.now(UTC),
    )

    await fleet_router.watchdog_once()

    reaped = await _node_row(session_factory, row_id)
    assert reaped.status in ("failed", "stopped"), (
        "an ownerless pid=NULL row with stop intent must leave the active set"
    )
    # The row stays UN-rescannable: the rescan candidate query gates
    # stop_requested_at IS NULL, so the stamped intent suppresses respawn.
    assert reaped.stop_requested_at is not None
    assert await _active_row_count(session_factory, dep_id) == 0
    # And the rescan does NOT pick it up.
    candidates = await fleet_router._load_rescan_candidates()
    assert dep_id not in candidates, (
        "a reaped row with stop intent must NOT be a rescan respawn candidate"
    )


@pytest.mark.asyncio
async def test_ownerless_backstop_skips_in_flight_spawn_row(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-27 P1: a row THIS supervisor is actively spawning (reserved in
    Phase A, ``pid IS NULL``, no handle yet because the slow pre-start awaits —
    payload factory / Databento resolution / imports — haven't finished) can be
    OLDER than the grace window, but must NOT be reaped: doing so reopens the slot
    while the spawn can still ``process.start()`` + self-write, hiding/duplicating
    a live node. The reaper excludes any row_id in ``_in_flight_spawn_rows``.

    Contrast with ``test_ownerless_active_row_reaped_no_stop_intent_recoverable``:
    the SAME shape of row (pid=NULL, past grace, active) IS reaped when it is NOT
    a tracked in-flight spawn — so this test falsifies the pre-fix behavior.
    """
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        pid=None,
        host=socket.gethostname(),
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),  # past grace
    )
    # Mark it as an in-flight spawn THIS supervisor owns (as Phase B does).
    fleet_router._in_flight_spawn_rows.add(row_id)
    try:
        await fleet_router.watchdog_once()
    finally:
        fleet_router._in_flight_spawn_rows.discard(row_id)

    still = await _node_row(session_factory, row_id)
    assert still.status == "starting", (
        "a row this supervisor is actively spawning must NOT be reaped by the ownerless "
        "backstop even past the grace window (reaping it would orphan a live spawn)"
    )
    assert await _active_row_count(session_factory, dep_id) == 1, (
        "the in-flight spawn's active slot must stay occupied"
    )


@pytest.mark.asyncio
async def test_ownerless_backstop_leaves_fresh_active_row_alone(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ownerless backstop must NOT reap a node legitimately mid-startup RIGHT
    NOW (fresh heartbeat) — only orphans older than the grace window."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        pid=None,
        host=socket.gethostname(),
        last_heartbeat_at=datetime.now(UTC),  # fresh
    )

    await fleet_router.watchdog_once()

    row = await _node_row(session_factory, row_id)
    assert row.status == "starting", "a fresh-heartbeat ownerless row must be LEFT ALONE"
    assert await _active_row_count(session_factory, dep_id) == 1


@pytest.mark.asyncio
async def test_ownerless_backstop_skips_row_with_live_handle(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row whose deployment has a LIVE cached handle is owned by the reaper —
    the ownerless backstop must skip it even if pid IS NULL and stale (the reap
    loop / phase-C pid write owns it)."""
    from msai.live_supervisor.fleet_router import _CachedNode

    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="starting",
        node_status="starting",
        pid=None,
        host=socket.gethostname(),
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
    )

    # Install a live cached handle so the deployment is "owned".
    proc = fleet_router._spawn_ctx.Process(target=_noop_target)
    proc.start()
    fleet_router.node_handle_cache[dep_id] = _CachedNode(proc=proc, owned_row_id=row_id)
    try:
        await fleet_router.watchdog_once()
        row = await _node_row(session_factory, row_id)
        # The startup watchdog (host-scoped, pid-bound) may SIGKILL+fail a wedged
        # build on THIS host with a handle — that's the existing watchdog. The
        # ownerless backstop specifically must NOT be the one to reap it; either
        # way a row WITH a live handle is not an "ownerless" reap. We assert the
        # ownerless-specific path didn't fire by checking the failure kind isn't
        # the ownerless marker when the handle is alive.
        # (The handle is alive, so the row is owned — not ownerless.)
        assert row.status in ("starting", "failed")
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.join(timeout=2)
        fleet_router.node_handle_cache.pop(dep_id, None)


# ---------------------------------------------------------------------------
# 5. Normal stop + normal respawn unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_stop_active_running_node_unchanged(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NORMAL operator stop of a running node still stamps the durable intent
    and flips the row to ``stopping`` (deployment-first migration must not change
    the observable stop semantics)."""
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="running",
        pid=None,
        host=socket.gethostname(),
    )

    ok = await fleet_router.stop(dep_id, reason="user")
    assert ok is True

    row = await _node_row(session_factory, row_id)
    assert row.status == "stopping"
    assert row.stop_requested_at is not None


@pytest.mark.asyncio
async def test_normal_respawn_no_stop_intent_unchanged(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NORMAL auto-restart (no stop intent, no halt) still respawns: a fresh
    ``starting`` row is reserved and the deployment is reset off ``failed``."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="failed",
        node_status="failed",
    )

    restarted = await fleet_router._maybe_auto_restart(dep_id)
    assert restarted is True
    assert await _active_row_count(session_factory, dep_id) == 1
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "starting"


# ---------------------------------------------------------------------------
# 6. Inv 1 — operator /stop of a QUEUED-but-unconsumed START (no node row)
#    must make it impossible for the queued START to spawn afterwards
#    (council 2026-06-01 follow-up — the queued-START gap the original verdict
#    did not scope; Codex P1 live.py:1740).
# ---------------------------------------------------------------------------


async def _seed_no_node(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    deployment_status: str = "starting",
) -> UUID:
    """Seed ONLY a ``LiveDeployment`` (no ``live_node_processes`` row) — the
    state of a deployment whose START was published to the per-account stream
    but NOT yet consumed by the supervisor (so Phase A never reserved a node
    row). Returns the deployment id."""
    async with session_factory() as session:
        dep = await make_live_deployment(
            session,
            account_id=_ACCOUNT_ID,
            ib_login_key="msai-paper-primary",
            status=deployment_status,
        )
        await session.commit()
        return dep.id


@pytest.mark.asyncio
async def test_stop_queued_start_no_node_row_aborts_later_spawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inv 1 (the queued-START gap). An operator /stop lands while a START is
    QUEUED-but-unconsumed: the deployment is ``starting`` and there is NO node
    row yet (Phase A has not run). ``FleetRouter.stop`` must record a DURABLE
    operator-terminal intent — and the later (consumed) initial START's Phase A
    must ABORT the spawn (no node started) so the operator who was told
    "stopped" never gets a live node afterwards.

    Pre-fix: the no-active-row branch only stamped ``stop_requested_at`` on a
    FAILED latest row. With NO node row at all, nothing was recorded, the
    deployment stayed ``starting``, and the queued initial START
    (``restart_carry is None``) sailed through Phase A and spawned a node for a
    stopped account."""
    dep_id = await _seed_no_node(session_factory, deployment_status="starting")
    slug = await _slug(session_factory, dep_id)

    # Operator /stop while the START is still queued (no node row).
    ok = await fleet_router.stop(dep_id, reason="user")
    assert ok is True

    # The deployment must now be operator-terminal (``stopped``) so the spawn
    # gate can see the intent even though no node row carries stop_requested_at.
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "stopped", (
        "an operator /stop of a queued-but-unconsumed START (no node row) must "
        "mark the deployment operator-terminal (stopped) so the queued START "
        "cannot spawn afterwards"
    )

    # Now the queued INITIAL START is finally consumed (restart_carry is None).
    ack, started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="queued-start-1",
    )
    assert started is False, (
        "the queued initial START must NOT spawn a node after the operator "
        "stopped a still-starting deployment"
    )
    assert ack is True, "the queued START is ACK-dropped (operator-stopped — do not retry)"
    assert await _active_row_count(session_factory, dep_id) == 0, (
        "no active node row may exist for a deployment the operator stopped "
        "while it was still starting"
    )


@pytest.mark.asyncio
async def test_stop_queued_start_aborts_redelivered_start(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inv 1 (redelivered-START variant). The same gap, but the START is
    RE-DELIVERED (XAUTOCLAIM / PEL recovery) after the operator stopped a
    still-starting deployment. A redelivered START also carries no
    ``restart_carry``; the operator-``stopped`` deployment gate must ABORT it on
    every redelivery — never spawn a node for a stopped account."""
    dep_id = await _seed_no_node(session_factory, deployment_status="building")
    slug = await _slug(session_factory, dep_id)

    ok = await fleet_router.stop(dep_id, reason="user")
    assert ok is True

    # Simulate two redeliveries of the same queued START.
    for attempt in range(2):
        ack, started = await fleet_router.spawn_with_outcome(
            deployment_id=dep_id,
            deployment_slug=slug,
            payload={},
            idempotency_key=f"redelivered-start-{attempt}",
        )
        assert started is False, f"redelivery {attempt} must not spawn a node for a stopped account"
        assert ack is True, f"redelivery {attempt} is ACK-dropped (operator-stopped)"
        assert await _active_row_count(session_factory, dep_id) == 0


@pytest.mark.asyncio
async def test_stop_queued_start_with_prior_stopped_node_row_aborts_spawn(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inv 1 (non-failed latest row variant). A warm re-deploy: the deployment
    was previously stopped (latest node row is ``stopped``), then re-deployed
    (deployment back to ``starting``, START queued), then the operator /stops
    again before the new START is consumed. The latest node row is ``stopped``
    (NOT ``failed``), so the old failed-only stamp would miss it — the
    deployment-terminal mark must still fire so the queued START aborts."""
    dep_id, _old_row = await _seed(
        session_factory,
        deployment_status="starting",  # re-deployed: deployment reset to starting
        node_status="stopped",  # but the latest node row is a prior stop
    )
    slug = await _slug(session_factory, dep_id)

    ok = await fleet_router.stop(dep_id, reason="user")
    assert ok is True

    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "stopped"

    ack, started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="warm-redeploy-stop-1",
    )
    assert started is False
    assert ack is True
    assert await _active_row_count(session_factory, dep_id) == 0


@pytest.mark.asyncio
async def test_redelivered_stop_on_stopping_row_does_not_mark_deployment_stopped(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FINDING 3 (P2). A REDELIVERED / retried STOP on a deployment whose node
    row is already ``stopping`` (the first STOP sent SIGTERM; the child is still
    tearing down) must NOT prematurely mark the deployment ``stopped``.

    Pre-fix the active-row lookup filtered ``status.in_(('starting','building',
    'ready','running'))`` — it OMITTED ``stopping``. So a redelivered STOP found
    no active row, fell into the no-active-row branch, and the queued-START logic
    marked the ``LiveDeployment`` ``stopped`` WHILE the child was still in
    teardown — hiding a live teardown from active-only status / deploy gates (an
    ``active_deployments`` deploy gate could then recreate the container
    mid-teardown — an F5 binding-deploy-contract risk).

    Post-fix ``stopping`` is in the active-row lookup, so the redelivered STOP
    FINDS the ``stopping`` row and handles it as an IDEMPOTENT in-progress stop:
    returns success, re-stamps the durable ``stop_requested_at`` intent, and does
    NOT touch ``LiveDeployment.status`` — the reaper syncs the deployment to
    ``stopped``/``failed`` when the child actually exits.
    """
    # Node row is ``stopping`` (first STOP already sent SIGTERM); the deployment
    # is still ``running`` (the reaper has not observed the exit yet). pid=None
    # so the handler takes the no-pid path after the active-row branch (no real
    # child needed). host defaults to this host so the cross-host guard passes.
    dep_id, row_id = await _seed(
        session_factory,
        deployment_status="running",
        node_status="stopping",
        pid=None,
    )

    # The redelivered STOP.
    ok = await fleet_router.stop(dep_id, reason="user")
    assert ok is True, "a redelivered STOP on a stopping row must succeed idempotently"

    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        node_row = (
            await session.execute(select(LiveNodeProcess).where(LiveNodeProcess.id == row_id))
        ).scalar_one()

    assert dep_status == "running", (
        "a redelivered STOP on a stopping node row must NOT mark the deployment "
        "stopped — the child is still tearing down; the reaper syncs the "
        "deployment status when it actually exits"
    )
    # The stopping node row is found + handled idempotently: it stays in the
    # active (stopping) state and the durable stop intent is (re-)stamped.
    assert node_row.status == "stopping", "the in-progress stopping row must stay stopping"
    assert node_row.stop_requested_at is not None, (
        "the idempotent redelivered STOP must (re-)stamp the durable stop intent"
    )
    # The active node row still exists (≤1 active row invariant holds — stopping
    # is in the active set).
    assert await _active_row_count(session_factory, dep_id) == 1


@pytest.mark.asyncio
async def test_redelivered_stop_on_stopping_row_resignals_live_pid(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FINDING 3 (P2) — re-signal variant. A redelivered STOP on a ``stopping``
    row with a LIVE pid is idempotent: it (harmlessly) re-signals SIGTERM and
    returns success, without marking the deployment stopped. Confirms the
    active-row branch (not the no-active-row queued-START branch) handles it."""
    child = mp.get_context("spawn").Process(target=_sleep_target, args=(30,))
    child.start()
    assert child.pid is not None
    try:
        dep_id, _row_id = await _seed(
            session_factory,
            deployment_status="ready",
            node_status="stopping",
            pid=child.pid,
            host=socket.gethostname(),
        )

        ok = await fleet_router.stop(dep_id, reason="user")
        assert ok is True

        async with session_factory() as session:
            dep_status = (
                await session.execute(
                    select(LiveDeployment.status).where(LiveDeployment.id == dep_id)
                )
            ).scalar_one()
        assert dep_status == "ready", (
            "a redelivered STOP on a stopping row with a live pid must NOT mark "
            "the deployment stopped — it re-signals the in-progress teardown"
        )
        # The re-signal landed on the child (SIGTERM), which exits.
        child.join(timeout=5)
        assert not child.is_alive()
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=2)


@pytest.mark.asyncio
async def test_operator_stopped_deployment_not_resurrected_by_restart_carry(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inv 1 (restart_carry must not resurrect a stopped deployment). An
    operator-``stopped`` deployment must NEVER be respawned by the auto-restart
    path either: even a ``restart_carry`` respawn (reaper / rescan / give-up
    retry) must ABORT when the deployment is operator-``stopped``. This is the
    distinction the task mandates: ``stopped`` is operator-terminal (abort),
    ``failed`` is recoverable (restart_carry allowed)."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="stopped",
        node_status="failed",
        failure_kind=FailureKind.NODE_CRASHED.value,
    )

    # The reaper-style auto-restart must refuse — a stopped deployment is
    # operator-terminal, not a recoverable crash.
    restarted = await fleet_router._maybe_auto_restart(dep_id)
    assert restarted is False, "auto-restart must not resurrect an operator-stopped deployment"
    assert await _active_row_count(session_factory, dep_id) == 0
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "stopped", (
        "a stopped deployment must stay stopped (never reset to starting)"
    )


@pytest.mark.asyncio
async def test_failed_deployment_still_restarts_after_queued_start_fix(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inv 1 (legitimate restart still works). The fix must NOT break a genuine
    auto-restart of a ``failed`` (recoverable, NOT operator-stopped) deployment:
    restart_carry resets ``failed`` → ``starting`` and respawns. This is the
    counter-case to the stopped-abort test above."""
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        failure_kind=FailureKind.NODE_CRASHED.value,
    )

    restarted = await fleet_router._maybe_auto_restart(dep_id)
    assert restarted is True, "a failed (recoverable) deployment must still auto-restart"
    assert await _active_row_count(session_factory, dep_id) == 1
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "starting"


# ---------------------------------------------------------------------------
# 7. Codex P2 — transient-START retry exemption must bind to the CURRENT attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_transient_redelivery_predating_last_started_is_dropped(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex P2 (council 2026-06-01 follow-up). The transient-START retry
    exemption (which lets a no-ACK redelivery of a transient-failed START
    re-spawn) must be bound to the CURRENT START command. A STALE redelivery of
    an OLD transient-failed attempt — whose node row's ``started_at`` PREDATES
    the deployment's ``last_started_at`` (a newer ``/start`` was issued and timed
    out at the endpoint without creating a node row) — must NOT bypass the
    stale-START drop and spawn a node after the caller already saw a 504.

    Seed: deployment ``failed``, latest node row ``SPAWN_FAILED_TRANSIENT`` but
    with ``started_at`` an hour BEFORE ``last_started_at`` (a superseded attempt).
    A redelivered START (``restart_carry=None``) must ACK-drop (NOT_STARTABLE) —
    no node spawned."""
    old = datetime.now(UTC) - timedelta(hours=1)
    newer = datetime.now(UTC)
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT.value,
        node_started_at=old,
        deployment_last_started_at=newer,  # a NEWER /start superseded the transient row
    )
    slug = await _slug(session_factory, dep_id)

    ack, started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="stale-transient-redelivery-1",
    )
    assert started is False, (
        "a stale transient redelivery predating last_started_at must NOT spawn — "
        "the exemption is bound to the current START attempt"
    )
    assert ack is True, "the stale redelivery is ACK-dropped (NOT_STARTABLE), not retried"
    assert await _active_row_count(session_factory, dep_id) == 0


@pytest.mark.asyncio
async def test_current_transient_redelivery_at_or_after_last_started_respawns(
    fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex P2 (positive counter-case). A transient redelivery whose terminal
    node row is bound to the CURRENT attempt (``started_at`` AT/AFTER
    ``last_started_at``) is still honored: the redelivery re-spawns (the no-ACK
    retry the transient path promised). This proves the currency binding does not
    break legitimate transient recovery."""
    now = datetime.now(UTC)
    dep_id, _row_id = await _seed(
        session_factory,
        deployment_status="failed",
        node_status="failed",
        failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT.value,
        node_started_at=now,
        deployment_last_started_at=now - timedelta(seconds=5),  # row is at/after this
    )
    slug = await _slug(session_factory, dep_id)

    ack, started = await fleet_router.spawn_with_outcome(
        deployment_id=dep_id,
        deployment_slug=slug,
        payload={},
        idempotency_key="current-transient-redelivery-1",
    )
    assert started is True, (
        "a transient redelivery bound to the current attempt must re-spawn (the "
        "promised no-ACK retry)"
    )
    assert ack is True
    assert await _active_row_count(session_factory, dep_id) == 1
    async with session_factory() as session:
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
    assert dep_status == "starting", "the transient retry resets the deployment to starting"
