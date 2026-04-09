from __future__ import annotations

import importlib.util
from pathlib import Path

STRATEGY_PATH = (
    Path(__file__).resolve().parents[3] / "strategies" / "user" / "slope_ma_breakout.py"
)


def _load_strategy_module():
    spec = importlib.util.spec_from_file_location("slope_ma_breakout", STRATEGY_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_linear_regression_slope_tracks_trend_direction() -> None:
    module = _load_strategy_module()

    assert module._linear_regression_slope([100.0, 101.0, 102.0, 103.0]) > 0
    assert module._linear_regression_slope([103.0, 102.0, 101.0, 100.0]) < 0
    assert module._linear_regression_slope([100.0, 100.0, 100.0, 100.0]) == 0.0


def test_true_range_uses_gap_when_larger_than_intrabar_range() -> None:
    module = _load_strategy_module()

    assert module._true_range(high=101.0, low=99.0, previous_close=100.0) == 2.0
    assert module._true_range(high=101.0, low=100.5, previous_close=98.0) == 3.0
