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


class _RecordingBase:
    """Stand-in for Nautilus's ``Strategy`` base — records the submit call the
    gated override delegates to via the unbound base call (``super()``)."""

    def submit_order(self, order: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.submitted.append(order)  # type: ignore[attr-defined]


class DummyStrategy(RiskAwareStrategy, _RecordingBase):
    """Concrete gated subclass (mixin FIRST) that captures the orders the
    override delegates to the base impl. The PR-2 gate runs the position /
    exposure / daily-loss suite ONLY when armed AND ``_risk_limits`` is wired —
    both are set here so these legacy risk-suite tests still exercise it.

    A fresh ``_halt_cache`` (value False) keeps the halt branch open so the
    risk-suite checks are reached; individual tests set ``_halt_cache`` to a
    True/None value to exercise the halt path.
    """

    def __init__(
        self, *, limits: RiskLimits | None, portfolio: Any, audit: Any, armed: bool = True
    ) -> None:
        import time

        self._risk_limits = limits
        self.portfolio = portfolio
        self._audit = audit
        self._halt_gate_armed = armed
        self._halt_cache = (False, time.monotonic())
        self._market_hours_check = None
        self.submitted: list[Any] = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_strategy(
    *,
    daily_loss_limit_usd: str = "1000000",
    max_notional_exposure_usd: str = "1000000",
    max_position_per_instrument: str = "10000",
    portfolio: Any = None,
    armed: bool = True,
    wire_limits: bool = True,
) -> DummyStrategy:
    limits: RiskLimits | None = None
    if wire_limits:
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
        armed=armed,
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
    import time

    strat = _build_strategy()
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001
    order = _buy_order()

    strat.submit_order(order)

    # Halted -> the override never delegates to the base submit.
    assert strat.submitted == []


def test_submit_order_with_risk_check_reports_false_under_halt() -> None:
    """The deprecated thin alias must report the HONEST gate decision: when the
    armed halt latch BLOCKS an opening order, the returned result is
    ``allowed=False`` (reason ``risk:halt``) and the order is NOT submitted."""
    import time

    strat = _build_strategy()
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001
    order = _buy_order()

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:halt"
    assert strat.submitted == []


def test_submit_order_with_risk_check_reports_specific_risk_reason_not_halt() -> None:
    """PR 2 F4 (review P3): when a denial is caused by a RISK-LIMIT check (not a
    halt), the thin alias must report the ACTUAL reason (e.g.
    ``risk:position_limit``) — NOT a misleading ``risk:halt``.

    The node is NOT halted (``_halt_cache`` is False); the order is blocked by
    the position-limit check. Pre-fix the alias returned ``risk:halt`` for ANY
    ``_gate_allows``-False, mislabelling the denial for any PR-5 risk-limit
    block. The order is still correctly blocked; only the reason was wrong.
    """
    portfolio = FakePortfolio()
    portfolio._net_position[str(_instrument_id())] = Decimal("9950")  # noqa: SLF001
    strat = _build_strategy(max_position_per_instrument="10000", portfolio=portfolio)
    # 9950 + 100 = 10050 > 10000 → position-limit reject (NOT a halt).
    order = _buy_order(qty="100")

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:position_limit", (
        "a position-limit denial must report risk:position_limit, not risk:halt"
    )
    assert strat.submitted == []


def test_submit_order_with_risk_check_reports_daily_loss_reason() -> None:
    """PR 2 F4 (review P3): a daily-loss denial reports ``risk:daily_loss`` from
    the thin alias — confirming the reason tracks the actual failing check, not
    a blanket ``risk:halt``."""
    portfolio = FakePortfolio()
    portfolio._total_pnls["NASDAQ"] = {"USD": FakeMoney("-12000")}  # noqa: SLF001
    strat = _build_strategy(daily_loss_limit_usd="10000", portfolio=portfolio)

    result = strat.submit_order_with_risk_check(_buy_order())

    assert result.allowed is False
    assert result.reason == "risk:daily_loss"
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

    strat.submit_order(order)

    assert strat.submitted == []


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

    strat.submit_order(sell)

    # 0 + (-75) = -75 → abs(-75) = 75 > 50 → reject
    assert strat.submitted == []


# ---------------------------------------------------------------------------
# FINDING 2 (P2): the risk suite must NOT be gated by ``_halt_gate_armed``.
#
# ``_halt_gate_armed`` gates ONLY the live Redis halt-latch branch. The general
# position/exposure/daily-loss/market-hours suite must run whenever
# ``_risk_limits`` is wired — REGARDLESS of arming — so a backtest / non-live /
# legacy ``submit_order_with_risk_check`` caller with configured limits still
# enforces them. (The suite self-skips when ``_risk_limits is None``.)
# ---------------------------------------------------------------------------


def test_unarmed_with_limits_blocks_position_limit_violation() -> None:
    # FINDING 2 (P2): gate NOT armed (backtest/non-live) but ``_risk_limits`` IS
    # wired and the order trips the per-instrument position cap. The risk suite
    # must STILL run — the order is BLOCKED with ``risk:position_limit``. Pre-fix
    # the unarmed short-circuit returned allowed=True and skipped the suite.
    portfolio = FakePortfolio()
    portfolio._net_position[str(_instrument_id())] = Decimal("9950")  # noqa: SLF001
    strat = _build_strategy(max_position_per_instrument="10000", portfolio=portfolio, armed=False)
    assert strat._halt_gate_armed is False  # noqa: SLF001
    # 9950 + 100 = 10050 > 10000 → position-limit reject even though unarmed.
    order = _buy_order(qty="100")

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False, (
        "an unarmed gate with configured limits must STILL enforce the risk "
        "suite — the order must be blocked"
    )
    assert result.reason == "risk:position_limit"
    assert strat.submitted == []


def test_unarmed_with_limits_passes_when_within_limits() -> None:
    # FINDING 2 (P2) counter-case: unarmed + limits wired but the order is
    # within all caps → allowed (and actually submitted via the override path).
    strat = _build_strategy(armed=False)
    assert strat._halt_gate_armed is False  # noqa: SLF001

    strat.submit_order(_buy_order(qty="100", price="150"))

    assert strat.submitted == [_buy_order(qty="100", price="150")] or len(strat.submitted) == 1, (
        "a within-limits order on an unarmed gate must still be submitted"
    )


def test_unarmed_without_limits_allows_order() -> None:
    # FINDING 2 (P2) backtest-default: unarmed AND ``_risk_limits is None`` (the
    # PR-2 production default for backtests) → the suite self-skips and the order
    # is allowed. This is the path the unarmed short-circuit used to (over-broadly)
    # cover; it must still pass after decoupling the suite from arming.
    strat = _build_strategy(armed=False, wire_limits=False)
    assert strat._halt_gate_armed is False  # noqa: SLF001
    assert strat._risk_limits is None  # noqa: SLF001

    result = strat.submit_order_with_risk_check(_buy_order())

    assert result.allowed is True
    assert result.reason is None
    assert strat.submitted == [_buy_order()] or len(strat.submitted) == 1


def test_unarmed_with_limits_does_not_evaluate_halt_branch() -> None:
    # FINDING 2 (P2): when unarmed, the HALT-LATCH branch must NOT run even
    # though the risk suite does. A ``None`` halt cache (which would fail-closed
    # if armed) must NOT block an unarmed order — only the risk suite governs it.
    strat = _build_strategy(armed=False)
    strat._halt_cache = None  # noqa: SLF001 — would fail-closed IF armed

    result = strat.submit_order_with_risk_check(_buy_order(qty="100", price="150"))

    assert result.allowed is True, (
        "unarmed must skip the halt-latch branch — a None cache must not block "
        "an unarmed order; only the risk suite (within limits here) governs it"
    )


def test_armed_runs_halt_branch_before_risk_suite() -> None:
    # FINDING 2 (P2): when ARMED and the halt latch is active, the halt branch
    # wins (reason ``risk:halt``) — even if the order would ALSO trip a risk
    # limit. The halt branch runs BEFORE the suite (unchanged ordering).
    import time

    portfolio = FakePortfolio()
    portfolio._net_position[str(_instrument_id())] = Decimal("9950")  # noqa: SLF001
    strat = _build_strategy(max_position_per_instrument="10000", portfolio=portfolio, armed=True)
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001 — halt active

    result = strat.submit_order_with_risk_check(_buy_order(qty="100"))

    assert result.allowed is False
    assert result.reason == "risk:halt", "armed + halt active must report risk:halt, not the suite"
    assert strat.submitted == []


# ---------------------------------------------------------------------------
# Daily loss limit (Codex v3 P1 — uses PLURAL total_pnls(venue))
# ---------------------------------------------------------------------------


def test_daily_loss_limit_blocks_when_pnl_exceeds() -> None:
    portfolio = FakePortfolio()
    portfolio._total_pnls["NASDAQ"] = {  # noqa: SLF001
        "USD": FakeMoney("-12000"),
    }
    strat = _build_strategy(daily_loss_limit_usd="10000", portfolio=portfolio)

    strat.submit_order(_buy_order())

    assert strat.submitted == []


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

    strat.submit_order(_buy_order())

    # Total = -7000, limit = 5000 → -7000 < -5000 → reject
    assert strat.submitted == []


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

    strat.submit_order(order)

    assert strat.submitted == []


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

    strat.submit_order(_buy_order())

    assert strat.submitted == []


def test_market_hours_check_fail_closed_on_exception() -> None:
    strat = _build_strategy()

    def boom(_id: Any) -> bool:
        raise RuntimeError("market data unavailable")

    strat._market_hours_check = boom  # noqa: SLF001

    strat.submit_order(_buy_order())

    # Fail-closed: an exception is treated as "outside hours" → not submitted.
    assert strat.submitted == []


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
    import time

    strat = _build_strategy()
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001
    order = _buy_order()

    strat.submit_order(order)

    strat._audit.update_denied.assert_called()  # noqa: SLF001
    call = strat._audit.update_denied.call_args  # noqa: SLF001
    assert call.args[0] == "ord-1" or call.kwargs.get("client_order_id") == "ord-1"
    assert call.kwargs.get("reason") == "risk:halt"


def test_passing_order_does_not_trigger_audit_denial() -> None:
    strat = _build_strategy()
    strat.submit_order(_buy_order())
    strat._audit.update_denied.assert_not_called()  # noqa: SLF001


# ---------------------------------------------------------------------------
# Durable node-side halt denial (PR 2 T2): write_denied vs update_denied
# ---------------------------------------------------------------------------


def _denial_order() -> FakeOrder:
    """A MARKET order the halt gate will block. ``order_type`` is absent on
    ``FakeOrder`` so the facts builder falls back defensively (empty string)."""
    return FakeOrder(
        client_order_id="halt-ord-9",
        instrument_id=_instrument_id(),
        side="BUY",
        quantity=Decimal("25"),
        price=None,
    )


def _build_audit_mock() -> Any:
    audit = MagicMock()
    audit.update_denied = AsyncMock()
    audit.write_denied = AsyncMock()
    return audit


def test_halt_block_with_denial_context_calls_write_denied_with_full_facts() -> None:
    """When the per-run identity is wired, a node-side halt block records a
    COMPLETE ``denied`` row via ``write_denied`` (durable even with no prior
    ``submitted`` row), NOT the UPDATE-only ``update_denied``."""
    import time
    from uuid import uuid4

    from msai.services.nautilus.risk import DenialContext

    audit = _build_audit_mock()
    strat = DummyStrategy(limits=None, portfolio=FakePortfolio(), audit=audit, armed=True)
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001 — force halt
    dep_id, sid = uuid4(), uuid4()
    strat._denial_context = DenialContext(  # noqa: SLF001
        deployment_id=dep_id,
        strategy_id=sid,
        strategy_code_hash="cafe" * 16,
    )
    order = _denial_order()

    strat.submit_order(order)

    # Blocked: never delegated to the base submit.
    assert strat.submitted == []
    # Durable path: write_denied, NOT the UPDATE-only fallback.
    audit.write_denied.assert_called_once()
    audit.update_denied.assert_not_called()

    facts = audit.write_denied.call_args.args[0]
    assert facts.client_order_id == "halt-ord-9"
    assert facts.deployment_id == dep_id
    assert facts.strategy_id == sid
    assert facts.strategy_code_hash == "cafe" * 16
    assert facts.instrument_id == "AAPL.NASDAQ"
    assert facts.side == "BUY"
    assert facts.quantity == Decimal("25")
    assert facts.order_type == ""  # FakeOrder has no order_type → safe fallback
    assert facts.reason == "risk:halt"
    assert facts.is_live is True
    assert facts.backtest_id is None


def test_halt_block_without_denial_context_falls_back_to_update_denied() -> None:
    """No per-run identity wired (``_denial_context is None``) → fall back to the
    best-effort UPDATE-only ``update_denied`` rather than fabricating placeholder
    identity into a real-money audit row."""
    import time

    audit = _build_audit_mock()
    strat = DummyStrategy(limits=None, portfolio=FakePortfolio(), audit=audit, armed=True)
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001
    assert strat._denial_context is None  # noqa: SLF001 — class default

    strat.submit_order(_denial_order())

    assert strat.submitted == []
    audit.update_denied.assert_called_once()
    audit.write_denied.assert_not_called()


def test_denial_audit_db_error_does_not_raise_and_order_stays_blocked() -> None:
    """Best-effort invariant: a DB error in the denial write is logged and
    swallowed — it MUST NOT raise and MUST NOT admit the order."""
    import time
    from uuid import uuid4

    from msai.services.nautilus.risk import DenialContext

    audit = MagicMock()
    audit.update_denied = AsyncMock()
    # write_denied raises synchronously when called (before a coroutine is even
    # built) — _record_denial must swallow it.
    audit.write_denied = MagicMock(side_effect=RuntimeError("db down"))

    strat = DummyStrategy(limits=None, portfolio=FakePortfolio(), audit=audit, armed=True)
    strat._halt_cache = (True, time.monotonic())  # noqa: SLF001
    strat._denial_context = DenialContext(  # noqa: SLF001
        deployment_id=uuid4(), strategy_id=uuid4(), strategy_code_hash="ab" * 32
    )

    # Must not raise.
    strat.submit_order(_denial_order())

    # Order still blocked despite the audit failure.
    assert strat.submitted == []


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
