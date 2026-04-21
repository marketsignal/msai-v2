"""Auto-heal workload guardrails.

Evaluates bounded-lazy constraints BEFORE enqueueing a provider download
— prevents accidental unbounded spend on a malformed or agent-generated
backtest request.

Council-locked invariants:
- ``max_years = 10`` (cap is inclusive)
- ``max_symbols = 20``
- ``allow_options = False`` (OPRA OHLCV-1m is $280/GB on Databento)
- Mixed-asset-class requests are out of scope — caller is responsible
  for dispatching one guardrail check per asset class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date

GuardrailReason = Literal[
    "options_disabled",
    "range_exceeds_max_years",
    "symbol_count_exceeds_max",
    "no_symbols",
]


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """Outcome of a single guardrail evaluation."""

    allowed: bool
    reason: GuardrailReason | None
    human_message: str
    details: dict[str, int | str] | None = None

    def __post_init__(self) -> None:
        """Enforce allowed/reason pairing invariant."""
        if self.allowed and self.reason is not None:
            raise ValueError(f"allowed=True must have reason=None, got {self.reason!r}")
        if not self.allowed and self.reason is None:
            raise ValueError("allowed=False must have a reason")


def evaluate_guardrails(
    *,
    asset_class: str,
    symbols: list[str],
    start: date,
    end: date,
    max_years: int,
    max_symbols: int,
    allow_options: bool,
) -> GuardrailResult:
    """Return whether the request passes all guardrails.

    First-match returns immediately — order is: empty, options, range, count.
    """
    if not symbols:
        return GuardrailResult(
            allowed=False,
            reason="no_symbols",
            human_message="Auto-download disabled — request has no symbols.",
        )

    if asset_class == "options" and not allow_options:
        return GuardrailResult(
            allowed=False,
            reason="options_disabled",
            human_message=(
                "Auto-download disabled for options (OPRA cost + chain-fan-out risk). "
                "Manually scope and run: msai ingest options <strike-scoped-ids> ..."
            ),
            details={"asset_class": asset_class},
        )

    range_years = (end - start).days / 365.25
    if range_years > max_years:
        return GuardrailResult(
            allowed=False,
            reason="range_exceeds_max_years",
            human_message=(
                f"Auto-download disabled — {range_years:.0f}-year range exceeds "
                f"{max_years}-year cap."
            ),
            details={"range_years": int(range_years), "max_years": max_years},
        )

    if len(symbols) > max_symbols:
        return GuardrailResult(
            allowed=False,
            reason="symbol_count_exceeds_max",
            human_message=(
                f"Auto-download disabled — {len(symbols)} symbols exceeds "
                f"{max_symbols}-symbol cap per request."
            ),
            details={"symbol_count": len(symbols), "max_symbols": max_symbols},
        )

    return GuardrailResult(
        allowed=True,
        reason=None,
        human_message="Guardrails passed.",
    )
