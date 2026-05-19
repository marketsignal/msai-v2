"""Portfolio service package.

Split out of the legacy ``portfolio_service.py`` monolith per the Maintainer's
council ruling (2026-05-17). The new layout separates orchestration from
lifecycle CRUD and pure-computation helpers so adding optimization + walk-forward
in Task E1 does not balloon a single file past ~1500 LOC.

Public re-exports — the surface every external caller relies on.
"""

from msai.services.portfolio.orchestration import (
    PortfolioOrchestrationError,
    PortfolioRunMemberFailureError,
    PortfolioRunTerminalStateError,
    PortfolioService,
)

__all__ = [
    "PortfolioOrchestrationError",
    "PortfolioRunMemberFailureError",
    "PortfolioRunTerminalStateError",
    "PortfolioService",
]
