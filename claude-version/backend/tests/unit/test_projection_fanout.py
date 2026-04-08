"""Unit tests for the dual pub/sub fanout (Phase 3 task 3.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    PositionSnapshot,
)
from msai.services.nautilus.projection.fanout import (
    EVENTS_CHANNEL_PREFIX,
    STATE_CHANNEL_PREFIX,
    DualPublisher,
    events_channel_for,
    state_channel_for,
)


class FakeRedis:
    """Minimal async Redis stub that records every PUBLISH call."""

    def __init__(self, state_subs: int = 1, events_subs: int = 1) -> None:
        self.state_subs = state_subs
        self.events_subs = events_subs
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, channel: str, payload: bytes) -> int:
        self.publishes.append((channel, payload))
        return self.state_subs if channel.startswith(STATE_CHANNEL_PREFIX) else self.events_subs


def test_state_channel_for_uses_prefix() -> None:
    deployment_id = uuid4()
    assert state_channel_for(deployment_id) == f"{STATE_CHANNEL_PREFIX}{deployment_id}"


def test_events_channel_for_uses_prefix() -> None:
    deployment_id = uuid4()
    assert events_channel_for(deployment_id) == f"{EVENTS_CHANNEL_PREFIX}{deployment_id}"


@pytest.mark.asyncio
async def test_publish_writes_to_both_channels() -> None:
    fake = FakeRedis(state_subs=2, events_subs=3)
    publisher = DualPublisher(fake)  # type: ignore[arg-type]
    deployment_id = uuid4()
    event = PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id="AAPL.NASDAQ",
        qty=Decimal("100"),
        avg_price=Decimal("150"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=datetime.now(UTC),
    )

    state_subs, events_subs = await publisher.publish(event)

    assert state_subs == 2
    assert events_subs == 3
    assert len(fake.publishes) == 2

    state_call, events_call = fake.publishes
    assert state_call[0] == f"{STATE_CHANNEL_PREFIX}{deployment_id}"
    assert events_call[0] == f"{EVENTS_CHANNEL_PREFIX}{deployment_id}"


@pytest.mark.asyncio
async def test_publish_payload_is_identical_on_both_channels() -> None:
    fake = FakeRedis()
    publisher = DualPublisher(fake)  # type: ignore[arg-type]
    event = AccountStateUpdate(
        deployment_id=uuid4(),
        account_id="DU12345",
        balance=Decimal("100000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        ts=datetime.now(UTC),
    )

    await publisher.publish(event)

    state_payload = fake.publishes[0][1]
    events_payload = fake.publishes[1][1]
    assert state_payload == events_payload
    assert state_payload == event.model_dump_json().encode("utf-8")


@pytest.mark.asyncio
async def test_publish_propagates_redis_failure() -> None:
    class FailingRedis:
        async def publish(self, channel: str, payload: bytes) -> int:
            raise RuntimeError("redis down")

    publisher = DualPublisher(FailingRedis())  # type: ignore[arg-type]
    event = PositionSnapshot(
        deployment_id=uuid4(),
        instrument_id="AAPL.NASDAQ",
        qty=Decimal("100"),
        avg_price=Decimal("150"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=datetime.now(UTC),
    )

    with pytest.raises(RuntimeError, match="redis down"):
        await publisher.publish(event)
