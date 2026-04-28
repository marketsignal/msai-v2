"""Structural guard — prevents architectural backsliding.

The instrument registry is the SOLE source of truth for instrument
metadata. This test walks the AST of every Python file under
``backend/src/msai/`` and fails if any forbidden legacy symbol is
reintroduced — by definition, by import, by attribute access, or by
``Name`` reference.

Forbidden symbols:
    - canonical_instrument_id  (closed-universe helper, deleted)
    - InstrumentCache          (legacy model, deleted)
    - _read_cache              (cache-IO method, deleted)
    - _read_cache_bulk         (cache-IO method, deleted)
    - _write_cache             (cache-IO method, deleted)
    - _instrument_from_cache_row (cache helper, deleted)
    - _ROLL_SENSITIVE_ROOTS    (dead-code constant, deleted)

Allowed:
    - This test file itself (defines the forbidden list).

Note: Alembic migrations under ``backend/alembic/versions/`` legitimately
reference legacy symbols in their docstrings/op.drop_table calls; they
live outside ``backend/src/msai/`` and are not part of the scan scope.
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "canonical_instrument_id",
        "InstrumentCache",
        "_read_cache",
        "_read_cache_bulk",
        "_write_cache",
        "_instrument_from_cache_row",
        "_ROLL_SENSITIVE_ROOTS",
    }
)

ALLOWLIST: frozenset[str] = frozenset(
    {
        # This test file — defines the forbidden list.
        "backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py",
    }
)


def _repo_root() -> pathlib.Path:
    """Walk up from the test file to the worktree/repo root.

    Test file lives at ``backend/tests/unit/structure/<this_file>.py``,
    so the repo root is 4 levels up:
    parents[0]=structure, [1]=unit, [2]=tests, [3]=backend, [4]=root.
    """
    return pathlib.Path(__file__).parents[4]


def _scan_python_files() -> list[pathlib.Path]:
    """Return every .py under backend/src/msai/."""
    root = _repo_root()
    src = root / "backend" / "src" / "msai"
    files = sorted(src.rglob("*.py"))
    # rglob already excludes __pycache__ in normal cases, but be defensive.
    return [f for f in files if "__pycache__" not in f.parts]


def _find_forbidden_references(
    path: pathlib.Path,
) -> list[tuple[int, str, str]]:
    """Walk the AST of ``path``. Return (line, kind, symbol) for every
    forbidden reference.

    Catches: ``Name`` refs, ``Attribute`` access (``mod.<sym>``),
    ``ImportFrom`` (``from x import <sym>``), plain ``Import``,
    ``FunctionDef`` / ``ClassDef`` definitions, and ``Assign`` targets.
    Plain string literals (e.g. inside docstrings) are ``Constant``
    nodes and are NOT matched.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [(0, "syntax_error", str(exc))]

    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            hits.append((node.lineno, "name_ref", node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            hits.append((node.lineno, "attr_access", node.attr))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    hits.append((node.lineno, "import_from", alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES or alias.name.endswith(
                    tuple(f".{n}" for n in FORBIDDEN_NAMES)
                ):
                    hits.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.FunctionDef) and node.name in FORBIDDEN_NAMES:
            hits.append((node.lineno, "function_def", node.name))
        elif isinstance(node, ast.ClassDef) and node.name in FORBIDDEN_NAMES:
            hits.append((node.lineno, "class_def", node.name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in FORBIDDEN_NAMES:
                    hits.append((node.lineno, "assignment", target.id))
    return hits


def test_no_legacy_symbols_in_runtime_source() -> None:
    """Every .py under backend/src/msai/ MUST NOT reference any legacy
    symbol. Allowlist applies only to this test file."""
    root = _repo_root()
    violations: dict[str, list[tuple[int, str, str]]] = {}
    for path in _scan_python_files():
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWLIST:
            continue
        hits = _find_forbidden_references(path)
        if hits:
            violations[rel] = hits

    assert not violations, (
        f"Forbidden legacy symbols still referenced in runtime source:\n"
        f"{violations!r}\n\n"
        f"Replace any legacy reference with the registry-backed equivalent. "
        f"The instrument registry (instrument_definitions + instrument_aliases) "
        f"is the single source of truth for instrument metadata; reintroducing "
        f"a closed-universe helper or the deleted instrument_cache table puts "
        f"the live + backtest paths back out of sync."
    )


def test_alembic_migrations_are_not_scanned() -> None:
    """Sanity check: Alembic migrations live under backend/alembic/versions/
    and are NOT under backend/src/msai/, so they're outside the scan scope.
    This test asserts that fact so a future move doesn't accidentally
    pull them in."""
    root = _repo_root()
    alembic_dir = root / "backend" / "alembic" / "versions"
    src_dir = root / "backend" / "src" / "msai"
    assert not str(alembic_dir).startswith(str(src_dir)), (
        "alembic/versions accidentally moved under src/msai — the structural "
        "guard would falsely flag the migration's docstring references to "
        "instrument_cache. Move alembic back to backend/alembic."
    )


# ---------------------------------------------------------------------------
# Canary tests — positive falsification
#
# Without these, the walker could regress silently (e.g. someone refactors
# `_find_forbidden_references` and the negative test above keeps passing
# because there are no forbidden references in the source tree). We seed
# a synthetic file with a known forbidden symbol and assert the walker
# detects it.
# ---------------------------------------------------------------------------


def test_walker_detects_forbidden_name_when_present(tmp_path: pathlib.Path) -> None:
    """Feed the walker a synthetic file with a forbidden ``Name`` reference;
    the walker MUST detect it. Without this canary, future regressions in
    the walker logic would silently pass the guard above."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("x = canonical_instrument_id('AAPL')\n", encoding="utf-8")
    hits = _find_forbidden_references(bad_file)
    assert hits, "walker failed to detect forbidden Name reference"
    assert any(name == "canonical_instrument_id" for _, _, name in hits)


def test_walker_detects_forbidden_via_import_from(tmp_path: pathlib.Path) -> None:
    """Canary: ``from x import <forbidden>`` must be flagged."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("from somewhere import _ROLL_SENSITIVE_ROOTS\n", encoding="utf-8")
    hits = _find_forbidden_references(bad_file)
    assert hits and hits[0][2] == "_ROLL_SENSITIVE_ROOTS"


def test_walker_detects_class_def_with_forbidden_name(tmp_path: pathlib.Path) -> None:
    """Canary: a ``ClassDef`` with a forbidden name must be flagged."""
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("class InstrumentCache:\n    pass\n", encoding="utf-8")
    hits = _find_forbidden_references(bad_file)
    assert hits and any(name == "InstrumentCache" for _, _, name in hits)


def test_walker_detects_forbidden_via_attribute(tmp_path: pathlib.Path) -> None:
    """Canary: ``mod.canonical_instrument_id`` attribute access must be flagged.

    Without this, a regression in the ``Attribute`` branch of the walker
    would silently allow ``some_mod.canonical_instrument_id(...)`` calls
    to pass the structural guard.
    """
    bad_file = tmp_path / "bad.py"
    bad_file.write_text(
        "import some_mod\nx = some_mod.canonical_instrument_id('AAPL')\n",
        encoding="utf-8",
    )
    hits = _find_forbidden_references(bad_file)
    assert hits and any(name == "canonical_instrument_id" for _, _, name in hits)


def test_walker_detects_function_def_with_forbidden_name(tmp_path: pathlib.Path) -> None:
    """Canary: a function literally NAMED ``canonical_instrument_id`` must be flagged.

    Without this, a regression in the ``FunctionDef`` branch would let a
    reintroduction of the deleted helper pass the guard.
    """
    bad_file = tmp_path / "bad.py"
    bad_file.write_text(
        "def canonical_instrument_id(x):\n    return x\n",
        encoding="utf-8",
    )
    hits = _find_forbidden_references(bad_file)
    assert hits and any(name == "canonical_instrument_id" for _, _, name in hits)


def test_walker_detects_assign_to_forbidden_name(tmp_path: pathlib.Path) -> None:
    """Canary: ``canonical_instrument_id = lambda x: ...`` must be flagged.

    Without this, a regression in the ``Assign`` branch would let a
    rebinding of the forbidden name pass the guard.
    """
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("canonical_instrument_id = lambda x: x\n", encoding="utf-8")
    hits = _find_forbidden_references(bad_file)
    assert hits and any(name == "canonical_instrument_id" for _, _, name in hits)


def test_walker_detects_dotted_import_with_forbidden_name(tmp_path: pathlib.Path) -> None:
    """Canary: ``import some_pkg.canonical_instrument_id`` must be flagged.

    Without this, a regression in the dotted-``Import`` branch would let
    a module path ending in a forbidden name slip past the guard.
    """
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("import some_pkg.canonical_instrument_id\n", encoding="utf-8")
    hits = _find_forbidden_references(bad_file)
    assert hits and any("canonical_instrument_id" in name for _, _, name in hits)
