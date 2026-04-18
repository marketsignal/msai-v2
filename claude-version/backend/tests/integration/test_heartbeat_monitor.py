"""Integration tests for ``HeartbeatMonitor`` (Phase 1 task 1.7).

Verifies the ownership split from plan v7 (Codex v6 P0):

- Post-startup rows (``ready``/``running``/``stopping``) with stale
  heartbeats are flipped to ``failed`` with
  :attr:`FailureKind.HEARTBEAT_TIMEOUT`, and the parent
  ``live_deployments.status`` is synced to ``failed``.
- Startup rows (``starting``/``building``) are NEVER touched — the
  watchdog owns them.
- Fresh heartbeats in any status are left alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.failure_kind import FailureKind
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


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
async def deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> LiveDeployment:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"hm-{uuid4().hex}",
            email=f"hm-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name="hm-test",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        dep = await make_live_deployment(session, user=user, strategy=strategy)
        await session.commit()
        return dep


async def _seed_row(
    session: AsyncSession,
    *,
    deployment_id,
    status: str,
    heartbeat_age_seconds: int,
) -> LiveNodeProcess:
    """Helper to insert a LiveNodeProcess row with a controllable
    heartbeat age."""
    now = datetime.now(UTC)
    row = LiveNodeProcess(
        id=uuid4(),
        deployment_id=deployment_id,
        pid=12345,
        host="test",
        started_at=now - timedelta(seconds=600),
        last_heartbeat_at=now - timedelta(seconds=heartbeat_age_seconds),
        status=status,
        gateway_session_key="msai-paper-primary:localhost:4002",
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Post-startup statuses: marked stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ready", "running", "stopping"])
async def test_stale_post_startup_row_marked_failed(
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
    status: str,
) -> None:
    """All three post-startup statuses with a stale heartbeat must be
    flipped to ``failed`` with ``FailureKind.HEARTBEAT_TIMEOUT``, and
    the parent ``LiveDeployment`` row must have its ``status`` synced
    to ``failed`` so the HTTP layer and UI observe the terminal state."""
    async with session_factory() as session:
        row = await _seed_row(
            session,
            deployment_id=deployment.id,
            status=status,
            heartbeat_age_seconds=120,
        )
        await session.commit()

    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30)
    flipped = await monitor._mark_stale_as_failed()

    assert str(deployment.id) in flipped

    async with session_factory() as session:
        fresh = await session.get(LiveNodeProcess, row.id)
        assert fresh is not None
        assert fresh.status == "failed"
        assert fresh.failure_kind == FailureKind.HEARTBEAT_TIMEOUT.value
        assert fresh.error_message == "heartbeat timeout"

        fresh_dep = await session.get(LiveDeployment, deployment.id)
        assert fresh_dep is not None
        assert fresh_dep.status == "failed"


# ---------------------------------------------------------------------------
# Startup statuses: NEVER touched (watchdog owns them)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["starting", "building"])
async def test_stale_startup_row_is_not_touched(
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
    status: str,
) -> None:
    """Codex v6 P0 / plan v7 regression guard: the HeartbeatMonitor
    MUST NOT touch ``starting`` or ``building`` rows even when their
    heartbeat is stale — the watchdog is the sole liveness authority
    for those statuses. v6 included them and raced the watchdog."""
    async with session_factory() as session:
        row = await _seed_row(
            session,
            deployment_id=deployment.id,
            status=status,
            heartbeat_age_seconds=120,
        )
        await session.commit()

    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30)
    flipped = await monitor._mark_stale_as_failed()

    assert str(deployment.id) not in flipped

    async with session_factory() as session:
        fresh = await session.get(LiveNodeProcess, row.id)
        assert fresh is not None
        assert fresh.status == status
        assert fresh.failure_kind is None
        assert fresh.error_message is None


# ---------------------------------------------------------------------------
# Fresh heartbeats: untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_running_row_is_not_touched(
    session_factory: async_sessionmaker[AsyncSession],
    deployment: LiveDeployment,
) -> None:
    async with session_factory() as session:
        row = await _seed_row(
            session,
            deployment_id=deployment.id,
            status="running",
            heartbeat_age_seconds=5,  # fresh
        )
        await session.commit()

    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30)
    flipped = await monitor._mark_stale_as_failed()

    assert flipped == []

    async with session_factory() as session:
        fresh = await session.get(LiveNodeProcess, row.id)
        assert fresh is not None
        assert fresh.status == "running"


@pytest.mark.asyncio
async def test_empty_db_sweep_returns_empty_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sanity check: a sweep against an empty table returns cleanly."""
    monitor = HeartbeatMonitor(db=session_factory, stale_seconds=30)
    flipped = await monitor._mark_stale_as_failed()
    assert flipped == []
