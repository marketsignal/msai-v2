"""Provider-scoped venue normalization at the registry write boundary.

Nautilus's ``DatabentoDataLoader.from_dbn_file(use_exchange_as_venue=True)``
emits MIC venue codes (``AAPL.XNAS``, ``SPY.XARC``) while IB's adapter
emits exchange-name venues (``AAPL.NASDAQ``, ``SPY.ARCA``). The registry
stores the exchange-name convention; live-start's ``lookup_for_live``
does exact-match on ``alias_string``, so a Databento-only bootstrap row
in MIC form would be invisible to the live-start resolver.

This helper runs at the write boundary (called by
``_upsert_definition_and_alias``) and translates ``provider="databento"``
aliases into the registry's canonical exchange-name convention. The raw
Databento venue is preserved separately in
``instrument_aliases.source_venue_raw`` as the lineage-preserving column.

Unknown MICs FAIL LOUDLY (``UnknownDatabentoVenueError``) — silent
passthrough would recreate the invisible-row failure mode for new venues.
"""

from __future__ import annotations


class UnknownDatabentoVenueError(ValueError):
    """Databento alias contains a MIC not in the provider-scoped map.

    Raised at the registry write boundary when the bootstrap path tries
    to store an alias whose venue suffix isn't in
    ``_DATABENTO_MIC_TO_EXCHANGE_NAME``. Fail-loud is mandatory here:
    silent passthrough on an unknown MIC would write a row that
    ``lookup_for_live`` exact-match can never find. To resolve the error,
    extend the map with the new MIC→exchange-name entry and add a
    matching unit test.
    """


# Closed enumeration of Databento MIC codes → IB exchange-name equivalents.
_DATABENTO_MIC_TO_EXCHANGE_NAME: dict[str, str] = {
    # Primary equity venues
    "XNAS": "NASDAQ",
    "XNYS": "NYSE",
    "XARC": "ARCA",
    "ARCX": "ARCA",
    "XASE": "AMEX",
    # Cboe family
    "BATS": "BATS",
    "BATY": "BATY",
    "EDGA": "EDGA",
    "EDGX": "EDGX",
    # Other equity venues
    "IEXG": "IEX",
    "XBOS": "BOSTON",
    "XPSX": "PSX",
    "XCHI": "CHX",
    "XCIS": "NSX",
    "MEMX": "MEMX",
    "EPRL": "PEARL",
    # Futures
    "GLBX": "CME",
}


def normalize_alias_for_registry(provider: str, alias_string: str) -> str:
    """Return the ``alias_string`` the registry should store for this provider.

    For ``provider="databento"``: splits on the LAST ``.`` to extract the
    venue segment, looks it up in the closed MIC map, rebuilds
    ``{symbol}.{exchange_name}``. Symbol is preserved verbatim (including
    internal dots like ``BRK.B``).

    For ``provider != "databento"``: passthrough — IB and other providers
    already emit the registry's canonical convention.

    Raises ``UnknownDatabentoVenueError`` on unknown MIC or missing suffix.
    """
    if provider != "databento":
        return alias_string
    if "." not in alias_string:
        raise UnknownDatabentoVenueError(
            f"Databento alias {alias_string!r} has no venue suffix (expected '{{symbol}}.{{MIC}}')."
        )
    symbol, _, mic = alias_string.rpartition(".")
    exchange_name = _DATABENTO_MIC_TO_EXCHANGE_NAME.get(mic)
    if exchange_name is None:
        raise UnknownDatabentoVenueError(
            f"Databento alias {alias_string!r} has unmapped MIC {mic!r}. "
            f"Extend _DATABENTO_MIC_TO_EXCHANGE_NAME in "
            f"backend/src/msai/services/nautilus/security_master/"
            f"venue_normalization.py and add a test, then retry."
        )
    return f"{symbol}.{exchange_name}"
