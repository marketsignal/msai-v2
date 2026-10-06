"""Unit tests for the strategy registry service."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from msai.services.strategy_registry import (
    DiscoveredStrategy,
    compute_file_hash,
    discover_strategies,
    load_strategy_class,
    validate_strategy_file,
)

if TYPE_CHECKING:
    from msai.models.strategy import Strategy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies" / "example"


@pytest.fixture()
def example_strategies_dir() -> Path:
    """Return the path to the example strategies directory."""
    return STRATEGIES_DIR


@pytest.fixture()
def empty_strategies_dir(tmp_path: Path) -> Path:
    """Return a temporary empty directory."""
    d = tmp_path / "empty_strategies"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Tests: discover_strategies
# ---------------------------------------------------------------------------


class TestDiscoverStrategies:
    """Tests for :func:`discover_strategies`."""

    def test_discover_strategies_finds_example(self, example_strategies_dir: Path) -> None:
        """discover_strategies finds EMACrossStrategy in the example directory."""
        # Act
        results = discover_strategies(example_strategies_dir)

        # Assert
        assert len(results) >= 1
        class_names = [r.strategy_class_name for r in results]
        assert "EMACrossStrategy" in class_names

        ema = next(r for r in results if r.strategy_class_name == "EMACrossStrategy")
        assert isinstance(ema, DiscoveredStrategy)
        assert ema.module_path.name == "ema_cross.py"
        assert ema.config_class_name == "EMACrossConfig"

    def test_discover_strategies_returns_code_hash(self, example_strategies_dir: Path) -> None:
        """Discovered strategies include a 64-character hex SHA256 hash."""
        # Act
        results = discover_strategies(example_strategies_dir)

        # Assert
        assert len(results) >= 1
        for info in results:
            assert len(info.code_hash) == 64
            int(info.code_hash, 16)  # must be valid hex

    def test_discover_strategies_empty_dir(self, empty_strategies_dir: Path) -> None:
        """An empty directory returns an empty list."""
        results = discover_strategies(empty_strategies_dir)

        assert results == []

    def test_discover_strategies_nonexistent_dir(self, tmp_path: Path) -> None:
        """A nonexistent directory returns an empty list without raising."""
        results = discover_strategies(tmp_path / "nonexistent")

        assert results == []

    def test_discovery_skips_literal_dot_path_components(self, tmp_path: Path) -> None:
        """Dotted names remain reversible to exactly one root-relative file."""
        strategy_file = tmp_path / "foo.bar.py"
        strategy_file.write_text(
            "from nautilus_trader.trading.strategy import Strategy\n"
            "class LiteralDotStrategy(Strategy):\n"
            "    pass\n",
            encoding="utf-8",
        )

        assert discover_strategies(tmp_path) == []


# ---------------------------------------------------------------------------
# Tests: compute_file_hash
# ---------------------------------------------------------------------------


class TestComputeFileHash:
    """Tests for :func:`compute_file_hash`."""

    def test_compute_file_hash_deterministic(self, example_strategies_dir: Path) -> None:
        """Hashing the same file twice produces the same result."""
        path = example_strategies_dir / "ema_cross.py"
        hash1 = compute_file_hash(path)
        hash2 = compute_file_hash(path)

        assert hash1 == hash2
        assert len(hash1) == 64

    def test_compute_file_hash_different_content(self, tmp_path: Path) -> None:
        """Different file content produces different hashes."""
        file_a = tmp_path / "a.py"
        file_b = tmp_path / "b.py"
        file_a.write_text("class AStrategy: pass\n")
        file_b.write_text("class BStrategy: pass\n")

        assert compute_file_hash(file_a) != compute_file_hash(file_b)


# ---------------------------------------------------------------------------
# Tests: validate_strategy_file
# ---------------------------------------------------------------------------


class TestValidateStrategyFile:
    """Tests for :func:`validate_strategy_file`."""

    def test_validate_example_strategy_passes(self, example_strategies_dir: Path) -> None:
        """The shipped example strategy validates successfully."""
        ok, message = validate_strategy_file(example_strategies_dir / "ema_cross.py")

        assert ok is True
        assert message == "EMACrossStrategy"

    def test_validate_missing_file_returns_error(self, tmp_path: Path) -> None:
        """A missing file returns ok=False with a clear message."""
        ok, message = validate_strategy_file(tmp_path / "nope.py")

        assert ok is False
        assert "not found" in message.lower()


# ---------------------------------------------------------------------------
# Tests: node-side halt-gate supported-API lint (PR 2 T2 / F6)
# ---------------------------------------------------------------------------


class TestSupportedSubmitApiLint:
    """Tests for the ``_lint_supported_submit_api`` defense-in-depth check.

    The runtime ``on_post_build`` MRO gate is the real guarantee; this lint
    rejects the OBVIOUS bypasses of the gated submit API at validation time so
    an operator gets a clear error before deploying a strategy that reaches the
    order path via Nautilus internals.
    """

    def _make_strategies_file(self, tmp_path: Path, body: str) -> Path:
        strategies_dir = tmp_path / "strategies"
        strategies_dir.mkdir()
        f = strategies_dir / "candidate.py"
        f.write_text(body, encoding="utf-8")
        return f

    def test_clean_strategy_passes_the_lint(self, tmp_path: Path) -> None:
        """A strategy that only uses the supported submit API is not rejected by
        the lint (it then proceeds to import + class discovery)."""
        from msai.services.strategy_registry import _lint_supported_submit_api

        f = self._make_strategies_file(
            tmp_path,
            "def on_bar(self, bar):\n"
            "    order = self.order_factory.market(...)\n"
            "    self.submit_order(order)\n",
        )
        ok, msg = _lint_supported_submit_api(f)

        assert ok is True
        assert msg == ""

    @pytest.mark.parametrize(
        "forbidden",
        ["self._msgbus", "self._manager.something", "self.send_risk_command(cmd)"],
    )
    def test_msgbus_manager_or_send_risk_command_is_rejected(
        self, tmp_path: Path, forbidden: str
    ) -> None:
        """Each forbidden Nautilus internal that would bypass the gate is rejected
        with a message naming the offending token."""
        from msai.services.strategy_registry import _lint_supported_submit_api

        f = self._make_strategies_file(
            tmp_path,
            f"def on_bar(self, bar):\n    {forbidden}\n",
        )
        ok, msg = _lint_supported_submit_api(f)

        assert ok is False
        assert "bypass" in msg.lower()

    def test_benign_substring_does_not_false_trip(self, tmp_path: Path) -> None:
        """A benign identifier that merely CONTAINS a forbidden token as a
        substring (e.g. ``manager_count``) must not trip the word-boundary lint."""
        from msai.services.strategy_registry import _lint_supported_submit_api

        f = self._make_strategies_file(
            tmp_path,
            "manager_count = 3\nmsgbus_label = 'ok'\n",
        )
        ok, msg = _lint_supported_submit_api(f)

        assert ok is True
        assert msg == ""

    def test_validate_strategy_file_rejects_bypass_before_import(self, tmp_path: Path) -> None:
        """End-to-end: ``validate_strategy_file`` surfaces the lint rejection
        (the operator sees the error before the strategy is ever imported)."""
        f = self._make_strategies_file(
            tmp_path,
            "class S:\n    def on_bar(self, bar):\n        self._msgbus.publish('x', 1)\n",
        )
        ok, message = validate_strategy_file(f)

        assert ok is False
        assert "_msgbus" in message


# ---------------------------------------------------------------------------
# Tests: load_strategy_class (legacy helper retained for tests)
# ---------------------------------------------------------------------------


class TestLoadStrategyClass:
    """Tests for :func:`load_strategy_class`."""

    def test_load_strategy_class_success(self, example_strategies_dir: Path) -> None:
        """load_strategy_class returns the EMACrossStrategy class."""
        module_path = example_strategies_dir / "ema_cross.py"
        cls = load_strategy_class(module_path, "EMACrossStrategy")

        assert inspect.isclass(cls)
        assert cls.__name__ == "EMACrossStrategy"

    def test_load_strategy_class_missing_class(self, example_strategies_dir: Path) -> None:
        """Requesting a nonexistent class raises ImportError."""
        module_path = example_strategies_dir / "ema_cross.py"

        with pytest.raises(ImportError, match="NonExistent"):
            load_strategy_class(module_path, "NonExistent")

    def test_load_strategy_class_bad_path(self, tmp_path: Path) -> None:
        """A path that does not exist raises ImportError."""
        bad_path = tmp_path / "does_not_exist.py"

        with pytest.raises(ImportError):
            load_strategy_class(bad_path, "SomeStrategy")


# ---------------------------------------------------------------------------
# Council pre-gate spike — msgspec JSON Schema fidelity for Nautilus types
# (2026-04-20 council verdict; see
#  docs/prds/strategy-config-schema-extraction-discussion.md)
#
# The Contrarian flagged that `msgspec.json.schema()` behavior on Nautilus-
# native types (`InstrumentId`, `BarType`, `Decimal`) is unverified. These
# tests ARE the verification. They pin the three properties any later
# extraction must rely on:
#
#   (a) msgspec.json.schema(..., schema_hook=...) produces usable schema
# The native V2 constructor helper is now the authoritative user contract.


class TestNativeUserSchemaFidelity:
    """The user field boundary replaces the historical V1 Msgspec spike."""

    def test_user_schema_excludes_native_base_fields(self):
        from strategies.example.config import EMACrossConfig

        from msai.services.nautilus.schema_hooks import build_user_schema

        schema, defaults, status = build_user_schema(EMACrossConfig)
        assert status == "ready"
        assert set(schema["properties"]) == {
            "instrument_id",
            "bar_type",
            "fast_ema_period",
            "slow_ema_period",
            "trade_size",
        }
        assert defaults == {"fast_ema_period": 10, "slow_ema_period": 30, "trade_size": "1"}
        assert schema["properties"]["trade_size"]["format"] == "decimal"


# ---------------------------------------------------------------------------
# Tests: sync_strategies_to_db (prune_missing branch)
# ---------------------------------------------------------------------------


class _FakeAsyncSession:
    """Minimal async session covering ``sync_strategies_to_db`` call surface.

    Supports ``execute(select(Strategy)).scalars().all()``, ``add()``,
    ``delete()``. Commit is a no-op — caller commits.
    """

    def __init__(self, existing: list[Strategy] | None = None) -> None:
        self._rows: list[Strategy] = list(existing or [])
        self.deleted: list[Strategy] = []

    async def execute(self, _stmt: object) -> _FakeAsyncSession:
        return self

    def scalars(self) -> _FakeAsyncSession:
        return self

    def all(self) -> list[Strategy]:
        return [r for r in self._rows if r not in self.deleted]

    def add(self, row: Strategy) -> None:
        self._rows.append(row)

    async def delete(self, row: Strategy) -> None:
        self.deleted.append(row)


class TestSyncStrategiesToDb:
    """Tests for :func:`sync_strategies_to_db` — orphan-prune branch."""

    async def test_sync_prunes_row_whose_file_no_longer_exists(self, tmp_path: Path) -> None:
        """Soft-prune contract (plan R2 / T3): a row whose ``file_path``
        has vanished from disk is marked archived via ``deleted_at`` —
        NOT hard-deleted. Hard delete would orphan historical backtest
        and deployment foreign keys.
        """
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_dir = tmp_path / "strategies"
        strategies_dir.mkdir()

        orphan_file = tmp_path / "deleted_strategy.py"
        orphan_row = Strategy(
            name="deleted.strategy",
            file_path=str(orphan_file),
            strategy_class="DeletedStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="deadbeef",
        )
        session = _FakeAsyncSession(existing=[orphan_row])

        # File does NOT exist; empty strategies_dir → no discovered rows
        assert not orphan_file.exists()

        result = await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_dir,
            prune_missing=True,
        )

        assert result == []
        # Soft-prune: ``deleted_at`` is stamped, row is NOT hard-deleted.
        assert orphan_row not in session.deleted, "Soft-prune must not hard-delete the row"
        assert orphan_row.deleted_at is not None, "Soft-prune must stamp deleted_at"

    async def test_sync_keeps_orphan_row_when_prune_missing_false(self, tmp_path: Path) -> None:
        """Opt-out: ``prune_missing=False`` leaves orphan rows untouched
        (no ``deleted_at`` stamp, no hard delete)."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_dir = tmp_path / "strategies"
        strategies_dir.mkdir()

        orphan_row = Strategy(
            name="kept.strategy",
            file_path=str(tmp_path / "gone.py"),
            strategy_class="KeptStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="cafebabe",
        )
        session = _FakeAsyncSession(existing=[orphan_row])

        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_dir,
            prune_missing=False,
        )

        assert orphan_row not in session.deleted
        assert orphan_row.deleted_at is None

    async def test_sync_preserves_user_patched_description(
        self, example_strategies_dir: Path
    ) -> None:
        """PATCH /api/v1/strategies/{id} sets ``description``; the next GET
        calls ``sync_strategies_to_db`` first. Before this regression test,
        the sync unconditionally overwrote ``row.description = info.description``
        on every call, so the on-disk docstring would clobber the
        PATCH-saved value — silent edit no-op caught by 2026-05-15 CLI
        completeness E2E.
        """
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import discover_strategies, sync_strategies_to_db

        # Arrange: an existing row whose description has been user-PATCHed
        # to something different from the on-disk docstring.
        discovered = discover_strategies(example_strategies_dir)
        assert discovered, "example strategies dir should yield at least one strategy"
        info = discovered[0]

        existing_row = Strategy(
            name=info.name,
            description="USER PATCHED DESCRIPTION — must survive sync",
            file_path=str(info.module_path),
            strategy_class=info.strategy_class_name,
            config_class=info.config_class_name,
            config_schema=info.config_schema,
            default_config=info.default_config,
            config_schema_status=info.config_schema_status,
            code_hash=info.code_hash,
        )
        session = _FakeAsyncSession(existing=[existing_row])

        # Act: trigger the same sync the GET endpoint runs.
        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            example_strategies_dir,
        )

        # Assert: description was NOT clobbered by the on-disk docstring.
        assert existing_row.description == "USER PATCHED DESCRIPTION — must survive sync"

    async def test_sync_matches_existing_row_by_canonical_name_across_filesystems(
        self, example_strategies_dir: Path
    ) -> None:
        """A foreign absolute path must not make the same strategy look orphaned."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = example_strategies_dir.parent
        discovered = discover_strategies(strategies_root)
        info = next(item for item in discovered if item.name == "example.ema_cross")
        row = Strategy(
            name=info.name,
            file_path="/guaranteed-foreign-root/strategies/example/ema_cross.py",
            strategy_class=info.strategy_class_name,
            config_class=info.config_class_name,
            config_schema=info.config_schema,
            default_config=info.default_config,
            config_schema_status=info.config_schema_status,
            code_hash=info.code_hash,
        )
        session = _FakeAsyncSession(existing=[row])

        paired = await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=True,
        )

        assert row.deleted_at is None
        assert [db_row for db_row in session._rows if db_row.name == info.name] == [row]
        assert any(db_row is row for db_row, _ in paired)
        assert row.file_path == "example/ema_cross.py"

    async def test_sync_retires_redundant_active_row_for_discovered_name(
        self, example_strategies_dir: Path
    ) -> None:
        """Path-era duplicates must not remain active and addressable by UUID."""
        from uuid import uuid4

        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = example_strategies_dir.parent
        discovered = discover_strategies(strategies_root)
        info = next(item for item in discovered if item.name == "example.ema_cross")
        duplicate = Strategy(
            id=uuid4(),
            name=info.name,
            file_path="/legacy-host/strategies/example/ema_cross.py",
            strategy_class=info.strategy_class_name,
            config_class=info.config_class_name,
            config_schema=info.config_schema,
            default_config=info.default_config,
            config_schema_status=info.config_schema_status,
            code_hash=info.code_hash,
        )
        canonical = Strategy(
            id=uuid4(),
            name=info.name,
            file_path="example/ema_cross.py",
            strategy_class=info.strategy_class_name,
            config_class=info.config_class_name,
            config_schema=info.config_schema,
            default_config=info.default_config,
            config_schema_status=info.config_schema_status,
            code_hash=info.code_hash,
        )
        session = _FakeAsyncSession(existing=[duplicate, canonical])

        paired = await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=True,
        )

        assert canonical.deleted_at is None
        assert duplicate.deleted_at is not None
        assert [row for row, found in paired if found.name == info.name] == [canonical]

    async def test_sync_persists_new_file_path_relative_to_strategies_root(
        self, example_strategies_dir: Path
    ) -> None:
        """A fresh sync stores a portable root-relative path, not a host path."""
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = example_strategies_dir.parent
        session = _FakeAsyncSession()

        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=False,
        )

        row = next(item for item in session._rows if item.name == "example.ema_cross")
        assert row.file_path == "example/ema_cross.py"

    @pytest.mark.parametrize("root_kind", ["missing", "file"])
    async def test_sync_prune_fails_closed_when_root_is_not_a_directory(
        self, tmp_path: Path, root_kind: str
    ) -> None:
        """An absent or invalid mount is not authoritative evidence of deletion."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = tmp_path / "strategies"
        if root_kind == "file":
            strategies_root.write_text("not a directory", encoding="utf-8")
        row = Strategy(
            name="example.ema_cross",
            file_path="example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="deadbeef",
        )
        session = _FakeAsyncSession(existing=[row])

        result = await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=True,
        )

        assert result == []
        assert row.deleted_at is None

    async def test_sync_excludes_smoke_rows_from_file_discovery_prune(self, tmp_path: Path) -> None:
        """Migration-seeded smoke rows are outside the filesystem registry lifecycle."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = tmp_path / "strategies"
        strategies_root.mkdir()
        row = Strategy(
            name="__smoke__/ema_cross/AAPL",
            file_path="example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="deadbeef",
        )
        session = _FakeAsyncSession(existing=[row])

        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=True,
        )

        assert row.deleted_at is None

    async def test_sync_keeps_undiscovered_row_while_stored_file_exists(
        self, tmp_path: Path
    ) -> None:
        """A governance/import skip is not evidence that an on-disk file was deleted."""
        from msai.models.strategy import Strategy
        from msai.services.strategy_registry import sync_strategies_to_db

        strategies_root = tmp_path / "strategies"
        strategies_root.mkdir()
        # Literal dots are deliberately undiscoverable because dotted names
        # cannot reverse them uniquely, but an existing row must remain safe.
        stored_file = strategies_root / "foo.bar.py"
        stored_file.write_text("not importable by design\n", encoding="utf-8")
        row = Strategy(
            name="foo.bar",
            file_path="foo.bar.py",
            strategy_class="LegacyStrategy",
            config_class=None,
            config_schema=None,
            default_config=None,
            config_schema_status="no_config_class",
            code_hash="deadbeef",
        )
        session = _FakeAsyncSession(existing=[row])

        await sync_strategies_to_db(
            session,  # type: ignore[arg-type]
            strategies_root,
            prune_missing=True,
        )

        assert row.deleted_at is None
