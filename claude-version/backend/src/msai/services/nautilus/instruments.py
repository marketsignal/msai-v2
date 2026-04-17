"""Resolve ticker symbols to NautilusTrader ``Instrument`` objects
(Phase 2 task 2.6 — SecurityMaster delegation; Task 2.10 — SIM shim removed).

Pre-v9, this module used ``TestInstrumentProvider.equity(venue="SIM")``
and rebound every instrument to the synthetic ``SIM`` venue. Phase 2
replaces that with a real security-master resolver keyed on
canonical IB venues (``NASDAQ`` for equities on Nasdaq, ``CME`` for
CME futures, ``IDEALPRO`` for forex, ...) so backtest and live
trading both see the SAME instrument objects.

The top-level entry point :func:`resolve_instrument` is still
**synchronous** so the existing catalog-builder + backtest-worker
call sites don't need to be rewritten in one sweep. It returns a
Nautilus ``Instrument`` pinned to the real venue passed in the
``venue`` kwarg (default ``NASDAQ`` — the most common equity case).

For async call sites that need the full SecurityMaster path
(cache-first read + IB qualify on miss + trading-hours extraction),
use :class:`msai.services.nautilus.security_master.SecurityMaster`
directly. This module is a thin synchronous wrapper around
``TestInstrumentProvider`` that produces the SAME venue / shape
SecurityMaster returns — so any code that mixes the two paths sees
consistent ``instrument_id`` strings.

Task 2.10 removed the transitional ``legacy_resolve_sim`` shim —
every test fixture that depended on the ``*.SIM`` venue binding
has been migrated to pass a real canonical venue via the
``venue`` kwarg or a dotted instrument ID.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.test_kit.providers import TestInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument


DEFAULT_EQUITY_VENUE = "NASDAQ"
"""Default venue for a bare ticker like ``"AAPL"`` — the most
common case for MSAI's current equity universe. Callers resolving
instruments on other venues pass ``venue=...`` explicitly."""


def resolve_instrument(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> Instrument:
    """Turn a raw ticker symbol (or canonical Nautilus ID) into an
    ``Instrument`` pinned to a real IB venue.

    Accepts either a bare symbol like ``"AAPL"`` (which gets bound
    to ``venue``) or a fully-qualified Nautilus identifier like
    ``"AAPL.NASDAQ"`` (in which case ``venue`` is ignored and the
    suffix in the ID wins — matching SecurityMaster's canonical
    round-trip contract).

    Args:
        symbol_or_id: Ticker symbol (``"AAPL"``) or Nautilus ID
            (``"AAPL.NASDAQ"``, ``"ESM5.CME"``). A dotted
            identifier's suffix wins over ``venue``.
        venue: Venue to bind a bare symbol to. Defaults to
            :data:`DEFAULT_EQUITY_VENUE` (``NASDAQ``).

    Returns:
        A :class:`nautilus_trader.model.instruments.Instrument`
        ready to write into a ``ParquetDataCatalog`` or pass to
        the backtest engine.
    """
    if "." in symbol_or_id:
        raw_symbol, parsed_venue = symbol_or_id.split(".", 1)
        resolved_venue = parsed_venue
    else:
        raw_symbol = symbol_or_id
        resolved_venue = venue
    return TestInstrumentProvider.equity(symbol=raw_symbol, venue=resolved_venue)


def canonical_instrument_id(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> str:
    """Return the canonical Nautilus instrument ID string for a symbol.

    Convenience wrapper around :func:`resolve_instrument` for
    call-sites that only need the ID string (e.g. building
    ``BacktestDataConfig.instrument_ids``).
    """
    return str(resolve_instrument(symbol_or_id, venue=venue).id)


def default_bar_type(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> str:
    """Return the default 1-minute last-external bar type for a symbol.

    MSAI ingests minute bars so the default bar type is hard-wired
    to ``1-MINUTE-LAST-EXTERNAL``. The ``EXTERNAL`` aggregation
    source tells Nautilus the bars are pre-computed (by our
    ingestion pipeline), not built on the fly from tick data.
    """
    return f"{canonical_instrument_id(symbol_or_id, venue=venue)}-1-MINUTE-LAST-EXTERNAL"


# Task 2.10 removed ``legacy_resolve_sim`` — callers must pass a
# real canonical venue via ``resolve_instrument(symbol, venue=...)``
# or supply a dotted instrument ID.
