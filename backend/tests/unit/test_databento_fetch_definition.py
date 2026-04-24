"""Tests for ``DatabentoClient.fetch_definition_instruments``.

The registry pipeline downloads Databento ``.definition.dbn.zst`` files
and decodes them via Nautilus's ``DatabentoDataLoader``.  These tests
verify the three contract guarantees:

1. The method exists and returns a ``list`` of Nautilus Instruments.
2. ``from_dbn_file`` is invoked with ``use_exchange_as_venue=True`` so
   CME futures emit ``venue='CME'`` (not ``GLBX``) — the per-call
   kwarg is on ``DatabentoDataLoader.from_dbn_file`` in
   ``nautilus_trader/adapters/databento/loaders.py``.
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
    """Create a fake ``databento`` module whose ``Historical`` records calls.

    ``get_range(..., path=...)`` writes a placeholder byte to ``path`` so
    the subsequent atomic ``tmp_path.replace(target_path)`` succeeds. The
    real Databento SDK writes the ``.dbn.zst`` payload here.
    """

    def _write_stub(*_args: object, **kwargs: object) -> None:
        from pathlib import Path

        path = kwargs.get("path")
        if path is not None:
            Path(str(path)).write_bytes(b"")

    mock_historical_instance = MagicMock()
    mock_historical_instance.timeseries.get_range.side_effect = _write_stub

    mock_module = ModuleType("databento")
    # Mark as a package so `from databento.common.error import ...` resolves.
    mock_module.__path__ = []  # type: ignore[attr-defined]
    mock_module.Historical = MagicMock(return_value=mock_historical_instance)  # type: ignore[attr-defined]

    return mock_module


def _make_mock_databento_common_error_module() -> ModuleType:
    """Fake ``databento.common.error`` exposing the two retryable-classification
    base classes the production code now imports for tenacity retry gating."""

    class _BentoClientError(Exception):
        def __init__(self, message: str = "", http_status: int | None = None) -> None:
            super().__init__(message)
            self.http_status = http_status

    class _BentoServerError(Exception):
        def __init__(self, message: str = "", http_status: int | None = None) -> None:
            super().__init__(message)
            self.http_status = http_status

    mod = ModuleType("databento.common.error")
    mod.BentoClientError = _BentoClientError  # type: ignore[attr-defined]
    mod.BentoServerError = _BentoServerError  # type: ignore[attr-defined]
    return mod


def _install_fake_databento() -> tuple[
    ModuleType, ModuleType | None, ModuleType | None, ModuleType | None
]:
    """Install the fake ``databento`` module (and its ``common.error`` sub-module).

    Returns a 4-tuple of the installed fake plus snapshots of whatever
    was at ``databento`` / ``databento.common`` / ``databento.common.error``
    BEFORE install, so ``_restore_databento`` can put them back exactly.
    Dropping the submodules instead of restoring breaks sibling tests
    that imported ``BentoClientError`` at module-load time: the real SDK
    re-imports a NEW class and stale `isinstance` checks fail.
    """
    original_databento = sys.modules.get("databento")
    original_common = sys.modules.get("databento.common")
    original_common_error = sys.modules.get("databento.common.error")

    mock_module = _make_mock_databento_module()
    sys.modules["databento"] = mock_module
    sys.modules["databento.common"] = ModuleType("databento.common")
    sys.modules["databento.common.error"] = _make_mock_databento_common_error_module()
    return mock_module, original_databento, original_common, original_common_error


def _restore_databento(
    snapshot: tuple[ModuleType, ModuleType | None, ModuleType | None, ModuleType | None]
    | ModuleType
    | None,
) -> None:
    """Restore the real ``databento`` module graph from a snapshot tuple.

    Backwards-compatible with the legacy single-argument shape for any
    callers still passing the old ``original = sys.modules.get(...)``
    form — in that case ``databento.common*`` are simply dropped.
    """
    if isinstance(snapshot, tuple):
        _, orig_db, orig_common, orig_common_error = snapshot
        _reinstate("databento.common.error", orig_common_error)
        _reinstate("databento.common", orig_common)
        _reinstate("databento", orig_db)
        return

    # Legacy single-argument form.
    sys.modules.pop("databento.common.error", None)
    sys.modules.pop("databento.common", None)
    if snapshot is not None:
        sys.modules["databento"] = snapshot
    else:
        sys.modules.pop("databento", None)


def _reinstate(name: str, mod: ModuleType | None) -> None:
    if mod is not None:
        sys.modules[name] = mod
    else:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchDefinitionInstruments:
    """Tests for ``DatabentoClient.fetch_definition_instruments``."""

    async def test_calls_from_dbn_file_with_use_exchange_as_venue_true(
        self,
        tmp_path: Path,
    ) -> None:
        """``use_exchange_as_venue=True`` is the per-call kwarg on
        ``DatabentoDataLoader.from_dbn_file`` (see
        ``nautilus_trader/adapters/databento/loaders.py``).
        """
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "GLBX.MDP3" / "ES" / "2024-01-01_2024-12-31.definition.dbn.zst"

        _snapshot = _install_fake_databento()
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
            _restore_databento(_snapshot)

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

        _snapshot = _install_fake_databento()
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
            _restore_databento(_snapshot)

        # Assert
        assert missing_dir.exists()

    async def test_overwrites_existing_target_atomically(
        self,
        tmp_path: Path,
    ) -> None:
        """When ``target_path`` already exists and the download succeeds, the
        stale file is atomically replaced with the fresh download.

        The SDK writes to a sibling ``.tmp`` path first; a successful
        download ends with ``tmp_path.replace(target_path)``. This preserves
        the prior good file if the SDK raises (covered by the SDK-error
        test below).
        """
        # Arrange
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"
        target.write_bytes(b"stale")
        assert target.exists()

        mock_module, *_orig = _install_fake_databento()
        _snapshot = (mock_module, *_orig)
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
            _restore_databento(_snapshot)

        # Assert — the target file exists (replaced by the rename) and the
        # sibling ``.tmp`` path has been consumed by the rename.
        historical_instance = mock_module.Historical.return_value  # type: ignore[attr-defined]
        historical_instance.timeseries.get_range.assert_called_once()
        assert target.exists()
        assert not (tmp_path / "ES.definition.dbn.zst.tmp").exists()

    async def test_preserves_prior_file_when_sdk_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """Atomic-rename semantics: if the SDK raises, the prior good file
        is preserved (no ``unlink`` before the download) and the ``.tmp``
        sibling is cleaned up.

        Non-retryable ``RuntimeError`` from the SDK is wrapped in
        ``DatabentoUpstreamError`` by the tenacity-retry glue (see
        ``databento_client.py`` ``except Exception`` branch).
        """
        from msai.services.data_sources.databento_errors import DatabentoUpstreamError

        # Arrange
        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"
        target.write_bytes(b"prior-good")

        _snapshot = _install_fake_databento()
        # Replace the stub with a raising side-effect.
        sys.modules["databento"].Historical.return_value.timeseries.get_range.side_effect = (  # type: ignore[attr-defined]
            RuntimeError("boom")
        )
        try:
            # Act / Assert
            with pytest.raises(DatabentoUpstreamError, match="Databento unexpected error"):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(_snapshot)

        # Prior good file is untouched; tmp path was cleaned up.
        assert target.read_bytes() == b"prior-good"
        assert not (tmp_path / "ES.definition.dbn.zst.tmp").exists()

    async def test_returns_list_of_instruments(
        self,
        tmp_path: Path,
    ) -> None:
        """Decoded iterable is materialized into a list (Nautilus typing).

        Both sentinels share the same ``id.value`` so dedup collapses
        them to one — avoids tripping the ambiguity guard while still
        verifying the iterator-to-list materialization.
        """
        # Arrange
        sentinel_a = MagicMock(name="InstrumentA")
        sentinel_a.id.value = "ESH6.CME"
        sentinel_b = MagicMock(name="InstrumentB")
        sentinel_b.id.value = "ESH6.CME"
        mock_loader = MagicMock()
        mock_loader.from_dbn_file.return_value = iter([sentinel_a, sentinel_b])

        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"

        _snapshot = _install_fake_databento()
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
            _restore_databento(_snapshot)

        # Assert: dedup collapses the two same-id sentinels to one.
        assert len(result) == 1
        assert result[0] is sentinel_a

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

        mock_module, *_orig = _install_fake_databento()
        _snapshot = (mock_module, *_orig)
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
            _restore_databento(_snapshot)

        # Assert — the SDK receives the sibling ``.tmp`` path (atomic download),
        # NOT the final ``target`` path. A successful rename promotes .tmp to
        # ``target``; a failing SDK leaves the prior good target in place.
        historical_instance = mock_module.Historical.return_value  # type: ignore[attr-defined]
        call = historical_instance.timeseries.get_range.call_args
        assert call.kwargs["dataset"] == "GLBX.MDP3"
        assert call.kwargs["schema"] == "definition"
        assert call.kwargs["symbols"] == ["ES.c.0"]
        assert call.kwargs["start"] == "2024-01-01"
        assert call.kwargs["end"] == "2024-12-31"
        assert call.kwargs["stype_in"] == "continuous"
        assert call.kwargs["stype_out"] == "instrument_id"
        assert call.kwargs["path"] == str(target) + ".tmp"

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
        """Non-retryable SDK errors are wrapped in ``DatabentoUpstreamError``."""
        from msai.services.data_sources.databento_errors import DatabentoUpstreamError

        # Arrange
        client = DatabentoClient(api_key="test_key")
        target = tmp_path / "ES.definition.dbn.zst"

        _snapshot = _install_fake_databento()
        sys.modules["databento"].Historical.return_value.timeseries.get_range.side_effect = (  # type: ignore[attr-defined]
            RuntimeError("boom")
        )
        try:
            # Act / Assert
            with pytest.raises(DatabentoUpstreamError, match="Databento unexpected error"):
                await client.fetch_definition_instruments(
                    "ES.c.0",
                    "2024-01-01",
                    "2024-12-31",
                    dataset="GLBX.MDP3",
                    target_path=target,
                )
        finally:
            _restore_databento(_snapshot)
