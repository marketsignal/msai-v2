"""Nautilus event → internal event translator (Phase 3 task 3.4).

The :class:`ProjectionConsumer` reads raw bytes from the
Nautilus message bus stream, deserializes them via Nautilus's
own ``MsgSpecSerializer``, and hands the result to this
module. The translator routes by the Nautilus topic prefix
and converts each Nautilus event into the corresponding
:mod:`msai.services.nautilus.projection.events` model.

Why a separate module instead of inlining in the consumer:

- Pure functions are unit-testable without Redis / streams /
  Nautilus subprocesses. We seed a fake Nautilus event dict
  and assert the right internal model comes out.
- A future Nautilus upgrade only changes this module — the
  consumer stays version-stable.

Routing table:

- ``events.position.*`` → :class:`PositionSnapshot`
- ``events.order.filled`` → :class:`FillEvent`
- ``events.order.{submitted,accepted,rejected,cancelled,denied,partially_filled}``
  → :class:`OrderStatusChange`
- ``events.account.state`` → :class:`AccountStateUpdate`

Unrouted topics return ``None`` so the consumer can XACK them
without forwarding. The plan calls for routing to be
extensible — the dispatch table is a public dict keyed by
topic prefix so future event types can be added without
touching the consumer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    FillEvent,
    OrderStatusChange,
    PositionSnapshot,
)

if TYPE_CHECKING:

    from msai.services.nautilus.projection.events import InternalEvent


def _ts_ns_to_dt(ts_ns: int | str | None) -> datetime:
    """Convert Nautilus's int64-nanoseconds-since-epoch
    timestamp to a UTC ``datetime``. Falls back to ``now()``
    if the field is missing — better than failing the whole
    event when only one timestamp is bad.

    The input may be an ``int`` (raw nanoseconds) or a ``str``
    (Nautilus's ``MsgSpecSerializer(timestamps_as_str=True)``
    stringifies int64 timestamps to avoid Redis 17-digit
    precision loss — see ``serialization/serializer.pyx`` and
    ``cache/database.pyx:121-128``). Both shapes are accepted
    — Codex batch 8 P0 fix.
    """
    if ts_ns is None or ts_ns == "":
        return datetime.now(UTC)
    return datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=UTC)


def _decimal(value: Any) -> Decimal:
    """Coerce any Nautilus-serialized number to a ``Decimal``.
    Nautilus encodes ``Quantity`` / ``Price`` / ``Money`` as
    strings; floats and ints are also accepted defensively."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _split_strategy_id(strategy_id: str) -> UUID:
    """Strategy IDs Nautilus emits are
    ``f"{StrategyClassName}-{deployment_slug}"`` (Task 1.5
    deterministic identity contract). The deployment_slug is
    the LAST hyphen-separated component, and we look up the
    deployment_id by slug from a registry the consumer
    maintains.

    For pure unit-testing of the translator, we accept either
    a slug-only string OR a fully-qualified strategy_id and
    extract the slug. The consumer wires a real
    ``slug → deployment_id`` resolver via a callback so the
    translator stays free of DB / Redis dependencies.

    This helper exists to make the test surface clean — it
    raises ``ValueError`` if the input doesn't look like a
    Nautilus strategy id, so a malformed Nautilus event
    surfaces as a translator error rather than silently
    routing to a wrong deployment.
    """
    if not strategy_id:
        raise ValueError("strategy_id is empty")
    # Pure helper — the slug-to-deployment-id lookup happens
    # at the consumer layer via a callback. The translator
    # only knows how to extract the slug.
    return _uuid_from_slug_or_id(strategy_id.rsplit("-", 1)[-1])


def _uuid_from_slug_or_id(value: str) -> UUID:
    """Best-effort UUID coercion. The consumer's resolver
    converts deployment_slug → deployment_id (UUID) via a DB
    lookup. For unit-testing the translator we let the test
    pass a UUID hex directly."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(
            f"could not coerce {value!r} to UUID — translator "
            "expects the consumer to resolve slug → deployment_id "
            "before calling translate()"
        ) from exc


# ---------------------------------------------------------------------------
# Public translator API
# ---------------------------------------------------------------------------


def translate(
    *,
    topic: str,
    event_dict: dict[str, Any],
    deployment_id: UUID,
) -> InternalEvent | None:
    """Convert one Nautilus event (decoded from the message
    bus stream) into an :class:`InternalEvent`, or ``None`` if
    the topic isn't routed.

    Args:
        topic: The Nautilus message bus topic
            (e.g. ``"events.order.filled"``).
        event_dict: The decoded event payload as a plain dict
            — the consumer calls ``MsgSpecSerializer.deserialize``
            and passes the result here.
        deployment_id: The deployment_id the consumer
            resolved from the trader_id (which carries the
            slug per Task 1.5).

    Returns:
        An ``InternalEvent`` instance or ``None`` for
        unrouted topics. ``None`` causes the consumer to ACK
        the message without forwarding to the pub/sub
        channels — important so unrouted Nautilus events don't
        accumulate in the PEL.
    """
    # Position topics — single dispatch handler
    if topic.startswith("events.position."):
        return _translate_position(event_dict, deployment_id)

    if topic == "events.order.filled":
        return _translate_fill(event_dict, deployment_id)

    if topic.startswith("events.order."):
        return _translate_order_status(topic, event_dict, deployment_id)

    if topic == "events.account.state":
        return _translate_account_state(event_dict, deployment_id)

    return None


def _translate_position(event_dict: dict[str, Any], deployment_id: UUID) -> PositionSnapshot:
    """Map a Nautilus ``Position*`` event to
    :class:`PositionSnapshot`. Nautilus emits separate
    ``PositionOpened`` / ``PositionChanged`` / ``PositionClosed``
    events but they all carry the SAME shape for the fields
    we care about (instrument_id, qty, avg_px, unrealized,
    realized) — the snapshot model is the union."""
    return PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id=str(event_dict.get("instrument_id", "")),
        qty=_decimal(event_dict.get("quantity") or event_dict.get("net_qty")),
        avg_price=_decimal(event_dict.get("avg_px_open") or event_dict.get("avg_price")),
        unrealized_pnl=_decimal(event_dict.get("unrealized_pnl")),
        realized_pnl=_decimal(event_dict.get("realized_pnl")),
        ts=_ts_ns_to_dt(event_dict.get("ts_event") or event_dict.get("ts_init")),
    )


def _translate_fill(event_dict: dict[str, Any], deployment_id: UUID) -> FillEvent:
    side_raw = str(event_dict.get("order_side") or event_dict.get("side") or "").upper()
    if side_raw not in {"BUY", "SELL"}:
        raise ValueError(f"unrecognized fill side: {side_raw!r}")
    return FillEvent(
        deployment_id=deployment_id,
        client_order_id=str(event_dict.get("client_order_id", "")),
        instrument_id=str(event_dict.get("instrument_id", "")),
        side=side_raw,  # type: ignore[arg-type]
        qty=_decimal(event_dict.get("last_qty") or event_dict.get("quantity")),
        price=_decimal(event_dict.get("last_px") or event_dict.get("price")),
        commission=_decimal(event_dict.get("commission")),
        ts=_ts_ns_to_dt(event_dict.get("ts_event") or event_dict.get("ts_init")),
    )


_ORDER_STATUS_BY_TOPIC: dict[str, str] = {
    "events.order.submitted": "submitted",
    "events.order.accepted": "accepted",
    "events.order.partially_filled": "partially_filled",
    "events.order.cancelled": "cancelled",
    "events.order.canceled": "cancelled",  # Nautilus uses both spellings
    "events.order.rejected": "rejected",
    "events.order.denied": "denied",
}


def _translate_order_status(
    topic: str, event_dict: dict[str, Any], deployment_id: UUID
) -> OrderStatusChange | None:
    status = _ORDER_STATUS_BY_TOPIC.get(topic)
    if status is None:
        return None  # Unknown order topic — let the consumer ACK it
    return OrderStatusChange(
        deployment_id=deployment_id,
        client_order_id=str(event_dict.get("client_order_id", "")),
        status=status,  # type: ignore[arg-type]
        reason=str(event_dict.get("reason")) if event_dict.get("reason") else None,
        ts=_ts_ns_to_dt(event_dict.get("ts_event") or event_dict.get("ts_init")),
    )


def _translate_account_state(event_dict: dict[str, Any], deployment_id: UUID) -> AccountStateUpdate:
    """Map a Nautilus ``AccountState`` event to
    :class:`AccountStateUpdate`. Nautilus emits ``balances`` and
    ``margins`` arrays, NOT flat fields (Codex batch 8 P1 fix —
    earlier code expected ``balance`` / ``margin_used`` /
    ``margin_available`` directly, which collapsed every
    streamed update to zeros).

    The shape comes from
    ``model/events/account.pyx:to_dict_c``::

        {
            "account_id": "DU12345",
            "balances": [
                {"total": "100000.00", "locked": "5000.00",
                 "free": "95000.00", "currency": "USD"},
            ],
            "margins": [
                {"initial": "10.00", "maintenance": "5.00",
                 "currency": "USD", "instrument_id": "..."},
            ],
            "ts_event": ...,
            ...
        }

    We only care about the FIRST balance row (single-currency
    accounts in Phase 1) and the SUM of margin_init across
    margin rows (matches the IB margin model).
    """
    balance = Decimal("0")
    margin_available = Decimal("0")

    balances = event_dict.get("balances") or []
    if balances:
        first = balances[0]
        balance = _decimal(first.get("total"))
        margin_available = _decimal(first.get("free"))

    margin_used = Decimal("0")
    margins = event_dict.get("margins") or []
    for m in margins:
        # ``initial`` is the margin reservation per Nautilus's
        # ``MarginBalance.to_dict``. Sum across instrument rows.
        margin_used += _decimal(m.get("initial"))
    if not margins and balances:
        # Fall back to the AccountBalance "locked" field — for
        # cash accounts Nautilus reports zero margins and the
        # locked column carries the same information.
        margin_used = _decimal(balances[0].get("locked"))

    return AccountStateUpdate(
        deployment_id=deployment_id,
        account_id=str(event_dict.get("account_id", "")),
        balance=balance,
        margin_used=margin_used,
        margin_available=margin_available,
        ts=_ts_ns_to_dt(event_dict.get("ts_event") or event_dict.get("ts_init")),
    )


# Public dispatch table for callers that want to extend the
# translator without monkey-patching. Keys are topic prefixes;
# values are functions ``(event_dict, deployment_id) → InternalEvent | None``.
TopicHandler = "Callable[[dict[str, Any], UUID], InternalEvent | None]"
