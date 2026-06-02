"""Node-side live-halt gate tests for :class:`RiskAwareStrategy` (PR 2 T2, F6 P0).

These tests pin the REAL-MONEY P0 behavior: the mixin OVERRIDES the public
submit cpdefs (``submit_order`` / ``submit_order_list`` / ``modify_order``) so a
strategy that calls ``self.submit_order(order)`` directly (as the example
strategies do) is halt-gated with no change to its body.

The halt gate is LIVE-ONLY armed: when ``_halt_gate_armed`` is False (backtest),
the halt branch is INERT and opening orders flow freely even with a ``None``
cache. When armed (live ``on_post_build`` wiring), a ``None`` / stale / ``True``
halt cache fails CLOSED for OPENING orders but ALWAYS allows reduce-only /
``MARKET_EXIT`` flatten orders.

The position/exposure/daily-loss suite is GUARDED behind ``_risk_limits is not
None`` — PR 2 does NOT wire risk limits (PR-5 scope), so an unwired instance must
NOT ``AttributeError`` and must let a non-halted opening order through.

Test coverage maps to the plan's failing-test checklist letters (b)-(h), (j).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.nautilus.risk import RiskAwareStrategy
from msai.services.nautilus.risk.risk_aware_strategy import HALT_CACHE_MAX_AGE_S

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeVenue:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _FakeInstrumentId:
    def __init__(self, symbol: str = "AAPL", venue: str = "NASDAQ") -> None:
        self.symbol = symbol
        self.venue = _FakeVenue(venue)

    def __str__(self) -> str:
        return f"{self.symbol}.{self.venue.value}"


@dataclass
class FakeOrder:
    """Stub Nautilus order. The gate reads ``instrument_id``,
    ``client_order_id``, ``is_reduce_only`` and ``tags``."""

    client_order_id: str = "ord-1"
    instrument_id: Any = field(default_factory=_FakeInstrumentId)
    is_reduce_only: bool = False
    tags: list[str] | None = None


# The mixin calls the REAL Nautilus impl via ``Strategy.submit_order(self, ...)``.
# To unit-test without a Nautilus runtime, the test strategy substitutes a base
# class whose submit methods record the call instead of routing to Nautilus.


class _RecordingBase:
    """Stand-in for ``nautilus_trader.trading.strategy.Strategy``. Records the
    submit-method calls the OVERRIDE delegates to via the unbound base call."""

    def submit_order(self, order: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_submitted.append(order)  # type: ignore[attr-defined]

    def submit_order_list(self, order_list: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_submitted_lists.append(order_list)  # type: ignore[attr-defined]

    def modify_order(self, order: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.base_modified.append(order)  # type: ignore[attr-defined]


class _GatedStrategy(RiskAwareStrategy, _RecordingBase):
    """Mixin FIRST so the gated overrides win the MRO, with a recording base
    standing in for Nautilus's ``Strategy``."""

    def __init__(self, *, audit: Any = None) -> None:
        self._audit = audit
        self.base_submitted: list[Any] = []
        self.base_submitted_lists: list[Any] = []
        self.base_modified: list[Any] = []


def _make(
    *, armed: bool, halt: tuple[bool, float] | None = None, audit: Any = None
) -> _GatedStrategy:
    strat = _GatedStrategy(audit=audit)
    strat._halt_gate_armed = armed  # noqa: SLF001
    strat._halt_cache = halt  # noqa: SLF001
    return strat


def _fresh(value: bool) -> tuple[bool, float]:
    return (value, time.monotonic())


def _stale(value: bool) -> tuple[bool, float]:
    return (value, time.monotonic() - (HALT_CACHE_MAX_AGE_S + 5.0))


# ---------------------------------------------------------------------------
# (b) gate on each submit method, no recursion, direct self.submit_order gated
# ---------------------------------------------------------------------------


def test_armed_not_halted_opening_order_passes_through_to_base() -> None:
    strat = _make(armed=True, halt=_fresh(False))
    order = FakeOrder()

    strat.submit_order(order)

    # The override delegated to the unbound base impl exactly once (no recursion).
    assert strat.base_submitted == [order]


def test_submit_order_list_and_modify_order_are_also_gated() -> None:
    strat = _make(armed=True, halt=_fresh(False))
    leg = FakeOrder()
    order_list = SimpleNamespace(orders=[leg])

    strat.submit_order_list(order_list)
    strat.modify_order(leg)

    assert strat.base_submitted_lists == [order_list]
    assert strat.base_modified == [leg]


# ---------------------------------------------------------------------------
# (c) halt true + opening order BLOCKED; denial recorded best-effort;
#     missing/raising _audit still BLOCKS (no crash, no admit)
# ---------------------------------------------------------------------------


def test_halt_true_blocks_opening_order() -> None:
    audit = MagicMock()
    audit.update_denied = AsyncMock()
    strat = _make(armed=True, halt=_fresh(True), audit=audit)

    strat.submit_order(FakeOrder())

    assert strat.base_submitted == []


def test_halt_block_records_denial_best_effort() -> None:
    audit = MagicMock()
    audit.update_denied = AsyncMock()
    strat = _make(armed=True, halt=_fresh(True), audit=audit)

    strat.submit_order(FakeOrder(client_order_id="ord-denied"))

    audit.update_denied.assert_called()


def test_missing_audit_still_blocks_and_does_not_crash() -> None:
    # _audit is None — _record_denial must log+continue, NOT raise, NOT admit.
    strat = _make(armed=True, halt=_fresh(True), audit=None)

    strat.submit_order(FakeOrder())

    assert strat.base_submitted == []


def test_raising_audit_still_blocks_and_does_not_admit() -> None:
    audit = MagicMock()
    audit.update_denied = MagicMock(side_effect=RuntimeError("db down"))
    strat = _make(armed=True, halt=_fresh(True), audit=audit)

    strat.submit_order(FakeOrder())

    assert strat.base_submitted == []


# ---------------------------------------------------------------------------
# (d) halt true + reduce-only / MARKET_EXIT order ALWAYS ALLOWED
# ---------------------------------------------------------------------------


def test_halt_true_allows_reduce_only_order() -> None:
    strat = _make(armed=True, halt=_fresh(True))
    order = FakeOrder(is_reduce_only=True)

    strat.submit_order(order)

    assert strat.base_submitted == [order]


def test_halt_true_allows_market_exit_tagged_order() -> None:
    strat = _make(armed=True, halt=_fresh(True))
    order = FakeOrder(tags=["MARKET_EXIT"])

    strat.submit_order(order)

    assert strat.base_submitted == [order]


# ---------------------------------------------------------------------------
# (e) submit_order_list atomic deny under halt; modify under halt
# ---------------------------------------------------------------------------


def test_submit_order_list_atomic_deny_when_any_leg_halted() -> None:
    strat = _make(armed=True, halt=_fresh(True))
    opening = FakeOrder(client_order_id="open")
    flatten = FakeOrder(client_order_id="flat", is_reduce_only=True)
    order_list = SimpleNamespace(orders=[flatten, opening])

    strat.submit_order_list(order_list)

    # Whole list denied atomically — NOT a partial submit.
    assert strat.base_submitted_lists == []


def test_submit_order_list_all_reduce_only_allowed_under_halt() -> None:
    strat = _make(armed=True, halt=_fresh(True))
    flatten1 = FakeOrder(client_order_id="f1", is_reduce_only=True)
    flatten2 = FakeOrder(client_order_id="f2", tags=["MARKET_EXIT"])
    order_list = SimpleNamespace(orders=[flatten1, flatten2])

    strat.submit_order_list(order_list)

    assert strat.base_submitted_lists == [order_list]


def test_opening_modify_blocked_under_halt_reduce_only_modify_allowed() -> None:
    strat = _make(armed=True, halt=_fresh(True))
    opening = FakeOrder(client_order_id="m-open")
    reducing = FakeOrder(client_order_id="m-reduce", is_reduce_only=True)

    strat.modify_order(opening)
    strat.modify_order(reducing)

    assert strat.base_modified == [reducing]


# ---------------------------------------------------------------------------
# (e2) submit_order_list with a PLAIN list/tuple shape (defensive hardening).
# The canonical Nautilus shape is an ``OrderList`` (has ``.orders``), but the
# node-side halt gate must NOT silently let an opening order through if an
# order-list is passed as a bare ``list``/``tuple`` — each element must be gated
# as a leg. (F2: Cython ``submit_order_list`` would actually TypeError on a plain
# list, so this is defensive, not a reachable silent bypass — but the gate must
# be shape-agnostic on this real-money path.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", [list, tuple])
def test_submit_order_list_plain_sequence_blocks_opening_leg_under_halt(
    shape: type,
) -> None:
    """A plain ``list``/``tuple`` order-list containing an OPENING leg must be
    BLOCKED under an active halt — the legs must be extracted from the sequence
    itself, not from a (missing) ``.orders`` attribute that would yield empty
    legs and delegate ungated."""
    strat = _make(armed=True, halt=_fresh(True))
    opening = FakeOrder(client_order_id="open")
    order_list = shape([opening])

    strat.submit_order_list(order_list)

    # Falsification: pre-fix ``_iter_order_list`` returns [] for a plain
    # sequence, so no leg is gated and the list is delegated ungated.
    assert strat.base_submitted_lists == []


@pytest.mark.parametrize("shape", [list, tuple])
def test_submit_order_list_plain_sequence_all_reduce_only_allowed_under_halt(
    shape: type,
) -> None:
    """A plain ``list``/``tuple`` of only reduce-only / ``MARKET_EXIT`` legs is
    ALWAYS allowed under a halt so flatten/kill-all still works."""
    strat = _make(armed=True, halt=_fresh(True))
    flatten1 = FakeOrder(client_order_id="f1", is_reduce_only=True)
    flatten2 = FakeOrder(client_order_id="f2", tags=["MARKET_EXIT"])
    order_list = shape([flatten1, flatten2])

    strat.submit_order_list(order_list)

    assert strat.base_submitted_lists == [order_list]


def test_iter_order_list_unrecognized_shape_returns_empty() -> None:
    """A truly-unrecognized non-None shape (no ``.orders``, not list/tuple)
    yields no legs — the subsequent delegation to Nautilus's Cython
    ``submit_order_list(OrderList)`` rejects it with TypeError (a safe error,
    not an ungated submit). This documents the intentional empty return."""
    legs = RiskAwareStrategy._iter_order_list(object())  # noqa: SLF001
    assert legs == []


def test_iter_order_list_empty_order_list_delegates_under_halt() -> None:
    """A legitimately-empty ``OrderList`` (``.orders=[]``) must NOT be fail-closed
    — with no legs there is nothing to gate, so it delegates (the empty-legs
    case is allowed, matching the canonical ``OrderList`` contract)."""
    strat = _make(armed=True, halt=_fresh(True))
    order_list = SimpleNamespace(orders=[])

    strat.submit_order_list(order_list)

    assert strat.base_submitted_lists == [order_list]


# ---------------------------------------------------------------------------
# (f) Redis unreachable -> cache None / stale -> opening BLOCKED, flatten ALLOWED
# ---------------------------------------------------------------------------


def test_none_cache_when_armed_blocks_opening_allows_flatten() -> None:
    strat = _make(armed=True, halt=None)

    strat.submit_order(FakeOrder(client_order_id="open"))
    strat.submit_order(FakeOrder(client_order_id="flat", is_reduce_only=True))

    assert strat.base_submitted == [strat.base_submitted[0]]
    assert [str(o.client_order_id) for o in strat.base_submitted] == ["flat"]


def test_stale_cache_when_armed_blocks_opening_order() -> None:
    # Stale cache even with value=False must fail closed for opening orders.
    strat = _make(armed=True, halt=_stale(False))

    strat.submit_order(FakeOrder())

    assert strat.base_submitted == []


def test_stale_cache_allows_flatten() -> None:
    strat = _make(armed=True, halt=_stale(False))
    order = FakeOrder(is_reduce_only=True)

    strat.submit_order(order)

    assert strat.base_submitted == [order]


# ---------------------------------------------------------------------------
# (j) BACKTEST mode (gate NOT armed): opening orders flow freely even with None
# ---------------------------------------------------------------------------


def test_unarmed_backtest_opening_order_passes_with_none_cache() -> None:
    strat = _make(armed=False, halt=None)

    strat.submit_order(FakeOrder())

    assert strat.base_submitted != []


def test_unarmed_backtest_ignores_true_halt_cache() -> None:
    # Even a True halt cache is inert when not armed (there is no live latch in
    # a backtest — the gate must never block backtests).
    strat = _make(armed=False, halt=_fresh(True))

    strat.submit_order(FakeOrder())

    assert strat.base_submitted != []


# ---------------------------------------------------------------------------
# (h) _risk_limits UNSET (PR-2 default): non-halted opening order ALLOWED, no
#     AttributeError (position/exposure/daily-loss suite guarded/skipped)
# ---------------------------------------------------------------------------


def test_unset_risk_limits_does_not_attribute_error() -> None:
    strat = _make(armed=True, halt=_fresh(False))
    # _risk_limits is the class default (None) — guard must skip the suite.
    assert strat._risk_limits is None  # noqa: SLF001

    strat.submit_order(FakeOrder())

    assert strat.base_submitted != []


# ---------------------------------------------------------------------------
# refresh_halt_cache populates the cache from the live latch readers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_halt_cache_populates_from_fleet_or_account_keys() -> None:
    strat = _make(armed=True, halt=None)

    # Account latch set, fleet clear -> halted True for this account.
    async def read_halt() -> bool:
        return True

    await strat.refresh_halt_cache(read_halt)

    assert strat._halt_cache is not None  # noqa: SLF001
    value, _ts = strat._halt_cache  # noqa: SLF001
    assert value is True


# ---------------------------------------------------------------------------
# submit_order_with_risk_check retired to a thin alias -> single gated path.
# The returned RiskCheckResult must report the HONEST gate decision: allowed
# only when the order was actually admitted to the base impl.
# ---------------------------------------------------------------------------


def test_submit_order_with_risk_check_is_thin_alias() -> None:
    strat = _make(armed=True, halt=_fresh(False))
    order = FakeOrder()

    result = strat.submit_order_with_risk_check(order)

    # Goes through the gated override exactly once (no double-gate / no recursion).
    assert strat.base_submitted == [order]
    # Admitted -> the alias reports allowed=True.
    assert result.allowed is True
    assert result.reason is None


def test_submit_order_with_risk_check_reports_blocked_under_halt() -> None:
    # Armed + halted: an OPENING order is BLOCKED by the gate. The alias must
    # report the HONEST result (allowed=False, reason="risk:halt") AND not submit
    # — a legacy caller using the result to update state must not believe the
    # order was accepted.
    audit = MagicMock()
    audit.update_denied = AsyncMock()
    strat = _make(armed=True, halt=_fresh(True), audit=audit)
    order = FakeOrder()

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is False
    assert result.reason == "risk:halt"
    assert strat.base_submitted == []
    # Single gated path: the gate is evaluated ONCE, so the denial is recorded
    # exactly once (no double-gating / duplicate denial audit).
    assert audit.update_denied.call_count == 1


def test_submit_order_with_risk_check_allows_flatten_under_halt() -> None:
    # Reduce-only flatten is ALWAYS allowed even under a halt — the alias reports
    # allowed=True and the order is submitted.
    strat = _make(armed=True, halt=_fresh(True))
    order = FakeOrder(is_reduce_only=True)

    result = strat.submit_order_with_risk_check(order)

    assert result.allowed is True
    assert result.reason is None
    assert strat.base_submitted == [order]
