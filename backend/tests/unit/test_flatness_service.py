"""Tests for the API-side flatness coordination service
(``services/live/flatness_service.py``).

Two responsibilities under test:
1. ``coalesce_or_publish_stop_with_flatness`` — SET-NX on
   ``inflight_stop:{deployment_id}`` so concurrent /stop callers
   converge on a single publish.
2. ``poll_stop_report`` — GET-based polling on ``stop_report:{nonce}``
   with exponential backoff and a wall-clock deadline.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest

from msai.services.live.flatness_service import (
    coalesce_or_publish_stop_with_flatness,
    poll_stop_report,
)


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def fake_bus() -> AsyncMock:
    bus = AsyncMock()
    bus.publish_stop_and_report_flatness = AsyncMock(return_value="entry-id")
    return bus


# ---------------------------------------------------------------------------
# coalesce_or_publish_stop_with_flatness
# ---------------------------------------------------------------------------


class TestCoalesceOrPublish:
    @pytest.mark.asyncio
    async def test_originator_publishes_and_returns_new_nonce(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        fake_bus: AsyncMock,
    ) -> None:
        deployment_id = uuid4()
        nonce, is_originator = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis,
            bus=fake_bus,
            deployment_id=deployment_id,
            member_strategy_id_fulls=["Strat-0-slug"],
        )
        assert is_originator is True
        assert nonce  # non-empty hex string
        fake_bus.publish_stop_and_report_flatness.assert_awaited_once()
        kw = fake_bus.publish_stop_and_report_flatness.call_args.kwargs
        assert kw["stop_nonce"] == nonce
        assert kw["member_strategy_id_fulls"] == ["Strat-0-slug"]

        stored = await fake_redis.get(f"inflight_stop:{deployment_id}")
        assert stored == nonce

    @pytest.mark.asyncio
    async def test_second_caller_coalesces_onto_existing_nonce(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        fake_bus: AsyncMock,
    ) -> None:
        """Bug #2 / Codex iter-6 P2 #1: second concurrent /stop caller
        must coalesce onto the originator's nonce instead of publishing
        a duplicate command + creating a phantom flatness ticket."""
        deployment_id = uuid4()
        first_nonce, first_origin = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis, bus=fake_bus, deployment_id=deployment_id, member_strategy_id_fulls=[]
        )
        second_nonce, second_origin = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis, bus=fake_bus, deployment_id=deployment_id, member_strategy_id_fulls=[]
        )
        assert first_origin is True
        assert second_origin is False
        assert second_nonce == first_nonce
        # Only ONE publish across the two calls.
        assert fake_bus.publish_stop_and_report_flatness.await_count == 1

    @pytest.mark.asyncio
    async def test_different_deployments_get_independent_nonces(
        self,
        fake_redis: fakeredis.aioredis.FakeRedis,
        fake_bus: AsyncMock,
    ) -> None:
        d1, d2 = uuid4(), uuid4()
        n1, _ = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis, bus=fake_bus, deployment_id=d1, member_strategy_id_fulls=[]
        )
        n2, _ = await coalesce_or_publish_stop_with_flatness(
            redis=fake_redis, bus=fake_bus, deployment_id=d2, member_strategy_id_fulls=[]
        )
        assert n1 != n2
        assert fake_bus.publish_stop_and_report_flatness.await_count == 2


# ---------------------------------------------------------------------------
# poll_stop_report
# ---------------------------------------------------------------------------


class TestPollStopReport:
    @pytest.mark.asyncio
    async def test_returns_report_when_key_materializes(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        nonce = "abc123"
        payload = {"stop_nonce": nonce, "broker_flat": True, "remaining_positions": []}
        await fake_redis.set(f"stop_report:{nonce}", json.dumps(payload), ex=120)

        report = await poll_stop_report(redis=fake_redis, stop_nonce=nonce, deadline_s=1.0)
        assert report == payload

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self, fake_redis: fakeredis.aioredis.FakeRedis) -> None:
        report = await poll_stop_report(
            redis=fake_redis,
            stop_nonce="no-such-nonce",
            deadline_s=0.2,
            initial_interval_s=0.05,
            max_interval_s=0.1,
        )
        assert report is None

    @pytest.mark.asyncio
    async def test_does_not_delete_report_on_hit(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """Codex iter-6 P2 #2: do NOT DEL on read — coalesced callers
        may poll the same nonce. The 120s TTL handles cleanup."""
        nonce = "abc123"
        await fake_redis.set(f"stop_report:{nonce}", json.dumps({"broker_flat": True}), ex=120)
        await poll_stop_report(redis=fake_redis, stop_nonce=nonce, deadline_s=1.0)
        # Key still present for a second reader.
        assert (await fake_redis.get(f"stop_report:{nonce}")) is not None

    @pytest.mark.asyncio
    async def test_returns_report_appearing_mid_poll(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        nonce = "abc123"
        payload = {"stop_nonce": nonce, "broker_flat": True}

        async def writer() -> None:
            await asyncio.sleep(0.1)
            await fake_redis.set(f"stop_report:{nonce}", json.dumps(payload), ex=120)

        writer_task = asyncio.create_task(writer())
        report = await poll_stop_report(
            redis=fake_redis,
            stop_nonce=nonce,
            deadline_s=1.0,
            initial_interval_s=0.02,
            max_interval_s=0.05,
        )
        await writer_task
        assert report == payload

    @pytest.mark.asyncio
    async def test_corrupt_payload_falls_through_to_timeout(
        self, fake_redis: fakeredis.aioredis.FakeRedis
    ) -> None:
        """If a corrupt non-JSON value lands in stop_report:{nonce}, the
        poll doesn't crash — it ignores and continues until deadline."""
        nonce = "abc123"
        await fake_redis.set(f"stop_report:{nonce}", "not-json", ex=120)
        report = await poll_stop_report(
            redis=fake_redis,
            stop_nonce=nonce,
            deadline_s=0.2,
            initial_interval_s=0.05,
            max_interval_s=0.1,
        )
        assert report is None
