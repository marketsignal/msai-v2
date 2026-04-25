from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.data_ingestion import IngestResult, ingest_symbols


@pytest.mark.asyncio
async def test_ingest_symbols_returns_structured_result(monkeypatch):
    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "asset_class": "stocks",
            "provider": "databento",
            "dataset": "XNAS.ITCH",
            "schema": "ohlcv-1m",
            "requested_symbols": ["SPY"],
            "symbols": ["SPY"],
            "start": "2024-01-01",
            "end": "2024-12-31",
            "ingested": {
                "SPY": {
                    "requested_symbol": "SPY",
                    "raw_symbol": "SPY",
                    "instrument_id": "SPY.XNAS",
                    "bars": 418,
                    "first_timestamp": "2024-01-02T14:31:00Z",
                    "last_timestamp": "2024-12-31T21:00:00Z",
                    "duplicates_dropped": 0,
                }
            },
            "empty_symbols": [],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await ingest_symbols("stocks", ["SPY"], "2024-01-01", "2024-12-31")
    assert isinstance(result, IngestResult)
    assert result.bars_written == 418
    assert result.symbols_covered == ["SPY"]
    assert result.empty_symbols == []


@pytest.mark.asyncio
async def test_ingest_symbols_carries_empty_symbols_when_zero_bars(monkeypatch):
    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "ingested": {
                "SPY": {"raw_symbol": "SPY", "bars": 0},
                "AAPL": {"raw_symbol": "AAPL", "bars": 58},
            },
            "empty_symbols": ["SPY"],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await ingest_symbols("stocks", ["SPY", "AAPL"], "2024-01-01", "2024-01-31")
    assert result.bars_written == 58
    assert result.symbols_covered == ["AAPL"]
    assert result.empty_symbols == ["SPY"]


@pytest.mark.asyncio
async def test_run_ingest_shim_returns_none(monkeypatch):
    from msai.services.data_ingestion import run_ingest

    fake_service = MagicMock()
    fake_service.ingest_historical = AsyncMock(
        return_value={
            "ingested": {"SPY": {"raw_symbol": "SPY", "bars": 1}},
            "empty_symbols": [],
        }
    )
    monkeypatch.setattr(
        "msai.services.data_ingestion._build_default_service",
        lambda: fake_service,
    )
    result = await run_ingest({}, "stocks", ["SPY"], "2024-01-01", "2024-12-31")
    assert result is None


@pytest.mark.asyncio
async def test_run_ingest_shim_delegates_to_ingest_symbols(monkeypatch):
    """The arq shim MUST delegate to ingest_symbols with identical args."""
    calls: list[dict[str, object]] = []

    async def fake_ingest_symbols(
        asset_class_ingest: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str = "auto",
        dataset: str | None = None,
        schema: str | None = None,
    ) -> IngestResult:
        calls.append(
            {
                "asset_class": asset_class_ingest,
                "symbols": symbols,
                "start": start,
                "end": end,
                "provider": provider,
                "dataset": dataset,
                "schema": schema,
            }
        )
        return IngestResult(
            bars_written=0,
            symbols_covered=[],
            empty_symbols=list(symbols),
            coverage_status="none",
        )

    monkeypatch.setattr("msai.services.data_ingestion.ingest_symbols", fake_ingest_symbols)

    from msai.services.data_ingestion import run_ingest

    result = await run_ingest({}, "equity", ["SPY"], "2024-01-01", "2024-12-31")
    assert result is None
    assert calls == [
        {
            "asset_class": "equity",
            "symbols": ["SPY"],
            "start": "2024-01-01",
            "end": "2024-12-31",
            "provider": "auto",
            "dataset": None,
            "schema": None,
        }
    ]
