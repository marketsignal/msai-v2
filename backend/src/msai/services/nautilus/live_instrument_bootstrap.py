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
    (2) ``live_node_config.build_ib_instrument_provider_config``,
    (3) ``SecurityMaster.resolve_for_live``'s cold-miss fallback.
    All three migrate to registry-driven resolution in the follow-up
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


# CME local timezone. Quarterly futures contracts roll on the third
# Friday of the expiry month in the exchange's local calendar. Using
# UTC here would mis-roll late-Thursday-evening Chicago spawns (DST
# pushes Chicago past midnight UTC at 19:00 local) — the operator-
# facing roll happens at Chicago midnight, not UTC midnight.
_CME_TZ = ZoneInfo("America/Chicago")


def exchange_local_today() -> date:
    """Return the current date in CME's exchange-local timezone.

    Public so callers (e.g., the live-supervisor) can compute the date
    ONCE per spawn and thread it through both
    :func:`canonical_instrument_id` and :func:`phase_1_paper_symbols`
    — guaranteeing they agree even if a quarterly roll happens in the
    middle of provisioning a deployment.
    """
    return datetime.now(_CME_TZ).date()


def third_friday_of(year: int, month: int) -> date:
    """Return the third Friday of ``(year, month)`` — the CME monthly
    futures expiration convention.

    Public so both :func:`_current_quarterly_expiry` (below) and
    :meth:`SecurityMaster._spec_from_canonical` can reuse the same
    arithmetic without re-implementing it.
    """
    first = date(year, month, 1)
    first_friday_offset = (4 - first.weekday()) % 7
    return first + timedelta(days=first_friday_offset + 14)


def _current_quarterly_expiry(today: date) -> str:
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


def _es_front_month_local_symbol(today: date) -> str:
    """Return the IB localSymbol for the ES front-month contract.

    Matches what IB returns when resolving our ``FUT ES CME USD`` contract
    with ``lastTradeDateOrContractMonth`` set to the output of
    :func:`_current_quarterly_expiry`. Verified 2026-04-15 against paper
    IB Gateway: June 2026 → ``ESM6``.
    """
    expiry = _current_quarterly_expiry(today)
    year = int(expiry[:4])
    month = int(expiry[4:])
    return f"ES{_FUT_MONTH_CODES[month]}{year % 10}"


def canonical_instrument_id(
    user_instrument_id: str,
    *,
    today: date | None = None,
) -> str:
    """Map a user-facing instrument_id to the concrete Nautilus
    instrument_id that will exist in the cache after
    :func:`build_ib_instrument_provider_config` preloads the matching
    IBContract.

    For stocks/ETFs/FX, the user-facing id is already canonical
    (``AAPL.NASDAQ``, ``EUR/USD.IDEALPRO``). For futures, the user writes
    ``ES.CME`` (stable across quarterly rolls) but Nautilus registers
    the instrument under the concrete month derived from IB's
    ``localSymbol`` (``ESM6.CME`` this quarter). Without this mapping
    the strategy subscribes to ``ES.CME`` while only ``ESM6.CME``
    exists — zero bar events fire.

    The venue suffix for futures is ``CME`` (IB's native name) —
    ``IB_SIMPLIFIED`` symbology uses IB's exchange strings verbatim, not
    the ISO MIC (``XCME``). Live-verified 2026-04-16: Nautilus
    registered our ``FUT ES CME`` contract as ``ESM6.CME``.

    Accepts either a bare symbol (``"ES"``) or a full instrument_id
    (``"ES.CME"`` or ``"ES.XCME"`` — legacy MIC accepted for input only).

    Args:
        user_instrument_id: Operator-facing symbol or id.
        today: Exchange-local date to use for futures rollover. Callers
            that also invoke :func:`phase_1_paper_symbols` in the same
            spawn should pass the same ``today`` value to both to avoid
            a midnight-on-roll-day race.

    Raises:
        ValueError: Unknown root symbol — Phase 1 has a closed universe.
    """
    root = user_instrument_id.split(".")[0]
    if root in {"AAPL", "MSFT"}:
        return f"{root}.NASDAQ"
    if root == "SPY":
        return "SPY.ARCA"
    if root in {"EUR/USD", "EUR"}:
        return "EUR/USD.IDEALPRO"
    if root == "ES":
        resolved_today = today if today is not None else exchange_local_today()
        return f"{_es_front_month_local_symbol(resolved_today)}.CME"
    raise ValueError(
        f"Unknown instrument root '{root}' — Phase 1 paper symbols: AAPL, MSFT, SPY, EUR/USD, ES"
    )


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
            lookup. Callers that also invoke
            :func:`canonical_instrument_id` in the same spawn should
            pass the same ``today`` value to both.
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
            lastTradeDateOrContractMonth=_current_quarterly_expiry(resolved_today),
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
