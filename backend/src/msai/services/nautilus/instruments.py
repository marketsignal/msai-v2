"""Resolve ticker symbols to NautilusTrader ``Instrument`` objects.

Synchronous wrapper around ``TestInstrumentProvider`` for catalog-builder
+ backtest-worker call sites that don't need the full async
:class:`SecurityMaster` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.test_kit.providers import TestInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


DEFAULT_EQUITY_VENUE = "NASDAQ"
"""Default venue for a bare ticker. Callers resolving instruments on
other venues pass ``venue=...`` explicitly."""


def resolve_instrument(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> Instrument:
    """Turn a raw ticker symbol (or canonical Nautilus ID) into an
    ``Instrument`` pinned to a real IB venue.

    Accepts either a bare symbol like ``"AAPL"`` or a fully-qualified
    Nautilus identifier like ``"AAPL.NASDAQ"``. A dotted identifier's
    suffix wins over ``venue``.
    """
    if "." in symbol_or_id:
        raw_symbol, parsed_venue = symbol_or_id.split(".", 1)
        resolved_venue = parsed_venue
    else:
        raw_symbol = symbol_or_id
        resolved_venue = venue
    return TestInstrumentProvider.equity(symbol=raw_symbol, venue=resolved_venue)


def default_bar_type(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> str:
    """Return the default 1-minute last-external bar type for a symbol.

    MSAI ingests minute bars; the bar type is hard-wired to
    ``1-MINUTE-LAST-EXTERNAL``.
    """
    return f"{resolve_instrument(symbol_or_id, venue=venue).id}-1-MINUTE-LAST-EXTERNAL"
