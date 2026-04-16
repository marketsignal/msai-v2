"""Integration tests for RevisionService."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, GraduationCandidate, Strategy, User
from msai.services.live.portfolio_service import PortfolioService
from msai.services.live.revision_service import (
    RevisionImmutableError,
    RevisionService,
)


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


async def _seed_portfolio_with_one_graduated_strategy(
    session: AsyncSession,
) -> tuple:
    user = User(
        id=uuid4(),
        entra_id=f"r-{uuid4().hex}",
        email=f"r-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    strategy = Strategy(
        id=uuid4(),
        name=f"r-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()
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

    psvc = PortfolioService(session)
    portfolio = await psvc.create_portfolio(
        name=f"P-{uuid4().hex[:8]}", description=None, created_by=user.id
    )
    await psvc.add_strategy(
        portfolio.id, strategy.id, {"fast": 10}, ["AAPL.NASDAQ"], Decimal("1")
    )
    return portfolio, strategy, user


@pytest.mark.asyncio
async def test_snapshot_freezes_draft_and_advances_number(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)
    psvc = PortfolioService(session)

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    assert revision.is_frozen is True
    assert revision.composition_hash != "0" * 64
    assert len(revision.composition_hash) == 64
    assert revision.revision_number == 1

    # No more draft after snapshot.
    assert await psvc.get_current_draft(portfolio.id) is None


@pytest.mark.asyncio
async def test_snapshot_same_composition_returns_existing_revision(
    session: AsyncSession,
) -> None:
    """Two snapshots with identical composition collapse to the same
    revision (no duplicate frozen rows)."""
    portfolio, strategy, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)
    psvc = PortfolioService(session)

    first = await rsvc.snapshot(portfolio.id)
    await session.commit()

    await psvc.add_strategy(
        portfolio.id, strategy.id, {"fast": 10}, ["AAPL.NASDAQ"], Decimal("1")
    )
    await session.commit()

    second = await rsvc.snapshot(portfolio.id)
    await session.commit()

    assert second.id == first.id


@pytest.mark.asyncio
async def test_get_active_revision_returns_latest_frozen(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)

    assert await rsvc.get_active_revision(portfolio.id) is None

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    active = await rsvc.get_active_revision(portfolio.id)
    assert active is not None
    assert active.id == revision.id


@pytest.mark.asyncio
async def test_enforce_immutability_raises_for_frozen(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    rsvc = RevisionService(session)

    revision = await rsvc.snapshot(portfolio.id)
    await session.commit()

    with pytest.raises(RevisionImmutableError):
        await rsvc.enforce_immutability(revision.id)


@pytest.mark.asyncio
async def test_enforce_immutability_noop_for_draft(session: AsyncSession) -> None:
    portfolio, _, _ = await _seed_portfolio_with_one_graduated_strategy(session)
    psvc = PortfolioService(session)
    rsvc = RevisionService(session)

    draft = await psvc.get_current_draft(portfolio.id)
    assert draft is not None

    await rsvc.enforce_immutability(draft.id)  # must not raise


@pytest.mark.asyncio
async def test_snapshot_raises_when_no_draft(session: AsyncSession) -> None:
    """Calling snapshot on a portfolio with no draft is a programming
    error, not a silent no-op."""
    user = User(
        id=uuid4(),
        entra_id=f"nd-{uuid4().hex}",
        email=f"nd-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    from msai.models import LivePortfolio

    portfolio = LivePortfolio(
        id=uuid4(), name="Empty", description=None, created_by=user.id
    )
    session.add(portfolio)
    await session.commit()

    rsvc = RevisionService(session)
    with pytest.raises(ValueError, match="no draft"):
        await rsvc.snapshot(portfolio.id)
