"""Unit tests for ``msai.services.nautilus.strategy_loader``."""

from __future__ import annotations

from pathlib import Path

import pytest

from msai.services.nautilus.strategy_loader import (
    ImportableStrategyPaths,
    resolve_importable_strategy_paths,
)

_STRATEGY_FILE = (
    Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"
)


class TestResolveImportableStrategyPaths:
    """Tests for :func:`resolve_importable_strategy_paths`."""

    def test_resolves_ema_cross_example(self) -> None:
        """The example strategy resolves to its expected module paths."""
        # Act
        resolved = resolve_importable_strategy_paths(str(_STRATEGY_FILE))

        # Assert
        assert isinstance(resolved, ImportableStrategyPaths)
        assert resolved.strategy_path.endswith(":EMACrossStrategy")
        assert resolved.config_path.endswith(":EMACrossConfig")
        assert "strategies.example.ema_cross" in resolved.strategy_path
        assert "strategies.example.config" in resolved.config_path or (
            "strategies.example.ema_cross" in resolved.config_path
        )

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """A nonexistent strategy file raises FileNotFoundError."""
        bogus = tmp_path / "does_not_exist.py"

        with pytest.raises(FileNotFoundError):
            resolve_importable_strategy_paths(str(bogus))

    def test_file_outside_strategies_dir_raises(self, tmp_path: Path) -> None:
        """A file not under a ``strategies/`` directory raises ValueError."""
        rogue = tmp_path / "rogue.py"
        rogue.write_text("class FooStrategy: pass\n")

        with pytest.raises((ValueError, FileNotFoundError)):
            resolve_importable_strategy_paths(str(rogue))
