"""Canonical smoke configurations — pinned in code for reproducibility.

PRD: docs/prds/ingest-backtest-smoke-test.md v1.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

SmokeConfigName = Literal["fast", "nightly"]


@dataclass(frozen=True)
class SmokeConfig:
    """Immutable canonical smoke configuration."""

    name: SmokeConfigName
    symbols: tuple[str, ...]
    strategy_names: tuple[str, ...]
    start_date: date
    end_date: date
    benchmark_symbol: str
    runtime_budget_warm_sec: int
    runtime_budget_cold_sec: int


_SMOKE_STRATEGY_NAMES = (
    "__smoke__/smoke_market_order/AAPL",
    "__smoke__/smoke_market_order/SPY",
    "__smoke__/ema_cross/AAPL",
    "__smoke__/ema_cross/SPY",
)


SMOKE_FAST = SmokeConfig(
    name="fast",
    symbols=("AAPL", "SPY"),
    strategy_names=_SMOKE_STRATEGY_NAMES,
    start_date=date(2024, 12, 1),
    end_date=date(2024, 12, 31),
    benchmark_symbol="SPY",
    runtime_budget_warm_sec=180,
    runtime_budget_cold_sec=600,
)


SMOKE_NIGHTLY = SmokeConfig(
    name="nightly",
    symbols=("AAPL", "SPY"),
    strategy_names=_SMOKE_STRATEGY_NAMES,
    start_date=date(2024, 1, 1),
    end_date=date(2024, 12, 31),
    benchmark_symbol="SPY",
    runtime_budget_warm_sec=600,
    runtime_budget_cold_sec=3600,
)


SMOKE_CONFIGS: dict[SmokeConfigName, SmokeConfig] = {
    "fast": SMOKE_FAST,
    "nightly": SMOKE_NIGHTLY,
}
