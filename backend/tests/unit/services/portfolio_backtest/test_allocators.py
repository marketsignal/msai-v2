import math

import pandas as pd
import pytest

from msai.services.portfolio_backtest.allocators import (
    ALLOCATORS,
    EqualWeightAllocator,
    FixedWeightAllocator,
    InverseVolAllocator,
    VolTargetedAllocator,
)


def test_equal_weight_three_strategies():
    a = EqualWeightAllocator()
    weights = a.compute(["s1", "s2", "s3"], returns=None)
    assert weights == {"s1": 1 / 3, "s2": 1 / 3, "s3": 1 / 3}


def test_equal_weight_sums_to_one():
    a = EqualWeightAllocator()
    weights = a.compute(["s1", "s2"], returns=None)
    assert math.isclose(sum(weights.values()), 1.0)


def test_fixed_weight_uses_provided():
    a = FixedWeightAllocator(weights={"s1": 0.7, "s2": 0.3})
    out = a.compute(["s1", "s2"], returns=None)
    assert out == {"s1": 0.7, "s2": 0.3}


def test_fixed_weight_rejects_unknown_strategy():
    a = FixedWeightAllocator(weights={"s1": 1.0})
    with pytest.raises(ValueError, match="weights"):
        a.compute(["s1", "s2"], returns=None)


def test_fixed_weight_normalizes_if_not_summing_to_one():
    a = FixedWeightAllocator(weights={"s1": 0.5, "s2": 0.5, "s3": 0.5})  # sums to 1.5
    out = a.compute(["s1", "s2", "s3"], returns=None)
    assert math.isclose(sum(out.values()), 1.0)


def test_inverse_vol_higher_weight_to_lower_vol():
    # Two strategies — s1 has higher volatility than s2 → s2 gets more weight
    s1 = pd.Series([0.1, -0.1, 0.1, -0.1, 0.1, -0.1])
    s2 = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01, -0.01])
    a = InverseVolAllocator()
    w = a.compute(["s1", "s2"], returns=pd.DataFrame({"s1": s1, "s2": s2}))
    assert w["s2"] > w["s1"], "lower-vol strategy should receive higher weight"
    assert math.isclose(sum(w.values()), 1.0)


def test_vol_targeted_scales_to_target():
    s1 = pd.Series([0.005, -0.005, 0.005, -0.005] * 50)
    a = VolTargetedAllocator(target_vol_annualized=0.10)
    w = a.compute(["s1"], returns=pd.DataFrame({"s1": s1}))
    # With realized vol << 10%, the scaler is >1 → weight > 1.0; with cap=2 it's bounded.
    assert "s1" in w
    assert 0.0 <= w["s1"] <= 2.0


def test_allocators_registry_contains_all_four():
    assert "equal_weight" in ALLOCATORS
    assert "fixed_weight" in ALLOCATORS
    assert "inverse_vol" in ALLOCATORS
    assert "vol_targeted" in ALLOCATORS
