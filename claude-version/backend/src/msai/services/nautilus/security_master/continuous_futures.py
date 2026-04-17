"""Databento continuous-futures symbology helpers.

Adapted from codex-version ``instrument_service.py:440-451`` (pattern +
raw-symbol derivation) and ``instrument_service.py:466-605`` (synthesis
+ window helpers). The Databento Python adapter in Nautilus 1.223.0 has
no native continuous-symbol normalization (verified: zero grep hits for
``continuous|\\.c\\.0|\\.Z\\.`` in ``nautilus_trader/adapters/databento/``),
so MSAI fills the gap.

Pattern: ``{root}.{c|Z}.{N}`` -- e.g. ``ES.Z.5`` = ES continuous, 5th
forward-month.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
    InstrumentId,
)

# ``instrument_to_payload`` is the existing parser helper at
# ``security_master/parser.py:171`` (``nautilus_instrument_to_cache_json``),
# re-exported here under the shorter name used by the synthesis logic.
from msai.services.nautilus.security_master.parser import (
    nautilus_instrument_to_cache_json as instrument_to_payload,
)

if TYPE_CHECKING:
    from pathlib import Path

    from nautilus_trader.model.instruments import Instrument

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


@dataclass(frozen=True, slots=True)
class ResolvedInstrumentDefinition:
    """Transport object between :func:`resolved_databento_definition` and the
    caller (``SecurityMaster.resolve_for_backtest``).

    Diverges from codex-version: MSAI uses ``listing_venue``/``routing_venue``
    (per PRD). ``instrument_data`` is NOT carried -- Nautilus's cache DB
    holds payloads. ``contract_details`` is a transport-only dict used
    during synthesis.
    """

    instrument_id: str
    raw_symbol: str
    listing_venue: str
    routing_venue: str
    asset_class: str
    provider: str
    contract_details: dict[str, Any]


def resolved_databento_definition(
    *,
    raw_symbol: str,
    instruments: list[Instrument],
    dataset: str,
    start: str,
    end: str,
    definition_path: str | Path,
) -> ResolvedInstrumentDefinition:
    """Build a synthetic continuous-futures ``ResolvedInstrumentDefinition``
    from a Databento-loaded set of concrete-month instruments.

    Adapted from codex ``instrument_service.py:466-539``. Picks the
    instrument with the latest ``ts_init``/``ts_event`` as the representative.
    """
    matching = [
        inst for inst in instruments if inst.raw_symbol.value == raw_symbol
    ]
    if not matching and is_databento_continuous_pattern(raw_symbol):
        matching = instruments
    if not matching:
        raise ValueError(
            f"Databento definition data for {raw_symbol!r} did not decode "
            "into a Nautilus instrument"
        )

    selected = max(
        matching,
        key=lambda inst: str(
            instrument_to_payload(inst).get("ts_init")
            or instrument_to_payload(inst).get("ts_event")
            or ""
        ),
    )
    payload = instrument_to_payload(selected)
    venue = selected.id.venue.value

    # For continuous patterns, rewrite the ID to the synthetic form
    if is_databento_continuous_pattern(raw_symbol):
        synthetic_id = f"{raw_symbol}.{venue}"
        requested_symbol_for_details: str | None = raw_symbol
    else:
        synthetic_id = str(selected.id)
        requested_symbol_for_details = None

    instrument_type = str(payload.get("type", type(selected).__name__))

    return ResolvedInstrumentDefinition(
        instrument_id=synthetic_id,
        raw_symbol=raw_symbol,
        listing_venue=venue,
        routing_venue=venue,
        asset_class=_asset_class_for_instrument_type(instrument_type),
        provider="databento",
        contract_details={
            "dataset": dataset,
            "schema": "definition",
            "definition_start": start,
            "definition_end": end,
            "definition_file_path": str(definition_path),
            "requested_symbol": requested_symbol_for_details or raw_symbol,
            "underlying_instrument_id": str(selected.id),
            "underlying_raw_symbol": selected.raw_symbol.value,
        },
    )


def _asset_class_for_instrument_type(instrument_type: str) -> str:
    if instrument_type in {"FuturesContract", "FuturesSpread"}:
        return "futures"
    if instrument_type in {"OptionContract", "OptionSpread"}:
        return "option"
    if instrument_type == "CurrencyPair":
        return "fx"
    if instrument_type in {
        "CryptoFuture",
        "CryptoOption",
        "CryptoPerpetual",
        "PerpetualContract",
    }:
        return "crypto"
    return "equity"


def definition_window_bounds_from_details(
    details: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    if not isinstance(details, dict):
        return (None, None)
    s = details.get("definition_start")
    e = details.get("definition_end")
    if not isinstance(s, str) or not isinstance(e, str):
        return (None, None)
    return (s, e)


def continuous_needs_refresh_for_window(
    *,
    cached_start: str | None,
    cached_end: str | None,
    requested_start: str,
    requested_end: str,
) -> bool:
    if cached_start is None or cached_end is None:
        return True
    return requested_start < cached_start or requested_end > cached_end


def raw_continuous_suffix(symbol: str) -> str | None:
    """Return the ``.<letter>.<N>`` suffix of a Databento continuous pattern.

    Example: ``"ES.Z.5" → ".Z.5"``. Returns ``None`` if ``symbol`` is not a
    Databento continuous pattern (lets callers pattern-match without a
    second regex hit).

    Reserved helper — not used by v3.0's ``resolve_for_backtest`` path but
    stored here for a planned follow-up that records the continuous
    suffix on :class:`InstrumentDefinition.continuous_pattern`. The
    shape matches the ``ck_instrument_definitions_continuous_pattern_shape``
    CHECK constraint regex ``^\\.[A-Za-z]\\.[0-9]+$`` on the model.
    """
    if not is_databento_continuous_pattern(symbol):
        return None
    # Pattern matched, so exactly two dots split root/letter/number.
    _, letter, number = symbol.rsplit(".", 2)
    return f".{letter}.{number}"
