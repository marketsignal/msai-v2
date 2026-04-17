"""Tests for ``DatabentoClient.fetch_definition_instruments``.

The registry pipeline downloads Databento ``.definition.dbn.zst`` files
and decodes them via Nautilus's ``DatabentoDataLoader``.  These tests
verify the three contract guarantees:

1. The method exists and returns a ``list`` of Nautilus Instruments.
2. ``from_dbn_file`` is invoked with ``use_exchange_as_venue=True`` so
   CME futures emit ``venue='CME'`` (not ``GLBX``) — see the per-call
   kwarg documented in
   ``nautilus_trader/adapters/databento/loaders.py:119-128,154-156``.
3. The destination parent directory is created idempotently and a
   pre-existing download is overwritten.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from msai.services.data_sources.databento_client import DatabentoClient

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_databento_module() -> ModuleType:
    """Create a fake ``databento`` module whose ``Historical`` records calls."""
    mock_historical_instance = MagicMock()
    # ``get_range`` with ``path=`` writes to disk and returns None
    mock_historical_instance.timeseries.get_range.return_value = None

    mock_module = ModuleType("databento")
    mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

    return mock_module


def _install_fake_databento() -> ModuleType:
    """Install the fake ``databento`` module and return the installed instance."""
    mock_module = _make_mock_databento_module()
    sys.modules["databento"] = mock_module
    return mock_module


def _restore_databento(original: ModuleType | None) -> None:
    """Restore the real ``databento`` module (or remove the fake)."""
    if original is not None:
        sys.modules["databento"] = original
    else:
        sys.modules.pop("databento", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchDefinitionInstruments:
    """Tests for ``DatabentoClient.fetch_definition_instruments``."""

    async def test_calls_from_dbn_file_with_use_exchange_as_venue_true(
        self,
        tmp_path: Path,
    ) -> None:
        """``use_exchange_as_venue=True`` is the per-call kwarg per Nautilus
        ``adapters/databento/loaders.py:119-128,154-156``.
        """
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "GLBX.MDP3" / "ES" / "2024-01-01_2024-12-31.definition.dbn.zst"

        original = sys.modules.get("databento")
        _install_fake_databento()
        try:
            # Act
            with patch(
                "msai.services.data_sources.databento_client.DatabentoDataLoader",
                return_value=mock_loader,
            ):
                result = await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)

        # Assert
        mock_loader.from_dbn_file.assert_called_once()
        args, kwargs = mock_loader.from_dbn_file.call_args
        assert kwargs.get("use_exchange_as_venue") is True
        assert isinstance(result, list)

    async def test_creates_parent_directory_when_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Parent directory of ``target_path`` is created if it doesn't exist."""
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        missing_dir = tmp_path / "deeply" / "nested" / "dir"
        target = missing_dir / "ES.definition.dbn.zst"
        assert not missing_dir.exists()

        original = sys.modules.get("databento")
        _install_fake_databento()
        try:
            # Act
            with patch(
                "msai.services.data_sources.databento_client.DatabentoDataLoader",
                return_value=mock_loader,
            ):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)

        # Assert
        assert missing_dir.exists()

    async def test_removes_existing_target_before_download(
        self,
        tmp_path: Path,
    ) -> None:
        """When ``target_path`` already exists, it is unlinked before download
        so the Databento SDK can write a fresh payload.
        """
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"
        target.write_bytes(b"stale")
        assert target.exists()

        original = sys.modules.get("databento")
        mock_module = _install_fake_databento()
        try:
            # Act
            with patch(
                "msai.services.data_sources.databento_client.DatabentoDataLoader",
                return_value=mock_loader,
            ):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)

        # Assert — the stale file was removed (the mock's get_range does not
        # re-create it), and get_range was still called exactly once.
        historical_instance = mock_module.Historical.return_value  # type: ignore[attr-defined]
        historical_instance.timeseries.get_range.assert_called_once()
        assert not target.exists()

    async def test_returns_list_of_instruments(
        self,
        tmp_path: Path,
    ) -> None:
        """Decoded iterable is materialized into a list (Nautilus typing)."""
        # Arrange
        sentinel_a = MagicMock(name="InstrumentA")
        sentinel_b = MagicMock(name="InstrumentB")
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([sentinel_a, sentinel_b])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"

        original = sys.modules.get("databento")
        _install_fake_databento()
        try:
            # Act
            with patch(
                "msai.services.data_sources.databento_client.DatabentoDataLoader",
                return_value=mock_loader,
            ):
                result = await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)

        # Assert
        assert result == [sentinel_a, sentinel_b]

    async def test_passes_dataset_and_definition_schema_to_sdk(
        self,
        tmp_path: Path,
    ) -> None:
        """The Databento SDK is invoked with ``schema='definition'`` and the
        caller-supplied ``dataset`` / symbol / window arguments.
        """
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"

        original = sys.modules.get("databento")
        mock_module = _install_fake_databento()
        try:
            # Act
            with patch(
                "msai.services.data_sources.databento_client.DatabentoDataLoader",
                return_value=mock_loader,
            ):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)

        # Assert
        historical_instance = mock_module.Historical.return_value  # type: ignore[attr-defined]
        call = historical_instance.timeseries.get_range.call_args
        assert call.kwargs["dataset"] == "GLBX.MDP3"
        assert call.kwargs["schema"] == "definition"
        assert call.kwargs["symbols"] == ["ES.c.0"]
        assert call.kwargs["start"] == "2024-01-01"
        assert call.kwargs["end"] == "2024-12-31"
        assert call.kwargs["stype_in"] == "continuous"
        assert call.kwargs["stype_out"] == "instrument_id"
        assert call.kwargs["path"] == target

    async def test_raises_without_api_key(self, tmp_path: Path) -> None:
        """When no API key is configured, raise ``RuntimeError`` before any I/O."""
        # Arrange
        client = DatabentoClient(api_key="")
        target = tmp_path / "ES.definition.dbn.zst"

        # Act / Assert
        with pytest.raises(RuntimeError, match="DATABENTO_API_KEY is not configured"):
            await client.fetch_definition_instruments(
                "ES.c.0",
                "2024-01-01",
                "2024-12-31",
                dataset="GLBX.MDP3",
                target_path=target,
            )

    async def test_raises_on_databento_sdk_error(self, tmp_path: Path) -> None:
        """SDK errors are wrapped in a ``RuntimeError`` with context."""
        # Arrange
        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"

        mock_module = ModuleType("databento")
        mock_historical_instance = MagicMock()
        mock_historical_instance.timeseries.get_range.side_effect = RuntimeError("boom")
        mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

        original = sys.modules.get("databento")
        sys.modules["databento"] = mock_module
        try:
            # Act / Assert
            with pytest.raises(RuntimeError, match="Databento definition request failed"):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(original)
