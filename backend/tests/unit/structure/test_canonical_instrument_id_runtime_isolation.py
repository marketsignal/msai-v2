"""Structural regression — council verdict constraint #4.

canonical_instrument_id() stays at `live_instrument_bootstrap.py` for
CLI seeding paths only. The runtime live-start wiring (supervisor +
live_node_config) MUST NOT reference it after Task 9 + Task 11. This
test walks the AST of each runtime file and fails if any Import,
ImportFrom, Name, or Attribute node references the forbidden name.
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN_NAME = "canonical_instrument_id"

# Paths relative to repo root (worktree root).
RUNTIME_FILES = (
    "backend/src/msai/live_supervisor/__main__.py",
    "backend/src/msai/services/nautilus/live_node_config.py",
    # live_instrument_bootstrap.py EXCLUDED — definition site + CLI
    # seeding path (still used by `msai instruments refresh`).
)


def _repo_root() -> pathlib.Path:
    """Walk up from the test file to the worktree root."""
    # test file lives at backend/tests/unit/structure/test_*.py
    # repo root is 4 levels up.
    return pathlib.Path(__file__).parents[4]


def _ast_references(path: pathlib.Path, name: str) -> list[tuple[int, str]]:
    """Return every AST location referencing ``name`` in ``path``.

    Catches: bare name references, attribute access
    (``mod.canonical_instrument_id``), import-from (``from x import
    canonical_instrument_id [as _y]``), and plain imports. Does NOT
    match string literals / docstrings.
    """
    tree = ast.parse(path.read_text())
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            hits.append((node.lineno, "name_ref"))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            hits.append((node.lineno, "attr_access"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == name:
                    hits.append(
                        (node.lineno, f"import_from:{alias.asname or alias.name}"),
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(f".{name}") or alias.name == name:
                    hits.append(
                        (node.lineno, f"import:{alias.asname or alias.name}"),
                    )
    return hits


def test_canonical_instrument_id_absent_from_runtime_paths() -> None:
    root = _repo_root()
    violations: dict[str, list[tuple[int, str]]] = {}
    for rel in RUNTIME_FILES:
        p = root / rel
        assert p.exists(), f"expected runtime file missing: {p}"
        hits = _ast_references(p, FORBIDDEN_NAME)
        if hits:
            violations[rel] = hits
    assert not violations, (
        f"canonical_instrument_id still referenced in runtime files: "
        f"{violations!r}. Council verdict constraint #4: the helper "
        "stays in CLI seeding only; the runtime live-start path must "
        "resolve via lookup_for_live."
    )


def test_canonical_instrument_id_still_exists_in_bootstrap_module() -> None:
    """Positive assertion: the definition site must still exist —
    CLI seeding at `msai instruments refresh` depends on it. This
    prevents an over-zealous refactor from removing the helper
    entirely."""
    path = _repo_root() / "backend/src/msai/services/nautilus/live_instrument_bootstrap.py"
    assert path.exists()
    tree = ast.parse(path.read_text())
    defined = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == FORBIDDEN_NAME:
            defined = True
            break
    assert defined, (
        "canonical_instrument_id definition missing from "
        "live_instrument_bootstrap.py — CLI seeding depends on it"
    )
