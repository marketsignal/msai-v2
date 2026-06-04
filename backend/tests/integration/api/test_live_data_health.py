"""Integration: operator data-feed health surface (PR 1b T7).

``GET /api/v1/live/data-health`` is the operator's read-only window onto the
in-node data-stale monitor: per active deployment it lists every required
Databento feed (built MANIFEST-FIRST so a feed the monitor declared but never
published a freshness row for shows up as a derived ``missing`` verdict — never
silently absent), plus the fleet halt latch + parsed halt-cause for context.

The same manifest-first reader hydrates the Prometheus metrics
(``msai_data_feed_age_seconds`` / ``msai_data_feed_stale`` /
``msai_databento_dataset_alive`` / ``msai_ib_exec_pacing_errors``) on BOTH the
data-health route and the ``/metrics`` scrape path, so a feed dropped from the
manifest disappears from the next scrape (children are REPLACED each hydrate).

ARRANGE mirrors ``test_live_resume_fail_closed.py``: dedicated
PostgresContainer + RedisContainer per module, deployments seeded via the
model, freshness state seeded on the TEXT Redis client (the monitor's wire
format). SAFETY: paper accounts only (``DU...``); no real node spawns.
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
    HaltCause,
    data_freshness_key,
    data_freshness_manifest_key,
    fleet_halt_key,
    halt_cause_key,
    ib_exec_pacing_key,
)
from msai.main import app
from msai.models import Base
from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio import LivePortfolio
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.strategy import Strategy
from msai.models.user import User
from msai.services.live_command_bus import LiveCommandBus
from msai.services.observability import get_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


_HALT = fleet_halt_key()
_DATASET = "EQUS.MINI"
_BAR_TYPE = "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"
_NODE_ID = "node-dh-1"
_ACCOUNT = "DUP-DH-1"


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


@pytest.fixture(autouse=True)
def _clear_data_health_children() -> Iterator[None]:
    """Clear ONLY the data-health gauge children before/after each test so a
    prior test's hydrated children don't leak into the metrics-shape / prune
    assertions. We do NOT reset() the whole registry — that would drop the
    module-level gauges registered at import time, leaving render() empty."""
    from msai.services.observability.trading_metrics import (
        DATA_FEED_AGE_SECONDS,
        DATA_FEED_STALE,
        DATA_MONITOR_MISSING,
        DATA_STALE_HALTS,
        DATABENTO_DATASET_ALIVE,
        IB_EXEC_PACING_ERRORS,
    )

    gauges = (
        DATA_FEED_AGE_SECONDS,
        DATA_FEED_STALE,
        DATABENTO_DATASET_ALIVE,
        IB_EXEC_PACING_ERRORS,
        DATA_STALE_HALTS,
        DATA_MONITOR_MISSING,
    )
    for g in gauges:
        g.replace_children([])
    yield
    for g in gauges:
        g.replace_children([])


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
    account_id: str = _ACCOUNT,
    status: str = "running",
) -> LiveDeployment:
    user = User(
        id=uuid4(),
        entra_id=f"dh-{uuid4().hex[:12]}",
        email=f"dh-{uuid4().hex[:8]}@example.com",
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
        description="data-health test",
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


async def _seed_manifest(
    redis: AsyncRedis,
    deployment_id: str,
    *,
    feeds: list[dict] | None = None,
) -> None:
    if feeds is None:
        feeds = [{"dataset": _DATASET, "feed": _BAR_TYPE, "symbol": "AAPL"}]
    await redis.set(data_freshness_manifest_key(deployment_id), json.dumps(feeds), ex=120)


async def _seed_feed_row(
    redis: AsyncRedis,
    deployment_id: str,
    *,
    account_id: str = _ACCOUNT,
    dataset: str = _DATASET,
    bar_type: str = _BAR_TYPE,
    symbol: str = "AAPL",
    verdict: str = "warm",
    last_event_ts: int | None = 1_700_000_000_000_000_000,
) -> None:
    """Publish the monitor's per-feed JSON + plain verdict companion."""
    payload = {
        "last_event_ts": last_event_ts,
        "last_arrival_ts": last_event_ts,
        "verdict": verdict,
        "phase": "regular",
        "grace_s": 90,
        "account_id": account_id,
        "node_id": _NODE_ID,
        "symbol": symbol,
        "published_at": "2026-06-03T00:00:00+00:00",
    }
    key = data_freshness_key(deployment_id, dataset, bar_type)
    await redis.set(key, json.dumps(payload), ex=120)
    await redis.set(key + ":verdict", verdict, ex=120)


async def _seed_halt(redis: AsyncRedis, *, cause: dict | None = None) -> None:
    await redis.set(_HALT, "true", ex=86400)
    cause = cause or {
        "reason": HaltCause.DATA_STALE.value,
        "dataset": _DATASET,
        "feed": _BAR_TYPE,
        "symbol": "AAPL",
    }
    await redis.set(halt_cause_key("fleet"), json.dumps(cause), ex=86400)


# ---------------------------------------------------------------------------
# Route — empty fleet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_empty_fleet_returns_empty_feeds(
    client: httpx.AsyncClient,
) -> None:
    """No active deployments → 200 with an empty feeds list and an un-halted
    fleet. An operator polling a quiet fleet gets a clean answer, not a 404."""
    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feeds"] == []
    assert body["fleet_halted"] is False
    assert body["halt_cause"] is None


# ---------------------------------------------------------------------------
# Route — warm rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_warm_rows_carry_all_fields(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An operator checking a healthy live deployment sees one feed row carrying
    the full freshness shape — account/node/deployment/dataset/feed/symbol plus
    last_event_ts, phase, grace_s, verdict, published_at — and can re-poll to
    confirm the row persists."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="warm")

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fleet_halted"] is False
    assert len(body["feeds"]) == 1
    row = body["feeds"][0]
    assert row["account_id"] == _ACCOUNT
    assert row["node_id"] == _NODE_ID
    assert row["deployment_id"] == dep_id
    assert row["dataset"] == _DATASET
    assert row["feed"] == _BAR_TYPE
    assert row["symbol"] == "AAPL"
    assert row["verdict"] == "warm"
    assert row["phase"] == "regular"
    assert row["grace_s"] == 90
    assert row["last_event_ts"] == 1_700_000_000_000_000_000
    assert row["published_at"] == "2026-06-03T00:00:00+00:00"

    # Persistence: re-poll → the same row is still present (TTL-alive).
    resp2 = await client.get("/api/v1/live/data-health")
    assert resp2.status_code == 200
    assert resp2.json()["feeds"][0]["feed"] == _BAR_TYPE


@pytest.mark.asyncio
async def test_data_health_pending_row_shown_verbatim(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-2 P1 — a feed the monitor published with verdict ``pending``
    (monitor alive, no data observed yet within startup grace) surfaces VERBATIM
    in data-health, distinct from API-derived ``missing`` (no row at all). The
    operator can re-poll and still see the pending row."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="pending")

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    feeds = resp.json()["feeds"]
    assert len(feeds) == 1
    row = feeds[0]
    assert row["dataset"] == _DATASET
    assert row["feed"] == _BAR_TYPE
    # 'pending' (monitor-published, row present) is distinct from 'missing'.
    assert row["verdict"] == "pending"

    # Persistence: re-poll → the pending row is still present (TTL-alive).
    resp2 = await client.get("/api/v1/live/data-health")
    assert resp2.status_code == 200
    assert resp2.json()["feeds"][0]["verdict"] == "pending"


# ---------------------------------------------------------------------------
# Route — manifest feed with NO row → derived 'missing'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_manifest_feed_without_row_is_missing(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A feed declared in the manifest but with no published freshness row must
    appear as a derived ``missing`` verdict — never silently absent — so an
    operator can see the feed the node expected but never delivered."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    # NO per-feed row seeded → API derives 'missing'.

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    feeds = resp.json()["feeds"]
    assert len(feeds) == 1
    row = feeds[0]
    assert row["dataset"] == _DATASET
    assert row["feed"] == _BAR_TYPE
    assert row["symbol"] == "AAPL"
    assert row["verdict"] == "missing"
    assert row["last_event_ts"] is None


@pytest.mark.asyncio
async def test_data_health_malformed_manifest_does_not_crash(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deployment whose manifest is malformed (non-list JSON, or list entries
    missing dataset/feed) must NOT crash the route → 200, and that deployment
    simply contributes no feed rows (FIX 4 — defensive shape handling)."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    # Non-list manifest (a JSON object) + a list with a non-dict and a
    # dict-missing-feed entry are all malformed shapes the reader must tolerate.
    await redis_text.set(
        data_freshness_manifest_key(dep_id),
        json.dumps({"dataset": _DATASET, "feed": _BAR_TYPE}),
        ex=120,
    )

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Malformed manifest → no rows for that deployment, no 500.
    assert body["feeds"] == []
    assert body["fleet_halted"] is False


# ---------------------------------------------------------------------------
# Route — active deployment with NO manifest → monitor_missing (FIX 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_active_deployment_without_manifest_is_monitor_missing(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 4 — an ACTIVE deployment whose freshness manifest is ABSENT (monitor
    never started, or its node died and the manifest TTL lapsed) must be
    surfaced explicitly under ``monitor_missing`` — distinct from an empty fleet
    — so an operator can tell a dead monitor apart from a quiet one."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    # NO manifest seeded → monitor is missing/dead for this active deployment.

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No feed rows (no manifest), but the deployment is NOT silently dropped.
    assert body["feeds"] == []
    assert len(body["monitor_missing"]) == 1
    entry = body["monitor_missing"][0]
    assert entry["deployment_id"] == dep_id
    assert entry["account_id"] == _ACCOUNT
    assert entry["reason"] == "manifest absent"

    # Persistence: re-poll → the deployment is still flagged monitor_missing.
    resp2 = await client.get("/api/v1/live/data-health")
    assert resp2.status_code == 200
    assert any(e["deployment_id"] == dep_id for e in resp2.json()["monitor_missing"])


@pytest.mark.asyncio
async def test_data_health_monitor_missing_hydrates_gauge(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 4 — a monitor-missing deployment hydrates the labeled
    ``msai_data_monitor_missing`` gauge so Prometheus can alert on a dead
    in-node monitor."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    # NO manifest seeded.
    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text

    rendered = get_registry().render()
    assert "msai_data_monitor_missing{" in rendered
    missing_line = next(
        ln for ln in rendered.splitlines() if ln.startswith("msai_data_monitor_missing{")
    )
    assert f'deployment="{dep_id}"' in missing_line
    assert missing_line.endswith(" 1.0") or missing_line.endswith(" 1")


@pytest.mark.asyncio
async def test_data_health_malformed_manifest_is_monitor_missing(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 4 — a MALFORMED manifest (non-list JSON) is also surfaced under
    monitor_missing with reason 'manifest malformed', distinct from 'absent'."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await redis_text.set(
        data_freshness_manifest_key(dep_id),
        json.dumps({"dataset": _DATASET, "feed": _BAR_TYPE}),  # object, not list
        ex=120,
    )

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feeds"] == []
    entry = next(e for e in body["monitor_missing"] if e["deployment_id"] == dep_id)
    assert entry["reason"] == "manifest malformed"


@pytest.mark.asyncio
async def test_data_health_malformed_list_entry_consistent_with_resume(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-12 P2 — data-health and /resume must agree on a manifest that
    is a LIST but whose ENTRIES are malformed (here ``[{}]`` — an entry that is
    a dict but lacks 'dataset'/'feed'). Previously data-health silently SKIPPED
    such entries → a ``running`` deployment rendered ``feeds:[]`` with NO
    monitor_missing and no gauge, while /resume 409'd the SAME manifest as
    malformed — contradictory operator surfaces.

    After the fix BOTH surfaces treat any malformed ENTRY as a malformed
    deployment-level manifest: data-health lists it under ``monitor_missing``
    with reason ``'manifest malformed'`` + hydrates the gauge, AND /resume
    fails closed (409 ``RESUME_BLOCKED_DATA_STALE``)."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    # A LIST manifest whose single entry is malformed (no dataset/feed).
    await redis_text.set(
        data_freshness_manifest_key(dep_id),
        json.dumps([{}]),
        ex=120,
    )

    # (a) data-health classifies the deployment monitor_missing 'manifest
    # malformed' (NOT silently skipped) and exposes the gauge.
    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feeds"] == []
    entry = next(e for e in body["monitor_missing"] if e["deployment_id"] == dep_id)
    assert entry["reason"] == "manifest malformed"

    rendered = get_registry().render()
    missing_line = next(
        ln for ln in rendered.splitlines() if ln.startswith("msai_data_monitor_missing{")
    )
    assert f'deployment="{dep_id}"' in missing_line

    # (b) /resume fail-closes (409) on the SAME manifest → the two surfaces agree.
    await _seed_halt(redis_text)
    resp_resume = await client.post("/api/v1/live/resume")
    assert resp_resume.status_code == 409, resp_resume.text
    assert resp_resume.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    # Latch retained (resume refused).
    assert await redis_text.exists(fleet_halt_key()) == 1


@pytest.mark.asyncio
async def test_data_health_null_field_entry_consistent_with_resume(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-13 P2 — a manifest entry carrying ``null`` for 'dataset' (or
    'feed') must be malformed on BOTH surfaces. Previously data-health coerced
    via ``str(None)`` → the truthy string "None" → the entry passed as a
    'valid' feed rendered ``missing``, with NO monitor_missing/gauge — while
    /resume rejected the same manifest as malformed. Raw values are now
    validated as non-empty STRINGS before coercion on both surfaces."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await redis_text.set(
        data_freshness_manifest_key(dep_id),
        json.dumps([{"dataset": None, "feed": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"}]),
        ex=120,
    )

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["feeds"] == []  # NOT rendered as a bogus 'None' feed
    entry = next(e for e in body["monitor_missing"] if e["deployment_id"] == dep_id)
    assert entry["reason"] == "manifest malformed"

    await _seed_halt(redis_text)
    resp_resume = await client.post("/api/v1/live/resume")
    assert resp_resume.status_code == 409, resp_resume.text
    assert resp_resume.json()["detail"]["error"]["code"] == "RESUME_BLOCKED_DATA_STALE"
    assert await redis_text.exists(fleet_halt_key()) == 1


# ---------------------------------------------------------------------------
# Route — Redis manifest READ-FAILURE must NOT page as monitor_missing (FIX 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_manifest_read_failure_is_not_monitor_missing(
    session_factory: async_sessionmaker[AsyncSession],
    redis_text: AsyncRedis,
    bus: LiveCommandBus,
) -> None:
    """FIX 1 — when the manifest GET itself FAILS (transient Redis error, not a
    genuine absence), the deployment must be SKIPPED: no ``monitor_missing``
    entry and no gauge=1. A Redis blip must never make every active monitor look
    dead and page on-call. We wrap the real Redis so ONLY manifest GETs raise;
    every other call (halt exists, counter scans) still works."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    class _ManifestGetFailsRedis:
        """Delegates everything to the real client EXCEPT a GET of the
        deployment's manifest key, which raises (a transient read failure)."""

        def __init__(self, inner: AsyncRedis, manifest_key: str) -> None:
            self._inner = inner
            self._manifest_key = manifest_key

        async def get(self, key: str, *a: object, **k: object) -> object:
            if key == self._manifest_key:
                raise ConnectionError("transient redis read failure")
            return await self._inner.get(key, *a, **k)

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    bus._redis = _ManifestGetFailsRedis(  # type: ignore[assignment]  # noqa: SLF001
        redis_text, data_freshness_manifest_key(dep_id)
    )

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    async def _override_get_bus() -> LiveCommandBus:
        return bus

    async def _override_user() -> dict:
        return {"sub": "test-operator", "oid": str(uuid4())}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus
    app.dependency_overrides[get_current_user] = _override_user
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get("/api/v1/live/data-health")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_command_bus, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The deployment is SKIPPED on read-failure: no feed rows, and crucially NO
    # monitor_missing entry (read-failed != absent).
    assert body["feeds"] == []
    assert all(e["deployment_id"] != dep_id for e in body["monitor_missing"])

    # And the monitor-missing gauge must NOT carry a 1.0 for this deployment.
    rendered = get_registry().render()
    missing_lines = [
        ln for ln in rendered.splitlines() if ln.startswith("msai_data_monitor_missing{")
    ]
    assert all(f'deployment="{dep_id}"' not in ln for ln in missing_lines)


# ---------------------------------------------------------------------------
# Route — monitor_missing ALERT fires only for 'running' deployments (FIX 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_starting_deployment_without_manifest_is_not_monitor_missing(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 — a deployment still in a normal lifecycle status ('starting') with
    no manifest must NOT appear under ``monitor_missing``. The in-node monitor
    only publishes its manifest after the node marks itself running, so flagging
    building/starting/ready/stopping deployments as dead monitors would page
    on-call during every normal startup and shutdown."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session, status="starting")
        dep_id = str(dep.id)

    # NO manifest seeded — normal for a starting node whose monitor isn't up yet.
    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(e["deployment_id"] != dep_id for e in body["monitor_missing"])


@pytest.mark.asyncio
async def test_data_health_running_deployment_without_manifest_is_monitor_missing(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FIX 2 — a 'running' deployment whose manifest is absent IS monitor_missing
    (its monitor should have published by now). Pairs with the 'starting' case to
    pin the running-only alert restriction."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session, status="running")
        dep_id = str(dep.id)

    # NO manifest seeded.
    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entry = next(e for e in body["monitor_missing"] if e["deployment_id"] == dep_id)
    assert entry["reason"] == "manifest absent"


# ---------------------------------------------------------------------------
# Route — fleet halted with data_stale cause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_reports_fleet_halt_with_parsed_cause(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the in-node monitor has latched a data-stale halt, the operator sees
    fleet_halted true and the parsed cause JSON (reason=data_stale + the
    offending dataset/feed) so they can diagnose without grepping Redis."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="stale")
    await _seed_halt(redis_text)

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fleet_halted"] is True
    assert body["halt_cause"] is not None
    assert body["halt_cause"]["reason"] == HaltCause.DATA_STALE.value
    assert body["halt_cause"]["dataset"] == _DATASET
    # The stale feed row is still present in the feeds list.
    assert any(f["verdict"] == "stale" for f in body["feeds"])


# ---------------------------------------------------------------------------
# Metrics shape — labels carried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_health_call_hydrates_labeled_metric_series(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After a data-health call the Prometheus series carry the PRD-contract
    labels: data_feed_age_seconds{account,node,dataset,symbol,feed} +
    databento_dataset_alive{account,node,dataset}."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="warm")

    resp = await client.get("/api/v1/live/data-health")
    assert resp.status_code == 200, resp.text

    rendered = get_registry().render()
    assert "msai_data_feed_age_seconds{" in rendered
    # All four feed-label keys present on the age series.
    age_line = next(
        ln for ln in rendered.splitlines() if ln.startswith("msai_data_feed_age_seconds{")
    )
    for label in ("account=", "node=", "dataset=", "symbol=", "feed="):
        assert label in age_line, f"missing {label} in {age_line}"
    # symbol must be present (PRD contract).
    assert 'symbol="AAPL"' in age_line

    assert "msai_data_feed_stale{" in rendered
    alive_line = next(
        ln for ln in rendered.splitlines() if ln.startswith("msai_databento_dataset_alive{")
    )
    for label in ("account=", "node=", "dataset="):
        assert label in alive_line


# ---------------------------------------------------------------------------
# Bare /metrics scrape hydrates the series with no prior data-health call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_metrics_scrape_hydrates_data_health_series(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Prometheus scrape with NO prior data-health call still exposes the
    hydrated feed series — the /metrics render path drives the same
    manifest-first hydration before rendering."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="warm")

    # No data-health call first — scrape directly.
    resp = await client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "msai_data_feed_age_seconds{" in body
    assert f'feed="{_BAR_TYPE}"' in body


# ---------------------------------------------------------------------------
# Prune — feed removed from manifest disappears from the next hydrate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_feed_series_pruned_when_feed_leaves_manifest(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A feed that drops out of the manifest (deployment stopped / feed removed)
    must disappear from the NEXT scrape — hydration REPLACES the children, so
    stopped feeds don't linger forever in the metrics output."""
    async with session_factory() as session:
        dep = await _seed_active_deployment(session)
        dep_id = str(dep.id)

    await _seed_manifest(redis_text, dep_id)
    await _seed_feed_row(redis_text, dep_id, verdict="warm")

    resp1 = await client.get("/metrics")
    assert f'feed="{_BAR_TYPE}"' in resp1.text

    # Manifest now empties (feed removed) + the per-feed keys expire.
    await _seed_manifest(redis_text, dep_id, feeds=[])
    await redis_text.delete(data_freshness_key(dep_id, _DATASET, _BAR_TYPE))
    await redis_text.delete(data_freshness_key(dep_id, _DATASET, _BAR_TYPE) + ":verdict")

    resp2 = await client.get("/metrics")
    assert f'feed="{_BAR_TYPE}"' not in resp2.text


# ---------------------------------------------------------------------------
# Redis-down scrape does not 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_scrape_does_not_500_when_redis_down(
    client: httpx.AsyncClient,
    bus: LiveCommandBus,
) -> None:
    """If Redis is unreachable at scrape time, the /metrics endpoint must still
    render whatever is registered — never 500 the scrape (a metrics outage must
    not look like an app outage)."""

    class _BrokenRedis:
        async def get(self, *_a: object, **_k: object) -> object:
            raise ConnectionError("redis down")

        async def exists(self, *_a: object, **_k: object) -> object:
            raise ConnectionError("redis down")

        async def mget(self, *_a: object, **_k: object) -> object:
            raise ConnectionError("redis down")

    bus._redis = _BrokenRedis()  # type: ignore[assignment]  # noqa: SLF001

    resp = await client.get("/metrics")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# IB exec pacing counter hydrates into the gauge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ib_exec_pacing_counter_hydrates_into_gauge(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
) -> None:
    """A pacing-error counter the node INCRed for an account is surfaced on the
    data-health render path as the labeled IB_EXEC_PACING_ERRORS gauge."""
    await redis_text.set(ib_exec_pacing_key(_ACCOUNT), "3")

    resp = await client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "msai_ib_exec_pacing_errors{" in body
    pacing_line = next(
        ln for ln in body.splitlines() if ln.startswith("msai_ib_exec_pacing_errors{")
    )
    assert f'account="{_ACCOUNT}"' in pacing_line
    assert pacing_line.endswith(" 3.0") or pacing_line.endswith(" 3")


# ---------------------------------------------------------------------------
# Data-stale-halts counter hydrates into the metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_stale_halts_counter_hydrates_into_metric(
    client: httpx.AsyncClient,
    redis_text: AsyncRedis,
) -> None:
    """A data-stale-halts counter the in-node monitor INCRed for an account is
    surfaced on the data-health render path as the labeled DATA_STALE_HALTS
    series — the monitor's registry INC happens in the SUBPROCESS, invisible to
    the API /metrics registry, so Redis is the source of truth."""
    from msai.core.halt_keys import data_stale_halts_key

    await redis_text.set(data_stale_halts_key(_ACCOUNT), "2")

    resp = await client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "msai_data_stale_halts_total{" in body
    halt_line = next(
        ln for ln in body.splitlines() if ln.startswith("msai_data_stale_halts_total{")
    )
    assert f'account="{_ACCOUNT}"' in halt_line
    assert halt_line.endswith(" 2.0") or halt_line.endswith(" 2")
