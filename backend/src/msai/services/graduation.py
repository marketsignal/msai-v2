"""Graduation pipeline -- manages strategy promotion through stages.

Strategies progress: discovery -> validation -> paper_candidate -> paper_running ->
paper_review -> live_candidate -> live_running. They can be paused or archived at
any point. Invalid transitions are rejected with HTTP 422.

Every stage change creates an immutable audit trail row in graduation_stage_transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.graduation_stage_transition import GraduationStageTransition
from msai.models.strategy import Strategy

log = get_logger(__name__)


class GraduationStageError(Exception):
    """Raised when a stage transition is invalid."""


class GraduationService:
    """Manages graduation candidate lifecycle with enforced state machine transitions."""

    VALID_TRANSITIONS: dict[str, set[str]] = {
        "discovery": {"validation", "archived"},
        "validation": {"paper_candidate", "archived"},
        "paper_candidate": {"paper_running", "archived"},
        "paper_running": {"paper_review", "archived"},
        "paper_review": {"live_candidate", "discovery", "archived"},
        "live_candidate": {"live_running", "archived"},
        "live_running": {"paused", "archived"},
        "paused": {"live_running", "archived"},
        "archived": set(),  # terminal state -- no transitions out
    }

    ALL_STAGES: set[str] = set(VALID_TRANSITIONS)

    async def create_candidate(
        self,
        session: AsyncSession,
        *,
        strategy_id: UUID,
        config: dict[str, Any],
        metrics: dict[str, Any],
        research_job_id: UUID | None = None,
        notes: str | None = None,
        user_id: UUID | None = None,
    ) -> GraduationCandidate:
        """Create a new candidate starting in 'discovery' stage."""
        # Validate strategy exists
        strategy = await session.get(Strategy, strategy_id)
        if strategy is None:
            raise ValueError(f"Strategy {strategy_id} not found")

        # Validate research_job_id if provided
        if research_job_id is not None:
            from msai.models.research_job import ResearchJob

            job = await session.get(ResearchJob, research_job_id)
            if job is None:
                raise ValueError(f"Research job {research_job_id} not found")

        candidate = GraduationCandidate(
            strategy_id=strategy_id,
            research_job_id=research_job_id,
            stage="discovery",
            config=config,
            metrics=metrics,
            notes=notes,
            promoted_by=user_id,
            promoted_at=datetime.now(UTC),
        )
        session.add(candidate)
        await session.flush()

        # Record the initial "creation" transition
        transition = GraduationStageTransition(
            candidate_id=candidate.id,
            from_stage="",  # no previous stage
            to_stage="discovery",
            reason="Candidate created",
            transitioned_by=user_id,
        )
        session.add(transition)
        await session.flush()

        log.info(
            "graduation_candidate_created",
            candidate_id=str(candidate.id),
            strategy_id=str(strategy_id),
        )
        return candidate

    async def update_stage(
        self,
        session: AsyncSession,
        candidate_id: UUID,
        *,
        new_stage: str,
        reason: str | None = None,
        user_id: UUID | None = None,
    ) -> GraduationCandidate:
        """Advance candidate to a new stage. Raises GraduationStageError if invalid."""
        candidate = await session.get(GraduationCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate {candidate_id} not found")

        current = candidate.stage
        allowed = self.VALID_TRANSITIONS.get(current, set())

        if new_stage not in allowed:
            raise GraduationStageError(
                f"Cannot transition from '{current}' to '{new_stage}'. "
                f"Allowed transitions: {sorted(allowed) if allowed else 'none (terminal state)'}"
            )

        old_stage = candidate.stage
        candidate.stage = new_stage

        transition = GraduationStageTransition(
            candidate_id=candidate_id,
            from_stage=old_stage,
            to_stage=new_stage,
            reason=reason,
            transitioned_by=user_id,
        )
        session.add(transition)
        await session.flush()

        log.info(
            "graduation_stage_updated",
            candidate_id=str(candidate_id),
            from_stage=old_stage,
            to_stage=new_stage,
        )
        return candidate

    async def list_candidates(
        self,
        session: AsyncSession,
        *,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[GraduationCandidate]:
        """List candidates, optionally filtered by stage."""
        stmt = (
            select(GraduationCandidate)
            .order_by(GraduationCandidate.created_at.desc())
            .limit(limit)
        )
        if stage is not None:
            stmt = stmt.where(GraduationCandidate.stage == stage)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_candidate(
        self, session: AsyncSession, candidate_id: UUID
    ) -> GraduationCandidate:
        """Get a single candidate by ID. Raises ValueError if not found."""
        candidate = await session.get(GraduationCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate {candidate_id} not found")
        return candidate

    async def get_transitions(
        self, session: AsyncSession, candidate_id: UUID
    ) -> list[GraduationStageTransition]:
        """Get the full audit trail for a candidate, ordered by creation time."""
        stmt = (
            select(GraduationStageTransition)
            .where(GraduationStageTransition.candidate_id == candidate_id)
            .order_by(GraduationStageTransition.created_at)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    def get_allowed_transitions(self, current_stage: str) -> list[str]:
        """Return sorted list of stages the candidate can move to."""
        return sorted(self.VALID_TRANSITIONS.get(current_stage, set()))
