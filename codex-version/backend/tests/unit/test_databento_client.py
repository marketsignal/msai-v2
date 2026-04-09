from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from msai.services.data_sources import databento_client as databento_module
from msai.services.data_sources.databento_client import DatabentoClient, _databento_stype_in


class _FakeRangeResponse:
    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ts_event": pd.to_datetime(["2026-04-06T14:30:00Z"]),
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1],
            }
        )


class _FakeTimeSeries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_range(self, **kwargs: object):
        self.calls.append(kwargs)
        if kwargs.get("schema") == "definition":
            path = kwargs.get("path")
            if path is not None:
                Path(str(path)).write_bytes(b"fake")
            return None
        return _FakeRangeResponse()


def test_databento_stype_in_detects_continuous_symbols() -> None:
    assert _databento_stype_in("ES.v.0") == "continuous"
    assert _databento_stype_in("NQ.v.1") == "continuous"
    assert _databento_stype_in("SPY") == "raw_symbol"
    assert _databento_stype_in("AAPL") == "raw_symbol"


def test_fetch_bars_uses_continuous_symbology_for_continuous_futures(
    monkeypatch,
) -> None:
    timeseries = _FakeTimeSeries()

    class _FakeHistorical:
        def __init__(self, *, key: str) -> None:
            self.key = key
            self.timeseries = timeseries

    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(Historical=_FakeHistorical))

    client = DatabentoClient(api_key="test-key")
    frame = asyncio.run(
        client.fetch_bars(
            "ES.v.0",
            "2026-04-06",
            "2026-04-07",
            dataset="GLBX.MDP3",
            schema="ohlcv-1m",
        )
    )

    assert not frame.empty
    assert timeseries.calls[0]["stype_in"] == "continuous"


def test_fetch_definition_instruments_uses_continuous_symbology(
    monkeypatch,
    tmp_path: Path,
) -> None:
    timeseries = _FakeTimeSeries()

    class _FakeHistorical:
        def __init__(self, *, key: str) -> None:
            self.key = key
            self.timeseries = timeseries

    class _FakeLoader:
        def from_dbn_file(self, *_: object, **__: object) -> list[object]:
            return []

    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(Historical=_FakeHistorical))
    monkeypatch.setattr(databento_module, "DatabentoDataLoader", _FakeLoader)

    client = DatabentoClient(api_key="test-key")
    instruments = asyncio.run(
        client.fetch_definition_instruments(
            "ES.v.0",
            "2026-04-06",
            "2026-04-07",
            dataset="GLBX.MDP3",
            target_path=tmp_path / "defs.dbn.zst",
        )
    )

    assert instruments == []
    assert timeseries.calls[0]["stype_in"] == "continuous"
    assert timeseries.calls[0]["stype_out"] == "instrument_id"


def test_fetch_definition_instruments_overwrites_existing_target_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    timeseries = _FakeTimeSeries()

    class _FakeHistorical:
        def __init__(self, *, key: str) -> None:
            self.key = key
            self.timeseries = timeseries

    class _FakeLoader:
        def from_dbn_file(self, *_: object, **__: object) -> list[object]:
            return []

    monkeypatch.setitem(sys.modules, "databento", SimpleNamespace(Historical=_FakeHistorical))
    monkeypatch.setattr(databento_module, "DatabentoDataLoader", _FakeLoader)

    target = tmp_path / "defs.dbn.zst"
    target.write_bytes(b"stale")

    client = DatabentoClient(api_key="test-key")
    asyncio.run(
        client.fetch_definition_instruments(
            "ES.v.0",
            "2026-04-06",
            "2026-04-07",
            dataset="GLBX.MDP3",
            target_path=target,
        )
    )

    assert target.read_bytes() == b"fake"
