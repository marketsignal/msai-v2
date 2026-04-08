"""Integration tests for LiveNodeProcess model (Phase 1 Task 1.1).

Verifies:
- Basic CRUD on the live_node_processes table
- pid is nullable (row is inserted before process.start() returns a pid)
- 'building' status is accepted (subprocess writes it during node.build())
- Partial unique index on deployment_id WHERE status IN active statuses:
    starting, building, ready, running, stopping
- failure_kind column accepts all FailureKind enum values AND NULL
- All required columns + timestamps

SAFETY (Codex review of Task 1.1, P1):
The ``session`` fixture in this module provisions its OWN dedicated
PostgreSQL testcontainer rather than reusing the session-scoped
``postgres_url`` fixture. Reusing that fixture is dangerous because
``tests/conftest.py`` prefers the ``DATABASE_URL`` env var over a
disposable testcontainer — and these tests run ``drop_all()`` on
whatever database they get. A reused dev database would be wiped.
The dedicated container here guarantees the tests can never touch a
configured DATABASE_URL regardless of env.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only.

    Intentionally does NOT read ``DATABASE_URL`` — we don't want these
    destructive tests (drop_all/create_all) to ever touch a configured
    dev database even if the env var is set. See the module docstring.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        yield url


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    """Provide a fresh database with all tables created.

    Uses the module-local ``isolated_postgres_url`` (NOT the session-scoped
    ``postgres_url`` from conftest) so ``drop_all()`` is guaranteed safe.
    """
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def deployment(session: AsyncSession) -> LiveDeployment:
    """Insert a minimal user + strategy + live_deployment to satisfy FKs.

    Populates every column required by the v9 ``live_deployments`` schema
    (Phase 1 task 1.1b) using the identity helper. Each test gets a fresh
    deployment with a unique slug + signature so the unique indexes don't
    collide across module-scoped tests.
    """
    from msai.services.live.deployment_identity import (
        derive_deployment_identity,
        derive_message_bus_stream,
        derive_strategy_id_full,
        derive_trader_id,
        generate_deployment_slug,
    )

    user = User(
        id=uuid4(),
        entra_id=f"test-{uuid4().hex}",
        email=f"test-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategy = Strategy(
        id=uuid4(),
        name="test-strategy",
        file_path="strategies/test.py",
        strategy_class="TestStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()

    config = {"x": 1}
    instruments = ["AAPL.NASDAQ"]
    account_id = "DU1234567"
    paper_trading = True
    strategy_code_hash = "deadbeef" * 8

    identity = derive_deployment_identity(
        user_id=user.id,
        strategy_id=strategy.id,
        strategy_code_hash=strategy_code_hash,
        config=config,
        account_id=account_id,
        paper_trading=paper_trading,
        instruments=instruments,
    )
    slug = generate_deployment_slug()

    deployment = LiveDeployment(
        id=uuid4(),
        strategy_id=strategy.id,
        strategy_code_hash=strategy_code_hash,
        config=config,
        instruments=instruments,
        status="stopped",
        paper_trading=paper_trading,
        started_by=user.id,
        deployment_slug=slug,
        identity_signature=identity.signature(),
        trader_id=derive_trader_id(slug),
        strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
        account_id=account_id,
        message_bus_stream=derive_message_bus_stream(slug),
        config_hash=identity.config_hash,
        instruments_signature=identity.instruments_signature,
    )
    session.add(deployment)
    await session.commit()
    return deployment


@pytest.mark.asyncio
async def test_insert_and_query(session: AsyncSession, deployment: LiveDeployment) -> None:
    """Baseline: create a row, read it back with the expected values."""
    row = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=12345,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="starting",
    )
    session.add(row)
    await session.commit()

    fetched = await session.get(LiveNodeProcess, row.id)
    assert fetched is not None
    assert fetched.deployment_id == deployment.id
    assert fetched.pid == 12345
    assert fetched.host == "test-host"
    assert fetched.status == "starting"
    assert fetched.exit_code is None
    assert fetched.error_message is None
    assert fetched.failure_kind is None


@pytest.mark.asyncio
async def test_pid_nullable(session: AsyncSession, deployment: LiveDeployment) -> None:
    """Codex v3 P1 fix: pid must be nullable because the supervisor inserts
    the row BEFORE process.start() returns a real pid. Subprocess self-write
    in 1.8 will populate it later.
    """
    row = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=None,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="starting",
    )
    session.add(row)
    await session.commit()
    assert row.pid is None


@pytest.mark.asyncio
async def test_building_status_accepted(session: AsyncSession, deployment: LiveDeployment) -> None:
    """Codex v3 P1 fix: the 'building' status is written by the subprocess
    during node.build() (decision #17). It must be a valid status value.
    """
    row = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=12345,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="building",
    )
    session.add(row)
    await session.commit()
    assert row.status == "building"


@pytest.mark.asyncio
async def test_failure_kind_accepts_all_enum_values(
    session: AsyncSession, deployment: LiveDeployment
) -> None:
    """v7/v8 addition: failure_kind is a String(32) column that stores
    FailureKind enum values (the model doesn't enforce the enum — the
    writers and readers do via FailureKind.parse_or_unknown())."""
    all_kinds = [
        "none",
        "halt_active",
        "spawn_failed_permanent",
        "reconciliation_failed",
        "build_timeout",
        "api_poll_timeout",
        "in_flight",
        "body_mismatch",
        "unknown",
    ]
    for kind in all_kinds:
        row = LiveNodeProcess(
            deployment_id=deployment.id,
            pid=None,
            host="test-host",
            started_at=_utcnow(),
            last_heartbeat_at=_utcnow(),
            status="failed",
            failure_kind=kind,
        )
        session.add(row)
        await session.flush()
        assert row.failure_kind == kind
        # Free the partial unique index slot by flipping to terminal status
        # (the 'failed' status is terminal — outside the active set — so
        # we can insert multiple rows for the same deployment).


@pytest.mark.asyncio
async def test_failure_kind_nullable(session: AsyncSession, deployment: LiveDeployment) -> None:
    """failure_kind is nullable for happy-path rows that haven't failed yet."""
    row = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=12345,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="running",
    )
    session.add(row)
    await session.commit()
    assert row.failure_kind is None


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_duplicate_active(
    session: AsyncSession, deployment: LiveDeployment
) -> None:
    """Codex v3 P0: at most one active row per deployment_id. The partial
    unique index covers status IN ('starting','building','ready','running','stopping').

    Decision #13 — this is the database layer of the idempotency model.
    """
    row1 = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=111,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="ready",
    )
    session.add(row1)
    await session.commit()

    # Second active row for the same deployment — should fail.
    row2 = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=222,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="starting",
    )
    session.add(row2)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_partial_unique_index_allows_terminal_plus_active(
    session: AsyncSession, deployment: LiveDeployment
) -> None:
    """A terminal row (stopped/failed) does NOT count toward the partial
    unique index, so a new active row can be created alongside it.

    This is what makes restart work: a prior run ended in 'stopped' or
    'failed', and the supervisor spawns a fresh process with a new row
    in 'starting' for the same deployment_id.
    """
    # First run ended in 'stopped'
    old = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=111,
        host="test-host",
        started_at=_utcnow() - timedelta(hours=1),
        last_heartbeat_at=_utcnow() - timedelta(hours=1),
        status="stopped",
        exit_code=0,
    )
    session.add(old)
    await session.commit()

    # New run starting
    new = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=None,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="starting",
    )
    session.add(new)
    await session.commit()

    assert old.status == "stopped"
    assert new.status == "starting"


@pytest.mark.asyncio
async def test_partial_unique_index_allows_failed_plus_active(
    session: AsyncSession, deployment: LiveDeployment
) -> None:
    """Same as above but the prior run failed."""
    old = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=111,
        host="test-host",
        started_at=_utcnow() - timedelta(hours=1),
        last_heartbeat_at=_utcnow() - timedelta(hours=1),
        status="failed",
        exit_code=1,
        error_message="bad config",
        failure_kind="spawn_failed_permanent",
    )
    session.add(old)
    await session.commit()

    new = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=None,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="building",
    )
    session.add(new)
    await session.commit()

    assert new.status == "building"


@pytest.mark.asyncio
async def test_stopping_status_counts_as_active(
    session: AsyncSession, deployment: LiveDeployment
) -> None:
    """Codex v4 P0: 'stopping' is included in the active-states query so
    a start-during-stop race is correctly blocked at the DB layer.

    (v4 documented this but the v4 ProcessManager.spawn query missed it;
    the partial index must cover it so the second spawn hits IntegrityError.)
    """
    stopping = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=111,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="stopping",
    )
    session.add(stopping)
    await session.commit()

    racing = LiveNodeProcess(
        deployment_id=deployment.id,
        pid=None,
        host="test-host",
        started_at=_utcnow(),
        last_heartbeat_at=_utcnow(),
        status="starting",
    )
    session.add(racing)
    with pytest.raises(IntegrityError):
        await session.commit()
