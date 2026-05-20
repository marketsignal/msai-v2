"""Hard safety caps the Full-mode optimizer cannot violate.

Two enforcement paths:

1. Search-space clip at construction time (in optimizer.py — bounds on the
   primary parameters so suggested values never exceed caps).
2. Reject-after-evaluation (here — for combined-parameter derived violations
   like total leverage emerging from per-strategy weights).
"""

from __future__ import annotations

from dataclasses import dataclass


class SafetyCapsBreach(Exception):  # noqa: N818 — plan-prescribed name
    """Raised when an evaluated trial violates a hard cap."""


@dataclass(frozen=True)
class SafetyCaps:
    max_leverage: float
    max_position_size: float | None = None
    max_drawdown_halt: float | None = None


def enforce_caps(
    caps: SafetyCaps,
    *,
    total_leverage: float,
    max_position: float,
    observed_max_dd: float,
) -> None:
    """Raise ``SafetyCapsBreach`` if ANY cap is violated by the observed values.

    ``observed_max_dd`` is the absolute drawdown magnitude (positive number).
    """
    if total_leverage > caps.max_leverage:
        raise SafetyCapsBreach(f"leverage {total_leverage:.3f} exceeds cap {caps.max_leverage}")
    if caps.max_position_size is not None and max_position > caps.max_position_size:
        raise SafetyCapsBreach(f"position {max_position:.3f} exceeds cap {caps.max_position_size}")
    if caps.max_drawdown_halt is not None and observed_max_dd > caps.max_drawdown_halt:
        raise SafetyCapsBreach(
            f"drawdown {observed_max_dd:.3f} exceeds halt cap {caps.max_drawdown_halt}"
        )
