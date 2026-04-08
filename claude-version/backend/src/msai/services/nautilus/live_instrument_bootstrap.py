"""Phase 1 Interactive Brokers instrument bootstrap.

Returns an :class:`InteractiveBrokersInstrumentProviderConfig` populated
with the contracts the live trading subprocess will subscribe to BEFORE
the run starts. This is the v9 replacement for the
``TestInstrumentProvider`` stub the architecture review flagged as
"multi-asset is fictional".

Two Nautilus gotchas drive the design:

- **Gotcha #9** — an instrument that wasn't pre-loaded fails at the
  first bar event, not at startup. The provider must therefore know
  every instrument the strategy will touch BEFORE ``node.run()`` is
  called.
- **Gotcha #11** — dynamic instrument loading is synchronous and slow
  (one IB round-trip per instrument). Never load on the trading
  critical path. Pre-load everything via ``load_contracts``.

Phase 1 hardcodes a closed AAPL/MSFT universe so the live-supervisor
smoke test can run end-to-end against IB Gateway paper. Phase 2
replaces this with the full SecurityMaster lookup driven by the
``Strategy.instruments`` JSONB column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# Closed Phase 1 universe. The keys are the user-facing symbol strings
# the rest of the codebase passes around (request bodies, strategy config,
# log fields). The values are the fully-qualified IB contracts the
# Nautilus IB adapter will resolve to live ``Instrument`` objects.
#
# Both symbols are NASDAQ common stock, USD-denominated, routed via
# IB's SMART order router (so IB picks the actual venue at execution
# time). ``primaryExchange="NASDAQ"`` is the disambiguator IB needs
# when SMART routing returns multiple matches.
PHASE_1_PAPER_SYMBOLS: dict[str, IBContract] = {
    "AAPL": IBContract(
        secType="STK",
        symbol="AAPL",
        exchange="SMART",
        primaryExchange="NASDAQ",
        currency="USD",
    ),
    "MSFT": IBContract(
        secType="STK",
        symbol="MSFT",
        exchange="SMART",
        primaryExchange="NASDAQ",
        currency="USD",
    ),
}


def build_ib_instrument_provider_config(
    symbols: Iterable[str],
) -> InteractiveBrokersInstrumentProviderConfig:
    """Build a Nautilus IB instrument provider config for the given symbols.

    Every requested symbol must be present in :data:`PHASE_1_PAPER_SYMBOLS`;
    the function raises ``ValueError`` (with the list of known symbols
    in the error message) if any are unknown. We never silently drop
    instruments — typos must fail at config-build time, not at the
    first bar event.

    Args:
        symbols: User-facing symbol strings (e.g. ``["AAPL", "MSFT"]``).
            Order is irrelevant; duplicates are deduped via the frozenset.

    Returns:
        An :class:`InteractiveBrokersInstrumentProviderConfig` ready to
        hand to ``InteractiveBrokersDataClientConfig`` /
        ``InteractiveBrokersExecClientConfig``.

    Raises:
        ValueError: If any symbol is not in ``PHASE_1_PAPER_SYMBOLS``.
            The message lists the supported symbols so an operator can
            fix the typo without grepping the source.
    """
    requested = list(symbols)
    unknown = [s for s in requested if s not in PHASE_1_PAPER_SYMBOLS]
    if unknown:
        known = ", ".join(sorted(PHASE_1_PAPER_SYMBOLS))
        raise ValueError(
            f"Symbols {unknown} not registered in PHASE_1_PAPER_SYMBOLS. Known symbols: {known}"
        )

    contracts = frozenset(PHASE_1_PAPER_SYMBOLS[s] for s in requested)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )
