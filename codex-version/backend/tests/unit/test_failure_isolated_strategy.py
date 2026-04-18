from __future__ import annotations

import importlib
from types import SimpleNamespace
from textwrap import dedent
from typing import Any

from msai.services.nautilus.failure_isolated_strategy import (
    FailureIsolatedStrategy,
    StrategyDegradedError,
    activate_runtime_strategy_safety,
)


class _StubLog:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


class HealthyStrategy(FailureIsolatedStrategy):
    def __init__(self) -> None:
        self.log = _StubLog()
        self.bars: list[Any] = []

    def on_bar(self, bar: Any) -> None:
        self.bars.append(bar)


class BuggyBarStrategy(FailureIsolatedStrategy):
    def __init__(self) -> None:
        self.log = _StubLog()

    def on_bar(self, bar: Any) -> None:
        raise RuntimeError("bar boom")


def test_healthy_strategy_runs_normally() -> None:
    strategy = HealthyStrategy()
    payload = SimpleNamespace(close=100.0)

    strategy.on_bar(payload)

    assert strategy.bars == [payload]
    assert strategy.log.errors == []
    assert strategy._is_degraded is False


def test_buggy_strategy_degrades_instead_of_raising() -> None:
    strategy = BuggyBarStrategy()

    strategy.on_bar(SimpleNamespace())
    strategy.on_bar(SimpleNamespace())

    assert strategy._is_degraded is True
    assert len(strategy.log.errors) == 1
    assert "RuntimeError" in strategy.log.errors[0]
    assert len(strategy.log.warnings) == 1
    assert "skipping" in strategy.log.warnings[0].lower()


def test_activate_runtime_strategy_safety_wraps_external_strategy_and_namespaces_cache(
    tmp_path,
    monkeypatch,
) -> None:
    package_dir = tmp_path / "runtime_safe_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "demo_runtime.py").write_text(
        dedent(
            """
            class DemoStrategy:
                def on_start(self):
                    self.cache.add("alpha", 1)

                def on_bar(self, bar):
                    raise ValueError("external crash")
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    wrapped_cls = activate_runtime_strategy_safety("runtime_safe_pkg.demo_runtime:DemoStrategy")
    module = importlib.import_module("runtime_safe_pkg.demo_runtime")

    assert module.DemoStrategy is wrapped_cls
    assert issubclass(wrapped_cls, FailureIsolatedStrategy)

    class _CacheStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def add(self, key: str, value: object) -> object:
            self.calls.append((key, value))
            return value

    strategy = wrapped_cls()
    strategy.log = _StubLog()
    strategy.id = SimpleNamespace(value="DemoStrategy-0-runtime")
    strategy.cache = _CacheStub()

    strategy.on_start()
    strategy.on_bar(SimpleNamespace())

    assert strategy.cache.calls == [("DemoStrategy-0-runtime:alpha", 1)]
    assert strategy._is_degraded is True
    assert len(strategy.log.errors) == 1


def test_strategy_degraded_error_is_exception() -> None:
    assert issubclass(StrategyDegradedError, Exception)
