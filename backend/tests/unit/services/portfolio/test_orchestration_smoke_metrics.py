"""Unit tests for the smoke-only G5 metrics enrichment in orchestration."""

from __future__ import annotations

import pytest

from msai.services.portfolio.orchestration import enrich_smoke_metrics


def test_enrich_smoke_metrics_adds_g5_keys() -> None:
    base = {
        "total_return": 0.05,
        "sharpe": 1.3,
        "sortino": 1.6,
        "max_drawdown": -0.08,
        "alpha": 0.02,
        "beta": 0.95,
        "win_rate": 0.55,
        "annualized_volatility": 0.18,
        "downside_risk": 0.12,
    }
    enriched = enrich_smoke_metrics(
        core_metrics=base,
        base_capital=100_000.0,
        trade_counts_by_strategy={
            "__smoke__/smoke_market_order/AAPL": 1,
            "__smoke__/smoke_market_order/SPY": 1,
            "__smoke__/ema_cross/AAPL": 2,
        },
        benchmark_symbol="SPY",
        smoke_config="fast",
    )
    assert enriched["pnl"] == pytest.approx(5_000.0, rel=1e-9)
    assert enriched["trade_count_by_strategy"] == {
        "__smoke__/smoke_market_order/AAPL": 1,
        "__smoke__/smoke_market_order/SPY": 1,
        "__smoke__/ema_cross/AAPL": 2,
    }
    assert enriched["trade_count_total"] == 4
    assert enriched["benchmark_symbol"] == "SPY"
    assert enriched["smoke_config"] == "fast"
    # Existing keys preserved
    assert enriched["sharpe"] == 1.3


def test_enrich_smoke_metrics_handles_empty_counts() -> None:
    base = {"total_return": 0.0}
    enriched = enrich_smoke_metrics(
        core_metrics=base,
        base_capital=100_000.0,
        trade_counts_by_strategy={},
        benchmark_symbol="SPY",
        smoke_config="fast",
    )
    assert enriched["trade_count_by_strategy"] == {}
    assert enriched["trade_count_total"] == 0
    assert enriched["pnl"] == pytest.approx(0.0, abs=1e-9)
