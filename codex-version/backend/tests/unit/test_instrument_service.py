from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentDefinition
from msai.services.nautilus.instrument_service import (
    NautilusInstrumentService,
    _raw_symbol_from_request,
    _resolved_databento_definition,
)
from msai.services.nautilus.instruments import instrument_to_payload


@pytest.mark.asyncio
async def test_ingest_databento_definitions_persists_definition(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    instrument = TestInstrumentProvider.equity(symbol="AAPL", venue="EQUS")
    databento = AsyncMock()
    databento.fetch_definition_instruments.return_value = [instrument]
    service = NautilusInstrumentService(databento=databento)

    async with session_factory() as session:
        definitions = await service.ingest_databento_definitions(
            session,
            ["AAPL"],
            dataset="EQUS.MINI",
            start="2026-03-31",
            end="2026-04-01",
        )
        await session.commit()

    assert [definition.instrument_id for definition in definitions] == ["AAPL.EQUS"]
    databento.fetch_definition_instruments.assert_awaited_once()

    async with session_factory() as session:
        row = await session.get(InstrumentDefinition, "AAPL.EQUS")
        assert row is not None
        assert row.provider == "databento"
        assert row.raw_symbol == "AAPL"
        assert row.venue == "EQUS"
        assert row.asset_class == "stocks"
        assert row.contract_details["dataset"] == "EQUS.MINI"
        assert row.instrument_data == instrument_to_payload(instrument)

    await engine.dispose()


@pytest.mark.asyncio
async def test_canonicalize_backtest_instruments_prefers_databento_definition(
    postgres_url: str,
) -> None:
    engine = create_async_engine(postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                instrument_id="AAPL.XNAS",
                provider="interactive_brokers",
                raw_symbol="AAPL",
                venue="XNAS",
                instrument_type="Equity",
                security_type="STK",
                asset_class="stocks",
                instrument_data=instrument_to_payload(
                    TestInstrumentProvider.equity(symbol="AAPL", venue="XNAS")
                ),
                contract_details={"exchange": "NASDAQ"},
            )
        )
        session.add(
            InstrumentDefinition(
                instrument_id="AAPL.EQUS",
                provider="databento",
                raw_symbol="AAPL",
                venue="EQUS",
                instrument_type="Equity",
                security_type="STK",
                asset_class="stocks",
                instrument_data=instrument_to_payload(
                    TestInstrumentProvider.equity(symbol="AAPL", venue="EQUS")
                ),
                contract_details={"dataset": "EQUS.MINI"},
            )
        )
        await session.commit()

    service = NautilusInstrumentService(databento=AsyncMock())
    async with session_factory() as session:
        instrument_ids = await service.canonicalize_backtest_instruments(session, ["AAPL.XNAS"])
        definitions = await service.ensure_backtest_definitions(session, ["AAPL"])

    assert instrument_ids == ["AAPL.EQUS"]
    assert [definition.instrument_id for definition in definitions] == ["AAPL.EQUS"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_backtest_definitions_requires_persisted_databento_definition(
    postgres_url: str,
) -> None:
    engine = create_async_engine(postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    service = NautilusInstrumentService(databento=AsyncMock())
    async with session_factory() as session:
        with pytest.raises(ValueError, match="Run market-data ingest"):
            await service.ensure_backtest_definitions(session, ["AAPL"])

    await engine.dispose()


@pytest.mark.asyncio
async def test_canonicalize_live_instruments_does_not_block_on_explicit_ids(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    service = NautilusInstrumentService(databento=AsyncMock())

    async def _unexpected_resolve(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("live canonicalization should not resolve explicit IDs via IB")

    monkeypatch.setattr(service, "_resolve_with_nautilus", _unexpected_resolve)

    async with session_factory() as session:
        instrument_ids = await service.canonicalize_live_instruments(
            session,
            ["AAPL.XNAS", "AAPL.XNAS", "MSFT.XNAS"],
        )

    assert instrument_ids == ["AAPL.XNAS", "MSFT.XNAS"]
    await engine.dispose()


def test_raw_symbol_from_request_preserves_databento_continuous_symbols() -> None:
    assert _raw_symbol_from_request("ES.v.0") == "ES.v.0"
    assert _raw_symbol_from_request("NQ.v.1") == "NQ.v.1"
    assert _raw_symbol_from_request("AAPL.XNAS") == "AAPL"


def test_resolved_databento_definition_uses_synthetic_id_for_continuous_request() -> None:
    instrument = TestInstrumentProvider.future(symbol="ESM6", underlying="ES", venue="GLBX")

    resolved = _resolved_databento_definition(
        raw_symbol="ES.v.0",
        instruments=[instrument],
        dataset="GLBX.MDP3",
        start="2026-04-06",
        end="2026-04-07",
        definition_path=Path("/tmp/es.definition.dbn.zst"),
    )

    assert resolved.instrument_id == "ES.v.0.GLBX"
    assert resolved.raw_symbol == "ES.v.0"
    assert resolved.contract_details["requested_symbol"] == "ES.v.0"
    assert resolved.contract_details["underlying_instrument_id"] == "ESM6.GLBX"
    assert resolved.contract_details["underlying_raw_symbol"] == "ESM6"
    assert resolved.instrument_data["id"] == "ES.v.0.GLBX"
    assert resolved.instrument_data["raw_symbol"] == "ES.v.0"
    instrument = resolved.to_instrument()
    assert str(instrument.id) == "ES.v.0.GLBX"
    assert instrument.raw_symbol.value == "ES.v.0"


def test_resolved_databento_definition_expands_activation_window_for_continuous_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front = TestInstrumentProvider.future(symbol="NQM6", underlying="NQ", venue="GLBX")
    back = TestInstrumentProvider.future(symbol="NQZ5", underlying="NQ", venue="GLBX")

    def _payload(instrument):  # noqa: ANN001
        payload = instrument_to_payload(instrument)
        if instrument.raw_symbol.value == "NQM6":
            payload["activation_ns"] = 100
            payload["expiration_ns"] = 200
            payload["ts_init"] = 100
        else:
            payload["activation_ns"] = 300
            payload["expiration_ns"] = 400
            payload["ts_init"] = 400
        return payload

    monkeypatch.setattr(
        "msai.services.nautilus.instrument_service.instrument_to_payload",
        _payload,
    )

    resolved = _resolved_databento_definition(
        raw_symbol="NQ.v.0",
        instruments=[front, back],
        dataset="GLBX.MDP3",
        start="2016-04-08",
        end="2026-04-07",
        definition_path=Path("/tmp/nq.definition.dbn.zst"),
    )

    assert resolved.instrument_data["activation_ns"] == 100
    assert resolved.instrument_data["expiration_ns"] > 400


@pytest.mark.asyncio
async def test_ingest_databento_definitions_refreshes_continuous_window_when_history_expands(
    postgres_url: str,
) -> None:
    engine = create_async_engine(postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    cached_instrument = TestInstrumentProvider.future(symbol="NQH4", underlying="NQ", venue="GLBX")
    refreshed_instrument = TestInstrumentProvider.future(symbol="NQM6", underlying="NQ", venue="GLBX")

    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                instrument_id="NQ.v.0.GLBX",
                provider="databento",
                raw_symbol="NQ.v.0",
                venue="GLBX",
                instrument_type="FuturesContract",
                security_type="FUT",
                asset_class="futures",
                instrument_data=instrument_to_payload(cached_instrument) | {
                    "id": "NQ.v.0.GLBX",
                    "raw_symbol": "NQ.v.0",
                },
                contract_details={
                    "dataset": "GLBX.MDP3",
                    "definition_start": "2023-04-03",
                    "definition_end": "2023-12-31",
                    "requested_symbol": "NQ.v.0",
                },
            )
        )
        await session.commit()

    databento = AsyncMock()
    databento.fetch_definition_instruments.return_value = [refreshed_instrument]
    service = NautilusInstrumentService(databento=databento)

    async with session_factory() as session:
        definitions = await service.ingest_databento_definitions(
            session,
            ["NQ.v.0"],
            dataset="GLBX.MDP3",
            start="2016-04-08",
            end="2026-04-07",
        )
        await session.commit()

    assert definitions[0].instrument_id == "NQ.v.0.GLBX"
    databento.fetch_definition_instruments.assert_awaited_once_with(
        "NQ.v.0",
        "2016-04-08",
        "2026-04-07",
        dataset="GLBX.MDP3",
        target_path=Path(
            "/Users/pablomarin/Code/msai-v2/codex-version/data/databento/definitions/GLBX.MDP3/NQ.v.0/2016-04-08_2026-04-07.definition.dbn.zst"
        ),
    )

    await engine.dispose()
