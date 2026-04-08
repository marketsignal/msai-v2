"""Integration tests for the live_deployments stable-identity schema
(Phase 1 task 1.1b).

Verifies that the new columns added by the v9 plan are present, populated,
and correctly enforced at the DB layer:

- ``deployment_slug``      (String(16), unique, indexed)
- ``identity_signature``   (String(64), unique, indexed)
- ``trader_id``            (String(32))
- ``strategy_id_full``     (String(64))
- ``account_id``           (String(32))
- ``message_bus_stream``   (String(96))
- ``config_hash``          (String(64))
- ``instruments_signature`` (String(512))
- ``last_started_at``      (DateTime(tz=True), nullable)
- ``last_stopped_at``      (DateTime(tz=True), nullable)
- ``startup_hard_timeout_s`` (Integer, nullable)

The OLD ``started_at`` / ``stopped_at`` columns are dropped — replaced
by ``last_started_at`` / ``last_stopped_at``.

SAFETY: provisions its own ``PostgresContainer`` (same pattern as
test_live_node_process_model.py) so the destructive ``drop_all/create_all``
fixture can never touch a configured DATABASE_URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, LiveDeployment, Strategy, User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def user_and_strategy(
    session: AsyncSession,
) -> AsyncIterator[tuple[User, Strategy]]:
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
        name="ema-cross",
        file_path="strategies/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.commit()
    yield user, strategy


def _make_deployment(
    *,
    user: User,
    strategy: Strategy,
    deployment_slug: str = "abcd1234abcd1234",
    identity_signature: str | None = None,
    config: dict | None = None,
    instruments: list[str] | None = None,
    paper_trading: bool = True,
) -> LiveDeployment:
    """Build a fully-populated LiveDeployment for the new schema."""
    if config is None:
        config = {"fast": 10, "slow": 20}
    if instruments is None:
        instruments = ["AAPL.NASDAQ"]
    if identity_signature is None:
        identity_signature = "f" * 64
    return LiveDeployment(
        id=uuid4(),
        strategy_id=strategy.id,
        strategy_code_hash="deadbeef" * 8,
        config=config,
        instruments=instruments,
        status="stopped",
        paper_trading=paper_trading,
        started_by=user.id,
        # New v9 columns:
        deployment_slug=deployment_slug,
        identity_signature=identity_signature,
        trader_id=f"MSAI-{deployment_slug}",
        strategy_id_full=f"{strategy.strategy_class}-{deployment_slug}",
        account_id="DU1234567",
        message_bus_stream=f"trader-MSAI-{deployment_slug}-stream",
        config_hash="cafebabe" * 8,
        instruments_signature=",".join(sorted(instruments)),
        last_started_at=None,
        last_stopped_at=None,
        startup_hard_timeout_s=None,
    )


# ---------------------------------------------------------------------------
# Column shape tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_with_all_new_columns(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Baseline: every new column accepts a value and survives a round-trip."""
    user, strategy = user_and_strategy
    deployment = _make_deployment(user=user, strategy=strategy)
    session.add(deployment)
    await session.commit()

    fetched = await session.get(LiveDeployment, deployment.id)
    assert fetched is not None
    assert fetched.deployment_slug == "abcd1234abcd1234"
    assert fetched.identity_signature == "f" * 64
    assert fetched.trader_id == "MSAI-abcd1234abcd1234"
    assert fetched.strategy_id_full == "EMACrossStrategy-abcd1234abcd1234"
    assert fetched.account_id == "DU1234567"
    assert fetched.message_bus_stream == "trader-MSAI-abcd1234abcd1234-stream"
    assert fetched.config_hash == "cafebabe" * 8
    assert fetched.instruments_signature == "AAPL.NASDAQ"
    assert fetched.last_started_at is None
    assert fetched.last_stopped_at is None
    assert fetched.startup_hard_timeout_s is None


@pytest.mark.asyncio
async def test_last_started_and_stopped_round_trip(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    user, strategy = user_and_strategy
    deployment = _make_deployment(user=user, strategy=strategy)
    now = _utcnow()
    deployment.last_started_at = now
    deployment.last_stopped_at = now
    session.add(deployment)
    await session.commit()

    fetched = await session.get(LiveDeployment, deployment.id)
    assert fetched is not None
    assert fetched.last_started_at is not None
    assert fetched.last_stopped_at is not None


@pytest.mark.asyncio
async def test_startup_hard_timeout_override(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Per-deployment override for the supervisor watchdog (Codex v7 P2)."""
    user, strategy = user_and_strategy
    deployment = _make_deployment(user=user, strategy=strategy)
    deployment.startup_hard_timeout_s = 3600  # 1 hour for huge options universe
    session.add(deployment)
    await session.commit()

    fetched = await session.get(LiveDeployment, deployment.id)
    assert fetched is not None
    assert fetched.startup_hard_timeout_s == 3600


# ---------------------------------------------------------------------------
# Uniqueness constraint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_signature_is_unique(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Decision #7: identity_signature is the single source of identity
    truth. Two rows with the same signature must be rejected at the DB
    layer (warm restart should reuse the existing row, not insert a duplicate).
    """
    user, strategy = user_and_strategy
    sig = "abcd" * 16  # 64 chars

    a = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="0000000000000001",
        identity_signature=sig,
    )
    session.add(a)
    await session.commit()

    b = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="0000000000000002",
        identity_signature=sig,
    )
    session.add(b)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_deployment_slug_is_unique(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Distinct identity signatures may NOT collide on deployment_slug
    either — the slug is what trader_id is built from and is the primary
    user-facing identifier.
    """
    user, strategy = user_and_strategy
    a = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="aaaaaaaaaaaaaaaa",
        identity_signature="1" * 64,
    )
    session.add(a)
    await session.commit()

    b = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="aaaaaaaaaaaaaaaa",  # collision
        identity_signature="2" * 64,
    )
    session.add(b)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_distinct_signatures_distinct_slugs_both_succeed(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """The happy multi-deployment case: same user/strategy can run two
    parameterizations side-by-side (e.g. EMA(10,20) vs EMA(50,200))
    with isolated state because each gets a distinct identity_signature."""
    user, strategy = user_and_strategy
    a = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="0000000000000001",
        identity_signature="1" * 64,
        config={"fast": 10, "slow": 20},
    )
    b = _make_deployment(
        user=user,
        strategy=strategy,
        deployment_slug="0000000000000002",
        identity_signature="2" * 64,
        config={"fast": 50, "slow": 200},
    )
    session.add_all([a, b])
    await session.commit()  # both succeed


@pytest.mark.asyncio
async def test_old_started_at_and_stopped_at_columns_are_gone(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """The plan drops the old columns. Verify the model no longer has them
    so a stale `LiveDeployment(started_at=...)` constructor call would fail.
    """
    # The model class itself must not expose these attributes any more.
    assert not hasattr(LiveDeployment, "started_at")
    assert not hasattr(LiveDeployment, "stopped_at")
