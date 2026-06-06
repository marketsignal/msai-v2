"""Helper-level tests for _poll_for_terminal attempt-scoping (fix/start-portfolio-503-but-spawned).

The 2026-06-05 LVP drill showed POST /live/start-portfolio returning a false,
cacheable 503 "unknown failure" 3/3 times while the spawn succeeded: the poll's
first read returned the PREVIOUS run's terminal row. These tests pin the hybrid
scoping contract (approach A', contrarian-validated):

  - terminal rows count ONLY when their id is NOT in exclude_terminal_row_ids
    (the pre-publish snapshot of the deployment's existing rows)
  - ready/running rows count UNCONDITIONALLY (an active row is by definition
    the current node — partial unique index)
  - exclude_terminal_row_ids=None (or empty) preserves the legacy unscoped
    behavior (the /stop and /drain call sites).

SAFETY: dedicated PostgresContainer per module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api.live import _poll_for_terminal
from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User

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


async def _seed_deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> LiveDeployment:
    """Minimal deployment row the FK requires. Reuses the shared factory."""
    from tests.integration._deployment_factory import make_live_deployment

    async with session_factory() as session, session.begin():
        user = User(
            id=uuid4(),
            entra_id=f"sub-{uuid4()}",
            email=f"{uuid4()}@test.com",
            role="operator",
        )
        session.add(user)
        strategy = Strategy(
            id=uuid4(),
            name=f"smoke-{uuid4().hex[:8]}",
            file_path="/dev/null",
            strategy_class="SmokeStrategy",
            default_config={},
            created_by=user.id,
        )
        session.add(strategy)
        dep = await make_live_deployment(
            session,
            user=user,
            strategy=strategy,
            status="starting",
            strategy_class="SmokeStrategy",
        )
    return dep


def _node_row(
    deployment_id: UUID,
    *,
    status: str,
    failure_kind: str | None = None,
    started_at: datetime | None = None,
) -> LiveNodeProcess:
    """``started_at`` is explicit where ordering matters — the helper's query
    orders by ``started_at DESC``, so tests must not rely on microsecond
    adjacency of back-to-back ``datetime.now()`` calls (plan-review iter-2 P3)."""
    ts = started_at or datetime.now(UTC)
    return LiveNodeProcess(
        id=uuid4(),
        deployment_id=deployment_id,
        gateway_session_key="msai-paper-primary:localhost:4002",
        pid=None,
        host="test-host",
        started_at=ts,
        last_heartbeat_at=ts,
        status=status,
        failure_kind=failure_kind,
    )


READY = frozenset({"ready", "running"})
TERMINAL = frozenset({"failed", "stopped"})


@pytest.mark.asyncio
async def test_snapshot_terminal_row_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE BUG: a terminal row in the pre-publish snapshot must NOT be returned
    as this attempt's outcome — the poll keeps waiting and returns None on
    timeout."""
    dep = await _seed_deployment(session_factory)
    stale = _node_row(dep.id, status="stopped")
    async with session_factory() as session, session.begin():
        session.add(stale)

    async with session_factory() as db:
        row = await _poll_for_terminal(
            db,
            dep.id,
            ready_statuses=READY,
            terminal_statuses=TERMINAL,
            timeout_s=0.3,
            interval_s=0.05,
            exclude_terminal_row_ids=frozenset({stale.id}),
        )
    assert row is None  # pre-fix the helper returned the stale stopped row


@pytest.mark.asyncio
async def test_new_terminal_row_outside_snapshot_is_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Case 5: this attempt's failure row (id not in the snapshot) must still
    surface so HALT/SPAWN_FAILED/registry classification keeps working."""
    dep = await _seed_deployment(session_factory)
    # Explicit started_at ordering: fresh must be the latest row by
    # started_at DESC, not by microsecond luck (plan-review iter-2 P3).
    base = datetime.now(UTC)
    stale = _node_row(dep.id, status="stopped", started_at=base - timedelta(hours=1))
    fresh = _node_row(dep.id, status="failed", failure_kind="halt_active", started_at=base)
    async with session_factory() as session, session.begin():
        session.add(stale)
        session.add(fresh)

    async with session_factory() as db:
        row = await _poll_for_terminal(
            db,
            dep.id,
            ready_statuses=READY,
            terminal_statuses=TERMINAL,
            timeout_s=2.0,
            interval_s=0.05,
            exclude_terminal_row_ids=frozenset({stale.id}),
        )
    assert row is not None
    assert row.id == fresh.id
    assert row.status == "failed"
    assert row.failure_kind == "halt_active"


@pytest.mark.asyncio
async def test_ready_row_in_snapshot_is_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Contrarian iter-1 case: an active/ready row is the current node even if
    it is in the snapshot (concurrent START won the spawn race)."""
    dep = await _seed_deployment(session_factory)
    active = _node_row(dep.id, status="running")
    async with session_factory() as session, session.begin():
        session.add(active)

    async with session_factory() as db:
        row = await _poll_for_terminal(
            db,
            dep.id,
            ready_statuses=READY,
            terminal_statuses=TERMINAL,
            timeout_s=2.0,
            interval_s=0.05,
            exclude_terminal_row_ids=frozenset({active.id}),
        )
    assert row is not None
    assert row.id == active.id
    assert row.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("exclude_terminal_row_ids", [None, frozenset()])
async def test_no_snapshot_preserves_legacy_unscoped_behavior(
    session_factory: async_sessionmaker[AsyncSession],
    exclude_terminal_row_ids: frozenset[UUID] | None,
) -> None:
    """Both ``None`` AND an empty ``frozenset`` are pinned as the legacy unscoped
    behavior. The /stop and /drain call sites pass ``None``; the endpoint's
    first-deploy path passes ``prior_node_row_ids = frozenset()`` (no prior rows
    snapshotted). In both cases nothing is excluded, so the latest terminal row
    must be returned exactly as before."""
    dep = await _seed_deployment(session_factory)
    stale = _node_row(dep.id, status="stopped")
    async with session_factory() as session, session.begin():
        session.add(stale)

    async with session_factory() as db:
        row = await _poll_for_terminal(
            db,
            dep.id,
            ready_statuses=frozenset(),
            terminal_statuses=TERMINAL,
            timeout_s=2.0,
            interval_s=0.05,
            exclude_terminal_row_ids=exclude_terminal_row_ids,
        )
    assert row is not None
    assert row.status == "stopped"
