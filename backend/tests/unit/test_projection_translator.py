"""Unit tests for the Nautilus → internal event translator
(Phase 3 task 3.4)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    FillEvent,
    OrderStatusChange,
    PositionSnapshot,
)
from msai.services.nautilus.projection.translator import translate

DEPLOYMENT_ID = uuid4()


def test_translate_position_opened_returns_snapshot() -> None:
    event = {
        "instrument_id": "AAPL.NASDAQ",
        "quantity": "100",
        "avg_px_open": "150.25",
        "unrealized_pnl": "10.50",
        "realized_pnl": "0",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.position.opened",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, PositionSnapshot)
    assert result.deployment_id == DEPLOYMENT_ID
    assert result.instrument_id == "AAPL.NASDAQ"
    assert result.qty == Decimal("100")
    assert result.avg_price == Decimal("150.25")
    assert result.unrealized_pnl == Decimal("10.50")


def test_translate_position_changed_returns_snapshot() -> None:
    event = {
        "instrument_id": "AAPL.NASDAQ",
        "quantity": "150",
        "avg_px_open": "151.00",
        "unrealized_pnl": "5.00",
        "realized_pnl": "2.00",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.position.changed",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, PositionSnapshot)
    assert result.qty == Decimal("150")
    assert result.realized_pnl == Decimal("2.00")


def test_translate_position_closed_returns_snapshot() -> None:
    event = {
        "instrument_id": "AAPL.NASDAQ",
        "quantity": "0",
        "avg_px_open": "0",
        "unrealized_pnl": "0",
        "realized_pnl": "100.00",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.position.closed",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, PositionSnapshot)
    assert result.realized_pnl == Decimal("100.00")


def test_translate_fill_buy_returns_fill_event() -> None:
    event = {
        "client_order_id": "ord-1",
        "instrument_id": "AAPL.NASDAQ",
        "order_side": "BUY",
        "last_qty": "100",
        "last_px": "150.25",
        "commission": "1.50",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.order.filled",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, FillEvent)
    assert result.side == "BUY"
    assert result.qty == Decimal("100")
    assert result.price == Decimal("150.25")
    assert result.commission == Decimal("1.50")


def test_translate_fill_sell_lowercase_normalizes_side() -> None:
    event = {
        "client_order_id": "ord-2",
        "instrument_id": "AAPL.NASDAQ",
        "side": "sell",
        "quantity": "50",
        "price": "151.00",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.order.filled",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, FillEvent)
    assert result.side == "SELL"


def test_translate_fill_unknown_side_raises() -> None:
    event = {
        "client_order_id": "ord-3",
        "instrument_id": "AAPL.NASDAQ",
        "order_side": "FLAT",
        "last_qty": "10",
        "last_px": "1.00",
        "ts_event": 1_700_000_000_000_000_000,
    }

    with pytest.raises(ValueError, match="unrecognized fill side"):
        translate(
            topic="events.order.filled",
            event_dict=event,
            deployment_id=DEPLOYMENT_ID,
        )


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("events.order.submitted", "submitted"),
        ("events.order.accepted", "accepted"),
        ("events.order.partially_filled", "partially_filled"),
        ("events.order.cancelled", "cancelled"),
        ("events.order.canceled", "cancelled"),  # American spelling normalizes
        ("events.order.rejected", "rejected"),
        ("events.order.denied", "denied"),
    ],
)
def test_translate_order_status_topics(topic: str, expected: str) -> None:
    event = {
        "client_order_id": "ord-7",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(topic=topic, event_dict=event, deployment_id=DEPLOYMENT_ID)

    assert isinstance(result, OrderStatusChange)
    assert result.status == expected


def test_translate_order_status_with_reason() -> None:
    event = {
        "client_order_id": "ord-8",
        "reason": "INSUFFICIENT_FUNDS",
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.order.rejected",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, OrderStatusChange)
    assert result.reason == "INSUFFICIENT_FUNDS"


def test_translate_account_state_returns_update() -> None:
    """Use the REAL Nautilus AccountState shape from
    ``model/events/account.pyx:to_dict_c`` — ``balances`` and
    ``margins`` arrays, NOT flat ``balance``/``margin_used``
    fields. Codex batch 8 P1 fix."""
    event = {
        "account_id": "DU12345",
        "balances": [
            {
                "type": "AccountBalance",
                "total": "100000.00",
                "locked": "5000.00",
                "free": "95000.00",
                "currency": "USD",
            }
        ],
        "margins": [],
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.account.state",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, AccountStateUpdate)
    assert result.account_id == "DU12345"
    assert result.balance == Decimal("100000.00")
    # margins=[] → fall back to AccountBalance.locked
    assert result.margin_used == Decimal("5000.00")
    assert result.margin_available == Decimal("95000.00")


def test_translate_account_state_with_margin_array_sums_initial() -> None:
    """Margin accounts: ``margins`` is non-empty, so
    ``margin_used`` is the SUM of margin row ``initial``
    fields, not the AccountBalance.locked fallback."""
    event = {
        "account_id": "U1234567",
        "balances": [
            {
                "type": "AccountBalance",
                "total": "200000.00",
                "locked": "0",
                "free": "180000.00",
                "currency": "USD",
            }
        ],
        "margins": [
            {
                "type": "MarginBalance",
                "initial": "8000.00",
                "maintenance": "4000.00",
                "currency": "USD",
                "instrument_id": "AAPL.NASDAQ",
            },
            {
                "type": "MarginBalance",
                "initial": "12000.00",
                "maintenance": "6000.00",
                "currency": "USD",
                "instrument_id": "MSFT.NASDAQ",
            },
        ],
        "ts_event": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.account.state",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, AccountStateUpdate)
    assert result.balance == Decimal("200000.00")
    # 8000 + 12000 from the margins array
    assert result.margin_used == Decimal("20000.00")
    assert result.margin_available == Decimal("180000.00")


def test_translate_account_state_accepts_string_timestamp() -> None:
    """Codex batch 8 P0 regression: Nautilus
    ``MsgSpecSerializer(timestamps_as_str=True)`` writes
    ``ts_event`` as a string. The translator must accept it."""
    event = {
        "account_id": "DU12345",
        "balances": [
            {
                "type": "AccountBalance",
                "total": "1",
                "locked": "0",
                "free": "1",
                "currency": "USD",
            }
        ],
        "margins": [],
        # str, not int
        "ts_event": "1700000000000000000",
    }

    result = translate(
        topic="events.account.state",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, AccountStateUpdate)
    assert result.ts.year == 2023


def test_translate_unrouted_topic_returns_none() -> None:
    result = translate(
        topic="events.bar.bar",
        event_dict={},
        deployment_id=DEPLOYMENT_ID,
    )
    assert result is None


def test_translate_unknown_order_topic_returns_none() -> None:
    result = translate(
        topic="events.order.mystery",
        event_dict={"client_order_id": "x"},
        deployment_id=DEPLOYMENT_ID,
    )
    assert result is None


def test_translate_falls_back_to_ts_init_when_ts_event_missing() -> None:
    event = {
        "instrument_id": "AAPL.NASDAQ",
        "quantity": "1",
        "avg_px_open": "1",
        "unrealized_pnl": "0",
        "realized_pnl": "0",
        "ts_init": 1_700_000_000_000_000_000,
    }

    result = translate(
        topic="events.position.opened",
        event_dict=event,
        deployment_id=DEPLOYMENT_ID,
    )

    assert isinstance(result, PositionSnapshot)
    assert result.ts.year == 2023
