"""Unit tests for the strategy registry service."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from msai.services.strategy_registry import (
    DiscoveredStrategy,
    compute_file_hash,
    discover_strategies,
    load_strategy_class,
    validate_strategy_file,
)

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
