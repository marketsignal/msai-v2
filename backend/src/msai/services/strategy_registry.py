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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 — runtime use on session.execute in sync_strategies_to_db
)

from msai.core.logging import get_logger
from msai.models.strategy import Strategy
from msai.services.nautilus.schema_hooks import (
    ConfigSchemaStatus,
    build_user_schema,
)
from msai.services.strategy_governance import StrategyGovernanceService

if TYPE_CHECKING:
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
            pin a backtest or live deployment to an exact code version
            AND to skip schema recompute in ``sync_strategies_to_db``
            when unchanged.
        description: The strategy class docstring, stripped.  ``None`` if
            the class has no docstring.
        config_schema: JSON Schema describing the user-defined fields of
            the strategy's ``*Config`` class. ``None`` iff
            ``config_schema_status != "ready"``. Trimmed to
            ``__annotations__`` keys — inherited ``StrategyConfig`` base
            plumbing is NOT included. Populated by
            :func:`build_user_schema`.
        default_config: Map of field name → default value (msgspec-encoded
            form) for fields that declare one. Fields without defaults
            are omitted. ``None`` iff ``config_schema_status != "ready"``.
        config_schema_status: One of ``"ready" | "unsupported" |
            "extraction_failed" | "no_config_class"`` — tells the frontend
            whether the auto-form is available for this strategy.
    """

    name: str
    module_path: Path
    strategy_class_name: str
    config_class_name: str | None
    code_hash: str
    description: str | None = None
    governance_status: str = "unchecked"
    config_schema: dict[str, Any] | None = None
    default_config: dict[str, Any] | None = None
    config_schema_status: str = "no_config_class"


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

        # Per-strategy ``try/except`` — a single broken ``*Config`` cannot
        # poison the whole list. This mirrors the import-failure
        # handling above. The status field disambiguates "extraction
        # failed" from "no config class" for the API response.
        try:
            schema, defaults, status = build_user_schema(config_cls)
        except Exception as exc:  # noqa: BLE001 — defensive; build_user_schema already catches
            log.warning(
                "strategy_schema_extraction_failed",
                path=str(py_file),
                config_class=(config_cls.__name__ if config_cls else None),
                error=str(exc),
            )
            schema, defaults = None, None
            status = ConfigSchemaStatus.EXTRACTION_FAILED
        if status is ConfigSchemaStatus.EXTRACTION_FAILED:
            log.warning(
                "strategy_schema_extraction_failed",
                path=str(py_file),
                config_class=(config_cls.__name__ if config_cls else None),
            )

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
                config_schema=schema,
                default_config=defaults,
                config_schema_status=status.value,
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
    # Mirror `_find_strategy_class`: prefer a ``*Config`` class DEFINED
    # in the target module. Without this filter, an alphabetically-late
    # user-defined config (e.g. ``ZetaConfig``) loses to any imported
    # base class (``StrategyConfig``, ``LiveExecEngineConfig``) whose
    # name happens to end with "config". Nautilus ``StrategyConfig``'s
    # base class IS hit by the old logic on strategies that
    # ``from nautilus_trader.trading.config import StrategyConfig``
    # + define a late-alphabetical subclass.
    #
    # Fallback to imported classes (the original behavior) ONLY if no
    # same-module match exists — covers the "strategies.example.config"
    # split-module case this helper's docstring flags.
    same_module: type | None = None
    imported_fallback: type | None = None
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if not (cls.__name__.lower().endswith("config") and hasattr(cls, "parse")):
            continue
        if cls.__module__ == module.__name__:
            if same_module is None:
                same_module = cls
        elif imported_fallback is None:
            imported_fallback = cls
    if same_module is not None:
        return same_module
    # The imported-fallback branch permits the ``strategies.example``
    # layout where the config class lives in a sibling ``config.py``
    # and is re-imported into the strategy module. Filter out the
    # Nautilus base ``StrategyConfig`` explicitly — it's imported by
    # ~every strategy file and has ``.parse``, so the naive fallback
    # would pick it up and emit an empty config schema.
    try:
        from nautilus_trader.trading.config import StrategyConfig as NautilusBase  # noqa: N806
    except Exception:  # pragma: no cover — Nautilus not importable
        NautilusBase = None  # type: ignore[misc,assignment]  # class-or-None sentinel: "Cannot assign to a type" [misc] + None→type[X] [assignment]  # noqa: N806
    if imported_fallback is not None and (
        NautilusBase is None or imported_fallback is not NautilusBase
    ):
        return imported_fallback
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

    cls: type[Any] | None = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(f"Class {class_name} not found in {module_path}")
    return cls


# ---------------------------------------------------------------------------
# DB sync — shared by list + detail endpoints
# ---------------------------------------------------------------------------


async def sync_strategies_to_db(
    session: AsyncSession,
    strategies_dir: Path,
    *,
    prune_missing: bool = True,
) -> list[tuple[Strategy, DiscoveredStrategy]]:
    """Scan ``strategies_dir``, upsert each discovered strategy, return rows.

    Called by both ``GET /api/v1/strategies/`` (list) and
    ``GET /api/v1/strategies/{id}`` (detail) so neither endpoint depends
    on the other's side effects (Maintainer blocking objection #2,
    2026-04-20 council).

    Memoization: if a row already exists with ``row.code_hash ==
    discovered.code_hash``, the schema/defaults/status columns are NOT
    recomputed or overwritten (Hawk blocking objection #1). When the
    hash changes (file edit, re-export, etc.), the row's schema columns
    refresh atomically with the hash bump.

    ``code_hash`` covers BOTH the strategy file and a sibling ``config.py``
    (if present) — Codex review P1 2026-04-20: a config-only edit in
    ``strategies/<pkg>/config.py`` wouldn't bump the strategy file's
    hash, so persisted schema would go stale. Combined hash fixes that.

    ``prune_missing`` deletes rows whose ``file_path`` no longer exists
    on disk (rename / delete). Orphaned rows would otherwise remain
    addressable by their stable UUID from backtest + portfolio foreign
    keys — leaking rows only users with an old bookmark would hit.

    Returns a list of ``(db_row, discovered_info)`` tuples in
    filesystem-sorted order, mirroring ``discover_strategies`` output.
    Caller is responsible for ``await session.commit()`` after calling.
    """
    discovered = discover_strategies(strategies_dir)

    existing_rows = (await session.execute(select(Strategy))).scalars().all()
    existing_by_path: dict[str, Strategy] = {row.file_path: row for row in existing_rows}

    discovered_paths = {str(info.module_path) for info in discovered}

    paired: list[tuple[Strategy, DiscoveredStrategy]] = []
    for info in discovered:
        file_path = str(info.module_path)
        # Combined hash: strategy file + sibling config.py (if any).
        # A schema/defaults change in config.py must invalidate the
        # memoized schema cache even if the strategy file is unchanged.
        combined_hash = _combined_strategy_hash(info)
        row = existing_by_path.get(file_path)
        if row is None:
            row = Strategy(
                name=info.name,
                description=info.description,
                file_path=file_path,
                strategy_class=info.strategy_class_name,
                config_class=info.config_class_name,
                config_schema=info.config_schema,
                default_config=info.default_config,
                config_schema_status=info.config_schema_status,
                code_hash=combined_hash,
            )
            session.add(row)
        else:
            row.name = info.name
            row.description = info.description
            row.strategy_class = info.strategy_class_name
            # Memoize: only recompute schema when the combined hash
            # actually changed. Avoids re-running msgspec.json.schema
            # on every /api/v1/strategies/ GET.
            if row.code_hash != combined_hash:
                row.config_class = info.config_class_name
                row.config_schema = info.config_schema
                row.default_config = info.default_config
                row.config_schema_status = info.config_schema_status
                row.code_hash = combined_hash
        paired.append((row, info))

    # Prune orphaned rows — strategy file was renamed or deleted.
    # Keeps the DB consistent with disk state.
    if prune_missing:
        for path, stale_row in existing_by_path.items():
            if path in discovered_paths:
                continue
            if not Path(path).exists():
                await session.delete(stale_row)

    return paired


def _combined_strategy_hash(info: DiscoveredStrategy) -> str:
    """Return a SHA256 that changes when the strategy file OR its
    sibling ``config.py`` changes.

    MSAI's example ``strategies/example/ema_cross.py`` imports its
    config class from ``strategies/example/config.py``. If the user
    edits only ``config.py`` (e.g. changes a default value or adds a
    field), the strategy file's own hash is unchanged, so the raw
    ``info.code_hash`` wouldn't invalidate the memoized schema cache
    in :func:`sync_strategies_to_db` — users would see stale defaults
    in the UI form until they manually touch the strategy file.

    Defense-in-depth: if a sibling ``config.py`` exists in the same
    directory, fold its content hash into the returned hash. Works
    for both the split-module layout (``config.py`` alongside the
    strategy) and the single-file layout (no ``config.py`` → returns
    ``info.code_hash`` verbatim). Doesn't recurse through arbitrary
    imports — that would balloon the hash surface — but covers the
    most-common Nautilus layout.
    """
    sibling = info.module_path.parent / "config.py"
    if not sibling.is_file() or sibling.resolve() == info.module_path.resolve():
        return info.code_hash
    combined = hashlib.sha256()
    combined.update(info.code_hash.encode("ascii"))
    combined.update(b"\x00")
    combined.update(compute_file_hash(sibling).encode("ascii"))
    return combined.hexdigest()
