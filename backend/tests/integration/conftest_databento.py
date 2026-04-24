"""Reusable fixtures for Databento-bootstrap integration tests.

``session_factory`` gives each test a fresh testcontainers-backed
Postgres with the full schema applied via ``Base.metadata.create_all``.
``mock_databento`` returns a ``DatabentoClient``-shaped mock whose
``fetch_definition_instruments`` is pre-configured for common test
symbols (AAPL → single equity, BRK.B → ambiguous, ES.n.0 → single
continuous future)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Side-effect import: registers every model against Base.metadata so
# Base.metadata.create_all covers the full schema.
from msai.models import Base

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
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_equity_instrument(raw_symbol: str, venue_mic: str):
    inst = MagicMock()
    inst.id = MagicMock()
    inst.id.value = f"{raw_symbol}.{venue_mic}"
    inst.raw_symbol = MagicMock()
    inst.raw_symbol.value = raw_symbol
    inst.__class__.__name__ = "Equity"
    return inst


@pytest.fixture
def mock_databento():
    client = MagicMock()
    client.api_key = "test-key"
    client.fetch_definition_instruments = AsyncMock()

    def _default_side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        if symbol == "AAPL":
            return [_make_equity_instrument("AAPL", "XNAS")]
        if symbol == "SPY":
            return [_make_equity_instrument("SPY", "XARC")]
        if symbol == "QQQ":
            return [_make_equity_instrument("QQQ", "XNAS")]
        if symbol in {"ES.n.0", "ES.c.0"}:
            # Continuous-futures happy-path for direct fetch_definition_instruments
            # unit tests. The _bootstrap_continuous_future branch delegates to
            # SecurityMaster.resolve_for_backtest which calls its OWN path —
            # this mock arm exists only so direct calls don't fall through to
            # the generic RuntimeError raise.
            fut = MagicMock()
            fut.id = MagicMock()
            fut.id.value = f"{symbol}.CME"
            fut.raw_symbol = MagicMock()
            fut.raw_symbol.value = symbol
            fut.__class__.__name__ = "FuturesContract"
            return [fut]
        if symbol == "BRK.B":
            from msai.services.data_sources.databento_client import (
                AmbiguousDatabentoSymbolError,
            )

            raise AmbiguousDatabentoSymbolError(
                symbol="BRK.B",
                candidates=[
                    {
                        "alias_string": "BRK.B.XNYS",
                        "raw_symbol": "BRK.B",
                        "asset_class": "equity",
                        "dataset": dataset,
                    },
                    {
                        "alias_string": "BRK.BP.XNYS",
                        "raw_symbol": "BRK.BP",
                        "asset_class": "equity",
                        "dataset": dataset,
                    },
                ],
            )
        raise RuntimeError(f"Databento definition request failed for {symbol}")

    client.fetch_definition_instruments.side_effect = _default_side_effect
    return client
