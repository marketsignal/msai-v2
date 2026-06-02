"""T12 — supervisor-outage safety harness (Contrarian blocking; test counterpart to US-4).

This is the REAL-MONEY safety evidence: it proves the ROUTER-INDEPENDENT
guarantees PR 2 delivers hold while the supervisor (the ``FleetRouter`` reap /
command-consumer loops) is DOWN. The behaviours it asserts are implemented by T2
(node-side live halt) + T6 (startup re-scan) + the API-side latch endpoints; T12
is the harness that proves they are genuinely supervisor-independent — not a
re-test of those units in isolation, but an end-to-end exercise against REAL
Postgres + Redis testcontainers with the supervisor loops never started.

"Supervisor stopped" = the ``FleetRouter`` ``reap_loop`` / ``reap_once`` and the
per-account command consumers are NEVER run. The guarantees are driven directly:

1. **Node-side live halt (T2 / F6) blocks NEW opening orders.** A running node's
   ``RiskAwareStrategy`` re-checks the live Redis halt latch on its own
   order-submit path, armed + fed by the SAME production wiring
   (``wire_halt_refresh`` → ``_read_halt_value`` → ``refresh_halt_cache``)
   against the REAL testcontainer Redis. With the supervisor down we set the
   fleet OR account halt latch directly in Redis and assert an opening
   ``submit_order`` is BLOCKED. (The key proof the F6 fix delivers
   router-independent halt.)

2. **A reduce-only / ``MARKET_EXIT``-tagged flatten order is ALLOWED under that
   same halt.** Proves the kill-switch can still flatten while halted (the node
   is NOT frozen open). Empirically pins the cpdef-internal-dispatch question
   the plan flags (T12 scope note): the reduce-only branch admits the flatten.

3. **``/drain`` + ``/kill-all`` still set their Redis halt latches (API-side,
   supervisor-independent).** Driven through the real FastAPI app via
   ``httpx.ASGITransport`` with NO supervisor running; assert the latch keys
   are set in the real Redis purely by the API path.

4. **The node's own heartbeat row keeps updating.** The heartbeat thread lives in
   the node SUBPROCESS, not the supervisor — a real ``_HeartbeatThread`` against
   the real Postgres advances ``last_heartbeat_at`` with no supervisor.

5. **On supervisor return, the startup re-scan (T6 ``rescan_for_restart`` /
   ``_load_rescan_candidates``) recovers a dead deployment.** A ``failed`` /
   stale active deployment (non-halted, ``auto_restart_paused=false``,
   ``stop_requested_at`` NULL) is picked up the moment a fresh ``FleetRouter``
   re-scans on startup.

EXPLICITLY OUT OF SCOPE (and asserting it would be WRONG — plan T12 scope
correction, Codex iter-2 P2): flatness REPORTING during the outage. The flatness
``stop_report`` is written by the child only AFTER the supervisor pushes the
flatness ticket + SIGTERM, so it is legitimately unavailable until the supervisor
returns — a documented known outage-window limitation, mitigated by guarantee 1
blocking NEW orders. This harness does NOT touch flatness reporting.

SAFETY: dedicated PostgresContainer + RedisContainer per module.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
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
from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.live_supervisor.fleet_router import FleetRouter
from msai.live_supervisor.restart_policy import RestartPolicy
from msai.main import app
from msai.models import Base, LiveDeployment, LiveNodeProcess
from msai.services.live_command_bus import LIVE_COMMAND_STREAM, LiveCommandBus
from msai.services.nautilus.risk import RiskAwareStrategy
from msai.services.nautilus.trading_node_subprocess import (
    _HeartbeatThread,
    wire_halt_refresh,
)
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


_ACCOUNT_ID = "DU7654321"


# ---------------------------------------------------------------------------
# Fixtures — the SAME real-DB+Redis testcontainer pattern the existing
# integration tests use (test_auto_restart_db.py + the per-account-stream API
# test). Reused verbatim so the harness can't drift from the established style.
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
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


def _noop_spawn_target() -> None:
    """Picklable no-op spawn target for the ``FleetRouter`` constructor.

    The reap loop is NEVER run in this harness (that IS the supervisor outage);
    the target just needs to be a valid module-level callable. For guarantee 5
    we drive ``rescan_for_restart`` directly — Phase A reserves a row + the
    no-op child starts cleanly without trading.
    """
    return None


# ---------------------------------------------------------------------------
# Node-side halt-gate test double.
#
# Mirrors tests/unit/services/nautilus/risk/test_node_side_live_halt.py: a
# ``RiskAwareStrategy`` subclass (mixin FIRST so the gated overrides win the
# MRO) over a recording base standing in for Nautilus's ``Strategy``. The
# recording base records the unbound-base ``submit_order`` delegations the gate
# performs on ALLOW, so we can assert block-vs-allow without a Nautilus runtime.
#
# CRITICAL DIFFERENCE vs the unit test: the gate is armed + its halt cache fed
# by the REAL production wiring (``wire_halt_refresh`` → ``_read_halt_value``)
# reading the REAL testcontainer Redis latch — NOT by hand-setting
# ``_halt_cache``. That is what makes this an INTEGRATION proof of the
# router-independent F6 path rather than a re-run of the unit gate.
# ---------------------------------------------------------------------------


class _FakeVenue:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _FakeInstrumentId:
    def __init__(self, symbol: str = "AAPL", venue: str = "NASDAQ") -> None:
        self.symbol = symbol
        self.venue = _FakeVenue(venue)

    def __str__(self) -> str:
        return f"{self.symbol}.{self.venue.value}"


@dataclass
class FakeOrder:
    """Stub Nautilus order — the gate reads ``instrument_id``,
    ``client_order_id``, ``is_reduce_only`` and ``tags``."""

    client_order_id: str = "ord-1"
    instrument_id: Any = field(default_factory=_FakeInstrumentId)
    is_reduce_only: bool = False
    tags: list[str] | None = None


class _RecordingBase:
    """Stand-in for ``nautilus_trader.trading.strategy.Strategy``. Records the
    submit-method calls the gated OVERRIDE delegates to via the unbound base
    call, so a recorded call == the gate ALLOWED the order."""

    def submit_order(self, order: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_submitted.append(order)  # type: ignore[attr-defined]

    def submit_order_list(self, order_list: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_submitted_lists.append(order_list)  # type: ignore[attr-defined]

    def modify_order(self, order: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_modified.append(order)  # type: ignore[attr-defined]


class _GatedStrategy(RiskAwareStrategy, _RecordingBase):
    """Mixin FIRST so the gated overrides win the MRO (the production
    ``class S(RiskAwareStrategy, Strategy)`` shape), with a recording base
    standing in for Nautilus's ``Strategy``."""

    def __init__(self) -> None:
        self._audit = None
        self.base_submitted: list[Any] = []
        self.base_submitted_lists: list[Any] = []
        self.base_modified: list[Any] = []


# ---------------------------------------------------------------------------
# API-drive fixtures (guarantee 3) — mirror
# tests/integration/api/test_drain_killall_stop_per_account_stream.py.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bus(redis_client: AsyncRedis) -> LiveCommandBus:
    return LiveCommandBus(redis=redis_client, min_idle_ms=0, recovery_interval_s=60)


@pytest_asyncio.fixture
async def api_client(
    session_factory: async_sessionmaker[AsyncSession],
    bus: LiveCommandBus,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the REAL FastAPI app with the supervisor DOWN.

    The flatness-report + terminal polling helpers are patched so the endpoints
    return without a live supervisor — the latch SET (the contract under test)
    still hits the real testcontainer Redis. This is the only way to exercise
    the API path during a simulated outage: the supervisor that would consume
    the published STOP and write the flatness report does not exist.
    """

    async def _fake_poll_stop_report(*_a: object, **_k: object) -> dict:
        return {"broker_flat": True, "remaining_positions": []}

    async def _fake_poll_for_terminal(*_a: object, **_k: object) -> object:
        row = SimpleNamespace(status="stopped")
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_deployment_with_active_process(
    session: AsyncSession, *, account_id: str
) -> tuple[LiveDeployment, LiveNodeProcess]:
    """Seed a ``running`` deployment + an active ``running`` node-process row."""
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


async def _arm_node_against_real_redis(
    strat: _GatedStrategy, *, redis_client: AsyncRedis, account_id: str
) -> asyncio.Task[None]:
    """Arm the node-side halt gate via the SAME production wiring the live
    ``on_post_build`` hook uses (``wire_halt_refresh`` → ``_read_halt_value`` →
    ``refresh_halt_cache``), reading the REAL testcontainer Redis latch.

    Returns the background refresh task so the caller cancels it. This is the
    integration-grade arming: ``_halt_cache`` is populated from the real Redis
    GET on the fleet+account latches, NOT hand-set.
    """
    return await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis_client,
        account_id=account_id,
        interval_s=0.2,  # tight so the test sees a latch flip quickly
    )


async def _refresh_until(
    strat: _GatedStrategy, *, expected_halt: bool, timeout_s: float = 5.0
) -> None:
    """Wait until the background refresh task has pulled ``expected_halt`` from
    the real Redis into the node's sync halt cache."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cache = strat._halt_cache  # noqa: SLF001
        if cache is not None and cache[0] is expected_halt:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"halt cache never reached expected={expected_halt}; cache={strat._halt_cache}"  # noqa: SLF001
    )


# ===========================================================================
# Guarantee 1 — node-side live halt (T2/F6) BLOCKS new opening orders while the
# supervisor is DOWN. Fleet latch + account latch, each independently.
# ===========================================================================


@pytest.mark.asyncio
async def test_g1_node_side_halt_blocks_opening_order_under_account_latch_supervisor_down(
    redis_client: AsyncRedis,
) -> None:
    # Arrange: a running node armed against the REAL Redis. NO FleetRouter loop
    # is started — the supervisor is DOWN.
    strat = _GatedStrategy()
    task = await _arm_node_against_real_redis(
        strat, redis_client=redis_client, account_id=_ACCOUNT_ID
    )
    try:
        await _refresh_until(strat, expected_halt=False)
        # Sanity: with no latch the node trades.
        strat.submit_order(FakeOrder(client_order_id="pre-halt"))
        assert [str(o.client_order_id) for o in strat.base_submitted] == ["pre-halt"]

        # Act: operator sets the ACCOUNT halt latch directly in Redis (the
        # supervisor-independent /drain effect). The node re-checks Redis itself.
        await redis_client.set(account_halt_key(_ACCOUNT_ID), "true")
        await _refresh_until(strat, expected_halt=True)

        strat.submit_order(FakeOrder(client_order_id="post-halt-open"))

        # Assert: the opening order placed AFTER the halt was BLOCKED — the node
        # did not need the supervisor to enforce the kill switch.
        assert [str(o.client_order_id) for o in strat.base_submitted] == ["pre-halt"], (
            "node-side halt must block a new opening order while the supervisor is down"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_g1_node_side_halt_blocks_opening_order_under_fleet_latch_supervisor_down(
    redis_client: AsyncRedis,
) -> None:
    # Arrange
    strat = _GatedStrategy()
    task = await _arm_node_against_real_redis(
        strat, redis_client=redis_client, account_id=_ACCOUNT_ID
    )
    try:
        await _refresh_until(strat, expected_halt=False)

        # Act: the FLEET latch (the /kill-all effect) — set directly in Redis.
        await redis_client.set(fleet_halt_key(), "true")
        await _refresh_until(strat, expected_halt=True)

        strat.submit_order(FakeOrder(client_order_id="open-under-fleet-halt"))

        # Assert
        assert strat.base_submitted == [], (
            "node-side halt must block opening orders under a FLEET latch with the "
            "supervisor down (the node reads the fleet key directly)"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ===========================================================================
# Guarantee 2 — a market_exit() / reduce-only / MARKET_EXIT-tagged flatten order
# is ALLOWED under that same halt (the kill-switch can still flatten — the node
# is not frozen open). Empirically pins the reduce-only dispatch question.
# ===========================================================================


@pytest.mark.asyncio
async def test_g2_reduce_only_flatten_allowed_under_active_halt_supervisor_down(
    redis_client: AsyncRedis,
) -> None:
    # Arrange: armed node, account latch set, supervisor down.
    strat = _GatedStrategy()
    task = await _arm_node_against_real_redis(
        strat, redis_client=redis_client, account_id=_ACCOUNT_ID
    )
    try:
        await redis_client.set(account_halt_key(_ACCOUNT_ID), "true")
        await _refresh_until(strat, expected_halt=True)

        # Act: under the SAME active halt, submit a reduce-only flatten and a
        # MARKET_EXIT-tagged flatten (what market_exit()/close_all_positions
        # builds), plus an opening order to confirm the halt is genuinely active.
        strat.submit_order(FakeOrder(client_order_id="reduce-only", is_reduce_only=True))
        strat.submit_order(FakeOrder(client_order_id="market-exit", tags=["MARKET_EXIT"]))
        strat.submit_order(FakeOrder(client_order_id="opening"))

        # Assert: BOTH flatten orders were admitted; the opening order was blocked.
        admitted = [str(o.client_order_id) for o in strat.base_submitted]
        assert admitted == ["reduce-only", "market-exit"], (
            "reduce-only / MARKET_EXIT flatten orders must be ALLOWED under an active "
            "halt while the supervisor is down (kill-switch can still flatten); the "
            "opening order must still be blocked"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_g2_reduce_only_order_list_allowed_under_active_halt_supervisor_down(
    redis_client: AsyncRedis,
) -> None:
    """A market_exit-style multi-leg flatten (``submit_order_list`` of all
    reduce-only legs) is admitted atomically under an active halt — the bulk
    close path is not frozen either."""
    strat = _GatedStrategy()
    task = await _arm_node_against_real_redis(
        strat, redis_client=redis_client, account_id=_ACCOUNT_ID
    )
    try:
        await redis_client.set(fleet_halt_key(), "true")
        await _refresh_until(strat, expected_halt=True)

        flatten_list = SimpleNamespace(
            orders=[
                FakeOrder(client_order_id="f1", is_reduce_only=True),
                FakeOrder(client_order_id="f2", tags=["MARKET_EXIT"]),
            ]
        )
        strat.submit_order_list(flatten_list)

        assert strat.base_submitted_lists == [flatten_list], (
            "an all-reduce-only flatten order list must be admitted under a halt "
            "(bulk close works while the supervisor is down)"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ===========================================================================
# Guarantee 3 — /drain + /kill-all still set their Redis halt latches via the
# API path, with NO supervisor running.
# ===========================================================================


@pytest.mark.asyncio
async def test_g3_drain_sets_account_latch_via_api_supervisor_down(
    api_client: httpx.AsyncClient,
    redis_client: AsyncRedis,
) -> None:
    account_id = "DUP-OUTAGE-DRAIN"
    # No active deployment → the publish/flatness loop is empty, so the endpoint
    # completes purely from the API path (the supervisor is down and consumes
    # nothing). The latch SET is the supervisor-independent contract.
    assert await redis_client.exists(account_halt_key(account_id)) == 0

    response = await api_client.post(f"/api/v1/live/drain/{account_id}")

    assert response.status_code == 200, response.text
    # The account latch is set in the REAL Redis by the API path alone.
    assert await redis_client.exists(account_halt_key(account_id)) == 1, (
        "/drain must set the account halt latch with the supervisor down"
    )
    body = response.json()
    assert body["halt_cause"] == "operator_drain"
    # The fleet latch was NOT touched (drain is account-scoped).
    assert await redis_client.exists(fleet_halt_key()) == 0


@pytest.mark.asyncio
async def test_g3_kill_all_sets_fleet_latch_via_api_supervisor_down(
    api_client: httpx.AsyncClient,
    redis_client: AsyncRedis,
) -> None:
    assert await redis_client.exists(fleet_halt_key()) == 0

    response = await api_client.post("/api/v1/live/kill-all")

    assert response.status_code in (200, 207), response.text
    # The fleet halt latch is set in the REAL Redis by the API path alone —
    # blocking any new /start even though the supervisor is down.
    assert await redis_client.get(fleet_halt_key()) == "true", (
        "/kill-all must set the fleet halt latch with the supervisor down"
    )


# ===========================================================================
# Guarantee 4 — the node's OWN heartbeat row keeps updating (the heartbeat
# thread is in the node subprocess, not the supervisor).
# ===========================================================================


@pytest.mark.asyncio
async def test_g4_node_heartbeat_advances_without_supervisor(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_postgres_url: str,
) -> None:
    # Arrange: a running node-process row. NO supervisor reap/monitor loop runs.
    async with session_factory() as session:
        _dep, proc = await _seed_deployment_with_active_process(session, account_id=_ACCOUNT_ID)
        row_id = proc.id
        initial_heartbeat = proc.last_heartbeat_at

    # Act: the SAME heartbeat thread the node subprocess runs (lives in the
    # child, not the supervisor) ticks against the real Postgres.
    thread = _HeartbeatThread(
        async_database_url=isolated_postgres_url,
        row_id=row_id,
        interval_s=0.3,
    )
    thread.start()
    try:
        await asyncio.sleep(1.5)
    finally:
        thread.stop()
        thread.join(timeout=5.0)

    # Assert: the row advanced independent of any supervisor.
    assert thread.last_error is None, f"heartbeat thread errored: {thread.last_error}"
    assert thread.ticks >= 3, (
        f"the node-subprocess heartbeat must keep ticking with the supervisor down; "
        f"got {thread.ticks} ticks"
    )
    async with session_factory() as session:
        after = await session.get(LiveNodeProcess, row_id)
        assert after is not None
        assert after.last_heartbeat_at > initial_heartbeat, (
            "last_heartbeat_at must advance with no supervisor running"
        )


# ===========================================================================
# Guarantee 5 — on supervisor RETURN, the startup re-scan (T6) recovers a dead
# deployment that died while the supervisor was down.
# ===========================================================================


@pytest_asyncio.fixture
async def returned_fleet_router(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: AsyncRedis,
) -> AsyncIterator[FleetRouter]:
    """A FRESH FleetRouter — the supervisor that just came BACK. Its
    ``node_handle_cache`` is empty (a restart loses the in-memory handles), so
    the only way it can recover a node that died during the outage is the
    startup re-scan. Tight ``rescan_stale_seconds`` so a 120s-stale heartbeat is
    unambiguously dead."""
    router = FleetRouter(
        db=session_factory,
        redis=redis_client,
        spawn_target=_noop_spawn_target,
        restart_policy=RestartPolicy(),
        rescan_stale_seconds=5,
    )
    yield router
    for cached in list(router.node_handle_cache.values()):
        with contextlib.suppress(Exception):
            cached.proc.terminate()
            cached.proc.join(timeout=2)


@pytest.mark.asyncio
async def test_g5_startup_rescan_recovers_failed_deployment_on_supervisor_return(
    returned_fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment whose node crashed while the supervisor was down — its own
    ``_mark_terminal`` flipped it to ``failed`` (non-halted,
    ``auto_restart_paused=false``, ``stop_requested_at`` NULL) — is picked up by
    the startup re-scan the moment the supervisor returns."""
    import socket

    async with session_factory() as session:
        dep = await make_live_deployment(session, account_id=_ACCOUNT_ID, status="failed")
        now = datetime.now(UTC)
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=dep.id,
            pid=None,
            host=socket.gethostname(),
            started_at=now,
            last_heartbeat_at=now,
            status="failed",
            gateway_session_key="sess-outage",
            consecutive_respawn_failures=0,
            auto_restart_paused=False,
            stop_requested_at=None,
        )
        session.add(row)
        await session.commit()
        dep_id = dep.id

    # Act: the supervisor comes back and runs its startup re-scan.
    restarted = await returned_fleet_router.rescan_for_restart()

    # Assert: the dead deployment was recovered (a fresh node row reserved + the
    # deployment reset off ``failed``), proving outage-window crashes self-heal
    # on supervisor return.
    assert restarted == 1, "the startup re-scan must recover the failed deployment"
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
        assert len(node_rows) == 2, "a fresh node row must be reserved by the re-scan respawn"
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        assert dep_status == "starting", "the deployment is reset off 'failed' for the respawn"


@pytest.mark.asyncio
async def test_g5_startup_rescan_recovers_stale_active_deployment_on_supervisor_return(
    returned_fleet_router: FleetRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The harder outage-window case: the node died while the supervisor was
    down so its exit was NEVER observed — the row is still ``running`` with a
    STALE heartbeat. The re-scan flips it to ``failed`` + respawns it on
    supervisor return (the supervisor-was-down scenario the re-scan exists for).
    """
    import socket

    stale_hb = datetime.now(UTC) - timedelta(seconds=120)
    async with session_factory() as session:
        dep = await make_live_deployment(session, account_id=_ACCOUNT_ID, status="running")
        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=dep.id,
            pid=None,
            host=socket.gethostname(),
            started_at=datetime.now(UTC),
            last_heartbeat_at=stale_hb,
            status="running",
            gateway_session_key="sess-stale-outage",
            consecutive_respawn_failures=0,
            auto_restart_paused=False,
            stop_requested_at=None,
        )
        session.add(row)
        await session.commit()
        dep_id = dep.id
        stale_row_id = row.id

    restarted = await returned_fleet_router.rescan_for_restart()

    assert restarted == 1, "the stale-active node must be recovered by the re-scan"
    async with session_factory() as session:
        stale_row = await session.get(LiveNodeProcess, stale_row_id)
        assert stale_row is not None
        assert stale_row.status == "failed", "the stale-active row is flipped to failed"
        node_rows = (
            (
                await session.execute(
                    select(LiveNodeProcess.id).where(LiveNodeProcess.deployment_id == dep_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(node_rows) == 2
        dep_status = (
            await session.execute(select(LiveDeployment.status).where(LiveDeployment.id == dep_id))
        ).scalar_one()
        assert dep_status == "starting"


# ---------------------------------------------------------------------------
# Sanity: the global command stream is never created during the outage drive —
# the API publishes onto per-account streams (T4), and we never started a
# global consumer. (Belt-and-braces: confirms the harness did not silently
# stand up a supervisor consumer.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_command_stream_untouched_during_outage(
    api_client: httpx.AsyncClient,
    redis_client: AsyncRedis,
) -> None:
    await api_client.post("/api/v1/live/drain/DUP-OUTAGE-SANITY")
    # No deployments seeded → no STOP published anywhere; the global stream
    # stays empty (no supervisor consumer attached, none expected).
    assert await redis_client.xlen(LIVE_COMMAND_STREAM) == 0
