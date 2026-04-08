"""OrderAttemptAudit model — every order intent recorded for auditability.

Phase 1 task 1.2 (decision: Codex finding #7 — ``client_order_id`` is the
stable correlation key).

Every order the platform attempts — live OR backtest, submitted OR
denied — gets a row here. The live audit hook (Task 1.11) generates the
``client_order_id`` UUID, writes the initial ``submitted`` row, and then
looks the row up by ``client_order_id`` to update through accepted →
filled. The backtest runner (Task 4.4) writes equivalent rows with
``backtest_id`` populated and ``is_live=False`` so we can do
backtest-vs-production comparison after the fact.

State machine values for ``status``:

- ``submitted``        — order has been sent to the broker
- ``accepted``         — broker acknowledged
- ``filled``           — fully filled
- ``partially_filled`` — partial fill (further updates may follow)
- ``cancelled``        — caller cancelled or broker timed out
- ``rejected``         — broker rejected (reason in ``reason``)
- ``denied``           — risk engine blocked before submission; never
                         reached the broker. ``broker_order_id`` stays NULL.

Constraint: every row MUST belong to either a live deployment OR a
backtest (CHECK constraint at the DB level), so an audit row can never
be orphaned from its execution context.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — required at runtime for SQLAlchemy Mapped[]
from decimal import Decimal  # noqa: TC003 — required at runtime for SQLAlchemy Mapped[]
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class OrderAttemptAudit(Base, TimestampMixin):
    """Audit record for a single order attempt (live or backtest).

    Identified by the random ``client_order_id`` UUID generated at submit
    time. The audit hook updates the row through its lifecycle by
    re-querying on ``client_order_id``, so that column is the correlation
    key and is enforced UNIQUE at the DB layer.
    """

    __tablename__ = "order_attempt_audits"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    client_order_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, unique=True
    )
    """Stable correlation key the audit hook uses to update a row through
    its state machine. Generated at submit time, persisted on the broker
    side as the order's client tag, and re-used by every subsequent
    UPDATE on this row."""

    deployment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("live_deployments.id"), index=True, nullable=True
    )
    """The live deployment that issued the order, or NULL if this row
    came from a backtest. Exactly one of ``deployment_id`` /
    ``backtest_id`` must be non-NULL — enforced via CHECK constraint."""

    backtest_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("backtests.id"), index=True, nullable=True
    )
    """The backtest run that issued the order, or NULL if this row came
    from a live deployment."""

    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    """Always populated — the strategy that authored the order, regardless
    of whether it was live or backtest."""

    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """sha256 of the strategy file at submit time. Pinning the row to a
    specific code version means we can replay the same order intent
    against historical code without losing the link to the execution."""

    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    """Optional 40-char git SHA, when the strategy file lives inside a
    git checkout (production deploys)."""

    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    """Canonical Nautilus instrument id, e.g. ``"AAPL.NASDAQ"``."""

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    """``BUY`` or ``SELL``."""

    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    """Order quantity in units of the instrument. ``Numeric(20, 8)`` is
    enough for any equity, future, or crypto size we trade."""

    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    """Limit price, or NULL for market orders. Same precision as
    ``quantity`` so we never lose digits to float drift."""

    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    """``MARKET``, ``LIMIT``, ``STOP``, ``STOP_LIMIT``, etc."""

    ts_attempted: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """Wall-clock UTC timestamp at the moment the order intent was
    generated. Distinct from ``created_at`` (the DB insert time) — these
    can differ noticeably if the audit hook batches writes."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    """One of: ``submitted``, ``accepted``, ``filled``, ``partially_filled``,
    ``cancelled``, ``rejected``, ``denied``. State machine documented in
    the module docstring."""

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Free-form reason text — broker rejection message, risk engine
    denial reason, etc. NULL on the success path."""

    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """The broker-side order id, populated once the broker accepts.
    Indexed because the broker reconciliation pass on restart looks
    rows up by this column. NULL until ``status`` reaches ``accepted``,
    and stays NULL forever on the ``denied`` path (the order never
    reached the broker)."""

    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    """``True`` for live orders, ``False`` for backtest orders. Lets us
    filter the table for live-only analytics without having to JOIN to
    ``live_deployments`` / ``backtests``."""

    __table_args__ = (
        CheckConstraint(
            # Enforce exactly-one (XOR): Postgres treats ``!=`` on
            # booleans as xor, so this rejects rows where BOTH fields
            # are NULL AND rows where BOTH are populated. Populating
            # both would create an ambiguous audit row downstream
            # reconciliation / analytics can't classify (Codex Task
            # 1.2 iter2 P2 fix — was "at least one" before).
            "(deployment_id IS NOT NULL) != (backtest_id IS NOT NULL)",
            name="ck_order_attempt_audits_deployment_or_backtest",
        ),
    )
