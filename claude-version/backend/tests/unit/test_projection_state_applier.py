"""Unit tests for the StateApplier dispatch path (Phase 3 task 3.4).

We do NOT spin up a real Redis pubsub loop — instead we drive
``_dispatch`` directly with synthetic pub/sub messages and assert
the local ProjectionState is updated correctly. The async run()
loop is exercised by the integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from msai.services.nautilus.projection.events import (
    PositionSnapshot,
    RiskHaltEvent,
)
from msai.services.nautilus.projection.projection_state import ProjectionState
from msai.services.nautilus.projection.state_applier import StateApplier


def _build_applier() -> tuple[StateApplier, ProjectionState]:
    state = ProjectionState()
    # The redis client is unused for the _dispatch path; pass None
    # via type-ignore so we don't need a stub.
    applier = StateApplier(redis=None, projection_state=state)  # type: ignore[arg-type]
    return applier, state


def test_dispatch_position_snapshot_updates_state() -> None:
    applier, state = _build_applier()
    deployment_id = uuid4()
    event = PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id="AAPL.NASDAQ",
        qty=Decimal("100"),
        avg_price=Decimal("150"),
        unrealized_pnl=Decimal("5"),
        realized_pnl=Decimal("0"),
        ts=datetime.now(UTC),
    )

    msg = {"type": "pmessage", "data": event.model_dump_json().encode("utf-8")}
    applier._dispatch(msg)  # noqa: SLF001

    snapshot = state.get_position(deployment_id, "AAPL.NASDAQ")
    assert snapshot is not None
    assert snapshot.qty == Decimal("100")


def test_dispatch_risk_halt_marks_halted() -> None:
    applier, state = _build_applier()
    deployment_id = uuid4()
    halt = RiskHaltEvent(
        deployment_id=deployment_id,
        reason="DAILY_LOSS",
        set_at=datetime.now(UTC),
    )

    msg = {"type": "pmessage", "data": halt.model_dump_json()}  # str also accepted
    applier._dispatch(msg)  # noqa: SLF001

    assert state.is_halted(deployment_id) is True


def test_dispatch_invalid_payload_does_not_raise() -> None:
    applier, state = _build_applier()
    msg = {"type": "pmessage", "data": b'{"event_type": "garbage"}'}

    # Must NOT raise — a malformed message would otherwise crash
    # the StateApplier loop and stop the worker from receiving
    # any further updates (drift bug).
    applier._dispatch(msg)  # noqa: SLF001


def test_dispatch_with_none_data_does_nothing() -> None:
    applier, state = _build_applier()
    msg = {"type": "subscribe", "data": None}
    applier._dispatch(msg)  # noqa: SLF001
    # No exception, no state mutation
    assert len(state.get_positions(uuid4())) == 0


def test_dispatch_decodes_bytes_payload() -> None:
    applier, state = _build_applier()
    deployment_id = uuid4()
    event = PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id="MSFT.NASDAQ",
        qty=Decimal("50"),
        avg_price=Decimal("400"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=datetime.now(UTC),
    )

    msg = {"type": "pmessage", "data": event.model_dump_json().encode("utf-8")}
    applier._dispatch(msg)  # noqa: SLF001

    assert state.get_position(deployment_id, "MSFT.NASDAQ") is not None
