"""Resolve ticker symbols to NautilusTrader Instrument objects.

For dev/test we use TestInstrumentProvider equities with the XNAS venue.
For production this should be replaced with a real security-master resolver
or an adapter-backed instrument provider (e.g. IB contract details).
"""

from __future__ import annotations

from nautilus_trader.model.instruments import Instrument
from nautilus_trader.test_kit.providers import TestInstrumentProvider


def resolve_instrument(symbol_or_id: str) -> Instrument:
    """Resolve a raw symbol (e.g. ``AAPL``) or Nautilus ID (e.g. ``AAPL.SIM``)
    to a Nautilus ``Instrument``.

    All instruments are pinned to the ``SIM`` venue so their canonical IDs
    match the ``BacktestVenueConfig(name="SIM")`` used by the backtest runner.
    This avoids the ``Venue 'XNAS' does not have a BacktestVenueConfig`` error
    when Nautilus validates that every instrument's venue has a configured
    simulation venue.
    """
    if "." in symbol_or_id:
        symbol = symbol_or_id.split(".", 1)[0]
    else:
        symbol = symbol_or_id
    return TestInstrumentProvider.equity(symbol=symbol, venue="SIM")


def canonical_instrument_id(symbol_or_id: str) -> str:
    """Return the canonical Nautilus instrument ID string."""
    return str(resolve_instrument(symbol_or_id).id)


def default_bar_type(symbol_or_id: str) -> str:
    """Return a default 1-minute last-external bar type for the instrument."""
    return f"{canonical_instrument_id(symbol_or_id)}-1-MINUTE-LAST-EXTERNAL"
