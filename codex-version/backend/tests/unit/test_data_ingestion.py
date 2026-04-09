from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from msai.core.config import settings
from msai.services.data_ingestion import (
    DataIngestionService,
    _build_ingest_targets,
    _raw_symbol_from_request,
)
from msai.services.nautilus.instrument_service import ResolvedInstrumentDefinition


class _RecordingParquetStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, pd.DataFrame]] = []

    def write_bars(self, asset_class: str, symbol: str, df: pd.DataFrame) -> list[str]:
        self.writes.append((asset_class, symbol, df.copy()))
        if df.empty:
            return []
        return [f"/tmp/{asset_class}/{symbol}/2024/01.parquet"]


class _NoopSession:
    async def commit(self) -> None:
        return None


class _NoopSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self) -> _NoopSession:
        return _NoopSession()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_ingest_historical_prefers_databento_for_equities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    ensure_catalog_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "msai.services.data_ingestion.ensure_catalog_data",
        lambda **kwargs: ensure_catalog_calls.append(kwargs) or ["AAPL.EQUS"],
    )
    store = _RecordingParquetStore()
    databento = AsyncMock()
    databento.fetch_bars.return_value = pd.DataFrame(
        {
            "timestamp": ["2024-01-02T14:30:00Z"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.5],
            "volume": [1000],
        }
    )
    polygon = AsyncMock()
    service = DataIngestionService(
        store,  # type: ignore[arg-type]
        polygon=polygon,
        databento=databento,
        session_factory=_NoopSessionFactory(),  # type: ignore[arg-type]
    )
    ingest_definitions = AsyncMock(
        return_value=[
            ResolvedInstrumentDefinition(
                instrument_id="AAPL.EQUS",
                raw_symbol="AAPL",
                venue="EQUS",
                instrument_type="Equity",
                security_type="STK",
                asset_class="stocks",
                instrument_data={"type": "Equity", "id": "AAPL.EQUS"},
                contract_details={"dataset": "EQUS.MINI"},
                provider="databento",
            )
        ]
    )
    service.instrument_service.ingest_databento_definitions = ingest_definitions  # type: ignore[method-assign]
    service.status_file = tmp_path / "ingestion_status.json"

    result = await service.ingest_historical("equities", ["AAPL"], "2024-01-01", "2024-01-02")

    databento.fetch_bars.assert_awaited_once_with(
        "AAPL",
        "2024-01-01",
        "2024-01-02",
        dataset="EQUS.MINI",
        schema="ohlcv-1m",
    )
    polygon.fetch_bars.assert_not_called()
    ingest_definitions.assert_awaited_once()
    assert result["provider"] == "databento"
    assert result["dataset"] == "EQUS.MINI"
    assert result["instrument_ids"] == ["AAPL.EQUS"]
    assert result["ingested"]["AAPL"]["instrument_id"] == "AAPL.EQUS"
    assert result["ingested"]["AAPL"]["bars"] == 1
    assert len(ensure_catalog_calls) == 1
    assert ensure_catalog_calls[0]["asset_class"] == "equities"


@pytest.mark.asyncio
async def test_ingest_historical_prefers_databento_for_futures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    monkeypatch.setattr(
        "msai.services.data_ingestion.ensure_catalog_data",
        lambda **kwargs: ["ESH4.GLBX"],
    )
    store = _RecordingParquetStore()
    databento = AsyncMock()
    databento.fetch_bars.return_value = pd.DataFrame(
        {
            "timestamp": ["2024-01-02T14:30:00Z"],
            "open": [5000.0],
            "high": [5002.0],
            "low": [4998.0],
            "close": [5001.0],
            "volume": [250],
        }
    )
    service = DataIngestionService(store, databento=databento)  # type: ignore[arg-type]
    service.status_file = tmp_path / "ingestion_status.json"
    service.instrument_service.ingest_databento_definitions = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ResolvedInstrumentDefinition(
                instrument_id="ESH4.GLBX",
                raw_symbol="ESH4",
                venue="GLBX",
                instrument_type="FuturesContract",
                security_type="FUT",
                asset_class="futures",
                instrument_data={"type": "FuturesContract", "id": "ESH4.GLBX"},
                contract_details={"dataset": "GLBX.MDP3"},
                provider="databento",
            )
        ]
    )
    service.session_factory = _NoopSessionFactory()  # type: ignore[assignment]

    result = await service.ingest_historical("futures", ["ESH4"], "2024-01-01", "2024-01-02")

    databento.fetch_bars.assert_awaited_once_with(
        "ESH4",
        "2024-01-01",
        "2024-01-02",
        dataset="GLBX.MDP3",
        schema="ohlcv-1m",
    )
    assert result["provider"] == "databento"
    assert result["dataset"] == "GLBX.MDP3"


@pytest.mark.asyncio
async def test_ingest_historical_supports_polygon_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    store = _RecordingParquetStore()
    polygon = AsyncMock()
    polygon.fetch_bars.return_value = pd.DataFrame(
        {
            "timestamp": ["2024-01-02T14:30:00Z"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.5],
            "volume": [1000],
        }
    )
    databento = AsyncMock()
    service = DataIngestionService(
        store,  # type: ignore[arg-type]
        polygon=polygon,
        databento=databento,
        session_factory=_NoopSessionFactory(),  # type: ignore[arg-type]
    )
    ingest_definitions = AsyncMock()
    service.instrument_service.ingest_databento_definitions = ingest_definitions  # type: ignore[method-assign]
    service.status_file = tmp_path / "ingestion_status.json"

    result = await service.ingest_historical(
        "equities",
        ["AAPL"],
        "2024-01-01",
        "2024-01-02",
        provider="polygon",
        schema="ohlcv-1d",
    )

    polygon.fetch_bars.assert_awaited_once_with("AAPL", "2024-01-01", "2024-01-02", timespan="day")
    databento.fetch_bars.assert_not_called()
    ingest_definitions.assert_not_called()
    assert result["provider"] == "polygon"


@pytest.mark.asyncio
async def test_ingest_historical_raises_when_all_symbols_are_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    store = _RecordingParquetStore()
    databento = AsyncMock()
    databento.fetch_bars.return_value = pd.DataFrame(
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    service = DataIngestionService(store, databento=databento)  # type: ignore[arg-type]
    service.status_file = tmp_path / "ingestion_status.json"
    service.instrument_service.ingest_databento_definitions = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ResolvedInstrumentDefinition(
                instrument_id="AAPL.EQUS",
                raw_symbol="AAPL",
                venue="EQUS",
                instrument_type="Equity",
                security_type="STK",
                asset_class="stocks",
                instrument_data={"type": "Equity", "id": "AAPL.EQUS"},
                contract_details={"dataset": "EQUS.MINI"},
                provider="databento",
            )
        ]
    )
    service.session_factory = _NoopSessionFactory()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="No historical data returned"):
        await service.ingest_historical("equities", ["AAPL"], "2024-01-01", "2024-01-02")


@pytest.mark.asyncio
async def test_ingest_historical_uses_raw_symbol_from_canonical_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    monkeypatch.setattr(
        "msai.services.data_ingestion.ensure_catalog_data",
        lambda **kwargs: ["AAPL.EQUS"],
    )
    store = _RecordingParquetStore()
    databento = AsyncMock()
    databento.fetch_bars.return_value = pd.DataFrame(
        {
            "timestamp": ["2024-01-02T14:30:00Z"],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.5],
            "volume": [1000],
        }
    )
    service = DataIngestionService(
        store,  # type: ignore[arg-type]
        databento=databento,
        session_factory=_NoopSessionFactory(),  # type: ignore[arg-type]
    )
    service.instrument_service.ingest_databento_definitions = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            ResolvedInstrumentDefinition(
                instrument_id="AAPL.EQUS",
                raw_symbol="AAPL",
                venue="EQUS",
                instrument_type="Equity",
                security_type="STK",
                asset_class="stocks",
                instrument_data={"type": "Equity", "id": "AAPL.EQUS"},
                contract_details={"dataset": "EQUS.MINI"},
                provider="databento",
            )
        ]
    )
    service.status_file = tmp_path / "ingestion_status.json"

    result = await service.ingest_historical(
        "equities",
        ["AAPL.XNAS"],
        "2024-01-01",
        "2024-01-02",
    )

    databento.fetch_bars.assert_awaited_once_with(
        "AAPL",
        "2024-01-01",
        "2024-01-02",
        dataset="EQUS.MINI",
        schema="ohlcv-1m",
    )
    assert result["symbols"] == ["AAPL"]
    assert result["instrument_ids"] == ["AAPL.EQUS"]


def test_data_ingestion_raw_symbol_preserves_databento_continuous_symbols() -> None:
    assert _raw_symbol_from_request("ES.v.0") == "ES.v.0"
    assert _raw_symbol_from_request("AAPL.XNAS") == "AAPL"


def test_build_ingest_targets_uses_resolved_tradable_contract_for_continuous_requests() -> None:
    definition = ResolvedInstrumentDefinition(
        instrument_id="ESM6.GLBX",
        raw_symbol="ESM6",
        venue="GLBX",
        instrument_type="FuturesContract",
        security_type="FUT",
        asset_class="futures",
        instrument_data={},
        contract_details={"requested_symbol": "ES.v.0"},
        provider="databento",
    )

    targets = _build_ingest_targets(["ES.v.0"], [definition])

    assert len(targets) == 1
    assert targets[0].requested_symbol == "ES.v.0"
    assert targets[0].raw_symbol == "ESM6"
    assert targets[0].instrument_id == "ESM6.GLBX"
