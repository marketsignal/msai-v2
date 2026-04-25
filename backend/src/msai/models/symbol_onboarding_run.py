"""SymbolOnboardingRun — one row per ``POST /api/v1/symbols/onboard`` request.

Owns the run-level status machine (``pending`` → ``in_progress`` →
terminal) plus per-symbol sub-states under ``symbol_states`` JSONB.
Single worker task writes this row; no cross-row coordination.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime  # noqa: TC003 — SQLA Mapped[...] resolves at runtime
from decimal import Decimal  # noqa: TC003 — SQLA Mapped[...] resolves at runtime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, Numeric, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class SymbolOnboardingRunStatus(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


class SymbolOnboardingRun(Base):
    __tablename__ = "symbol_onboarding_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','completed','completed_with_failures','failed')",
            name="ck_symbol_onboarding_runs_status",
        ),
        CheckConstraint(
            "cost_ceiling_usd IS NULL OR cost_ceiling_usd >= 0",
            name="ck_symbol_onboarding_runs_cost_ceiling_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    watchlist_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        Enum(
            SymbolOnboardingRunStatus,
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SymbolOnboardingRunStatus.PENDING.value,
    )
    # Per-symbol state map:
    # { "<symbol>": {"status": "...", "step": "...", "error": {...}|null, "next_action": str|null,
    #                "asset_class": str, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} }
    symbol_states: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    request_live_qualification: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Idempotency key — hex-encoded digest of (watchlist_name, sorted_symbols,
    # request_live_qualification). Indexed + UNIQUE so a duplicate POST can
    # look up the existing run in O(log n) and return its id. Stored as text
    # rather than the raw int so it matches the arq ``_job_id`` string passed
    # to ``pool.enqueue_job``.
    job_id_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    cost_ceiling_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
