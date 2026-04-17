"""Databento continuous-futures symbology helpers.

Adapted from codex-version ``instrument_service.py:440-451``. The
Databento Python adapter in Nautilus 1.223.0 has no native continuous-
symbol normalization (verified: zero grep hits for
``continuous|\\.c\\.0|\\.Z\\.`` in ``nautilus_trader/adapters/databento/``),
so MSAI fills the gap.

Pattern: ``{root}.{c|Z}.{N}`` -- e.g. ``ES.Z.5`` = ES continuous, 5th
forward-month.
"""

from __future__ import annotations

import re

from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
    InstrumentId,
)

_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def is_databento_continuous_pattern(value: str) -> bool:
    return bool(_DATABENTO_CONTINUOUS_SYMBOL.match(value))


def raw_symbol_from_request(requested: str) -> str:
    value = requested.strip()
    if not value:
        raise ValueError("Instrument ID cannot be empty")
    if is_databento_continuous_pattern(value):
        return value
    if "." in value:
        return str(InstrumentId.from_str(value).symbol.value)
    return value
