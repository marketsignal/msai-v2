"""Integration tests for the subprocess heartbeat thread (Phase 1 task 1.9).

Verifies the ordering rule from decision #17 / task 1.8 (heartbeat
starts BEFORE ``node.build()``) and that the thread actually writes
to ``live_node_processes.last_heartbeat_at`` against a real Postgres
container.

The heartbeat thread is intentionally **sync** SQLAlchemy — it runs
in a daemon thread, not an asyncio task, so tests can observe its
DB writes with a plain sync select.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, LiveNodeProcess, Strategy, User
from msai.services.nautilus.trading_node_subprocess import (
    TradingNodePayload,
    _HeartbeatThread,
    run_subprocess_async,
)
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
async def seeded_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> LiveNodeProcess:
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"hb-{uuid4().hex}",
            email=f"hb-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name="hb-test",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        dep = await make_live_deployment(session, user=user, strategy=strategy, status="starting")

        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=dep.id,
            pid=None,
            host="test-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="building",
            gateway_session_key="msai-paper-primary:localhost:4002",
        )
        session.add(row)
        await session.commit()
        return row


# ---------------------------------------------------------------------------
# Heartbeat thread writes to the row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_thread_advances_last_heartbeat_at(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    isolated_postgres_url: str,
) -> None:
    """Start the real heartbeat thread and verify it bumps
    ``last_heartbeat_at`` at least three times over a 1.5s window
    (with a 0.3s interval)."""
    # Snapshot the initial heartbeat timestamp
    async with session_factory() as session:
        before = await session.get(LiveNodeProcess, seeded_row.id)
        assert before is not None
        initial_heartbeat = before.last_heartbeat_at

    thread = _HeartbeatThread(
        async_database_url=isolated_postgres_url,
        row_id=seeded_row.id,
        interval_s=0.3,
    )
    thread.start()
    try:
        # Let the thread tick at least a few times.
        time.sleep(1.5)
    finally:
        thread.stop()
        thread.join(timeout=5.0)

    assert thread.last_error is None, f"heartbeat thread errored: {thread.last_error}"
    assert thread.ticks >= 3, (
        f"expected at least 3 heartbeat ticks in 1.5s with 0.3s interval; got {thread.ticks}"
    )

    async with session_factory() as session:
        after = await session.get(LiveNodeProcess, seeded_row.id)
        assert after is not None
        assert after.last_heartbeat_at > initial_heartbeat


# ---------------------------------------------------------------------------
# Heartbeat ordering inside the subprocess lifecycle
# ---------------------------------------------------------------------------


class _StubHeartbeat:
    """Minimal stub that records start/stop ordering so tests can
    assert heartbeat.start() runs BEFORE node.build() and
    heartbeat.stop() runs in the finally block."""

    def __init__(self) -> None:
        self.start_ts: float | None = None
        self.stop_ts: float | None = None

    def start(self) -> None:
        self.start_ts = time.monotonic()

    def stop(self) -> None:
        self.stop_ts = time.monotonic()

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
        pass


class _TimestampedFakeNode:
    """Fake TradingNode that records call timestamps for heartbeat
    ordering assertions.

    Codex batch 3 iter10 P0: matches the real Nautilus 1.223.0
    ``TradingNode`` API — ``run_async`` is the single async entry
    point, no separate ``start_async`` / ``run``."""

    def __init__(self) -> None:
        import asyncio

        from tests.integration.test_trading_node_subprocess import _FakeKernel, _FakeTrader

        self.kernel = _FakeKernel(_FakeTrader(flip_on_poll=1))
        self.build_ts: float | None = None
        self.dispose_ts: float | None = None
        self.stop_async_ts: float | None = None
        self._stop_event = asyncio.Event()

    def build(self) -> None:
        self.build_ts = time.monotonic()

    async def run_async(self) -> None:
        """Real Nautilus ``run_async`` flips the trader state
        (kernel.start_async) then blocks. Default fake behavior is
        to yield once and return so happy-path tests don't hang
        waiting for stop_async."""
        import asyncio

        await asyncio.sleep(0)

    async def stop_async(self) -> None:
        self.stop_async_ts = time.monotonic()
        self._stop_event.set()

    def dispose(self) -> None:
        self.dispose_ts = time.monotonic()


@pytest.mark.asyncio
async def test_heartbeat_starts_before_build_and_stops_after_dispose(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Heartbeat ordering invariants:

    - Decision #17: the heartbeat thread MUST start BEFORE
      ``node.build()`` so a hung build doesn't stall stale detection.
    - Codex batch 3 iter4 P1: the heartbeat thread MUST stop AFTER
      ``node.stop_async()`` + ``dispose()`` complete so a slow IB
      teardown can't exceed the HeartbeatMonitor stale threshold and
      let the monitor flip the still-live row to ``failed`` before
      the terminal write runs (which would open the
      duplicate-spawn restart-race window).
    """
    stub = _StubHeartbeat()
    node = _TimestampedFakeNode()

    payload = TradingNodePayload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
        strategy_path="x:Y",
        strategy_config_path="x:YConfig",
        strategy_config={},
        paper_symbols=["AAPL"],
        startup_health_timeout_s=1.0,
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        heartbeat_factory=lambda _p: stub,
    )

    assert exit_code == 0
    assert stub.start_ts is not None, "heartbeat never started"
    assert node.build_ts is not None, "build never ran"
    assert stub.stop_ts is not None, "heartbeat never stopped"
    assert node.dispose_ts is not None, "dispose never ran"

    # Ordering assertions
    assert stub.start_ts < node.build_ts, (
        f"heartbeat must start before build; got start={stub.start_ts}, build={node.build_ts}"
    )
    # Iter4 P1 fix: heartbeat stops AFTER dispose, not before. Keeping
    # the heartbeat alive through stop_async + dispose ensures a slow
    # IB teardown can't exceed the stale threshold and let
    # HeartbeatMonitor mark the still-live row failed before the
    # terminal write runs.
    assert stub.stop_ts > node.dispose_ts, (
        f"heartbeat must stop AFTER dispose; got stop={stub.stop_ts}, dispose={node.dispose_ts}"
    )


@pytest.mark.asyncio
async def test_heartbeat_stopped_on_failure_path_too(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """If the build raises, the finally block must still stop the
    heartbeat — otherwise a failed spawn leaves a daemon thread
    writing to a terminal row forever."""
    stub = _StubHeartbeat()

    class _BoomNode(_TimestampedFakeNode):
        def build(self) -> None:
            super().build()
            raise RuntimeError("build went boom")

    node = _BoomNode()
    payload = TradingNodePayload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
        strategy_path="x:Y",
        strategy_config_path="x:YConfig",
        strategy_config={},
        paper_symbols=["AAPL"],
        startup_health_timeout_s=1.0,
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        heartbeat_factory=lambda _p: stub,
    )
    assert exit_code == 1
    assert stub.stop_ts is not None, "heartbeat must stop even on build failure"
