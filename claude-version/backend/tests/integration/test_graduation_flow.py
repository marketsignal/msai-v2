"""Integration tests for the graduation pipeline stage machine.

Exercises the full GraduationService against a real Postgres container
(via testcontainers) — creating candidates, walking them through valid
transitions, verifying invalid transitions are rejected, and confirming
the immutable audit trail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, Strategy, User
from msai.services.graduation import GraduationService, GraduationStageError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def seed_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> User:
    """Seed a User row so Strategy.created_by FK is satisfied."""
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"grad-{uuid4().hex[:12]}",
            email=f"grad-{uuid4().hex[:8]}@example.com",
            role="trader",
        )
        session.add(user)
        await session.commit()
        return user


@pytest_asyncio.fixture
async def seed_strategy(
    session_factory: async_sessionmaker[AsyncSession],
    seed_user: User,
) -> Strategy:
    """Seed a Strategy row for graduation candidates to reference."""
    async with session_factory() as session:
        strategy = Strategy(
            id=uuid4(),
            name="test_graduation",
            description="Test strategy for graduation flow",
            strategy_class="TestStrategy",
            file_path="strategies/test/graduation.py",
            created_by=seed_user.id,
        )
        session.add(strategy)
        await session.commit()
        return strategy


@pytest.fixture
def grad_service() -> GraduationService:
    return GraduationService()


# ---------------------------------------------------------------------------
# Tests: full graduation pipeline (happy path)
# ---------------------------------------------------------------------------


class TestFullGraduationPipeline:
    """Walk a candidate through the full happy path: discovery -> live_running."""

    async def test_walk_through_all_stages(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """Candidate progresses through every stage to live_running."""
        async with session_factory() as session:
            # Arrange
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={"fast_period": 12},
                metrics={"sharpe_ratio": 1.8},
            )
            await session.flush()
            assert candidate.stage == "discovery"

            # Walk through stages
            stages = [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
                "live_running",
            ]
            for target_stage in stages:
                candidate = await grad_service.update_stage(
                    session,
                    candidate.id,
                    new_stage=target_stage,
                    reason=f"Advancing to {target_stage}",
                )
                await session.flush()
                assert candidate.stage == target_stage

            await session.commit()

            # Verify final state
            final = await grad_service.get_candidate(session, candidate.id)
            assert final.stage == "live_running"

    async def test_audit_trail_records_every_transition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """Every stage change is recorded as an immutable transition row."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={"fast_period": 12},
                metrics={"sharpe_ratio": 1.8},
            )
            await session.flush()

            stages = [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
                "live_running",
            ]
            for target_stage in stages:
                candidate = await grad_service.update_stage(
                    session,
                    candidate.id,
                    new_stage=target_stage,
                    reason=f"Advancing to {target_stage}",
                )
                await session.flush()

            await session.commit()

            # Verify audit trail
            transitions = await grad_service.get_transitions(session, candidate.id)

            # 1 creation transition + 6 stage advances = 7 total
            assert len(transitions) == 7

            # First transition: creation (empty -> discovery)
            assert transitions[0].from_stage == ""
            assert transitions[0].to_stage == "discovery"
            assert transitions[0].reason == "Candidate created"

            # Subsequent transitions follow the stage list
            for i, stage in enumerate(stages):
                t = transitions[i + 1]
                assert t.to_stage == stage
                assert t.reason == f"Advancing to {stage}"

            # Last transition goes to live_running
            assert transitions[-1].to_stage == "live_running"


# ---------------------------------------------------------------------------
# Tests: invalid transitions
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Verify the state machine rejects illegal transitions."""

    async def test_skip_to_live_running_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """Cannot skip from discovery directly to live_running."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()
            assert candidate.stage == "discovery"

            with pytest.raises(GraduationStageError, match="Cannot transition"):
                await grad_service.update_stage(
                    session, candidate.id, new_stage="live_running"
                )

    async def test_archived_is_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """Once archived, no transitions are possible."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()

            # Archive from discovery
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="archived", reason="Not viable"
            )
            await session.flush()
            assert candidate.stage == "archived"

            # Verify no transition out
            with pytest.raises(GraduationStageError, match="terminal state"):
                await grad_service.update_stage(
                    session, candidate.id, new_stage="discovery"
                )

    async def test_nonexistent_candidate_raises_value_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        grad_service: GraduationService,
    ) -> None:
        """Updating a non-existent candidate raises ValueError."""
        async with session_factory() as session:
            with pytest.raises(ValueError, match="not found"):
                await grad_service.update_stage(
                    session, uuid4(), new_stage="validation"
                )


# ---------------------------------------------------------------------------
# Tests: archive from any stage
# ---------------------------------------------------------------------------


class TestArchiveFromAnyStage:
    """Verify archiving works from any non-terminal stage."""

    @pytest.mark.parametrize(
        "pre_stages",
        [
            [],  # archive from discovery
            ["validation"],  # archive from validation
            ["validation", "paper_candidate"],  # archive from paper_candidate
            ["validation", "paper_candidate", "paper_running"],  # from paper_running
            [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
            ],  # from paper_review
            [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
            ],  # from live_candidate
            [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
                "live_running",
            ],  # from live_running
        ],
        ids=[
            "discovery",
            "validation",
            "paper_candidate",
            "paper_running",
            "paper_review",
            "live_candidate",
            "live_running",
        ],
    )
    async def test_archive_from_stage(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
        pre_stages: list[str],
    ) -> None:
        """Archive is reachable from every non-terminal stage."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()

            # Advance to the target stage
            for stage in pre_stages:
                candidate = await grad_service.update_stage(
                    session, candidate.id, new_stage=stage
                )
                await session.flush()

            # Archive
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="archived", reason="Not viable"
            )
            await session.flush()
            assert candidate.stage == "archived"


# ---------------------------------------------------------------------------
# Tests: paper_review loop-back
# ---------------------------------------------------------------------------


class TestPaperReviewLoopBack:
    """Verify paper_review -> discovery loop-back is allowed."""

    async def test_loop_back_to_discovery(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """paper_review can send a candidate back to discovery for more research."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()

            # Walk to paper_review
            for stage in ["validation", "paper_candidate", "paper_running", "paper_review"]:
                candidate = await grad_service.update_stage(
                    session, candidate.id, new_stage=stage
                )
                await session.flush()

            assert candidate.stage == "paper_review"

            # Loop back to discovery
            candidate = await grad_service.update_stage(
                session,
                candidate.id,
                new_stage="discovery",
                reason="Needs more research",
            )
            await session.flush()
            assert candidate.stage == "discovery"

            # Verify it can start the pipeline again
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="validation"
            )
            await session.flush()
            assert candidate.stage == "validation"

    async def test_loop_back_audit_trail_is_preserved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """Loop-back transitions are fully recorded in the audit trail."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()

            for stage in ["validation", "paper_candidate", "paper_running", "paper_review"]:
                candidate = await grad_service.update_stage(
                    session, candidate.id, new_stage=stage
                )
                await session.flush()

            # Loop back
            candidate = await grad_service.update_stage(
                session,
                candidate.id,
                new_stage="discovery",
                reason="Needs more research",
            )
            await session.flush()

            # Re-advance
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="validation"
            )
            await session.commit()

            transitions = await grad_service.get_transitions(session, candidate.id)

            # 1 creation + 4 advances + 1 loop-back + 1 re-advance = 7
            assert len(transitions) == 7

            # The loop-back transition
            loopback = transitions[5]
            assert loopback.from_stage == "paper_review"
            assert loopback.to_stage == "discovery"
            assert loopback.reason == "Needs more research"


# ---------------------------------------------------------------------------
# Tests: paused state
# ---------------------------------------------------------------------------


class TestPausedState:
    """Verify pause/resume behavior for live_running candidates."""

    async def test_pause_and_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """live_running -> paused -> live_running round-trip."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={"fast_period": 12},
                metrics={"sharpe_ratio": 1.8},
            )
            await session.flush()

            # Walk to live_running
            for stage in [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
                "live_running",
            ]:
                candidate = await grad_service.update_stage(
                    session, candidate.id, new_stage=stage
                )
                await session.flush()

            # Pause
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="paused", reason="Market holiday"
            )
            await session.flush()
            assert candidate.stage == "paused"

            # Resume
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="live_running", reason="Market open"
            )
            await session.commit()
            assert candidate.stage == "live_running"

    async def test_paused_can_be_archived(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """A paused candidate can be archived without resuming."""
        async with session_factory() as session:
            candidate = await grad_service.create_candidate(
                session,
                strategy_id=seed_strategy.id,
                config={},
                metrics={},
            )
            await session.flush()

            for stage in [
                "validation",
                "paper_candidate",
                "paper_running",
                "paper_review",
                "live_candidate",
                "live_running",
                "paused",
            ]:
                candidate = await grad_service.update_stage(
                    session, candidate.id, new_stage=stage
                )
                await session.flush()

            # Archive from paused
            candidate = await grad_service.update_stage(
                session, candidate.id, new_stage="archived", reason="Strategy retired"
            )
            await session.commit()
            assert candidate.stage == "archived"


# ---------------------------------------------------------------------------
# Tests: list_candidates
# ---------------------------------------------------------------------------


class TestListCandidates:
    """Verify list_candidates returns correct results from the database."""

    async def test_list_filters_by_stage(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_strategy: Strategy,
        grad_service: GraduationService,
    ) -> None:
        """list_candidates with stage filter returns only matching candidates."""
        async with session_factory() as session:
            # Create two candidates in discovery
            for _ in range(2):
                await grad_service.create_candidate(
                    session,
                    strategy_id=seed_strategy.id,
                    config={},
                    metrics={},
                )
            await session.flush()

            # Advance one to validation
            candidates = await grad_service.list_candidates(session, stage="discovery")
            first = candidates[0]
            await grad_service.update_stage(
                session, first.id, new_stage="validation"
            )
            await session.commit()

            # Filter by discovery — should have one less
            discovery_list = await grad_service.list_candidates(session, stage="discovery")
            validation_list = await grad_service.list_candidates(session, stage="validation")

            assert len(discovery_list) >= 1
            assert len(validation_list) >= 1
            assert all(c.stage == "discovery" for c in discovery_list)
            assert all(c.stage == "validation" for c in validation_list)
