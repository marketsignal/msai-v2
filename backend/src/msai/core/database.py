"""Async database engine and session factory for MSAI v2.

Provides:
- ``engine`` -- a shared :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
- ``async_session_factory`` -- a session maker bound to the engine.
- ``get_db`` -- a FastAPI dependency that yields an ``AsyncSession``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.core.config import settings

engine: AsyncEngine = create_async_engine(settings.database_url, echo=False)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a scoped async database session.

    The session is automatically closed when the request finishes.
    """
    async with async_session_factory() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency returning the module-level ``async_session_factory``.

    Wrapping the instance in a callable lets endpoints that need
    session-per-subtask ownership get the factory injected via
    ``Depends(get_session_factory)`` and be overridable from tests
    (``app.dependency_overrides[get_session_factory] = lambda:
    test_factory``). Mirrors the ``get_db`` pattern one level up.
    """
    return async_session_factory
