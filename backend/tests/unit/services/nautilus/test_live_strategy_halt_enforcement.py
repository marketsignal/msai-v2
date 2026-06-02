"""``on_post_build`` mandatory-base-class enforcement + halt-refresh wiring
(PR 2 T2, F6 P0).

The child imports strategies via ``ImportableStrategyConfig`` INSIDE
``node.build()`` — strategy CLASSES don't exist before build. So the gate is
enforced in the existing ``on_post_build`` hook, AFTER build but BEFORE
``node.run_async()``: every live strategy MUST have ``RiskAwareStrategy`` as the
FIRST MRO owner of each gated submit method, else the subprocess raises (mapped
to ``SPAWN_FAILED_PERMANENT`` by the run-loop catch-all) — a non-halt-aware live
strategy never trades (fail-closed).

``isinstance`` ALONE is insufficient: ``class S(Strategy, RiskAwareStrategy)``
(mixin LAST) passes ``isinstance`` but resolves ``self.submit_order`` to
Nautilus's un-gated impl. And a subclass that RE-OVERRIDES ``submit_order`` slips
past an identity check. The enforcement therefore verifies the FIRST MRO owner of
each gated name is ``RiskAwareStrategy``.

Test coverage maps to the plan's failing-test checklist letters (a), (i), (k).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from msai.services.nautilus.risk import DenialContext, RiskAwareStrategy
from msai.services.nautilus.trading_node_subprocess import (
    enforce_halt_gate_mro,
    wire_halt_refresh,
)

# ---------------------------------------------------------------------------
# Strategy class doubles standing in for Nautilus's ``Strategy`` base
# ---------------------------------------------------------------------------


class _Strategy:
    """Stand-in for ``nautilus_trader.trading.strategy.Strategy`` — owns un-gated
    submit methods, simulating Nautilus's Cython cpdefs."""

    def submit_order(self, order: Any, *a: Any, **k: Any) -> None: ...
    def submit_order_list(self, ol: Any, *a: Any, **k: Any) -> None: ...
    def modify_order(self, order: Any, *a: Any, **k: Any) -> None: ...


class _GoodStrategy(RiskAwareStrategy, _Strategy):
    """Mixin FIRST — the gated overrides win the MRO. ACCEPTED."""


class _MixinLastStrategy(_Strategy, RiskAwareStrategy):
    """Base FIRST — ``isinstance`` passes but the un-gated ``Strategy.submit_order``
    wins the MRO. REJECTED."""


class _ReoverrideStrategy(RiskAwareStrategy, _Strategy):
    """Mixin first BUT re-overrides ``submit_order`` with an un-gated impl, so the
    first MRO owner is the subclass, not ``RiskAwareStrategy``. REJECTED."""

    def submit_order(self, order: Any, *a: Any, **k: Any) -> None:
        # Bypasses the gate by calling the base directly.
        _Strategy.submit_order(self, order)


class _PlainStrategy(_Strategy):
    """Bare ``Strategy`` subclass — no mixin at all. REJECTED."""


# ---------------------------------------------------------------------------
# (a) / (k) MRO enforcement
# ---------------------------------------------------------------------------


def test_plain_strategy_is_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017,PT011 — mapped to SPAWN_FAILED_PERMANENT by run loop
        enforce_halt_gate_mro([_PlainStrategy()])


def test_mixin_last_is_rejected_even_though_isinstance_passes() -> None:
    strat = _MixinLastStrategy()
    assert isinstance(strat, RiskAwareStrategy)  # the trap the identity check would fall for
    with pytest.raises(Exception):  # noqa: B017,PT011
        enforce_halt_gate_mro([strat])


def test_reoverride_subclass_is_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017,PT011
        enforce_halt_gate_mro([_ReoverrideStrategy()])


def test_correctly_ordered_mixin_first_is_accepted() -> None:
    # Does not raise.
    enforce_halt_gate_mro([_GoodStrategy()])


def test_enforcement_rejects_if_any_strategy_in_a_portfolio_is_bad() -> None:
    with pytest.raises(Exception):  # noqa: B017,PT011
        enforce_halt_gate_mro([_GoodStrategy(), _PlainStrategy()])


# ---------------------------------------------------------------------------
# Wiring: arms the gate + starts background refresh task + immediate pre-run
# refresh populates the cache (i)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, *, fleet: bool = False, account: bool = False) -> None:
        self._fleet = fleet
        self._account = account
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        if key.endswith(":cause") or "cause" in key:
            return None
        if ":account:" in key:
            return b"1" if self._account else None
        return b"1" if self._fleet else None

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_wire_halt_refresh_arms_gate_and_immediate_refresh_populates_cache() -> None:
    strat = _GoodStrategy()
    redis = _FakeRedis(fleet=False, account=False)

    task = await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.05,
    )
    try:
        # Gate armed (live-only arming).
        assert strat._halt_gate_armed is True  # noqa: SLF001
        # Immediate pre-run refresh populated the cache (no cold-start false-block).
        assert strat._halt_cache is not None  # noqa: SLF001
        value, _ts = strat._halt_cache  # noqa: SLF001
        assert value is False  # neither fleet nor account latch set
        # A first-bar opening order with no halt is ALLOWED.
        strat.base_submitted = []  # type: ignore[attr-defined]
        # Use a minimal order stub.
        order = type(
            "O",
            (),
            {
                "client_order_id": "x",
                "instrument_id": "AAPL.NASDAQ",
                "is_reduce_only": False,
                "tags": None,
            },
        )()
        # The override resolves to RiskAwareStrategy.submit_order; the un-gated base
        # is _Strategy.submit_order (a no-op), so "allowed" just means no exception.
        strat.submit_order(order)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_wire_halt_refresh_reads_account_latch() -> None:
    strat = _GoodStrategy()
    redis = _FakeRedis(fleet=False, account=True)

    task = await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.05,
    )
    try:
        assert strat._halt_cache is not None  # noqa: SLF001
        value, _ts = strat._halt_cache  # noqa: SLF001
        assert value is True  # account latch is set
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_wire_halt_refresh_background_task_keeps_cache_fresh() -> None:
    strat = _GoodStrategy()
    redis = _FakeRedis(fleet=False, account=False)

    task = await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.02,
    )
    try:
        first_ts = strat._halt_cache[1]  # noqa: SLF001
        # Flip the fleet latch; the background task should pick it up.
        redis._fleet = True  # noqa: SLF001
        await asyncio.sleep(0.1)
        value, ts = strat._halt_cache  # noqa: SLF001
        assert value is True
        assert ts >= first_ts
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Redis-down-at-arm — FAIL-CLOSED (P1): the precise F6 hole. An immediate-refresh
# error during the build window must STILL arm the gate (None cache → BLOCK
# opening orders) and STILL create the recovery task — NOT leave the gate
# disarmed (which would admit ALL opening orders for the node's lifetime).
# ---------------------------------------------------------------------------


class _BrokenRedis:
    """A Redis client whose ``get`` always raises — simulates a connection-refused
    / timeout / DNS blip during the few-second build window."""

    def __init__(self) -> None:
        self.closed = False

    async def get(self, key: str) -> bytes | None:  # noqa: ARG002
        raise ConnectionError("redis unreachable")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_wire_halt_refresh_arms_gate_even_when_immediate_refresh_fails() -> None:
    strat = _GoodStrategy()
    redis = _BrokenRedis()

    # MUST NOT raise — wire_halt_refresh swallows the immediate-refresh error and
    # arms the gate so a None cache fails CLOSED.
    task = await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.05,
    )
    try:
        # Armed fail-closed: gate is ON even though the cache could not be primed.
        assert strat._halt_gate_armed is True  # noqa: SLF001
        # Cache never primed → None → the sync gate treats it as halted.
        assert strat._halt_cache is None  # noqa: SLF001
        # An opening order is BLOCKED (fail-closed), NOT admitted.
        opening = type(
            "O",
            (),
            {
                "client_order_id": "open",
                "instrument_id": "AAPL.NASDAQ",
                "is_reduce_only": False,
                "tags": None,
            },
        )()
        assert strat._gate_allows(opening) is False  # noqa: SLF001
        # A flatten order is still ALLOWED (kill-all/drain works under fail-closed).
        flatten = type(
            "F",
            (),
            {
                "client_order_id": "flat",
                "instrument_id": "AAPL.NASDAQ",
                "is_reduce_only": True,
                "tags": None,
            },
        )()
        assert strat._gate_allows(flatten) is True  # noqa: SLF001
        # The background recovery task EXISTS (it can re-prime when Redis returns).
        assert task is not None
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# _audit injection (P1): the wirer MUST assign the OrderAuditWriter to each gated
# strategy's ``_audit`` so node-side halt-denial audit is functional in prod
# (a BLOCKED order returns before super().submit_order, so the engine-level
# msgbus hook fires NO order event — strategy._audit.update_denied is the ONLY
# path to record a node-side denial).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wire_halt_refresh_injects_audit_writer_onto_each_gated_strategy() -> None:
    strat = _GoodStrategy()
    redis = _FakeRedis(fleet=False, account=False)
    sentinel_audit = object()

    task = await wire_halt_refresh(
        strategies=[strat],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.05,
        audit_writer=sentinel_audit,
    )
    try:
        assert strat._audit is sentinel_audit  # noqa: SLF001
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_wire_halt_refresh_sets_denial_context_by_strategy_id() -> None:
    """The denial-audit context is injected onto each gated strategy keyed by
    ``str(strategy.id)`` (== ``strategy_id_full``). This locks the exact seam the
    iter-23 audit-completeness fix depends on: a key-format drift between the
    Nautilus ``StrategyId`` and the ``denial_contexts`` map would SILENTLY degrade
    a real-money halt-denial to the logged-only ``update_denied`` fallback. A
    matching key → context injected; a non-matching key → ``_denial_context``
    stays ``None`` (best-effort fallback, never fabricated identity)."""
    matched = _GoodStrategy()
    matched.id = "EmaCross-DU1234567-001"  # the str(StrategyId) the map is keyed by
    unmatched = _GoodStrategy()
    unmatched.id = "Other-DU1234567-002"
    redis = _FakeRedis(fleet=False, account=False)
    ctx = DenialContext(deployment_id="dep-1", strategy_id="strat-1", strategy_code_hash="hash-1")

    task = await wire_halt_refresh(
        strategies=[matched, unmatched],
        redis_client=redis,
        account_id="DU1234567",
        interval_s=0.05,
        denial_contexts={"EmaCross-DU1234567-001": ctx},
    )
    try:
        assert matched._denial_context is ctx  # noqa: SLF001 — matched key
        assert unmatched._denial_context is None  # noqa: SLF001 — no key → logged-only fallback
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
