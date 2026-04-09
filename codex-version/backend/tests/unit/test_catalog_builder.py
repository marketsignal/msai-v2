from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from nautilus_trader.model.identifiers import InstrumentId

from msai.services.nautilus.catalog_builder import (
    _asset_class_aliases,
    _catalog_covers_raw_months,
    _catalog_instrument_matches_definition,
    _month_range,
    build_catalog_for_instrument,
)
from msai.services.nautilus.instrument_service import ResolvedInstrumentDefinition


def test_asset_class_aliases_cover_equities_and_stocks() -> None:
    assert _asset_class_aliases("stocks") == ["equities"]
    assert _asset_class_aliases("equities") == ["stocks"]
    assert _asset_class_aliases("futures") == []


def test_catalog_builder_uses_equities_alias_for_stock_definition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "parquet"
    symbol_dir = raw_root / "equities" / "QQQ" / "2026"
    symbol_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "timestamp": "2026-04-07T13:30:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1000.0,
            },
            {
                "timestamp": "2026-04-07T13:31:00Z",
                "open": 100.5,
                "high": 101.5,
                "low": 100.0,
                "close": 101.0,
                "volume": 1200.0,
            },
        ]
    ).to_parquet(symbol_dir / "04.parquet", index=False)

    definition = ResolvedInstrumentDefinition(
        instrument_id="QQQ.EQUS",
        raw_symbol="QQQ",
        venue="EQUS",
        instrument_type="Equity",
        security_type="STK",
        asset_class="stocks",
        instrument_data={
            "id": "QQQ.EQUS",
            "raw_symbol": "QQQ",
            "type": "Equity",
            "currency": "USD",
            "price_precision": 2,
            "size_precision": 0,
            "lot_size": "1",
            "multiplier": "1",
            "isin": None,
            "ts_event": 0,
            "ts_init": 0,
            "info": {},
        },
        contract_details=None,
        provider="databento",
    )

    class _FakeCatalog:
        def __init__(self, path: str) -> None:
            self.path = path

        def instruments(self) -> list[object]:
            return []

        def write_data(self, data: list[object]) -> None:
            return None

    class _FakeWrangler:
        def __init__(self, *, bar_type, instrument) -> None:  # noqa: ANN001
            self.bar_type = bar_type
            self.instrument = instrument

        def process(self, frame):  # noqa: ANN001
            return [object()] * len(frame)

    monkeypatch.setattr(
        ResolvedInstrumentDefinition,
        "to_instrument",
        lambda self: SimpleNamespace(id=InstrumentId.from_str("QQQ.EQUS")),
    )
    monkeypatch.setattr(
        "msai.services.nautilus.catalog_builder.ParquetDataCatalog",
        _FakeCatalog,
    )
    monkeypatch.setattr(
        "msai.services.nautilus.catalog_builder.BarDataWrangler",
        _FakeWrangler,
    )

    catalog_root = tmp_path / "catalog"
    instrument_id = build_catalog_for_instrument(definition, raw_root, catalog_root)
    assert instrument_id == "QQQ.EQUS"


def test_month_range_covers_inclusive_months() -> None:
    months = _month_range(
        start=pd.Timestamp("2025-04-07T00:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2025-07-08T00:00:00Z").to_pydatetime(),
    )

    assert months == {(2025, 4), (2025, 5), (2025, 6), (2025, 7)}


def test_catalog_covers_raw_months_detects_incomplete_history(tmp_path: Path) -> None:
    raw_symbol_dir = tmp_path / "raw" / "equities" / "QQQ"
    for month in ("04", "05", "06", "07"):
        target_dir = raw_symbol_dir / "2025"
        target_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "timestamp": "2025-04-07T13:30:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1000.0,
                }
            ]
        ).to_parquet(target_dir / f"{month}.parquet", index=False)

    bar_dir = tmp_path / "catalog" / "data" / "bar" / "QQQ.EQUS-1-MINUTE-LAST-EXTERNAL"
    bar_dir.mkdir(parents=True, exist_ok=True)
    (bar_dir / "2025-10-01T08-00-00-000000000Z_2025-10-31T23-59-00-000000000Z.parquet").touch()

    assert _catalog_covers_raw_months(
        instrument_id="QQQ.EQUS",
        parquet_files=sorted(raw_symbol_dir.rglob("*.parquet")),
        catalog_root=tmp_path / "catalog",
    ) is False


def test_catalog_covers_raw_months_accepts_complete_history(tmp_path: Path) -> None:
    raw_symbol_dir = tmp_path / "raw" / "equities" / "QQQ"
    for month in ("04", "05", "06", "07"):
        target_dir = raw_symbol_dir / "2025"
        target_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "timestamp": "2025-04-07T13:30:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1000.0,
                }
            ]
        ).to_parquet(target_dir / f"{month}.parquet", index=False)

    bar_dir = tmp_path / "catalog" / "data" / "bar" / "QQQ.EQUS-1-MINUTE-LAST-EXTERNAL"
    bar_dir.mkdir(parents=True, exist_ok=True)
    (bar_dir / "2025-04-01T08-00-00-000000000Z_2025-07-31T23-59-00-000000000Z.parquet").touch()

    assert _catalog_covers_raw_months(
        instrument_id="QQQ.EQUS",
        parquet_files=sorted(raw_symbol_dir.rglob("*.parquet")),
        catalog_root=tmp_path / "catalog",
    ) is True


def test_catalog_instrument_match_detects_stale_activation_window(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "catalog" / "data" / "futures_contract" / "NQ.v.0.GLBX"
    instrument_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "id": "NQ.v.0.GLBX",
                "raw_symbol": "NQ.v.0",
                "activation_ns": 1671201000000000000,
                "expiration_ns": 1710509400000000000,
                "price_increment": "0.25",
                "multiplier": "20",
            }
        ]
    ).to_parquet(
        instrument_dir / "2023-12-29T00-00-00-000000000Z_2023-12-29T00-00-00-000000000Z.parquet",
        index=False,
    )

    definition = ResolvedInstrumentDefinition(
        instrument_id="NQ.v.0.GLBX",
        raw_symbol="NQ.v.0",
        venue="GLBX",
        instrument_type="FuturesContract",
        security_type="FUT",
        asset_class="futures",
        instrument_data={
            "id": "NQ.v.0.GLBX",
            "raw_symbol": "NQ.v.0",
            "activation_ns": 1460102400000000000,
            "expiration_ns": 1775606400000000000,
            "price_increment": "0.25",
            "multiplier": "20",
        },
        contract_details=None,
        provider="databento",
    )

    assert (
        _catalog_instrument_matches_definition(
            definition=definition,
            catalog_root=tmp_path / "catalog",
        )
        is False
    )


def test_catalog_builder_rewrites_only_instrument_when_bars_are_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "parquet"
    symbol_dir = raw_root / "futures" / "NQ.v.0" / "2026"
    symbol_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "timestamp": "2026-04-07T13:30:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 1000.0,
            }
        ]
    ).to_parquet(symbol_dir / "04.parquet", index=False)

    instrument_dir = tmp_path / "catalog" / "data" / "futures_contract" / "NQ.v.0.GLBX"
    instrument_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "id": "NQ.v.0.GLBX",
                "raw_symbol": "NQ.v.0",
                "activation_ns": 1,
                "expiration_ns": 2,
                "price_increment": "0.25",
                "multiplier": "20",
            }
        ]
    ).to_parquet(
        instrument_dir / "2023-12-29T00-00-00-000000000Z_2023-12-29T00-00-00-000000000Z.parquet",
        index=False,
    )
    bar_dir = tmp_path / "catalog" / "data" / "bar" / "NQ.v.0.GLBX-1-MINUTE-LAST-EXTERNAL"
    bar_dir.mkdir(parents=True, exist_ok=True)
    (bar_dir / "2026-04-01T08-00-00-000000000Z_2026-04-30T23-59-00-000000000Z.parquet").touch()

    definition = ResolvedInstrumentDefinition(
        instrument_id="NQ.v.0.GLBX",
        raw_symbol="NQ.v.0",
        venue="GLBX",
        instrument_type="FuturesContract",
        security_type="FUT",
        asset_class="futures",
        instrument_data={
            "id": "NQ.v.0.GLBX",
            "raw_symbol": "NQ.v.0",
            "activation_ns": 10,
            "expiration_ns": 20,
            "price_increment": "0.25",
            "multiplier": "20",
        },
        contract_details=None,
        provider="databento",
    )

    writes: list[list[object]] = []

    class _FakeCatalog:
        def __init__(self, path: str) -> None:
            self.path = path

        def write_data(self, data: list[object]) -> None:
            writes.append(data)

    monkeypatch.setattr(
        ResolvedInstrumentDefinition,
        "to_instrument",
        lambda self: SimpleNamespace(id=InstrumentId.from_str("NQ.v.0.GLBX")),
    )
    monkeypatch.setattr(
        "msai.services.nautilus.catalog_builder.ParquetDataCatalog",
        _FakeCatalog,
    )

    instrument_id = build_catalog_for_instrument(definition, raw_root, tmp_path / "catalog")

    assert instrument_id == "NQ.v.0.GLBX"
    assert len(writes) == 1
    assert bar_dir.exists()
