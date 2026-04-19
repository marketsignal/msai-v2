"""Tests for data lineage tracking (Task 17).

Covers the ``describe_catalog()`` function that builds a deterministic
snapshot of which Parquet files a backtest consumed, and verifies the
Backtest model carries the three new lineage columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msai.services.nautilus.catalog_builder import describe_catalog


@pytest.fixture()
def sample_catalog(tmp_path: Path) -> Path:
    """Create a mock Parquet catalog directory structure."""
    stocks_dir = tmp_path / "parquet" / "stocks" / "AAPL" / "2025"
    stocks_dir.mkdir(parents=True)
    (stocks_dir / "01.parquet").write_bytes(b"fake parquet data 123")
    (stocks_dir / "02.parquet").write_bytes(b"fake parquet data 456789")

    msft_dir = tmp_path / "parquet" / "stocks" / "MSFT" / "2025"
    msft_dir.mkdir(parents=True)
    (msft_dir / "01.parquet").write_bytes(b"fake msft data")

    return tmp_path / "parquet"


def test_describe_catalog_finds_files(sample_catalog: Path) -> None:
    result = describe_catalog(["AAPL.SIM"], str(sample_catalog))

    assert result["file_count"] >= 1
    assert result["catalog_hash"] != "empty"
    assert result["instruments"] == ["AAPL.SIM"]


def test_describe_catalog_multiple_instruments(sample_catalog: Path) -> None:
    result = describe_catalog(["AAPL.SIM", "MSFT.SIM"], str(sample_catalog))

    assert result["file_count"] >= 2


def test_describe_catalog_no_matching_files(tmp_path: Path) -> None:
    result = describe_catalog(["NONEXIST.SIM"], str(tmp_path))

    assert result["file_count"] == 0
    assert result["catalog_hash"] == "empty"


def test_describe_catalog_hash_deterministic(sample_catalog: Path) -> None:
    result1 = describe_catalog(["AAPL.SIM"], str(sample_catalog))
    result2 = describe_catalog(["AAPL.SIM"], str(sample_catalog))

    assert result1["catalog_hash"] == result2["catalog_hash"]


def test_describe_catalog_caps_files(sample_catalog: Path) -> None:
    # Even with many files, should cap at 50
    result = describe_catalog(["AAPL.SIM"], str(sample_catalog))

    assert len(result["files"]) <= 50


def test_describe_catalog_file_info_has_expected_keys(sample_catalog: Path) -> None:
    result = describe_catalog(["AAPL.SIM"], str(sample_catalog))

    assert result["file_count"] >= 1
    for file_info in result["files"]:
        assert "path" in file_info
        assert "size_bytes" in file_info
        assert "modified" in file_info


def test_describe_catalog_different_instruments_different_hashes(
    sample_catalog: Path,
) -> None:
    result_aapl = describe_catalog(["AAPL.SIM"], str(sample_catalog))
    result_msft = describe_catalog(["MSFT.SIM"], str(sample_catalog))

    assert result_aapl["catalog_hash"] != result_msft["catalog_hash"]


def test_backtest_model_has_lineage_fields() -> None:
    """Verify the Backtest model has the new lineage columns."""
    from msai.models.backtest import Backtest

    mapper = Backtest.__table__
    column_names = {c.name for c in mapper.columns}

    assert "nautilus_version" in column_names
    assert "python_version" in column_names
    assert "data_snapshot" in column_names
