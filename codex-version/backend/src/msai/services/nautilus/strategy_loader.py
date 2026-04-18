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
    _invalidate_cached_strategy_modules(module_name, file_path)
    module = importlib.import_module(module_name)

    strategy_name = strategy_class_name or _discover_strategy_class_name(module)
    config_name = config_class_name or _discover_config_class_name(module, file_path)

    return ImportableStrategyPaths(
        strategy_path=f"{module_name}:{strategy_name}",
        config_path=config_name if ":" in config_name else f"{module_name}:{config_name}",
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


def _invalidate_cached_strategy_modules(module_name: str, file_path: Path) -> None:
    parts = module_name.split(".")
    for depth in range(1, len(parts) + 1):
        candidate = ".".join(parts[:depth])
        cached = sys.modules.get(candidate)
        if cached is None:
            continue
        if depth == len(parts):
            cached_file = getattr(cached, "__file__", None)
            if cached_file is not None and Path(cached_file).resolve() != file_path:
                sys.modules.pop(candidate, None)
            continue
        expected_dir = file_path.parents[len(parts) - depth - 1]
        cached_paths = getattr(cached, "__path__", None)
        if cached_paths is None:
            continue
        try:
            normalized_paths = {str(Path(path).resolve()) for path in list(cached_paths)}
        except Exception:
            sys.modules.pop(candidate, None)
            continue
        if str(expected_dir.resolve()) not in normalized_paths:
            sys.modules.pop(candidate, None)
    importlib.invalidate_caches()


def _discover_strategy_class_name(module: ModuleType) -> str:
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        if cls.__name__.lower().endswith("strategy") and cls.__name__.lower() != "strategy":
            return cls.__name__
    raise ValueError(f"No Strategy class found in module {module.__name__}")


def _discover_config_class_name(module: ModuleType, file_path: Path) -> str:
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__name__.lower().endswith("config") and hasattr(cls, "parse"):
            return cls.__name__
    config_module_name = f"{module.__package__}.config" if module.__package__ else None
    if config_module_name:
        _invalidate_cached_strategy_modules(config_module_name, file_path.with_name("config.py"))
        try:
            config_module = importlib.import_module(config_module_name)
        except ModuleNotFoundError:
            config_module = None
        if config_module is not None:
            for _, cls in inspect.getmembers(config_module, inspect.isclass):
                if cls.__module__ != config_module.__name__:
                    continue
                if cls.__name__.lower().endswith("config") and hasattr(cls, "parse"):
                    return f"{config_module.__name__}:{cls.__name__}"
    raise ValueError(f"No Nautilus config class found in module {module.__name__}")
