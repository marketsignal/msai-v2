"""Unit tests for portable strategy-file path handling."""

from pathlib import Path


def test_resolve_strategy_file_joins_relative_path_to_configured_root(tmp_path: Path) -> None:
    """Removing the root join would send relative DB paths through the process cwd."""
    from msai.services import strategy_paths

    assert hasattr(strategy_paths, "resolve_strategy_file")
    resolved = strategy_paths.resolve_strategy_file(
        "example/ema_cross.py",
        strategies_root=tmp_path / "strategies",
    )

    assert resolved == (tmp_path / "strategies" / "example" / "ema_cross.py").resolve()


def test_resolve_strategy_file_preserves_legacy_absolute_path(tmp_path: Path) -> None:
    """Rolling upgrades can still read an absolute value before migration completes."""
    from msai.services import strategy_paths

    assert hasattr(strategy_paths, "resolve_strategy_file")
    strategy_file = tmp_path / "strategies" / "example" / "ema_cross.py"

    assert strategy_paths.resolve_strategy_file(
        str(strategy_file),
        strategies_root=tmp_path / "other-root",
    ) == strategy_file.resolve()
