"""Resolve a strategy file path to Nautilus ``ImportableStrategyConfig`` paths.

NautilusTrader's :class:`~nautilus_trader.trading.config.ImportableStrategyConfig`
needs two ``"module:ClassName"`` strings -- one for the ``Strategy`` subclass
and one for its matching ``StrategyConfig`` subclass -- so that the backtest
engine can instantiate both in the spawned subprocess where it actually runs.

MSAI strategies live on disk as ``.py`` files under ``strategies/`` (not
installed as a proper package), so we have to:

1. Make sure ``strategies/`` is importable by adding its *parent* directory
   to ``sys.path`` -- the spawned subprocess re-imports this module so the
   path hack runs there too.
2. Import the module and discover the ``*Strategy`` and ``*Config`` classes
   via introspection.  Callers can override the class names explicitly if
   they have more than one strategy in a single file.
3. Build the ``"strategies.example.ema_cross:EMACrossStrategy"`` style
   strings Nautilus expects and return them in a small container.

The discovery logic is deliberately forgiving -- it takes the first class
whose name ends with ``Strategy`` (case-insensitive) that is actually
defined in the target module (not imported).  That handles the common
``from foo import BaseStrategy`` case without accidentally picking up the
base class.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


@dataclass(frozen=True, slots=True)
class ImportableStrategyPaths:
    """Container for the two module-path strings Nautilus needs.

    Attributes:
        strategy_path: ``"package.module:StrategyClass"`` string used as
            :attr:`ImportableStrategyConfig.strategy_path`.
        config_path: ``"package.module:ConfigClass"`` string used as
            :attr:`ImportableStrategyConfig.config_path`.
    """

    strategy_path: str
    config_path: str


def resolve_importable_strategy_paths(
    strategy_file: str,
    *,
    strategy_class_name: str | None = None,
    config_class_name: str | None = None,
) -> ImportableStrategyPaths:
    """Resolve a strategy source file to Nautilus importable path strings.

    This is the function the backtest runner calls while building its
    ``BacktestRunConfig``.  It is safe to call from inside a spawned
    subprocess because :func:`_ensure_module_importable` inserts the
    ``strategies/`` parent directory into :data:`sys.path`.

    Args:
        strategy_file: Absolute or relative path to the strategy source file
            (e.g. ``"strategies/example/ema_cross.py"``).  Must live under
            a directory called ``strategies`` somewhere in its path.
        strategy_class_name: Optional explicit ``Strategy`` class name to
            use.  When omitted we auto-discover the first ``*Strategy``
            class defined in the target module.
        config_class_name: Optional explicit ``StrategyConfig`` class name
            to use.  When omitted we auto-discover the first ``*Config``
            class defined in the target module.

    Returns:
        An :class:`ImportableStrategyPaths` holding the two
        ``"module:ClassName"`` strings.

    Raises:
        FileNotFoundError: The strategy source file does not exist.
        ValueError: The file is not under a ``strategies/`` directory, or
            the module does not contain a ``*Strategy`` / ``*Config`` pair.
    """
    file_path = Path(strategy_file).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {file_path}")

    module_name = _ensure_module_importable(file_path)
    module = importlib.import_module(module_name)

    strategy_name = strategy_class_name or _find_strategy_class_name(module)
    cfg_name = config_class_name or _find_config_class_name(module)

    return ImportableStrategyPaths(
        strategy_path=f"{module_name}:{strategy_name}",
        config_path=f"{module_name}:{cfg_name}",
    )


def _ensure_module_importable(file_path: Path) -> str:
    """Return the dotted module name and ensure the strategies root is importable.

    Walks ``file_path`` upwards until it finds a directory called
    ``strategies``.  That directory's *parent* is inserted at the front of
    :data:`sys.path` so ``import strategies.example.ema_cross`` resolves
    correctly regardless of the current working directory.

    Args:
        file_path: Absolute path to the strategy ``.py`` file.

    Returns:
        The dotted module name (e.g. ``"strategies.example.ema_cross"``).

    Raises:
        ValueError: ``file_path`` is not located under a ``strategies``
            directory -- our convention for where strategy files live.
    """
    parts = list(file_path.parts)
    if "strategies" not in parts:
        raise ValueError(
            f"Strategy file must live under a 'strategies/' directory: {file_path}"
        )

    strategies_idx = parts.index("strategies")
    strategies_parent = Path(*parts[:strategies_idx]) if strategies_idx > 0 else Path("/")
    parent_str = str(strategies_parent)
    if parent_str not in sys.path:
        sys.path.insert(0, parent_str)

    # Re-build the dotted module name from "strategies" onwards, dropping
    # the ``.py`` extension from the final component.
    module_parts = parts[strategies_idx:-1] + [file_path.stem]
    return ".".join(module_parts)


def _find_strategy_class_name(module: ModuleType) -> str:
    """Locate a ``*Strategy`` class defined directly in ``module``.

    Only classes whose ``__module__`` matches ``module.__name__`` are
    considered -- this excludes imported base classes like Nautilus's own
    ``Strategy`` that happen to be in the namespace.

    Args:
        module: The imported strategy module.

    Returns:
        The class name (e.g. ``"EMACrossStrategy"``).

    Raises:
        ValueError: No suitable ``*Strategy`` class was found.
    """
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        name_lower = cls.__name__.lower()
        if name_lower.endswith("strategy") and name_lower != "strategy":
            return cls.__name__
    raise ValueError(f"No Strategy class found in module {module.__name__}")


def _find_config_class_name(module: ModuleType) -> str:
    """Locate a Nautilus-style ``*Config`` class in ``module``.

    Nautilus strategy configs inherit from ``StrategyConfig`` which is a
    msgspec-backed dataclass exposing a ``parse`` classmethod.  We use that
    attribute as a signature so we don't accidentally pick up unrelated
    pydantic/dataclass configs that happen to share the naming convention.

    Args:
        module: The imported strategy module.

    Returns:
        The config class name (e.g. ``"EMACrossConfig"``).

    Raises:
        ValueError: No suitable ``*Config`` class was found.
    """
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__name__.lower().endswith("config") and hasattr(cls, "parse"):
            return cls.__name__
    raise ValueError(f"No Nautilus StrategyConfig class found in module {module.__name__}")
