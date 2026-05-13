"""Full lifecycle: create portfolio -> add strategies -> snapshot ->
simulate deployment creation -> verify LiveDeploymentStrategy rows ->
verify read path.

Capstone integration test for PR#2 portfolio-per-account-live. Exercises
the full chain from portfolio creation through to deployment with a real
Postgres (via testcontainers) to catch FK constraints, partial unique
indexes, and cascade behavior that SQLite-backed tests would miss.
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
    LiveDeployment,
    LiveDeploymentStrategy,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
    Strategy,
    User,
)
from msai.services.live.deployment_identity import (
    derive_message_bus_stream,
    derive_portfolio_deployment_identity,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
)
from msai.services.live.portfolio_service import PortfolioService
from msai.services.live.revision_service import RevisionService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer — matches repo convention."""
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
async def test_full_portfolio_deploy_cycle(session: AsyncSession) -> None:
    """End-to-end: create portfolio, add strategies, snapshot (freeze),
    create LiveDeployment + LiveDeploymentStrategy rows, verify all
    invariants hold."""

    # -----------------------------------------------------------------
    # 1. Create user + 2 graduated strategies
    # -----------------------------------------------------------------
    user = User(
        id=uuid4(),
        entra_id=f"deploy-{uuid4().hex}",
        email=f"deploy-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategies: list[Strategy] = []
    for i in range(2):
        strat = Strategy(
            id=uuid4(),
            name=f"strat-{uuid4().hex[:8]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strat)
        await session.flush()
        # Graduate the strategy so the portfolio service accepts it
        session.add(
            GraduationCandidate(
                id=uuid4(),
                strategy_id=strat.id,
                stage="live_candidate",
                config={"fast": 10 + i},
                metrics={"sharpe": 1.5 + i * 0.1},
            )
        )
        await session.flush()
        strategies.append(strat)

    # -----------------------------------------------------------------
    # 2. Create portfolio via PortfolioService
    # -----------------------------------------------------------------
    psvc = PortfolioService(session)
    rsvc = RevisionService(session)

    portfolio = await psvc.create_portfolio(
        name=f"DeployTest-{uuid4().hex[:8]}",
        description="Full deploy cycle test",
        created_by=user.id,
    )
    assert isinstance(portfolio, LivePortfolio)

    # -----------------------------------------------------------------
    # 3. Add 2 strategies with weights (0.3, 0.7)
    # -----------------------------------------------------------------
    weights = [Decimal("0.3"), Decimal("0.7")]
    for i, strat in enumerate(strategies):
        await psvc.add_strategy(
            portfolio.id,
            strat.id,
            {"fast": 10 + i},
            [f"SYM{i}.NASDAQ"],
            weights[i],
        )
    await session.commit()

    # -----------------------------------------------------------------
    # 4. Snapshot (freeze) revision
    # -----------------------------------------------------------------
    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    assert revision.is_frozen is True
    assert revision.revision_number == 1

    # Verify revision strategies exist
    rev_strategies = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == revision.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rev_strategies) == 2

    # -----------------------------------------------------------------
    # 5. Create a LiveDeployment row with portfolio_revision_id set
    # -----------------------------------------------------------------
    slug = generate_deployment_slug()
    identity = derive_portfolio_deployment_identity(
        user_id=user.id,
        portfolio_revision_id=revision.id,
        account_id="DU1234567",
        paper_trading=True,
    )

    deployment = LiveDeployment(
        id=uuid4(),
        strategy_id=strategies[0].id,  # nullable FK, kept for audit trail
        status="running",
        paper_trading=True,
        started_by=user.id,
        deployment_slug=slug,
        identity_signature=identity.signature(),
        trader_id=derive_trader_id(slug),
        strategy_id_full=derive_strategy_id_full(
            strategies[0].strategy_class,
            slug,
            order_index=0,
        ),
        account_id="DU1234567",
        ib_login_key="msai-paper-primary",
        portfolio_revision_id=revision.id,
        message_bus_stream=derive_message_bus_stream(slug),
    )
    session.add(deployment)
    await session.flush()

    # -----------------------------------------------------------------
    # 6. Create 2 LiveDeploymentStrategy rows
    # -----------------------------------------------------------------
    for i, rev_strat in enumerate(rev_strategies):
        sid_full = derive_strategy_id_full(
            strategies[i].strategy_class,
            slug,
            order_index=i,
        )
        lds = LiveDeploymentStrategy(
            id=uuid4(),
            deployment_id=deployment.id,
            revision_strategy_id=rev_strat.id,
            strategy_id_full=sid_full,
        )
        session.add(lds)
    await session.commit()

    # -----------------------------------------------------------------
    # 7. Assert the deployment has portfolio_revision_id
    # -----------------------------------------------------------------
    fetched_deployment = await session.get(LiveDeployment, deployment.id)
    assert fetched_deployment is not None
    assert fetched_deployment.portfolio_revision_id == revision.id

    # -----------------------------------------------------------------
    # 8. Assert 2 LiveDeploymentStrategy rows exist
    # -----------------------------------------------------------------
    lds_rows = (
        (
            await session.execute(
                select(LiveDeploymentStrategy).where(
                    LiveDeploymentStrategy.deployment_id == deployment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(lds_rows) == 2
    # Each row should have a unique strategy_id_full
    sid_fulls = {row.strategy_id_full for row in lds_rows}
    assert len(sid_fulls) == 2
    # Each strategy_id_full should contain the slug
    for sid in sid_fulls:
        assert slug in sid

    # -----------------------------------------------------------------
    # 9. Assert the revision is frozen
    # -----------------------------------------------------------------
    fetched_revision = await session.get(LivePortfolioRevision, revision.id)
    assert fetched_revision is not None
    assert fetched_revision.is_frozen is True


@pytest.mark.asyncio
async def test_deployment_cascade_deletes_deployment_strategies(
    session: AsyncSession,
) -> None:
    """Deleting a LiveDeployment cascades to its LiveDeploymentStrategy rows.

    The FK ``live_deployment_strategies.deployment_id`` has
    ``ondelete=CASCADE``. This test verifies that constraint works end-to-end
    with real Postgres (SQLite handles CASCADE differently).
    """
    # Setup: user + strategy + graduation + portfolio + revision + deployment
    user = User(
        id=uuid4(),
        entra_id=f"cascade-{uuid4().hex}",
        email=f"cascade-{uuid4().hex}@example.com",
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
        name=f"CascadeTest-{uuid4().hex[:8]}",
        description=None,
        created_by=user.id,
    )
    await psvc.add_strategy(portfolio.id, strat.id, {}, ["AAPL.NASDAQ"], Decimal("1"))
    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    rev_strats = (
        (
            await session.execute(
                select(LivePortfolioRevisionStrategy).where(
                    LivePortfolioRevisionStrategy.revision_id == revision.id
                )
            )
        )
        .scalars()
        .all()
    )

    slug = generate_deployment_slug()
    identity = derive_portfolio_deployment_identity(
        user_id=user.id,
        portfolio_revision_id=revision.id,
        account_id="DU9999999",
        paper_trading=True,
    )
    deployment = LiveDeployment(
        id=uuid4(),
        strategy_id=strat.id,
        status="running",
        paper_trading=True,
        started_by=user.id,
        deployment_slug=slug,
        identity_signature=identity.signature(),
        trader_id=derive_trader_id(slug),
        strategy_id_full=derive_strategy_id_full(strat.strategy_class, slug),
        account_id="DU9999999",
        ib_login_key="msai-paper-primary",
        portfolio_revision_id=revision.id,
        message_bus_stream=derive_message_bus_stream(slug),
    )
    session.add(deployment)
    await session.flush()

    lds = LiveDeploymentStrategy(
        id=uuid4(),
        deployment_id=deployment.id,
        revision_strategy_id=rev_strats[0].id,
        strategy_id_full=derive_strategy_id_full(strat.strategy_class, slug),
    )
    session.add(lds)
    await session.commit()

    deployment_id = deployment.id

    # Delete the deployment
    await session.delete(deployment)
    await session.commit()

    # Verify cascade: no LiveDeploymentStrategy rows remain
    remaining = (
        (
            await session.execute(
                select(LiveDeploymentStrategy).where(
                    LiveDeploymentStrategy.deployment_id == deployment_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == [], (
        "LiveDeploymentStrategy rows should be cascade-deleted with the deployment"
    )
