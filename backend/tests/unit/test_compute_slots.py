"""Unit tests for the Redis-based compute slot semaphore.

All Redis operations are mocked via ``unittest.mock.AsyncMock`` so these
tests run without a running Redis instance.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    describe_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis(
    *,
    smembers_return: set[bytes] | None = None,
    get_side_effect: Any = None,
) -> AsyncMock:
    """Build an ``AsyncMock`` that quacks like ``ArqRedis``."""
    redis = AsyncMock()
    redis.smembers = AsyncMock(return_value=smembers_return or set())
    redis.set = AsyncMock()
    redis.sadd = AsyncMock()
    redis.srem = AsyncMock()
    redis.delete = AsyncMock()

    if get_side_effect is not None:
        redis.get = AsyncMock(side_effect=get_side_effect)
    else:
        redis.get = AsyncMock(return_value=None)

    return redis


def _lease_payload(
    lease_id: str = "test-lease",
    job_kind: str = "backtest",
    job_id: str = "job-1",
    slot_count: int = 1,
) -> str:
    return json.dumps({
        "lease_id": lease_id,
        "job_kind": job_kind,
        "job_id": job_id,
        "slot_count": slot_count,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }, sort_keys=True)


# ---------------------------------------------------------------------------
# acquire_compute_slots
# ---------------------------------------------------------------------------

class TestAcquireComputeSlots:
    """Test ``acquire_compute_slots``."""

    @pytest.mark.asyncio
    async def test_acquire_returns_lease_id(self) -> None:
        """Acquire succeeds when slots are available and returns a UUID string."""
        redis = _make_redis()

        lease_id = await acquire_compute_slots(
            redis,
            job_kind="backtest",
            job_id="bt-001",
            slot_count=1,
        )

        assert isinstance(lease_id, str)
        assert len(lease_id) == 36  # UUID4 format: 8-4-4-4-12
        redis.set.assert_awaited_once()
        redis.sadd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_acquire_stores_correct_payload(self) -> None:
        """The JSON payload written to Redis contains the expected fields."""
        redis = _make_redis()

        lease_id = await acquire_compute_slots(
            redis,
            job_kind="research",
            job_id="res-42",
            slot_count=2,
        )

        call_args = redis.set.call_args
        stored_key: str = call_args[0][0]
        stored_json: str = call_args[0][1]
        stored_payload = json.loads(stored_json)

        assert stored_key == f"msai:compute:lease:{lease_id}"
        assert stored_payload["lease_id"] == lease_id
        assert stored_payload["job_kind"] == "research"
        assert stored_payload["job_id"] == "res-42"
        assert stored_payload["slot_count"] == 2
        assert "updated_at" in stored_payload

    @pytest.mark.asyncio
    async def test_acquire_adds_to_active_set(self) -> None:
        """The lease_id is added to the ``msai:compute:active`` set."""
        redis = _make_redis()

        lease_id = await acquire_compute_slots(
            redis,
            job_kind="backtest",
            job_id="bt-001",
        )

        redis.sadd.assert_awaited_once_with("msai:compute:active", lease_id)

    @pytest.mark.asyncio
    async def test_acquire_respects_slot_limit(self) -> None:
        """When all slots are taken, acquire raises ``ComputeSlotUnavailableError``."""
        # Simulate 4 active leases each holding 1 slot (limit=4)
        existing_ids = {b"lease-a", b"lease-b", b"lease-c", b"lease-d"}

        def _get_side_effect(key: str) -> str | None:
            for lid in ["lease-a", "lease-b", "lease-c", "lease-d"]:
                if key == f"msai:compute:lease:{lid}":
                    return _lease_payload(lease_id=lid)
            return None

        redis = _make_redis(
            smembers_return=existing_ids,
            get_side_effect=_get_side_effect,
        )

        with pytest.raises(ComputeSlotUnavailableError, match="Timed out"):
            await acquire_compute_slots(
                redis,
                job_kind="backtest",
                job_id="bt-blocked",
                slot_count=1,
                timeout_seconds=0,
            )

    @pytest.mark.asyncio
    async def test_acquire_clamps_slot_count_to_limit(self) -> None:
        """Requesting more slots than the limit clamps to the limit."""
        redis = _make_redis()

        with patch("msai.services.compute_slots.settings") as mock_settings:
            mock_settings.compute_slot_limit = 4
            mock_settings.compute_slot_wait_seconds = 5
            mock_settings.compute_slot_lease_seconds = 120
            mock_settings.compute_slot_poll_seconds = 1

            await acquire_compute_slots(
                redis,
                job_kind="backtest",
                job_id="bt-big",
                slot_count=99,  # way over limit
            )

        stored_json: str = redis.set.call_args[0][1]
        stored_payload = json.loads(stored_json)
        assert stored_payload["slot_count"] == 4  # clamped to limit


# ---------------------------------------------------------------------------
# renew_compute_slots
# ---------------------------------------------------------------------------

class TestRenewComputeSlots:
    """Test ``renew_compute_slots``."""

    @pytest.mark.asyncio
    async def test_renew_updates_payload_and_ttl(self) -> None:
        """Renew reads the existing payload, updates ``updated_at``, and re-sets with TTL."""
        existing = _lease_payload(lease_id="renew-me")
        redis = _make_redis(get_side_effect=lambda _key: existing)

        await renew_compute_slots(redis, "renew-me")

        redis.set.assert_awaited_once()
        call_args = redis.set.call_args
        stored_json = call_args[0][1]
        stored = json.loads(stored_json)
        assert stored["lease_id"] == "renew-me"
        # TTL is set via `ex=` keyword
        assert call_args[1]["ex"] == 120  # default lease_seconds

    @pytest.mark.asyncio
    async def test_renew_noop_for_expired_lease(self) -> None:
        """If the lease key is gone (expired), renew is a no-op."""
        redis = _make_redis()  # get returns None

        await renew_compute_slots(redis, "gone-lease")

        redis.set.assert_not_awaited()


# ---------------------------------------------------------------------------
# release_compute_slots
# ---------------------------------------------------------------------------

class TestReleaseComputeSlots:
    """Test ``release_compute_slots``."""

    @pytest.mark.asyncio
    async def test_release_deletes_key_and_removes_from_set(self) -> None:
        """Release deletes the lease key and removes the id from the active set."""
        redis = _make_redis()

        await release_compute_slots(redis, "release-me")

        redis.delete.assert_awaited_once_with("msai:compute:lease:release-me")
        redis.srem.assert_awaited_once_with("msai:compute:active", "release-me")


# ---------------------------------------------------------------------------
# describe_compute_slots
# ---------------------------------------------------------------------------

class TestDescribeComputeSlots:
    """Test ``describe_compute_slots``."""

    @pytest.mark.asyncio
    async def test_describe_empty(self) -> None:
        """With no active leases, reports all slots available."""
        redis = _make_redis()

        result = await describe_compute_slots(redis)

        assert result["total"] == 4
        assert result["used"] == 0
        assert result["available"] == 4
        assert result["leases"] == []

    @pytest.mark.asyncio
    async def test_describe_with_active_leases(self) -> None:
        """Reports correct counts when leases are active."""
        existing_ids = {b"l1", b"l2"}

        def _get_side_effect(key: str) -> str | None:
            if key == "msai:compute:lease:l1":
                return _lease_payload(lease_id="l1", slot_count=1)
            if key == "msai:compute:lease:l2":
                return _lease_payload(lease_id="l2", slot_count=2)
            return None

        redis = _make_redis(
            smembers_return=existing_ids,
            get_side_effect=_get_side_effect,
        )

        result = await describe_compute_slots(redis)

        assert result["total"] == 4
        assert result["used"] == 3
        assert result["available"] == 1
        assert len(result["leases"]) == 2

    @pytest.mark.asyncio
    async def test_describe_prunes_stale_leases(self) -> None:
        """Stale lease ids (key expired but still in set) are pruned."""
        existing_ids = {b"live-lease", b"dead-lease"}

        def _get_side_effect(key: str) -> str | None:
            if key == "msai:compute:lease:live-lease":
                return _lease_payload(lease_id="live-lease")
            return None  # dead-lease key expired

        redis = _make_redis(
            smembers_return=existing_ids,
            get_side_effect=_get_side_effect,
        )

        result = await describe_compute_slots(redis)

        assert result["used"] == 1
        assert len(result["leases"]) == 1
        # Stale id should have been removed from the active set
        redis.srem.assert_awaited_once_with("msai:compute:active", "dead-lease")
