"""Unit tests for the internal event schema (Phase 3 task 3.3).

Round-trip every event type through ``model_dump_json`` →
``model_validate_json`` so we know the WebSocket boundary won't
silently drop / mangle a field. Also verifies the discriminated
union routes correctly by ``event_type``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    DeploymentStatusEvent,
    FillEvent,
    InternalEvent,
    OrderStatusChange,
    PositionSnapshot,
    RiskHaltEvent,
)

_DEPLOYMENT_ID = uuid4()
_NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Per-event round-trip
# ---------------------------------------------------------------------------


class TestPositionSnapshot:
    def test_round_trip(self) -> None:
        original = PositionSnapshot(
            deployment_id=_DEPLOYMENT_ID,
            instrument_id="AAPL.NASDAQ",
            qty=Decimal("100"),
            avg_price=Decimal("150.50"),
            unrealized_pnl=Decimal("250.00"),
            realized_pnl=Decimal("0"),
            ts=_NOW,
        )
        encoded = original.model_dump_json()
        decoded = PositionSnapshot.model_validate_json(encoded)
        assert decoded == original
        assert decoded.event_type == "position_snapshot"

    def test_extra_field_rejected(self) -> None:
        """``extra='forbid'`` — drift between writer and reader
        must be loud, not silent."""
        with pytest.raises(ValidationError):
            PositionSnapshot.model_validate(
                {
                    "deployment_id": str(_DEPLOYMENT_ID),
                    "instrument_id": "AAPL.NASDAQ",
                    "qty": "1",
                    "avg_price": "100",
                    "unrealized_pnl": "0",
                    "realized_pnl": "0",
                    "ts": _NOW.isoformat(),
                    "rogue_field": "should_not_be_allowed",
                }
            )


class TestFillEvent:
    def test_round_trip_buy(self) -> None:
        original = FillEvent(
            deployment_id=_DEPLOYMENT_ID,
            client_order_id="ord-12345",
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            qty=Decimal("10"),
            price=Decimal("150.25"),
            ts=_NOW,
        )
        decoded = FillEvent.model_validate_json(original.model_dump_json())
        assert decoded == original
        assert decoded.commission == Decimal("0")  # default

    def test_round_trip_sell_with_commission(self) -> None:
        original = FillEvent(
            deployment_id=_DEPLOYMENT_ID,
            client_order_id="ord-67890",
            instrument_id="MSFT.NASDAQ",
            side="SELL",
            qty=Decimal("5"),
            price=Decimal("400.00"),
            commission=Decimal("1.25"),
            ts=_NOW,
        )
        decoded = FillEvent.model_validate_json(original.model_dump_json())
        assert decoded.commission == Decimal("1.25")
        assert decoded.side == "SELL"

    def test_invalid_side_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FillEvent(
                deployment_id=_DEPLOYMENT_ID,
                client_order_id="ord-1",
                instrument_id="AAPL.NASDAQ",
                side="FLIP",  # type: ignore[arg-type]
                qty=Decimal("1"),
                price=Decimal("100"),
                ts=_NOW,
            )


class TestOrderStatusChange:
    @pytest.mark.parametrize(
        "status",
        [
            "submitted",
            "accepted",
            "filled",
            "partially_filled",
            "cancelled",
            "rejected",
            "denied",
        ],
    )
    def test_every_status_value_round_trips(self, status: str) -> None:
        original = OrderStatusChange(
            deployment_id=_DEPLOYMENT_ID,
            client_order_id="ord-1",
            status=status,  # type: ignore[arg-type]
            ts=_NOW,
        )
        decoded = OrderStatusChange.model_validate_json(original.model_dump_json())
        assert decoded.status == status

    def test_reason_optional(self) -> None:
        original = OrderStatusChange(
            deployment_id=_DEPLOYMENT_ID,
            client_order_id="ord-1",
            status="rejected",
            reason="insufficient buying power",
            ts=_NOW,
        )
        decoded = OrderStatusChange.model_validate_json(original.model_dump_json())
        assert decoded.reason == "insufficient buying power"


class TestAccountStateUpdate:
    def test_round_trip_with_defaults(self) -> None:
        original = AccountStateUpdate(
            deployment_id=_DEPLOYMENT_ID,
            account_id="DU1234567",
            balance=Decimal("100000.50"),
            ts=_NOW,
        )
        decoded = AccountStateUpdate.model_validate_json(original.model_dump_json())
        assert decoded.balance == Decimal("100000.50")
        assert decoded.margin_used == Decimal("0")
        assert decoded.margin_available == Decimal("0")


class TestRiskHaltEvent:
    def test_round_trip(self) -> None:
        original = RiskHaltEvent(
            deployment_id=_DEPLOYMENT_ID,
            reason="manual /kill-all",
            set_at=_NOW,
        )
        decoded = RiskHaltEvent.model_validate_json(original.model_dump_json())
        assert decoded == original


class TestDeploymentStatusEvent:
    @pytest.mark.parametrize(
        "status",
        [
            "starting",
            "building",
            "ready",
            "running",
            "stopping",
            "stopped",
            "failed",
        ],
    )
    def test_every_lifecycle_status_round_trips(self, status: str) -> None:
        original = DeploymentStatusEvent(
            deployment_id=_DEPLOYMENT_ID,
            status=status,  # type: ignore[arg-type]
            ts=_NOW,
        )
        decoded = DeploymentStatusEvent.model_validate_json(original.model_dump_json())
        assert decoded.status == status


# ---------------------------------------------------------------------------
# Discriminated union (InternalEvent)
# ---------------------------------------------------------------------------


class TestInternalEventUnion:
    """The WebSocket layer accepts ``InternalEvent`` and routes
    by ``event_type``. The discriminator must dispatch to the
    correct concrete model on parse."""

    @pytest.fixture
    def adapter(self) -> TypeAdapter:
        return TypeAdapter(InternalEvent)

    def test_position_snapshot_dispatches_correctly(self, adapter: TypeAdapter) -> None:
        original = PositionSnapshot(
            deployment_id=_DEPLOYMENT_ID,
            instrument_id="AAPL.NASDAQ",
            qty=Decimal("1"),
            avg_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            ts=_NOW,
        )
        encoded = original.model_dump_json()
        decoded = adapter.validate_json(encoded)
        assert isinstance(decoded, PositionSnapshot)

    def test_fill_event_dispatches_correctly(self, adapter: TypeAdapter) -> None:
        original = FillEvent(
            deployment_id=_DEPLOYMENT_ID,
            client_order_id="ord-1",
            instrument_id="AAPL.NASDAQ",
            side="BUY",
            qty=Decimal("1"),
            price=Decimal("100"),
            ts=_NOW,
        )
        decoded = adapter.validate_json(original.model_dump_json())
        assert isinstance(decoded, FillEvent)

    def test_unknown_event_type_rejected(self, adapter: TypeAdapter) -> None:
        with pytest.raises(ValidationError):
            adapter.validate_python(
                {
                    "event_type": "unknown_event_kind",
                    "deployment_id": str(_DEPLOYMENT_ID),
                }
            )
