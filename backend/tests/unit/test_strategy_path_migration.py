"""Contract tests for the strategy file-path normalization migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "f6a7b8c9d0e1_normalize_strategy_file_paths.py"
    )
    assert path.is_file(), "normalization migration must exist"
    spec = importlib.util.spec_from_file_location("strategy_path_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_strategy_file_path_is_portable_and_idempotent() -> None:
    """Changing host prefixes must never change the stored registry identity."""
    migration = _load_migration()

    cases = {
        "/app/strategies/example/ema_cross.py": "example/ema_cross.py",
        "/Users/dev/msai-v2/strategies/example/ema_cross.py": "example/ema_cross.py",
        "strategies/example/ema_cross.py": "example/ema_cross.py",
        "example/ema_cross.py": "example/ema_cross.py",
        "C:\\workspace\\strategies\\example\\ema_cross.py": "example/ema_cross.py",
        "/unrelated/location/custom.py": "/unrelated/location/custom.py",
    }

    for source, expected in cases.items():
        normalized = migration._normalize_path(source)
        assert normalized == expected
        assert migration._normalize_path(normalized) == expected


def test_normalize_strategy_file_path_preserves_nested_strategies_component() -> None:
    """A canonical nested package named strategies must never be truncated."""
    migration = _load_migration()

    assert migration._normalize_path("desk/strategies/foo.py") == "desk/strategies/foo.py"
    assert (
        migration._normalize_path(
            "/app/strategies/strategies/foo.py",
            strategy_name="strategies.foo",
        )
        == "strategies/foo.py"
    )
