"""Integration tests for ``LiveCommandBus`` DLQ (Phase 1 task 1.6 — poison handling).

Verifies the Codex v4 P2 poison-message DLQ:

- Entries whose delivery_count reaches ``MAX_DELIVERY_ATTEMPTS`` get
  copied onto ``msai:live:commands:dlq`` (with diagnostic metadata)
  and XACKed on the primary stream so they stop bouncing.
- The alerting service hook is invoked on every DLQ move so oncall
  notices a poison message the same day it appears.
- The original payload + ``idempotency_key`` are preserved on the DLQ
  entry so the operator can manually replay or triage.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from msai.services.live_command_bus import (
    LIVE_COMMAND_DLQ_STREAM,
    LIVE_COMMAND_STREAM,
    MAX_DELIVERY_ATTEMPTS,
    LiveCommand,
    LiveCommandBus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis


@pytest.fixture(scope="module")
def isolated_redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def redis_client(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    with contextlib.suppress(Exception):
        await client.delete(LIVE_COMMAND_STREAM, LIVE_COMMAND_DLQ_STREAM)
    try:
        yield client
    finally:
        await client.delete(LIVE_COMMAND_STREAM, LIVE_COMMAND_DLQ_STREAM)
        await client.aclose()


@pytest_asyncio.fixture
async def bus_with_alert(
    redis_client: AsyncRedis,
) -> tuple[LiveCommandBus, MagicMock]:
    """A bus wired with a mock alert callback. ``min_idle_ms=0`` so
    ``_recover_pending`` reclaims on the very next consume — lets us
    simulate 5 delivery attempts in a single test without sleeping."""
    alert_mock = MagicMock()
    bus = LiveCommandBus(
        redis=redis_client,
        min_idle_ms=0,
        recovery_interval_s=60,
        max_delivery_attempts=MAX_DELIVERY_ATTEMPTS,
        alert_callback=alert_mock,
    )
    await bus.ensure_group()
    return bus, alert_mock


async def _consume_one(
    bus: LiveCommandBus, consumer_id: str, *, timeout_s: float = 2.0
) -> LiveCommand | None:
    """Consume a SINGLE command, then stop. Returns None if nothing
    was yielded within the timeout (used by the DLQ test to confirm
    the primary stream is drained after a DLQ move)."""
    stop_event = asyncio.Event()
    captured: list[LiveCommand] = []

    async def _drain() -> None:
        async for cmd in bus.consume(consumer_id, stop_event):
            captured.append(cmd)
            stop_event.set()
            return

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    except TimeoutError:
        return None
    return captured[0] if captured else None


@pytest.mark.asyncio
async def test_poison_message_moved_to_dlq_after_max_attempts(
    bus_with_alert: tuple[LiveCommandBus, MagicMock],
    redis_client: AsyncRedis,
) -> None:
    """Codex v4 P2: an entry whose handler never ACKs must be moved to
    the DLQ stream after ``MAX_DELIVERY_ATTEMPTS`` delivery attempts,
    then XACKed on the primary stream so it stops bouncing.

    The fixture's ``min_idle_ms=0`` lets ``_recover_pending`` reclaim
    the entry on every consume() call, so we can simulate 5 delivery
    attempts in one test without sleeping.
    """
    bus, alert_mock = bus_with_alert
    dep_id = uuid4()
    await bus.publish_start(dep_id, {"key": "value"}, idempotency_key="poison-key-1")

    # Simulate N consecutive delivery attempts without ACK. The entry
    # bounces through _recover_pending each iteration; its delivery_count
    # in the PEL grows by 1 per attempt. After MAX_DELIVERY_ATTEMPTS it
    # must be routed to the DLQ instead of yielded.
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        cmd = await _consume_one(bus, "consumer-a")
        if cmd is None:
            break  # moved to DLQ; yield loop is now empty

    # Next consume must see NOTHING on the primary stream — the poison
    # message has been moved to DLQ and XACKed.
    after_dlq = await _consume_one(bus, "consumer-a", timeout_s=1.0)
    assert after_dlq is None, (
        "primary stream must be empty after DLQ move; otherwise the poison "
        "message would keep bouncing"
    )

    # DLQ stream must now contain one entry with the original payload
    # plus the diagnostic metadata.
    dlq_entries = await redis_client.xrange(LIVE_COMMAND_DLQ_STREAM)
    assert len(dlq_entries) == 1
    _entry_id, fields = dlq_entries[0]
    assert fields["deployment_id"] == str(dep_id)
    assert fields["dlq_reason"] == "max_attempts"
    assert int(fields["delivery_count"]) >= MAX_DELIVERY_ATTEMPTS
    assert "original_entry_id" in fields
    assert "moved_at" in fields
    # Original payload preserved verbatim so an operator can replay.
    # Each payload_* field is JSON-encoded (see LiveCommandBus._publish),
    # so "value" → the JSON string '"value"'.
    import json as _json

    assert _json.loads(fields["payload_key"]) == "value"
    # Idempotency key round-trips so the supervisor's dedupe still applies
    # if the operator replays the command from the DLQ.
    assert fields["idempotency_key"] == "poison-key-1"

    # Alerting hook fired at least once with the DLQ context.
    assert alert_mock.called, "alert callback must fire on DLQ move"
    call_kwargs = alert_mock.call_args.kwargs
    assert call_kwargs.get("reason") == "max_attempts"
    assert "entry_id" in call_kwargs


@pytest.mark.asyncio
async def test_dlq_move_acks_original_entry_on_primary_stream(
    bus_with_alert: tuple[LiveCommandBus, MagicMock],
    redis_client: AsyncRedis,
) -> None:
    """After the DLQ move, the original entry must no longer appear
    in the PEL — otherwise the supervisor's XPENDING queries would
    keep showing a phantom backlog."""
    bus, _alert = bus_with_alert
    dep_id = uuid4()
    await bus.publish_start(dep_id, {})

    for _ in range(MAX_DELIVERY_ATTEMPTS):
        _ = await _consume_one(bus, "consumer-a")

    # XPENDING with no range args returns the summary: (count, min, max, consumers)
    summary = await redis_client.xpending(
        LIVE_COMMAND_STREAM,
        "live-supervisor",
    )
    # redis-py returns a dict for the summary form
    pending_count = summary["pending"] if isinstance(summary, dict) else summary[0]
    assert pending_count == 0, (
        f"primary stream PEL must be empty after DLQ ack; got {pending_count}"
    )


@pytest.mark.asyncio
async def test_non_poison_messages_are_not_sent_to_dlq(
    bus_with_alert: tuple[LiveCommandBus, MagicMock],
    redis_client: AsyncRedis,
) -> None:
    """Sanity: a command the handler ACKs promptly must NEVER appear
    on the DLQ. Ensures the DLQ path is only triggered by the
    delivery-count ceiling, not by normal consumption."""
    bus, alert_mock = bus_with_alert
    dep_id = uuid4()
    await bus.publish_start(dep_id, {"paper_trading": True})

    cmd = await _consume_one(bus, "consumer-a")
    assert cmd is not None
    await bus.ack(cmd.entry_id)

    dlq_len = await redis_client.xlen(LIVE_COMMAND_DLQ_STREAM)
    assert dlq_len == 0
    assert not alert_mock.called
