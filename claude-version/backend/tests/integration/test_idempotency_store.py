"""Integration tests for ``IdempotencyStore`` (Phase 1 task 1.14).

Uses a dedicated testcontainers Redis instance so the SETNX/TTL/GET
semantics are exercised against the real server, not a mock.

Covers:
- reserve() returns Reserved on first call, InFlight on second with same body
- reserve() returns BodyMismatchReservation when a pending slot sees a
  different body_hash
- reserve() returns CachedOutcome when a prior commit() completed
- reserve() returns BodyMismatchReservation when a cached outcome
  exists for a different body
- commit() rejects non-cacheable outcomes (ValueError)
- release() deletes the reservation so the next reserve() can retry
- body_hash() is deterministic and order-independent
- User-scoping: same key under different user_ids → two independent
  reservations
- Concurrent reserve() race: exactly one Reserved, exactly one InFlight
- commit() then reserve() round-trip preserves the outcome exactly
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio

from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import (
    RESERVATION_TTL_S,
    BodyMismatchReservation,
    CachedOutcome,
    EndpointOutcome,
    IdempotencyStore,
    InFlight,
    Reserved,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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

    client = aioredis.from_url(isolated_redis_url, decode_responses=False)
    # Clean slate per test so cross-test key collisions don't leak.
    with contextlib.suppress(Exception):
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def store(redis_client: AsyncRedis) -> IdempotencyStore:
    return IdempotencyStore(redis=redis_client)


# ---------------------------------------------------------------------------
# body_hash
# ---------------------------------------------------------------------------


class TestBodyHash:
    def test_identical_dicts_produce_same_hash(self) -> None:
        h1 = IdempotencyStore.body_hash({"a": 1, "b": 2})
        h2 = IdempotencyStore.body_hash({"a": 1, "b": 2})
        assert h1 == h2

    def test_key_order_is_irrelevant(self) -> None:
        """Canonical-JSON (sorted keys) means dict ordering does not
        affect the hash — critical for cross-Python-version
        reproducibility."""
        h1 = IdempotencyStore.body_hash({"a": 1, "b": 2})
        h2 = IdempotencyStore.body_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_values_produce_different_hashes(self) -> None:
        h1 = IdempotencyStore.body_hash({"a": 1})
        h2 = IdempotencyStore.body_hash({"a": 2})
        assert h1 != h2

    def test_hash_is_sha256_hex(self) -> None:
        h = IdempotencyStore.body_hash({})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# reserve / commit / release — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_first_call_returns_reserved(store: IdempotencyStore) -> None:
    user_id = uuid4()
    result = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(result, Reserved)
    assert result.redis_key.startswith("msai:idem:start:")


@pytest.mark.asyncio
async def test_reserve_second_call_same_body_returns_in_flight(
    store: IdempotencyStore,
) -> None:
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r1, Reserved)
    assert isinstance(r2, InFlight)


@pytest.mark.asyncio
async def test_reserve_second_call_different_body_returns_body_mismatch(
    store: IdempotencyStore,
) -> None:
    """A concurrent request hitting a still-pending reservation with a
    DIFFERENT body_hash means the caller is reusing the idempotency
    key with a different request body. Treat it as body_mismatch —
    not in_flight — because in_flight implies the caller can retry
    and eventually succeed."""
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h2")
    assert isinstance(r1, Reserved)
    assert isinstance(r2, BodyMismatchReservation)


@pytest.mark.asyncio
async def test_commit_then_reserve_returns_cached_outcome(
    store: IdempotencyStore,
) -> None:
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r1, Reserved)

    outcome = EndpointOutcome.ready({"id": "abc", "status": "running"})
    await store.commit(r1.redis_key, "h1", outcome)

    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r2, CachedOutcome)
    assert r2.outcome.status_code == 201
    assert r2.outcome.response == {"id": "abc", "status": "running"}
    assert r2.outcome.failure_kind == FailureKind.NONE
    assert r2.outcome.cacheable is True


@pytest.mark.asyncio
async def test_commit_then_reserve_different_body_returns_body_mismatch(
    store: IdempotencyStore,
) -> None:
    """After a commit, reusing the key with a DIFFERENT body hash
    must return BodyMismatchReservation — the caller cannot
    overwrite the cached response, and must NOT receive it
    (different body means different expected response)."""
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r1, Reserved)

    await store.commit(
        r1.redis_key,
        "h1",
        EndpointOutcome.ready({"id": "abc"}),
    )

    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h2")
    assert isinstance(r2, BodyMismatchReservation)


@pytest.mark.asyncio
async def test_release_allows_next_reserve_to_win(store: IdempotencyStore) -> None:
    """release() deletes the reservation so the next retry with the
    same key gets a fresh Reserved. Used after transient outcomes
    (HALT_ACTIVE, API_POLL_TIMEOUT) and on raised exceptions."""
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r1, Reserved)

    await store.release(r1.redis_key)

    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r2, Reserved)


@pytest.mark.asyncio
async def test_user_scoping_isolates_same_key_across_users(
    store: IdempotencyStore,
) -> None:
    """Two different users using the same Idempotency-Key get two
    independent reservations — Codex v4 P2 regression. A leak here
    would let an attacker observe another user's cached response."""
    user_a = uuid4()
    user_b = uuid4()

    r_a = await store.reserve(user_id=user_a, key="same-key", body_hash="h1")
    r_b = await store.reserve(user_id=user_b, key="same-key", body_hash="h2")

    assert isinstance(r_a, Reserved)
    assert isinstance(r_b, Reserved)
    assert r_a.redis_key != r_b.redis_key


# ---------------------------------------------------------------------------
# commit() guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_rejects_non_cacheable_outcome(store: IdempotencyStore) -> None:
    """commit() on a non-cacheable outcome is a programming error —
    the endpoint should have called release()."""
    user_id = uuid4()
    r = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r, Reserved)

    non_cacheable = EndpointOutcome.halt_active()
    with pytest.raises(ValueError, match="non-cacheable"):
        await store.commit(r.redis_key, "h1", non_cacheable)


# ---------------------------------------------------------------------------
# Concurrent race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_reserve_race_exactly_one_winner(
    store: IdempotencyStore,
) -> None:
    """Codex v4 P2 regression for Task 1.14: 10 concurrent reserve()
    calls with the same user+key+body must produce exactly ONE
    Reserved and the rest InFlight. A naive check-then-insert
    without SETNX would let multiple callers through."""
    user_id = uuid4()

    async def _reserve() -> object:
        return await store.reserve(user_id=user_id, key="race-key", body_hash="h1")

    results = await asyncio.gather(*(_reserve() for _ in range(10)))

    reserved = [r for r in results if isinstance(r, Reserved)]
    in_flight = [r for r in results if isinstance(r, InFlight)]

    assert len(reserved) == 1, f"expected exactly 1 Reserved, got {len(reserved)}"
    assert len(in_flight) == 9, f"expected 9 InFlight, got {len(in_flight)}"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_then_reserve_roundtrip_preserves_permanent_failure(
    store: IdempotencyStore,
) -> None:
    """A cached permanent_failure outcome must survive
    JSON serialization and come back with the same failure_kind
    enum member."""
    user_id = uuid4()
    r1 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r1, Reserved)

    outcome = EndpointOutcome.permanent_failure(
        FailureKind.RECONCILIATION_FAILED,
        "trader.is_running=False; data_engine.check_connected()=False",
    )
    await store.commit(r1.redis_key, "h1", outcome)

    r2 = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r2, CachedOutcome)
    assert r2.outcome.status_code == 503
    assert r2.outcome.failure_kind == FailureKind.RECONCILIATION_FAILED
    assert r2.outcome.cacheable is True
    assert "trader.is_running=False" in r2.outcome.response["detail"]
    assert r2.outcome.response["failure_kind"] == "reconciliation_failed"


# ---------------------------------------------------------------------------
# TTL sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_key_has_reservation_ttl(
    store: IdempotencyStore,
    redis_client: AsyncRedis,
) -> None:
    """The reservation marker is set with EX=RESERVATION_TTL_S so a
    crashed caller eventually releases the slot."""
    user_id = uuid4()
    r = await store.reserve(user_id=user_id, key="k1", body_hash="h1")
    assert isinstance(r, Reserved)

    ttl = await redis_client.ttl(r.redis_key)
    # TTL is within the expected band — between 1 and RESERVATION_TTL_S
    # inclusive (the server may have ticked down a second by the time
    # we read it).
    assert 1 <= ttl <= RESERVATION_TTL_S
