"""Pre-trade risk checks and emergency controls.

Provides a lightweight risk engine that validates orders and deployments
against configurable position, loss, and exposure limits.  The engine
maintains a per-process halted flag that acts as a circuit breaker --
once tripped, all further trading activity is blocked until an explicit
reset (typically at the start of the next trading day).
"""

from __future__ import annotations

from dataclasses import dataclass

from msai.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class RiskLimits:
    """Configurable risk thresholds for the trading engine.

    Attributes:
        max_position_size: Maximum absolute quantity allowed per instrument.
        max_daily_loss: Maximum daily loss before automatic halt (negative value).
        max_notional_exposure: Maximum total portfolio notional value.
        max_concurrent_strategies: Maximum number of live strategy deployments.
    """

    max_position_size: float = 10_000.0
    max_daily_loss: float = -5_000.0
    max_notional_exposure: float = 500_000.0
    max_concurrent_strategies: int = 5


class RiskEngine:
    """Pre-trade risk validation and emergency controls.

    The engine acts as a gatekeeper before any order or deployment is
    submitted.  It checks position sizes, daily P&L, notional exposure,
    and concurrent strategy limits.

    Once the ``_halted`` flag is set -- either by a daily loss breach or
    a manual ``kill_all()`` -- all position and notional checks return
    ``False`` and no new deployments are allowed until ``reset()`` is called.
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits: RiskLimits = limits or RiskLimits()
        self._halted: bool = False
        self._daily_pnl: float = 0.0

    def check_position_limit(self, instrument: str, quantity: float) -> bool:
        """Check if a position size is within limits.

        Args:
            instrument: The instrument identifier (for logging purposes).
            quantity: Signed quantity of the proposed order.

        Returns:
            ``True`` if the order is allowed, ``False`` if halted or
            the quantity exceeds ``max_position_size``.
        """
        if self._halted:
            return False
        allowed = abs(quantity) <= self.limits.max_position_size
        if not allowed:
            log.warning(
                "position_limit_exceeded",
                instrument=instrument,
                quantity=quantity,
                limit=self.limits.max_position_size,
            )
        return allowed

    def check_daily_loss(self, current_pnl: float) -> bool:
        """Check if the daily P&L is above the loss threshold.

        When the threshold is breached the engine is automatically halted.

        Args:
            current_pnl: Current cumulative daily profit/loss.

        Returns:
            ``True`` if within limits, ``False`` if breached (and now halted).
        """
        self._daily_pnl = current_pnl
        if current_pnl <= self.limits.max_daily_loss:
            self._halted = True
            log.error(
                "daily_loss_limit_breached",
                pnl=current_pnl,
                limit=self.limits.max_daily_loss,
            )
            return False
        return True

    def check_notional_exposure(self, total_notional: float) -> bool:
        """Check if total notional exposure is within limits.

        Args:
            total_notional: Aggregate absolute notional across all positions.

        Returns:
            ``True`` if allowed, ``False`` if halted or over the limit.
        """
        if self._halted:
            return False
        allowed = total_notional <= self.limits.max_notional_exposure
        if not allowed:
            log.warning(
                "notional_exposure_exceeded",
                total_notional=total_notional,
                limit=self.limits.max_notional_exposure,
            )
        return allowed

    def validate_deployment(self, config: dict, num_active: int) -> tuple[bool, str]:
        """Validate whether a new live deployment is allowed.

        Args:
            config: Strategy configuration dict (reserved for future checks).
            num_active: Current count of active running deployments.

        Returns:
            A ``(allowed, reason)`` tuple.  ``reason`` is ``"OK"`` when allowed.
        """
        if self._halted:
            return False, "Trading halted due to risk limit breach"
        if num_active >= self.limits.max_concurrent_strategies:
            return False, f"Max {self.limits.max_concurrent_strategies} concurrent strategies"
        return True, "OK"

    def kill_all(self) -> None:
        """Emergency halt -- set the halted flag immediately."""
        self._halted = True
        log.critical("kill_all_triggered")

    def reset(self) -> None:
        """Reset daily state.  Typically called at the start of a trading day."""
        self._halted = False
        self._daily_pnl = 0.0
        log.info("risk_engine_reset")

    @property
    def is_halted(self) -> bool:
        """Whether the engine is currently halted."""
        return self._halted
