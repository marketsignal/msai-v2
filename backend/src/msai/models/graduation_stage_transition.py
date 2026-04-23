"""GraduationStageTransition model — immutable audit trail for graduation pipeline stage changes."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base

if TYPE_CHECKING:
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.user import User


class GraduationStageTransition(Base):
    """An immutable record of a graduation candidate moving between stages.

    This table is append-only — rows are never updated or deleted.  It provides
    a complete audit trail for the graduation pipeline.

    Note: Uses BigInteger autoincrement PK (not UUID) for efficient sequential
    inserts on an append-only audit table.  No ``updated_at`` column since rows
    are immutable.
    """

    __tablename__ = "graduation_stage_transitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("graduation_candidates.id", ondelete="CASCADE"), index=True, nullable=False
    )
    from_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    transitioned_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    candidate: Mapped[GraduationCandidate] = relationship(lazy="selectin")  # noqa: F821
    transitioner: Mapped[User] = relationship(lazy="selectin")  # noqa: F821
