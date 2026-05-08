"""Re-export the shared symbol-onboarding fixtures for service tests.

Mirrors the pattern in ``tests/integration/symbol_onboarding/conftest.py``:
the canonical fixture module lives at
``tests/integration/conftest_symbol_onboarding.py`` (named so pytest does
not auto-discover it as a parent-level conftest). Tests under
``tests/integration/services/symbol_onboarding/`` opt in here.

We additionally expose a thin ``db_session`` fixture that yields a single
:class:`AsyncSession` instance — convenient for tests that want a single
session rather than a session factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from tests.integration.conftest_symbol_onboarding import (  # noqa: F401
    isolated_postgres_url,
    session_factory,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811 — pytest fixture reuse
) -> AsyncIterator[AsyncSession]:
    """A single :class:`AsyncSession` per test, closed on teardown.

    The underlying engine + schema are owned by ``session_factory``
    (module-scoped); each test gets a fresh session bound to that engine.
    """
    async with session_factory() as session:
        yield session
