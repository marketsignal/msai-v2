"""Tests for the child-subprocess flatness drain
(``_drain_and_report_flatness`` in trading_node_subprocess.py).

Bug #2 (live-deploy-safety-trio): on shutdown the child drains
``flatness_pending:{deployment_id}`` and writes a per-nonce
``stop_report:{nonce}`` Redis key the API picks up via GET polling.

Uses fakeredis so the test exercises the real aioredis client path
without needing a Redis container.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis
import pytest


# Match the production import shape (``redis.asyncio.from_url``) so the
# patch at the production call site uses the fakeredis factory.
@pytest.fixture
def fake_redis_module(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """Patch redis.asyncio.from_url() to return a fakeredis instance.

    The instance is created once per test so RPUSH-ed tickets and the
    drained stop_report keys live in the same in-memory store and
    assertions can read them back.
    """
    server = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _factory(_url: str, **_kwargs: object) -> fakeredis.aioredis.FakeRedis:
        return server

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", _factory)
    return server


def _fake_position(*, strategy_id: str, instrument_id: str, quantity: str) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        quantity=quantity,
        side="LONG",
    )


def _fake_node(*, positions_open: list[SimpleNamespace]) -> SimpleNamespace:
    cache = SimpleNamespace(positions_open=lambda: positions_open)
    kernel = SimpleNamespace(cache=cache)
    return SimpleNamespace(kernel=kernel)


@pytest.mark.asyncio
async def test_drain_writes_stop_report_when_flat(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """Happy path: child cache shows no open positions for the deployment's
    members → stop_report has broker_flat=True and empty remaining_positions."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    nonce = "abc123"
    members = ["EMACrossStrategy-0-slug1"]
    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": members}),
    )

    node = _fake_node(positions_open=[])
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    raw = await fake_redis_module.get(f"stop_report:{nonce}")
    assert raw is not None
    report = json.loads(raw)
    assert report["broker_flat"] is True
    assert report["remaining_positions"] == []
    assert report["stop_nonce"] == nonce
    assert report["reason"] == "ok"


@pytest.mark.asyncio
async def test_drain_filters_positions_by_member_strategy_id_fulls(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """Positions belonging to OTHER strategies (not in the request's
    member list) must be filtered out — they're not this deployment's
    responsibility (Codex iter-2 P1 #3)."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    nonce = "abc123"
    my_strategy = "EMACrossStrategy-0-slug1"
    other_strategy = "OtherStrategy-0-slug2"

    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": [my_strategy]}),
    )

    node = _fake_node(
        positions_open=[
            _fake_position(strategy_id=other_strategy, instrument_id="X.NASDAQ", quantity="10"),
        ]
    )
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    raw = await fake_redis_module.get(f"stop_report:{nonce}")
    report = json.loads(raw)
    # Other strategy's position is filtered out → deployment is flat.
    assert report["broker_flat"] is True
    assert report["remaining_positions"] == []


@pytest.mark.asyncio
async def test_drain_reports_non_flat_when_my_positions_remain(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """If a member's strategy_id matches and has open positions, the
    report must surface them with broker_flat=False."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    nonce = "abc123"
    my_strategy = "EMACrossStrategy-0-slug1"

    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": [my_strategy]}),
    )

    node = _fake_node(
        positions_open=[
            _fake_position(strategy_id=my_strategy, instrument_id="AAPL.NASDAQ", quantity="1"),
        ]
    )
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    raw = await fake_redis_module.get(f"stop_report:{nonce}")
    report = json.loads(raw)
    assert report["broker_flat"] is False
    assert len(report["remaining_positions"]) == 1
    assert report["remaining_positions"][0]["strategy_id"] == my_strategy
    assert report["reason"] == "max_attempts_exhausted"


@pytest.mark.asyncio
async def test_drain_handles_multiple_concurrent_stops(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """Two RPUSH-ed tickets (concurrent stop calls) — both get drained
    and both get their own stop_report keys (Codex iter-4 P2 #1 — list,
    not singleton key)."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    members = ["EMACrossStrategy-0-slug1"]

    for nonce in ("nonce-a", "nonce-b"):
        await fake_redis_module.rpush(
            f"flatness_pending:{deployment_id}",
            json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": members}),
        )

    node = _fake_node(positions_open=[])
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    raw_a = await fake_redis_module.get("stop_report:nonce-a")
    raw_b = await fake_redis_module.get("stop_report:nonce-b")
    assert raw_a is not None
    assert raw_b is not None
    assert json.loads(raw_a)["stop_nonce"] == "nonce-a"
    assert json.loads(raw_b)["stop_nonce"] == "nonce-b"


@pytest.mark.asyncio
async def test_drain_with_empty_list_is_noop(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """If no flatness_pending ticket was published (the deployment got
    SIGTERMed via the regular /stop path, not /stop-and-report-flatness),
    the drain block is a no-op — no stop_report key is written."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    node = _fake_node(positions_open=[])
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    # No keys touched.
    keys = await fake_redis_module.keys("stop_report:*")
    assert keys == []


@pytest.mark.asyncio
async def test_drain_with_empty_redis_url_is_noop(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """Defensive: an empty redis_url (test fixtures sometimes omit it)
    must not crash the shutdown path. The helper returns silently."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    node = _fake_node(positions_open=[])
    # Should not raise. No assertion on side-effects — fakeredis is
    # untouched because we never called from_url.
    await _drain_and_report_flatness(node=node, deployment_id=uuid4(), redis_url="")


@pytest.mark.asyncio
async def test_drain_skips_ticket_with_missing_nonce(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """A ticket missing ``stop_nonce`` is logged and skipped (don't crash
    the loop on a malformed entry from a misbehaving producer)."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"member_strategy_id_fulls": ["EMACross-0-slug1"]}),  # no stop_nonce
    )

    node = _fake_node(positions_open=[])
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    # Helper completed; no stop_report key created for the malformed entry.
    keys = await fake_redis_module.keys("stop_report:*")
    assert keys == []


@pytest.mark.asyncio
async def test_cache_read_failure_reports_non_flat(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """PR #65 Codex P1: when ``cache.positions_open()`` raises during
    shutdown, the helper MUST NOT report ``broker_flat=True`` — the
    verification mechanism itself has failed, so the operator must be
    told positions are unverified. Surface ``broker_flat=False`` with
    ``reason='cache_read_failed'``."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    nonce = "abc123"
    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": ["S-0-slug"]}),
    )

    class _ExplodingCache:
        def positions_open(self) -> list:
            raise RuntimeError("Rust cache adapter went away")

    node = SimpleNamespace(kernel=SimpleNamespace(cache=_ExplodingCache()))

    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    raw = await fake_redis_module.get(f"stop_report:{nonce}")
    assert raw is not None
    report = json.loads(raw)
    assert report["broker_flat"] is False, (
        "cache-read failure must NOT report flat — would mask unverified state"
    )
    assert report["reason"] == "cache_read_failed"


@pytest.mark.asyncio
async def test_stop_report_has_ttl(
    fake_redis_module: fakeredis.aioredis.FakeRedis,
) -> None:
    """The stop_report key must have a TTL (120s) so abandoned keys
    don't accumulate in Redis."""
    from msai.services.nautilus.trading_node_subprocess import (
        _drain_and_report_flatness,
    )

    deployment_id = uuid4()
    nonce = "abc123"
    await fake_redis_module.rpush(
        f"flatness_pending:{deployment_id}",
        json.dumps({"stop_nonce": nonce, "member_strategy_id_fulls": []}),
    )

    node = _fake_node(positions_open=[])
    await _drain_and_report_flatness(
        node=node, deployment_id=deployment_id, redis_url="redis://fake"
    )

    ttl = await fake_redis_module.ttl(f"stop_report:{nonce}")
    # fakeredis returns the remaining TTL in seconds. Expect ~120s
    # (give a wide tolerance — wall-clock between SET and TTL read).
    assert 100 < ttl <= 120
