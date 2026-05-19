"""Allocator strategies for portfolio composition.

Each allocator turns a list of strategy IDs (and optionally their historical
returns) into a dict of weight per strategy. Weights are normalized to sum to
1.0 for unleveraged allocators; vol-targeted may exceed 1.0 within its cap.

Reference: ``docs/research/2026-05-18-portfolio-backtest.md`` § 1 (the chosen
default uses these 4 allocators in v1).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import pandas as pd

# 252 trading days per year, the conventional annualization factor.
TRADING_DAYS_PER_YEAR: Final = 252


class Allocator(ABC):
    """Abstract allocator. Subclasses implement ``compute``."""

    name: str = "abstract"

    @abstractmethod
    def compute(self, strategy_ids: list[str], returns: pd.DataFrame | None) -> dict[str, float]:
        """Return a dict mapping strategy_id -> weight."""


class EqualWeightAllocator(Allocator):
    """1/N across all strategies."""

    name = "equal_weight"

    def compute(self, strategy_ids: list[str], returns: pd.DataFrame | None) -> dict[str, float]:
        if not strategy_ids:
            raise ValueError("at least one strategy required")
        n = len(strategy_ids)
        w = 1.0 / n
        return {sid: w for sid in strategy_ids}


class FixedWeightAllocator(Allocator):
    """Operator-specified per-strategy weights, normalized to sum to 1.0."""

    name = "fixed_weight"

    def __init__(self, weights: dict[str, float]) -> None:
        self._weights = weights

    def compute(self, strategy_ids: list[str], returns: pd.DataFrame | None) -> dict[str, float]:
        for sid in strategy_ids:
            if sid not in self._weights:
                raise ValueError(f"weights missing for strategy {sid}")
        raw = {sid: float(self._weights[sid]) for sid in strategy_ids}
        total = sum(raw.values())
        if total <= 0:
            raise ValueError("weights must sum to a positive value")
        return {sid: w / total for sid, w in raw.items()}


class InverseVolAllocator(Allocator):
    """Weights inversely proportional to each strategy's realized volatility."""

    name = "inverse_vol"

    def compute(self, strategy_ids: list[str], returns: pd.DataFrame | None) -> dict[str, float]:
        if returns is None or returns.empty:
            raise ValueError("returns required for inverse_vol allocator")
        if any(sid not in returns.columns for sid in strategy_ids):
            missing = [sid for sid in strategy_ids if sid not in returns.columns]
            raise ValueError(f"returns missing for {missing}")
        vols = {sid: float(returns[sid].std()) for sid in strategy_ids}
        if any(v == 0.0 for v in vols.values()):
            # Zero-vol strategy collapses to equal-weight to avoid div-by-zero.
            n = len(strategy_ids)
            return {sid: 1.0 / n for sid in strategy_ids}
        inv = {sid: 1.0 / v for sid, v in vols.items()}
        total = sum(inv.values())
        return {sid: w / total for sid, w in inv.items()}


class VolTargetedAllocator(Allocator):
    """Scale equal-weight portfolio to hit a target annualized volatility.

    Cap individual weights at ``max_weight`` to prevent runaway leverage.
    """

    name = "vol_targeted"

    def __init__(
        self,
        target_vol_annualized: float = 0.15,
        max_weight: float = 2.0,
    ) -> None:
        self._target = target_vol_annualized
        self._cap = max_weight

    def compute(self, strategy_ids: list[str], returns: pd.DataFrame | None) -> dict[str, float]:
        if returns is None or returns.empty:
            raise ValueError("returns required for vol_targeted allocator")
        n = len(strategy_ids)
        base = {sid: 1.0 / n for sid in strategy_ids}
        # Equal-weight portfolio returns over the lookback
        eq_returns = sum(returns[sid] * (1.0 / n) for sid in strategy_ids)
        realized = float(eq_returns.std()) * math.sqrt(TRADING_DAYS_PER_YEAR)
        scaler = 1.0 if realized == 0.0 else self._target / realized
        return {sid: min(w * scaler, self._cap) for sid, w in base.items()}


ALLOCATORS: dict[str, type[Allocator]] = {
    "equal_weight": EqualWeightAllocator,
    "fixed_weight": FixedWeightAllocator,
    "inverse_vol": InverseVolAllocator,
    "vol_targeted": VolTargetedAllocator,
}
