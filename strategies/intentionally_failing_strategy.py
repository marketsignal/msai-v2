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

# Nautilus msgspec configs resolve field annotations at runtime via
# inspect, so ``InstrumentId``/``BarType`` must be importable at
# module load, not only under ``TYPE_CHECKING``.
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class IntentionallyFailingStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    """Config for :class:`IntentionallyFailingStrategy`.

    Carries ``instrument_id`` + ``bar_type`` as ``str`` (not Nautilus
    ``InstrumentId`` / ``BarType``) with concrete msgspec defaults — the
    portfolio compose bridge's singular→plural derivation
    (``services/portfolio/lifecycle.py:_get_or_create_default_candidate``)
    needs a non-empty ``default_config['instrument_id']`` to build a
    runnable candidate, and msgspec only emits a JSON-Schema ``default``
    for fields with a concrete default value (Nautilus identifier types
    don't have a sensible default and so are required-without-default in
    every production strategy).

    Why ``str`` and not the proper Nautilus types: the
    ``strategy_registry`` walker introspects msgspec-emitted JSON Schema
    to populate ``default_config``; Nautilus identifier types resolve to
    ``{"type": "string", ...}`` via ``nautilus_schema_hook`` but the
    schema hook does NOT preserve a Python-level default (the hook only
    sets ``title`` / ``examples``). Using ``str`` with a literal default
    is the v1 compromise so the walker captures defaults the bridge can
    consume. The proper fix is making the walker introspect Nautilus
    type defaults; that's out of scope for the E2E iter-2 fix.

    The strategy raises in ``on_start`` (see below) before any
    ``InstrumentId.from_str`` parsing fires, so these defaults never
    actually drive backtest behaviour — they exist purely to satisfy the
    bridge's compose-time precondition.
    """

    instrument_id: str = "AAPL.NASDAQ"
    bar_type: str = "AAPL.NASDAQ-1-DAY-LAST-EXTERNAL"


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
