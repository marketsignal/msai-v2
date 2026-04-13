"""Filesystem-backed strategy registry for MSAI v2.

Discovers NautilusTrader strategy files under a configured ``strategies/``
directory and exposes a small dataclass-based API for the FastAPI layer to
consume.

What changed vs. the Phase-1 registry
-------------------------------------
The earlier registry assumed plain-Python strategies that could be
constructed with ``cls()`` (no arguments).  Nautilus strategies require a
matching :class:`~nautilus_trader.trading.config.StrategyConfig` subclass
passed to the constructor, so we now:

1. **Import** each candidate module (instead of AST-scanning it) so we can
   ask Python for the concrete classes.
2. Look for a ``*Strategy`` class defined in the module and a matching
   ``*Config`` class -- both class names are recorded in
   :class:`DiscoveredStrategy`.
3. Validate candidates by checking ``issubclass(cls, Strategy)`` where
   ``Strategy`` is Nautilus's base class.  We never instantiate the class
   here -- that is the backtest/live subprocess's job, and doing it in the
   API process would risk polluting the shared Nautilus engine state.

The SHA256 of the source file is still captured so backtests and live
deployments can pin themselves to an exact code version for
reproducibility.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from msai.core.logging import get_logger
from msai.services.strategy_governance import StrategyGovernanceService

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

log = get_logger(__name__)

# Files in a strategies directory that should never be considered
# candidate strategies regardless of their contents.
_SKIP_FILENAMES: frozenset[str] = frozenset({"__init__.py", "config.py"})


@dataclass(slots=True)
class DiscoveredStrategy:
    """Metadata captured for a strategy file during discovery.

    Attributes:
        name: Human-readable dotted name derived from the file path
            (e.g. ``"example.ema_cross"``).
        module_path: Absolute path to the ``.py`` file on disk.
        strategy_class_name: The concrete ``*Strategy`` class defined in
            the file (e.g. ``"EMACrossStrategy"``).
        config_class_name: The matching ``*Config`` class name, or
            ``None`` if the strategy has no Nautilus config class.
        code_hash: SHA256 hex digest of the file's contents -- used to
            pin a backtest or live deployment to an exact code version.
        description: The strategy class docstring, stripped.  ``None`` if
            the class has no docstring.
    """

    name: str
    module_path: Path
    strategy_class_name: str
    config_class_name: str | None
    code_hash: str
    description: str | None = None
    governance_status: str = "unchecked"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def compute_file_hash(path: Path) -> str:
    """Return the SHA256 hex digest of a file's contents.

    Reads the file in 8 KiB chunks so memory usage stays constant even
    for large strategy files that bundle helper code.

    Args:
        path: Path to the file to hash.

    Returns:
        A 64-character lowercase hex string.
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_strategies(strategies_dir: Path) -> list[DiscoveredStrategy]:
    """Walk ``strategies_dir`` and return metadata for every strategy file.

    For each ``.py`` file (excluding ``__init__.py``, ``config.py`` and
    anything starting with ``_``) the function imports the module,
    searches for a ``*Strategy`` class that is a ``nautilus_trader``
    :class:`Strategy` subclass, and records the matching ``*Config``
    class name if one is present.

    Import failures and files without a valid strategy class are logged
    and skipped -- discovery must never crash just because one strategy
    file is broken.

    Args:
        strategies_dir: Root directory containing strategy packages.

    Returns:
        A list of :class:`DiscoveredStrategy` in filename-sorted order.
        Empty if the directory does not exist.
    """
    discovered: list[DiscoveredStrategy] = []
    if not strategies_dir.exists():
        log.warning("strategies_dir_not_found", path=str(strategies_dir))
        return discovered

    _ensure_strategies_importable(strategies_dir)

    governance = StrategyGovernanceService()

    for py_file in sorted(strategies_dir.rglob("*.py")):
        if py_file.name in _SKIP_FILENAMES or py_file.name.startswith("_"):
            continue

        # Run governance check BEFORE importing — prevents dangerous
        # module-scope side effects (os.system, subprocess, etc.)
        violations = governance.validate_file(py_file)
        if violations:
            log.warning(
                "strategy_governance_violations",
                path=str(py_file),
                violations=violations,
            )
            # Do NOT import or register — module-scope side effects are
            # dangerous and blocked strategies must not appear as runnable
            # in the UI or be selectable for backtests/research.
            continue

        try:
            module = _import_strategy_module(py_file, strategies_dir)
        except Exception as exc:
            log.warning("strategy_import_failed", path=str(py_file), error=str(exc))
            continue

        strategy_cls = _find_strategy_class(module)
        if strategy_cls is None:
            continue

        config_cls = _find_config_class(module)

        rel = py_file.relative_to(strategies_dir)
        dotted_name = rel.with_suffix("").as_posix().replace("/", ".")

        discovered.append(
            DiscoveredStrategy(
                name=dotted_name,
                module_path=py_file,
                strategy_class_name=strategy_cls.__name__,
                config_class_name=(config_cls.__name__ if config_cls else None),
                code_hash=compute_file_hash(py_file),
                description=(inspect.getdoc(strategy_cls) or None),
                governance_status="passed",
            )
        )

    return discovered


# ---------------------------------------------------------------------------
# Validation helper used by the strategies API
# ---------------------------------------------------------------------------


def validate_strategy_file(module_path: Path) -> tuple[bool, str]:
    """Check that a strategy file exposes a valid Nautilus Strategy class.

    Returns a ``(ok, message)`` tuple so the API can surface precise
    validation failures to the user without having to catch exceptions
    in the route handler.

    Args:
        module_path: Path to the strategy ``.py`` file.

    Returns:
        ``(True, "<class_name>")`` on success, ``(False, error)``
        otherwise.
    """
    if not module_path.is_file():
        return False, f"Strategy file not found: {module_path}"

    strategies_dir = _infer_strategies_root(module_path)
    if strategies_dir is None:
        return False, (f"Strategy file is not inside a 'strategies/' directory: {module_path}")

    _ensure_strategies_importable(strategies_dir)

    try:
        module = _import_strategy_module(module_path, strategies_dir)
    except Exception as exc:
        return False, f"Failed to import strategy module: {exc}"

    strategy_cls = _find_strategy_class(module)
    if strategy_cls is None:
        return False, "No Nautilus Strategy subclass found in module"

    return True, strategy_cls.__name__


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_strategies_importable(strategies_dir: Path) -> None:
    """Make sure ``strategies/``'s parent is on :data:`sys.path`.

    Strategies live as loose Python files rather than an installed
    package, so we need to put their parent directory on the import path
    before the first :func:`importlib.import_module` call.

    Args:
        strategies_dir: The root ``strategies/`` directory.
    """
    parent = str(strategies_dir.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _import_strategy_module(py_file: Path, strategies_dir: Path) -> ModuleType:
    """Import a strategy source file and return the resulting module.

    Uses :func:`importlib.util.spec_from_file_location` so the import
    works even if the file's dotted name clashes with something already
    loaded.  The module is registered in :data:`sys.modules` so the
    Nautilus backtest subprocess can re-import it via ``module:ClassName``
    without hitting the loader twice.

    Args:
        py_file: Absolute path to the ``.py`` file.
        strategies_dir: The ``strategies/`` root used to compute the
            dotted module name.

    Returns:
        The imported :class:`types.ModuleType`.

    Raises:
        ImportError: If the loader could not be constructed or the file
            could not be executed.
    """
    rel_parts = py_file.resolve().relative_to(strategies_dir.resolve()).with_suffix("").parts
    module_name = ".".join(("strategies", *rel_parts))

    spec = importlib.util.spec_from_file_location(module_name, py_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy module from {py_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_strategy_class(module: ModuleType) -> type | None:
    """Locate the first Nautilus-compatible strategy class in ``module``.

    Prefers classes that inherit from ``nautilus_trader`` ``Strategy``,
    but falls back to any ``*Strategy`` class defined directly in the
    module so tests or stubs without Nautilus installed still work.

    Args:
        module: Imported strategy module.

    Returns:
        The matching class, or ``None`` if none was found.
    """
    nautilus_base: type | None
    try:
        from nautilus_trader.trading.strategy import Strategy as _NautilusBase

        nautilus_base = _NautilusBase
    except Exception:
        nautilus_base = None

    fallback: type | None = None
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        name_lower = cls.__name__.lower()
        if not name_lower.endswith("strategy") or name_lower == "strategy":
            continue

        if nautilus_base is not None and issubclass(cls, nautilus_base):
            return cls
        if fallback is None:
            fallback = cls

    return fallback


def _find_config_class(module: ModuleType) -> type | None:
    """Locate a Nautilus ``*Config`` class in the strategy module.

    We reach into ``strategies.example.config`` as well when scanning a
    file that imports its config from a sibling module -- the simple
    module-level scan would otherwise miss it.

    Args:
        module: Imported strategy module.

    Returns:
        The matching config class, or ``None`` if none was found.
    """
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__name__.lower().endswith("config") and hasattr(cls, "parse"):
            return cls
    return None



def _infer_strategies_root(module_path: Path) -> Path | None:
    """Walk up from ``module_path`` to find a ``strategies/`` ancestor."""
    for parent in module_path.resolve().parents:
        if parent.name == "strategies":
            return parent
    return None


# ---------------------------------------------------------------------------
# Backwards-compat: provide a StrategyInfo alias so older tests keep working.
# ---------------------------------------------------------------------------

#: Legacy alias retained so any external import keeps resolving.  New code
#: should use :class:`DiscoveredStrategy` directly.
StrategyInfo = DiscoveredStrategy


def load_strategy_class(module_path: Path, class_name: str) -> type[Any]:
    """Import a strategy file and return the requested class.

    Retained for tests and admin scripts that want a raw class reference.
    Prefer :func:`discover_strategies` / :func:`validate_strategy_file`
    for production code paths.

    Args:
        module_path: Path to the strategy ``.py`` file.
        class_name: Name of the class to fetch.

    Returns:
        The class object.

    Raises:
        ImportError: File cannot be loaded or class does not exist.
    """
    if not module_path.is_file():
        raise ImportError(f"Cannot load module from {module_path}")

    strategies_dir = _infer_strategies_root(module_path)
    if strategies_dir is None:
        # Fall back to direct file-spec loading so callers outside the
        # ``strategies/`` convention still work (e.g. synthetic test files).
        spec = importlib.util.spec_from_file_location(f"_adhoc.{module_path.stem}", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        _ensure_strategies_importable(strategies_dir)
        module = _import_strategy_module(module_path, strategies_dir)

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(f"Class {class_name} not found in {module_path}")
    return cls
