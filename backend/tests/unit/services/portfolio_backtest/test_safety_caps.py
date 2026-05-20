import pytest

from msai.services.portfolio_backtest.safety_caps import (
    SafetyCaps,
    SafetyCapsBreach,
    enforce_caps,
)


def test_safety_caps_dataclass():
    s = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    assert s.max_leverage == 2.0


def test_enforce_caps_allowed():
    s = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    enforce_caps(s, total_leverage=1.5, max_position=0.20, observed_max_dd=0.10)
    # no exception → allowed


def test_enforce_caps_rejects_excess_leverage():
    s = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    with pytest.raises(SafetyCapsBreach, match="leverage"):
        enforce_caps(s, total_leverage=2.5, max_position=0.20, observed_max_dd=0.10)


def test_enforce_caps_rejects_excess_position():
    s = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    with pytest.raises(SafetyCapsBreach, match="position"):
        enforce_caps(s, total_leverage=1.0, max_position=0.30, observed_max_dd=0.10)


def test_enforce_caps_rejects_excess_drawdown():
    s = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    with pytest.raises(SafetyCapsBreach, match="drawdown"):
        enforce_caps(s, total_leverage=1.0, max_position=0.20, observed_max_dd=0.25)
