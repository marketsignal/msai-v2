"""Unit tests for PR 2 T4 — per-account command streams + failure containment.

Covers the producer/consumer migration off the GLOBAL command stream onto the
pre-built per-account stream (``command_stream_for_account``), the supervisor's
per-account consumer fan-out with an exception boundary (one account's command
exception can't stall another account), the attach-late idempotent
stream/group create (a START published before the consumer attaches is still
read, not lost), startup account enumeration (static pool + active-deployment
account_ids), and the ``router_heartbeat`` signal that replaces the global
consumer-group probe in ``_supervisor_is_alive``.

These tests use ``fakeredis.aioredis`` — it implements XADD / XREADGROUP /
XAUTOCLAIM / XPENDING / XINFO CONSUMERS faithfully, so the per-account stream
wire contract is exercised without a testcontainer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest

from msai.live_supervisor.main import run_forever
from msai.services.live.flatness_service import coalesce_or_publish_stop_with_flatness
from msai.services.live_command_bus import (
    LIVE_COMMAND_STREAM,
    LiveCommandBus,
    LiveCommandType,
    command_stream_for_account,
)


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def bus(fake_redis: fakeredis.aioredis.FakeRedis) -> LiveCommandBus:
    return LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)


async def _stream_entries(redis: fakeredis.aioredis.FakeRedis, stream: str) -> list:
    return await redis.xrange(stream)


# ---------------------------------------------------------------------------
# Producer migration: publish targets the per-account stream
# ---------------------------------------------------------------------------


class TestProducerTargetsPerAccountStream:
    @pytest.mark.asyncio
    async def test_publish_start_with_account_id_lands_on_account_stream(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        dep_id = uuid4()
        await bus.publish_start(dep_id, {"deployment_slug": "p-A"}, account_id="DUP733214")

        account_stream = command_stream_for_account("DUP733214")
        assert account_stream == "msai:live:commands:DUP733214"
        # The command landed on the per-account stream...
        account_entries = await _stream_entries(fake_redis, account_stream)
        assert len(account_entries) == 1
        # ...and NOT on the global stream (no double-publish).
        global_entries = await _stream_entries(fake_redis, LIVE_COMMAND_STREAM)
        assert global_entries == []

    @pytest.mark.asyncio
    async def test_publish_start_without_account_id_uses_global_stream(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        # Backwards-compat: no account_id → global stream (legacy callers).
        dep_id = uuid4()
        await bus.publish_start(dep_id, {"deployment_slug": "p-A"})
        global_entries = await _stream_entries(fake_redis, LIVE_COMMAND_STREAM)
        assert len(global_entries) == 1

    @pytest.mark.asyncio
    async def test_publish_stop_with_account_id_lands_on_account_stream(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        dep_id = uuid4()
        await bus.publish_stop(dep_id, account_id="DUP733215")
        assert len(await _stream_entries(fake_redis, "msai:live:commands:DUP733215")) == 1
        assert await _stream_entries(fake_redis, LIVE_COMMAND_STREAM) == []

    @pytest.mark.asyncio
    async def test_publish_stop_and_report_flatness_with_account_id(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        dep_id = uuid4()
        await bus.publish_stop_and_report_flatness(
            dep_id,
            stop_nonce="nonce123",
            member_strategy_id_fulls=["Strat-0-slug"],
            account_id="DUP733214",
        )
        entries = await _stream_entries(fake_redis, "msai:live:commands:DUP733214")
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["command_type"] == LiveCommandType.STOP_AND_REPORT_FLATNESS.value
        assert await _stream_entries(fake_redis, LIVE_COMMAND_STREAM) == []


# ---------------------------------------------------------------------------
# Codex iter-19 P2: a bus built with a CUSTOM stream must publish onto that
# configured base (consistent with consume/ack), not the global stream.
# ---------------------------------------------------------------------------


class TestPublishHonorsConfiguredBaseStream:
    @pytest.mark.asyncio
    async def test_command_stream_for_account_respects_base(self) -> None:
        # Account-less → the base itself; with-account → base-namespaced.
        assert command_stream_for_account(None, base="custom:base") == "custom:base"
        assert command_stream_for_account("DU1", base="custom:base") == "custom:base:DU1"
        # Default base is unchanged for existing callers.
        assert command_stream_for_account(None) == LIVE_COMMAND_STREAM
        assert command_stream_for_account("DU1") == f"{LIVE_COMMAND_STREAM}:DU1"

    @pytest.mark.asyncio
    async def test_custom_stream_bus_publishes_account_less_to_its_base(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        custom_bus = LiveCommandBus(
            redis=fake_redis, stream="custom:base", min_idle_ms=0, recovery_interval_s=60
        )
        await custom_bus.publish_start(uuid4(), {"deployment_slug": "p-A"})
        # Lands on the bus's configured base (where consume/ack read) — NOT the global stream.
        assert len(await _stream_entries(fake_redis, "custom:base")) == 1
        assert await _stream_entries(fake_redis, LIVE_COMMAND_STREAM) == []

    @pytest.mark.asyncio
    async def test_custom_stream_bus_publishes_with_account_under_its_base(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        custom_bus = LiveCommandBus(
            redis=fake_redis, stream="custom:base", min_idle_ms=0, recovery_interval_s=60
        )
        await custom_bus.publish_stop(uuid4(), account_id="DU1")
        assert len(await _stream_entries(fake_redis, "custom:base:DU1")) == 1
        # Neither the global stream nor the global per-account stream is used.
        assert await _stream_entries(fake_redis, LIVE_COMMAND_STREAM) == []
        assert await _stream_entries(fake_redis, f"{LIVE_COMMAND_STREAM}:DU1") == []


# ---------------------------------------------------------------------------
# coalesce_or_publish_stop_with_flatness threads account_id to the producer
# ---------------------------------------------------------------------------


class TestCoalescePassesAccountId:
    @pytest.mark.asyncio
    async def test_account_id_forwarded_to_publish(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        fake_bus = AsyncMock()
        fake_bus.publish_stop_and_report_flatness = AsyncMock(return_value="entry-id")
        dep_id = uuid4()

        nonce, is_origin = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis,
            bus=fake_bus,
            deployment_id=dep_id,
            member_strategy_id_fulls=[],
            account_id="DUP733214",
        )

        assert is_origin is True
        kw = fake_bus.publish_stop_and_report_flatness.call_args.kwargs
        assert kw["account_id"] == "DUP733214"
        assert kw["stop_nonce"] == nonce


# ---------------------------------------------------------------------------
# router_heartbeat — replaces the global consumer-group probe
# ---------------------------------------------------------------------------


class TestRouterHeartbeat:
    @pytest.mark.asyncio
    async def test_publish_then_read_age_is_small(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        await bus.publish_router_heartbeat()
        age = await bus.read_router_heartbeat_age_s()
        assert age is not None
        assert age < 5.0

    @pytest.mark.asyncio
    async def test_read_age_is_none_when_never_published(self, bus: LiveCommandBus) -> None:
        # No heartbeat ever published → None (caller treats as "dead", fail-closed).
        assert await bus.read_router_heartbeat_age_s() is None

    @pytest.mark.asyncio
    async def test_supervisor_is_alive_true_via_heartbeat_with_global_consumer_off(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The 503 gate must NOT false-trip once the global consumer is
        retired. ``_supervisor_is_alive`` reads ``router_heartbeat`` — a
        fresh heartbeat means alive even though NO consumer is registered on
        the global stream's group."""
        from msai.api.live import _supervisor_is_alive

        # No global-stream consumer group exists at all here.
        await bus.publish_router_heartbeat()
        assert await _supervisor_is_alive(bus) is True

    @pytest.mark.asyncio
    async def test_supervisor_is_alive_false_when_heartbeat_stale_or_absent(
        self, bus: LiveCommandBus, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        from msai.api.live import _supervisor_is_alive

        # Absent heartbeat → dead.
        assert await _supervisor_is_alive(bus) is False


# ---------------------------------------------------------------------------
# Per-account consumer fan-out + failure containment (run_forever)
# ---------------------------------------------------------------------------


class _RecordingRouter:
    """FleetRouter stub recording dispatches; can raise for a chosen
    deployment to simulate a per-account command-handler exception."""

    def __init__(self, *, raise_for_deployment: object = None) -> None:
        self.raise_for_deployment = raise_for_deployment
        self.started: list = []
        self.stopped: list = []

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def watchdog_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def rescan_for_restart(self) -> int:
        # Council #4 OPT C Part 1: the periodic reconciling rescan loop calls
        # this on a cadence; a no-op here keeps these command-stream tests clean.
        return 0

    async def cancel_restart_tasks(self) -> None:
        return None

    async def has_prior_operation_evidence(self) -> bool:
        # PR 2 / F3: the heartbeat boot handshake consults this on a None
        # heartbeat. These command-stream tests don't exercise the SPOF boot
        # branch (no prior heartbeat outage), so a benign default is fine.
        return False

    async def spawn(
        self, *, deployment_id, deployment_slug, payload, idempotency_key, **_kw
    ) -> bool:
        if deployment_id == self.raise_for_deployment:
            raise RuntimeError("account-A handler boom")
        self.started.append(deployment_id)
        return True

    async def stop(self, deployment_id, *, reason: str = "user") -> bool:
        if deployment_id == self.raise_for_deployment:
            raise RuntimeError("account-A handler boom")
        self.stopped.append(deployment_id)
        return True

    async def push_flatness_request(self, deployment_id, *, stop_nonce, member_strategy_id_fulls):
        return None


class _NoopHeartbeatMonitor:
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()


async def _wait_until(predicate, timeout_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class TestPerAccountConsumerFanout:
    @pytest.mark.asyncio
    async def test_account_b_stop_consumed_while_account_a_exception_does_not_stall(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """A poison START on account A's stream raises in the handler; it
        must NOT stall account B's STOP (separate per-account consumer +
        exception boundary)."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        dep_a = uuid4()
        dep_b = uuid4()
        router = _RecordingRouter(raise_for_deployment=dep_a)
        stop_event = asyncio.Event()

        # Publish BEFORE the consumers attach — both must still be read
        # (idempotent stream/group create; Redis Streams retain entries).
        await bus.publish_start(dep_a, {"deployment_slug": "p-A"}, account_id="ACC-A")
        await bus.publish_stop(dep_b, account_id="ACC-B")

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-A", "ACC-B"],
            )
        )
        try:
            # Account B's STOP must be dispatched even though account A's
            # START keeps raising on its own consumer task.
            await _wait_until(lambda: dep_b in router.stopped)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert dep_b in router.stopped
        # Account A's poison START never reached "started" (it raised),
        # and it stays in the PEL un-ACKed.
        assert dep_a not in router.started
        pending = await fake_redis.xpending(command_stream_for_account("ACC-A"), "live-supervisor")
        assert int(pending["pending"]) >= 1

    @pytest.mark.asyncio
    async def test_start_published_before_consumer_attaches_is_consumed(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """A START published to an account whose consumer attaches a beat
        later must still be consumed + ACKed (not lost)."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        dep = uuid4()
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        # Publish first; the producer idempotently creates the stream+group.
        await bus.publish_start(dep, {"deployment_slug": "p-late"}, account_id="ACC-LATE")

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-LATE"],
            )
        )
        try:
            await _wait_until(lambda: dep in router.started)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert router.started == [dep]
        # ACKed: no longer pending.
        pending = await fake_redis.xpending(
            command_stream_for_account("ACC-LATE"), "live-supervisor"
        )
        assert int(pending["pending"]) == 0

    @pytest.mark.asyncio
    async def test_custom_stream_bus_consumer_reads_from_configured_base(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """Codex iter-20 (consume-side mirror of iter-19): a bus configured with a
        non-default ``stream`` must have its per-account CONSUMER read+ACK from
        that base. The producer wrote the START to ``custom:base:ACC-CUS``; the
        supervisor consumer must derive the same stream via ``bus.account_stream``
        (not the global ``msai:live:commands:ACC-CUS``) or the command strands."""
        bus = LiveCommandBus(
            redis=fake_redis, stream="custom:base", min_idle_ms=0, recovery_interval_s=60
        )
        dep = uuid4()
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        await bus.publish_start(dep, {"deployment_slug": "p-cus"}, account_id="ACC-CUS")
        # Sanity: the producer used the configured base, NOT the global stream.
        assert len(await _stream_entries(fake_redis, "custom:base:ACC-CUS")) == 1
        assert await _stream_entries(fake_redis, "msai:live:commands:ACC-CUS") == []

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-CUS"],
            )
        )
        try:
            # The consumer must read the START off the configured base stream.
            await _wait_until(lambda: dep in router.started)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert router.started == [dep]
        # ACKed on the configured base per-account stream (not the global one).
        pending = await fake_redis.xpending("custom:base:ACC-CUS", "live-supervisor")
        assert int(pending["pending"]) == 0

    @pytest.mark.asyncio
    async def test_no_double_processing_within_one_account(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        dep = uuid4()
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        await bus.publish_start(dep, {"deployment_slug": "p-single"}, account_id="ACC-1")

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
            )
        )
        try:
            await _wait_until(lambda: dep in router.started)
            # Give the loop time to (incorrectly) re-process if it would.
            await asyncio.sleep(0.2)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert router.started.count(dep) == 1

    @pytest.mark.asyncio
    async def test_run_forever_publishes_router_heartbeat(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The supervisor loop must publish ``router_heartbeat`` so the
        503 gate (and T8/T9) can observe liveness."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
            )
        )
        try:
            await _wait_until_async(
                lambda: bus.read_router_heartbeat_age_s(),
                lambda age: age is not None,
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)


async def _wait_until_async(coro_factory, predicate, timeout_s: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        value = await coro_factory()
        if predicate(value):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("async condition not met within timeout")


# ---------------------------------------------------------------------------
# Startup account enumeration (static pool + active-deployment account_ids)
# ---------------------------------------------------------------------------


class TestStartupAccountEnumeration:
    def test_enumerate_accounts_unions_static_pool_and_active_ids(self) -> None:
        from msai.live_supervisor.main import enumerate_known_accounts

        accounts = enumerate_known_accounts(
            static_accounts=["LVP-1", "HVP-1"],
            active_deployment_account_ids=["HVP-1", "DUP999"],
        )
        assert set(accounts) == {"LVP-1", "HVP-1", "DUP999"}

    def test_enumerate_accounts_drops_empty_and_dedupes(self) -> None:
        from msai.live_supervisor.main import enumerate_known_accounts

        accounts = enumerate_known_accounts(
            static_accounts=["LVP-1", "", "LVP-1"],
            active_deployment_account_ids=[None, "LVP-1", ""],  # type: ignore[list-item]
        )
        assert accounts == ["LVP-1"]


# ---------------------------------------------------------------------------
# Lazy consumer refresh (P1 strand fix — spec step 2: "lazily starts the
# consumer ... via a lightweight known-accounts refresh on each reaper pass").
# ---------------------------------------------------------------------------


class TestLazyConsumerRefresh:
    @pytest.mark.asyncio
    async def test_account_unknown_at_boot_gets_consumer_after_refresh(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """A STOP for an account that was NOT in the boot-time known set must
        still be consumed once the account becomes discoverable — the
        supervisor lazily starts a per-account consumer on the next refresh
        pass. Without this, the command strands in the PEL forever and a
        surviving live node is unstoppable (the P1 strand)."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        dep = uuid4()
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        # The account becomes known only AFTER boot. The refresher returns []
        # at boot and the late account on subsequent passes.
        discovered: list[str] = []

        async def _refresher() -> list[str]:
            return list(discovered)

        # Publish the STOP to an account that has ZERO consumers at boot
        # (empty known_accounts, empty refresher result).
        await bus.publish_stop(dep, account_id="ACC-LATE-DISCOVERY")

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=[],
                account_refresher=_refresher,
                account_refresh_interval_s=0.05,
            )
        )
        try:
            # Confirm nothing consumed it yet (no consumer exists).
            await asyncio.sleep(0.15)
            assert dep not in router.stopped
            # Now the account becomes discoverable (e.g. a deploy landed, or
            # the boot DB scan that previously blipped now succeeds).
            discovered.append("ACC-LATE-DISCOVERY")
            # The refresh pass must spin up a consumer that drains the
            # previously-stranded command.
            await _wait_until(lambda: dep in router.stopped)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert dep in router.stopped
        pending = await fake_redis.xpending(
            command_stream_for_account("ACC-LATE-DISCOVERY"), "live-supervisor"
        )
        assert int(pending["pending"]) == 0

    @pytest.mark.asyncio
    async def test_refresh_does_not_double_start_a_consumer(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The refresher returning an already-consumed account is a no-op —
        the command must be processed exactly once (no duplicate consumer)."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        dep = uuid4()
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        async def _refresher() -> list[str]:
            # Keeps returning the same account every pass.
            return ["ACC-STABLE"]

        await bus.publish_start(dep, {"deployment_slug": "p-stable"}, account_id="ACC-STABLE")

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-STABLE"],
                account_refresher=_refresher,
                account_refresh_interval_s=0.05,
            )
        )
        try:
            await _wait_until(lambda: dep in router.started)
            # Let several refresh passes run — must not re-process.
            await asyncio.sleep(0.3)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        assert router.started.count(dep) == 1


# ---------------------------------------------------------------------------
# Consumer coverage published alongside the heartbeat (P2 — liveness must
# reflect consumption, not just router-loop aliveness).
# ---------------------------------------------------------------------------


class TestConsumedAccountCoverage:
    @pytest.mark.asyncio
    async def test_run_forever_publishes_consumed_account_set(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """The supervisor must publish the set of accounts it is actually
        consuming so the SPOF/coverage alert + /live/status can tell whether
        an active-deployment account has a running consumer."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1", "ACC-2"],
            )
        )
        try:
            await _wait_until_async(
                lambda: bus.read_consumed_accounts(),
                lambda accts: accts is not None and {"ACC-1", "ACC-2"} <= set(accts),
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)


# ---------------------------------------------------------------------------
# Production glue: _async_main derives known_accounts + the refresher from
# GatewayRouter.all_accounts() + the active-deployment scan (the spec's named
# checklist item that was only inspection-verified in the prior iteration).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fleet-alert wiring (PR 2 T4 review P1/P3): the mandatory fleet alerts (T9)
# MUST be evaluated by a running loop in the supervisor — otherwise the
# router-SPOF / account-consumer-missing safety net is dead code. ``run_forever``
# runs a fleet-alert loop that builds a snapshot from the router heartbeat age
# + the consumed-account set + a deployment-health provider and forwards each
# detected condition to the injected ``AlertService``.
# ---------------------------------------------------------------------------


class TestFleetAlertLoopWiring:
    @pytest.mark.asyncio
    async def test_account_with_active_deployment_but_no_consumer_fires_alert(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """An active-deployment account with NO running consumer (the
        un-stoppable strand) must page the operator via the wired alert loop —
        the heartbeat is fresh (gate thinks supervisor alive) but the command
        consumer is missing."""
        from msai.services.fleet_alerts import FleetHealthSnapshot

        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        router = _RecordingRouter()
        stop_event = asyncio.Event()
        alert_service = AsyncMock()

        # The supervisor consumes ACC-COVERED only; ACC-UNCOVERED has an active
        # deployment but no consumer (its STOP/kill-all would strand). The loop
        # passes the supervisor's authoritative in-memory consumed set.
        async def _fleet_health(consumed: list[str]) -> FleetHealthSnapshot:
            return FleetHealthSnapshot(
                router_heartbeat_age_s=await bus.read_router_heartbeat_age_s(),
                deployments=[],
                accounts_with_active_deployments=["ACC-COVERED", "ACC-UNCOVERED"],
                consumed_accounts=consumed,
            )

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-COVERED"],
                alert_service=alert_service,
                fleet_health_provider=_fleet_health,
                fleet_alert_interval_s=0.05,
            )
        )
        try:
            await _wait_until(
                lambda: any(
                    "ACC-UNCOVERED" in str(call.args)
                    for call in alert_service.send_alert.await_args_list
                )
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        # The account_consumer_missing alert fired and named the uncovered account.
        assert alert_service.send_alert.await_count >= 1
        bodies = " ".join(str(call.args) for call in alert_service.send_alert.await_args_list)
        assert "ACC-UNCOVERED" in bodies

    @pytest.mark.asyncio
    async def test_healthy_fleet_fires_no_alert(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """A healthy fleet (fresh heartbeat, every active account consumed, no
        failed deployment) must fire nothing on the wired loop."""
        from msai.services.fleet_alerts import FleetHealthSnapshot

        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        router = _RecordingRouter()
        stop_event = asyncio.Event()
        alert_service = AsyncMock()

        async def _fleet_health(consumed: list[str]) -> FleetHealthSnapshot:
            return FleetHealthSnapshot(
                router_heartbeat_age_s=await bus.read_router_heartbeat_age_s(),
                deployments=[],
                accounts_with_active_deployments=["ACC-1"],
                consumed_accounts=consumed,
            )

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                alert_service=alert_service,
                fleet_health_provider=_fleet_health,
                fleet_alert_interval_s=0.05,
            )
        )
        try:
            # Let the heartbeat publish + several alert passes run.
            await _wait_until_async(
                lambda: bus.read_router_heartbeat_age_s(),
                lambda age: age is not None,
            )
            await asyncio.sleep(0.25)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)

        alert_service.send_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fleet_alert_loop_absent_without_provider(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """When no ``alert_service``/``fleet_health_provider`` is supplied (e.g.
        a unit test of the consumer fan-out), ``run_forever`` must still run —
        the alert loop is opt-in and its absence is not an error."""
        bus = LiveCommandBus(redis=fake_redis, min_idle_ms=0, recovery_interval_s=60)
        router = _RecordingRouter()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            run_forever(
                bus=bus,
                process_manager=router,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
            )
        )
        try:
            await _wait_until_async(
                lambda: bus.read_router_heartbeat_age_s(),
                lambda age: age is not None,
            )
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=3.0)


class TestProductionAccountDiscoveryGlue:
    @pytest.mark.asyncio
    async def test_build_account_refresher_unions_static_pool_and_db_scan(self) -> None:
        """``_build_account_refresher`` must return a callable that re-derives
        the known-account union (static GatewayRouter pool + a fresh
        active-deployment scan) so a runtime refresh sees newly-active
        accounts without a supervisor restart."""
        from msai.live_supervisor.__main__ import _build_account_refresher
        from msai.services.live.gateway_router import GatewayRouter

        router = GatewayRouter("lvp:ib-gateway-lvp:4003:accounts=LVP-1")

        # A stub scan that flips from [] (boot blip / no deploy yet) to a
        # discovered account on the second call.
        calls = {"n": 0}

        async def _scan() -> list[str]:
            calls["n"] += 1
            return [] if calls["n"] == 1 else ["DISCOVERED-1"]

        refresher = _build_account_refresher(gateway_router=router, scan=_scan)

        first = await refresher()
        assert set(first) == {"LVP-1"}  # static pool only, scan blipped

        second = await refresher()
        assert set(second) == {"LVP-1", "DISCOVERED-1"}  # union after scan recovers
