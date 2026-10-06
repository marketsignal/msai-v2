"""Test fixture strategy — raises on every ``on_start`` call.

Used by ``tests/e2e/use-cases/.../UC-PB-NEG-001`` (and any other
negative-path use case that needs a deterministic backtest failure).
Drop this file in ``strategies/`` and the filesystem-based strategy
walker (``services/strategy_registry.discover_strategies``) picks it up
automatically — no DB seed needed.

**Do NOT** deploy this strategy to live trading.  The supervisor's
``FailureIsolatedStrategy`` base class would catch the runtime error
and quarantine the strategy, but a paper deployment is still a waste
of broker round-trips and clutters the audit log.
"""

from __future__ import annotations

# Native imports are shared by discovery and the execution subprocess.
from nautilus_trader.model import InstrumentId
from nautilus_trader.trading import Strategy, StrategyConfig


class IntentionallyFailingStrategyConfig(StrategyConfig):
    """Keep concrete string defaults used by the candidate compose bridge.

    These defaults arrange the intentionally failing fixture; on_start always
    raises and this strategy is never a supported live deployment.
    """

    def __init__(
        self,
        instrument_id: str = "AAPL.NASDAQ",
        bar_type: str = "AAPL.NASDAQ-1-DAY-LAST-EXTERNAL",
        order_id_tag: str | None = None,
    ) -> None:
        super().__init__()
        self.instrument_id = instrument_id
        self.bar_type = bar_type


class IntentionallyFailingStrategy(Strategy):
    """Strategy that raises ``RuntimeError`` on every ``on_start`` call.

    Designed for negative-path E2E tests: any backtest / portfolio run
    that includes this strategy MUST surface the failure as a
    per-member attribution error (Quick mode) or an unhandled-trial
    failure (Full mode).  Tests that assert on the failure path use
    this strategy as the deterministic crash source — any production
    strategy could theoretically work, but the failure shape would
    drift over time.
    """

    def __init__(self, config: IntentionallyFailingStrategyConfig) -> None:
        super().__init__(config=config)
        # Parse the string config fields into the Nautilus identifier
        # types the base Strategy expects. Wrapped in try/except because
        # ``on_start`` is the deterministic crash point; if a malformed
        # ``instrument_id`` string slipped in we'd rather crash here
        # than mask the test's intentional failure.
        self.instrument_id: InstrumentId = InstrumentId.from_str(config.instrument_id)

    def on_start(self) -> None:
        """Raise immediately so the runner attributes the failure to this strategy.

        Raising in ``on_start`` (rather than ``on_bar``) is the canonical
        early-failure path — the BacktestNode hasn't started its bar
        replay loop yet, so the error surfaces in the most-attributable
        position (no ambiguity about whether a bar event was processed
        partially).
        """
        msg = "intentional failure for E2E test"
        raise RuntimeError(msg)
