"""Strategy registry -- discovers and loads strategies from the local filesystem."""

import ast
import hashlib
import importlib.util
from pathlib import Path

from msai.core.logging import get_logger

log = get_logger(__name__)


class StrategyInfo:
    """Metadata about a discovered strategy."""

    def __init__(
        self,
        name: str,
        module_path: Path,
        class_name: str,
        code_hash: str,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.module_path = module_path
        self.class_name = class_name
        self.code_hash = code_hash
        self.description = description

    def __repr__(self) -> str:
        return (
            f"StrategyInfo(name={self.name!r}, class_name={self.class_name!r}, "
            f"code_hash={self.code_hash[:12]}...)"
        )


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file.

    Reads the file in 8 KiB chunks to keep memory usage low even for large
    strategy files.

    Args:
        path: Filesystem path to the file to hash.

    Returns:
        Hex-encoded SHA256 digest string (64 characters).
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def discover_strategies(strategies_dir: Path) -> list[StrategyInfo]:
    """Scan a directory for Python files containing strategy classes.

    Looks for ``.py`` files (excluding ``__init__.py``, ``config.py``, and any
    file whose name starts with ``_``) anywhere under *strategies_dir*.

    For each file the function:

    1. Computes a SHA256 code hash.
    2. Parses the AST (does **not** import the module) to find class
       definitions whose name ends with ``Strategy``.
    3. Extracts the class docstring if present.

    Args:
        strategies_dir: Root directory to scan recursively.

    Returns:
        A list of :class:`StrategyInfo` objects, one per discovered strategy
        class.  The list is empty when the directory does not exist or
        contains no matching files.
    """
    results: list[StrategyInfo] = []

    if not strategies_dir.exists():
        log.warning("strategies_dir_not_found", path=str(strategies_dir))
        return results

    for py_file in sorted(strategies_dir.rglob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "config.py":
            continue

        code_hash = compute_file_hash(py_file)
        strategy_name = py_file.stem

        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            log.error("strategy_syntax_error", path=str(py_file))
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Strategy"):
                docstring = ast.get_docstring(node)
                results.append(
                    StrategyInfo(
                        name=strategy_name,
                        module_path=py_file,
                        class_name=node.name,
                        code_hash=code_hash,
                        description=docstring,
                    )
                )

    return results


def load_strategy_class(module_path: Path, class_name: str) -> type:
    """Dynamically import and return a strategy class from a file path.

    Uses :mod:`importlib.util` to load the module from the given filesystem
    path without requiring it to be on ``sys.path``.

    Args:
        module_path: Absolute or relative path to the ``.py`` file containing
            the strategy class.
        class_name: Name of the class to retrieve from the loaded module.

    Returns:
        The strategy class object.

    Raises:
        ImportError: If the module cannot be loaded or the class is not found.
    """
    if not module_path.is_file():
        raise ImportError(f"Cannot load module from {module_path}")

    spec = importlib.util.spec_from_file_location(
        f"strategies.{module_path.stem}", module_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(f"Class {class_name} not found in {module_path}")
    return cls
