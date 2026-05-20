"""Per-strategy P&L attribution via Nautilus Cache.

NautilusTrader's Cache supports a strategy_id filter on positions() —
verified at backend/.venv/lib/python3.12/site-packages/nautilus_trader/cache/base.pyx:282-510.
We read it post-run; no event subscription on the trading path.

Reference: docs/nautilus-natives-audit.md § D, docs/research/2026-05-18-portfolio-backtest.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class _CacheLike(Protocol):
    """Minimal Cache surface we depend on.

    Mirrors the relevant attributes of ``nautilus_trader.cache.Cache``:
    a ``positions(strategy_id=...)`` filter and a ``strategy_ids()`` listing.
    """

    def positions(self, strategy_id: str | None = ...) -> list[Any]:  # pragma: no cover - protocol
        ...

    def strategy_ids(self) -> list[str]:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class PerStrategyPnL:
    """Realized P&L attributed to a single strategy after a backtest run."""

    strategy_id: str
    realized_pnl: float


def extract_per_strategy_pnl(cache: _CacheLike) -> list[PerStrategyPnL]:
    """Iterate strategies in the cache and sum their realized P&L.

    Uses ``cache.strategy_ids()`` as the iteration source (defends against
    rename bugs vs. hard-coding the input list) and ``cache.positions(strategy_id=)``
    as the filter source.
    """
    out: list[PerStrategyPnL] = []
    for sid in cache.strategy_ids():
        total = sum(float(p.realized_pnl) for p in cache.positions(strategy_id=sid))
        out.append(PerStrategyPnL(strategy_id=sid, realized_pnl=total))
    return out
