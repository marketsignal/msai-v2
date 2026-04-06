from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskEngine:
    def __init__(self, max_position_size: float = 500.0, max_daily_loss: float = -10_000.0, max_exposure: float = 1.0) -> None:
        self.max_position_size = max_position_size
        self.max_daily_loss = max_daily_loss
        self.max_exposure = max_exposure

    def check_position_limit(self, strategy: str, instrument: str, quantity: float) -> RiskDecision:
        _ = strategy, instrument
        if abs(quantity) > self.max_position_size:
            return RiskDecision(False, "position limit exceeded")
        return RiskDecision(True, "ok")

    def check_daily_loss(self, current_pnl: float, threshold: float | None = None) -> RiskDecision:
        limit = threshold if threshold is not None else self.max_daily_loss
        if current_pnl < limit:
            return RiskDecision(False, "daily loss threshold breached")
        return RiskDecision(True, "ok")

    def check_notional_exposure(self, portfolio_value: float, notional_exposure: float) -> RiskDecision:
        if portfolio_value <= 0:
            return RiskDecision(False, "invalid portfolio value")
        ratio = notional_exposure / portfolio_value
        if ratio > self.max_exposure:
            return RiskDecision(False, "notional exposure limit exceeded")
        return RiskDecision(True, "ok")

    def validate_start(
        self,
        strategy: str,
        instrument: str,
        quantity: float,
        current_pnl: float,
        portfolio_value: float,
        notional_exposure: float,
    ) -> RiskDecision:
        for decision in (
            self.check_position_limit(strategy, instrument, quantity),
            self.check_daily_loss(current_pnl),
            self.check_notional_exposure(portfolio_value, notional_exposure),
        ):
            if not decision.allowed:
                return decision
        return RiskDecision(True, "ok")

    def kill_all(self) -> RiskDecision:
        return RiskDecision(True, "kill-all triggered")
