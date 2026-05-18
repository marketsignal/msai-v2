"""Integration test: soft-deleted strategies remain resolvable on live paths.

Plan R14/R20 (``docs/plans/2026-05-16-ui-completeness.md``): when a
strategy is archived while an active deployment references it, the
SUPERVISOR-side queries — multi-strategy resolution in
``/api/v1/live/start-portfolio`` (``api/live.py:828``) and member
resolution in ``live_supervisor/__main__.py:231`` — must keep resolving
the archived ``Strategy`` row so the deployment continues running until
it is explicitly stopped.

This test exercises the actual database listener (registered on
``AsyncSession``) end-to-end against a real Postgres container to prove
the partial index migration + listener + ``include_deleted=True`` opt-in
work together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.core.soft_delete import register_soft_delete_listeners
from msai.models import Base, Strategy, User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


# ---------------------------------------------------------------------------
# Fixtures — module-scoped Postgres container per repo convention
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Register the listener on the sync ``Session`` class — every
    # ``AsyncSession`` factory inherits it via ``sync_session``. Mirrors
    # the production wiring T4b will install in ``main.py`` lifespan.
    register_soft_delete_listeners()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        id=uuid4(),
        entra_id=f"sub-{uuid4().hex}",
        email=f"t-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_strategy(session: AsyncSession, user: User) -> Strategy:
    strategy = Strategy(
        id=uuid4(),
        name=f"strat-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()
    return strategy


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_resolves_archived_strategy_via_include_deleted(
    session: AsyncSession,
) -> None:
    """SUPERVISOR opt-in path resolves an archived strategy by id-in.

    Replays the exact query shape used by ``/live/start-portfolio`` and
    ``live_supervisor/__main__.py``: ``select(Strategy).where(id.in_(...))
    .execution_options(include_deleted=True)``.
    """
    user = await _seed_user(session)
    strategy = await _seed_strategy(session, user)
    strategy_id = strategy.id
    await session.commit()

    # Archive the strategy mid-deployment.
    strategy.deleted_at = datetime.now(UTC)
    await session.commit()

    # Default-filtered SELECT — archived rows are hidden.
    default_result = await session.execute(select(Strategy).where(Strategy.id.in_([strategy_id])))
    assert default_result.scalar_one_or_none() is None, (
        "default filter must hide archived strategies"
    )

    # SUPERVISOR opt-in SELECT — archived row is resolved.
    opt_in_result = await session.execute(
        select(Strategy)
        .where(Strategy.id.in_([strategy_id]))
        .execution_options(include_deleted=True)
    )
    rows = opt_in_result.scalars().all()
    assert len(rows) == 1
    assert rows[0].id == strategy_id
    assert rows[0].deleted_at is not None


@pytest.mark.asyncio
async def test_archived_strategy_listed_via_list_query_is_hidden(
    session: AsyncSession,
) -> None:
    """LIST path: archived strategies disappear from ``select(Strategy)``."""
    user = await _seed_user(session)
    active = await _seed_strategy(session, user)
    archived = await _seed_strategy(session, user)
    archived.deleted_at = datetime.now(UTC)
    await session.commit()

    rows = (await session.execute(select(Strategy))).scalars().all()
    ids = {r.id for r in rows}
    assert active.id in ids
    assert archived.id not in ids


@pytest.mark.asyncio
async def test_archived_strategy_visible_to_include_deleted_list_query(
    session: AsyncSession,
) -> None:
    """Sanity: a list query with the opt-in returns active + archived."""
    user = await _seed_user(session)
    active = await _seed_strategy(session, user)
    archived = await _seed_strategy(session, user)
    archived.deleted_at = datetime.now(UTC)
    await session.commit()

    rows = (
        (await session.execute(select(Strategy).execution_options(include_deleted=True)))
        .scalars()
        .all()
    )
    ids = {r.id for r in rows}
    assert {active.id, archived.id}.issubset(ids)
