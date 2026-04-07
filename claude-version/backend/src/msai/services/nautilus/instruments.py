"""Resolve ticker symbols to NautilusTrader ``Instrument`` objects.

MSAI v2 uses NautilusTrader's backtest engine end-to-end for historical
simulations.  Nautilus requires every bar and order to be associated with a
real ``Instrument`` object that declares things like price precision, size
increment, tick size, and -- crucially -- a venue identifier.

For the development / backtest path we build synthetic equity instruments
via :class:`TestInstrumentProvider`.  This keeps the runtime dependency
surface small (no live security-master call) while still producing fully
functional instruments that NautilusTrader can validate against the
simulated venue configuration.

Production live trading will replace this with a real security-master
resolver (e.g. an IB contract-details adapter), but the interface -- one
function that turns a raw symbol into an ``Instrument`` -- stays the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.test_kit.providers import TestInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument

# Canonical simulated venue.  Every instrument and every ``BacktestVenueConfig``
# the runner emits must agree on this name, otherwise Nautilus will raise
# ``Venue '<X>' does not have a BacktestVenueConfig`` during pre-flight checks.
_SIM_VENUE = "SIM"


def resolve_instrument(symbol_or_id: str) -> Instrument:
    """Turn a raw ticker symbol (or canonical Nautilus ID) into an ``Instrument``.

    Accepts either a bare symbol like ``"AAPL"`` or a fully-qualified
    Nautilus identifier like ``"AAPL.SIM"``.  In both cases the returned
    instrument is pinned to the :data:`_SIM_VENUE` simulated venue so that
    its canonical ID matches the ``BacktestVenueConfig(name="SIM", ...)``
    used by :class:`msai.services.nautilus.backtest_runner.BacktestRunner`.

    Args:
        symbol_or_id: Ticker symbol (``"AAPL"``) or Nautilus ID
            (``"AAPL.SIM"``, ``"AAPL.XNAS"``).  If a dotted identifier is
            supplied the venue suffix is stripped -- we always rebind to
            ``SIM`` to guarantee consistency with the backtest venue config.

    Returns:
        A :class:`nautilus_trader.model.instruments.Instrument` ready to be
        written into a ``ParquetDataCatalog`` or passed to the
        backtest engine.
    """
    raw_symbol = symbol_or_id.split(".", 1)[0] if "." in symbol_or_id else symbol_or_id
    return TestInstrumentProvider.equity(symbol=raw_symbol, venue=_SIM_VENUE)


def canonical_instrument_id(symbol_or_id: str) -> str:
    """Return the canonical Nautilus instrument ID string for a symbol.

    This is a convenience wrapper around :func:`resolve_instrument` for
    call-sites that only need the ID string (e.g. building
    ``BacktestDataConfig.instrument_ids``).

    Args:
        symbol_or_id: Ticker or Nautilus ID.

    Returns:
        The canonical instrument ID, e.g. ``"AAPL.SIM"``.
    """
    return str(resolve_instrument(symbol_or_id).id)


def default_bar_type(symbol_or_id: str) -> str:
    """Return the default 1-minute last-external bar type for a symbol.

    MSAI ingests minute bars so the default bar type is hard-wired to
    ``1-MINUTE-LAST-EXTERNAL``.  The ``EXTERNAL`` aggregation source tells
    Nautilus the bars are pre-computed (by our ingestion pipeline), not
    built on the fly from tick data.

    Args:
        symbol_or_id: Ticker or Nautilus ID.

    Returns:
        A bar-type string like ``"AAPL.SIM-1-MINUTE-LAST-EXTERNAL"``.
    """
    return f"{canonical_instrument_id(symbol_or_id)}-1-MINUTE-LAST-EXTERNAL"
