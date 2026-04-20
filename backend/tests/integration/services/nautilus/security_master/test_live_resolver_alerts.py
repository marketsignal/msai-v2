"""Integration test: lookup_for_live fires bounded WARN alert on
registry miss via alerting_service (offloaded to _HISTORY_EXECUTOR).

The alerting helper uses `loop.run_in_executor(executor, send_alert,
level, title, message)` which passes POSITIONAL args. Assertions
therefore check `args`, not `kwargs`.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    RegistryMissError,
    lookup_for_live,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(delete(InstrumentAlias))
        await conn.execute(delete(InstrumentDefinition))
    await engine.dispose()


async def test_registry_miss_fires_warning_alert(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`_fire_alert_bounded` offloads `alerting_service.send_alert` to
    the shared history executor via a fire-and-forget task. Verify the
    call happens with the expected level/title/message positional args.
    """
    import asyncio

    mock_service = MagicMock()
    monkeypatch.setattr(
        "msai.services.nautilus.security_master.live_resolver.alerting_service",
        mock_service,
    )

    async with session_factory() as session:
        with pytest.raises(RegistryMissError):
            await lookup_for_live(
                ["UNKNOWN"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )

    # Poll for the fire-and-forget alert task to complete. The chain is
    # asyncio.create_task → _fire_alert_bounded → run_in_executor, so
    # the mock may not have been hit the instant the raise returns.
    for _ in range(50):
        if mock_service.send_alert.called:
            break
        await asyncio.sleep(0.05)

    mock_service.send_alert.assert_called_once()
    args = mock_service.send_alert.call_args.args
    assert args[0] == "warning"  # level
    assert "registry miss" in args[1].lower()  # title
    assert "UNKNOWN" in args[2]  # message body
    assert "msai instruments refresh" in args[2]
