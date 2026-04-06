import sys
from pathlib import Path

from msai.services.strategy_registry import StrategyRegistry


def test_registry_discovers_example_strategy() -> None:
    root = (Path(__file__).resolve().parents[3] / "strategies").resolve()
    registry = StrategyRegistry(root)
    discovered = registry.discover()
    assert any(item.strategy_class == "EMACrossStrategy" for item in discovered)


def test_registry_discovers_strategy_without_existing_sys_path_entry() -> None:
    root = (Path(__file__).resolve().parents[3] / "strategies").resolve()
    root_parent = str(root.parent)
    while root_parent in sys.path:
        sys.path.remove(root_parent)

    registry = StrategyRegistry(root)
    discovered = registry.discover()

    assert any(item.strategy_class == "EMACrossStrategy" for item in discovered)
