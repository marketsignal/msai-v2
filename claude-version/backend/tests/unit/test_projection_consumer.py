"""Unit tests for ProjectionConsumer (Phase 3 task 3.4).

We use a fake async Redis stub to drive the consumer through
the read → translate → publish → ACK path without standing up
a real Redis. The integration test against testcontainers
covers the XAUTOCLAIM PEL recovery + DLQ paths end-to-end.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from msgspec import msgpack

from msai.services.nautilus.projection.consumer import (
    DLQ_STREAM_PREFIX,
    ProjectionConsumer,
    default_consumer_name,
)
from msai.services.nautilus.projection.fanout import DualPublisher
from msai.services.nautilus.projection.registry import StreamRegistry


class FakeRedis:
    """Records every Redis call the consumer makes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.acked: list[tuple[str, str]] = []
        self.created_groups: list[str] = []
        self.dlq_xadds: list[tuple[str, dict[Any, Any]]] = []
        self.xpending_attempts: dict[tuple[str, str], int] = {}

    async def publish(self, channel: str, payload: bytes) -> int:
        self.published.append((channel, payload))
        return 1

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append((stream, entry_id))
        return 1

    async def xgroup_create(self, *, name: str, groupname: str, id: str, mkstream: bool) -> bool:
        self.created_groups.append(name)
        return True

    async def xadd(self, stream: str, fields: dict[Any, Any]) -> str:
        self.dlq_xadds.append((stream, fields))
        return "1-0"

    async def xpending_range(
        self, *, name: str, groupname: str, min: str, max: str, count: int
    ) -> list[dict[str, Any]]:
        attempts = self.xpending_attempts.get((name, min), 0)
        if attempts == 0:
            return []
        return [
            {
                "message_id": min,
                "consumer": "x",
                "time_since_delivered": 0,
                "times_delivered": attempts,
            }
        ]


def _build_consumer(
    redis: FakeRedis | None = None,
) -> tuple[ProjectionConsumer, FakeRedis, StreamRegistry]:
    redis = redis or FakeRedis()
    registry = StreamRegistry()
    publisher = DualPublisher(redis)  # type: ignore[arg-type]
    consumer = ProjectionConsumer(
        redis=redis,  # type: ignore[arg-type]
        registry=registry,
        publisher=publisher,
        consumer_name="test-consumer",
    )
    return consumer, redis, registry


def test_default_consumer_name_format() -> None:
    name = default_consumer_name()
    assert name.startswith("projection-")
    # hostname-pid format
    assert name.count("-") >= 2


def test_extract_payload_handles_bytes_key() -> None:
    fields = {b"payload": b"hello"}
    assert ProjectionConsumer._extract_payload(fields) == b"hello"


def test_extract_payload_handles_str_key() -> None:
    fields = {"payload": b"hello"}
    assert ProjectionConsumer._extract_payload(fields) == b"hello"


def test_extract_topic_decodes_bytes() -> None:
    fields = {b"topic": b"events.position.opened"}
    assert ProjectionConsumer._extract_topic(fields) == "events.position.opened"


def test_extract_topic_handles_str() -> None:
    fields = {"topic": "events.account.state"}
    assert ProjectionConsumer._extract_topic(fields) == "events.account.state"


def test_resolve_deployment_id_from_stream_strips_prefix_suffix() -> None:
    consumer, _, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    result = consumer._resolve_deployment_id_from_stream("trader-MSAI-ema-cross-stream")  # noqa: SLF001
    assert result == deployment_id


def test_resolve_deployment_id_from_stream_returns_none_for_unknown_shape() -> None:
    consumer, _, _ = _build_consumer()
    assert consumer._resolve_deployment_id_from_stream("not-a-trader-stream") is None  # noqa: SLF001


def test_resolve_deployment_id_from_stream_returns_none_for_unknown_slug() -> None:
    consumer, _, _ = _build_consumer()
    # Correct shape but the slug isn't registered
    assert (
        consumer._resolve_deployment_id_from_stream("trader-MSAI-mystery-stream") is None  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_ensure_group_creates_consumer_group() -> None:
    consumer, redis, _ = _build_consumer()
    await consumer.ensure_group("trader-MSAI-x-stream")
    assert "trader-MSAI-x-stream" in redis.created_groups


@pytest.mark.asyncio
async def test_ensure_group_swallows_busygroup_error() -> None:
    class BusyRedis(FakeRedis):
        async def xgroup_create(
            self, *, name: str, groupname: str, id: str, mkstream: bool
        ) -> bool:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")

    consumer, _, _ = _build_consumer(redis=BusyRedis())
    # Must NOT raise
    await consumer.ensure_group("trader-MSAI-y-stream")


@pytest.mark.asyncio
async def test_ensure_group_propagates_other_errors() -> None:
    class FailingRedis(FakeRedis):
        async def xgroup_create(
            self, *, name: str, groupname: str, id: str, mkstream: bool
        ) -> bool:
            raise RuntimeError("connection refused")

    consumer, _, _ = _build_consumer(redis=FailingRedis())
    with pytest.raises(RuntimeError, match="connection refused"):
        await consumer.ensure_group("trader-MSAI-z-stream")


@pytest.mark.asyncio
async def test_dispatch_entry_translates_publishes_and_acks() -> None:
    consumer, redis, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    payload = msgpack.encode(
        {
            "instrument_id": "AAPL.NASDAQ",
            "quantity": "100",
            "avg_px_open": "150",
            "unrealized_pnl": "0",
            "realized_pnl": "0",
            "ts_event": 1_700_000_000_000_000_000,
        }
    )
    fields = {b"topic": b"events.position.opened", b"payload": payload}

    await consumer._dispatch_entry("trader-MSAI-ema-cross-stream", "1-0", fields)  # noqa: SLF001

    # Both pub/sub channels were written
    assert len(redis.published) == 2
    # XACK fired
    assert redis.acked == [("trader-MSAI-ema-cross-stream", "1-0")]


@pytest.mark.asyncio
async def test_dispatch_entry_unrouted_topic_acks_without_publishing() -> None:
    consumer, redis, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    payload = msgpack.encode({"foo": "bar"})
    fields = {b"topic": b"events.bar.bar", b"payload": payload}

    await consumer._dispatch_entry("trader-MSAI-ema-cross-stream", "1-1", fields)  # noqa: SLF001

    assert redis.published == []
    assert redis.acked == [("trader-MSAI-ema-cross-stream", "1-1")]


@pytest.mark.asyncio
async def test_dispatch_entry_unknown_deployment_acks() -> None:
    consumer, redis, _ = _build_consumer()
    payload = msgpack.encode({"instrument_id": "AAPL.NASDAQ", "quantity": "1", "avg_px_open": "1"})
    fields = {b"topic": b"events.position.opened", b"payload": payload}

    # Stream isn't in the registry — must ACK + skip, not crash
    await consumer._dispatch_entry("trader-MSAI-unknown-stream", "1-2", fields)  # noqa: SLF001

    assert redis.published == []
    assert redis.acked == [("trader-MSAI-unknown-stream", "1-2")]


@pytest.mark.asyncio
async def test_dispatch_entry_payload_not_dict_acks() -> None:
    consumer, redis, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    payload = msgpack.encode("not a dict")
    fields = {b"topic": b"events.position.opened", b"payload": payload}

    await consumer._dispatch_entry("trader-MSAI-ema-cross-stream", "1-3", fields)  # noqa: SLF001

    assert redis.published == []
    assert redis.acked == [("trader-MSAI-ema-cross-stream", "1-3")]


@pytest.mark.asyncio
async def test_dispatch_entry_translation_failure_no_ack() -> None:
    """Translation errors must NOT ACK — leave the entry in the
    PEL so XAUTOCLAIM picks it up next sweep."""
    consumer, redis, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    # Bad fill side will raise inside _translate_fill
    payload = msgpack.encode({"order_side": "FLAT", "last_qty": "1", "last_px": "1"})
    fields = {b"topic": b"events.order.filled", b"payload": payload}

    await consumer._dispatch_entry("trader-MSAI-ema-cross-stream", "1-4", fields)  # noqa: SLF001

    # No publish, no ack — entry stays in PEL
    assert redis.published == []
    assert redis.acked == []


@pytest.mark.asyncio
async def test_move_to_dlq_writes_dlq_stream_and_acks_primary() -> None:
    consumer, redis, registry = _build_consumer()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    fields = {b"topic": b"events.order.filled", b"payload": b"poison-bytes"}
    await consumer._move_to_dlq("trader-MSAI-ema-cross-stream", "1-5", fields)  # noqa: SLF001

    # DLQ XADD recorded
    assert len(redis.dlq_xadds) == 1
    dlq_stream, dlq_fields = redis.dlq_xadds[0]
    assert dlq_stream == f"{DLQ_STREAM_PREFIX}{deployment_id}"
    assert dlq_fields["dlq_reason"] == "max_delivery_attempts_exceeded"
    assert dlq_fields["original_entry_id"] == "1-5"
    assert dlq_fields["original_stream"] == "trader-MSAI-ema-cross-stream"
    # Primary is XACKed so it stops being redelivered
    assert redis.acked == [("trader-MSAI-ema-cross-stream", "1-5")]


@pytest.mark.asyncio
async def test_move_to_dlq_invokes_alert_callback() -> None:
    received: list[tuple[str, dict[str, Any]]] = []

    async def on_alert(name: str, payload: dict[str, Any]) -> None:
        received.append((name, payload))

    redis = FakeRedis()
    registry = StreamRegistry()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )
    publisher = DualPublisher(redis)  # type: ignore[arg-type]
    consumer = ProjectionConsumer(
        redis=redis,  # type: ignore[arg-type]
        registry=registry,
        publisher=publisher,
        on_alert=on_alert,
    )

    await consumer._move_to_dlq(  # noqa: SLF001
        "trader-MSAI-ema-cross-stream",
        "1-6",
        {b"topic": b"events.order.filled", b"payload": b"poison"},
    )

    assert len(received) == 1
    assert received[0][0] == "projection_dlq"
    assert received[0][1]["entry_id"] == "1-6"


@pytest.mark.asyncio
async def test_delivery_attempts_returns_count_from_xpending() -> None:
    redis = FakeRedis()
    redis.xpending_attempts[("trader-MSAI-x-stream", "1-0")] = 3
    consumer, _, _ = _build_consumer(redis=redis)

    attempts = await consumer._delivery_attempts("trader-MSAI-x-stream", "1-0")  # noqa: SLF001
    assert attempts == 3


@pytest.mark.asyncio
async def test_delivery_attempts_returns_zero_when_empty() -> None:
    consumer, _, _ = _build_consumer()
    attempts = await consumer._delivery_attempts("trader-MSAI-x-stream", "1-0")  # noqa: SLF001
    assert attempts == 0
