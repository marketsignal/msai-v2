"""Integration tests for ``LiveCommandBus`` (Phase 1 task 1.6 — happy path).

Exercises the Redis-Streams-backed control plane end-to-end against a
real testcontainers Redis. Verifies the baseline publish/consume/ack
loop and the explicit XAUTOCLAIM PEL recovery required by decision #12
(Codex v3 P0).

DLQ / poison-message tests live in ``test_live_command_bus_dlq.py`` so
the two files can run independently.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio

from msai.services.live_command_bus import (
    LIVE_COMMAND_DLQ_STREAM,
    LIVE_COMMAND_GROUP,
    LIVE_COMMAND_STREAM,
    LiveCommand,
    LiveCommandBus,
    LiveCommandType,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_redis_url() -> Iterator[str]:
    """Dedicated Redis testcontainer for this module so a stray state
    from another test file can never poison these assertions."""
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def redis_client(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    """A decoded-response async Redis client per test. The bus decodes
    internally so tests can assert on plain strings without reaching
    into bytes."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    # Clean slate — drop any streams / groups left by a previous test.
    with contextlib.suppress(Exception):
        await client.delete(LIVE_COMMAND_STREAM, LIVE_COMMAND_DLQ_STREAM)
    try:
        yield client
    finally:
        await client.delete(LIVE_COMMAND_STREAM, LIVE_COMMAND_DLQ_STREAM)
        await client.aclose()


@pytest_asyncio.fixture
async def bus(redis_client: AsyncRedis) -> LiveCommandBus:
    """A ready-to-use ``LiveCommandBus`` with its consumer group created."""
    b = LiveCommandBus(redis=redis_client, min_idle_ms=0, recovery_interval_s=60)
    await b.ensure_group()
    return b


async def _consume_n(
    bus: LiveCommandBus,
    consumer_id: str,
    n: int,
    *,
    timeout_s: float = 3.0,
) -> list[LiveCommand]:
    """Consume exactly ``n`` commands via the bus, then stop.

    Used by tests that want to assert on a precise batch rather than
    exercise the long-running loop.
    """
    out: list[LiveCommand] = []
    stop_event = asyncio.Event()

    async def _drain() -> None:
        async for cmd in bus.consume(consumer_id, stop_event):
            out.append(cmd)
            if len(out) >= n:
                stop_event.set()
                return

    await asyncio.wait_for(_drain(), timeout=timeout_s)
    return out


# ---------------------------------------------------------------------------
# Publish + consume happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_group_is_idempotent(bus: LiveCommandBus) -> None:
    """Calling ``ensure_group`` twice must NOT raise. The first call
    creates the group via MKSTREAM; the second must swallow the
    ``BUSYGROUP`` error so start-up remains idempotent across restarts."""
    # Already called once in the fixture; second call must be a no-op
    await bus.ensure_group()
    await bus.ensure_group()


@pytest.mark.asyncio
async def test_publish_start_returns_entry_id(bus: LiveCommandBus) -> None:
    dep_id = uuid4()
    entry_id = await bus.publish_start(dep_id, {"paper_trading": True})
    # Redis stream entry ids are shaped ``<ms>-<seq>``.
    assert "-" in entry_id


@pytest.mark.asyncio
async def test_publish_stop_returns_entry_id(bus: LiveCommandBus) -> None:
    dep_id = uuid4()
    entry_id = await bus.publish_stop(dep_id, reason="user")
    assert "-" in entry_id


@pytest.mark.asyncio
async def test_consume_yields_published_command(bus: LiveCommandBus) -> None:
    dep_id = uuid4()
    await bus.publish_start(dep_id, {"paper_trading": True, "instruments": ["AAPL"]})

    [cmd] = await _consume_n(bus, "consumer-a", n=1)
    assert cmd.command_type == LiveCommandType.START
    assert cmd.deployment_id == dep_id
    assert cmd.payload["paper_trading"] is True
    assert cmd.payload["instruments"] == ["AAPL"]
    assert cmd.entry_id  # populated
    assert cmd.idempotency_key  # populated by publish_start


@pytest.mark.asyncio
async def test_consume_stop_command_round_trip(bus: LiveCommandBus) -> None:
    dep_id = uuid4()
    await bus.publish_stop(dep_id, reason="user")

    [cmd] = await _consume_n(bus, "consumer-a", n=1)
    assert cmd.command_type == LiveCommandType.STOP
    assert cmd.deployment_id == dep_id
    assert cmd.payload["reason"] == "user"


@pytest.mark.asyncio
async def test_acked_commands_do_not_reappear_on_next_consume(
    bus: LiveCommandBus,
) -> None:
    """After ACK, XREADGROUP must not redeliver the entry on the next
    consume() call. This is the baseline Redis Streams consumer-group
    semantic — the PEL recovery path is tested separately below."""
    dep_id = uuid4()
    await bus.publish_start(dep_id, {})

    [cmd] = await _consume_n(bus, "consumer-a", n=1)
    await bus.ack(cmd.entry_id)

    # Second consume must time out (no new entries) — use a tight
    # asyncio wait that raises if something IS yielded unexpectedly.
    stop_event = asyncio.Event()

    async def _should_be_empty() -> LiveCommand | None:
        async for c in bus.consume("consumer-a", stop_event):
            stop_event.set()
            return c
        return None

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_should_be_empty(), timeout=1.5)


@pytest.mark.asyncio
async def test_three_commands_consumed_and_acked_in_order(
    bus: LiveCommandBus,
) -> None:
    dep_ids = [uuid4() for _ in range(3)]
    for dep_id in dep_ids:
        await bus.publish_start(dep_id, {"n": str(dep_id)})

    commands = await _consume_n(bus, "consumer-a", n=3)
    assert [c.deployment_id for c in commands] == dep_ids

    for c in commands:
        await bus.ack(c.entry_id)


# ---------------------------------------------------------------------------
# PEL recovery (XAUTOCLAIM) — decision #12 / Codex v3 P0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unacked_entry_is_redelivered_by_recover_pending(
    bus: LiveCommandBus,
) -> None:
    """A consumer that yielded an entry WITHOUT ACKing (simulating a
    crash) must see the same entry again on the next consume() call —
    via the explicit XAUTOCLAIM step in ``_recover_pending``, NOT via
    Redis's own redelivery (which does not happen for consumer groups).
    """
    dep_id = uuid4()
    await bus.publish_start(dep_id, {"crash": "simulated"})

    # First pass: yield but don't ACK (simulates crash).
    [first] = await _consume_n(bus, "consumer-a", n=1)
    assert first.deployment_id == dep_id

    # Second pass: the recover_pending step must yield the same entry.
    # Using the SAME consumer_id so we verify the recovery path works
    # for self-recovery too (handler crashed, pod restarted).
    [recovered] = await _consume_n(bus, "consumer-a", n=1)
    assert recovered.entry_id == first.entry_id
    assert recovered.deployment_id == dep_id


@pytest.mark.asyncio
async def test_peer_crash_recovered_by_different_consumer(
    bus: LiveCommandBus,
) -> None:
    """A peer consumer crashes mid-flight. A different consumer (e.g.
    a newly-started supervisor pod) must reclaim the pending entry via
    XAUTOCLAIM when ``min_idle_ms`` is reached."""
    dep_id = uuid4()
    await bus.publish_start(dep_id, {})

    # Peer consumes but crashes (no ACK)
    [_peer] = await _consume_n(bus, "consumer-crashed", n=1)

    # Different consumer reclaims the entry
    [recovered] = await _consume_n(bus, "consumer-restarted", n=1)
    assert recovered.deployment_id == dep_id


@pytest.mark.asyncio
async def test_idempotency_key_preserved_through_publish_consume(
    bus: LiveCommandBus,
) -> None:
    """The ``idempotency_key`` the supervisor uses for dedupe (decision
    #13) must round-trip verbatim through the stream."""
    dep_id = uuid4()
    await bus.publish_start(dep_id, {}, idempotency_key="user-supplied-key-abc")

    [cmd] = await _consume_n(bus, "consumer-a", n=1)
    assert cmd.idempotency_key == "user-supplied-key-abc"


@pytest.mark.asyncio
async def test_idempotency_key_autogenerated_when_not_supplied(
    bus: LiveCommandBus,
) -> None:
    """When the caller doesn't supply a key, the bus must mint a stable
    per-deployment default so supervisor dedupe still works."""
    dep_id = uuid4()
    await bus.publish_start(dep_id, {})

    [cmd] = await _consume_n(bus, "consumer-a", n=1)
    assert cmd.idempotency_key
    # The default form is derived from the deployment UUID so two
    # publishes for the same deployment (without explicit keys) collide
    # and dedupe on the supervisor side.
    await bus.ack(cmd.entry_id)
    await bus.publish_start(dep_id, {})
    [second] = await _consume_n(bus, "consumer-a", n=1)
    assert second.idempotency_key == cmd.idempotency_key


@pytest.mark.asyncio
async def test_ack_of_unknown_entry_does_not_raise(
    bus: LiveCommandBus,
) -> None:
    """``ack`` must be safe to call with a stale/unknown entry id —
    Redis XACK returns 0 for unknown entries. A handler that retries
    ACK after a transient error shouldn't crash the whole loop."""
    await bus.ack("0-0")  # never-existed entry id


# ---------------------------------------------------------------------------
# Group / stream names match the plan constants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_constants_are_stable() -> None:
    """Guard against accidental renames — the stream/group names are
    part of the decision #12 wire contract between API + supervisor."""
    assert LIVE_COMMAND_STREAM == "msai:live:commands"
    assert LIVE_COMMAND_GROUP == "live-supervisor"
    assert LIVE_COMMAND_DLQ_STREAM == "msai:live:commands:dlq"
