"""Internal event schema (Phase 3 task 3.3).

These Pydantic models are the **stable** contract between the
projection layer and the rest of MSAI (FastAPI WebSocket
broadcast, frontend live page, audit table, risk overlay).
They are deliberately decoupled from Nautilus's internal event
shape — the translator (3.4) maps Nautilus events to these
models, so a Nautilus version bump that renames a field on
``OrderFilled`` only requires a translator update, not a
frontend rewrite.

Why Pydantic and not msgspec/dataclass:

- Pydantic gives us free JSON serialization (the WebSocket
  layer needs ``model_dump_json()``) and validation (the
  StateApplier deserializes incoming pub/sub messages and we
  want a hard error on schema drift, not silent dict access).
- The performance overhead is irrelevant — these events live
  on a per-deployment Redis pub/sub channel, not the order
  hot path.

Six event types from the plan (with the discriminator field
``event_type`` so a single union type can be used at the
WebSocket boundary):

1. ``PositionSnapshot`` — full position state for one
   instrument inside one deployment. Emitted on every fill,
   on every cold-read hydration, and as a periodic heartbeat
   from the projection consumer.
2. ``FillEvent`` — atomic fill notification. One row per
   broker fill the audit table also writes.
3. ``OrderStatusChange`` — order state-machine transitions
   (submitted → accepted → filled / cancelled / rejected).
4. ``AccountStateUpdate`` — broker account snapshot
   (balance, margin used, margin available).
5. ``RiskHaltEvent`` — kill switch fired. The frontend uses
   this to flip the UI into "halted" mode.
6. ``DeploymentStatusEvent`` — supervisor-side row status
   change (starting → ready → running → stopped / failed).
   Lets the UI track the lifecycle WITHOUT polling
   ``/api/v1/live/status``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves at runtime
from decimal import Decimal  # noqa: TC003 — Pydantic resolves at runtime
from typing import Annotated, Literal
from uuid import UUID  # noqa: TC003 — Pydantic resolves at runtime

from pydantic import BaseModel, ConfigDict, Field


class _BaseEvent(BaseModel):
    """Common config for every event in the schema. Frozen +
    populates from attribute names so the translator can pass
    Nautilus event objects in via ``model_validate``."""

    model_config = ConfigDict(frozen=True, from_attributes=True, extra="forbid")


class PositionSnapshot(_BaseEvent):
    """Full position state for one instrument inside one
    deployment. Emitted on every fill, on cold-read hydration,
    and periodically by the consumer as a heartbeat."""

    event_type: Literal["position_snapshot"] = "position_snapshot"
    deployment_id: UUID
    instrument_id: str
    qty: Decimal
    avg_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    ts: datetime


class FillEvent(_BaseEvent):
    """Atomic fill notification — one row per broker fill the
    audit table also writes."""

    event_type: Literal["fill"] = "fill"
    deployment_id: UUID
    client_order_id: str
    instrument_id: str
    side: Literal["BUY", "SELL"]
    qty: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")
    ts: datetime


class OrderStatusChange(_BaseEvent):
    """Order state-machine transition: submitted → accepted →
    filled / cancelled / rejected. Used by the WebSocket layer
    to update the order ribbon in the live UI."""

    event_type: Literal["order_status"] = "order_status"
    deployment_id: UUID
    client_order_id: str
    status: Literal[
        "submitted",
        "accepted",
        "filled",
        "partially_filled",
        "cancelled",
        "rejected",
        "denied",
    ]
    reason: str | None = None
    ts: datetime


class AccountStateUpdate(_BaseEvent):
    """Broker account snapshot. ``balance`` is the total cash
    + margin position; ``margin_used`` and ``margin_available``
    feed the risk overlay."""

    event_type: Literal["account_state"] = "account_state"
    deployment_id: UUID
    account_id: str
    balance: Decimal
    margin_used: Decimal = Decimal("0")
    margin_available: Decimal = Decimal("0")
    ts: datetime


class RiskHaltEvent(_BaseEvent):
    """Kill switch fired (manual ``/kill-all`` or auto-halt
    from a risk breach). The frontend uses this to flip the
    UI into halted mode and disable trade-submit buttons."""

    event_type: Literal["risk_halt"] = "risk_halt"
    deployment_id: UUID
    reason: str
    set_at: datetime


class DeploymentStatusEvent(_BaseEvent):
    """Supervisor-side row status change (starting → ready →
    running → stopped / failed). Frontend tracks the lifecycle
    without polling ``/api/v1/live/status``."""

    event_type: Literal["deployment_status"] = "deployment_status"
    deployment_id: UUID
    status: Literal[
        "starting",
        "building",
        "ready",
        "running",
        "stopping",
        "stopped",
        "failed",
    ]
    ts: datetime


# Discriminated union — the WebSocket layer accepts any of these
# six event types and routes by ``event_type``. The
# ``Annotated[..., Field(discriminator="event_type")]`` tag tells
# Pydantic to use the discriminator field for fast tagged-union
# parsing instead of trying every model in turn.
InternalEvent = Annotated[
    PositionSnapshot
    | FillEvent
    | OrderStatusChange
    | AccountStateUpdate
    | RiskHaltEvent
    | DeploymentStatusEvent,
    Field(discriminator="event_type"),
]
