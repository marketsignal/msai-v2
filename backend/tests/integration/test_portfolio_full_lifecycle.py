"""Full-lifecycle integration test — exercises every PR#1 surface.

No FK cycle: the portfolio can be deleted and CASCADE removes
revisions + member rows in one step, without needing to null any
back-pointer first.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import (
    Base,
    GraduationCandidate,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
    Strategy,
    User,
)
from msai.services.live.portfolio_service import PortfolioService
from msai.services.live.revision_service import RevisionService

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


@pytest.mark.asyncio
async def test_full_lifecycle_create_add_snapshot_rebalance(
    session: AsyncSession,
) -> None:
    user = User(
        id=uuid4(),
        entra_id=f"full-{uuid4().hex}",
        email=f"full-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategies = []
    for _ in range(3):
        strat = Strategy(
            id=uuid4(),
            name=f"s-{uuid4().hex[:8]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strat)
        await session.flush()
        session.add(
            GraduationCandidate(
                id=uuid4(),
                strategy_id=strat.id,
                stage="live_candidate",
                config={},
                metrics={},
            )
        )
        await session.flush()
        strategies.append(strat)

    psvc = PortfolioService(session)
    rsvc = RevisionService(session)

    portfolio = await psvc.create_portfolio(
        name="Full-Lifecycle", description="End-to-end", created_by=user.id
    )
    for i, strat in enumerate(strategies):
        await psvc.add_strategy(
            portfolio.id,
            strat.id,
            {"fast": 10 + i},
            [f"SYM{i}.NASDAQ"],
            Decimal("0.333333"),
        )
    await session.commit()

    rev1 = await rsvc.snapshot(portfolio.id)
    await session.commit()
    assert rev1.is_frozen is True
    assert rev1.revision_number == 1

    # Start a new draft: add 2 strategies with different weights.
    await psvc.add_strategy(
        portfolio.id, strategies[0].id, {"fast": 10}, ["SYM0.NASDAQ"], Decimal("0.5")
    )
    await psvc.add_strategy(
        portfolio.id, strategies[1].id, {"fast": 11}, ["SYM1.NASDAQ"], Decimal("0.5")
    )
    await session.commit()

    rev2 = await rsvc.snapshot(portfolio.id)
    await session.commit()
    assert rev2.id != rev1.id
    assert rev2.revision_number == 2
    assert rev2.composition_hash != rev1.composition_hash

    # get_active_revision returns the latest frozen.
    active = await rsvc.get_active_revision(portfolio.id)
    assert active is not None
    assert active.id == rev2.id

    # rev1 is preserved as audit history with its 3 members.
    rev1_members = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == rev1.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rev1_members) == 3


@pytest.mark.asyncio
async def test_deleting_portfolio_cascades_cleanly(session: AsyncSession) -> None:
    """FK ondelete=CASCADE removes revisions and their member rows in
    one DELETE. No pointer-nulling workaround needed — there's no FK
    cycle in this schema."""
    user = User(
        id=uuid4(),
        entra_id=f"casc-{uuid4().hex}",
        email=f"casc-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    strat = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strat)
    await session.flush()
    session.add(
        GraduationCandidate(
            id=uuid4(),
            strategy_id=strat.id,
            stage="live_candidate",
            config={},
            metrics={},
        )
    )
    await session.flush()

    psvc = PortfolioService(session)
    rsvc = RevisionService(session)
    portfolio = await psvc.create_portfolio(
        name="ToDelete", description=None, created_by=user.id
    )
    await psvc.add_strategy(
        portfolio.id, strat.id, {}, ["AAPL.NASDAQ"], Decimal("1")
    )
    rev = await rsvc.snapshot(portfolio.id)
    await session.commit()

    portfolio_id = portfolio.id
    revision_id = rev.id
    await session.delete(portfolio)
    await session.commit()

    remaining_revisions = (
        (
            await session.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.portfolio_id == portfolio_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining_revisions == []

    remaining_members = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == revision_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining_members == []
