"""Narrow restart test for Nautilus ``Cache(database=redis)`` durability.

The whole db-backed-strategy-registry PR rests on one architectural claim
from the planning council: *Nautilus's Cache owns the Instrument payload;
MSAI's ``instrument_definitions`` table holds only control-plane metadata
(aliases, effective dates, audit trail)*. If Instruments don't survive a
``Cache`` recreation against the same Redis, that whole split collapses
and we'd have to persist the payload ourselves.

This test directly verifies the claim at the lowest level — no
TradingNode, no IB Gateway, no Rust kernel — by:

1. Building a ``Cache`` wired through ``CacheDatabaseAdapter`` → Redis.
2. Calling ``cache_a.add_instrument(inst)`` (writes to Redis via
   ``database.pyx:1914`` ``_database.add_instrument``).
3. Disposing cache_a's adapter.
4. Building a NEW ``CacheDatabaseAdapter`` + ``Cache`` against the same
   Redis, using the SAME ``TraderId`` so the key prefix matches.
5. Calling ``cache_b.cache_instruments()`` (``cache.pyx:328`` — loads
   from the backing ``load_instruments()`` call at ``database.pyx:340``).
6. Asserting the instrument comes back with the same id + type.

The setup differs from the plan's sketch:

- The plan wrote ``Cache(config=cache_cfg)`` but Nautilus's
  ``Cache.__init__`` expects a ``CacheDatabaseFacade`` (an adapter
  INSTANCE), not a ``CacheConfig`` with a ``database=`` field. The
  config-only form silently gives you an in-memory cache because
  ``_database`` stays ``None`` — which would make the test always pass
  even if Redis persistence were broken. The right pattern is
  ``Cache(database=CacheDatabaseAdapter(...), config=...)``, which is
  the same pattern :mod:`msai.services.nautilus.projection.position_reader`
  uses in production (``position_reader.py:196-230``).

- The plan used ``cache_a.flush_db()`` as a "flush buffered writes"
  call. That's wrong — ``flush_db`` is a ``FLUSHDB`` in the Redis
  sense: *delete all persisted data*. Calling it between add and
  reload would make the round-trip tautologically fail. We call
  ``adapter.close()`` instead, which is the correct "dispose" action.
  Writes are already synchronous by default (no ``buffer_interval_ms``
  set), so there's nothing to flush.

- ``TestInstrumentProvider`` exposes ``equity(symbol, venue)`` not
  ``aapl_equity()``. We use ``equity("AAPL", "XNAS")``.

- We define a local ``isolated_redis_url`` fixture instead of using the
  session-scoped ``redis_url`` in :mod:`tests.conftest`. That shared
  fixture calls ``RedisContainer.get_connection_url()``, a method the
  installed ``testcontainers`` version no longer exposes — every other
  integration test that needs Redis (``test_idempotency_store``,
  ``test_live_command_bus``, ``test_process_manager``, etc.) defines
  its own ``isolated_redis_url`` the same way. We mirror that pattern
  to keep this change narrowly focused on the PR's architectural
  question; fixing the shared fixture is out of scope.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import msgspec
import pytest
from nautilus_trader.cache.cache import Cache  # type: ignore[import-not-found]
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.cache.database import (  # type: ignore[import-not-found]
    CacheDatabaseAdapter,
)
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
    TraderId,
)
from nautilus_trader.serialization.serializer import (  # type: ignore[import-not-found]
    MsgSpecSerializer,
)
from nautilus_trader.test_kit.providers import TestInstrumentProvider

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_redis() -> Iterator[tuple[str, int]]:
    """Spin up a module-scoped Redis container and yield (host, port).

    See the module docstring for why this is local rather than using the
    shared ``redis_url`` from ``tests/conftest.py``.
    """
    from testcontainers.redis import RedisContainer  # type: ignore[import-untyped]

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        yield host, port


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_cache(
    *,
    redis_host: str,
    redis_port: int,
    trader_id: TraderId,
) -> tuple[Cache, CacheDatabaseAdapter]:
    """Construct a ``Cache`` wired through Redis.

    Returns both the ``Cache`` and the ``CacheDatabaseAdapter`` so the
    caller can close the adapter explicitly at the end of each
    "process" — mimicking what a TradingNode does on ``dispose()``.

    The ``MsgSpecSerializer`` is constructed the same way
    ``nautilus_trader/system/kernel.py:313-317`` constructs it in the
    live path, so the on-wire format is identical to what a live
    TradingNode would produce.
    """
    db_cfg = DatabaseConfig(type="redis", host=redis_host, port=redis_port)
    cache_cfg = CacheConfig(database=db_cfg, encoding="msgpack")
    adapter = CacheDatabaseAdapter(
        trader_id=trader_id,
        instance_id=UUID4(),  # Fresh per adapter instance; NOT the trader id.
        serializer=MsgSpecSerializer(
            encoding=msgspec.msgpack,
            timestamps_as_str=True,
            timestamps_as_iso8601=False,
        ),
        config=cache_cfg,
    )
    cache = Cache(database=adapter, config=cache_cfg)
    return cache, adapter


def test_instrument_persists_across_cache_recreate(
    isolated_redis: tuple[str, int],
) -> None:
    """Write an Instrument via one ``Cache`` → Redis → tear that Cache
    down → construct a brand new ``Cache`` pointed at the same Redis
    with the same ``TraderId`` → confirm the instrument loads back.

    This is the central durability claim of the whole PR: the
    ``instrument_definitions`` table holds *only* control-plane metadata
    (aliases, effective-date windows, audit trail). The actual
    ``Instrument`` payload lives in Nautilus's Redis-backed cache.
    If this test fails, the architecture falls apart.
    """
    # Arrange
    host, port = isolated_redis
    # Unique TraderId per test run — Nautilus prefixes all keys with
    # ``trader-{trader_id}:`` (CacheConfig.use_trader_prefix=True), so a
    # fresh trader id guarantees we won't collide with anything a prior
    # test left behind in the (session-scoped) testcontainers Redis.
    trader_id = TraderId(f"MSAI-ROUNDTRIP-{uuid.uuid4().hex[:12]}")

    expected = TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS")

    # Act — "process A": write and dispose.
    cache_a, adapter_a = _build_cache(
        redis_host=host,
        redis_port=port,
        trader_id=trader_id,
    )
    try:
        cache_a.add_instrument(expected)
    finally:
        adapter_a.close()  # Severs the Redis connection; payload stays.

    # Act — "process B": fresh Cache, same Redis + TraderId, reload.
    cache_b, adapter_b = _build_cache(
        redis_host=host,
        redis_port=port,
        trader_id=trader_id,
    )
    try:
        cache_b.cache_instruments()  # Populates from Redis via load_instruments().
        retrieved = cache_b.instrument(expected.id)
    finally:
        adapter_b.close()

    # Assert
    assert retrieved is not None, (
        "Cache(database=redis) did NOT persist the Instrument across "
        "Cache recreation. The whole db-backed-strategy-registry PR's "
        "architecture depends on this working."
    )
    assert retrieved.id == expected.id
    assert type(retrieved) is type(expected)
