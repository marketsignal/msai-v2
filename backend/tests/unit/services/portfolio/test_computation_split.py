"""Refactor contract: pure-computation helpers live in ``portfolio.computation``.

Task A3 moved the pure-compute helpers out of ``portfolio.orchestration``
(where Task A1 staged them) and into a dedicated ``portfolio.computation``
module.  The new names drop the leading underscore — they are module-level
functions now, not class-internal helpers.

Task A4 then swept the call sites onto the new path and deleted the legacy
``portfolio_service.py`` shim.  This module asserts the new module surface
directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from msai.models.portfolio_enums import PortfolioObjective


def test_heuristic_weight_in_computation() -> None:
    """``heuristic_weight`` is importable from the new ``computation`` module."""
    from msai.services.portfolio.computation import heuristic_weight

    metrics = {"sharpe": 1.5, "sortino": 2.0, "total_return": 0.20}
    w = heuristic_weight(metrics, PortfolioObjective.MAXIMIZE_SHARPE)
    assert isinstance(w, float)
    assert w > 0


def test_effective_leverage_in_computation() -> None:
    """``effective_leverage`` is importable from the new ``computation`` module.

    Signature is preserved from the original ``_effective_leverage`` (verbatim
    move per Task A3 — no behavior change).  Scaling drives leverage down when
    combined-portfolio downside risk exceeds ``downside_target``.
    """
    from msai.services.portfolio.computation import effective_leverage

    # If downside_target is None -> requested_leverage passes through.
    weighted_flat = [
        (
            "s",
            1.0,
            pd.Series(
                [0.01, -0.02, 0.03],
                index=pd.date_range("2024-01-01", periods=3, freq="D"),
            ),
        ),
    ]
    assert (
        effective_leverage(
            weighted_series=weighted_flat,
            requested_leverage=2.0,
            downside_target=None,
        )
        == 2.0
    )

    # High losses + tight downside_target -> leverage scales down (but never below 0.1).
    weighted_lossy = [
        (
            "s",
            1.0,
            pd.Series(
                [-0.05, -0.08, -0.06, -0.04],
                index=pd.date_range("2024-01-01", periods=4, freq="D"),
            ),
        ),
    ]
    scaled = effective_leverage(
        weighted_series=weighted_lossy,
        requested_leverage=2.0,
        downside_target=0.05,
    )
    assert 0.1 <= scaled < 2.0


def test_raw_benchmark_symbol_in_computation() -> None:
    """``raw_benchmark_symbol`` is importable from the new ``computation`` module."""
    from msai.services.portfolio.computation import raw_benchmark_symbol

    # Strip uppercase venue suffix
    assert raw_benchmark_symbol("SPY.NASDAQ") == "SPY"
    # Preserve share-class suffix
    assert raw_benchmark_symbol("BRK.B") == "BRK.B"
    # No dot -> unchanged
    assert raw_benchmark_symbol("AAPL") == "AAPL"


def test_load_benchmark_returns_in_computation() -> None:
    """``load_benchmark_returns`` is importable from the new ``computation`` module."""
    from msai.services.portfolio.computation import load_benchmark_returns

    assert callable(load_benchmark_returns)

    # Empty symbol -> None (by-design, no logs).
    mq = MagicMock()
    assert (
        load_benchmark_returns(
            mq,
            benchmark_symbol="",
            start_date="2024-01-01",
            end_date="2024-01-02",
        )
        is None
    )


def test_legacy_shim_module_is_gone() -> None:
    """Task A4 deleted ``portfolio_service.py``; importing it must fail.

    This test guards against accidental resurrection of the shim during
    later refactors — every caller now lives on
    ``msai.services.portfolio[.computation|.orchestration|.lifecycle]``.
    """
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("msai.services.portfolio_service")
