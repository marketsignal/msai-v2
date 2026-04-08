"""Unit tests for the RiskAwareStrategy mixin
(Phase 3 task 3.7).

The mixin is intentionally decoupled from Nautilus's
``Strategy`` base class so we can unit-test the risk logic
without standing up a full Nautilus runtime. The tests build
a thin ``DummyStrategy`` that combines the mixin with a
``submit_order`` capture and a stub ``Portfolio``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.nautilus.risk import RiskAwareStrategy, RiskCheckResult, RiskLimits

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeMoney:
    """Stub for Nautilus's ``Money`` — only the ``as_decimal``
    accessor the mixin actually calls."""

    value: str

    def as_decimal(self) -> Decimal:
        return Decimal(self.value)


@dataclass
class FakeOrder:
    """Stub Nautilus order. The mixin only reads
    ``instrument_id``, ``side``, ``quantity``, ``price``, and
    ``client_order_id``."""

    client_order_id: str
    instrument_id: Any
    side: str  # "BUY" or "SELL"
    quantity: Decimal
    price: Decimal | None = None


class _InstrumentId:
    """Fake Nautilus ``InstrumentId`` with a deterministic
    ``__str__`` so test fixtures can key on it consistently."""

    def __init__(self, symbol: str = "AAPL", venue: str = "NASDAQ") -> None:
        self.symbol = symbol
        self.venue = SimpleNamespace(value=venue)
        self.venue.__str__ = lambda self=self.venue: venue  # type: ignore[method-assign]

    def __str__(self) -> str:
        return f"{self.symbol}.{self.venue.value}"


def _instrument_id(symbol: str = "AAPL", venue: str = "NASDAQ") -> _InstrumentId:
    return _InstrumentId(symbol, venue)


class FakePortfolio:
    """Stub Nautilus Portfolio with the FIVE methods the mixin
    calls. Defaults return "no data" so individual tests can
    override what they care about."""

    def __init__(self) -> None:
        self._net_position: dict[str, Decimal] = {}
        self._total_pnls: dict[str, dict[str, FakeMoney]] = {}
        self._net_exposures: dict[str, dict[str, FakeMoney]] = {}

    def net_position(self, instrument_id: Any) -> Decimal | None:
        return self._net_position.get(str(instrument_id))

    def total_pnls(
        self,
        venue: Any,
        target_currency: Any = None,  # noqa: ARG002 — accepted for API parity
    ) -> dict[str, FakeMoney]:
        return self._total_pnls.get(getattr(venue, "value", str(venue)), {})

    def net_exposures(
        self,
        venue: Any,
        target_currency: Any = None,  # noqa: ARG002
    ) -> dict[str, FakeMoney]:
        return self._net_exposures.get(getattr(venue, "value", str(venue)), {})


class DummyStrategy(RiskAwareStrategy):
    """Concrete subclass that captures ``submit_order`` calls
    instead of routing them to a real Nautilus runtime."""

    def __init__(self, *, limits: RiskLimits, portfolio: Any, audit: Any) -> None:
        self._risk_limits = limits
        self.portfolio = portfolio
        self._audit = audit
        self._halt_flag_cached = False
        self._market_hours_check = None
        self.submitted: list[Any] = []

    def submit_order(self, order: Any) -> None:
        self.submitted.append(order)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_strategy(
    *,
    daily_loss_limit_usd: str = "1000000",
    max_notional_exposure_usd: str = "1000000",
    max_position_per_instrument: str = "10000",
    portfolio: Any = None,
) -> DummyStrategy:
    limits = RiskLimits(
        daily_loss_limit_usd=Decimal(daily_loss_limit_usd),
        max_notional_exposure_usd=Decimal(max_notional_exposure_usd),
        max_position_per_instrument=Decimal(max_position_per_instrument),
    )
    audit = MagicMock()
    audit.update_denied = AsyncMock()
    return DummyStrategy(
        limits=limits,
        portfolio=portfolio or FakePortfolio(),
        audit=audit,
    )


def _buy_order(qty: str = "100", price: str = "150") -> FakeOrder:
    return FakeOrder(
        client_order_id="ord-1",
        instrument_id=_instrument_id(),
        side="BUY",
        quantity=Decimal(qty),
        price=Decimal(price),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_order_passes_when_within_all_limits() -> None:
    strat = _build_strategy()
    order = _buy_order(qty="100", price="150")

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is True
    assert result.reason is None
    assert strat.submitted == [order]


# ---------------------------------------------------------------------------
# Halt flag (defense in depth)
# ---------------------------------------------------------------------------


def test_halt_flag_blocks_order() -> None:
    strat = _build_strategy()
    strat._halt_flag_cached = True  # noqa: SLF001
    order = _buy_order()

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:halt"
    assert strat.submitted == []


# ---------------------------------------------------------------------------
# Per-instrument position cap
# ---------------------------------------------------------------------------


def test_position_limit_blocks_when_projected_exceeds() -> None:
    portfolio = FakePortfolio()
    portfolio._net_position[str(_instrument_id())] = Decimal("9950")  # noqa: SLF001
    strat = _build_strategy(max_position_per_instrument="10000", portfolio=portfolio)
    # 9950 + 100 = 10050 > 10000 → reject
    order = _buy_order(qty="100")

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:position_limit"


def test_position_limit_signs_sell_quantity_negatively() -> None:
    portfolio = FakePortfolio()
    portfolio._net_position[str(_instrument_id())] = Decimal("0")  # noqa: SLF001
    strat = _build_strategy(max_position_per_instrument="50", portfolio=portfolio)
    sell = FakeOrder(
        client_order_id="ord-sell",
        instrument_id=_instrument_id(),
        side="SELL",
        quantity=Decimal("75"),
        price=Decimal("150"),
    )

    result = strat.submit_order_with_risk_check(sell)

    # 0 + (-75) = -75 → abs(-75) = 75 > 50 → reject
    assert result.allowed is False
    assert result.reason == "risk:position_limit"


# ---------------------------------------------------------------------------
# Daily loss limit (Codex v3 P1 — uses PLURAL total_pnls(venue))
# ---------------------------------------------------------------------------


def test_daily_loss_limit_blocks_when_pnl_exceeds() -> None:
    portfolio = FakePortfolio()
    portfolio._total_pnls["NASDAQ"] = {  # noqa: SLF001
        "USD": FakeMoney("-12000"),
    }
    strat = _build_strategy(daily_loss_limit_usd="10000", portfolio=portfolio)

    result = strat.submit_order_with_risk_check(_buy_order())

    assert result.allowed is False
    assert result.reason == "risk:daily_loss"


def test_daily_loss_limit_uses_plural_total_pnls_with_venue() -> None:
    """Codex v3 P1 regression: the mixin MUST call
    ``portfolio.total_pnls(venue, target_currency=...)`` (plural,
    takes Venue), NOT ``portfolio.total_pnl(venue)`` (singular,
    expects InstrumentId). The plural is the only one that
    returns venue-aggregated PnL.
    """
    portfolio = MagicMock()
    portfolio.net_position.return_value = Decimal("0")
    portfolio.total_pnls.return_value = {"USD": FakeMoney("-100")}
    portfolio.net_exposures.return_value = {}
    strat = _build_strategy(portfolio=portfolio)

    strat.submit_order_with_risk_check(_buy_order())

    # The plural was called with the venue
    portfolio.total_pnls.assert_called()
    call_args = portfolio.total_pnls.call_args
    assert call_args.args[0] is _buy_order().instrument_id.venue or hasattr(
        call_args.args[0], "value"
    )
    # The singular MUST NOT have been called with a Venue
    portfolio.total_pnl.assert_not_called()


def test_multi_currency_pnl_aggregation() -> None:
    """Sum across currencies — Nautilus has already converted
    each Money to the target_currency=USD on its end, so we
    just sum the as_decimal() values."""
    portfolio = FakePortfolio()
    portfolio._total_pnls["NASDAQ"] = {  # noqa: SLF001
        "USD": FakeMoney("-3000"),
        "EUR_in_USD": FakeMoney("-4000"),
    }
    strat = _build_strategy(daily_loss_limit_usd="5000", portfolio=portfolio)

    result = strat.submit_order_with_risk_check(_buy_order())

    # Total = -7000, limit = 5000 → -7000 < -5000 → reject
    assert result.allowed is False
    assert result.reason == "risk:daily_loss"


def test_daily_loss_limit_passes_when_no_pnl_data() -> None:
    """Cold start with no PnL data — let the order through."""
    strat = _build_strategy()  # default portfolio has empty pnls
    result = strat.submit_order_with_risk_check(_buy_order())
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Exposure limit (per-venue, plural net_exposures)
# ---------------------------------------------------------------------------


def test_exposure_limit_blocks_when_projected_exceeds() -> None:
    portfolio = FakePortfolio()
    portfolio._net_exposures["NASDAQ"] = {  # noqa: SLF001
        "USD": FakeMoney("995000"),
    }
    strat = _build_strategy(max_notional_exposure_usd="1000000", portfolio=portfolio)
    # current 995_000 + (100 * 150) = 1_010_000 > 1_000_000
    order = _buy_order(qty="100", price="150")

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:exposure"


def test_exposure_limit_uses_plural_net_exposures_with_venue() -> None:
    portfolio = MagicMock()
    portfolio.net_position.return_value = Decimal("0")
    portfolio.total_pnls.return_value = {}
    portfolio.net_exposures.return_value = {"USD": FakeMoney("0")}
    strat = _build_strategy(portfolio=portfolio)

    strat.submit_order_with_risk_check(_buy_order())

    portfolio.net_exposures.assert_called()
    portfolio.net_exposure.assert_not_called()  # singular MUST NOT be called


def test_market_order_zero_notional_for_exposure_check() -> None:
    """Market orders have ``price=None`` — the mixin treats
    notional as zero so the exposure check doesn't reject
    every market order on ambiguous fill price."""
    portfolio = FakePortfolio()
    portfolio._net_exposures["NASDAQ"] = {  # noqa: SLF001
        "USD": FakeMoney("999999"),
    }
    strat = _build_strategy(max_notional_exposure_usd="1000000", portfolio=portfolio)
    market_order = FakeOrder(
        client_order_id="ord-mkt",
        instrument_id=_instrument_id(),
        side="BUY",
        quantity=Decimal("100"),
        price=None,
    )

    result = strat.submit_order_with_risk_check(market_order)

    # 999999 + 0 = 999999 <= 1000000 → allowed
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------


def test_market_hours_check_blocks_when_callable_returns_false() -> None:
    strat = _build_strategy()
    strat._market_hours_check = lambda _id: False  # noqa: SLF001

    result = strat.submit_order_with_risk_check(_buy_order())

    assert result.allowed is False
    assert result.reason == "risk:market_hours"


def test_market_hours_check_fail_closed_on_exception() -> None:
    strat = _build_strategy()

    def boom(_id: Any) -> bool:
        raise RuntimeError("market data unavailable")

    strat._market_hours_check = boom  # noqa: SLF001

    result = strat.submit_order_with_risk_check(_buy_order())

    # Fail-closed: an exception is treated as "outside hours"
    assert result.allowed is False
    assert result.reason == "risk:market_hours"


def test_market_hours_check_none_callable_passes() -> None:
    """Phase 1: no MarketHoursService yet. Default ``None``
    callable means "always allow" — Phase 4 wires the real
    check."""
    strat = _build_strategy()
    assert strat._market_hours_check is None  # noqa: SLF001

    result = strat.submit_order_with_risk_check(_buy_order())

    assert result.allowed is True


# ---------------------------------------------------------------------------
# Audit denial
# ---------------------------------------------------------------------------


def test_denied_order_records_audit_with_reason() -> None:
    strat = _build_strategy()
    strat._halt_flag_cached = True  # noqa: SLF001
    order = _buy_order()

    strat.submit_order_with_risk_check(order)

    strat._audit.update_denied.assert_called()  # noqa: SLF001
    call = strat._audit.update_denied.call_args  # noqa: SLF001
    assert call.args[0] == "ord-1" or call.kwargs.get("client_order_id") == "ord-1"
    assert call.kwargs.get("reason") == "risk:halt"


def test_passing_order_does_not_trigger_audit_denial() -> None:
    strat = _build_strategy()
    strat.submit_order_with_risk_check(_buy_order())
    strat._audit.update_denied.assert_not_called()  # noqa: SLF001


# ---------------------------------------------------------------------------
# RiskCheckResult dataclass
# ---------------------------------------------------------------------------


def test_risk_check_result_default_reason_is_none() -> None:
    result = RiskCheckResult(allowed=True)
    assert result.allowed is True
    assert result.reason is None


@pytest.mark.parametrize(
    "reason",
    [
        "risk:halt",
        "risk:position_limit",
        "risk:daily_loss",
        "risk:exposure",
        "risk:market_hours",
    ],
)
def test_risk_check_result_carries_reason(reason: str) -> None:
    result = RiskCheckResult(allowed=False, reason=reason)
    assert result.allowed is False
    assert result.reason == reason
