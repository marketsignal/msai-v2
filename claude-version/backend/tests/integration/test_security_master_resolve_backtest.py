"""Integration tests for :meth:`SecurityMaster.resolve_for_backtest` (Task 9).

Exercises the four paths of the registry-backed backtest resolve:

- Empty registry + bare ticker → ``DatabentoDefinitionMissing`` (operator
  hasn't run ``msai instruments refresh`` yet).
- ``.Z.N`` continuous pattern with no ``DatabentoClient`` configured →
  ``ValueError`` (cold-miss requires the Databento fetch).
- ``.Z.N`` happy path — mocked ``DatabentoClient.fetch_definition_instruments``
  + mocked ``resolved_databento_definition`` → synthesis path upserts a
  definition + active alias via the shared
  :meth:`SecurityMaster._upsert_definition_and_alias` helper and returns
  the synthetic Nautilus ``InstrumentId`` string.

Follows the per-module ``session_factory`` + ``isolated_postgres_url``
fixture pattern from ``test_security_master_resolve_live.py`` /
``test_instrument_registry.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.continuous_futures import (
    ResolvedInstrumentDefinition,
)
from msai.services.nautilus.security_master.service import (
    DatabentoDefinitionMissing,
    SecurityMaster,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest.mark.asyncio
async def test_resolve_for_backtest_raises_on_empty_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty registry + bare ticker → fail-loud DatabentoDefinitionMissing.

    Backtests must NOT call IB on cold-miss — the operator is expected to
    run ``msai instruments refresh`` first. The exception carries the
    operator hint so the failure is actionable.
    """
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(DatabentoDefinitionMissing) as exc:
            await sm.resolve_for_backtest(["AAPL"])

        assert "AAPL" in str(exc.value)
        assert "msai instruments refresh" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_for_backtest_continuous_requires_databento_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``.Z.N`` cold-miss + ``databento_client=None`` → ValueError.

    The continuous-futures synthesis path needs a live
    :class:`DatabentoClient` to download the ``.definition.dbn.zst`` file.
    Constructing the :class:`SecurityMaster` with ``databento_client=None``
    and requesting a ``.Z.N`` symbol must fail with a clear error rather
    than silently dereferencing ``None``.
    """
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(ValueError, match="DatabentoClient required"):
            await sm.resolve_for_backtest(
                ["ES.Z.0"], start="2024-01-01", end="2024-03-01"
            )


@pytest.mark.asyncio
async def test_resolve_for_backtest_continuous_happy_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``.Z.N`` cold-miss happy path — synthesis writes definition + alias.

    Mocks ``DatabentoClient.fetch_definition_instruments`` (returns a sentinel
    list; we don't care about the actual Nautilus objects because
    ``resolved_databento_definition`` is also mocked) and patches the
    synthesis helper at the *call site* inside ``service.py`` so the
    :meth:`_resolve_databento_continuous` path lines up with the mocked
    return value.

    Asserts:
      1. The returned list contains the synthetic ``{raw}.{venue}`` id.
      2. An :class:`InstrumentDefinition` row is inserted with
         provider=``databento``.
      3. An :class:`InstrumentAlias` row with venue_format
         ``databento_continuous`` is inserted.
      4. The Databento client was called once with the expected args.
    """
    async with session_factory() as session:
        fake_instruments = [MagicMock()]  # sentinel — opaque to the SUT
        mock_databento = MagicMock()
        mock_databento.fetch_definition_instruments = AsyncMock(
            return_value=fake_instruments
        )

        sm = SecurityMaster(db=session, databento_client=mock_databento)

        resolved = ResolvedInstrumentDefinition(
            instrument_id="ES.Z.0.CME",
            raw_symbol="ES.Z.0",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="databento",
            contract_details={
                "dataset": "GLBX.MDP3",
                "schema": "definition",
                "definition_start": "2024-01-01",
                "definition_end": "2024-03-01",
                "definition_file_path": "(mocked)",
                "requested_symbol": "ES.Z.0",
                "underlying_instrument_id": "ESH4.CME",
                "underlying_raw_symbol": "ESH4",
            },
        )

        with patch(
            "msai.services.nautilus.security_master.service"
            ".resolved_databento_definition",
            return_value=resolved,
        ) as mock_resolved:
            # Act
            ids = await sm.resolve_for_backtest(
                ["ES.Z.0"], start="2024-01-01", end="2024-03-01"
            )

        # Assert — return value
        assert ids == ["ES.Z.0.CME"]

        # Assert — Databento client was invoked once
        mock_databento.fetch_definition_instruments.assert_awaited_once()
        call = mock_databento.fetch_definition_instruments.await_args
        assert call.args[0] == "ES.Z.0"
        assert call.kwargs["dataset"] == "GLBX.MDP3"

        # Assert — synthesis helper was invoked once with our fake instruments
        mock_resolved.assert_called_once()
        assert mock_resolved.call_args.kwargs["instruments"] is fake_instruments

        # Assert — definition row was upserted
        from sqlalchemy import select

        idef_row = (
            await session.execute(
                select(InstrumentDefinition).where(
                    InstrumentDefinition.raw_symbol == "ES.Z.0",
                    InstrumentDefinition.provider == "databento",
                )
            )
        ).scalar_one()
        assert idef_row.listing_venue == "CME"
        assert idef_row.routing_venue == "CME"
        assert idef_row.asset_class == "futures"

        # Assert — alias row uses venue_format=databento_continuous
        alias_row = (
            await session.execute(
                select(InstrumentAlias).where(
                    InstrumentAlias.alias_string == "ES.Z.0.CME",
                    InstrumentAlias.provider == "databento",
                )
            )
        ).scalar_one()
        assert alias_row.venue_format == "databento_continuous"
        assert alias_row.effective_to is None
