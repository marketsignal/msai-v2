"""Unit: IB exec-side pacing/throttle detection + Redis counter INCR (PR 1b T7d).

The engine-level OrderRejected audit branch in ``trading_node_subprocess`` must
recognise IB EXEC-side pacing/throttle indications in the rejection reason and
INCR a plain Redis counter ``msai:metrics:ib_exec_pacing:{account_id}`` so the
data-health API can hydrate it into the ``IB_EXEC_PACING_ERRORS`` gauge.

Critical discrimination (per the task): the legacy market-data pacing codes
``100`` / ``162`` / ``420`` must NOT match — only exec-side throttle PHRASES
(``pacing violation``, ``max rate of messages``, ``throttle``), case-insensitive.
"""

from __future__ import annotations

import pytest

from msai.core.halt_keys import ib_exec_pacing_key
from msai.services.nautilus.trading_node_subprocess import (
    is_ib_exec_pacing_reason,
    record_ib_exec_pacing,
)


@pytest.mark.parametrize(
    "reason",
    [
        "Order rejected: pacing violation",
        "PACING VIOLATION detected",
        "The max rate of messages per second has been exceeded",
        "request was throttled by the gateway",
        "Throttle limit reached",
        # A codeless throttle phrase still matches — exec-side throttles may
        # carry no IB error code.
        "pacing violation",
        # A coded reason whose code is NOT a market-data pacing code (100/162/420)
        # still matches on the phrase.
        "Error 999: pacing violation",
    ],
)
def test_exec_pacing_phrases_match(reason: str) -> None:
    assert is_ib_exec_pacing_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "Insufficient buying power",
        # Legacy market-data pacing codes must NOT be treated as exec pacing.
        "Error 100: max number of tickers reached",
        "Error 162: historical market data service error",
        "Error 420: invalid real-time query pacing",
        "order quantity exceeds limit",
        # IB's *canonical* market-data pacing message texts embed the matched
        # phrases verbatim — these MUST be suppressed by the leading code, not
        # mis-counted as exec pacing (the bug this regression guards).
        "Error 100: max rate of messages per second exceeded",
        "Error 162: Historical Market Data Service error message: pacing violation",
        "Error 420: Invalid real-time query: pacing violation",
    ],
)
def test_non_exec_pacing_reasons_do_not_match(reason: str | None) -> None:
    assert is_ib_exec_pacing_reason(reason) is False


class _FakeRedis:
    """Minimal async Redis stub recording incr calls."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]


@pytest.mark.asyncio
async def test_record_ib_exec_pacing_incrs_account_counter() -> None:
    redis = _FakeRedis()
    await record_ib_exec_pacing(
        redis, reason="Order rejected: pacing violation", account_id="DUP-1"
    )
    assert redis.store[ib_exec_pacing_key("DUP-1")] == 1


@pytest.mark.asyncio
async def test_record_ib_exec_pacing_ignores_non_pacing_reason() -> None:
    redis = _FakeRedis()
    await record_ib_exec_pacing(redis, reason="Insufficient buying power", account_id="DUP-1")
    assert redis.store == {}


@pytest.mark.asyncio
async def test_record_ib_exec_pacing_swallows_redis_errors() -> None:
    """A metrics-counter failure must never propagate into the audit hook."""

    class _BrokenRedis:
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis down")

    # Must not raise.
    await record_ib_exec_pacing(_BrokenRedis(), reason="pacing violation", account_id="DUP-1")


@pytest.mark.asyncio
async def test_record_ib_exec_pacing_noops_without_account() -> None:
    redis = _FakeRedis()
    await record_ib_exec_pacing(redis, reason="pacing violation", account_id=None)
    assert redis.store == {}
