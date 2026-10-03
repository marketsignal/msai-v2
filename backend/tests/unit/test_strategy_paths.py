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


def test_resolve_strategy_file_preserves_nested_package_named_strategies(
    tmp_path: Path,
) -> None:
    """A canonical nested package must not be mistaken for a legacy root prefix."""
    from msai.services import strategy_paths

    root = tmp_path / "strategies"
    nested = root / "strategies" / "nested.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("# fixture\n", encoding="utf-8")

    assert strategy_paths.resolve_strategy_file(
        "strategies/nested.py",
        strategies_root=root,
    ) == nested.resolve()


def test_resolve_strategy_file_falls_back_for_legacy_prefixed_relative_path(
    tmp_path: Path,
) -> None:
    """A pre-migration relative value remains readable when its canonical path is absent."""
    from msai.services import strategy_paths

    root = tmp_path / "strategies"
    legacy_target = root / "example" / "ema_cross.py"
    legacy_target.parent.mkdir(parents=True)
    legacy_target.write_text("# fixture\n", encoding="utf-8")

    assert strategy_paths.resolve_strategy_file(
        "strategies/example/ema_cross.py",
        strategies_root=root,
    ) == legacy_target.resolve()
