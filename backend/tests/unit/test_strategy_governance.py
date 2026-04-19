"""Tests for strategy governance service."""

import textwrap
from pathlib import Path

import pytest

from msai.services.strategy_governance import StrategyGovernanceService


@pytest.fixture
def gov() -> StrategyGovernanceService:
    return StrategyGovernanceService()


@pytest.fixture
def tmp_strategy(tmp_path: Path):
    """Factory: write Python content to a temp file and return its path."""

    def _make(content: str) -> Path:
        p = tmp_path / "test_strategy.py"
        p.write_text(textwrap.dedent(content))
        return p

    return _make


def test_clean_strategy_passes(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        import numpy as np

        class MyStrategy:
            def on_start(self):
                pass
    """)
    violations = gov.validate_file(path)
    assert violations == []


def test_blocked_import_os(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        import os
        import numpy as np
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "os" in violations[0]


def test_blocked_import_subprocess(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        import subprocess
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "subprocess" in violations[0]


def test_blocked_from_import(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        from os.path import join
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "os" in violations[0]


def test_eval_detected(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        x = eval("1 + 2")
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "eval" in violations[0]


def test_exec_detected(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        exec("print('hello')")
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "exec" in violations[0]


def test_dunder_import_detected(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        mod = __import__("os")
    """)
    violations = gov.validate_file(path)
    assert any("__import__" in v for v in violations)


def test_syntax_error_detected(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        def broken(
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "Syntax error" in violations[0]


def test_multiple_violations(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        import os
        import subprocess
        x = eval("bad")
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 3


def test_nonexistent_file(gov: StrategyGovernanceService, tmp_path: Path):
    path = tmp_path / "does_not_exist.py"
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "Cannot read" in violations[0]


def test_pickle_blocked(gov: StrategyGovernanceService, tmp_strategy):
    path = tmp_strategy("""
        import pickle
    """)
    violations = gov.validate_file(path)
    assert len(violations) == 1
    assert "pickle" in violations[0]
