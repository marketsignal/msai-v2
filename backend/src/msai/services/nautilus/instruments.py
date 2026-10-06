"""Explicit native USD Equity metadata for the supported research catalog."""

from __future__ import annotations

from nautilus_trader.model import Currency, Equity, InstrumentId, Price, Quantity, Symbol

DEFAULT_EQUITY_VENUE = "NASDAQ"
"""Default venue for a bare ticker. Callers resolving instruments on
other venues pass ``venue=...`` explicitly."""

_EQUITY_VENUES = frozenset({"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "IEX", "SMART", "SIM"})


def resolve_instrument(
    symbol_or_id: str,
    *,
    venue: str = DEFAULT_EQUITY_VENUE,
) -> Equity:
    """Turn a raw ticker symbol (or canonical Nautilus ID) into an
    ``Instrument`` for minute-equity research. This does not qualify it for IB.

    Accepts either a bare symbol like ``"AAPL"`` or a fully-qualified
    Nautilus identifier like ``"AAPL.NASDAQ"``. A dotted identifier's
    suffix wins over ``venue``.
    """
    if "." in symbol_or_id:
        raw_symbol, parsed_venue = symbol_or_id.rsplit(".", 1)
        resolved_venue = parsed_venue
    else:
        raw_symbol = symbol_or_id
        resolved_venue = venue
    if resolved_venue not in _EQUITY_VENUES:
        raise ValueError(
            "V2 research supports minute equities on supported USD equity venues; "
            f"{resolved_venue!r} is unsupported",
        )
    if not raw_symbol or "/" in raw_symbol or "\\" in raw_symbol or raw_symbol in {".", ".."}:
        raise ValueError("V2 research requires a valid equity ticker")
    return Equity(
        instrument_id=InstrumentId.from_str(f"{raw_symbol}.{resolved_venue}"),
        raw_symbol=Symbol(raw_symbol),
        currency=Currency.from_str("USD"),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


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
