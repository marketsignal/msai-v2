"""Unit tests for ``_resolve_strategy_code_hash`` (Phase 1 Task 1.1b Codex P1 fix).

Verifies the helper that computes the real SHA256 of a strategy file on
disk — previously hard-coded to ``"live"``, which would have silently
collided with prior deployments after any strategy edit and broken the
cold-start semantics from decision #7.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from msai.api.live import _resolve_strategy_code_hash
from msai.core.config import settings

if TYPE_CHECKING:
    from pathlib import Path


def _make_strategy(file_path: str) -> MagicMock:
    """Build a minimal Strategy-like mock with only ``file_path`` set."""
    strategy = MagicMock()
    strategy.file_path = file_path
    return strategy


class TestResolveStrategyCodeHash:
    def test_returns_real_sha256_of_file_contents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        strategies_root = tmp_path / "strategies"
        (strategies_root / "example").mkdir(parents=True)
        body = b"# fake strategy\nfrom nautilus_trader import Strategy\n"
        (strategies_root / "example" / "ema.py").write_bytes(body)

        monkeypatch.setattr(settings, "strategies_root", strategies_root)
        strategy = _make_strategy("strategies/example/ema.py")

        result = _resolve_strategy_code_hash(strategy)

        expected = hashlib.sha256(body).hexdigest()
        assert result == expected
        assert len(result) == 64

    def test_different_contents_produce_different_hashes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of Codex P1 fix — editing a strategy file must
        change the hash so the next deployment starts cold instead of
        warm-restarting on incompatible persisted state."""
        strategies_root = tmp_path / "strategies"
        (strategies_root / "example").mkdir(parents=True)
        strategy_file = strategies_root / "example" / "ema.py"
        strategy_file.write_bytes(b"fast = 10")

        monkeypatch.setattr(settings, "strategies_root", strategies_root)
        strategy = _make_strategy("strategies/example/ema.py")
        before = _resolve_strategy_code_hash(strategy)

        strategy_file.write_bytes(b"fast = 20")
        after = _resolve_strategy_code_hash(strategy)

        assert before != after

    def test_bare_relative_path_without_strategies_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Strategy.file_path`` values persisted before the v9 convention
        change may not carry the ``strategies/`` prefix. The helper must
        resolve both shapes under ``settings.strategies_root``."""
        strategies_root = tmp_path / "strategies"
        (strategies_root / "nested").mkdir(parents=True)
        (strategies_root / "nested" / "thing.py").write_bytes(b"body")

        monkeypatch.setattr(settings, "strategies_root", strategies_root)
        strategy = _make_strategy("nested/thing.py")

        result = _resolve_strategy_code_hash(strategy)
        assert result == hashlib.sha256(b"body").hexdigest()

    def test_absolute_path_is_used_as_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        abs_file = tmp_path / "absolute.py"
        abs_file.write_bytes(b"abs body")

        # strategies_root is irrelevant when file_path is absolute
        monkeypatch.setattr(settings, "strategies_root", tmp_path / "somewhere-else")
        strategy = _make_strategy(str(abs_file))

        result = _resolve_strategy_code_hash(strategy)
        assert result == hashlib.sha256(b"abs body").hexdigest()

    def test_missing_file_raises_500(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the strategy file was deleted out from under us, fail loudly
        rather than silently hashing an empty string or using the old
        ``'live'`` placeholder (which would collide identities)."""
        monkeypatch.setattr(settings, "strategies_root", tmp_path / "strategies")
        strategy = _make_strategy("strategies/ghost.py")

        with pytest.raises(HTTPException) as exc_info:
            _resolve_strategy_code_hash(strategy)

        assert exc_info.value.status_code == 500
        assert "not found on disk" in exc_info.value.detail
