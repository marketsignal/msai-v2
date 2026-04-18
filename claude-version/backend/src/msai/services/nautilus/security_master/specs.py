"""Immutable :class:`InstrumentSpec` value object (Phase 2 task 2.1).

An :class:`InstrumentSpec` describes an instrument MSAI wants to trade
or backtest at the **logical** level — "AAPL equity on NASDAQ",
"ESM5 continuous future on CME", "AAPL 2026-05-15 150 call on SMART".
It is a source-of-truth input to
:class:`msai.services.nautilus.security_master.SecurityMaster`, which
resolves it against IB and returns the concrete Nautilus
``Instrument`` object.

``canonical_id()`` returns the Nautilus ``InstrumentId`` string in
IB simplified symbology (the default for Nautilus 1.223.0's IB
adapter — ``nautilus_trader/adapters/interactive_brokers/config.py``
``SymbologyMethod.IB_SIMPLIFIED``). The format is what
``ib_contract_to_instrument_id_simplified_symbology`` in
``adapters/interactive_brokers/parsing/instruments.py`` produces for
each security type:

- **STK**: ``AAPL.NASDAQ`` — plain symbol on the venue
- **IND**: ``^SPX.CBOE`` — caret prefix for cash indexes
- **FUT** (fixed month): ``ESM5.CME`` — ``{root}{month_code}{year_digit}``
- **CONTFUT** (continuous): ``ES.CME`` — just the root
- **OPT**: ``C AAPL 20260515 150.XSMART`` —
  ``{right} {tradingClass} {yyyyMMdd} {strike:g}`` on the venue
- **CASH**/**CRYPTO**: ``EUR/USD.IDEALPRO`` — slash-separated pair

The spec validates required fields at construction time. Invalid
combinations (e.g. an option without a strike) raise ``ValueError``
so a caller can't construct an unresolvable spec.

Why a frozen slotted dataclass: the spec is a hash key in the
in-memory cache layer and a primary-key source for the
``instrument_cache`` DB table, so equality + hashability must be
stable across runs. Slots keep the memory footprint tiny for bulk
resolve paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date  # noqa: TC003 — dataclass annotation resolved at runtime
from decimal import Decimal  # noqa: TC003 — dataclass annotation resolved at runtime
from typing import Literal

AssetClass = Literal["equity", "future", "option", "forex", "index"]
"""The MSAI-side asset-class taxonomy. Maps to Nautilus sec_types:

- ``equity`` → STK
- ``future`` → FUT (fixed) or CONTFUT (continuous, when ``expiry`` is ``None``)
- ``option`` → OPT
- ``forex`` → CASH
- ``index`` → IND
"""

OptionRight = Literal["C", "P"]
"""Call or Put — matches the Nautilus ``OptionKind`` single-letter codes."""


_FUT_MONTH_CODES: dict[int, str] = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}
"""CME/ICE futures-month letter codes. Shared across all futures exchanges."""


@dataclass(slots=True, frozen=True)
class InstrumentSpec:
    """Logical description of an instrument MSAI wants to resolve.

    Fields:
        asset_class: One of ``equity``/``future``/``option``/``forex``/``index``.
        symbol: Root ticker / underlying (e.g. ``AAPL``, ``ES``, ``SPX``,
            ``EUR``). For forex, the base currency.
        venue: IB exchange acronym used as the Nautilus ``Venue``
            (e.g. ``NASDAQ``, ``CME``, ``SMART``, ``IDEALPRO``). MUST
            match the ``Venue`` Nautilus derives from the resolved
            contract — see gotcha #4.
        currency: Quote currency. Defaults to USD. For forex this is
            the quote side of the pair.
        expiry: Required for options and fixed-month futures. A
            ``future`` spec with ``expiry=None`` resolves to the
            continuous (CONTFUT) contract.
        strike: Required for options.
        right: Required for options.
        underlying: Required for options — the root symbol of the
            underlying (e.g. ``AAPL`` for an AAPL option). For
            options on an index, this is the underlying index
            symbol (e.g. ``SPX``).
        multiplier: Contract multiplier. Optional for most assets
            (Nautilus reads the authoritative value from IB contract
            details at qualify time); reserved for scenarios where
            the caller wants to pin a specific multiplier.
    """

    asset_class: AssetClass
    symbol: str
    venue: str
    currency: str = "USD"
    expiry: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None
    underlying: str | None = None
    multiplier: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate asset-class-specific field requirements.

        Frozen dataclasses still run ``__post_init__``. We raise
        ``ValueError`` for any combination that can't resolve to a
        concrete IB contract so a broken spec never escapes the
        constructor.
        """
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.venue:
            raise ValueError("venue is required")

        if self.asset_class == "option":
            if self.expiry is None:
                raise ValueError("option spec requires an expiry")
            if self.strike is None:
                raise ValueError("option spec requires a strike")
            if self.right is None:
                raise ValueError("option spec requires a right (C or P)")
            if self.underlying is None:
                raise ValueError("option spec requires an underlying symbol")

        if self.asset_class == "future" and self.expiry is None:
            # Continuous future — allowed. No extra required fields.
            pass

        if self.asset_class == "forex" and not self.currency:
            # Forex pairs are ``{base}/{quote}``; ``symbol`` is the base,
            # ``currency`` is the quote. Both must be set and non-empty.
            raise ValueError("forex spec requires a (quote) currency")

        if self.asset_class in ("equity", "index") and (
            self.strike is not None or self.right is not None
        ):
            # No extra required fields, but reject stray option-only ones.
            raise ValueError(
                f"{self.asset_class} spec cannot have strike/right — those are option-only fields",
            )

    def canonical_id(self) -> str:
        """Return the Nautilus ``InstrumentId`` string in IB simplified
        symbology.

        The format mirrors
        ``ib_contract_to_instrument_id_simplified_symbology`` in
        ``nautilus_trader/adapters/interactive_brokers/parsing/instruments.py``
        — so a spec round-trips through
        :class:`SecurityMaster` and comes back with the same
        ``instrument_id`` Nautilus produces from the IB contract
        details. Downstream code keys on this string directly
        (e.g. ``instrument_cache.canonical_id`` PK).
        """
        if self.asset_class == "equity":
            return f"{self.symbol}.{self.venue}"

        if self.asset_class == "index":
            # Nautilus prefixes index symbols with ``^`` (see
            # ``parsing/instruments.py`` line 1062). Idempotent if
            # the caller already supplied a caret.
            sym = self.symbol if self.symbol.startswith("^") else f"^{self.symbol}"
            return f"{sym}.{self.venue}"

        if self.asset_class == "future":
            if self.expiry is None:
                # Continuous future — just the root on the venue.
                return f"{self.symbol}.{self.venue}"
            month_code = _FUT_MONTH_CODES[self.expiry.month]
            # Nautilus uses a single-digit year for IB-style simplified
            # symbology (``m['year'][-1]`` in
            # ``parsing/instruments.py`` line 1081).
            year_digit = str(self.expiry.year)[-1]
            return f"{self.symbol}{month_code}{year_digit}.{self.venue}"

        if self.asset_class == "option":
            # ``{right} {underlying} {yyyyMMdd} {strike:g}.{venue}``
            # matches the OPT branch at parsing/instruments.py:1068.
            assert self.expiry is not None  # validated in __post_init__
            assert self.strike is not None
            assert self.right is not None
            assert self.underlying is not None
            strike_str = f"{float(self.strike):g}"
            expiry_str = self.expiry.strftime("%Y%m%d")
            return f"{self.right} {self.underlying} {expiry_str} {strike_str}.{self.venue}"

        if self.asset_class == "forex":
            # ``{base}/{quote}.{venue}`` matches the CASH branch at
            # parsing/instruments.py:1085.
            return f"{self.symbol}/{self.currency}.{self.venue}"

        raise ValueError(f"unknown asset_class: {self.asset_class!r}")
