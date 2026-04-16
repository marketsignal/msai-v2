"""Integration tests for PortfolioService."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import (
    Base,
    GraduationCandidate,
    Strategy,
    User,
)
from msai.services.live.portfolio_service import (
    PortfolioService,
    StrategyNotGraduatedError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer per module — matches the repo
    convention (`test_live_node_process_model.py`, `test_heartbeat_thread.py`)."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        id=uuid4(),
        entra_id=f"p-{uuid4().hex}",
        email=f"p-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_strategy(
    session: AsyncSession, user: User, *, graduated: bool
) -> Strategy:
    strategy = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()
    if graduated:
        # GraduationCandidate requires config + metrics NOT NULL —
        # empty dicts satisfy the constraint without faking metrics.
        session.add(
            GraduationCandidate(
                id=uuid4(),
                strategy_id=strategy.id,
                stage="promoted",
                config={},
                metrics={},
            )
        )
        await session.flush()
    return strategy


@pytest.mark.asyncio
async def test_create_portfolio_has_no_draft_initially(session: AsyncSession) -> None:
    user = await _seed_user(session)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(
        name="Growth-1", description=None, created_by=user.id
    )
    await session.commit()

    assert portfolio.name == "Growth-1"
    assert await svc.get_current_draft(portfolio.id) is None


@pytest.mark.asyncio
async def test_add_strategy_creates_draft_lazily(session: AsyncSession) -> None:
    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G2", description=None, created_by=user.id)
    await svc.add_strategy(
        portfolio_id=portfolio.id,
        strategy_id=strategy.id,
        config={"fast": 10},
        instruments=["AAPL.NASDAQ"],
        weight=Decimal("0.5"),
    )
    await session.commit()

    members = await svc.list_draft_members(portfolio.id)
    assert len(members) == 1
    assert members[0].strategy_id == strategy.id
    assert members[0].order_index == 0

    draft = await svc.get_current_draft(portfolio.id)
    assert draft is not None
    assert draft.is_frozen is False


@pytest.mark.asyncio
async def test_add_ungraduated_strategy_rejected(session: AsyncSession) -> None:
    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=False)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G3", description=None, created_by=user.id)

    with pytest.raises(StrategyNotGraduatedError):
        await svc.add_strategy(
            portfolio_id=portfolio.id,
            strategy_id=strategy.id,
            config={},
            instruments=["AAPL.NASDAQ"],
            weight=Decimal("1"),
        )


@pytest.mark.asyncio
async def test_second_add_assigns_next_order_index(session: AsyncSession) -> None:
    user = await _seed_user(session)
    s1 = await _seed_strategy(session, user, graduated=True)
    s2 = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G4", description=None, created_by=user.id)
    await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("0.5"))
    await svc.add_strategy(portfolio.id, s2.id, {}, ["MSFT.NASDAQ"], Decimal("0.5"))
    await session.commit()

    members = await svc.list_draft_members(portfolio.id)
    assert [m.order_index for m in members] == [0, 1]
    assert [m.strategy_id for m in members] == [s1.id, s2.id]


@pytest.mark.asyncio
async def test_add_same_strategy_twice_raises(session: AsyncSession) -> None:
    user = await _seed_user(session)
    s1 = await _seed_strategy(session, user, graduated=True)
    svc = PortfolioService(session)

    portfolio = await svc.create_portfolio(name="G5", description=None, created_by=user.id)
    await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("1"))
    await session.commit()

    with pytest.raises(ValueError, match="already a member"):
        await svc.add_strategy(portfolio.id, s1.id, {}, ["AAPL.NASDAQ"], Decimal("0.5"))


@pytest.mark.asyncio
async def test_partial_index_rejects_second_draft(session: AsyncSession) -> None:
    """The ``uq_one_draft_per_portfolio`` partial unique index must
    enforce "at most one unfrozen revision per portfolio" at the DB
    level. Declared inline on ``LivePortfolioRevision.__table_args__``
    so ``Base.metadata.create_all`` produces the same schema as the
    migration."""
    from sqlalchemy.exc import IntegrityError

    from msai.models import LivePortfolio, LivePortfolioRevision

    user = await _seed_user(session)
    portfolio = LivePortfolio(
        id=uuid4(),
        name=f"PartialIdx-{uuid4().hex[:8]}",
        description=None,
        created_by=user.id,
    )
    session.add(portfolio)
    await session.flush()

    session.add(
        LivePortfolioRevision(
            id=uuid4(),
            portfolio_id=portfolio.id,
            revision_number=1,
            composition_hash="0" * 64,
            is_frozen=False,
        )
    )
    await session.flush()

    session.add(
        LivePortfolioRevision(
            id=uuid4(),
            portfolio_id=portfolio.id,
            revision_number=2,
            composition_hash="1" * 64,
            is_frozen=False,
        )
    )
    with pytest.raises(IntegrityError, match="uq_one_draft_per_portfolio"):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_partial_index_allows_two_frozen_revisions(session: AsyncSession) -> None:
    """The partial index only constrains ``is_frozen = false`` rows.
    Two frozen revisions on the same portfolio must coexist (audit
    history across rebalances)."""
    from msai.models import LivePortfolio, LivePortfolioRevision

    user = await _seed_user(session)
    portfolio = LivePortfolio(
        id=uuid4(),
        name=f"TwoFrozen-{uuid4().hex[:8]}",
        description=None,
        created_by=user.id,
    )
    session.add(portfolio)
    await session.flush()

    for n, h in ((1, "a" * 64), (2, "b" * 64)):
        session.add(
            LivePortfolioRevision(
                id=uuid4(),
                portfolio_id=portfolio.id,
                revision_number=n,
                composition_hash=h,
                is_frozen=True,
            )
        )
    await session.flush()
    await session.commit()


@pytest.mark.asyncio
async def test_weight_check_rejects_zero(session: AsyncSession) -> None:
    """``ck_lprs_weight_range`` enforces weight ∈ (0, 1] at the DB
    level. Weight == 0 violates ``weight > 0``."""
    from sqlalchemy.exc import IntegrityError

    from msai.models import LivePortfolio, LivePortfolioRevision, LivePortfolioRevisionStrategy

    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=True)
    portfolio = LivePortfolio(
        id=uuid4(), name=f"WZero-{uuid4().hex[:8]}", description=None, created_by=user.id
    )
    session.add(portfolio)
    await session.flush()
    rev = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=portfolio.id,
        revision_number=1,
        composition_hash="0" * 64,
        is_frozen=False,
    )
    session.add(rev)
    await session.flush()
    session.add(
        LivePortfolioRevisionStrategy(
            id=uuid4(),
            revision_id=rev.id,
            strategy_id=strategy.id,
            config={},
            instruments=["AAPL.NASDAQ"],
            weight=Decimal("0"),
            order_index=0,
        )
    )
    with pytest.raises(IntegrityError, match="ck_lprs_weight_range"):
        await session.flush()
    await session.rollback()


@pytest.mark.asyncio
async def test_weight_check_rejects_over_one(session: AsyncSession) -> None:
    """Weight > 1 violates ``weight <= 1``."""
    from sqlalchemy.exc import IntegrityError

    from msai.models import LivePortfolio, LivePortfolioRevision, LivePortfolioRevisionStrategy

    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user, graduated=True)
    portfolio = LivePortfolio(
        id=uuid4(), name=f"WOver-{uuid4().hex[:8]}", description=None, created_by=user.id
    )
    session.add(portfolio)
    await session.flush()
    rev = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=portfolio.id,
        revision_number=1,
        composition_hash="0" * 64,
        is_frozen=False,
    )
    session.add(rev)
    await session.flush()
    session.add(
        LivePortfolioRevisionStrategy(
            id=uuid4(),
            revision_id=rev.id,
            strategy_id=strategy.id,
            config={},
            instruments=["AAPL.NASDAQ"],
            weight=Decimal("1.5"),
            order_index=0,
        )
    )
    with pytest.raises(IntegrityError, match="ck_lprs_weight_range"):
        await session.flush()
    await session.rollback()
