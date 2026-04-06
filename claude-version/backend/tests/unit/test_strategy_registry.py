"""Unit tests for the strategy registry service."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from msai.services.strategy_registry import (
    compute_file_hash,
    discover_strategies,
    load_strategy_class,
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


@pytest.fixture()
def strategies_dir_with_init_and_config(tmp_path: Path) -> Path:
    """Return a directory containing only __init__.py and config.py (should be skipped)."""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "__init__.py").write_text("# init\n")
    (d / "config.py").write_text("# config\n")
    (d / "_private.py").write_text("class PrivateStrategy: pass\n")
    return d


# ---------------------------------------------------------------------------
# Tests: discover_strategies
# ---------------------------------------------------------------------------


class TestDiscoverStrategies:
    """Tests for discover_strategies function."""

    def test_discover_strategies_finds_example(self, example_strategies_dir: Path) -> None:
        """discover_strategies finds EMACrossStrategy in the example directory."""
        results = discover_strategies(example_strategies_dir)

        assert len(results) >= 1
        names = [r.class_name for r in results]
        assert "EMACrossStrategy" in names

        ema = next(r for r in results if r.class_name == "EMACrossStrategy")
        assert ema.name == "ema_cross"
        assert ema.module_path.name == "ema_cross.py"
        assert ema.description is not None
        assert "EMA crossover" in ema.description

    def test_discover_strategies_returns_code_hash(self, example_strategies_dir: Path) -> None:
        """Discovered strategies include a 64-character hex SHA256 hash."""
        results = discover_strategies(example_strategies_dir)

        assert len(results) >= 1
        for info in results:
            assert len(info.code_hash) == 64
            # Verify it is a valid hex string
            int(info.code_hash, 16)

    def test_discover_strategies_empty_dir(self, empty_strategies_dir: Path) -> None:
        """An empty directory returns an empty list."""
        results = discover_strategies(empty_strategies_dir)

        assert results == []

    def test_discover_strategies_nonexistent_dir(self, tmp_path: Path) -> None:
        """A nonexistent directory returns an empty list without raising."""
        results = discover_strategies(tmp_path / "nonexistent")

        assert results == []

    def test_discover_strategies_skips_init_and_config(
        self, strategies_dir_with_init_and_config: Path
    ) -> None:
        """__init__.py, config.py, and _private.py files are skipped."""
        results = discover_strategies(strategies_dir_with_init_and_config)

        assert results == []


# ---------------------------------------------------------------------------
# Tests: compute_file_hash
# ---------------------------------------------------------------------------


class TestComputeFileHash:
    """Tests for compute_file_hash function."""

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
# Tests: load_strategy_class
# ---------------------------------------------------------------------------


class TestLoadStrategyClass:
    """Tests for load_strategy_class function."""

    def test_load_strategy_class_success(self, example_strategies_dir: Path) -> None:
        """load_strategy_class returns the EMACrossStrategy class."""
        module_path = example_strategies_dir / "ema_cross.py"
        cls = load_strategy_class(module_path, "EMACrossStrategy")

        assert inspect.isclass(cls)
        assert cls.__name__ == "EMACrossStrategy"

        # Verify we can instantiate it
        instance = cls(fast_period=5, slow_period=15)
        assert instance.fast_period == 5
        assert instance.slow_period == 15

    def test_load_strategy_class_missing_class(self, example_strategies_dir: Path) -> None:
        """Requesting a nonexistent class raises ImportError."""
        module_path = example_strategies_dir / "ema_cross.py"

        with pytest.raises(ImportError, match="Class NonExistent not found"):
            load_strategy_class(module_path, "NonExistent")

    def test_load_strategy_class_bad_path(self, tmp_path: Path) -> None:
        """A path that does not exist raises ImportError."""
        bad_path = tmp_path / "does_not_exist.py"

        with pytest.raises(ImportError, match="Cannot load module"):
            load_strategy_class(bad_path, "SomeStrategy")
