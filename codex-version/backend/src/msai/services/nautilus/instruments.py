"""Helpers for rebuilding canonical Nautilus instruments from stored payloads."""

from __future__ import annotations

from typing import Any

from nautilus_trader.model.instruments import (
    BettingInstrument,
    BinaryOption,
    Cfd,
    Commodity,
    CryptoFuture,
    CryptoOption,
    CryptoPerpetual,
    CurrencyPair,
    Equity,
    FuturesContract,
    FuturesSpread,
    IndexInstrument,
    Instrument,
    OptionContract,
    OptionSpread,
    PerpetualContract,
    SyntheticInstrument,
)

_INSTRUMENT_TYPES: dict[str, type[Instrument]] = {
    "BettingInstrument": BettingInstrument,
    "BinaryOption": BinaryOption,
    "Cfd": Cfd,
    "Commodity": Commodity,
    "CryptoFuture": CryptoFuture,
    "CryptoOption": CryptoOption,
    "CryptoPerpetual": CryptoPerpetual,
    "CurrencyPair": CurrencyPair,
    "Equity": Equity,
    "FuturesContract": FuturesContract,
    "FuturesSpread": FuturesSpread,
    "IndexInstrument": IndexInstrument,
    "OptionContract": OptionContract,
    "OptionSpread": OptionSpread,
    "PerpetualContract": PerpetualContract,
    "SyntheticInstrument": SyntheticInstrument,
}


def instrument_from_payload(payload: dict[str, Any]) -> Instrument:
    instrument_type = str(payload.get("type", "")).strip()
    instrument_cls = _INSTRUMENT_TYPES.get(instrument_type)
    if instrument_cls is None:
        raise ValueError(f"Unsupported Nautilus instrument type: {instrument_type!r}")
    return instrument_cls.from_dict(payload)


def instrument_to_payload(instrument: Instrument) -> dict[str, Any]:
    return type(instrument).to_dict(instrument)


def default_bar_type(instrument_id: str) -> str:
    return f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
