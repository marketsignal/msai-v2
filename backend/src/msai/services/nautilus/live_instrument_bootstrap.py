"""Phase 1 Interactive Brokers instrument bootstrap.

Returns an :class:`InteractiveBrokersInstrumentProviderConfig` populated
with the contracts the live trading subprocess will subscribe to BEFORE
the run starts. This is the v9 replacement for the
``TestInstrumentProvider`` stub the architecture review flagged as
"multi-asset is fictional".

Two Nautilus gotchas drive the design:

- **Gotcha #9** — an instrument that wasn't pre-loaded fails at the
  first bar event, not at startup. The provider must therefore know
  every instrument the strategy will touch BEFORE ``node.run()`` is
  called.
- **Gotcha #11** — dynamic instrument loading is synchronous and slow
  (one IB round-trip per instrument). Never load on the trading
  critical path. Pre-load everything via ``load_contracts``.

Phase 1 hardcodes a closed AAPL/MSFT universe so the live-supervisor
smoke test can run end-to-end against IB Gateway paper. Phase 2
replaces this with the full SecurityMaster lookup driven by the
``Strategy.instruments`` JSONB column.

.. deprecated::
    The front-month rollover + provider-config helpers here remain
    load-bearing for:
    (1) the live-supervisor payload factory,
    (2) ``live_node_config.build_ib_instrument_provider_config``.
    Both migrate to registry-driven resolution in the follow-up
    live-wiring PR.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from msai.services.nautilus.security_master.live_resolver import (
        ResolvedInstrument,
    )


# CME local timezone. Quarterly futures contracts roll on the third
# Friday of the expiry month in the exchange's local calendar. Using
# UTC here would mis-roll late-Thursday-evening Chicago spawns (DST
# pushes Chicago past midnight UTC at 19:00 local) — the operator-
# facing roll happens at Chicago midnight, not UTC midnight.
_CME_TZ = ZoneInfo("America/Chicago")


def exchange_local_today() -> date:
    """Return the current date in CME's exchange-local timezone.

    Public so callers (e.g., the live-supervisor) can compute the date
    ONCE per spawn and thread it through :func:`phase_1_paper_symbols`
    — guaranteeing the futures front-month is stable even if a
    quarterly roll happens in the middle of provisioning a deployment.
    """
    return datetime.now(_CME_TZ).date()


def third_friday_of(year: int, month: int) -> date:
    """Return the third Friday of ``(year, month)`` — the CME monthly
    futures expiration convention.

    Public so both :func:`current_quarterly_expiry` (below) and CLI
    quarterly-futures factories can reuse the same arithmetic without
    re-implementing it.
    """
    first = date(year, month, 1)
    first_friday_offset = (4 - first.weekday()) % 7
    return first + timedelta(days=first_friday_offset + 14)


def current_quarterly_expiry(today: date) -> str:
    """Return the next quarterly futures expiry as ``YYYYMM``.

    Used for CME E-mini index futures (ES, NQ, RTY, YM) which all expire
    on the third Friday of March, June, September, and December. Returns
    the nearest quarterly month whose third-Friday expiry is STRICTLY
    AFTER ``today`` — so April 15 returns ``202606`` (June), and the 3rd
    Friday of June itself already rolls forward to ``202609``.

    **Rollover-day tradeoff (date-precision, not hour-precision):** The
    ES contract actually trades until 09:30 ET on the third Friday. A
    deployment started between 00:00 CT and 08:30 CT on that Friday
    will see this helper return the NEXT quarter — subscribing to the
    September contract while June is still tradable for a few more
    hours. That is the intentional conservative choice: a deployment
    started later that Friday (after 09:30 ET) would otherwise use the
    now-expired contract and produce zero bars for the rest of the
    session. Rolling early (benign) beats rolling late (dead).

    Operators who need the expiring contract on Friday morning should
    start deployments before midnight CT Thursday, or pin the explicit
    fully-qualified month id (e.g., ``ESM6.CME``) in deployment config.

    IB rejects FUT contracts without an explicit
    ``lastTradeDateOrContractMonth`` as ambiguous ("Unable to resolve
    contract details").
    """
    for months_ahead in range(12):
        year = today.year + (today.month + months_ahead - 1) // 12
        month = (today.month + months_ahead - 1) % 12 + 1
        if month % 3 != 0:
            continue
        if third_friday_of(year, month) > today:
            return f"{year}{month:02d}"
    raise RuntimeError("Unreachable: no quarterly expiry within 12 months")


# IB futures month codes (CME quarterly cycle). These are the single-
# letter codes IB embeds in ``localSymbol`` — e.g., ``ESM6`` = ES + June
# (M) + 2026 (last digit). Nautilus parses ``localSymbol`` directly to
# derive its ``InstrumentId``, so we need to compute the same string
# here to match what the cache will hold after IB resolves the contract.
_FUT_MONTH_CODES: dict[int, str] = {3: "H", 6: "M", 9: "U", 12: "Z"}


# Stable Phase 1 universe — stocks, ETF, and FX contracts don't depend
# on wall-clock time, so we precompute them at import. The futures entry
# rolls quarterly and is appended fresh by :func:`phase_1_paper_symbols`.
#
# Stocks route via IB's SMART order router; ``primaryExchange`` is the
# disambiguator IB needs when SMART returns multiple matches. EUR/USD
# routes via IDEALPRO (IB's FX venue) and trades 24h — useful for
# after-hours smoke testing.
_STATIC_SYMBOLS: dict[str, IBContract] = {
    "AAPL": IBContract(
        secType="STK",
        symbol="AAPL",
        exchange="SMART",
        primaryExchange="NASDAQ",
        currency="USD",
    ),
    "MSFT": IBContract(
        secType="STK",
        symbol="MSFT",
        exchange="SMART",
        primaryExchange="NASDAQ",
        currency="USD",
    ),
    "EUR/USD": IBContract(
        secType="CASH",
        symbol="EUR",
        exchange="IDEALPRO",
        currency="USD",
    ),
    "SPY": IBContract(
        secType="STK",
        symbol="SPY",
        exchange="SMART",
        primaryExchange="ARCA",
        currency="USD",
    ),
}


def phase_1_paper_symbols(*, today: date | None = None) -> dict[str, IBContract]:
    """Return the user-facing symbol → IBContract mapping.

    ES front-month is computed fresh on every call so quarterly rolls
    don't require a worker restart. Stocks/ETF/FX come from
    :data:`_STATIC_SYMBOLS` unchanged.

    The returned dict is safe to mutate — each call builds a new dict so
    callers can't corrupt the module-level state.

    Args:
        today: Exchange-local date to use for the futures front-month
            lookup. Callers should compute the date once per spawn (via
            :func:`exchange_local_today`) and thread the same value
            through any subsequent symbol-resolution calls.
    """
    resolved_today = today if today is not None else exchange_local_today()
    return {
        **_STATIC_SYMBOLS,
        # E-mini S&P 500 futures (CME Globex, ~23h/day Sun-Fri).
        # ``exchange="CME"`` is IB's canonical name (not ``GLOBEX``).
        # ``lastTradeDateOrContractMonth`` MUST be set — IB rejects
        # FUT contracts without an expiry as ambiguous.
        "ES": IBContract(
            secType="FUT",
            symbol="ES",
            exchange="CME",
            lastTradeDateOrContractMonth=current_quarterly_expiry(resolved_today),
            currency="USD",
        ),
    }


# Back-compat module-level dict for test introspection. Production code
# paths call ``phase_1_paper_symbols()`` to get a fresh snapshot.
PHASE_1_PAPER_SYMBOLS: dict[str, IBContract] = phase_1_paper_symbols()


def build_ib_instrument_provider_config(
    symbols: Iterable[str],
    *,
    today: date | None = None,
) -> InteractiveBrokersInstrumentProviderConfig:
    """Build a Nautilus IB instrument provider config for the given symbols.

    Every requested symbol must be present in :data:`PHASE_1_PAPER_SYMBOLS`;
    the function raises ``ValueError`` (with the list of known symbols
    in the error message) if any are unknown. We never silently drop
    instruments — typos must fail at config-build time, not at the
    first bar event.

    Args:
        symbols: User-facing symbol strings (e.g. ``["AAPL", "MSFT"]``).
            Order is irrelevant; duplicates are deduped via the frozenset.
        today: Exchange-local date to use for futures rollover. Callers
            that have already computed a spawn-scoped ``today`` should
            pass it through so both the canonical instrument_id and the
            preloaded IB contract agree on the same front-month.

    Returns:
        An :class:`InteractiveBrokersInstrumentProviderConfig` ready to
        hand to ``InteractiveBrokersDataClientConfig`` /
        ``InteractiveBrokersExecClientConfig``.

    Raises:
        ValueError: If any symbol is not in ``PHASE_1_PAPER_SYMBOLS``.
            The message lists the supported symbols so an operator can
            fix the typo without grepping the source.
    """
    requested = list(symbols)
    symbols_map = phase_1_paper_symbols(today=today)
    unknown = [s for s in requested if s not in symbols_map]
    if unknown:
        known = ", ".join(sorted(symbols_map))
        raise ValueError(
            f"Symbols {unknown} not registered in PHASE_1_PAPER_SYMBOLS. Known symbols: {known}"
        )

    contracts = frozenset(symbols_map[s] for s in requested)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )


# Known IB contract kwargs for ``_ibcontract_from_spec``. The IB adapter
# rejects unknown kwargs with ``TypeError``, so we whitelist the fields
# our ``ResolvedInstrument.contract_spec`` producers emit today (equity,
# FX, futures) and will emit tomorrow (options extension adds new keys;
# we filter them out here so the Phase 1 path tolerates forward-compat
# rows). Keep in sync with
# ``live_resolver._build_contract_spec`` when new asset classes are wired.
_IB_CONTRACT_KWARGS: frozenset[str] = frozenset(
    {
        "secType",
        "symbol",
        "exchange",
        "primaryExchange",
        "currency",
        "lastTradeDateOrContractMonth",
    }
)


def _ibcontract_from_spec(spec: dict[str, object]) -> IBContract:
    """Build an :class:`IBContract` from a ``ResolvedInstrument.contract_spec``.

    Filters unknown keys so an options extension (expiry / strike /
    right) can land in ``contract_spec`` without breaking the Phase 1
    equity/FX/futures preload path. The IB adapter would otherwise raise
    ``TypeError: unexpected keyword argument`` on any unknown kwarg.
    """
    filtered = {k: v for k, v in spec.items() if k in _IB_CONTRACT_KWARGS}
    return IBContract(**filtered)  # type: ignore[arg-type]


def build_ib_instrument_provider_config_from_resolved(
    resolved: list[ResolvedInstrument],
) -> InteractiveBrokersInstrumentProviderConfig:
    """Build a Nautilus IB instrument provider config from resolved rows.

    Consumes the output of
    :func:`msai.services.nautilus.security_master.live_resolver.lookup_for_live`
    — the registry-driven path used by the live-supervisor. Unlike
    :func:`build_ib_instrument_provider_config` there is NO closed-universe
    gate; any well-formed ``contract_spec`` is accepted. The resolver is
    already the validator for Phase 1.

    The configuration structure matches the legacy builder
    (``IB_SIMPLIFIED`` symbology, ``cache_validity_days=1``) so both
    paths produce identical provider configs and the subprocess cannot
    tell which builder fed it.

    Args:
        resolved: Output of ``lookup_for_live``. May be empty — the
            supervisor's payload factory is responsible for guarding
            "no instruments" at the aggregation layer (Task 11's
            :func:`build_portfolio_trading_node_config`).

    Returns:
        An :class:`InteractiveBrokersInstrumentProviderConfig` ready to
        hand to ``InteractiveBrokersDataClientConfig`` /
        ``InteractiveBrokersExecClientConfig``.
    """
    contracts = frozenset(_ibcontract_from_spec(r.contract_spec) for r in resolved)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )
