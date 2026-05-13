"""GraduationCandidate model — a strategy config moving through the graduation pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from msai.models.live_deployment import LiveDeployment
    from msai.models.research_job import ResearchJob
    from msai.models.strategy import Strategy
    from msai.models.user import User


class GraduationCandidate(TimestampMixin, Base):
    """A strategy+config pair moving through the graduation pipeline.

    Stages (per ``services.graduation.VALID_TRANSITIONS``):
    discovery → validation → paper_candidate → paper_running → paper_review
    → live_candidate → live_running ↔ paused. Any stage can also → archived
    (terminal). ``paper_review`` can also regress → discovery for re-eval.
    Each stage transition is recorded as an immutable
    :class:`GraduationStageTransition` row.
    """

    __tablename__ = "graduation_candidates"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    research_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_jobs.id"), index=True, nullable=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False, server_default="discovery")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    deployment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("live_deployments.id"), index=True, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    strategy: Mapped[Strategy] = relationship(lazy="selectin")  # noqa: F821
    research_job: Mapped[ResearchJob] = relationship(lazy="selectin")  # noqa: F821
    deployment: Mapped[LiveDeployment] = relationship(lazy="selectin")  # noqa: F821
    promoter: Mapped[User] = relationship(lazy="selectin")  # noqa: F821
