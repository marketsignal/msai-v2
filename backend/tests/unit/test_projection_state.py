"""Unit tests for ProjectionState — the in-memory rolling
state used by every uvicorn worker (Phase 3 task 3.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    DeploymentStatusEvent,
    FillEvent,
    OrderStatusChange,
    PositionSnapshot,
    RiskHaltEvent,
)
from msai.services.nautilus.projection.projection_state import ProjectionState

NOW = datetime.now(UTC)


def _position(deployment_id, instrument_id="AAPL.NASDAQ", qty="100") -> PositionSnapshot:
    return PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id=instrument_id,
        qty=Decimal(qty),
        avg_price=Decimal("150.00"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=NOW,
    )


def _account(deployment_id) -> AccountStateUpdate:
    return AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("100000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        ts=NOW,
    )


def test_apply_position_stores_per_instrument() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    state.apply(_position(deployment_id, "AAPL.NASDAQ", "100"))
    state.apply(_position(deployment_id, "MSFT.NASDAQ", "50"))

    positions = state.get_positions(deployment_id)
    assert set(positions.keys()) == {"AAPL.NASDAQ", "MSFT.NASDAQ"}
    assert positions["AAPL.NASDAQ"].qty == Decimal("100")
    assert positions["MSFT.NASDAQ"].qty == Decimal("50")


def test_apply_position_overwrites_same_instrument() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    state.apply(_position(deployment_id, "AAPL.NASDAQ", "100"))
    state.apply(_position(deployment_id, "AAPL.NASDAQ", "150"))

    snapshot = state.get_position(deployment_id, "AAPL.NASDAQ")
    assert snapshot is not None
    assert snapshot.qty == Decimal("150")


def test_get_positions_returns_copy_not_internal_reference() -> None:
    state = ProjectionState()
    deployment_id = uuid4()
    state.apply(_position(deployment_id))

    snapshot_a = state.get_positions(deployment_id)
    snapshot_a.clear()

    # Mutating the snapshot must NOT affect internal state
    assert "AAPL.NASDAQ" in state.get_positions(deployment_id)


def test_apply_account_replaces_previous() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    state.apply(_account(deployment_id))
    new_account = AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("110000"),
        margin_used=Decimal("100"),
        margin_available=Decimal("109900"),
        ts=NOW,
    )
    state.apply(new_account)

    account = state.get_account(deployment_id)
    assert account is not None
    assert account.balance == Decimal("110000")


def test_apply_risk_halt_marks_deployment_halted() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    halt = RiskHaltEvent(
        deployment_id=deployment_id,
        reason="DAILY_LOSS_LIMIT",
        set_at=NOW,
    )
    state.apply(halt)

    assert state.is_halted(deployment_id) is True
    assert state.get_halt(deployment_id) is not None


def test_unhalted_deployment_returns_false() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    assert state.is_halted(deployment_id) is False
    assert state.get_halt(deployment_id) is None


def test_apply_deployment_status_stores_latest() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    starting = DeploymentStatusEvent(
        deployment_id=deployment_id,
        status="starting",
        ts=NOW,
    )
    running = DeploymentStatusEvent(
        deployment_id=deployment_id,
        status="running",
        ts=NOW,
    )
    state.apply(starting)
    state.apply(running)

    status = state.get_status(deployment_id)
    assert status is not None
    assert status.status == "running"


def test_fill_event_is_passthrough_not_persisted() -> None:
    state = ProjectionState()
    deployment_id = uuid4()
    fill = FillEvent(
        deployment_id=deployment_id,
        client_order_id="ord-1",
        instrument_id="AAPL.NASDAQ",
        side="BUY",
        qty=Decimal("10"),
        price=Decimal("150"),
        commission=Decimal("1"),
        ts=NOW,
    )
    state.apply(fill)

    # FillEvent is not state-affecting; nothing was added
    assert state.get_positions(deployment_id) == {}
    assert state.has_deployment(deployment_id) is False


def test_order_status_change_is_passthrough() -> None:
    state = ProjectionState()
    deployment_id = uuid4()
    change = OrderStatusChange(
        deployment_id=deployment_id,
        client_order_id="ord-2",
        status="accepted",
        reason=None,
        ts=NOW,
    )
    state.apply(change)
    assert state.has_deployment(deployment_id) is False


def test_forget_removes_all_state_for_deployment() -> None:
    state = ProjectionState()
    deployment_id = uuid4()
    other_id = uuid4()

    state.apply(_position(deployment_id))
    state.apply(_account(deployment_id))
    state.apply(_position(other_id))

    state.forget(deployment_id)

    assert state.get_positions(deployment_id) == {}
    assert state.get_account(deployment_id) is None
    # Other deployment must be untouched
    assert "AAPL.NASDAQ" in state.get_positions(other_id)


def test_has_deployment_after_position_apply() -> None:
    state = ProjectionState()
    deployment_id = uuid4()

    state.apply(_position(deployment_id))

    assert state.has_deployment(deployment_id) is True


def test_positions_filters_closed_positions() -> None:
    """Codex batch 8 P1 regression: ``positions()`` (the
    PositionReader fast path) must filter out closed positions
    (qty == 0) so it matches the cold path's
    ``cache.positions_open()`` behavior. Without this filter,
    the fast path would serve closed positions indefinitely
    after a ``events.position.closed`` event."""
    state = ProjectionState()
    deployment_id = uuid4()

    # Open AAPL with qty=100
    state.apply(_position(deployment_id, "AAPL.NASDAQ", "100"))
    # Open MSFT with qty=50
    state.apply(_position(deployment_id, "MSFT.NASDAQ", "50"))
    # Close AAPL (qty=0 marks closed)
    state.apply(_position(deployment_id, "AAPL.NASDAQ", "0"))

    open_positions = state.positions(deployment_id)
    assert len(open_positions) == 1
    assert open_positions[0].instrument_id == "MSFT.NASDAQ"


def test_hydrate_account_with_none_marks_hydrated() -> None:
    """Codex batch 8 P1 regression: passing ``None`` as the
    account argument must flip ``is_account_hydrated`` to True
    so the next read serves ``None`` from the fast path
    instead of cold-reading again."""
    state = ProjectionState()
    deployment_id = uuid4()

    state.hydrate_from_cold_read(deployment_id, account=None)

    assert state.is_account_hydrated(deployment_id) is True
    assert state.account(deployment_id) is None


def test_hydrate_account_omitted_does_not_mark_hydrated() -> None:
    """Omitting the ``account`` argument leaves the account
    domain in the cold state — sentinel-distinct from passing
    ``None`` explicitly."""
    state = ProjectionState()
    deployment_id = uuid4()

    state.hydrate_from_cold_read(
        deployment_id,
        positions=[_position(deployment_id)],
    )

    assert state.is_positions_hydrated(deployment_id) is True
    assert state.is_account_hydrated(deployment_id) is False
