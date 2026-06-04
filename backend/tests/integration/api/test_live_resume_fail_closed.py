"""Integration: fail-closed preconditions on fleet ``POST /api/v1/live/resume`` (PR 1b T6).

Resume is the operator's recovery path after a kill-all OR a data-stale
auto-halt. PR 1b T6 makes it FAIL-CLOSED: it refuses (409) to clear the fleet
halt latch unless, for EVERY active deployment,

* the data-stale monitor's required-feed MANIFEST is present (an ABSENT manifest
  means the monitor never started / the node is gone → ``RESUME_BLOCKED_DATA_STALE``),
* every manifest feed has a present, TTL-alive freshness verdict equal to
  ``warm`` (absent/expired/``stale`` → ``RESUME_BLOCKED_DATA_STALE`` naming the feed),
* the node still carries a reconciliation marker (absent →
  ``RESUME_BLOCKED_RECONCILIATION``).

A clean resume re-verifies the SAME preconditions atomically inside
``RESUME_CLEAR_LUA`` (closing the monitor-death race between the Python probe
and the clear) before deleting the halt keyset, and returns a receipt of the
verified preconditions.

ARRANGE follows the established integration pattern in
``test_live_start_broker_account.py``: dedicated PostgresContainer +
RedisContainer per module, deployments seeded directly via the model, freshness
state seeded directly on the TEXT Redis client (the monitor's wire format).

SAFETY: paper accounts only (``DU...`` prefixes); no real node spawns.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api.live_deps import get_command_bus
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.halt_keys import (
    RESUME_CLEAR_LUA,
    VERDICT_KEY_SUFFIX,
    HaltCause,
    data_freshness_key,
    data_freshness_manifest_key,
    fleet_halt_key,
    halt_cause_key,
    reconciled_key,
)
from msai.main import app
from msai.models import Base
from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio import LivePortfolio
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.strategy import Strategy
from msai.models.user import User
from msai.services.live_command_bus import LiveCommandBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


_HALT = fleet_halt_key()
_DATASET = "EQUS.MINI"
_BAR_TYPE = "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="module")
def isolated_redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_text(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    with contextlib.suppress(Exception):
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def bus(redis_text: AsyncRedis) -> LiveCommandBus:
    return LiveCommandBus(redis=redis_text, min_idle_ms=0, recovery_interval_s=60)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    bus: LiveCommandBus,
) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_get_bus() -> LiveCommandBus:
        return bus

    async def _override_user() -> dict:
        return {"sub": "test-operator", "oid": str(uuid4())}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus
    app.dependency_overrides[get_current_user] = _override_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_active_deployment(
    session: AsyncSession,
    *,
    account_id: str = "DUP-RESUME-1",
    status: str = "running",
) -> LiveDeployment:
    """Seed the minimal User → Strategy → Portfolio → Revision → Deployment
    chain needed for an ACTIVE-status deployment the /resume probe will inspect."""
    user = User(
        id=uuid4(),
        entra_id=f"rfc-{uuid4().hex[:12]}",
        email=f"rfc-{uuid4().hex[:8]}@example.com",
        role="trader",
    )
    session.add(user)
    await session.flush()

    strategy = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()

    portfolio = LivePortfolio(
        id=uuid4(),
        name=f"P-{uuid4().hex[:8]}",
        description="resume fail-closed test",
        created_by=user.id,
    )
    session.add(portfolio)
    await session.flush()

    revision = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=portfolio.id,
        revision_number=1,
        composition_hash=uuid4().hex + uuid4().hex,
        is_frozen=True,
    )
    session.add(revision)
    await session.flush()

    slug = uuid4().hex[:16]
    dep = LiveDeployment(
        id=uuid4(),
        strategy_id=strategy.id,
        status=status,
        paper_trading=True,
        started_by=user.id,
        deployment_slug=slug,
        identity_signature=uuid4().hex + uuid4().hex,
        trader_id=f"T-{slug[:8]}",
        strategy_id_full=f"EMACrossStrategy-{slug}",
        account_id=account_id,
        ib_login_key="msai-paper-primary",
        portfolio_revision_id=revision.id,
        message_bus_stream=f"mbus:{slug}",
        broker_account_id=None,
    )
    session.add(dep)
    await session.commit()
    return dep


async def _seed_halt_latch(redis: AsyncRedis, *, cause: dict | None = None) -> None:
    """Set the fleet halt latch + cause companion (+ history) so /resume has
    something to clear. Mirrors what /kill-all or the data-stale monitor write."""
    await redis.set(_HALT, "true", ex=86400)
    await redis.set(f"{_HALT}:set_by", "test", ex=86400)
    await redis.set(f"{_HALT}:set_at", "2026-06-03T00:00:00+00:00", ex=86400)
    cause = cause or {"reason": HaltCause.FLEET_EMERGENCY.value, "source": "operator"}
    await redis.set(halt_cause_key("fleet"), json.dumps(cause), ex=86400)
    await redis.rpush(f"{halt_cause_key('fleet')}:history", json.dumps(cause))


async def _seed_manifest(
    redis: AsyncRedis,
    deployment_id: str,
    *,
    feeds: list[dict] | None = None,
) -> None:
    """Publish the monitor's required-feed manifest for a deployment.

    ``feeds=None`` → one default AAPL feed; ``feeds=[]`` → an EMPTY manifest
    (legacy / non-Databento node, vacuously warm for freshness)."""
    if feeds is None:
        feeds = [{"dataset": _DATASET, "feed": _BAR_TYPE, "symbol": "AAPL"}]
    await redis.set(data_freshness_manifest_key(deployment_id), json.dumps(feeds), ex=120)


async def _seed_feed_verdict(
    redis: AsyncRedis,
    deployment_id: str,
    *,
    dataset: str = _DATASET,
    bar_type: str = _BAR_TYPE,
    verdict: str = "warm",
) -> None:
    """Publish a per-feed verdict companion key (the monitor's wire format)."""
    key = data_freshness_key(deployment_id, dataset, bar_type) + VERDICT_KEY_SUFFIX
    await redis.set(key, verdict, ex=120)


async def _seed_reconciled(redis: AsyncRedis, deployment_id: str) -> None:
    await redis.set(reconciled_key(deployment_id), "2026-06-03T00:00:00+00:00")


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_warm_and_reconciled_clears_all_halt_keys(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An operator who killed the fleet, confirmed the feed recovered, and is
    ready to re-deploy: resume succeeds (200), clears the latch + ALL cause
    keys (incl. legacy ``:reason`` / ``:source``) + ``stop_unknown`` markers,
    and the response carries the verified preconditions. A follow-up /status
    then shows the fleet is no longer halted."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    # Legacy transition-compat keys a pre-T5 node wrote ON THE LATCH KEY itself
    # (``msai:risk:halt:reason`` / ``:source`` — see disconnect_handler
    # ``_HALT_REASON_KEY`` / ``_HALT_SOURCE_KEY``), plus a stale stop_unknown.
    await redis_text.set(f"{_HALT}:reason", "data_stale", ex=86400)
    await redis_text.set(f"{_HALT}:source", "monitor", ex=86400)
    await redis_text.set("stop_unknown:abc", "1", ex=86400)
    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_verdict(redis_text, dep_id, verdict="warm")
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resumed"] is True
    verified = body["verified"]
    assert verified["active_deployments_checked"] == 1
    assert f"{dep_id}:{_DATASET}:{_BAR_TYPE}" in verified["feeds_verified"]
    assert dep_id in verified["reconciled_verified"]

    # Latch + every cause key cleared.
    assert await redis_text.exists(_HALT) == 0
    assert await redis_text.exists(f"{_HALT}:set_by") == 0
    assert await redis_text.exists(f"{_HALT}:set_at") == 0
    assert await redis_text.exists(halt_cause_key("fleet")) == 0
    assert await redis_text.exists(f"{halt_cause_key('fleet')}:history") == 0
    assert await redis_text.exists(f"{_HALT}:reason") == 0
    assert await redis_text.exists(f"{_HALT}:source") == 0
    # stop_unknown marker cleared.
    assert await redis_text.exists("stop_unknown:abc") == 0


@pytest.mark.asyncio
async def test_resume_zero_active_deployments_vacuous_pass(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
) -> None:
    """No active deployments → resume is a vacuous pass: it clears the latch
    and reports zero deployments checked. (No DB seed; the only state is the
    halt latch itself.)"""
    await _seed_halt_latch(redis_text)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resumed"] is True
    assert body["verified"]["active_deployments_checked"] == 0
    assert await redis_text.exists(_HALT) == 0


@pytest.mark.asyncio
async def test_resume_empty_manifest_legacy_node_passes(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legacy / non-Databento node publishes an EMPTY manifest (no feeds).
    It is vacuously warm for freshness but still must be reconciled — resume
    succeeds (200) and clears the latch."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id, feeds=[])  # EMPTY → vacuous
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"]["active_deployments_checked"] == 1
    assert await redis_text.exists(_HALT) == 0


# ---------------------------------------------------------------------------
# Refusal paths (data-stale)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_stale_feed_409_names_feed_and_keeps_latch(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A feed whose verdict is ``stale`` blocks resume (409
    ``RESUME_BLOCKED_DATA_STALE``), names the feed, and leaves the latch set."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_verdict(redis_text, dep_id, verdict="stale")
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert f"{dep_id}:{_DATASET}:{_BAR_TYPE}" in err["message"]
    # Latch UNTOUCHED.
    assert await redis_text.exists(_HALT) == 1


@pytest.mark.asyncio
async def test_resume_pending_feed_409_names_no_data_observed(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-2 P1 — a feed whose verdict is ``pending`` (no data observed
    yet, within startup grace) blocks resume (409 ``RESUME_BLOCKED_DATA_STALE``)
    with a DISTINCT 'no data observed yet' message (not the stale/absent
    message), and leaves the latch set. This prevents an operator clearing the
    halt after a node restart DURING an ongoing data outage."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_verdict(redis_text, dep_id, verdict="pending")
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert "no data observed yet" in err["message"]
    assert f"{dep_id}:{_DATASET}:{_BAR_TYPE}" in err["message"]
    # Latch UNTOUCHED.
    assert await redis_text.exists(_HALT) == 1


@pytest.mark.asyncio
async def test_resume_manifest_feed_with_no_verdict_row_409(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A manifest lists a feed but the per-feed verdict row is absent/expired —
    the feed is API-derived ``missing`` → 409, latch retained."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id)  # lists the feed
    # NO verdict row seeded → absent/expired.
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert await redis_text.exists(_HALT) == 1


@pytest.mark.asyncio
async def test_resume_missing_manifest_409(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ABSENT manifest means the monitor never started (or the node is gone
    + the manifest TTL lapsed) → fail-closed 409, latch retained."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    # NO manifest seeded.
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert await redis_text.exists(_HALT) == 1


@pytest.mark.asyncio
async def test_resume_non_list_manifest_409_not_500(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A syntactically-valid but NON-LIST manifest (e.g. a JSON object) is
    malformed → fail-closed 409 ``RESUME_BLOCKED_DATA_STALE``, latch retained.
    Must NOT 500 (FIX 4 — shape validation, fail-closed not fail-open)."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    # Parses fine as JSON, but it's a dict, not the expected list.
    await redis_text.set(
        data_freshness_manifest_key(dep_id), json.dumps({"dataset": _DATASET}), ex=120
    )
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert await redis_text.exists(_HALT) == 1


@pytest.mark.asyncio
async def test_resume_manifest_entry_missing_feed_409_not_500(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A manifest list whose entry is missing the 'feed' key is malformed →
    fail-closed 409, latch retained. Pre-FIX this raised KeyError → 500."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    # Entry has 'dataset' but no 'feed'.
    await _seed_manifest(redis_text, dep_id, feeds=[{"dataset": _DATASET, "symbol": "AAPL"}])
    await _seed_reconciled(redis_text, dep_id)

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert await redis_text.exists(_HALT) == 1


# ---------------------------------------------------------------------------
# Refusal path (reconciliation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_missing_reconciled_marker_409(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A warm feed but NO reconciliation marker → 409
    ``RESUME_BLOCKED_RECONCILIATION``, latch retained. The node is not proven
    genuinely running."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_verdict(redis_text, dep_id, verdict="warm")
    # NO reconciled marker.

    resp = await client.post("/api/v1/live/resume")
    assert resp.status_code == 409, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "RESUME_BLOCKED_RECONCILIATION"
    assert dep_id in err["message"]
    assert await redis_text.exists(_HALT) == 1


# ---------------------------------------------------------------------------
# RESUME_CLEAR_LUA atomic abort semantics (monitor-death race)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_clear_lua_aborts_on_manifest_disappearing_mid_clear(
    redis_text: AsyncRedis,
) -> None:
    """Direct unit-style test of the atomic clear's monitor-death race guard:
    if a manifest key vanishes between the Python probe and the clear, the Lua
    re-check aborts — deleting NOTHING and returning the offending key.

    Seeds state that would pass the Python probe, then deletes one manifest key
    and invokes RESUME_CLEAR_LUA directly (simulating the race window)."""
    dep_id = str(uuid4())
    manifest_key = data_freshness_manifest_key(dep_id)
    verdict_key = data_freshness_key(dep_id, _DATASET, _BAR_TYPE) + VERDICT_KEY_SUFFIX
    rec_key = reconciled_key(dep_id)
    delete_keys = [_HALT, f"{_HALT}:set_by", halt_cause_key("fleet")]

    await redis_text.set(_HALT, "true")
    await redis_text.set(f"{_HALT}:set_by", "test")
    await redis_text.set(halt_cause_key("fleet"), "{}")
    await redis_text.set(verdict_key, "warm")
    await redis_text.set(rec_key, "ts")
    # Manifest key DELIBERATELY absent (the race: monitor died after the probe).

    lua_keys = [manifest_key, verdict_key, rec_key, *delete_keys]
    result = await redis_text.eval(RESUME_CLEAR_LUA, len(lua_keys), *lua_keys, "1", "1", "1")
    result_str = result.decode() if isinstance(result, bytes) else str(result)
    assert result_str.startswith("MANIFEST_MISSING:")
    assert manifest_key in result_str
    # NOTHING deleted — the latch survives the aborted clear.
    assert await redis_text.exists(_HALT) == 1
    assert await redis_text.exists(halt_cause_key("fleet")) == 1


@pytest.mark.asyncio
async def test_resume_clear_lua_clears_on_all_preconditions_met(
    redis_text: AsyncRedis,
) -> None:
    """Happy-path of the atomic clear: all preconditions present → returns
    ``OK`` and deletes exactly the delete-key list."""
    dep_id = str(uuid4())
    manifest_key = data_freshness_manifest_key(dep_id)
    verdict_key = data_freshness_key(dep_id, _DATASET, _BAR_TYPE) + VERDICT_KEY_SUFFIX
    rec_key = reconciled_key(dep_id)
    delete_keys = [_HALT, f"{_HALT}:set_by", halt_cause_key("fleet")]

    expected_manifest = "[]"
    await redis_text.set(manifest_key, expected_manifest)
    await redis_text.set(verdict_key, "warm")
    await redis_text.set(rec_key, "ts")
    for k in delete_keys:
        await redis_text.set(k, "x")

    lua_keys = [manifest_key, verdict_key, rec_key, *delete_keys]
    # ARGV: n_manifest, n_verdict, n_reconciled, then the expected raw manifest
    # value(s) — one per manifest key, in order (TOCTOU content-pin).
    result = await redis_text.eval(
        RESUME_CLEAR_LUA, len(lua_keys), *lua_keys, "1", "1", "1", expected_manifest
    )
    result_str = result.decode() if isinstance(result, bytes) else str(result)
    assert result_str == "OK"
    for k in delete_keys:
        assert await redis_text.exists(k) == 0
    # Check keys survive (only delete-keys are removed).
    assert await redis_text.exists(manifest_key) == 1
    assert await redis_text.exists(verdict_key) == 1


@pytest.mark.asyncio
async def test_resume_clear_lua_aborts_on_manifest_value_changed_mid_clear(
    redis_text: AsyncRedis,
) -> None:
    """Codex iter-21 P1 — changed-universe TOCTOU: if the monitor OVERWRITES a
    manifest with a CHANGED feed universe between the Python probe and the
    atomic clear, the Lua must abort (``MANIFEST_CHANGED:<key>``) and delete
    NOTHING — the latch survives. The pre-derived verdict-key list reflects the
    OLD feeds, so clearing on it would clear the halt without proving the
    CURRENT required feeds are warm.

    Seeds state that would pass the probe (expected_manifest pinned in ARGV),
    then overwrites the manifest with a DIFFERENT value before invoking the Lua
    directly (simulating the race window)."""
    dep_id = str(uuid4())
    manifest_key = data_freshness_manifest_key(dep_id)
    verdict_key = data_freshness_key(dep_id, _DATASET, _BAR_TYPE) + VERDICT_KEY_SUFFIX
    rec_key = reconciled_key(dep_id)
    delete_keys = [_HALT, f"{_HALT}:set_by", halt_cause_key("fleet")]

    expected_manifest = json.dumps([{"dataset": _DATASET, "feed": _BAR_TYPE, "symbol": "AAPL"}])
    # The monitor restarted the node with a DIFFERENT (larger) feed universe.
    changed_manifest = json.dumps(
        [
            {"dataset": _DATASET, "feed": _BAR_TYPE, "symbol": "AAPL"},
            {"dataset": _DATASET, "feed": "MSFT.XNAS-1-MINUTE-LAST-EXTERNAL", "symbol": "MSFT"},
        ]
    )
    await redis_text.set(manifest_key, changed_manifest)  # CURRENT value differs
    await redis_text.set(verdict_key, "warm")
    await redis_text.set(rec_key, "ts")
    for k in delete_keys:
        await redis_text.set(k, "x")

    lua_keys = [manifest_key, verdict_key, rec_key, *delete_keys]
    result = await redis_text.eval(
        RESUME_CLEAR_LUA, len(lua_keys), *lua_keys, "1", "1", "1", expected_manifest
    )
    result_str = result.decode() if isinstance(result, bytes) else str(result)
    assert result_str.startswith("MANIFEST_CHANGED:")
    assert manifest_key in result_str
    # NOTHING deleted — the latch survives the aborted clear.
    assert await redis_text.exists(_HALT) == 1
    assert await redis_text.exists(halt_cause_key("fleet")) == 1


# ---------------------------------------------------------------------------
# Endpoint-level changed-universe TOCTOU (manifest swapped between probe + clear)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_manifest_swapped_between_probe_and_clear_409(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-21 P1 — changed-universe TOCTOU at the ENDPOINT level: the
    monitor overwrites a deployment's manifest with a CHANGED feed universe in
    the window between the Python probe (``GET manifest``) and the atomic clear
    (``EVAL``). The route must refuse (409 ``RESUME_BLOCKED_DATA_STALE`` naming a
    'manifest changed during resume') and clear NOTHING — the latch survives.

    Injection point: wrap ``bus._redis`` so the SUCCESS-path ``eval`` is
    preceded by an in-band manifest overwrite, reproducing the race precisely
    where the route hands the pre-derived (now-stale) verdict-key list to Lua.
    """
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_halt_latch(redis_text)
    await _seed_manifest(redis_text, dep_id)  # one AAPL feed
    await _seed_feed_verdict(redis_text, dep_id, verdict="warm")
    await _seed_reconciled(redis_text, dep_id)

    manifest_key = data_freshness_manifest_key(dep_id)
    changed_manifest = json.dumps(
        [
            {"dataset": _DATASET, "feed": _BAR_TYPE, "symbol": "AAPL"},
            {"dataset": _DATASET, "feed": "MSFT.XNAS-1-MINUTE-LAST-EXTERNAL", "symbol": "MSFT"},
        ]
    )

    real_redis = bus._redis  # noqa: SLF001

    class _RaceRedis:
        """Delegates everything to the real client, but overwrites the manifest
        in the instant BEFORE the resume ``eval`` fires — the race window."""

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(real_redis, name)

        async def eval(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await real_redis.set(manifest_key, changed_manifest, ex=120)
            return await real_redis.eval(*args, **kwargs)

    bus._redis = _RaceRedis()  # type: ignore[assignment]  # noqa: SLF001
    try:
        resp = await client.post("/api/v1/live/resume")
    finally:
        bus._redis = real_redis  # noqa: SLF001

    assert resp.status_code == 409, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert "manifest changed during resume" in err["message"]
    # Latch UNTOUCHED — nothing cleared.
    assert await redis_text.exists(_HALT) == 1
    assert await redis_text.exists(halt_cause_key("fleet")) == 1


# ---------------------------------------------------------------------------
# /kill-all atop a data-stale halt preserves the original cause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_all_preserves_existing_data_stale_cause_and_appends_history(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
) -> None:
    """If an operator hits /kill-all while a data-stale auto-halt is ALREADY
    latched, the ORIGINAL data_stale cause must be PRESERVED (HALT_WRITE_LUA's
    SET-NX) and the manual fleet_emergency cause appended to the capped
    ``:history`` list — not silently erased by a blind cause SET."""
    # Pre-existing data-stale auto-halt cause.
    data_stale_cause = {
        "reason": HaltCause.DATA_STALE.value,
        "dataset": _DATASET,
        "feed": _BAR_TYPE,
    }
    await redis_text.set(halt_cause_key("fleet"), json.dumps(data_stale_cause), ex=86400)
    await redis_text.rpush(f"{halt_cause_key('fleet')}:history", json.dumps(data_stale_cause))

    resp = await client.post("/api/v1/live/kill-all")
    assert resp.status_code in (200, 207), resp.text

    # The cause key STILL reads the original data_stale attribution (preserved).
    cause_now = json.loads(await redis_text.get(halt_cause_key("fleet")))
    assert cause_now["reason"] == HaltCause.DATA_STALE.value

    # The manual fleet_emergency cause was appended to history.
    history = await redis_text.lrange(f"{halt_cause_key('fleet')}:history", 0, -1)
    reasons = [json.loads(h)["reason"] for h in history]
    assert HaltCause.DATA_STALE.value in reasons
    assert HaltCause.FLEET_EMERGENCY.value in reasons
