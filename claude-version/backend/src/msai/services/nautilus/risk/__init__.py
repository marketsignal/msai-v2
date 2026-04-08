"""Risk-overlay primitives for live trading.

The :class:`RiskAwareStrategy` mixin runs custom pre-submit
risk checks BEFORE calling Nautilus's ``submit_order``. The
mixin is designed to be added to user strategies; the built-in
``LiveRiskEngine`` (configured separately in Task 3.8) still
runs after, so this mixin is in addition to Nautilus's native
checks, not instead of them.
"""

from msai.services.nautilus.risk.risk_aware_strategy import (
    RiskAwareStrategy,
    RiskCheckResult,
    RiskLimits,
)

__all__ = [
    "RiskAwareStrategy",
    "RiskCheckResult",
    "RiskLimits",
]
