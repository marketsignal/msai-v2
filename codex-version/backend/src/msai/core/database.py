from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.core.config import settings


def create_engine() -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


engine: AsyncEngine = create_engine()
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


async def check_db_ready() -> None:
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
