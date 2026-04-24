from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from msai.services.data_sources.databento_client import (
    AmbiguousDatabentoSymbolError,
    DatabentoClient,
)
from msai.services.data_sources.databento_errors import DatabentoUpstreamError


def _make_inst(alias: str, raw_sym: str, class_name: str = "Equity") -> MagicMock:
    inst = MagicMock()
    inst.id = MagicMock()
    inst.id.value = alias
    inst.raw_symbol = MagicMock()
    inst.raw_symbol.value = raw_sym
    inst.__class__ = type(class_name, (), {"__name__": class_name})
    return inst


@pytest.mark.asyncio
async def test_multi_candidate_raises_ambiguous(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")
    cand1 = _make_inst("BRK.B.XNYS", "BRK.B")
    cand2 = _make_inst("BRK.BP.XNYS", "BRK.BP")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock()
        with patch(
            "msai.services.data_sources.databento_client.DatabentoDataLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.from_dbn_file.return_value = [cand1, cand2]
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            with pytest.raises(AmbiguousDatabentoSymbolError) as exc_info:
                await client.fetch_definition_instruments(
                    symbol="BRK.B",
                    start="2024-01-01",
                    end="2024-01-02",
                    dataset="XNYS.PILLAR",
                    target_path=target,
                )
    assert len(exc_info.value.candidates) == 2
    assert exc_info.value.candidates[0]["alias_string"] == "BRK.B.XNYS"
    assert exc_info.value.candidates[1]["alias_string"] == "BRK.BP.XNYS"
    assert exc_info.value.dataset == "XNYS.PILLAR"


@pytest.mark.asyncio
async def test_single_candidate_returns_normally(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")
    inst = _make_inst("AAPL.XNAS", "AAPL")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock()
        with patch(
            "msai.services.data_sources.databento_client.DatabentoDataLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.from_dbn_file.return_value = [inst]
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            result = await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=target,
            )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_duplicate_instrument_id_not_ambiguous(tmp_path: Path) -> None:
    """Two decoded rows with the same id.value are dedup'd — single return."""
    client = DatabentoClient(api_key="test-key")
    inst1 = _make_inst("AAPL.XNAS", "AAPL")
    inst2 = _make_inst("AAPL.XNAS", "AAPL")  # duplicate id

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock()
        with patch(
            "msai.services.data_sources.databento_client.DatabentoDataLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.from_dbn_file.return_value = [inst1, inst2]
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            result = await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=target,
            )
    assert len(result) == 1  # deduped


@pytest.mark.asyncio
async def test_exact_id_filters_multi_to_one(tmp_path: Path) -> None:
    """With exact_id, the pre-filter selects the matching candidate and
    ambiguity is NOT raised."""
    client = DatabentoClient(api_key="test-key")
    cand1 = _make_inst("BRK.B.XNYS", "BRK.B")
    cand2 = _make_inst("BRK.BP.XNYS", "BRK.BP")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock()
        with patch(
            "msai.services.data_sources.databento_client.DatabentoDataLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.from_dbn_file.return_value = [cand1, cand2]
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            result = await client.fetch_definition_instruments(
                symbol="BRK.B",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNYS.PILLAR",
                target_path=target,
                exact_id="BRK.B.XNYS",
            )
    assert len(result) == 1
    assert str(result[0].id.value) == "BRK.B.XNYS"


@pytest.mark.asyncio
async def test_exact_id_no_match_raises_upstream(tmp_path: Path) -> None:
    """exact_id that matches none of the distinct candidates raises
    DatabentoUpstreamError (not AmbiguousDatabentoSymbolError)."""
    client = DatabentoClient(api_key="test-key")
    cand1 = _make_inst("BRK.B.XNYS", "BRK.B")
    cand2 = _make_inst("BRK.BP.XNYS", "BRK.BP")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock()
        with patch(
            "msai.services.data_sources.databento_client.DatabentoDataLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.from_dbn_file.return_value = [cand1, cand2]
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            with pytest.raises(DatabentoUpstreamError) as exc_info:
                await client.fetch_definition_instruments(
                    symbol="BRK.B",
                    start="2024-01-01",
                    end="2024-01-02",
                    dataset="XNYS.PILLAR",
                    target_path=target,
                    exact_id="BRK.Q.FAKE",
                )
    assert "exact_id" in str(exc_info.value)
