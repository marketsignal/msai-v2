"""Unit tests for ``msai.core.soft_delete`` global query filter.

Covers the ``do_orm_execute`` event listener that injects
``with_loader_criteria(Strategy, deleted_at IS NULL)`` into every ORM
SELECT by default and the ``include_deleted=True`` opt-out used by
DETAIL / SUPERVISOR / SYNC code paths (plan R20 in
``docs/plans/2026-05-16-ui-completeness.md``).

Uses a Postgres testcontainer to exercise the real SQLAlchemy event
plumbing — ``AsyncSession`` delegates ORM events to its underlying sync
``Session``, so the listener target must be ``Session`` (not
``AsyncSession``). A pure-in-memory ORM stub would not catch a wrong
target binding, hence the real-DB choice for this "unit" test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from msai.core.soft_delete import register_soft_delete_listeners, soft_delete_filter
from msai.models import Base, Strategy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    """Per-test async session with the soft-delete listener registered."""
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    register_soft_delete_listeners()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _make_strategy(*, archived: bool = False, name: str | None = None) -> Strategy:
    return Strategy(
        id=uuid4(),
        name=name or f"strat-{uuid4().hex[:8]}",
        file_path=f"/tmp/{uuid4().hex}.py",
        strategy_class="DummyStrategy",
        config_class=None,
        config_schema=None,
        default_config=None,
        config_schema_status="no_config_class",
        code_hash="0" * 64,
        deleted_at=datetime.now(UTC) if archived else None,
    )


@pytest.mark.asyncio
async def test_default_select_hides_soft_deleted_strategies(session: AsyncSession) -> None:
    # Arrange
    active = _make_strategy(name="alive", archived=False)
    archived = _make_strategy(name="ghost", archived=True)
    session.add_all([active, archived])
    await session.commit()

    # Act
    result = await session.execute(select(Strategy))
    rows = result.scalars().all()

    # Assert
    ids = {row.id for row in rows}
    assert active.id in ids
    assert archived.id not in ids


@pytest.mark.asyncio
async def test_default_select_filters_by_id_lookup(session: AsyncSession) -> None:
    """A targeted ``WHERE id == <archived_id>`` still returns nothing."""
    # Arrange
    archived = _make_strategy(archived=True)
    session.add(archived)
    await session.commit()

    # Act
    result = await session.execute(select(Strategy).where(Strategy.id == archived.id))
    row = result.scalar_one_or_none()

    # Assert
    assert row is None


@pytest.mark.asyncio
async def test_include_deleted_opt_in_returns_archived_rows(session: AsyncSession) -> None:
    """``execution_options(include_deleted=True)`` skips the filter."""
    # Arrange
    active = _make_strategy(name="alive", archived=False)
    archived = _make_strategy(name="ghost", archived=True)
    session.add_all([active, archived])
    await session.commit()

    # Act
    result = await session.execute(select(Strategy).execution_options(include_deleted=True))
    rows = result.scalars().all()

    # Assert
    ids = {row.id for row in rows}
    assert active.id in ids
    assert archived.id in ids


@pytest.mark.asyncio
async def test_include_deleted_opt_in_resolves_archived_by_id(session: AsyncSession) -> None:
    """DETAIL path: ``WHERE id == <archived_id>`` + opt-in returns row."""
    # Arrange
    archived = _make_strategy(archived=True)
    session.add(archived)
    await session.commit()

    # Act
    result = await session.execute(
        select(Strategy).where(Strategy.id == archived.id).execution_options(include_deleted=True)
    )
    row = result.scalar_one_or_none()

    # Assert
    assert row is not None
    assert row.id == archived.id


def test_register_soft_delete_listeners_is_idempotent() -> None:
    """Re-registering the listener does not stack duplicate bindings.

    The function targets the abstract sync ``Session`` class (not
    ``AsyncSession``) because SQLAlchemy 2.0 routes async ORM events
    through the underlying ``sync_session``. Listening on ``Session``
    covers both surfaces with one binding.
    """
    register_soft_delete_listeners()
    register_soft_delete_listeners()
    register_soft_delete_listeners()

    assert event.contains(Session, "do_orm_execute", soft_delete_filter)


async def test_relationship_load_requires_include_deleted_opt_in(
    session: AsyncSession,
) -> None:
    """Historical backtests need to surface the (archived) strategy that
    produced them. PR test-analyzer iter-1 P1 #2 asked for a test pinning
    this behavior. Writing it revealed that ``selectinload``'s secondary
    SELECT in SQLAlchemy 2.0 reports ``state.is_relationship_load=False``,
    so ``soft_delete_filter``'s skip does NOT pass relationship loads
    through — the contract is "use ``execution_options(include_deleted=True)``
    on the parent SELECT" (plan R20 DETAIL classification).
    """
    from datetime import date

    from sqlalchemy.orm import selectinload

    from msai.models.backtest import Backtest

    # 1) Active strategy
    active = _make_strategy(name="rel-load-active", archived=False)
    session.add(active)
    await session.flush()

    # 2) Backtest referencing it
    bt = Backtest(
        strategy_id=active.id,
        strategy_code_hash="x" * 64,
        config={},
        instruments=[],
        start_date=date(2024, 1, 1),
        end_date=date(2025, 1, 1),
        status="completed",
    )
    session.add(bt)
    await session.commit()
    backtest_id = bt.id

    # 3) Archive the strategy
    active.deleted_at = datetime.now(UTC)
    await session.commit()
    session.expire_all()

    # 4) WITHOUT opt-in: relationship load filters out archived rows
    result = await session.execute(
        select(Backtest).where(Backtest.id == backtest_id).options(selectinload(Backtest.strategy))
    )
    fetched_bt = result.scalar_one()
    assert fetched_bt.strategy is None

    session.expire_all()

    # 5) WITH execution_options(include_deleted=True): resolves archived row
    result2 = await session.execute(
        select(Backtest)
        .where(Backtest.id == backtest_id)
        .options(selectinload(Backtest.strategy))
        .execution_options(include_deleted=True)
    )
    fetched_bt2 = result2.scalar_one()
    assert fetched_bt2.strategy is not None
    assert fetched_bt2.strategy.id == active.id
    assert fetched_bt2.strategy.deleted_at is not None
