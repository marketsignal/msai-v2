from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


@dataclass(slots=True)
class ImportableStrategyPaths:
    strategy_path: str
    config_path: str


def resolve_importable_strategy_paths(
    strategy_file: str,
    strategy_class_name: str | None = None,
    config_class_name: str | None = None,
) -> ImportableStrategyPaths:
    file_path = Path(strategy_file).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {file_path}")

    module_name = _module_name_from_file(file_path)
    module = importlib.import_module(module_name)

    strategy_name = strategy_class_name or _discover_strategy_class_name(module)
    config_name = config_class_name or _discover_config_class_name(module)

    return ImportableStrategyPaths(
        strategy_path=f"{module_name}:{strategy_name}",
        config_path=f"{module_name}:{config_name}",
    )


def _module_name_from_file(file_path: Path) -> str:
    parts = list(file_path.parts)
    if "strategies" not in parts:
        raise ValueError("Strategy file must live under a 'strategies/' directory")

    strategies_index = parts.index("strategies")
    root_parts = parts[:strategies_index]
    root = Path(*root_parts) if root_parts else Path("/")
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    module_parts = parts[strategies_index:-1] + [file_path.stem]
    return ".".join(module_parts)


def _discover_strategy_class_name(module: ModuleType) -> str:
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        if cls.__name__.lower().endswith("strategy") and cls.__name__.lower() != "strategy":
            return cls.__name__
    raise ValueError(f"No Strategy class found in module {module.__name__}")


def _discover_config_class_name(module: ModuleType) -> str:
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__name__.lower().endswith("config") and hasattr(cls, "parse"):
            return cls.__name__
    raise ValueError(f"No Nautilus config class found in module {module.__name__}")
