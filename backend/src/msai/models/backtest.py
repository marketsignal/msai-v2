"""Backtest model — a historical simulation run for a given strategy and configuration."""

from __future__ import annotations

from datetime import (  # noqa: TC003 — SQLAlchemy needs concrete types at Mapped[] reflection
    date,
    datetime,
)
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4  # noqa: TC003 — same reason

if TYPE_CHECKING:
    from msai.models.strategy import Strategy
    from msai.models.user import User

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class Backtest(Base):
    """A single backtest execution record.

    Tracks the strategy version (via ``strategy_code_hash`` and optional
    ``strategy_git_sha``), the configuration used, date range, execution
    status, and resulting performance metrics.

    Note: This model intentionally uses ``created_at`` only (no ``updated_at``)
    because backtests are immutable after creation — status transitions are
    append-only state changes, not logical edits.
    """

    __tablename__ = "backtests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # --- Canonical analytics series ------------------------------------
    # ``series`` holds the canonical daily-normalized payload
    # (:class:`msai.schemas.backtest.SeriesPayload`) — equity curve,
    # drawdown series, daily returns, monthly aggregation — used by both
    # the native React charts and the /results endpoint. Nullable because
    # legacy rows (pre-migration) and failed-materialize rows carry no
    # payload.
    # ``series_status`` disambiguates ``ready`` (payload populated),
    # ``not_materialized`` (legacy / never computed; DB DEFAULT), and
    # ``failed`` (worker hit an error while building the payload — the
    # backtest itself may still be ``completed``).
    series: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    series_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default="not_materialized",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- Error classification ------------------------------------------
    # Populated by the worker at ``_mark_backtest_failed`` time via
    # ``services/backtests/classifier.py``. Read back by the API's
    # ``_build_error_envelope`` helper, which returns ``None`` for non-failed
    # rows and uses :meth:`FailureCode.parse_or_unknown` + sanitizer for
    # pre-migration rows that carry ``error_code == 'unknown'`` + a raw
    # ``error_message`` but no ``error_public_message``.
    error_code: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown")
    error_public_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_remediation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # --- Auto-heal lifecycle (added by the backtest-auto-ingest PR) -------
    # Populated by ``services/backtests/auto_heal.py`` while the worker
    # waits for a triggered ingest job to complete. All four are cleared
    # together when the heal reaches a terminal state.
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    heal_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heal_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Data lineage fields — captured before each backtest run so every result
    # can be traced back to the exact software versions and data files used.
    nautilus_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    python_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    data_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Job lifecycle fields (used by the watchdog to detect stale/orphaned jobs)
    queue_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    queue_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    strategy: Mapped[Strategy] = relationship(lazy="selectin")  # noqa: F821
    creator: Mapped[User] = relationship(lazy="selectin")  # noqa: F821

    __table_args__ = (
        CheckConstraint(
            "series_status IN ('ready', 'not_materialized', 'failed')",
            name="ck_backtests_series_status",
        ),
    )
