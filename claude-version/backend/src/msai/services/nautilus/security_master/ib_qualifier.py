"""IB qualification adapter (Phase 2 task 2.3).

Thin wrapper that converts an :class:`InstrumentSpec` into the
Nautilus ``IBContract`` struct and delegates the actual IB round-trip
to Nautilus's own :class:`InteractiveBrokersInstrumentProvider`. Per
the project's "always use Nautilus API, never reinvent" rule, we do
NOT write our own IB connection / contract-details pipeline — Nautilus
already has one in ``nautilus_trader/adapters/interactive_brokers/``.

What this module owns:

1. :func:`spec_to_ib_contract` — PURE function that converts an
   ``InstrumentSpec`` to an ``IBContract``. Fully unit-testable
   without any IB connection, network, or Nautilus runtime.

2. :class:`IBQualifier` — async adapter. Takes an
   ``InteractiveBrokersInstrumentProvider`` (Nautilus object) at
   construction, exposes ``qualify(spec)`` and ``qualify_many(specs)``
   methods that call ``provider.get_instrument(contract)`` under
   the hood. The provider is what actually throttles to IB's
   ≤50 msg/sec limit and handles reconciliation with IB's
   ``reqContractDetails`` response.

Production wiring: the :class:`SecurityMaster` service (Task 2.5)
constructs a short-lived ``InteractiveBrokersInstrumentProvider``
bound to an isolated ``InteractiveBrokersClient`` and passes it to
``IBQualifier``. The provider lives only as long as the qualification
call — it's NOT the same provider the live ``TradingNode`` uses
(gotcha #3: sharing a provider across connections can leak ``conId``
state).

Not yet implemented here: options chain loading, continuous futures
front-month resolution. Those hit different Nautilus methods
(``get_options_chain``, ``CONTFUT`` secType on the contract) and
are covered by Task 2.12 (multi-asset). The Phase 2 acceptance only
requires equity + fixed-month futures + forex, which all go through
``get_instrument``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nautilus_trader.adapters.interactive_brokers.common import IBContract

if TYPE_CHECKING:
    from nautilus_trader.adapters.interactive_brokers.providers import (
        InteractiveBrokersInstrumentProvider,
    )
    from nautilus_trader.model.instruments import Instrument

    from msai.services.nautilus.security_master.specs import InstrumentSpec


# ---------------------------------------------------------------------------
# Pure spec → IBContract conversion
# ---------------------------------------------------------------------------


def spec_to_ib_contract(spec: InstrumentSpec) -> IBContract:
    """Convert an :class:`InstrumentSpec` into a Nautilus
    :class:`IBContract` struct suitable for
    ``InteractiveBrokersInstrumentProvider.get_instrument()``.

    This is a PURE function — no network, no IB connection — so
    it's trivially unit-testable and deterministic. The conversion
    mirrors IB's contract-field contract for each supported
    ``secType``:

    - **STK**: ``symbol``, ``exchange``, ``currency``
    - **IND**: ``symbol`` (caret-stripped), ``exchange``, ``currency``
    - **FUT** (fixed month): ``symbol``, ``exchange``, ``currency``,
      ``lastTradeDateOrContractMonth`` (yyyyMMdd)
    - **CONTFUT** (continuous, ``spec.expiry is None``): ``symbol``,
      ``exchange``, ``currency``
    - **OPT**: ``symbol`` (underlying), ``exchange``, ``currency``,
      ``lastTradeDateOrContractMonth``, ``strike``, ``right``
    - **CASH** (forex): ``symbol`` (base), ``exchange``,
      ``currency`` (quote)

    Raises ``ValueError`` on an unsupported asset_class. The caller
    (:class:`IBQualifier`) does NOT catch this — a bad spec is a
    programming error that should surface to the API layer.
    """
    asset_class = spec.asset_class

    if asset_class == "equity":
        return IBContract(
            secType="STK",
            symbol=spec.symbol,
            exchange=spec.venue,
            currency=spec.currency,
        )

    if asset_class == "index":
        # Nautilus prefixes index symbols with ``^`` in the
        # InstrumentId, but the IB contract itself uses the bare
        # symbol. Strip the leading caret if present.
        symbol = spec.symbol.lstrip("^")
        return IBContract(
            secType="IND",
            symbol=symbol,
            exchange=spec.venue,
            currency=spec.currency,
        )

    if asset_class == "future":
        if spec.expiry is None:
            # Continuous future (CONTFUT) — no expiry field.
            return IBContract(
                secType="CONTFUT",
                symbol=spec.symbol,
                exchange=spec.venue,
                currency=spec.currency,
            )
        # Fixed-month future: IB expects yyyyMMdd in
        # ``lastTradeDateOrContractMonth``.
        return IBContract(
            secType="FUT",
            symbol=spec.symbol,
            exchange=spec.venue,
            currency=spec.currency,
            lastTradeDateOrContractMonth=spec.expiry.strftime("%Y%m%d"),
        )

    if asset_class == "option":
        assert spec.expiry is not None  # validated by InstrumentSpec
        assert spec.strike is not None
        assert spec.right is not None
        assert spec.underlying is not None
        return IBContract(
            secType="OPT",
            symbol=spec.underlying,
            exchange=spec.venue,
            currency=spec.currency,
            lastTradeDateOrContractMonth=spec.expiry.strftime("%Y%m%d"),
            strike=float(spec.strike),
            right=spec.right,
        )

    if asset_class == "forex":
        return IBContract(
            secType="CASH",
            symbol=spec.symbol,
            exchange=spec.venue,
            currency=spec.currency,
        )

    raise ValueError(f"unsupported asset_class: {asset_class!r}")


# ---------------------------------------------------------------------------
# IBQualifier async adapter
# ---------------------------------------------------------------------------


class IBQualifier:
    """Thin async adapter over Nautilus's
    :class:`InteractiveBrokersInstrumentProvider`.

    Responsibilities scoped intentionally:

    - Convert ``InstrumentSpec`` → ``IBContract`` (via
      :func:`spec_to_ib_contract`)
    - Delegate the qualification round-trip to the provider's
      ``get_instrument`` method
    - Iterate for ``qualify_many``

    Non-responsibilities (deliberately delegated to Nautilus):

    - IB connection lifecycle (the provider owns that)
    - Throttling (the client inside the provider owns that)
    - Caching (the provider has an in-memory cache; persistence is
      :class:`SecurityMaster`'s job)
    - Contract-details parsing (the provider calls
      ``parse_instrument`` internally)
    """

    def __init__(self, provider: InteractiveBrokersInstrumentProvider) -> None:
        self._provider = provider

    async def qualify(self, spec: InstrumentSpec) -> Instrument:
        """Qualify a single spec. Raises ``ValueError`` if the
        provider cannot find a matching contract (propagated from
        Nautilus's ``get_instrument``)."""
        contract = spec_to_ib_contract(spec)
        instrument = await self._provider.get_instrument(contract)
        if instrument is None:
            raise ValueError(
                f"Nautilus provider returned None for spec {spec!r} "
                f"(contract={contract!r}) — check filter_sec_types or "
                "IB contract definition"
            )
        return instrument

    async def qualify_many(self, specs: list[InstrumentSpec]) -> list[Instrument]:
        """Qualify a batch of specs in order.

        We iterate sequentially (not ``asyncio.gather``) on purpose:
        IB's ``reqContractDetails`` is rate-limited (≤50 msg/sec),
        and Nautilus's provider enforces that limit at the single-
        request level. Running N concurrent ``get_instrument`` calls
        would just get throttled into the same sequential order
        Nautilus ends up serializing anyway — no speedup, higher
        risk of log spam.
        """
        results: list[Instrument] = []
        for spec in specs:
            results.append(await self.qualify(spec))
        return results
