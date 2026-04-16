"""DB-backed readers for the WebSocket reconnect snapshot.

Claude-version's live WebSocket uses an event-driven architecture
(``ProjectionConsumer`` → per-deployment Redis pub/sub channel →
forwarded verbatim to connected clients). Connected clients never
miss events. BUT when a client reconnects it only gets what the
``ProjectionState`` currently holds — positions and account.

These readers fill the reconnect gap for state that is NOT kept in
``ProjectionState`` (orders + trades) by querying the authoritative
DB tables the audit pipeline already writes:

- Open orders come from :class:`OrderAttemptAudit` — filtered to
  still-open statuses (``submitted``, ``accepted``,
  ``partially_filled``). Terminal statuses (``filled``, ``cancelled``,
  ``rejected``, ``denied``) are excluded from the *open* view but
  live-filled ones show up in the trade list below.
- Recent trades come from :class:`Trade` — keyed on deployment_id
  and ordered by ``executed_at DESC``. This is the same table the
  ``/live/trades`` endpoint reads, just scoped to one deployment.

This module exists *in addition to* claude's ``PositionReader``
(which reads positions + account from the Nautilus Cache / Redis
via cold-path or the hydrated ``ProjectionState``). Orders and
trades are not in the Nautilus Cache layer — they live in the
audit tables only — so a DB read is the only authoritative source
for reconnect snapshots.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — required at runtime by SQLAlchemy annotations

from sqlalchemy import select

from msai.models.order_attempt_audit import OrderAttemptAudit
from msai.models.trade import Trade

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Order statuses considered "still open" — matches the
# ``OrderAttemptAudit`` state machine (see ``models/order_attempt_audit.py``
# lines 14-31). Anything outside this set is terminal and not worth
# replaying on reconnect.
OPEN_ORDER_STATUSES: tuple[str, ...] = ("submitted", "accepted", "partially_filled")

_DEFAULT_TRADES_LIMIT = 50
"""Tails the trades blotter on reconnect so UI has history to render.
Matches the ``/live/trades`` endpoint's default page size."""


async def load_open_orders_for_deployment(
    session: AsyncSession,
    deployment_id: UUID,
) -> list[dict[str, Any]]:
    """Load still-open order attempts for one deployment.

    Returns newest-first so the reconnecting UI can paint the
    order ribbon in the same order the live event stream would.

    The shape matches what the ``/live/audits/{deployment_id}``
    endpoint already serves — picked intentionally so the frontend
    can reuse its existing type. Fresh ``OrderStatusChange`` events
    on the pub/sub channel will replace rows by ``client_order_id``.
    """
    rows = (
        (
            await session.execute(
                select(OrderAttemptAudit)
                .where(
                    OrderAttemptAudit.deployment_id == deployment_id,
                    OrderAttemptAudit.status.in_(OPEN_ORDER_STATUSES),
                )
                .order_by(OrderAttemptAudit.ts_attempted.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "client_order_id": row.client_order_id,
            "instrument_id": row.instrument_id,
            "side": row.side,
            "quantity": str(row.quantity),
            "price": str(row.price) if row.price is not None else None,
            "order_type": row.order_type,
            "status": row.status,
            "reason": row.reason,
            "broker_order_id": row.broker_order_id,
            "ts_attempted": row.ts_attempted.isoformat(),
        }
        for row in rows
    ]


async def load_recent_trades_for_deployment(
    session: AsyncSession,
    deployment_id: UUID,
    *,
    limit: int = _DEFAULT_TRADES_LIMIT,
) -> list[dict[str, Any]]:
    """Load the most recent fills for one deployment.

    Ordered newest-first by ``executed_at`` so the reconnecting UI
    lays the trade blotter out in the same order the live fill
    stream would. Defaults to 50 rows — enough for a full trading
    session's context without dragging the snapshot payload into
    the hundreds of KB for heavy days.
    """
    if limit <= 0:
        return []
    rows = (
        (
            await session.execute(
                select(Trade)
                .where(Trade.deployment_id == deployment_id)
                .order_by(Trade.executed_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "deployment_id": str(row.deployment_id) if row.deployment_id else None,
            "instrument": row.instrument,
            "side": row.side,
            "quantity": str(row.quantity),
            "price": str(row.price),
            "commission": str(row.commission) if row.commission is not None else None,
            "broker_trade_id": row.broker_trade_id,
            "client_order_id": row.client_order_id,
            "pnl": str(row.pnl) if row.pnl is not None else None,
            "is_live": row.is_live,
            "executed_at": row.executed_at.isoformat(),
        }
        for row in rows
    ]
